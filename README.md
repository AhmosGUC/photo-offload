# photo-offload

Back up an iPhone's photos and videos to this Mac over USB — with location,
albums, favorites and captions kept — so you can free up space on the phone.

- **Per phone:** each iPhone gets its own folder, matched by UDID (renaming the phone is fine).
- **Incremental + idempotent:** re-running copies only what's new. No duplicates.
- **Never deletes anything** — not on the phone, not in the backup.
- **Everything on the phone:** camera roll, photos synced from a computer, iCloud Shared Albums,
  and photos saved from Messages. Each sidecar's `source` says which. (Recently Deleted is skipped.)
- **Verified:** every file is SHA-256 checked after copying; partial copies never appear under a real name.

---

## 1. One-time setup

```bash
cd ~/photo-offload
uv venv --python 3.12          # skip if .venv already exists
uv pip install -r requirements.txt
```

Then plug in the phone, unlock it, and tap **Trust This Computer**.

**Try it:** confirm the Mac can see the phone.

```bash
source .venv/bin/activate
pymobiledevice3 usbmux list
```

You should see your phone's name and UDID. If not, unplug, unlock, replug, re-trust.

---

## 2. Back up a phone

Always run in this order. Keep the phone **unlocked and plugged in** the whole time.

```bash
source .venv/bin/activate

python offload.py --dry-run       # a) checks + size estimate, copies nothing
python offload.py --limit 20      # b) test: copy only the 20 newest items
python offload.py                 # c) full run (asks "Copy N items (X GB)? [y/N]")
```

Every run does these checks **before** copying anything:

1. Phone reachable (plugged in, unlocked, trusted) — otherwise stops with a hint.
2. What's new vs. already backed up, and what's missing (iCloud-only).
3. **Size estimate** of what will be copied vs. free space on the Mac
   (keeps a margin of 2 GB or 5%, whichever is bigger). Not enough → stops.
4. Asks you to confirm. Use `--yes` to skip the prompt.

Example:

```
✓ Phone: Aitch's iPhone (iPhone15,4, iOS 18.0)
✓ Photos library read: 5210 items on the phone
✓ Already backed up: 3370   Not on phone: 12
  To copy: 1828 items, 14.6 GB
  Free on Mac: 112.4 GB  (need 15.3 GB incl. safety margin)
✓ Enough space

Copy 1828 items (14.6 GB)? [y/N] y
Copying:  42%|████▏     | 6.1G/14.6G [03:10<04:25, 32.8MB/s, 771/1828 IMG_4412.HEIC]
```

The bar counts bytes, so the ETA is realistic even when videos are mixed in.
`count_dcim.py` still works for a quick total of everything on the phone.

After step **b**, open `~/PhotoBackup/` and check a couple of photos and their
`.json` sidecars before doing the full run.

The full run ends with a summary like:

```
Copied: 1840  Skipped (already done): 0  Duplicates: 3  Metadata refreshed: 0  Failed: 0
Everything taken up to 2026-10-04 18:22 is verified and safe to delete.
```

**Try it:** run `python offload.py` a second time straight away. It should
report `Copied: 0` and skip everything — that's the idempotency check.

---

## 3. Second phone

Unplug phone 1, plug in phone 2, trust it, and repeat section 2. It lands in its
own folder automatically — nothing to configure.

---

## 4. Freeing space on the phone

1. Read the **"safe to delete"** date from the last full run (not a `--limit` run).
2. On the phone, delete only photos taken **on or before** that date.
3. Empty **Recently Deleted** on the phone to actually reclaim the space.
4. Next time, run `offload.py` again before deleting anything new.

If `Failed` is not 0, re-run first. The safe date stops at the first item that
isn't verified, so it never over-promises.

---

## 5. What you get

```
~/PhotoBackup/
  Hesham-s-iPhone__00008110/
    device.json             # name, model, iOS, UDID
    manifest.sqlite         # what's been copied + checksums (the "memory" for re-runs)
    db-snapshots/<date>/    # copy of the phone's Photos database from each run
    logs/<date>.log         # every warning/error of that run (console shows only counts)
    media/                  # ALL photos/videos for this phone, one flat folder
      IMG_1234.HEIC
      IMG_1234.MOV          # Live Photo video part
      IMG_1234.AAE          # edits, if any
      IMG_1234.HEIC.json    # metadata sidecar
      IMG_1234_3F2A9C1B.HEIC  # same name already taken -> short UUID added
```

No year/month folders. Each file's date is set to the **date taken**, so in
Finder use *View → as List* and sort by **Date Modified** to browse
chronologically (or filter by date in Finder search).

**Try it:** after the `--limit 20` run, open `media/` in Finder, sort by Date
Modified, and check the order matches the order on your phone.

Sidecar example:

```json
{
  "albums": ["Cairo trip"],
  "caption": "Sunset from the roof",
  "date_taken": "2026-10-04T18:22:10",
  "favorite": true,
  "hidden": false,
  "latitude": 30.04,
  "longitude": 31.23,
  "original_path": "/DCIM/104APPLE/IMG_1234.HEIC",
  "source": "camera_roll",
  "uuid": "…"
}
```

Changed an album or caption on the phone? The next run rewrites just the `.json`
("Metadata refreshed"), without re-copying the photo.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `No device connected` / pairing error | Unlock phone, replug, tap Trust. Check `pymobiledevice3 usbmux list`. |
| `! not on phone: …` | Original lives only in iCloud (Optimize iPhone Storage). Download originals on the phone, or accept it's skipped. |
| `Failed` / `Unreadable` / `Not on phone` above 0 | Open the `Details:` log path printed at the end — one line per item, with the full error. |
| Run interrupted / cable pulled | Just re-run. Unfinished items restart; finished ones are skipped. |
| `size mismatch` / `verification failed` | Usually a flaky cable. Re-run; that item is retried. |
| iCloud-only items missing | By design — only files actually stored on the phone are copied. |
| `! N folder(s) not reachable over USB` | iOS didn't expose that folder (usually Messages items). Tell Claude the folder names. |

---

## 7. Viewer (local web app)

```bash
cd ~/photo-offload && source .venv/bin/activate
uvicorn viewer.app:app --reload
open http://localhost:8000
```

- **Sidebar:** pick a phone (or all), then All Photos / Favorites / Live Photos / Videos / Hidden, or an album.
- **Grid:** newest first, grouped by month. It loads 120 items at a time as you scroll, and
  only requests thumbnails for tiles near the screen. Drag **Size** to zoom.
- **Viewer:** click a tile. ← → to move, Esc to close. Live Photos play once on open;
  press and hold the photo, the **LIVE** button or Space to replay. Click an album tag to jump to it.
  📍 opens the location in Google Maps. **Download original** gives you the untouched file.
- **After a new backup run:** click **↻ Rescan backup**.
- HEIC is converted to JPEG with macOS `sips` (video posters with `qlmanage`), cached in
  `~/PhotoBackup/.viewer-cache/`. The first scroll through is slower; after that it's instant.
  Deleting that folder is safe — it's rebuilt as needed.
- **Screenshots / Downloaded tabs:** classified from the phone's Photos database
  (see `docs/SPEC_media_kind_classification.md`). In All Photos they carry a ▣ Screenshot or
  ⤓ Downloaded badge. New backups are tagged automatically; for photos backed up earlier run
  `python retag.py` once (no phone needed, only sidecars change), then click **↻ Rescan backup**.
  On older iOS without the needed column, screenshots are still found but downloads can't be.
- The URL keeps your place (e.g. `#view=live&phone=...`), so reload and bookmarks work.

---

## Files in this folder

| File | Purpose |
|---|---|
| `offload.py` | The backup tool. |
| `count_dcim.py` | Quick count + total size of media on the phone. |
| `check_local.py` | Diagnostic: compares the Photos database (`probe/Photos.sqlite`) against what's actually in DCIM. |
| `viewer/` | Local web viewer (FastAPI + plain JS). See section 7. |
| `retag.py` | Re-tags existing sidecars (screenshot/camera/saved) from the saved DB snapshot. |
| `probe/` | A Photos database copy used while exploring. Not needed for backups. |


---

## Connected phone page (pick what to import)

```bash
source .venv/bin/activate
uvicorn viewer.app:app --reload
open http://localhost:8000/phone.html     # or "📱 Connected phone" in the viewer sidebar
```

1. Plug in + unlock the phone, pick it, press **Connect**. It snapshots Photos.sqlite
   and measures every item (photo + Live Photo .MOV + edits) - takes a minute.
2. Default view: **Not backed up**, **Newest first** (switch Sort to Largest first to find space hogs). Filter by Photos / Videos / Live / Screenshots.
3. Click tiles to select (Shift-click = range, **Select all** = everything in the filter).
   ⤢ or double-click = big preview.
4. **Import** → same copy code as offload.py (same folder, manifest, SHA-256, sidecars),
   so imported items count as backed up for offload.py too. Stop = stops after the current file.

Thumbnails come from the phone's own thumbnail cache when available, else from the backup,
else from the original (videos > 150 MB show ▶ instead). Cache: ~/PhotoBackup/.viewer-cache/phone-*.
Log per import: <phone folder>/logs/<time>-viewer-import.log. **Never deletes anything.**
Code: viewer/phone.py, viewer/static/phone.{html,js,css}.
