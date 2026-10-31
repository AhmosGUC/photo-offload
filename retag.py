#!/usr/bin/env python3
"""
retag.py - refresh the .json sidecars of photos that are ALREADY backed up,
using the Photos database snapshot offload.py saved on the Mac. No phone needed.

  python retag.py            # all phones
  python retag.py --dry-run  # show what would change, write nothing

Adds/updates "kind" (screenshot / camera / saved) and the raw "kind_codes"
(see SPEC_media_kind_classification.md). Only sidecars are rewritten - media
files are never touched, copied or deleted. Safe to run any number of times.
Afterwards click "Rescan backup" in the viewer.
"""
import argparse, sqlite3
from collections import Counter
from pathlib import Path

from offload import backup_root, open_manifest, read_library, write_meta


def latest_snapshot(phone_dir: Path):
    snaps = sorted(phone_dir.glob("db-snapshots/*/Photos.sqlite"))
    return snaps[-1] if snaps else None


def retag_phone(d: Path, dry_run: bool):
    db = latest_snapshot(d)
    if not db:
        print(f"{d.name}: no database snapshot, skipped")
        return
    library = read_library(db)
    con = open_manifest(d)
    kinds, codes, updated = Counter(), Counter(), 0
    for asset in library:
        row = con.execute("SELECT dest_dir, dest_stem, status FROM assets WHERE uuid=?",
                          (asset["uuid"],)).fetchone()
        if not row or row[2] != "verified":
            continue                     # not backed up (or a duplicate) - nothing to tag
        kinds[asset["meta"]["kind"]] += 1
        c = asset["meta"]["kind_codes"]
        codes[(c["subtype"], c["saved_type"])] += 1
        if not dry_run and write_meta(con, Path(row[0]), row[1], Path(asset["filename"]).suffix, asset):
            updated += 1
    con.commit()
    con.close()

    print(f"\n{d.name}  (snapshot {db.parent.name})")
    print("  " + "  ".join(f"{k or 'unknown'}: {n}" for k, n in kinds.most_common()))
    print(f"  sidecars {'that would change' if dry_run else 'updated'}: "
          f"{'(not checked in dry run)' if dry_run else updated}")
    print("  raw codes (subtype, saved_type): " +
          ", ".join(f"{k}={n}" for k, n in sorted(codes.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    root = backup_root()
    phones = sorted(f.parent for f in root.glob("*/device.json"))
    if not phones:
        print(f"No phone folders in {root}")
    for d in phones:
        retag_phone(d, args.dry_run)
    if phones and not args.dry_run:
        print("\nDone. In the viewer, click '↻ Rescan backup'.")
