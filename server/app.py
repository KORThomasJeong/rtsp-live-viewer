"""Flask app for rtsp-live-viewer.

Serves the web/ UI and a small JSON API that drives the per-channel ffmpeg
transcoders in streams.py. HLS media is served under /hls/; when auth is
enabled it is protected too (browsers reuse Digest credentials per realm).
"""
import hashlib
import hmac
import json
import os
import re
import secrets

from flask import Flask, Response, request, send_from_directory

import config
import encoders
import streams

# All H.264 encoders the UI knows about (whether or not usable here).
_ALL_ENCODERS = ["libx264", "h264_videotoolbox", "h264_nvenc", "h264_qsv"]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.environ.get("RLV_WEB_DIR", os.path.join(BASE_DIR, "web"))

# Digest auth parameters. The nonce is randomised per process start (instead of
# a fixed value) so digest responses can't be trivially replayed across restarts.
# Note: this is a lightweight LAN-oriented guard, not a substitute for HTTPS.
# For real exposure, put the app behind a TLS reverse proxy.
REALM = "rtsp-live-viewer"
NONCE = secrets.token_hex(16)

app = Flask(__name__, static_folder=None)

_cfg = None  # populated by configure() / __main__


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _parse_digest(header):
    parts = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]+))', header):
        parts[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return parts


def _auth_ok():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("digest "):
        return False
    d = _parse_digest(header[len("digest "):])
    auth = _cfg.auth
    if d.get("username") != auth.get("user"):
        return False
    ha1 = _md5("%s:%s:%s" % (d.get("username", ""), REALM, auth.get("password")))
    ha2 = _md5("%s:%s" % (request.method, d.get("uri", "")))
    if d.get("qop"):
        resp = _md5(
            "%s:%s:%s:%s:%s:%s"
            % (ha1, d.get("nonce", ""), d.get("nc", ""), d.get("cnonce", ""), d.get("qop"), ha2)
        )
    else:
        resp = _md5("%s:%s:%s" % (ha1, d.get("nonce", ""), ha2))
    # Constant-time compare, and require the client to echo our own nonce.
    if d.get("nonce") != NONCE:
        return False
    return hmac.compare_digest(resp, d.get("response") or "")


def _challenge():
    return Response(
        status=401,
        headers={
            "WWW-Authenticate": 'Digest qop="auth", realm="%s", nonce="%s"' % (REALM, NONCE)
        },
    )


@app.before_request
def _require_auth():
    # HLS media is NOT exempt: when auth is enabled, segment requests must also
    # authenticate. Browsers reuse Digest credentials within the realm, so
    # hls.js fetches authenticate transparently without per-segment prompts.
    if _cfg and _cfg.auth.get("enabled") and not _auth_ok():
        return _challenge()
    return None


def _json(obj):
    return Response(json.dumps(obj), mimetype="application/json")


# --- API -------------------------------------------------------------------
@app.route("/api/streams")
def api_streams():
    return _json({"streams": streams.list_streams(), "encoder": streams.active_encoder()})


@app.route("/api/streams/<sid>/start")
def api_start(sid):
    return _json(streams.start(sid))


@app.route("/api/streams/<sid>/stop")
def api_stop(sid):
    streams.stop(sid)
    return _json({"ok": True})


@app.route("/api/streams/<sid>/status")
def api_status(sid):
    return _json(streams.status(sid))


@app.route("/api/play")
def api_play():
    url = request.args.get("url", "")
    return _json(streams.start_url(url))


# --- Settings API ----------------------------------------------------------
def _config_payload():
    """Build the effective-config JSON (never exposes the actual password)."""
    auth = _cfg.auth
    return {
        "encoder": _cfg.encoder,
        "height": _cfg.height,
        "fps": _cfg.fps,
        "video_bitrate": _cfg.video_bitrate,
        "audio_mode": _cfg.audio_mode,
        "audio_bitrate": _cfg.audio_bitrate,
        "hls_time": _cfg.hls_time,
        "hls_list_size": _cfg.hls_list_size,
        "buffer_seconds": _cfg.buffer_seconds,
        "catch_up_rate": _cfg.catch_up_rate,
        "grid_columns": _cfg.grid_columns,
        "idle_timeout": _cfg.idle_timeout,
        "auth": {
            "enabled": bool(auth.get("enabled")),
            "user": auth.get("user"),
            "password_set": bool(auth.get("password")),
        },
        "streams": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "url": s.get("url"),
                "enabled": bool(s.get("enabled", True)),
            }
            for s in _cfg.streams
        ],
        "encoders": {
            "available": sorted(encoders.available()),
            "all": list(_ALL_ENCODERS),
            "active": streams.active_encoder(),
        },
    }


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return _json(_config_payload())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    global _cfg
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "invalid JSON body"}), 400
    try:
        new_cfg = config.save(body)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}), 400
    # Refresh app config and apply to the running stream manager.
    _cfg = new_cfg
    streams.reconfigure(new_cfg)
    return _json(_config_payload())


# --- HLS media -------------------------------------------------------------
@app.route("/hls/<sid>/<path:name>")
def hls_file(sid, name):
    streams.note_access(sid)
    full = streams.file_path(sid, name)
    if not full:
        return Response(status=404)
    d, f = os.path.split(full)
    mime = "application/vnd.apple.mpegurl" if f.endswith(".m3u8") else "video/mp2t"
    resp = send_from_directory(d, f, mimetype=mime)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --- Static UI -------------------------------------------------------------
@app.route("/")
def index():
    return _static("index.html")


@app.route("/<path:path>", methods=["GET"])
def static_path(path):
    return _static(path)


def _static(path):
    full = os.path.normpath(os.path.join(WEB_DIR, path))
    if not full.startswith(os.path.abspath(WEB_DIR)):
        return Response(status=403)
    if os.path.isfile(full):
        d, f = os.path.split(full)
        return send_from_directory(d, f)
    return Response(status=404)


def configure(cfg):
    global _cfg
    _cfg = cfg
    streams.configure(cfg)


if __name__ == "__main__":
    _cfg = config.load()
    configure(_cfg)
    port = int(os.environ.get("RLV_PORT") or 80)
    app.run(host="0.0.0.0", port=port, threaded=True)
