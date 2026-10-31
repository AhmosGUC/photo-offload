import json, os
from pathlib import Path

APP_DIR = Path.home() / "Library/Application Support/Photo Backup"
CFG = APP_DIR / "config.json"
DEFAULT = Path.home() / "PhotoBackup"
PREF_DEFAULTS = {"auto_live": True, "muted": False}


def _load() -> dict:
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {}

def _save(d: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CFG.with_name(CFG.name + ".tmp")   # write + rename: never a half-written config
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, CFG)

def is_configured() -> bool:
    """True once the user has picked a backup folder."""
    return "backup_root" in _load()

def get_backup_root() -> Path:
    r = _load().get("backup_root")
    return Path(r).expanduser() if r else DEFAULT

def set_backup_root(path) -> Path:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    probe = p / ".write-test"          # fail early if drive is read-only
    probe.write_text("x"); probe.unlink()
    d = _load(); d["backup_root"] = str(p); _save(d)
    return p

def get_prefs() -> dict:
    saved = _load().get("prefs", {})
    return {k: saved.get(k, v) for k, v in PREF_DEFAULTS.items()}

def set_pref(key: str, value) -> dict:
    if key not in PREF_DEFAULTS:
        raise KeyError(key)
    d = _load(); d.setdefault("prefs", {})[key] = bool(value); _save(d)
    return get_prefs()
