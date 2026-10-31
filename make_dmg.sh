#!/usr/bin/env bash
# Package dist/"Photo Backup.app" into a drag-to-install DMG.
# Run on the Mac from the project root, after `bash build.sh`:
#   bash make_dmg.sh
set -euo pipefail
APP="Photo Backup"
VERSION="${1:-1.0}"
SRC="dist/$APP.app"
OUT="dist/PhotoBackup-$VERSION.dmg"
STAGE="build/dmg-stage"

[ -d "$SRC" ] || { echo "Missing $SRC — run: bash build.sh"; exit 1; }

echo "== Ad-hoc signing (lets it run on Apple Silicon; not notarized) =="
codesign --force --deep --sign - "$SRC"
codesign --verify --deep --strict "$SRC" && echo "Signature OK"

echo "== Staging =="
rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"
cp -R "$SRC" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-to-Applications shortcut

echo "== Building DMG =="
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
rm -rf "$STAGE"

echo
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Arch: $(lipo -archs "$SRC/Contents/MacOS/$APP")"
