# Spec: Classifying backed-up media (screenshot / camera / saved-imported)

Status: **implemented 2026-10-06** (provisional rule below): `offload.py` writes `kind` + raw `kind_codes` to sidecars, `retag.py` re-tags existing backups from the saved DB snapshot, viewer has Screenshots / Downloaded tabs. Open questions below still apply.

Original status: research findings. Written 2026-10-06 from live queries on the user's own `Photos.sqlite` (the database `offload.py` reads, found under `~/PhotoBackup/<phone>/.../2026-10-05_184914/Photos.sqlite`).

Goal: add a `kind` field to each item's `.json` sidecar and a `kind` filter to the viewer, for photos that are **already backed up** (re-tag by re-reading the DB; no file re-copy needed).

## Where the signal lives

There is **no column named "screenshot"** anywhere in the DB (checked every table with `pragma_table_info ... LIKE '%SCREENSHOT%'`, zero hits). The signal is in integer codes on `ZASSET`:

| Column | Meaning |
|---|---|
| `ZKINDSUBTYPE` | Asset subtype (screenshot, live photo, pano, special video) |
| `ZSAVEDASSETTYPE` | How the asset got into the library (captured here vs. came from outside) |
| `ZFILENAME` | Original filename (useful for cross-checks) |

Other candidate columns seen but not yet used: `ZKIND`, `ZIMPORTSESSION`, `ZLIBRARYSCOPEORIGINATOR`, `ZCLOUDPLACEHOLDERKIND`.

Codes can shift between iOS versions and the user has two phones (iPhone 15 and an older iPhone 12 on an older iOS). **Re-verify per phone/DB before trusting the mapping.**

## Observed data (one DB, ~22,000 assets)

`ZSAVEDASSETTYPE` counts: `3`=19272, `256`=1763, `4`=1185, `12`=12, `0`=1.

`ZKINDSUBTYPE` counts: `0`=9737, `2`=9519, `10`=2814, `100`=94, `103`=34, `102`=19, `1`=10, `101`=6.

Subtype vs file extension:

- `10`: PNG 2782, JPG 32 (almost all PNG, which matches iOS screenshots)
- `2`: HEIC 9251, JPG 266, JPEG 2 (Live Photos)
- `0`: HEIC 5150, JPG 2864, MOV 1537, JPEG 69, MP4 60, PNG 47, WEBP 8, GIF 2
- `1`: HEIC 7, JPG 3 (likely panoramas)
- `100`-`103`: MP4/MOV only (special videos)

Cross-tab `ZKINDSUBTYPE | ZSAVEDASSETTYPE | count`:

```
0|3|7138   0|256|1741  0|4|845  0|12|12  0|0|1
1|3|5      1|4|3       1|256|2
2|3|9276   2|4|243
10|3|2794  10|256|20
100|4|94   101|3|6     102|3|19  103|3|34
```

Sample filenames: group `ZSAVEDASSETTYPE=256` is `IMG_0002..IMG_0016.heic`; group `=4` is `IMG_0002..IMG_0019.JPG`. Low sequential numbers that do not continue the camera counter suggest items that came from outside the phone.

## Mapping

### Verified (strong evidence)
- `ZKINDSUBTYPE = 10` is **screenshot** (2782/2814 are PNG).
- `ZKINDSUBTYPE = 2` is **Live Photo** (HEIC-dominated).
- `ZSAVEDASSETTYPE = 3` is **created on this device** (contains the Live Photos and screenshots, so it is camera plus screenshots).

### Provisional (needs eyeball verification)
- `ZSAVEDASSETTYPE` in `(4, 256, 12, 0)` means **saved/imported from outside** (WhatsApp, Safari, AirDrop, imports). Which source maps to which code is **unknown**. `4` is JPG-heavy, `256` is HEIC-heavy, so they may be different sources.
- `ZKINDSUBTYPE = 1` is probably panorama; `100`-`103` special videos (slow-mo, time-lapse, etc.). Not needed for this feature.

### Provisional classification rule

```sql
SELECT CASE
  WHEN ZKINDSUBTYPE = 10  THEN 'screenshot'
  WHEN ZSAVEDASSETTYPE = 3 THEN 'camera'
  ELSE 'saved/imported'
END AS kind, COUNT(*)
FROM ZASSET GROUP BY 1;
```

Edge case: 20 screenshots have `ZSAVEDASSETTYPE=256` (screenshots that arrived from outside). The rule above labels them `screenshot`; decide whether that is desired.

## Open questions / next steps

1. **Eyeball-verify** 3 files from `ZSAVEDASSETTYPE=256` and 3 from `=4` (what are they: WhatsApp, AirDrop, downloads?). Do the same on the second phone's DB.
2. **Source app probe** (best signal for "saved to gallery"). Find columns:
   ```sql
   SELECT m.name, p.name FROM sqlite_master m, pragma_table_info(m.name) p
   WHERE p.name LIKE '%IMPORTEDBY%' OR p.name LIKE '%ORIGINALFILENAME%';
   ```
   If e.g. `ZIMPORTEDBYBUNDLEIDENTIFIER` exists in `ZADDITIONALASSETATTRIBUTES`:
   ```sql
   SELECT a.ZSAVEDASSETTYPE, aa.ZIMPORTEDBYBUNDLEIDENTIFIER, COUNT(*)
   FROM ZASSET a JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
   GROUP BY 1,2 ORDER BY 1,3 DESC;
   ```
   This could expose the exact source app per group and let us split `saved/imported` further.
3. **Fallback if the DB signal is missing** (other phone/iOS): classify from EXIF with `exiftool`. Camera = `Make=Apple` plus `LensModel`/`ExposureTime`; screenshot = PNG without camera EXIF (often `UserComment=Screenshot`); everything else = saved/other.

## Implementation plan (not done yet)

1. In `offload.py`, where sidecars are written, add `kind` (and `source_app` if step 2 works), derived from the DB columns above.
2. Add a re-tagging pass that re-reads each backup's `Photos.sqlite` and updates existing `.json` sidecars in place, matching by filename. Never touch or re-copy media.
3. Viewer: add `kind=screenshot|camera|saved` to `/api/items` and `/api/summary` counts, plus a sidebar filter. Rescan backup after re-tagging.

## Constraints to respect

- Tool never deletes media; runs are incremental and idempotent; one flat folder per phone; phones are never mixed.
- Treat the mapping as per-phone/per-iOS-version data, not a global constant.
