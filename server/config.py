"""Configuration loader for rtsp-live-viewer.

Reads config.yaml (committed, sample defaults) and layers environment variables
(from a local, git-ignored .env) and the web-UI runtime file on top. Real
deployment values (camera URLs, credentials) live in .env / the web UI — never
hardcode them in committed files.

Precedence (low -> high): defaults <- config.yaml <- .env (environment) <- data/settings.json.
The web UI (settings.json) wins so live edits stick; .env seeds real values
without committing them.
"""
import json
import os
import re
import tempfile

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; env vars still work without a .env file
    def load_dotenv(*_a, **_k):
        return False

# Project root is the parent of this server/ package.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env (does not override variables already set in the real environment).
load_dotenv(os.environ.get("RLV_ENV") or os.path.join(ROOT_DIR, ".env"))

# ffmpeg binary path is shared across modules (encoders.py, streams.py).
FFMPEG = os.environ.get("RLV_FFMPEG") or os.environ.get("PBOX_FFMPEG") or "ffmpeg"

_DEFAULTS = {
    "encoder": "auto",
    "height": 720,
    "fps": 30,
    "video_bitrate": "2500k",
    "audio_mode": "aac",       # aac|copy|none
    "audio_bitrate": "128k",
    "hls_time": 2,
    "hls_list_size": 6,
    "buffer_seconds": 6,       # player-side
    "catch_up_rate": 1.2,      # player-side
    "grid_columns": "auto",    # "auto"|1|2|3|4
    "idle_timeout": 30,
    "output_dir": "/tmp/rtsp_live",
    # No hardcoded credential: password comes from .env (RLV_AUTH_PASSWORD) or
    # the web UI. Auth is off by default anyway.
    "auth": {"enabled": False, "user": "admin", "password": ""},
    "streams": [],
}

# Keys editable from the web UI and persisted to settings.json.
# Excludes output_dir / ffmpeg / env-only knobs (encoder still editable).
EDITABLE_KEYS = [
    "encoder",
    "height",
    "fps",
    "video_bitrate",
    "audio_mode",
    "audio_bitrate",
    "hls_time",
    "hls_list_size",
    "buffer_seconds",
    "catch_up_rate",
    "grid_columns",
    "idle_timeout",
    "auth",
    "streams",
]


class Config(dict):
    """A dict that also allows attribute access to its top-level keys."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _config_path():
    return os.environ.get("RLV_CONFIG", os.path.join(ROOT_DIR, "config.yaml"))


def _data_dir():
    return os.environ.get("RLV_DATA_DIR", os.path.join(ROOT_DIR, "data"))


def _settings_path():
    return os.path.join(_data_dir(), "settings.json")


def _read_yaml():
    """Return config.yaml as a dict (empty on missing/invalid)."""
    try:
        with open(_config_path(), encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        return loaded if isinstance(loaded, dict) else {}
    except OSError:
        return {}


def _read_settings():
    """Return data/settings.json as a dict (empty on missing/invalid)."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _clean_streams(raw):
    """Validate/normalize a streams list: require unique non-empty id."""
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for s in raw:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        url = s.get("url")
        if not sid or sid in seen:
            continue  # drop empty/duplicate ids
        seen.add(sid)
        result.append({
            "id": sid,
            "name": s.get("name") or sid,
            "url": url,
            "enabled": bool(s.get("enabled", True)),
        })
    return result


def _env_overrides():
    """Scalar/auth overrides pulled from environment variables (.env)."""
    out = {}
    if os.environ.get("RLV_ENCODER"):
        out["encoder"] = os.environ["RLV_ENCODER"]
    auth = {}
    enabled = os.environ.get("RLV_AUTH_ENABLED")
    if enabled:
        auth["enabled"] = enabled.strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("RLV_AUTH_USER"):
        auth["user"] = os.environ["RLV_AUTH_USER"]
    if os.environ.get("RLV_AUTH_PASSWORD"):
        auth["password"] = os.environ["RLV_AUTH_PASSWORD"]
    if auth:
        out["auth"] = auth
    return out


def _env_streams(base):
    """Apply stream overrides from env onto a base list (from config.yaml).

    RLV_STREAMS (JSON) replaces the list entirely. Otherwise per-id overrides
    RLV_STREAM_<ID>_URL / RLV_STREAM_<ID>_NAME patch matching entries (ID is the
    stream id upper-cased with non-alphanumerics turned into '_').
    """
    if os.environ.get("RLV_STREAMS"):
        try:
            return _clean_streams(json.loads(os.environ["RLV_STREAMS"]))
        except ValueError:
            pass
    result = []
    for s in base:
        key = re.sub(r"[^A-Z0-9]", "_", s["id"].upper())
        s2 = dict(s)
        url = os.environ.get("RLV_STREAM_%s_URL" % key)
        name = os.environ.get("RLV_STREAM_%s_NAME" % key)
        if url:
            s2["url"] = url
        if name:
            s2["name"] = name
        result.append(s2)
    return result


def load():
    """Merge defaults <- config.yaml <- .env (env) <- data/settings.json.

    Deep merge for auth (dict). For streams: settings.json (web UI) wins entirely
    when present; otherwise config.yaml's list with .env per-id overrides applied.
    """
    data = dict(_DEFAULTS)
    yaml_data = _read_yaml()
    env_data = _env_overrides()
    settings = _read_settings()

    # Flat keys: later sources override earlier ones.
    for src in (yaml_data, env_data, settings):
        for k, v in src.items():
            if k in ("auth", "streams"):
                continue
            data[k] = v

    # auth: deep merge across all layers (settings/UI highest).
    auth = dict(_DEFAULTS["auth"])
    for src in (yaml_data, env_data, settings):
        if isinstance(src.get("auth"), dict):
            auth.update(src["auth"])
    data["auth"] = Config(auth)

    # streams: web UI wins entirely; else config.yaml + .env per-id overrides.
    if isinstance(settings.get("streams"), list):
        data["streams"] = _clean_streams(settings["streams"])
    else:
        base = _clean_streams(yaml_data["streams"]) if isinstance(yaml_data.get("streams"), list) else []
        data["streams"] = _env_streams(base)

    data["ffmpeg"] = FFMPEG
    return Config(data)


def save(updates):
    """Persist editable updates into data/settings.json and return a fresh Config.

    Only EDITABLE_KEYS are taken from `updates`; they are merged over the
    existing settings.json so unrelated fields are preserved. The write is
    atomic (tmp file + os.replace). Returns load() afterwards.

    Password handling: a missing/empty updates.auth.password keeps the existing
    password; a non-empty value replaces it.

    Raises ValueError on invalid streams (empty/duplicate ids).
    """
    if not isinstance(updates, dict):
        raise ValueError("updates must be an object")

    existing = _read_settings()
    merged = dict(existing)

    for key in EDITABLE_KEYS:
        if key not in updates:
            continue
        val = updates[key]

        if key == "auth":
            if not isinstance(val, dict):
                continue
            cur_auth = existing.get("auth") if isinstance(existing.get("auth"), dict) else {}
            new_auth = dict(cur_auth)
            if "enabled" in val:
                new_auth["enabled"] = bool(val["enabled"])
            if "user" in val:
                new_auth["user"] = val["user"]
            # Keep existing password unless a non-empty one is supplied.
            pw = val.get("password")
            if pw:
                new_auth["password"] = pw
            merged["auth"] = new_auth

        elif key == "streams":
            cleaned = _validate_streams(val)
            merged["streams"] = cleaned

        else:
            merged[key] = val

    _write_settings(merged)
    return load()


def _validate_streams(raw):
    """Strict validation for incoming streams: error on empty/duplicate id."""
    if not isinstance(raw, list):
        raise ValueError("streams must be a list")
    result = []
    seen = set()
    for s in raw:
        if not isinstance(s, dict):
            raise ValueError("each stream must be an object")
        sid = str(s.get("id") or "").strip()
        if not sid:
            raise ValueError("stream id is required")
        if sid in seen:
            raise ValueError("duplicate stream id: %s" % sid)
        seen.add(sid)
        result.append({
            "id": sid,
            "name": s.get("name") or sid,
            "url": s.get("url"),
            "enabled": bool(s.get("enabled", True)),
        })
    return result


def _write_settings(data):
    """Atomically write the settings dict to data/settings.json."""
    ddir = _data_dir()
    os.makedirs(ddir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ddir, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _settings_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
