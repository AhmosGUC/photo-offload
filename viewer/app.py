"""
viewer/app.py - local web viewer for ~/PhotoBackup

  cd ~/photo-offload && source .venv/bin/activate
  uvicorn viewer.app:app --reload
  open http://localhost:8000              # the backup
  open http://localhost:8000/phone.html   # the phone plugged in right now

Reads the .json sidecars offload.py wrote, keeps an index in memory, and
serves pages of items, cached JPEG thumbnails/previews and the originals.
"""
import json, shutil, subprocess, tempfile, threading
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles


import config

def backup_root():
    return config.get_backup_root()

def cache_dir():
    d = backup_root() / ".viewer-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

STATIC = Path(__file__).parent / "static"
THUMB_PX, PREVIEW_PX = 400, 2048
IMAGE_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".dng", ".webp", ".tif", ".tiff"}
BROWSER_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CACHE_HEADERS = {"Cache-Control": "max-age=86400"}   # one day: cached images can change
LOCAL_HOSTS = {"127.0.0.1", "localhost"}

INDEX: dict = {"phones": {}, "items": []}
VIEWS = ("all", "favorites", "live", "videos", "screenshots", "downloaded", "hidden")


# ---------------------------------------------------------------- index
def build_index():
    """Scan every phone folder once. One item per sidecar (= one photo or video)."""
    phones, items = {}, []
    for d in sorted(backup_root().glob("*/device.json")):
        phone_dir = d.parent
        pid = phone_dir.name
        phones[pid] = json.loads(d.read_text()).get("name", pid)
        media = phone_dir / "media"
        if not media.is_dir():
            continue
        names = {p.name for p in media.iterdir()}
        for sc in media.glob("*.json"):
            try:
                meta = json.loads(sc.read_text())
            except ValueError:
                continue
            file = sc.name[:-len(".json")]           # IMG_1234.HEIC.json -> IMG_1234.HEIC
            if file not in names:
                continue
            stem, ext = Path(file).stem, Path(file).suffix.lower()
            live = next((f"{stem}{e}" for e in (".MOV", ".mov") if f"{stem}{e}" in names), None) \
                if ext in IMAGE_EXT else None
            items.append({
                "id": f"{pid}/{file}",
                "phone": pid,
                "file": file,
                "kind": "image" if ext in IMAGE_EXT else "video",
                "live": live,                         # companion .MOV of a Live Photo
                "mkind": meta.get("kind"),            # screenshot / camera / saved (retag.py)
                "date": meta.get("date_taken"),
                "favorite": bool(meta.get("favorite")),
                "hidden": bool(meta.get("hidden")),
                "albums": meta.get("albums") or [],
                "caption": meta.get("caption"),
                "lat": meta.get("latitude"), "lon": meta.get("longitude"),
            })
    items.sort(key=lambda i: i["date"] or "", reverse=True)   # newest first
    INDEX["phones"], INDEX["items"] = phones, items
    print(f"indexed {len(items)} items from {len(phones)} phone(s)")


@asynccontextmanager
async def lifespan(_app):
    build_index()
    yield

app = FastAPI(lifespan=lifespan)


def _hostname(value: str) -> str | None:
    try:
        return urlsplit(value if "//" in value else "//" + value).hostname
    except ValueError:
        return None


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Only this Mac's own window may talk to us: blocks DNS rebinding (Host) and
    other websites posting to the API from a normal browser (Origin)."""
    if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
        return PlainTextResponse("forbidden host", status_code=403)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if origin is not None and _hostname(origin) not in LOCAL_HOSTS:
            return PlainTextResponse("forbidden origin", status_code=403)
    return await call_next(request)


def media_path(phone: str, file: str) -> Path:
    """Resolve a file inside a phone's media folder - and refuse anything outside it."""
    if phone not in INDEX["phones"] or Path(file).name != file:
        raise HTTPException(404)
    p = backup_root() / phone / "media" / file
    if not p.is_file():
        raise HTTPException(404)
    return p


def filtered(phone=None, album=None, view="all"):
    rows = INDEX["items"]
    if phone:
        rows = [i for i in rows if i["phone"] == phone]
    if view == "hidden":
        return [i for i in rows if i["hidden"]]
    rows = [i for i in rows if not i["hidden"]]          # hidden stays out of everything else
    if album:
        rows = [i for i in rows if album in i["albums"]]
    if view == "favorites":
        rows = [i for i in rows if i["favorite"]]
    elif view == "live":
        rows = [i for i in rows if i["live"]]
    elif view == "videos":
        rows = [i for i in rows if i["kind"] == "video"]
    elif view == "screenshots":
        rows = [i for i in rows if i["mkind"] == "screenshot"]
    elif view == "downloaded":
        rows = [i for i in rows if i["mkind"] == "saved"]
    return rows


# ---------------------------------------------------------------- api
@app.get("/api/summary")
def summary(phone: str | None = None):
    """Everything the sidebar needs: phones, counts per view, albums."""
    albums: dict[str, int] = {}
    for i in filtered(phone):
        for a in i["albums"]:
            albums[a] = albums.get(a, 0) + 1
    return {
        "phones": [{"id": k, "name": v, "count": len(filtered(k))} for k, v in INDEX["phones"].items()],
        "views": {v: len(filtered(phone, view=v)) for v in VIEWS},
        "albums": [{"name": k, "count": v} for k, v in sorted(albums.items(), key=lambda x: x[0].lower())],
    }


@app.get("/api/items")
def list_items(phone: str | None = None, album: str | None = None,
               view: str = Query("all", pattern="^(" + "|".join(VIEWS) + ")$"),
               offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=500)):
    """One page of items, newest first. The grid asks for the next page as you scroll."""
    rows = filtered(phone, album, view)
    return {"total": len(rows), "offset": offset, "items": rows[offset:offset + limit]}


@app.post("/api/reindex")
def reindex():
    build_index()
    return {"items": len(INDEX["items"])}


# ---------------------------------------------------------------- image conversion
# A fixed set of striped locks instead of one per path (which grew forever).
# "phone:" keys get their own stripes: phone_image holds one while make_image
# takes another, and separate pools keep that ordering deadlock-free.
_STRIPES = 64
_locks = [threading.Lock() for _ in range(_STRIPES)]
_phone_locks = [threading.Lock() for _ in range(_STRIPES)]

def _lock(key):
    pool = _phone_locks if key.startswith("phone:") else _locks
    return pool[hash(key) % _STRIPES]


def make_image(src: Path, dest: Path, px: int) -> bool:
    """Photo -> JPEG (max px) with macOS `sips`. Video -> poster frame with `qlmanage`."""
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
        return True
    with _lock(str(dest)):
        if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp.jpg")
        ok = False
        if src.suffix.lower() in IMAGE_EXT and shutil.which("sips"):
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                                "-Z", str(px), str(src), "--out", str(tmp)], capture_output=True)
            ok = r.returncode == 0 and tmp.is_file()
        elif src.suffix.lower() not in IMAGE_EXT and shutil.which("qlmanage"):
            with tempfile.TemporaryDirectory() as td:
                subprocess.run(["qlmanage", "-t", "-s", str(px), "-o", td, str(src)],
                               capture_output=True, timeout=60)
                out = next(Path(td).glob("*.png"), None)
                if out and shutil.which("sips"):
                    r = subprocess.run(["sips", "-s", "format", "jpeg", str(out), "--out", str(tmp)],
                                       capture_output=True)
                    ok = r.returncode == 0 and tmp.is_file()
        if not ok:  # fallback (non-Mac / formats Pillow can read)
            try:
                from PIL import Image
                with Image.open(src) as im:
                    im.thumbnail((px, px))
                    im.convert("RGB").save(tmp, "JPEG", quality=80)
                ok = True
            except Exception:
                ok = False
        if ok:
            tmp.replace(dest)
        return ok


def _jpeg(phone, file, px, folder):
    src = media_path(phone, file)
    dest = cache_dir() / folder / phone / f"{file}.jpg"
    if not make_image(src, dest, px):
        raise HTTPException(404, "no preview")
    return FileResponse(dest, media_type="image/jpeg", headers=CACHE_HEADERS)


@app.get("/thumb/{phone}/{file}")
def thumb(phone: str, file: str):
    return _jpeg(phone, file, THUMB_PX, "thumbs")


@app.get("/preview/{phone}/{file}")
def preview(phone: str, file: str):
    """Large image the browser can show. JPG/PNG are served as-is, HEIC is converted."""
    if Path(file).suffix.lower() in BROWSER_IMAGE_EXT:
        return FileResponse(media_path(phone, file))
    return _jpeg(phone, file, PREVIEW_PX, "previews")


@app.get("/media/{phone}/{file}")
def media(phone: str, file: str, download: bool = False):
    """Original file. iPhone .MOV is served as video/mp4 so Chrome will play it too."""
    p = media_path(phone, file)
    mt = "video/mp4" if p.suffix.lower() in (".mov", ".mp4", ".m4v") else None
    return FileResponse(p, media_type=mt, filename=file if download else None,
                        content_disposition_type="attachment" if download else "inline")


# Connected-phone page (/phone.html): live list of the plugged-in iPhone + import.
from viewer.phone import router as phone_router  # noqa: E402
app.include_router(phone_router)

# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def get_settings():
    return {"backup_root": str(backup_root()), "configured": config.is_configured(),
            "prefs": config.get_prefs()}


@app.post("/api/settings/pref")
def set_pref(body: dict = Body(...)):
    """Viewer preferences (auto-play Live Photos, sound) - saved in config.json."""
    try:
        return {"prefs": config.set_pref(str(body.get("key")), body.get("value"))}
    except KeyError:
        raise HTTPException(400, "unknown setting")


@app.post("/api/settings/backup-root")
def set_backup_root(body: dict = Body(...)):
    """Switch the backup folder. Old backups stay where they are; the new folder gets its own index."""
    from viewer.phone import S, STATE_LOCK, on_loop, close_phone
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "No folder given")
    with STATE_LOCK:    # no scan/import may start while we switch
        if S.state in ("scanning", "importing"):
            raise HTTPException(409, "Wait until the phone scan/import has finished, then try again.")
        try:
            p = config.set_backup_root(path)
        except OSError as e:
            raise HTTPException(400, f"Can't write to that folder ({e.strerror or e})")
        try:            # close the old phone connection before forgetting it
            on_loop(close_phone(), timeout=10)
        except Exception:
            pass
        S.__init__()    # the phone page pointed at the old folder: reconnect to rescan
    build_index()       # the library now shows the new folder
    return {"backup_root": str(p), "configured": True, "items": len(INDEX["items"])}


# static files must stay LAST, otherwise they swallow the /api routes above
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
