# Photo Backup – Developer Guide

Personal macOS tool that backs up an iPhone's photos/videos over USB (via `pymobiledevice3`) into a per-phone folder, keeps metadata in JSON sidecars, and ships a local web viewer (FastAPI + plain JS) that can also browse the plugged-in phone and import selected items. A PyInstaller `.app` wraps the viewer in a pywebview window.

Everything described here was read from the source in `photo-offload/` (excluding `_archive/`, `build/`, `dist/`, `.venv/`, `probe/`). Older copies live in `_archive/pre-review/` (code before the review fixes) and `_archive/bak/` (former `*.bak-*` files). Remaining known issues are listed in [section 11](#11-known-issues--inconsistencies).

---

## 1. Architecture and data flow

```
 iPhone (USB, usbmuxd + lockdown + AFC)
   │  /PhotoData/Photos.sqlite(+ -wal, -shm)   ── snapshot ──►  <phone>/db-snapshots/<stamp>[-viewer]/
   │  /DCIM/1xxAPPLE/*, /PhotoData/Sync/..., Messages folders
   │  /PhotoData/Thumbnails/V2/...  (thumbnails only, used by phone page)
   ▼
 offload.py  (CLI)                    viewer/phone.py  (/api/phone/*, background asyncio loop)
   read_library(snapshot)  ◄────────── reuses offload.read_library / find_folders /
   backup_asset() per item             stream_copy / backup_asset / open_manifest / asset_complete
   ▼
 <backup_root>/<Name>__<udid8>/
   device.json, manifest.sqlite, media/*, media/*.json, logs/*.log, db-snapshots/*
   ▼
 viewer/app.py   build_index() scans */device.json + media/*.json into in-memory INDEX
   /api/summary, /api/items, /thumb, /preview, /media  ──►  static/index.html + app.js
   /api/phone/*                                       ──►  static/phone.html + phone.js
   ▲
 launcher.py  starts uvicorn on a free 127.0.0.1 port in a thread, opens pywebview window at /phone.html
```

Key properties (from code):
- Phone folder is matched by UDID in `device.json`, so renaming the phone keeps the folder.
- Idempotent: `manifest.sqlite` records each asset; `asset_complete()` re-checks files exist with recorded sizes.
- Nothing is ever deleted on the phone; backups are only deleted in failure paths (`.part` on size mismatch, a dest file failing SHA verification, or a primary file found to be a byte-duplicate of another UUID).
- Copies go to `<dest>.part`, are fsync'd, size-checked against AFC stat, then `os.replace`d; then re-hashed from disk and compared. Any failure (including cancellation) deletes the `.part`.
- Small metadata files (`device.json`, sidecars, `config.json`) are written via `<name>.tmp` + `os.replace`, never half-written.
- The Photos DB snapshot is opened read-only (`file:...?mode=ro`).

---

## 2. On-disk layout

### 2.1 App config (`config.py`)
`~/Library/Application Support/Photo Backup/config.json`:
```json
{ "backup_root": "/Volumes/Drive/PhotoBackup",
  "prefs": { "auto_live": true, "muted": false } }
```
If `backup_root` is absent, the default `~/PhotoBackup` is used and `is_configured()` is `False`.

### 2.2 Backup root
```
<backup_root>/
  .viewer-cache/                      # viewer/app.py cache_dir(); safe to delete
    thumbs/<phone_id>/<file>.jpg      # 400 px (THUMB_PX)
    previews/<phone_id>/<file>.jpg    # 2048 px (PREVIEW_PX), non-JPG/PNG/GIF/WEBP only
    phone-thumbs/<udid[:8]>/<uuid>.jpg
    phone-previews/<udid[:8]>/<uuid>.jpg
    phone-tmp/<uuid>-<random><ext>    # transient pulls (tempfile.mkstemp), unlinked after conversion or on error
  <SafeName>__<udid[:8]>/             # created by offload.device_dir()
    device.json
    manifest.sqlite
    db-snapshots/<YYYY-MM-DD_HHMMSS>/          # offload.py runs
    db-snapshots/<YYYY-MM-DD_HHMMSS>-viewer/   # phone page scans
        Photos.sqlite, Photos.sqlite-wal, Photos.sqlite-shm (each only if present on phone)
    logs/<YYYY-MM-DD_HHMMSS>.log               # offload.py run log
    logs/<YYYY-MM-DD_HHMMSS>-viewer-import.log # phone page import log
    media/                                     # one flat folder
      IMG_1234.HEIC
      IMG_1234.MOV          # companion (Live Photo video), same stem
      IMG_1234.AAE          # companion (edits)
      IMG_1234.HEIC.json    # sidecar: <stem><primary ext>.json (written atomically via .tmp)
      IMG_1234_3f2a9c1b.HEIC  # stem collision -> stem + "_" + uuid[:8]
```
`SafeName` = `re.sub(r"[^\w\-]+", "-", name).strip("-")` or `"iPhone"`. Every media file's mtime/atime is set to the date taken (`os.utime`).

### 2.3 `device.json`
Rewritten atomically (`write_atomic`) on every connect: `{"udid", "name", "product_type", "ios"}` (from lockdown `DeviceName`, `ProductType`, `ProductVersion`). An unreadable/corrupt `device.json` only causes that folder to be skipped (warning logged).

### 2.4 `manifest.sqlite` (`offload.open_manifest`)
```sql
assets(uuid TEXT PRIMARY KEY, dest_dir TEXT, dest_stem TEXT,
       meta_hash TEXT, status TEXT, duplicate_of TEXT, updated_at TEXT)
files(src_path TEXT PRIMARY KEY, uuid TEXT, dest_path TEXT, size INTEGER, sha256 TEXT)
INDEX files_sha ON files(sha256)
INDEX files_uuid ON files(uuid)
```
- `assets.status`: `copying` (in progress / interrupted), `verified`, `duplicate` (`duplicate_of` = uuid of the asset that holds the bytes).
- `assets.meta_hash`: SHA-256 of the serialized sidecar JSON, used to skip unchanged rewrites.
- `files`: one row per copied file (primary + companions); `src_path` is the phone path. A `src_path` already recorded for another uuid is never taken over (`backup_asset` raises `IOError`).

### 2.5 Sidecar JSON (`<stem><ext>.json`)
Written by `write_meta()` with `indent=2, ensure_ascii=False, sort_keys=True`. Not written for `duplicate` assets.
| Key | Value |
|---|---|
| `uuid` | Photos `ZUUID` |
| `original_path` | `/<ZDIRECTORY>/<ZFILENAME>` (the DB path, not the resolved real folder) |
| `source` | `camera_roll` / `synced_from_computer` / `shared_album` / `messages` / `other` (`asset_source`) |
| `kind` | `screenshot` / `camera` / `saved` / `null` (`asset_kind`) |
| `kind_codes` | `{"subtype": ZKINDSUBTYPE, "saved_type": ZSAVEDASSETTYPE}` (raw, may be null) |
| `date_taken` | ISO local time (naive) from `APPLE_EPOCH + ZDATECREATED` |
| `date_taken_utc` | Same instant as ISO UTC with offset (`+00:00`). Missing `ZDATECREATED` → 2001-01-01 and a log warning |
| `latitude`, `longitude` | float or `null` (null if lat is None or either equals -180) |
| `favorite`, `hidden` | bool |
| `albums` | sorted list of album titles (ZGENERICALBUM.ZKIND in 2, 1505, not trashed) |
| `caption` | `ZASSETDESCRIPTION.ZLONGDESCRIPTION` or `null` |

---

## 3. Module reference

### 3.1 `config.py`
Constants: `APP_DIR`, `CFG`, `DEFAULT = ~/PhotoBackup`, `PREF_DEFAULTS = {"auto_live": True, "muted": False}`.

| Function | Behaviour |
|---|---|
| `_load() -> dict` | Read `config.json`; any error → `{}`. |
| `_save(d)` | mkdir `APP_DIR`, write JSON to `config.json.tmp`, then `os.replace`. |
| `is_configured() -> bool` | `"backup_root" in _load()`. Used by `/api/settings`. |
| `get_backup_root() -> Path` | Configured path (expanduser) or `DEFAULT`. Used by `offload.backup_root`, `viewer.app.backup_root`. |
| `set_backup_root(path) -> Path` | mkdir, write+unlink `.write-test` probe (raises `OSError` if read-only), save. Called by `POST /api/settings/backup-root`. |
| `get_prefs() -> dict` | Saved prefs merged over `PREF_DEFAULTS` (only known keys). |
| `set_pref(key, value) -> dict` | `KeyError` for unknown key; stores `bool(value)`; returns `get_prefs()`. |

### 3.2 `offload.py` (CLI + shared copy engine)
Constants: `CHUNK = 4 MiB`, `APPLE_EPOCH = 978307200`, `MIN_FREE_MARGIN = 2 GiB`, `SPACE_CHECK_EVERY = 25`, `COMPANION_EXTS = {".MOV", ".AAE"}`, `SKIP_DIRS = {Thumbnails, Metadata, Mutations, CPLAssets, PhotoCloudSharingData, Sync, Caches, Journals, MISC, private, DCIM}`. Globals: `log = logging.getLogger("offload")`, `LOG_PATH`.

| Function / class | Signature → return | Notes / side effects / callers |
|---|---|---|
| `backup_root()` | `-> Path` | `config.get_backup_root()`. Used here and by `viewer/phone.py`. |
| `start_log(d, stamp)` | `-> Path` | Sets global `LOG_PATH = d/logs/<stamp>.log`, `logging.basicConfig(filename=..., force=True)`, `captureWarnings(True)`. Called by `main`. |
| `human(n)` | `-> str` | Bytes → "1.2 GB". Used by `main`, `phone.import_items`. |
| `class StatusLine` | `__init__()`, `show(text)`, `close()` | Second tqdm line (`position=1`); redraws only on change. Used by `main`. |
| `write_atomic(p: Path, text: str)` | `-> None` | Writes `<p>.tmp` then `os.replace`. Used for `device.json` and sidecars. |
| `find_device_dir(udid: str)` | `-> tuple[Path, dict] \| None` | `(folder, device.json contents)` of the folder under `backup_root()` whose `device.json` has this udid; `None` if root missing or no match. Unreadable/non-dict `device.json` is skipped with a warning. Used by `device_dir`, `phone.name_from_backup`. |
| `device_dir(info: dict)` | `-> Path` | `find_device_dir(udid)`, else creates `<Safe>__<udid[:8]>` (`mkdir(exist_ok=True)`); always rewrites `device.json` via `write_atomic`. Called by `main`, `phone.scan`. |
| `open_manifest(d)` | `-> sqlite3.Connection` | Opens/creates `manifest.sqlite` schema (2.4). Called by `main`, `retag`, `phone.scan`, `phone.import_items`. |
| `table_columns(db, table)` | `-> set[str]` | `PRAGMA table_info`. |
| `asset_source(directory, bundle_scope)` | `-> str` | `DCIM/`→camera_roll; `PhotoData/Sync/`→synced_from_computer; contains `PhotoCloudSharingData`→shared_album; `bundle_scope == 3`→messages; else other. |
| `asset_kind(subtype, saved_type)` | `-> str\|None` | subtype 10→screenshot; saved_type None→None; 3→camera; else saved. |
| `read_library(db_path: Path)` | `-> list[dict]` | Opens the DB read-only (`file:<abs>?mode=ro`, `uri=True`). Reads `ZASSET` (or `ZGENERICASSET`) where `ZTRASHEDSTATE=0` and dir/filename not null, ordered by `ZDATECREATED` (oldest first). Optional columns `ZHIDDEN`, `ZBUNDLESCOPE`, `ZKINDSUBTYPE`, `ZSAVEDASSETTYPE` substituted with constants if missing. Albums discovered via tables matching `Z_\d+ASSETS` joined to `ZGENERICALBUM`. Captions via `ZADDITIONALASSETATTRIBUTES`⋈`ZASSETDESCRIPTION` (errors ignored). Each item: `{"uuid","src_dir":"/"+dir,"source","filename","date":datetime,"meta":{sidecar dict}}`. Callers: `main`, `retag`, `phone.scan`. |
| `async find_folders(afc, wanted)` | `wanted: {db_folder: set(filenames)}` → `{db_folder: real_path}` | BFS (`collections.deque`) from `/PhotoData`, depth ≤5, skipping `SKIP_DIRS` and dotted names; matches a path ending in the db folder that contains any wanted filename. Used by `main`, `phone.group_files`. |
| `async stream_copy(afc, src, dest, on_bytes=None)` | `-> (size, sha256hex)` | Writes `dest.part`, fsync, compares with `afc.stat(src)["st_size"]` (mismatch → `IOError`), `os.replace`. Any exception (incl. `BaseException`/cancel) deletes the `.part`; a failing `fclose` is logged at debug and never masks the original error. `on_bytes(n)` per chunk. |
| `companions_for(filename: str, group: list[str])` | `-> list[str]` | Primary first, then sorted same-stem siblings whose suffix (upper) is in `COMPANION_EXTS` (`.MOV`, `.AAE`). `IMG_1.JPG` + `IMG_1.HEIC` are separate assets. Used by `main`, `phone.scan`. |
| `async snapshot_db(afc, snap: Path, on_bytes=None, tries: int = 3)` | `-> None` | Copies `Photos.sqlite`, `-wal`, `-shm` (each if present) into `snap`, deleting old copies first, then runs `PRAGMA quick_check`. On `IOError`/`sqlite3.Error`/check failure logs a warning, sleeps 1 s and retries the whole set; after `tries` raises `IOError`. Used by `main`, `phone.scan`. |
| `sha256_file(p)` | `-> str` | Hash from disk. |
| `pick_stem(con, dest_dir, filename, uuid)` | `-> str` | Original stem, or `stem_<uuid[:8]>` if any file in `dest_dir` starts with `stem.` (case-insensitive `os.listdir` compare, no glob wildcards) or another asset holds that stem. |
| `asset_complete(con, uuid, _seen=None)` | `-> bool` | `verified`: every `files` row exists with matching size. `duplicate`: recursively requires its `duplicate_of` asset to be complete (False if missing, self-referencing or cyclic; `_seen` tracks the chain). |
| `write_meta(con, dest_dir, stem, primary_ext, asset)` | `-> bool` | Writes sidecar (via `write_atomic`) unless `meta_hash` matches and file exists; updates `assets.meta_hash`. Does **not** commit. |
| `async backup_asset(afc, con, d, asset, companions, on_bytes=None)` | `-> "verified"\|"duplicate"` | Reuses stored dest_dir/stem or picks new under `d/media`; upserts `assets` status `copying` + commit; copies each companion (primary first), re-verifies SHA from disk (fail → unlink, `IOError`); if primary SHA already in `files` for another uuid **and that copy still exists with the recorded size** → unlink, mark `duplicate`, stop; else set mtime; if `src_path` is already recorded for another uuid → warning + `IOError`; else insert `files` row. Sets status, writes sidecar if verified, commits. Callers: `main`, `phone.import_items`. |
| `async main(args)` | `-> int` exit code | See 3.2.1. |

#### 3.2.1 `main` flow
1. `create_using_usbmux(connection_type="USB")`; failure → message, return 1.
2. `device_dir`, `start_log`.
3. Inside `AfcService`: `snapshot_db` to `db-snapshots/<stamp>/`, `read_library`; `--limit N` keeps the N newest (`library[-N:]`).
4. `afc.listdir` each `src_dir`, group names by `(folder, STEM.upper())`; unreachable folders → `find_folders`; still missing → log warning.
5. Check loop: complete assets skipped (and sidecar refreshed via `write_meta` if verified and not dry-run); filename not in group → "not on phone"; else stat all companions (`companions_for`: primary + same-stem `.MOV`/`.AAE`).
6. Space check: need + `max(2 GiB, 5% of need)` ≤ free on backup root.
7. `--dry-run` or insufficient space → return (0 / 1). Without `--yes`, prompts `[y/N]`.
8. Copy with byte-based tqdm; failures logged. Every `SPACE_CHECK_EVERY` (25) items free space is re-checked (stop if < `MIN_FREE_MARGIN` + item size). Stops early on `ConnectionError`/`BrokenPipeError` or 3 failures in a row ("phone disconnected — re-run to resume").
9. "Safe to delete" date: last camera_roll asset date before the first incomplete one (suppressed for `--limit`).
10. Returns 1 if any failed/unreadable/metadata errors or an early stop, else 0. `KeyboardInterrupt` → exit 130; other exceptions → exit 1.

CLI: `--limit N`, `--dry-run`, `--yes/-y`.

### 3.3 `viewer/app.py` (FastAPI app)
Constants: `STATIC = <dir>/static`, `THUMB_PX=400`, `PREVIEW_PX=2048`, `IMAGE_EXT={.heic,.heif,.jpg,.jpeg,.png,.gif,.dng,.webp,.tif,.tiff}`, `BROWSER_IMAGE_EXT={.jpg,.jpeg,.png,.gif,.webp}`, `VIEWS=("all","favorites","live","videos","screenshots","downloaded","hidden")`, `CACHE_HEADERS={"Cache-Control": "max-age=86400"}` (also used by `phone.py`), `LOCAL_HOSTS={"127.0.0.1","localhost"}`. Global `INDEX = {"phones": {id: name}, "items": [...]}`.

| Function | Notes |
|---|---|
| `backup_root()` | `config.get_backup_root()` |
| `cache_dir()` | `<root>/.viewer-cache` (mkdir). Also used by `phone.py`. |
| `build_index()` | For each `*/device.json` reads `media/*.json`; skips unparsable sidecars and sidecars without their media file. Item: `{id:"<phone>/<file>", phone, file, kind:"image"\|"video" (by ext), live: "<stem>.MOV"\|"<stem>.mov"\|None (images only), mkind, date, favorite, hidden, albums, caption, lat, lon}`; sorted newest first. Prints count. Called at startup (`lifespan`), `/api/reindex`, backup-root change, and after phone imports. |
| `lifespan(_app)` | Async context manager; runs `build_index()`. |
| `_hostname(value: str) -> str \| None` | Hostname of a `Host` header or `Origin` URL via `urlsplit` (`None` on `ValueError`). |
| `async local_only(request, call_next)` | `@app.middleware("http")`. 403 `forbidden host` unless the `Host` hostname is in `LOCAL_HOSTS` (DNS-rebinding guard). For non-GET/HEAD requests, 403 `forbidden origin` if an `Origin` header is present and not local. |
| `media_path(phone, file)` | 404 unless phone in INDEX, `file` is a bare name, and file exists. |
| `filtered(phone=None, album=None, view="all")` | `hidden` view returns only hidden; all other views exclude hidden; then album, then view filter (favorites / live / videos / screenshots=`mkind=="screenshot"` / downloaded=`mkind=="saved"`). |
| `_lock(key)` | Striped locks: `_STRIPES = 64`; keys starting with `phone:` use `_phone_locks`, others `_locks`; returns `pool[hash(key) % 64]`. Separate pools keep `phone_image` → `make_image` lock ordering deadlock-free. |
| `make_image(src, dest, px) -> bool` | Cached if `dest` mtime ≥ src mtime. Image ext → `sips -s format jpeg -s formatOptions 80 -Z px`; non-image → `qlmanage -t -s px` (60 s timeout) then `sips` PNG→JPEG; fallback Pillow (`PIL` optional, not in build). Writes `<dest>.tmp.jpg` then replace. |
| `_jpeg(phone, file, px, folder)` | `make_image` into cache, `FileResponse` with `CACHE_HEADERS` (1 day); 404 "no preview" on failure. |

The `viewer.phone` router is included, settings routes added, and finally `StaticFiles(directory=STATIC, html=True)` is mounted at `/` (must stay last).

### 3.4 `viewer/phone.py` (connected-phone backend)
Starts a daemon thread `phone-io` running `LOOP` (a new asyncio loop) at import time. All pymobiledevice3 I/O runs there.

Constants: `VIDEO_THUMB_MAX = 150 MiB`, `SORTS=("size_desc","size_asc","date_desc","date_asc")`, `SHOWS=("new","done","all")`, `KINDS=("all","photo","video","live","screenshot")`. Logger: shared `"offload"`.

`class Session` (single global `S`): `state` (`idle|scanning|ready|importing|error`), `message`, `progress` (`{"done","total","unit":"items"|"bytes"}`), `device`, `dir`, `lockdown`, `afc`, `items`, `by_uuid`, `not_on_phone`, `job`, `cancel`. `NAMES: {udid: name}` caches device names.

Globals: `STATE_LOCK = threading.Lock()` guards every check-then-set of `S.state` from request threads (`/connect`, `/import`, `POST /api/settings/backup-root`). `PULL_SLOTS = threading.BoundedSemaphore(4)` caps concurrent phone pulls for thumbnails/previews.

| Function | Notes |
|---|---|
| `on_loop(coro, timeout=None)` | Run on LOOP and block for result. |
| `start_on_loop(coro)` | Schedule on LOOP; a done-callback logs any exception and, if state is `scanning`/`importing`, finishes an open `S.job` with the error and calls `fail()`. |
| `public(it)` | Drops keys starting with `_`. |
| `fail(msg)` | `S.state="error"`, message, logs warning. |
| `async close_phone()` | Closes `S.afc` (`aclose`) and `S.lockdown`, ignoring errors. |
| `name_from_backup(udid)` | `offload.find_device_dir(udid)` → its `name`, else `None`. |
| `async list_phones()` | `usbmux.list_devices()`, USB only; name from `NAMES` → backup → lockdown with `autopair=False`; fallback `"iPhone <udid8> (unlock + Trust)"`. Returns `[{udid,name}]`. |
| `extra_fields(db_path)` | `{uuid: (ZKIND, ZKINDSUBTYPE, ZDURATION, ZORIGINALFILESIZE)}`, degrading to `(ZKIND, None, None, None)` or `{}`. |
| `async group_files(afc, library)` | Same grouping + `find_folders` logic as offload `main`; mutates `src_dir` of moved assets. |
| `local_copy(con, uuid, filename)` | Backed-up `dest_path` with same suffix as primary, if it exists. |
| `async scan(udid)` | Connect (`pair_timeout=60`, may pop Trust dialog), sets `S.lockdown`; inside the `try`: `device_dir`, open AFC, `offload.snapshot_db` to `db-snapshots/<stamp>-viewer/` with byte progress (clamped to total on retries), `read_library` + `extra_fields` in threads, group files, build item list (companions via `offload.companions_for`, manifest opened with `contextlib.closing`), measure real sizes with 32 concurrent `afc.stat`. Item fields: `uuid, file, date, size, kind ("video" if ZKIND==1 else by ext), live, screenshot (subtype==10), duration, favorite, hidden, source, backed_up` + private `_asset, _companions, _local`. Ends `ready` or `error`. |
| `async phone_alive()` | `afc.listdir("/")` within 5 s. |
| `async import_items(uuids)` | Wrapper: runs `_import_items`; any exception is logged, finishes `S.job` with the error and returns state `importing` → `ready`. |
| `async _import_items(uuids)` | Dedups, skips backed-up, space check (same margin as offload), creates `S.job`, adds a `FileHandler` to `logs/<stamp>-viewer-import.log`, loops `offload.backup_asset`, honours `S.cancel` between files, stops with error if phone dead after a failure. Finally sets `finished`, state back to `ready`, then `build_index()` in a thread (errors logged, not raised). |
| `phone_filtered(show, kind, sort)` | Filter + sort on `S.items` (renamed from `filtered` to avoid clashing with `app.filtered`). |
| `pattern(values)` | Regex `^(a\|b)$` for Query validation. |
| `item_or_404(uuid)` | |
| `async pull_for_image(it, want_original)` | Thumbnails: tries `/PhotoData/Thumbnails/V2<src_dir>/<file>/`, picks `5005.JPG` or last `.JPG`. Else original (None if video > `VIDEO_THUMB_MAX`). Copies to a unique `tempfile.mkstemp` file in `phone-tmp/` (deleted if the copy fails). |
| `phone_image(uuid, px, folder, want_original)` | Cached JPEG; else from `_local` backup copy, else pull from phone: waits up to 60 s for a `PULL_SLOTS` slot, then `run_coroutine_threadsafe(pull_for_image)` with 180 s timeout (future cancelled on failure); tmp deleted after. Responses use `CACHE_HEADERS`. |

Pydantic bodies: `ConnectBody{udid: str}`, `ImportBody{uuids: list[str]}`.

### 3.5 `launcher.py` (app entry point)
Module-level script: picks a free port, runs `uvicorn.Server(app, host=127.0.0.1)` in a daemon thread, waits up to ~10 s for the port, defines `class Api` with `pick_folder()` (native folder dialog, returns path or `None`; exposed to JS as `window.pywebview.api.pick_folder`), opens window "Photo Backup" at `/phone.html` (1280×850), `webview.start(debug=False)`. The listening socket is bound once and passed to uvicorn (`server.run(sockets=[s])`), so the port can't be taken in between. After the window closes: sets `phone.S.cancel = True`, waits up to 10 s while `S.state == "importing"` (import stops after its current file), sets `server.should_exit`, `os._exit(0)`.

### 3.6 Scripts
| Script | Purpose |
|---|---|
| `retag.py [--dry-run]` | Re-writes sidecars of `verified` assets from the latest `db-snapshots/*/Photos.sqlite` per phone (`latest_snapshot(phone_dir)`, `retag_phone(d, dry_run)`). Prints kind counts and raw `(subtype, saved_type)` codes. Uses `offload.backup_root()`. |
| `check_local.py [db]` | Diagnostic: compares `sys.argv[1]` or default `probe/Photos.sqlite` (relative to cwd, `ZASSET` only) with the files in `/DCIM/*APPLE`, then checks `afc.exists` for the rest; prints counts by location and up to 10 missing paths. Functions: `library_items()`, `async phone_files()`, `async main()`. |
| `count_dcim.py` | Counts files + total GB in `/DCIM/*APPLE` on the phone. |
| `explain_gap.py [db]` | Explains Photos-app count vs. script count from a snapshot (default: newest `<config.get_backup_root()>/*/db-snapshots/*/Photos.sqlite`, opened read-only). `SCRIPT` filter = `ZTRASHEDSTATE=0 AND ZDIRECTORY IS NOT NULL AND ZFILENAME IS NOT NULL` (same as `read_library`). Helper `where(dir_)` buckets directories. |

---

## 4. HTTP API

All routes pass through the `local_only` middleware: `Host` must be `127.0.0.1`/`localhost` (any port), and non-GET/HEAD requests with a non-local `Origin` get 403.

### Backup library (`viewer/app.py`)
| Method | Path | Params | Response |
|---|---|---|---|
| GET | `/api/summary` | `phone?` | `{phones:[{id,name,count}], views:{<view>: n}, albums:[{name,count}]}` (counts exclude hidden; albums sorted case-insensitively) |
| GET | `/api/items` | `phone?`, `album?`, `view` (one of VIEWS, default all), `offset=0` (≥0), `limit=120` (1–500) | `{total, offset, items:[index item]}` |
| POST | `/api/reindex` | – | `{items: n}` |
| GET | `/thumb/{phone}/{file}` | – | JPEG 400 px (`Cache-Control: max-age=86400`) |
| GET | `/preview/{phone}/{file}` | – | Original if browser-viewable ext, else JPEG 2048 px |
| GET | `/media/{phone}/{file}` | `download=false` | Original; `.mov/.mp4/.m4v` sent as `video/mp4`; `download=true` → attachment |
| GET | `/api/settings` | – | `{backup_root, configured, prefs:{auto_live, muted}}` |
| POST | `/api/settings/pref` | JSON `{key, value}` | `{prefs}`; 400 unknown setting |
| POST | `/api/settings/backup-root` | JSON `{path}` | `{backup_root, configured:true, items}`; 400 empty path; under `STATE_LOCK`: 409 while scanning/importing, 400 unwritable, then closes the phone connection (`on_loop(close_phone(), timeout=10)`, errors ignored) and resets `S.__init__()`; then rebuilds the index. |
| GET | `/*` | – | Static files (`index.html` at `/`) |

### Connected phone (`viewer/phone.py`, prefix `/api/phone`)
| Method | Path | Params | Response |
|---|---|---|---|
| GET | `/devices` | – | `{devices:[{udid,name}]}` or `{devices:[], error}` (20 s timeout) |
| POST | `/connect` | JSON `{udid}` | Status object (below); 409 if busy (checked under `STATE_LOCK`). Starts `scan` |
| GET | `/status` | – | `{state, message, progress, device, folder, counts:{total,new,done,new_bytes,total_bytes,not_on_phone}, job}` |
| GET | `/items` | `show` (new\|done\|all, default new), `kind` (all\|photo\|video\|live\|screenshot), `sort` (size_desc\|size_asc\|date_desc\|date_asc, default date_desc), `offset=0` (≥0), `limit=120` (1–500) | `{total, bytes, offset, items:[public item]}` |
| GET | `/ids` | `show` (default new), `kind` (default all) | `{items:[{uuid,size,backed_up}]}` (sorted size_desc) |
| POST | `/import` | JSON `{uuids:[...]}` | `{ok:true}`; 400 if empty, then 409 unless ready (checked + set to `importing` under `STATE_LOCK`) |
| POST | `/cancel` | – | `{ok:true}` (stops after current file) |
| GET | `/thumb/{uuid}` | – | JPEG 400 px |
| GET | `/preview/{uuid}` | – | JPEG 2048 px (videos: from thumbnail cache / poster, not streamed) |

`job` object: `{items, done_items, bytes, done_bytes, already, copied, duplicate, failed, current, finished, cancelled, error, log, started}`.

---

## 5. Frontend

### `static/common.js` (shared, loaded before each page script)
Plain globals, no build step. Both `index.html` and `phone.html` include `<script src="common.js"></script>` before their page script.
- `$(s)`, `enc` (= `encodeURIComponent`), `esc(s)` (HTML-escape).
- `getJSON(url, opts)` (throws `Error(detail \|\| statusText)` on non-OK), `post(url, body)` (JSON POST via `getJSON`).
- `lazyImageObserver(rootMargin)` → IntersectionObserver on `#main` that sets `src` from `data-src` once.
- `watchSentinel(loadMore)` – observes `#sentinel` (1200 px margin).
- `makeTileFocusable(t, label)` (`tabIndex=0`, `role=button`, `aria-label`), `tileKeys(grid)` (Enter/Space on a focused tile = click).
- `noHashNav(nav)` – prevents `href="#"` links reaching the URL (defined, currently unused; pages call `e.preventDefault()` in their handlers).
- `showViewer()` – unhides `#viewer` and focuses `.v-close` if it was hidden.
- `viewerKeys(actions)` – keydown handler while `#viewer` is visible; `actions()` returns `{key: fn}`; handled keys get `preventDefault` (avoids the macOS alert sound).

Both `#viewer` elements have `role="dialog" aria-modal="true"`; sidebar links are `<a href="#">`.

### `static/index.html` + `app.js` (backup library)
State in URL hash `#phone=&view=&album=`; `PAGE = 120`.
- URL: `readHash()`, `writeHash()`, `navigate(patch)`.
- Sidebar: `loadSidebar()` (`getJSON("/api/summary")`, keeps old sidebar on error); handlers on `#phone`, `#views`, `#albums`, `#menu`, `#reindex` (`post("/api/reindex")`, shows "Rescan failed" on error).
- Grid: `refresh()`, `loadMore()` (`/api/items`, generation counter drops stale responses, "Couldn't load items" on error), `addTile(it, idx)` (escaped month headers, badges via `KIND_BADGE`, `makeTileFocusable`), `tileObserver = lazyImageObserver("800px 0px")`, `watchSentinel(loadMore)`, `tileKeys(#grid)`, `#zoom` sets `--tile`.
- Viewer: `thumbUrl`, `previewUrl`, `mediaUrl`; `openViewer(idx)` (video plays `/media`; image shows thumb then preview; Live companion via `#v-live`), `renderMeta(it)` (Google Maps link, album tags clickable), `prefetch(idx)`.
- Prefs (saved server-side, since the app port changes each launch): `savePref` (`post`, errors ignored), `renderAutoLive`, `toggleAutoLive`, `applySound`, `toggleSound`, `playLive` (falls back to muted on `NotAllowedError`), `stopLive`, `stopMedia`, `closeViewer`.
- Keys via `viewerKeys`: Esc, ←/→, Space (replay Live), M (sound), L (auto Live).

### `static/phone.html` + `phone.js` (connected phone)
`PAGE = 150`; `selected: Map<uuid,size>`.
- Helpers: `human`, `dur` (plus `common.js`).
- Devices/status: `loadDevices()` (guarded by `devicesLoading`, no overlapping calls), `busy()`, `poll()` (guarded by `polling` so only one loop runs) → `pollOnce()` (700 ms while busy else 4 s; skipped while `document.hidden`, resumed on `visibilitychange`; triggers `refresh` when scan finishes), `renderStatus()` (escapes `s.message`), `#connect` handler.
- Filters/grid: `renderFilters`, `refresh`, `loadMore`, `addTile`, `tileObserver = lazyImageObserver("600px 0px")`, `watchSentinel`, `tileKeys`.
- Selection: grid click (Shift range, ⤢ opens preview), dblclick, `setSelected(it, on, idx)` (ignores backed-up; `idx` avoids `indexOf`), `toggle(it, idx)`, `clearSelection`, `renderSelection`, `#selall` (uses `/api/phone/ids`; alerts on error).
- Import: `#import` confirm dialog, `#ib-go` (POST import), `#ib-cancel` (POST cancel, re-enables with "Couldn't stop" on error, or close), `renderJob()` (smoothed speed + ETA, final summary, then refresh).
- Preview: `openViewer`, `renderViewerMeta`, `closeViewer`, `#v-select`; keys via `viewerKeys`: Esc, ←/→, S/Space toggle select.
- Backup folder: `nativeApi()` (waits up to 1.5 s for `pywebviewready`), `renderRoot`, `loadSettings()` (message if server unreachable; first-run alert + picker if not configured), `chooseFolder()` (native picker or `prompt()`, then reload).

`style.css` / `phone.css` are styling only (not reviewed in detail).

---

## 6. Configuration options
| Setting | Where | Default |
|---|---|---|
| `backup_root` | config.json | `~/PhotoBackup` |
| `prefs.auto_live` | config.json | `true` |
| `prefs.muted` | config.json | `false` |
| `CHUNK`, `MIN_FREE_MARGIN`, 5% margin | offload.py | 4 MiB, 2 GiB |
| `SPACE_CHECK_EVERY` | offload.py | 25 |
| `THUMB_PX`, `PREVIEW_PX`, `CACHE_HEADERS`, `_STRIPES` | viewer/app.py | 400, 2048, max-age=86400, 64 |
| `VIDEO_THUMB_MAX`, scan concurrency, `PULL_SLOTS` | viewer/phone.py | 150 MiB, 32, 4 |

No environment variables are read.

---

## 7. Running

```bash
cd ~/photo-offload
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements.txt  # pinned runtime deps (pymobiledevice3, fastapi, uvicorn, tqdm, pywebview, pillow, ...)

python offload.py --dry-run
python offload.py --limit 20
python offload.py [--yes]

uvicorn viewer.app:app --reload     # run from project root (imports `config`, `offload`)
open http://localhost:8000          # library
open http://localhost:8000/phone.html

python launcher.py                  # desktop window
```
Open the viewer via `localhost` or `127.0.0.1` only; other Host names get 403. Viewer image conversion needs macOS `sips` / `qlmanage` (Pillow, in requirements.txt, is the fallback).

`requirements.txt` (runtime) and `requirements-build.txt` (`-r requirements.txt` + PyInstaller 6.22.3 and its deps) are pinned from the project venv.

## 8. Building the .app
`bash build.sh` (venv active, from project root):
1. Checks `launcher.py` exists and warns if Python isn't arm64.
2. Installs `requirements-build.txt` (`uv pip` if available, else `pip`), `rm -rf build dist`, then `pyinstaller --noconfirm "Photo Backup.spec"`. Fails if `dist/Photo Backup.app` is missing.
3. Smoke test: launches `dist/Photo Backup.app/Contents/MacOS/Photo Backup`, checks it's alive after 8 s (log in `build/smoke.log`, tail printed on failure), kills it.
4. Checks for leftover processes.

`Photo Backup.spec` is the source of truth: `Analysis(['launcher.py'], pathex=['.'])`, datas `viewer/static` + `collect_all('pymobiledevice3')`, `console=False`, `upx=True`, icon `assets/PhotoBackup.icns`, `BUNDLE` name `Photo Backup.app`, `bundle_identifier='com.aitch.photobackup'`.

`build/` and `dist/` are build outputs (not kept in the project); `docs/` holds this guide, `CODE_REVIEW_v1.md` and `SPEC_media_kind_classification.md`.

## 9. Logging
- `offload.py`: all per-item warnings/errors to `<phone>/logs/<stamp>.log` (root logger file handler); console shows counts only.
- Phone page: logger `"offload"`; a file handler is attached only during an import. Scan warnings/exceptions outside an import go to whatever handlers exist (uvicorn console).

## 10. Concurrency notes
- `viewer/phone.py` owns one asyncio loop thread; sync FastAPI handlers (threadpool) call into it with `on_loop`/`start_on_loop`. `S` is mutated from the loop thread; request threads take `STATE_LOCK` for check-then-set of `S.state` (connect, import, backup-root switch). Reads (e.g. `/status`) are unlocked.
- Image conversion is serialized via striped `_lock` (64 stripes, separate pool for `phone:` keys). Phone pulls are capped at 4 concurrent (`PULL_SLOTS`) so `/status` and `/cancel` keep a worker thread.
- `offload.py` CLI and the phone page share the same manifest; running both against the same phone simultaneously isn't guarded.

## 11. Known issues / inconsistencies
1. `check_local.py` defaults to `probe/Photos.sqlite` relative to cwd (path can now be passed as `argv[1]`) and only reads table `ZASSET` (no `ZGENERICASSET` fallback).
2. Sidecar `original_path` keeps the DB path even when `find_folders` resolved a different real folder (the real path is in `files.src_path`).
3. `offload.py` CLI and the phone page share the same manifest; running both against the same phone simultaneously isn't guarded.
4. README still shows `~/PhotoBackup/` in its layout and cache examples; the backup root is configurable (default `~/PhotoBackup`).
