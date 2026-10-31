#!/usr/bin/env python3
"""
explain_gap.py - why the Photos app count != offload.py's count.

  python explain_gap.py                  # uses the newest db-snapshot in the backup folder
  python explain_gap.py path/to/Photos.sqlite

No phone needed: it only reads the database snapshot the last dry run saved.
"""
import sqlite3, sys
from pathlib import Path

import config

if len(sys.argv) > 1:
    db_path = Path(sys.argv[1])
else:
    snaps = sorted(config.get_backup_root().glob("*/db-snapshots/*/Photos.sqlite"))
    if not snaps:
        sys.exit("No snapshot found - run `python offload.py --dry-run` first.")
    db_path = snaps[-1]
print(f"Database: {db_path}\n")

db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
q = lambda sql: db.execute(sql).fetchall()

# What the Photos app "Library" shows: not deleted, not hidden, visible
# (visibility 2 = extra burst frames / hidden pairs folded into one tile).
GALLERY = "ZTRASHEDSTATE=0 AND ZHIDDEN=0 AND ZVISIBILITYSTATE=0"
# What offload.py backs up (every source, same WHERE as offload.read_library).
SCRIPT = "ZTRASHEDSTATE=0 AND ZDIRECTORY IS NOT NULL AND ZFILENAME IS NOT NULL"

def where(dir_):
    d = dir_ or ""
    if d.startswith("DCIM/"):                          return "DCIM (on phone)"
    if "CPLAssets" in d:                                return "PhotoData/CPLAssets (iCloud Photos copy)"
    if "PhotoCloudSharingData" in d:                    return "Shared Albums"
    if d.startswith("PhotoData/Sync") or "Sync" in d:   return "Synced from a computer"
    return "other: " + d.split("/")[0] + "/" + (d.split("/")[1] if "/" in d else "")

kinds = {0: "photos", 1: "videos"}
g = dict(q(f"SELECT ZKIND, COUNT(*) FROM ZASSET WHERE {GALLERY} GROUP BY 1"))
s = q(f"SELECT COUNT(*) FROM ZASSET WHERE {SCRIPT}")[0][0]
print(f"Gallery (Photos app):  {g.get(0,0)} photos + {g.get(1,0)} videos = {sum(g.values())}")
print(f"offload.py library:    {s}\n")

print("In the GALLERY but NOT picked up by offload.py:")
rows = q(f"SELECT ZDIRECTORY, ZKIND FROM ZASSET WHERE {GALLERY} AND NOT ({SCRIPT})")
buckets = {}
for d, k in rows:
    buckets.setdefault(where(d), [0, 0])[k if k in (0, 1) else 0] += 1
for b, (p, v) in sorted(buckets.items(), key=lambda x: -sum(x[1])):
    print(f"  {sum((p,v)):>6}  {b}   ({p} photos, {v} videos)")
print(f"  {len(rows):>6}  total\n")

print("Picked up by offload.py but NOT counted in the gallery:")
h = q(f"SELECT COUNT(*) FROM ZASSET WHERE {SCRIPT} AND ZHIDDEN=1")[0][0]
b = q(f"SELECT COUNT(*) FROM ZASSET WHERE {SCRIPT} AND ZHIDDEN=0 AND ZVISIBILITYSTATE!=0")[0][0]
print(f"  {h:>6}  Hidden album items")
print(f"  {b:>6}  Extra burst frames (gallery shows 1 tile per burst)")
print(f"  {h+b:>6}  total\n")

print(f"Check: {sum(g.values())} - {len(rows)} + {h+b} = {sum(g.values()) - len(rows) + h + b}  (should equal {s})")
