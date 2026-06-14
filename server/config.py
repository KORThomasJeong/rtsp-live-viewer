"""Configuration loader for rtsp-live-viewer.

Reads config.yaml (project root by default, overridable via RLV_CONFIG) as the
committed defaults, then merges data/settings.json on top of it as the runtime
override edited from the web UI. A few values can also be overridden by
environment variables so deployments don't have to edit anything.

Precedence (low -> high): defaults <- config.yaml <- data/settings.json <- env.
"""
import json
import os
import tempfile

import yaml

# Project root is the parent of this server/ package.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ffmpeg binary path is shared across modules (encoders.py, streams.py).
FFMPEG = os.environ.get("PBOX_FFMPEG", "ffmpeg")

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
    "auth": {"enabled": False, "user": "admin", "password": "admin"},
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


def load():
    """Merge defaults <- config.yaml <- data/settings.json, then env overrides.

    Deep merge for auth (dict). For streams (list), settings.json wins entirely
    when present, otherwise config.yaml's list is used.
    """
    data = dict(_DEFAULTS)
    yaml_data = _read_yaml()
    settings = _read_settings()

    # Flat keys: later sources override earlier ones.
    for src in (yaml_data, settings):
        for k, v in src.items():
            if k in ("auth", "streams"):
                continue
            data[k] = v

    # auth: deep merge across all three layers.
    auth = dict(_DEFAULTS["auth"])
    for src in (yaml_data, settings):
        if isinstance(src.get("auth"), dict):
            auth.update(src["auth"])
    data["auth"] = Config(auth)

    # streams: settings.json wins when present, else config.yaml, else default.
    if isinstance(settings.get("streams"), list):
        data["streams"] = _clean_streams(settings["streams"])
    elif isinstance(yaml_data.get("streams"), list):
        data["streams"] = _clean_streams(yaml_data["streams"])
    else:
        data["streams"] = []

    # Environment overrides.
    if os.environ.get("RLV_ENCODER"):
        data["encoder"] = os.environ["RLV_ENCODER"]
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
