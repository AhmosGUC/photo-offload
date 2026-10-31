#!/usr/bin/env python3
"""
offload.py - incremental, idempotent iPhone photo/video backup over USB.

  python offload.py --dry-run          # checks + size estimate, copies nothing
  python offload.py --limit 20         # copy only the 20 newest items (testing)
  python offload.py                    # full run (asks before copying)
  python offload.py --yes              # full run, no confirmation prompt

Each phone gets its own folder under ~/PhotoBackup, found by UDID.
Re-running only copies what is new. Nothing is ever deleted - from the
phone or from the backup.
"""
import argparse, asyncio, collections, datetime, hashlib, json, logging, os, re, shutil, sqlite3, sys
from pathlib import Path

from tqdm import tqdm

import config

def backup_root() -> Path:
    return config.get_backup_root()

CHUNK = 4 * 1024 * 1024
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds; Photos stores dates from here
MIN_FREE_MARGIN = 2 * 1024**3  # always leave at least 2 GB free on the Mac
SPACE_CHECK_EVERY = 25         # re-check free space every N items while copying
COMPANION_EXTS = {".MOV", ".AAE"}  # only these count as siblings of a primary file

# Per-item problems go to <phone folder>/logs/<run>.log, never to the console.
# The console only shows counts and where the log is.
log = logging.getLogger("offload")
LOG_PATH = None


def start_log(d: Path, stamp: str) -> Path:
    global LOG_PATH
    LOG_PATH = d / "logs" / f"{stamp}.log"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # force=True replaces any console handler, so library warnings land in the file too
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    logging.captureWarnings(True)
    return LOG_PATH


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


class StatusLine:
    """A second console line under the progress bar with live counts.
    Redraws only when the text changes, so it costs nothing per item."""
    def __init__(self):
        self.bar = tqdm(total=0, bar_format="  {desc}", position=1, leave=False, dynamic_ncols=True)
        self.text = None

    def show(self, text: str):
        if text != self.text:
            self.text = text
            self.bar.set_description_str(text)

    def close(self):
        self.bar.close()


def write_atomic(p: Path, text: str):
    """Write via a .tmp file + rename, so an interruption never leaves half a file."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


# ---------------------------------------------------------------- device folder
def find_device_dir(udid: str) -> tuple[Path, dict] | None:
    """(folder, device.json contents) of the phone with this UDID, if backed up before.
    A corrupt device.json only skips that one folder."""
    root = backup_root()
    if not root.is_dir():
        return None
    for d in root.iterdir():
        f = d / "device.json"
        if not f.is_file():
            continue
        try:
            info = json.loads(f.read_text())
        except (ValueError, OSError):
            log.warning("unreadable device.json, skipped: %s", f)
            continue
        if isinstance(info, dict) and info.get("udid") == udid:
            return d, info
    return None


def device_dir(info: dict) -> Path:
    """Find this phone's folder by UDID (survives renames); create it if new."""
    backup_root().mkdir(parents=True, exist_ok=True)
    udid = info["udid"]
    found = find_device_dir(udid)
    if found:
        d = found[0]
    else:
        safe = re.sub(r"[^\w\-]+", "-", info["name"]).strip("-") or "iPhone"
        d = backup_root() / f"{safe}__{udid[:8]}"
        d.mkdir(exist_ok=True)
    write_atomic(d / "device.json", json.dumps(info, indent=2, ensure_ascii=False))
    return d


# ---------------------------------------------------------------- manifest
def open_manifest(d: Path) -> sqlite3.Connection:
    con = sqlite3.connect(d / "manifest.sqlite")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS assets (
        uuid TEXT PRIMARY KEY, dest_dir TEXT, dest_stem TEXT,
        meta_hash TEXT, status TEXT, duplicate_of TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS files (
        src_path TEXT PRIMARY KEY, uuid TEXT, dest_path TEXT,
        size INTEGER, sha256 TEXT);
    CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
    CREATE INDEX IF NOT EXISTS files_uuid ON files(uuid);
    """)
    return con


# ---------------------------------------------------------------- phone database
def table_columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def asset_source(directory: str, bundle_scope) -> str:
    """Where the phone keeps this item - also written to the .json sidecar."""
    if directory.startswith("DCIM/"):
        return "camera_roll"            # taken or saved on this phone
    if directory.startswith("PhotoData/Sync/"):
        return "synced_from_computer"   # put there by Finder/iTunes sync
    if "PhotoCloudSharingData" in directory:
        return "shared_album"           # iCloud Shared Albums
    if bundle_scope == 3:
        return "messages"               # photos shared with you in Messages
    return "other"


def asset_kind(subtype, saved_type):
    """screenshot / camera / saved - see SPEC_media_kind_classification.md.
    Codes come from Photos.sqlite and may shift between iOS versions, so the raw
    values are kept in the sidecar too (kind_codes) for re-checking per phone."""
    if subtype == 10:
        return "screenshot"         # incl. the few screenshots that arrived from outside
    if saved_type is None:
        return None                 # older iOS without ZSAVEDASSETTYPE: unknown
    if saved_type == 3:
        return "camera"             # created on this phone
    return "saved"                  # saved/imported from outside (WhatsApp, Safari, AirDrop...)


def read_library(db_path: Path):
    """Return every photo/video the phone stores (not in Recently Deleted), oldest first."""
    db = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    A = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"
    cols = table_columns(db, A)
    hidden = "a.ZHIDDEN" if "ZHIDDEN" in cols else "0"
    scope = "a.ZBUNDLESCOPE" if "ZBUNDLESCOPE" in cols else "0"
    subtype = "a.ZKINDSUBTYPE" if "ZKINDSUBTYPE" in cols else "NULL"
    saved = "a.ZSAVEDASSETTYPE" if "ZSAVEDASSETTYPE" in cols else "NULL"

    assets = db.execute(f"""
        SELECT a.Z_PK, a.ZUUID, a.ZDIRECTORY, a.ZFILENAME, a.ZDATECREATED,
               a.ZLATITUDE, a.ZLONGITUDE, a.ZFAVORITE, {hidden}, {scope},
               {subtype}, {saved}
        FROM {A} a
        WHERE a.ZTRASHEDSTATE = 0 AND a.ZDIRECTORY IS NOT NULL AND a.ZFILENAME IS NOT NULL
        ORDER BY a.ZDATECREATED""").fetchall()

    # Albums: the link table is Z_<n>ASSETS, with columns Z_<n>ALBUMS and Z_<m>ASSETS.
    # The numbers differ between iOS versions, so discover them.
    albums = {}
    for t in tables:
        if not re.fullmatch(r"Z_\d+ASSETS", t):
            continue
        cols = table_columns(db, t)
        alb = next((c for c in cols if re.fullmatch(r"Z_\d+ALBUMS", c)), None)
        ast = next((c for c in cols if re.fullmatch(r"Z_\d+ASSETS", c)), None)
        if not (alb and ast):
            continue
        for asset_pk, title in db.execute(f"""
                SELECT j.{ast}, g.ZTITLE FROM {t} j
                JOIN ZGENERICALBUM g ON g.Z_PK = j.{alb}
                WHERE g.ZKIND IN (2, 1505) AND g.ZTITLE IS NOT NULL
                  AND COALESCE(g.ZTRASHEDSTATE, 0) = 0"""):
            albums.setdefault(asset_pk, []).append(title)

    # Captions live in a side table on newer iOS; skip quietly if the schema differs.
    captions = {}
    try:
        captions = dict(db.execute("""
            SELECT aa.ZASSET, d.ZLONGDESCRIPTION
            FROM ZADDITIONALASSETATTRIBUTES aa
            JOIN ZASSETDESCRIPTION d ON d.ZASSETATTRIBUTES = aa.Z_PK
            WHERE d.ZLONGDESCRIPTION IS NOT NULL AND d.ZLONGDESCRIPTION != ''"""))
    except sqlite3.Error:
        pass
    db.close()

    out = []
    for pk, uuid, directory, filename, created, lat, lon, fav, hid, scp, sub, sav in assets:
        source = asset_source(directory, scp)
        if created is None:
            log.warning("no date in Photos database, using 2001-01-01: /%s/%s", directory, filename)
        when = datetime.datetime.fromtimestamp(APPLE_EPOCH + (created or 0))
        when_utc = datetime.datetime.fromtimestamp(APPLE_EPOCH + (created or 0), datetime.timezone.utc)
        has_loc = lat is not None and lat != -180 and lon != -180
        out.append({
            "uuid": uuid,
            "src_dir": "/" + directory,
            "source": source,
            "filename": filename,
            "date": when,
            "meta": {
                "uuid": uuid,
                "original_path": f"/{directory}/{filename}",
                "source": source,
                "kind": asset_kind(sub, sav),
                "kind_codes": {"subtype": sub, "saved_type": sav},
                "date_taken": when.isoformat(),
                "date_taken_utc": when_utc.isoformat(),
                "latitude": lat if has_loc else None,
                "longitude": lon if has_loc else None,
                "favorite": bool(fav),
                "hidden": bool(hid),
                "albums": sorted(albums.get(pk, [])),
                "caption": captions.get(pk),
            },
        })
    return out


# ---------------------------------------------------------------- finding files
SKIP_DIRS = {"Thumbnails", "Metadata", "Mutations", "CPLAssets", "PhotoCloudSharingData",
             "Sync", "Caches", "Journals", "MISC", "private", "DCIM"}


async def find_folders(afc, wanted: dict) -> dict:
    """wanted: {db_folder: set(filenames)} for folders not found at '/<db_folder>'.
    Walks /PhotoData (skipping the big bookkeeping folders) and returns
    {db_folder: real_folder} for each folder whose files turn up somewhere."""
    found, queue = {}, collections.deque([("/PhotoData", 0)])
    while queue and len(found) < len(wanted):
        path, depth = queue.popleft()
        try:
            names = await afc.listdir(path)
        except Exception:
            continue
        for db_folder, files in wanted.items():
            if db_folder not in found and path.endswith("/" + db_folder.strip("/")) \
                    and files & set(names):
                found[db_folder] = path
        if depth < 5:
            for n in names:
                if n not in SKIP_DIRS and "." not in n:
                    queue.append((f"{path}/{n}", depth + 1))
    return found


# ---------------------------------------------------------------- copying
async def stream_copy(afc, src: str, dest: Path, on_bytes=None) -> tuple[int, str]:
    """Copy one phone file to dest via a .part file. Returns (size, sha256)."""
    part = dest.with_name(dest.name + ".part")
    h, size = hashlib.sha256(), 0
    handle = await afc.fopen(src, "r")
    try:
        try:
            with open(part, "wb") as f:
                while True:
                    chunk = await afc.fread(handle, CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
                f.flush()
                os.fsync(f.fileno())
        finally:
            try:  # a failing fclose (phone gone) must not hide the original error
                await afc.fclose(handle)
            except Exception:
                log.debug("fclose failed for %s", src, exc_info=True)
        expected = (await afc.stat(src))["st_size"]
        if size != expected:
            raise IOError(f"size mismatch for {src}: got {size}, phone says {expected}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)  # atomic: a file only appears under its real name when complete
    return size, h.hexdigest()


def companions_for(filename: str, group: list[str]) -> list[str]:
    """Primary file first, then only its .MOV (Live Photo) / .AAE (edits) siblings.
    IMG_1.JPG and IMG_1.HEIC are two separate assets, not companions."""
    return [filename] + sorted(n for n in group
                               if n != filename and Path(n).suffix.upper() in COMPANION_EXTS)


async def snapshot_db(afc, snap: Path, on_bytes=None, tries: int = 3):
    """Copy Photos.sqlite + -wal + -shm. The WAL can grow mid-copy (size check fails)
    or the three files can be out of step; retry the whole set and check the result."""
    names = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")
    for attempt in range(1, tries + 1):
        try:
            for f in names:
                (snap / f).unlink(missing_ok=True)
            for f in names:
                if await afc.exists(f"/PhotoData/{f}"):
                    await stream_copy(afc, f"/PhotoData/{f}", snap / f, on_bytes)
            db = sqlite3.connect(snap / "Photos.sqlite")
            try:
                ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                db.close()
            if ok:
                return
            err = "quick_check failed"
        except (IOError, sqlite3.Error) as e:
            err = str(e)
        log.warning("Photos database snapshot attempt %d/%d failed: %s", attempt, tries, err)
        if attempt < tries:
            await asyncio.sleep(1)
    raise IOError(f"couldn't take a consistent copy of the Photos database after {tries} tries")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_stem(con, dest_dir: Path, filename: str, uuid: str) -> str:
    """IMG_0001 repeats after 9999 - add a short UUID if the name is taken."""
    stem = Path(filename).stem
    # compare names ourselves: glob treats [ ] * ? as wildcards, and the
    # backup drive is usually case-insensitive while glob is not
    prefix = stem.lower() + "."
    try:
        on_disk = any(n.lower().startswith(prefix) for n in os.listdir(dest_dir))
    except FileNotFoundError:
        on_disk = False
    taken = on_disk or con.execute(
        "SELECT 1 FROM assets WHERE dest_dir=? AND dest_stem=? AND uuid!=?",
        (str(dest_dir), stem, uuid)).fetchone()
    return f"{stem}_{uuid[:8]}" if taken else stem


def asset_complete(con, uuid, _seen=None) -> bool:
    row = con.execute("SELECT status, duplicate_of FROM assets WHERE uuid=?", (uuid,)).fetchone()
    if not row or row[0] not in ("verified", "duplicate"):
        return False
    if row[0] == "duplicate":
        # a duplicate has no files of its own: it is only safe if the original is
        seen = _seen or set()
        if not row[1] or row[1] in seen or row[1] == uuid:
            return False
        seen.add(uuid)
        return asset_complete(con, row[1], seen)
    for dest, size in con.execute("SELECT dest_path, size FROM files WHERE uuid=?", (uuid,)):
        p = Path(dest)
        if not p.is_file() or p.stat().st_size != size:
            return False
    return True


def write_meta(con, dest_dir: Path, stem: str, primary_ext: str, asset):
    meta = json.dumps(asset["meta"], indent=2, ensure_ascii=False, sort_keys=True)
    mh = hashlib.sha256(meta.encode()).hexdigest()
    row = con.execute("SELECT meta_hash FROM assets WHERE uuid=?", (asset["uuid"],)).fetchone()
    sidecar = dest_dir / f"{stem}{primary_ext}.json"
    if row and row[0] == mh and sidecar.is_file():
        return False
    write_atomic(sidecar, meta)
    con.execute("UPDATE assets SET meta_hash=? WHERE uuid=?", (mh, asset["uuid"]))
    return True


async def backup_asset(afc, con, d: Path, asset, companions, on_bytes=None):
    uuid = asset["uuid"]
    row = con.execute("SELECT dest_dir, dest_stem FROM assets WHERE uuid=?", (uuid,)).fetchone()
    if row:
        dest_dir, stem = Path(row[0]), row[1]
    else:
        dest_dir = d / "media"  # one flat folder per phone; sort/filter by date
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = pick_stem(con, dest_dir, asset["filename"], uuid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO assets(uuid, dest_dir, dest_stem, status, updated_at)
                   VALUES (?,?,?,'copying',?)
                   ON CONFLICT(uuid) DO UPDATE SET status='copying', updated_at=excluded.updated_at""",
                (uuid, str(dest_dir), stem, now))
    con.commit()

    duplicate_of = None
    for name in companions:  # primary file first, then .MOV / .AAE siblings
        src = f"{asset['src_dir']}/{name}"
        dest = dest_dir / (stem + Path(name).suffix)
        size, sha = await stream_copy(afc, src, dest, on_bytes)
        if sha256_file(dest) != sha:
            dest.unlink(missing_ok=True)
            raise IOError(f"verification failed for {dest}")
        if name == companions[0]:
            dup = None
            for d_uuid, d_dest, d_size in con.execute(
                    "SELECT uuid, dest_path, size FROM files WHERE sha256=? AND uuid!=?", (sha, uuid)):
                p = Path(d_dest)
                if p.is_file() and p.stat().st_size == d_size:  # only trust a copy that is really there
                    dup = (d_uuid,)
                    break
            if dup:  # same bytes already backed up under another UUID
                dest.unlink()
                duplicate_of = dup[0]
                break
        ts = asset["date"].timestamp()
        os.utime(dest, (ts, ts))  # file date = date taken, so Finder sorts correctly
        other = con.execute("SELECT uuid FROM files WHERE src_path=? AND uuid!=?", (src, uuid)).fetchone()
        if other:  # never take over another asset's file row
            # skipping silently would let this asset pass as verified with no file to check
            log.warning("source file %s already belongs to asset %s; not recorded for %s", src, other[0], uuid)
            raise IOError(f"{src} is already recorded for another asset ({other[0]})")
        con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                    (src, uuid, str(dest), size, sha))

    status = "duplicate" if duplicate_of else "verified"
    con.execute("UPDATE assets SET status=?, duplicate_of=? WHERE uuid=?",
                (status, duplicate_of, uuid))
    if not duplicate_of:
        write_meta(con, dest_dir, stem, Path(companions[0]).suffix, asset)
    con.commit()
    return status


# ---------------------------------------------------------------- main
async def main(args):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    # ---- check 1: phone connected, unlocked and trusted
    try:
        lockdown = await create_using_usbmux(connection_type="USB")
    except Exception as e:
        print(f"✗ Can't reach the phone: {e}\n  Plug it in, unlock it, tap 'Trust', then retry.")
        return 1
    v = lockdown.all_values
    info = {"udid": lockdown.udid, "name": v.get("DeviceName", "iPhone"),
            "product_type": v.get("ProductType"), "ios": v.get("ProductVersion")}
    d = device_dir(info)
    print(f"✓ Phone: {info['name']} ({info['product_type']}, iOS {info['ios']})")
    print(f"  Backup folder: {d}")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    print(f"  Log for this run: {start_log(d, stamp)}")
    log.info("run started: %s  phone=%s", " ".join(sys.argv[1:]) or "(full run)", info)

    async with AfcService(lockdown) as afc:
        # Snapshot the Photos database (all three files belong together)
        snap = d / "db-snapshots" / stamp
        snap.mkdir(parents=True, exist_ok=True)
        await snapshot_db(afc, snap)
        library = read_library(snap / "Photos.sqlite")
        if args.limit:
            library = library[-args.limit:]
        by_src = {}
        for a in library:
            by_src[a["source"]] = by_src.get(a["source"], 0) + 1
        print(f"✓ Photos library read: {len(library)} items on the phone")
        print("  " + "  ".join(f"{k}: {v}" for k, v in sorted(by_src.items(), key=lambda x: -x[1])))

        # List every source folder once, grouping files by folder + stem
        # (IMG_1234.HEIC + IMG_1234.MOV). Folders: DCIM/1xxAPPLE and PhotoData/Sync/1xxSYNCD.
        dcim, unfound = {}, {}
        for folder in sorted({a["src_dir"] for a in library}):
            try:
                names = await afc.listdir(folder)
            except Exception:
                unfound[folder] = {a["filename"] for a in library if a["src_dir"] == folder}
                continue
            for name in names:
                dcim.setdefault((folder, Path(name).stem.upper()), []).append(name)
        # Some items (e.g. from Messages) are stored somewhere other than the
        # folder the database names - search for them once.
        if unfound:
            moved = await find_folders(afc, unfound)
            for folder, real in moved.items():
                for name in await afc.listdir(real):
                    dcim.setdefault((real, Path(name).stem.upper()), []).append(name)
                for a in library:
                    if a["src_dir"] == folder:
                        a["src_dir"] = real
            for folder in sorted(set(unfound) - set(moved)):
                log.warning("folder not reachable over USB: %s (%d items)", folder, len(unfound[folder]))

        # ---- check 2: work out what's new and how big it is
        con = open_manifest(d)
        todo, skipped, missing, meta_updated = [], 0, 0, 0
        errors = {"check": 0, "metadata": 0}
        live = StatusLine()
        for asset in tqdm(library, desc="Checking", unit="item", leave=False, position=0):
            live.show(f"Already backed up {skipped} · Not on phone {missing} · "
                      f"Unreadable {errors['check']} · Metadata errors {errors['metadata']}")
            if asset_complete(con, asset["uuid"]):
                skipped += 1
                row = con.execute("SELECT dest_dir, dest_stem, status FROM assets WHERE uuid=?",
                                  (asset["uuid"],)).fetchone()
                if row[2] == "verified" and not args.dry_run:
                    try:
                        if write_meta(con, Path(row[0]), row[1], Path(asset["filename"]).suffix, asset):
                            meta_updated += 1
                            con.commit()
                    except Exception:
                        errors["metadata"] += 1
                        log.exception("metadata update failed: %s", asset["meta"]["original_path"])
                continue
            group = dcim.get((asset["src_dir"], Path(asset["filename"]).stem.upper()), [])
            if asset["filename"] not in group:
                missing += 1
                log.warning("not on phone (iCloud only?): %s [%s]",
                            asset["meta"]["original_path"], asset["source"])
                continue
            companions = companions_for(asset["filename"], group)
            try:
                size = 0
                for n in companions:
                    size += (await afc.stat(f"{asset['src_dir']}/{n}"))["st_size"]
            except Exception:
                errors["check"] += 1
                log.exception("can't read size on phone: %s/%s", asset["src_dir"], asset["filename"])
                continue
            todo.append((asset, companions, size))

        live.close()
        need = sum(t[2] for t in todo)
        free = shutil.disk_usage(backup_root()).free
        margin = max(MIN_FREE_MARGIN, int(need * 0.05))
        print(f"✓ Already backed up: {skipped}   Not on phone: {missing}"
              + (f"   Unreadable: {errors['check']}" if errors["check"] else ""))
        log.info("checked %d: already backed up %d, not on phone %d, unreadable %d, to copy %d (%s)",
                 len(library), skipped, missing, errors["check"], len(todo),
                 human(sum(t[2] for t in todo)))
        print(f"  To copy: {len(todo)} items, {human(need)}")
        print(f"  Free on Mac: {human(free)}  (need {human(need + margin)} incl. safety margin)")

        # ---- check 3: enough disk space
        enough = need + margin <= free
        print(("✓ Enough space" if enough else "✗ NOT enough space on the Mac"))

        if args.dry_run or not enough:
            con.close()
            if args.dry_run:
                print("\n(dry run: nothing copied)")
                if missing or errors["check"]:
                    print(f"Details of the {missing + errors['check']} skipped items: {LOG_PATH}")
            return 0 if enough else 1
        if not todo:
            print("\nNothing new to copy.")

        # ---- check 4: confirm
        if todo and not args.yes:
            if input(f"\nCopy {len(todo)} items ({human(need)})? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                con.close()
                return 0

        # ---- copy, with a byte-based progress bar
        counts = {"copied": 0, "duplicate": 0, "failed": 0}
        last_error = ""
        in_a_row, stopped = 0, None
        live = StatusLine() if todo else None
        with tqdm(total=need, unit="B", unit_scale=True, unit_divisor=1024, position=0,
                  desc="Copying", dynamic_ncols=True, disable=not todo) as bar:
            for i, (asset, companions, size) in enumerate(todo, 1):
                if i > 1 and i % SPACE_CHECK_EVERY == 0 and \
                        shutil.disk_usage(backup_root()).free < MIN_FREE_MARGIN + size:
                    stopped = "backup drive is almost full - free some space, then re-run to resume"
                    break
                bar.set_postfix_str(f"{i}/{len(todo)} {asset['filename']}")
                live.show(f"Copied {counts['copied']} · Duplicates {counts['duplicate']} · "
                          f"Failed {counts['failed']}{last_error}")
                start = bar.n
                try:
                    status = await backup_asset(afc, con, d, asset, companions, bar.update)
                    counts["duplicate" if status == "duplicate" else "copied"] += 1
                    in_a_row = 0
                except Exception as e:
                    counts["failed"] += 1
                    in_a_row += 1
                    last_error = f" (last: {asset['filename']} - see log)"
                    log.exception("copy failed: %s [%s]", asset["meta"]["original_path"], asset["source"])
                    if isinstance(e, (ConnectionError, BrokenPipeError)) or in_a_row >= 3:
                        stopped = "phone disconnected — re-run to resume"
                        break
                # duplicates/failures stop early: top the bar up so it stays accurate
                bar.update(max(0, size - (bar.n - start)))
        if live:
            live.close()
        if stopped:
            log.warning("copy stopped early: %s", stopped)
            print(f"\n✗ Stopped: {stopped}")

        # Report: the latest date up to which *everything* is safely backed up
        through = None
        for asset in library:
            if asset["source"] != "camera_roll":
                continue  # the safe date is about what you delete from the camera roll
            if not asset_complete(con, asset["uuid"]):
                break
            through = asset["date"]
        con.close()

    problems = counts["failed"] + errors["check"] + errors["metadata"] + bool(stopped)
    print(f"\nCopied: {counts['copied']}  Skipped (already done): {skipped}  "
          f"Duplicates: {counts['duplicate']}  Metadata refreshed: {meta_updated}")
    print(f"Failed: {counts['failed']}  Unreadable: {errors['check']}  "
          f"Metadata errors: {errors['metadata']}  Not on phone: {missing}")
    log.info("done: copied %d, skipped %d, duplicates %d, metadata refreshed %d, failed %d, "
             "unreadable %d, metadata errors %d, not on phone %d", counts["copied"], skipped,
             counts["duplicate"], meta_updated, counts["failed"], errors["check"],
             errors["metadata"], missing)
    if problems or missing:
        print(f"Details: {LOG_PATH}")
    if through and not args.limit:
        print(f"Everything taken up to {through:%Y-%m-%d %H:%M} is verified and safe to delete.")
    elif args.limit:
        print("(--limit run: no 'safe to delete' date, older items weren't checked)")
    return 1 if problems else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, help="only process the N newest items")
    p.add_argument("--dry-run", action="store_true", help="run all checks + size estimate, copy nothing")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation before copying")
    try:
        sys.exit(asyncio.run(main(p.parse_args())))
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        print("\nStopped. Re-run to continue where it left off.")
        sys.exit(130)
    except Exception as e:
        log.exception("run crashed")
        print(f"\n✗ Stopped by an unexpected error: {type(e).__name__}"
              + (f"\n  Details: {LOG_PATH}" if LOG_PATH else f": {e}"))
        sys.exit(1)
