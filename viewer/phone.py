"""
viewer/phone.py - the phone that is plugged in right now.

Lists everything on the connected iPhone (sorted by size, date, ...), shows
thumbnails, and imports only the items you pick into ~/PhotoBackup.

It reuses offload.py for the actual copying (same folder per phone, same
manifest, same SHA-256 check, same .json sidecars), so an item imported here
counts as backed up for offload.py too, and the other way round.

Nothing is ever deleted - not on the phone, not on the Mac.
"""
import asyncio, contextlib, datetime, logging, os, shutil, sqlite3, tempfile, threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

import offload  # ~/photo-offload/offload.py - start uvicorn from ~/photo-offload


router = APIRouter(prefix="/api/phone")
log = logging.getLogger("offload")

# pymobiledevice3 is async: all phone I/O runs on one background event loop,
# and the (sync) FastAPI endpoints hand work to it.
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, name="phone-io", daemon=True).start()


def on_loop(coro, timeout=None):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


def start_on_loop(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)

    def done(f):        # never let a background job die silently and leave us "busy"
        if f.cancelled():
            return
        e = f.exception()
        if e is None:
            return
        log.error("phone background job failed", exc_info=(type(e), e, e.__traceback__))
        if S.state in ("scanning", "importing"):
            if S.job is not None and not S.job.get("finished"):
                S.job.update(finished=True, current=None, error=str(e))
            fail(f"Something went wrong ({type(e).__name__}: {e}).")
    fut.add_done_callback(done)


STATE_LOCK = threading.Lock()       # guards check-then-set of S.state from request threads
PULL_SLOTS = threading.BoundedSemaphore(4)   # phone pulls for thumbnails/previews at once


VIDEO_THUMB_MAX = 150 * 1024**2   # don't pull videos bigger than this just for a thumbnail
SORTS = ("size_desc", "size_asc", "date_desc", "date_asc")
SHOWS = ("new", "done", "all")
KINDS = ("all", "photo", "video", "live", "screenshot")


# ---------------------------------------------------------------- session state
class Session:
    """The one phone we are connected to. Written by the phone-io thread, read by requests."""

    def __init__(self):
        self.state = "idle"        # idle | scanning | ready | importing | error
        self.message = ""
        self.progress = None       # {"done": n, "total": n, "unit": "items"|"bytes"}
        self.device = None         # {"udid", "name", "product_type", "ios"}
        self.dir: Path | None = None
        self.lockdown = None
        self.afc = None
        self.items: list[dict] = []
        self.by_uuid: dict[str, dict] = {}
        self.not_on_phone = 0
        self.job: dict | None = None
        self.cancel = False


S = Session()
NAMES: dict[str, str] = {}         # udid -> phone name, so the picker doesn't reconnect every poll


def public(it: dict) -> dict:
    """Item as the browser sees it (drop the _private fields)."""
    return {k: v for k, v in it.items() if not k.startswith("_")}


def fail(msg: str):
    S.state, S.message, S.progress = "error", msg, None
    log.warning("phone: %s", msg)


async def close_phone():
    if S.afc:
        try:
            await S.afc.aclose()
        except Exception:
            pass
    if S.lockdown:
        try:
            await S.lockdown.close()
        except Exception:
            pass
    S.afc = S.lockdown = None


# ---------------------------------------------------------------- devices
def name_from_backup(udid: str) -> str | None:
    """A phone backed up before has its name in ~/PhotoBackup/<folder>/device.json."""
    found = offload.find_device_dir(udid)
    return found[1].get("name") if found else None


async def list_phones():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    out = []
    for dev in await list_devices():
        if not dev.is_usb:
            continue
        udid = dev.serial
        if udid not in NAMES:
            name = name_from_backup(udid)
            if not name:
                try:  # autopair=False: never pops the Trust dialog just for a name
                    ld = await create_using_usbmux(serial=udid, autopair=False)
                    name = ld.all_values.get("DeviceName")
                    await ld.close()
                except Exception:
                    name = None
            if name:
                NAMES[udid] = name
        out.append({"udid": udid, "name": NAMES.get(udid) or f"iPhone {udid[:8]} (unlock + Trust)"})
    return out


# ---------------------------------------------------------------- scanning
def extra_fields(db_path: Path) -> dict:
    """uuid -> (kind, subtype, duration, size in the database). Empty if the schema differs."""
    db = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        A = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"
        try:
            rows = db.execute(f"""
                SELECT a.ZUUID, a.ZKIND, a.ZKINDSUBTYPE, a.ZDURATION, aa.ZORIGINALFILESIZE
                FROM {A} a LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK""")
        except sqlite3.Error:
            rows = db.execute(f"SELECT ZUUID, ZKIND, NULL, NULL, NULL FROM {A}")
        return {r[0]: r[1:] for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        db.close()


async def group_files(afc, library) -> dict:
    """{(folder, STEM): [file names]} - same folder walk as offload.py, so
    IMG_1234.HEIC and its Live Photo IMG_1234.MOV end up together."""
    groups, unfound = {}, {}
    for folder in sorted({a["src_dir"] for a in library}):
        try:
            names = await afc.listdir(folder)
        except Exception:
            unfound[folder] = {a["filename"] for a in library if a["src_dir"] == folder}
            continue
        for name in names:
            groups.setdefault((folder, Path(name).stem.upper()), []).append(name)
    if unfound:  # e.g. Messages photos stored somewhere other than the folder the db names
        moved = await offload.find_folders(afc, unfound)
        for folder, real in moved.items():
            for name in await afc.listdir(real):
                groups.setdefault((real, Path(name).stem.upper()), []).append(name)
            for a in library:
                if a["src_dir"] == folder:
                    a["src_dir"] = real
    return groups


def local_copy(con, uuid: str, filename: str) -> str | None:
    """Path of the backed-up primary file, if there is one (used for fast thumbnails)."""
    for (dest,) in con.execute("SELECT dest_path FROM files WHERE uuid=?", (uuid,)):
        if Path(dest).suffix.lower() == Path(filename).suffix.lower() and Path(dest).is_file():
            return dest
    return None


async def scan(udid: str):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    S.state, S.message, S.progress, S.job = "scanning", "Connecting to the phone…", None, None
    S.items, S.by_uuid = [], {}
    await close_phone()
    try:
        # pops "Trust This Computer?" on the phone if needed, waits up to 60 s
        ld = await create_using_usbmux(serial=udid, connection_type="USB", pair_timeout=60)
    except Exception as e:
        return fail(f"Can't reach the phone ({type(e).__name__}). Unlock it, tap Trust, then Connect again.")
    v = ld.all_values
    info = {"udid": ld.udid, "name": v.get("DeviceName", "iPhone"),
            "product_type": v.get("ProductType"), "ios": v.get("ProductVersion")}
    NAMES[ld.udid] = info["name"]
    S.lockdown = ld

    try:
        d = offload.device_dir(info)
        afc = AfcService(ld)
        await afc.__aenter__()
        S.afc, S.device, S.dir = afc, info, d

        # 1. snapshot the Photos database (all three files belong together)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        snap = d / "db-snapshots" / f"{stamp}-viewer"
        snap.mkdir(parents=True, exist_ok=True)
        files = []
        for f in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
            if await afc.exists(f"/PhotoData/{f}"):
                files.append((f, (await afc.stat(f"/PhotoData/{f}"))["st_size"]))
        S.message = "Reading the Photos library…"
        S.progress = {"done": 0, "total": sum(s for _, s in files), "unit": "bytes"}

        def bump(n):   # a retry starts the bar again, never past 100 %
            S.progress["done"] = min(S.progress["total"], S.progress["done"] + n)
        await offload.snapshot_db(afc, snap, bump)
        library = await asyncio.to_thread(offload.read_library, snap / "Photos.sqlite")
        extra = await asyncio.to_thread(extra_fields, snap / "Photos.sqlite")

        # 2. which files exist on the phone (iCloud-only items don't)
        S.message, S.progress = "Listing folders…", None
        groups = await group_files(afc, library)
        items, missing = [], 0
        with contextlib.closing(offload.open_manifest(d)) as con:
            for a in library:
                group = groups.get((a["src_dir"], Path(a["filename"]).stem.upper()), [])
                if a["filename"] not in group:
                    missing += 1
                    continue
                companions = offload.companions_for(a["filename"], group)
                kind, subtype, duration, db_size = extra.get(a["uuid"], (None, None, None, None))
                ext = Path(a["filename"]).suffix.lower()
                is_video = kind == 1 if kind is not None else ext in (".mov", ".mp4", ".m4v")
                done = offload.asset_complete(con, a["uuid"])
                items.append({
                    "uuid": a["uuid"],
                    "file": a["filename"],
                    "date": a["date"].isoformat(timespec="seconds"),
                    "size": db_size or 0,       # replaced by the real size below
                    "kind": "video" if is_video else "photo",
                    "live": not is_video and any(n.lower().endswith(".mov") for n in companions[1:]),
                    "screenshot": subtype == 10,
                    "duration": round(duration) if is_video and duration else None,
                    "favorite": a["meta"]["favorite"],
                    "hidden": a["meta"]["hidden"],
                    "source": a["source"],
                    "backed_up": done,
                    "_asset": a,
                    "_companions": companions,
                    "_local": local_copy(con, a["uuid"], a["filename"]) if done else None,
                })

        # 3. real sizes, primary + Live Photo .MOV + edits (.AAE). AFC handles
        #    many requests in flight on one connection, so ask in parallel.
        S.message = "Measuring sizes…"
        S.progress = {"done": 0, "total": len(items), "unit": "items"}
        sem = asyncio.Semaphore(32)

        async def measure(it):
            async with sem:
                try:
                    total = 0
                    for n in it["_companions"]:
                        total += (await afc.stat(f"{it['_asset']['src_dir']}/{n}"))["st_size"]
                    it["size"] = total
                except Exception:
                    log.warning("can't read size on phone: %s", it["file"])
                S.progress["done"] += 1
        await asyncio.gather(*(measure(i) for i in items))

        S.items, S.by_uuid, S.not_on_phone = items, {i["uuid"]: i for i in items}, missing
        S.state, S.message, S.progress = "ready", "", None
    except Exception as e:
        log.exception("phone scan failed")
        fail(f"Reading the phone failed ({type(e).__name__}: {e}). Is it still plugged in and unlocked?")


# ---------------------------------------------------------------- importing
async def phone_alive() -> bool:
    try:
        await asyncio.wait_for(S.afc.listdir("/"), 5)
        return True
    except Exception:
        return False


async def import_items(uuids: list[str]):
    """Never leaves the state stuck on "importing": any error before or after
    the copy loop (drive unplugged, can't create the log...) ends the job."""
    try:
        await _import_items(uuids)
    except Exception as e:
        log.exception("viewer import failed")
        if S.job is not None:
            S.job.update(finished=True, current=None, error=str(e))
        if S.state == "importing":
            S.state = "ready"


async def _import_items(uuids: list[str]):
    picked = [S.by_uuid[u] for u in dict.fromkeys(uuids) if u in S.by_uuid]
    todo = [i for i in picked if not i["backed_up"]]
    need = sum(i["size"] for i in todo)
    margin = max(offload.MIN_FREE_MARGIN, int(need * 0.05))
    job = {"items": len(todo), "done_items": 0, "bytes": need, "done_bytes": 0,
           "already": len(picked) - len(todo), "copied": 0, "duplicate": 0, "failed": 0,
           "current": None, "finished": False, "cancelled": False, "error": None, "log": None,
           "started": datetime.datetime.now().timestamp()}
    S.job, S.cancel = job, False
    free = shutil.disk_usage(offload.backup_root()).free
    if need + margin > free:
        job.update(finished=True, error=f"Not enough space on the Mac: need {offload.human(need + margin)} "
                                        f"(incl. safety margin), {offload.human(free)} free.")
        S.state = "ready"
        return
    if not todo:                      # everything picked is already backed up
        job["finished"], S.state = True, "ready"
        return

    S.state = "importing"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = S.dir / "logs" / f"{stamp}-viewer-import.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    job["log"] = str(log_path)
    log.info("viewer import: %d picked, %d to copy (%s)", len(picked), len(todo), offload.human(need))

    con = None
    try:
        con = offload.open_manifest(S.dir)
        for it in todo:
            if S.cancel:
                job["cancelled"] = True
                break
            job["current"] = it["file"]
            start = job["done_bytes"]

            def on_bytes(n):
                job["done_bytes"] += n
            try:
                status = await offload.backup_asset(S.afc, con, S.dir, it["_asset"], it["_companions"], on_bytes)
                job["duplicate" if status == "duplicate" else "copied"] += 1
                it["backed_up"] = True
                it["_local"] = local_copy(con, it["uuid"], it["file"])
            except Exception:
                job["failed"] += 1
                log.exception("copy failed: %s [%s]", it["_asset"]["meta"]["original_path"], it["source"])
                if not await phone_alive():
                    job["error"] = "The phone was disconnected. Plug it back in and Connect to continue."
                    S.state, S.message = "error", job["error"]
                    break
            job["done_bytes"] = start + it["size"]   # duplicates stop early: keep the bar honest
            job["done_items"] += 1
    finally:
        if con is not None:
            con.close()
        log.info("viewer import done: copied %d, duplicates %d, failed %d, cancelled %s",
                 job["copied"], job["duplicate"], job["failed"], job["cancelled"])
        log.removeHandler(handler)
        handler.close()
        job["current"], job["finished"] = None, True
        if S.state == "importing":
            S.state = "ready"
    # let the backup browser pick up the new files
    from viewer.app import build_index
    try:
        await asyncio.to_thread(build_index)
    except Exception:
        log.exception("rebuilding the backup index failed")


# ---------------------------------------------------------------- api
class ConnectBody(BaseModel):
    udid: str


class ImportBody(BaseModel):
    uuids: list[str]


@router.get("/devices")
def devices():
    try:
        return {"devices": on_loop(list_phones(), timeout=20)}
    except Exception as e:
        return {"devices": [], "error": f"Can't talk to usbmuxd: {type(e).__name__}"}


@router.post("/connect")
def connect(body: ConnectBody):
    with STATE_LOCK:
        if S.state in ("scanning", "importing"):
            raise HTTPException(409, f"busy: {S.state}")
        S.state, S.message = "scanning", "Connecting to the phone…"
    start_on_loop(scan(body.udid))
    return status()


@router.get("/status")
def status():
    items = S.items
    new = [i for i in items if not i["backed_up"]]
    return {
        "state": S.state, "message": S.message, "progress": S.progress,
        "device": S.device, "folder": str(S.dir) if S.dir else None,
        "counts": {"total": len(items), "new": len(new), "done": len(items) - len(new),
                   "new_bytes": sum(i["size"] for i in new),
                   "total_bytes": sum(i["size"] for i in items),
                   "not_on_phone": S.not_on_phone},
        "job": S.job,
    }


def phone_filtered(show: str, kind: str, sort: str) -> list[dict]:
    rows = S.items
    if show == "new":
        rows = [i for i in rows if not i["backed_up"]]
    elif show == "done":
        rows = [i for i in rows if i["backed_up"]]
    if kind == "photo":
        rows = [i for i in rows if i["kind"] == "photo"]
    elif kind == "video":
        rows = [i for i in rows if i["kind"] == "video"]
    elif kind == "live":
        rows = [i for i in rows if i["live"]]
    elif kind == "screenshot":
        rows = [i for i in rows if i["screenshot"]]
    key = (lambda i: i["size"]) if sort.startswith("size") else (lambda i: i["date"])
    return sorted(rows, key=key, reverse=sort.endswith("desc"))


def pattern(values):
    return "^(" + "|".join(values) + ")$"


@router.get("/items")
def items(show: str = Query("new", pattern=pattern(SHOWS)),
          kind: str = Query("all", pattern=pattern(KINDS)),
          sort: str = Query("date_desc", pattern=pattern(SORTS)),
          offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=500)):
    rows = phone_filtered(show, kind, sort)
    return {"total": len(rows), "bytes": sum(i["size"] for i in rows), "offset": offset,
            "items": [public(i) for i in rows[offset:offset + limit]]}


@router.get("/ids")
def ids(show: str = Query("new", pattern=pattern(SHOWS)),
        kind: str = Query("all", pattern=pattern(KINDS))):
    """Every uuid + size matching the filter - for 'Select all'."""
    return {"items": [{"uuid": i["uuid"], "size": i["size"], "backed_up": i["backed_up"]}
                      for i in phone_filtered(show, kind, "size_desc")]}


@router.post("/import")
def start_import(body: ImportBody):
    if not body.uuids:
        raise HTTPException(400, "nothing selected")
    with STATE_LOCK:
        if S.state != "ready":
            raise HTTPException(409, f"can't import while {S.state}")
        S.state, S.job = "importing", None   # set now so a double click can't start two jobs
    start_on_loop(import_items(body.uuids))
    return {"ok": True}


@router.post("/cancel")
def cancel():
    S.cancel = True     # stops after the file being copied, never half-way
    return {"ok": True}


# ---------------------------------------------------------------- thumbnails
def item_or_404(uuid: str) -> dict:
    it = S.by_uuid.get(uuid)
    if not it:
        raise HTTPException(404)
    return it


async def pull_for_image(it: dict, want_original: bool) -> Path | None:
    """Copy a small source image off the phone into the cache's tmp folder.
    First choice for thumbnails: the phone's own thumbnail cache
    (/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/5005.JPG) - tiny and fast.
    Otherwise the original (photos, and videos up to VIDEO_THUMB_MAX)."""
    from viewer.app import cache_dir
    afc, src_dir = S.afc, it["_asset"]["src_dir"]
    src = None
    if not want_original:
        tdir = f"/PhotoData/Thumbnails/V2{src_dir}/{it['file']}"
        try:
            jpgs = sorted(n for n in await afc.listdir(tdir) if n.upper().endswith(".JPG"))
            if jpgs:
                src = f"{tdir}/{'5005.JPG' if '5005.JPG' in jpgs else jpgs[-1]}"
        except Exception:
            pass
    if src is None:
        if it["kind"] == "video" and it["size"] > VIDEO_THUMB_MAX:
            return None
        src = f"{src_dir}/{it['file']}"
    tmp_dir = cache_dir() / "phone-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # unique name: a thumbnail and a preview of the same item can run at once
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix=f"{it['uuid']}-", suffix=Path(src).suffix)
    os.close(fd)
    tmp = Path(name)
    try:
        await offload.stream_copy(afc, src, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def phone_image(uuid: str, px: int, folder: str, want_original: bool):
    from viewer.app import cache_dir, make_image, _lock, CACHE_HEADERS
    it = item_or_404(uuid)
    dest = cache_dir() / folder / S.device["udid"][:8] / f"{uuid}.jpg"
    if dest.is_file():
        return FileResponse(dest, media_type="image/jpeg", headers=CACHE_HEADERS)
    with _lock("phone:" + str(dest)):
        if not dest.is_file():
            ok = False
            if it.get("_local"):                       # already backed up: read it from the Mac
                ok = make_image(Path(it["_local"]), dest, px)
            if not ok and S.afc:
                tmp = None
                # at most 4 phone pulls at once, so a full grid can't take every
                # worker thread and starve /status and /cancel
                if PULL_SLOTS.acquire(timeout=60):
                    fut = asyncio.run_coroutine_threadsafe(pull_for_image(it, want_original), LOOP)
                    try:
                        tmp = fut.result(timeout=180)
                    except Exception:
                        fut.cancel()
                    finally:
                        PULL_SLOTS.release()
                if tmp:
                    try:
                        ok = make_image(tmp, dest, px)
                    finally:
                        tmp.unlink(missing_ok=True)
            if not ok:
                raise HTTPException(404, "no preview")
    return FileResponse(dest, media_type="image/jpeg", headers=CACHE_HEADERS)


@router.get("/thumb/{uuid}")
def thumb(uuid: str):
    from viewer.app import THUMB_PX
    return phone_image(uuid, THUMB_PX, "phone-thumbs", want_original=False)


@router.get("/preview/{uuid}")
def preview(uuid: str):
    from viewer.app import PREVIEW_PX
    it = item_or_404(uuid)
    if it["kind"] == "video":       # a big poster frame is enough; no video streaming from the phone
        return phone_image(uuid, PREVIEW_PX, "phone-previews", want_original=False)
    return phone_image(uuid, PREVIEW_PX, "phone-previews", want_original=True)
