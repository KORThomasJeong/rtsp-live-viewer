"""Multi-channel RTSP/RTMP/HTTP -> H.264/HLS transcoding manager.

Each stream runs its own ffmpeg process writing HLS segments into a per-stream
directory under output_dir. Streams come from config.yaml or are created
ad-hoc from a URL. An optional idle reaper stops streams nobody is watching.
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time

import encoders

PLAYLIST = "index.m3u8"
_ALLOWED_SCHEME = re.compile(r"^(rtsp|rtmp|rtmps|http|https)://", re.I)

_lock = threading.Lock()
# id -> {proc, url, name, dir, last_access, adhoc}
_streams = {}

_cfg = None
_encoder = None  # resolved active encoder name
_reaper_started = False


def configure(cfg):
    """Inject config, resolve the encoder once, and start the idle reaper."""
    global _cfg, _encoder, _reaper_started
    with _lock:
        _cfg = cfg
        _encoder = encoders.resolve(cfg.encoder)
    print("[streams] active encoder: %s (requested %s)" % (_encoder, cfg.encoder))
    # Only ever start one reaper, even across reconfigure() calls.
    if cfg.idle_timeout and cfg.idle_timeout > 0 and not _reaper_started:
        _reaper_started = True
        t = threading.Thread(target=_reaper, daemon=True)
        t.start()


def reconfigure(cfg):
    """Apply new settings at runtime: refresh config/encoder and stop all
    running streams so the new ffmpeg parameters take effect. The frontend is
    expected to re-start enabled streams on reload. The reaper is not restarted
    here (configure() already guards against duplicates)."""
    global _cfg, _encoder
    with _lock:
        _cfg = cfg
        _encoder = encoders.resolve(cfg.encoder)
        for sid in list(_streams.keys()):
            _stop_locked(sid)
    print("[streams] reconfigured; active encoder: %s (requested %s)"
          % (_encoder, cfg.encoder))


def active_encoder():
    return _encoder


def is_valid_url(url):
    return bool(url) and bool(_ALLOWED_SCHEME.match(url))


def _output_dir():
    return _cfg.output_dir if _cfg else "/tmp/rtsp_live"


def _stream_dir(sid):
    return os.path.join(_output_dir(), sid)


def _config_stream(sid):
    """Find a configured stream dict by id, or None."""
    if not _cfg:
        return None
    for s in _cfg.streams:
        if str(s.get("id")) == str(sid):
            return s
    return None


def _running(entry):
    proc = entry.get("proc") if entry else None
    return proc is not None and proc.poll() is None


def list_streams():
    """Configured streams plus any ad-hoc streams, with running state."""
    with _lock:
        result = []
        seen = set()
        if _cfg:
            for s in _cfg.streams:
                sid = str(s.get("id"))
                seen.add(sid)
                entry = _streams.get(sid)
                result.append({
                    "id": sid,
                    "name": s.get("name") or sid,
                    "url": s.get("url"),
                    "enabled": bool(s.get("enabled", True)),
                    "running": _running(entry),
                })
        for sid, entry in _streams.items():
            if sid in seen:
                continue
            result.append({
                "id": sid,
                "name": entry.get("name") or sid,
                "url": entry.get("url"),
                "enabled": True,  # ad-hoc streams are always enabled
                "running": _running(entry),
            })
        return result


def _stop_locked(sid):
    entry = _streams.get(sid)
    if not entry:
        return
    proc = entry.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    entry["proc"] = None


def _build_cmd(url, sdir):
    """Assemble the ffmpeg argv for one stream from config + encoder."""
    height = int(_cfg.height) if _cfg else 720
    fps = int(_cfg.fps) if _cfg else 30
    bitrate = str(_cfg.video_bitrate) if _cfg else "2500k"
    hls_time = int(_cfg.hls_time) if _cfg else 2
    hls_list_size = int(_cfg.hls_list_size) if _cfg else 6
    audio_mode = (_cfg.audio_mode if _cfg else "aac") or "aac"
    audio_bitrate = str(_cfg.audio_bitrate) if _cfg else "128k"

    gop = (fps if fps and fps > 0 else 30) * hls_time  # 1 keyframe per segment

    cmd = [
        _cfg.ffmpeg if _cfg else "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", url,
    ]
    if height and height > 0:
        cmd += ["-vf", "scale=-2:%d" % height]
    if fps and fps > 0:
        cmd += ["-r", str(fps)]
    cmd += encoders.video_args(_encoder, bitrate)
    # Align segment boundaries to keyframes so each HLS segment is self-contained;
    # mismatched GOP/segment boundaries are a common source of audio/video
    # glitches ("robotic" sound) at every segment switch in the browser.
    cmd += [
        "-g", str(gop),
        "-force_key_frames", "expr:gte(t,n_forced*%d)" % hls_time,
    ]
    # Audio handling per config.
    if audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    elif audio_mode == "none":
        cmd += ["-an"]
    else:  # "aac" (default): clean, fixed AAC kept in sync.
        cmd += [
            "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000", "-ac", "2",
            "-af", "aresample=async=1:first_pts=0",
        ]
    cmd += [
        "-f", "hls", "-hls_time", str(hls_time), "-hls_list_size", str(hls_list_size),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
        os.path.join(sdir, PLAYLIST),
    ]
    return cmd


def _start(sid, url, name, adhoc):
    """Internal: (re)start a stream by id. Caller supplies resolved url/name."""
    if not is_valid_url(url):
        return {"ok": False, "error": "invalid or unsupported url"}

    with _lock:
        entry = _streams.get(sid)
        if entry and _running(entry) and entry.get("url") == url:
            entry["last_access"] = time.time()
            return {"ok": True, "id": sid, "playlist": "/hls/%s/%s" % (sid, PLAYLIST),
                    "reused": True}
        # Stop any stale process and clear/recreate just this stream's dir.
        if entry:
            _stop_locked(sid)
        sdir = _stream_dir(sid)
        shutil.rmtree(sdir, ignore_errors=True)
        os.makedirs(sdir, exist_ok=True)
        cmd = _build_cmd(url, sdir)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _streams[sid] = {
            "proc": proc, "url": url, "name": name, "dir": sdir,
            "last_access": time.time(), "adhoc": adhoc,
        }

    # Warm-up: wait for the first playlist + .ts segment to appear.
    playlist_path = os.path.join(_stream_dir(sid), PLAYLIST)
    for _ in range(80):  # up to ~16s
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", "replace")[-500:] if proc.stderr else ""
            return {"ok": False, "error": "ffmpeg exited: " + err.strip()}
        sdir = _stream_dir(sid)
        if os.path.exists(playlist_path) and any(
            f.endswith(".ts") for f in os.listdir(sdir)
        ):
            return {"ok": True, "id": sid, "playlist": "/hls/%s/%s" % (sid, PLAYLIST)}
        time.sleep(0.2)
    return {"ok": False, "error": "timed out waiting for stream"}


def start(sid):
    """Start a configured stream by id."""
    sid = str(sid)
    cfg_stream = _config_stream(sid)
    if cfg_stream:
        return _start(sid, cfg_stream.get("url"), cfg_stream.get("name") or sid, False)
    # Allow restarting a previously-registered ad-hoc stream by id.
    entry = _streams.get(sid)
    if entry:
        return _start(sid, entry.get("url"), entry.get("name") or sid, entry.get("adhoc", True))
    return {"ok": False, "error": "unknown stream id"}


def start_url(url):
    """Start an ad-hoc stream from a raw URL, keyed by a hash of the URL."""
    if not is_valid_url(url):
        return {"ok": False, "error": "invalid or unsupported url"}
    sid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return _start(sid, url, url, True)


def stop(sid):
    sid = str(sid)
    with _lock:
        _stop_locked(sid)


def status(sid):
    sid = str(sid)
    with _lock:
        entry = _streams.get(sid)
        running = _running(entry)
        return {"running": running, "url": entry.get("url") if entry else None}


def note_access(sid):
    """Record that a viewer just fetched media for this stream."""
    sid = str(sid)
    with _lock:
        entry = _streams.get(sid)
        if entry:
            entry["last_access"] = time.time()


def file_path(sid, name):
    """Resolve a requested HLS file safely inside the stream's directory."""
    sid = str(sid)
    sdir = _stream_dir(sid)
    full = os.path.normpath(os.path.join(sdir, name))
    if not full.startswith(os.path.abspath(sdir)):
        return None
    return full if os.path.isfile(full) else None


def _reaper():
    """Stop streams that haven't been accessed within idle_timeout seconds."""
    while True:
        timeout = _cfg.idle_timeout if _cfg else 0
        time.sleep(min(timeout, 10) if timeout > 0 else 10)
        if not timeout or timeout <= 0:
            continue
        now = time.time()
        with _lock:
            for sid, entry in list(_streams.items()):
                if _running(entry) and (now - entry.get("last_access", now)) > timeout:
                    print("[streams] idle stop: %s" % sid)
                    _stop_locked(sid)
