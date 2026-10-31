#!/usr/bin/env bash
# Photo Backup: one-shot build. Run from ~/photo-offload with the venv active:
#   bash build.sh
set -u
APP="Photo Backup"

say() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

say "1/4 Environment check"
[ -f launcher.py ] || { echo "Run this from the project root (launcher.py not found)."; exit 1; }
file "$(which python)" | grep -q arm64 && echo "Python: arm64 OK" \
  || echo "WARNING: Python is not arm64. Build will be Intel-only (Rosetta)."

say "2/4 Install PyInstaller + build"
if command -v uv >/dev/null; then uv pip install --quiet -r requirements-build.txt || exit 1
else pip install --quiet -r requirements-build.txt || exit 1; fi
rm -rf build dist
# "Photo Backup.spec" is the source of truth for the build (static files, icon, bundle id).
pyinstaller --noconfirm "Photo Backup.spec" || { echo "BUILD FAILED. See the output above."; exit 1; }

[ -d "dist/$APP.app" ] || { echo "App bundle missing."; exit 1; }
echo "Built: dist/$APP.app"

say "3/4 Smoke test (launch, wait 8s, check it is alive, then close)"
"dist/$APP.app/Contents/MacOS/$APP" > build/smoke.log 2>&1 &
PID=$!
sleep 8
if kill -0 $PID 2>/dev/null; then
  echo "PASS: process still running after 8s. A window should be visible."
  kill $PID 2>/dev/null; sleep 1
else
  echo "FAIL: process exited early. Log tail:"
  tail -n 30 build/smoke.log
  echo "Typical fix: ModuleNotFoundError X -> add --hidden-import X (or --collect-all X)."
  exit 1
fi

say "4/4 Leftover-process check"
pgrep -fl "$APP" && echo "WARNING: process still alive after close" || echo "Clean exit: nothing left running"

echo
echo "Next: double-click dist/\"$APP.app\" from Finder and import 5 photos."
