# Photo Backup v1: Senior Code Review

**Scope:** every source file in `photo-offload/` except `*.bak-*`, `dist/`, `build/` and `.venv/`.
**Stack:** Python, pymobiledevice3 (AFC + lockdown), FastAPI + uvicorn, pywebview, PyInstaller.
**Method:** four reviewers each covered one area: structure, core logic, backend and frontend. The compiler then re-checked the top findings against the source.

## Summary

This is a solid v1. The copy path is careful:
- each file is written to a `.part` file, fsynced, and size-checked;
- it is re-hashed from disk before the atomic rename;
- runs can be resumed;
- nothing is ever deleted from the phone.

Metadata is escaped on the frontend, and the server only listens on 127.0.0.1. Most findings below are edge cases and housekeeping.

**No breaking change is required.** Existing backup folders, `manifest.sqlite`, the sidecar JSON, the API routes and the JSON shapes all stay compatible.

The only item that changes how users run things is optional: moving the diagnostic scripts into `tools/` (S8).

## Instructions for the applying agent

- Apply the items in the order of the table below.
- Never change the formats of the `assets` and `files` tables, the sidecar fields, or the API routes and response shapes. Adding things is allowed.
- After the changes:
  1. run `python offload.py --limit 5` against a phone;
  2. open the viewer and run `./build.sh`;
  3. check that `smoke.log` says "indexed … items".

### Priority order

| # | ID | Item | Severity | Breaking |
|---|----|------|----------|----------|
| 1 | C1 | `retag.py` crashes on import | Critical | No |
| 2 | B1/B2 | Phone page stuck in "scanning" or "importing" after an early error | High | No |
| 3 | C2 | Safe-to-delete date trusts "duplicate" items without checking the original | High (data safety) | No |
| 4 | B3 | No Host/Origin check on the local server | High | No |
| 5 | S1 | Add version control and `.gitignore`; archive the `.bak` files | Critical (process) | No |
| 6 | S3 | Pin dependencies | Major | No |
| 7 | C3 | Missing index on `files(uuid)` | Major (perf) | No |
| 8 | C4/C5 | Disconnect handling and `.part` cleanup | Major | No |
| 9 | everything else | — | Minor / nit | No |

---

## 1. Project structure and packaging

**S1: no version control (critical, process).**
There is no `.git` folder. About 20 `*.bak-before-*` files act as history instead. They sit in the root, in `viewer/` and in `viewer/static/`, which means the copies in `viewer/static/` are bundled into the .app and served over HTTP.

Fix:
1. Run `git init`.
2. Add the `.gitignore` below.
3. Commit the current files.
4. Move the `.bak` files to `_archive/` (ignored), or delete them.

**S2: large private data in the repo folder (major).**
- `probe/Photos.sqlite` is 325 MB of real library metadata (GPS, captions). Only `check_local.py:6` uses it.
- `PB.zip` (49 MB) is an outdated build.

Fix:
- Move `probe/` outside the project.
- Make `check_local.py` take the database path as `argv[1]`, defaulting to `probe/Photos.sqlite`.
- Delete `PB.zip`, or keep it under an ignored `releases/` folder.

**S3: no dependency manifest (major).**
The code imports pymobiledevice3, fastapi, uvicorn, pywebview, tqdm and Pillow. The README installs only pymobiledevice3, and `build.sh:24` installs unpinned packages.

Fix:
- Add `requirements.txt`, pinned to the versions currently in `.venv`. Get them with `.venv/bin/pip freeze`, filtered to these packages.
- Add `requirements-build.txt` containing pyinstaller.
- Point `build.sh` and the README at these files.

**S4: two build definitions (major).**
`build.sh` calls PyInstaller with CLI flags, which regenerates `Photo Backup.spec` on every build. The spec also has `bundle_identifier=None`.

Fix:
- Make the spec the source of truth: `pyinstaller --noconfirm "Photo Backup.spec"`.
- Set `bundle_identifier='com.aitch.photobackup'`. This gives macOS a stable app identity for permissions and preferences.

**S5: build.sh leftovers (minor).**
Remove these from `build.sh`:
- the step-2 grep "path audit", which mentions "see chat";
- the "paste to Claude" lines.

Also write `smoke.log` to `build/` instead of the project root.

**S6: SPEC file loose in the root (nit).**
Move `SPEC_media_kind_classification.md` to `docs/`. Put `DEVELOPER_GUIDE.md` and this review there as well.

**S7: keep the root modules where they are.**
`offload.py`, `config.py` and `launcher.py` should stay in the root. `viewer/phone.py:20` (`import offload`), `viewer/app.py`, `retag.py`, the spec's `Analysis(['launcher.py'])` and `pathex=['.']` all depend on these paths. Moving them into a package is not worth the breakage for v1.

**S8: diagnostics mixed with app code (minor, optional, changes how scripts are run).**
Move `retag.py`, `check_local.py`, `count_dcim.py` and `explain_gap.py` into `tools/`. Each one needs `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` at the top. Then update the script table in the README.

### Target layout
```
photo-offload/
├── launcher.py  config.py  offload.py     # unchanged
├── viewer/   __init__.py  app.py  phone.py  static/
├── tools/    retag.py  check_local.py  count_dcim.py  explain_gap.py
├── assets/   PhotoBackup.icns  PhotoBackup_icon.svg  PhotoBackup_icon_1024.png
├── docs/     DEVELOPER_GUIDE.md  SPEC_media_kind_classification.md  CODE_REVIEW_v1.md
├── Photo Backup.spec  build.sh
├── requirements.txt  requirements-build.txt
└── README.md  .gitignore
```

### `.gitignore`
```
.venv/
__pycache__/
*.pyc
build/
dist/
*.zip
smoke.log
.DS_Store
*.bak-before-*
_archive/
probe/
*.sqlite
*.sqlite-shm
*.sqlite-wal
```

---

## 2. Core backup logic (`offload.py` and scripts)

**What is already right:**
- The 2001 Core Data epoch is handled.
- Album link-table discovery works, and so does the caption join.
- Copies use `.part`, then fsync, size check, hash check and rename.
- A `--limit` run correctly gives no safe-delete date.
- Unchanged metadata is skipped by comparing hashes.

**C1: `retag.py` cannot run (critical).** *(verified)*
`retag.py:18` imports `BACKUP_ROOT`, which no longer exists; `offload.py:21` now has `backup_root()`.

Fix:
- Import `backup_root` instead.
- In `main`, set `root = backup_root()` and use `root` at lines 59 and 61.

**C2: duplicates count as backed up without checking the original (high, data safety).** *(verified)*
In `offload.py:282-290`, `asset_complete` accepts `status='duplicate'` without looking at the `duplicate_of` asset. Duplicates have no `files` rows of their own, so the loop has nothing to check. The result feeds the safe-to-delete date at lines 507-513.

Fix: when the status is `duplicate`, look up `duplicate_of` and return `asset_complete(con, duplicate_of)`. Keep a visited set to guard against loops.

**C3: `files` table has no index on `uuid` (major, performance).** *(verified)*
Only `files_sha` exists (line 95). `asset_complete` filters by uuid once per asset, so each run scans the whole table about 22,000 times.

Fix: add `CREATE INDEX IF NOT EXISTS files_uuid ON files(uuid);` to `open_manifest`. Existing manifests pick it up automatically.

**C4: phone disconnect mid-run (major).**
- At lines 497-500, every remaining item fails one by one, flooding the log.
- `.part` files are only removed on a size mismatch (line 259).
- At line 256, an exception from `fclose` inside `finally` can hide the original error.

Fix:
- In `stream_copy`, add `except BaseException: part.unlink(missing_ok=True); raise`.
- Wrap `fclose` in its own `try`.
- In the main loop, stop after a connection error, or after 3 failures in a row, with the message "phone disconnected — re-run to resume".

**C5: Photos.sqlite snapshot copy can abort the run (major).**
At lines 376-379, the WAL file can grow while it is being copied. The size check then raises and nothing catches it.

Fix:
- Retry copying all three files up to 3 times.
- Then run `PRAGMA quick_check` on the snapshot and retry if it fails.

**C6: one asset can overwrite another asset's file row (major).**
At line 339, `INSERT OR REPLACE` uses `src_path` as the key. If two UUIDs end up with the same source file, the first asset loses its row, and `asset_complete` then passes it without checking any file.

Fix: if a row with the same `src_path` and a different `uuid` already exists, log a warning and skip the insert.

**C7: duplicate matching is loose (minor).**
At line 331, any `files` row can match, including a companion file, and the file on disk is never checked.

Fix: accept the match only if that row's `dest_path` exists on disk with the recorded size. Otherwise copy normally.

**C8: name-collision check (minor).**
At line 276, `glob(stem + ".*")` treats `[`, `]`, `*` and `?` as wildcards. It is also case-sensitive, while APFS and exFAT drives usually are not, and `os.replace` overwrites silently.

Fix: list the directory once and compare names in lowercase.

**C9: files are not written atomically (minor).**
These are written in place, so an interruption can leave a half-written file:
- the sidecar JSON at line 300;
- `device.json` at line 81;
- `config.json` at `config.py:18`.

Fix: write to `*.tmp`, then `os.replace` it over the target.

**C10: `device_dir` is fragile (minor).**
- At line 75, one corrupt `device.json` breaks the lookup for every device. Fix: wrap it in `try` and skip that folder.
- At line 80, `mkdir` should use `exist_ok=True`.

**C11: dates (minor).**
At line 183, `fromtimestamp` produces naive local time, and a NULL date silently becomes 2001-01-01.

Fix:
- Keep `date_taken` as it is.
- Add a new `date_taken_utc` field.
- Log NULL dates.

**C12: free space is checked only once (minor).**
The check happens before the copy loop (lines 452-463). Re-check it every N items and stop cleanly when space falls below `MIN_FREE_MARGIN`.

**C13: companion grouping by stem (minor).**
At lines 398 and 439, two separate assets that share a stem (for example `IMG_1.JPG` and `IMG_1.HEIC`) pick each other up as companions.

Fix: only treat `.MOV` and `.AAE` files as companions.

**C14: `explain_gap.py` is stale (minor).**
- It hardcodes `~/PhotoBackup`; use `config.get_backup_root()` instead.
- It assumes only `DCIM/%` items are backed up, but every source is now included. Update the WHERE clause to match `offload.py:142-148`.

**Nits:**
- `queue.pop(0)` at line 220: use a `deque`.
- Open snapshots read-only (`mode=ro`) in `read_library`.

---

## 3. Viewer backend (`viewer/app.py`, `viewer/phone.py`, `launcher.py`)

**What is already right:**
- Path traversal is blocked. `media_path` only accepts indexed phone folders and plain file names.
- Phone routes look items up by uuid.
- `STATIC` resolves correctly inside the frozen app.
- Settings are stored in `~/Library/Application Support`.
- Preferences are whitelisted.

**B1: an early scan error leaves the state stuck on "scanning" (high).** *(verified)*
At `phone.py:193-200`, `device_dir`, `AfcService` and `__aenter__` run before the `try` at line 202. `start_on_loop` (line 36) throws away the future, so the exception is never logged. After that, `/connect` and the backup-root change keep returning 409 until the app restarts.

Fix:
- Move those lines inside the `try`.
- Have `start_on_loop` attach a `fut.add_done_callback(...)` that logs the exception and calls `fail(...)` if the state is still busy.

**B2: the same problem for import (high).**
In `import_items` (around lines 290-320), errors before the copy-loop `try` leave the state on "importing" with `job["finished"]` unset. Examples are an unplugged drive in `disk_usage`, `mkdir`, or `open_manifest`.

Fix: wrap the whole body. On error, set `job.update(finished=True, error=str(e))` and reset `S.state`. The B1 callback covers this too.

**B3: no Host or Origin validation (high).**
The bind to 127.0.0.1 at `launcher.py:8` is good. But any website open in a normal browser can still reach the random port, by scanning or by DNS rebinding. It could then:
- read `/media/*` and `/api/items`, including GPS and captions;
- POST to `/api/settings/backup-root`, which makes directories anywhere and redirects future imports.

Fix: add one HTTP middleware in `app.py`.
- Return 403 when the `Host` hostname is not `127.0.0.1` or `localhost`.
- For anything other than GET or HEAD, require `Origin` to be absent or equal to the app's own origin.

The pywebview window is same-origin, so the current frontend keeps working.

**B4: changing the backup folder resets state without closing the phone (medium).**
At `app.py:251-262`, `S.__init__()` throws away the open AFC and lockdown connections. The check-then-set on `S.state` also races. The same race exists in `connect` (`phone.py:378-381`) and `start_import` (442-447).

Fix:
- Add a `STATE_LOCK` around each check-and-set.
- In `set_backup_root`, call `on_loop(close_phone(), timeout=10)` before resetting.

**B5: phone thumbnails can exhaust the threadpool (medium).**
At `phone.py:497-511`, each thumbnail request blocks a worker for up to 180 s. A full grid of them can starve `/status` and `/cancel`.

Fix:
- Put a `BoundedSemaphore(4)` around phone pulls.
- Cancel the future on timeout.

**B6: thumbnail and preview share one temp file (medium).**
At `phone.py:485`, the path is `phone-tmp/{uuid}{suffix}`, but the two requests take different locks.

Fix: make the temp name unique, for example with `tempfile.mkstemp(dir=...)`.

**B7: the manifest connection is not closed on scan error (low).**
At `phone.py:224-253`, use `try/finally` or `contextlib.closing` so `con` is always closed.

**B8: paging inputs are not bounded (low).**
`offset` and `limit` accept negative values (`app.py:140`, `phone.py:426`). Use `Query(0, ge=0)` and `Query(120, ge=1, le=500)`.

**B9: the `_locks` dict grows forever (low).**
At `app.py:153-158`, one lock is created per path. Use 64 striped locks instead.

**B10: one-year cache on mutable URLs (low).**
Add `?v=<mtime>` to the URLs, or lower `max-age` to one day.

**B11: launcher port race (low).**
The launcher binds port 0, then closes the socket and binds again. Keep the socket open and pass it to uvicorn: `server.run(sockets=[s])`.

`os._exit(0)` also cuts off any copy in progress. Set `S.cancel = True` and wait briefly before exiting.

**B12: duplication (low).**
- `phone.name_from_backup` repeats the scan in `offload.device_dir`. Extract `offload.find_device_dir(udid)` and use it in both.
- `filtered` exists in both `app.py` and `phone.py`. Rename the phone one to `phone_filtered`.

**Error-shape note (no change for v1):**
- `/api/phone/devices` returns 200 with an `error` field.
- Other routes raise `HTTPException`, which returns `{"detail": ...}`.

Keep both for compatibility, and use `HTTPException` for all new routes. Making them uniform would be a breaking change, so leave it for v2.

---

## 4. Frontend (`viewer/static/`)

**What is already right:**
- Every metadata field is escaped with `esc()`.
- URLs are built with `encodeURIComponent`.
- `gen` guards prevent stale responses.
- Lazy loading uses IntersectionObserver.
- Every JS call matches an existing route.

**F1: raw `innerHTML` for the status message (medium).** *(verified)*
At `phone.js:79` and `93`, `s.message` can contain exception text and is inserted unescaped.

Fix: `let text = esc(s.message || "");`. The idle-state literal can keep its HTML.

**F2: no fetch error handling on the library page (medium).**
At `app.js:39`, `61` and `87`, a failed request in `loadMore` leaves `state.loading = true`, which kills infinite scroll. A failed reindex leaves the button stuck on "Rescanning…".

Fix:
- Add `getJSON`, as in `phone.js:28-32`.
- Wrap these calls in `try/catch` or `try/finally`, and show "Couldn't load items" in `#count` on failure.

**F3: unhandled rejections on the phone page (low-medium).**
`phone.js:248` (select all), `288` (cancel) and `399` (`loadSettings`) need `try/catch`. On cancel, also re-enable `#ib-cancel` in the catch.

**F4: undefined backup root in the import dialog (low).**
At `phone.js:262`, add `|| "the backup folder"` as a fallback.

**F5: polling while hidden (low).**
Skip `poll()` while `document.hidden` is true, and resume it on `visibilitychange`. Also prevent overlapping `loadDevices` calls.

**F6: large grids (low).**
- In `style.css`, add `content-visibility: auto; contain-intrinsic-size: var(--tile) var(--tile);` to `.tile`.
- Remove the redundant `img.loading="lazy"` at `app.js:128`.
- `setSelected` at `phone.js:221` calls `indexOf` for every item, which is O(n²). Pass the index in instead.

**F7: accessibility (low).**
- Add `href="#"` and `preventDefault` to the sidebar `<a data-view>` links.
- Make tiles focusable with `tabIndex=0`, `role="button"` and an Enter/Space handler.
- Set `vImg.alt = it.file`.
- Give `#viewer` the attributes `role="dialog" aria-modal="true"` and move focus to its close button on open.

**F8: duplication (low).**
Extract the code that `app.js` and `phone.js` share into `static/common.js`, loaded as plain globals before each page script:
- `$`, `esc`, `enc`;
- the observers;
- `openViewer`;
- the key map;
- `getJSON` and `post`.

Do this after F2.

**Nit:** `app.js:116`: wrap `${month}` in `esc()` for consistency.

---

## Reviewer verification notes
- These were re-checked directly against the source: C1, C2, C3, B1, F1, S2 (sizes) and the B3 bind address.
- The B3 line number was corrected: the server bind is at `launcher.py:8`.
- C7 was downgraded to minor. The match only runs for an asset's primary file, so a companion file rarely shares a hash with a primary.
- Nothing in this review changes a data format or an API contract.
