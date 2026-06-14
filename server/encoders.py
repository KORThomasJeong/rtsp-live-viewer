"""H.264 encoder detection and ffmpeg argument selection.

`ffmpeg -encoders` only tells us what is *compiled in* — it lists h264_nvenc /
h264_qsv even on machines with no matching GPU, which then fail at runtime
("Cannot load nvcuda" / "Could not open encoder"). So for hardware encoders we
do a real capability probe: a tiny throwaway encode to the null muxer, and only
treat the encoder as available if it actually succeeds. Software libx264 is
trusted from the compiled list.
"""
import re
import subprocess

import config

# Auto-detect order: prefer hardware encoders, fall back to software libx264.
_AUTO_ORDER = ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "libx264"]
_HW = {"h264_videotoolbox", "h264_nvenc", "h264_qsv"}
_FALLBACK = "libx264"

_compiled = None   # set of encoder names compiled into this ffmpeg
_usable = {}       # encoder name -> bool (functional probe result, cached)


def _compiled_set():
    global _compiled
    if _compiled is not None:
        return _compiled
    names = set()
    try:
        out = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.decode("utf-8", "replace")
        # Lines look like: " V..... libx264   libx264 H.264 ..."
        for m in re.finditer(r"^\s*[A-Z.]{6}\s+(\S+)", out, re.M):
            names.add(m.group(1))
    except OSError:
        pass
    _compiled = names
    return _compiled


def _probe(name):
    """Run a tiny encode to verify the encoder actually works on this machine."""
    if name in _usable:
        return _usable[name]
    ok = False
    if name in _compiled_set():
        cmd = [
            config.FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=0.2",
            "-frames:v", "3", "-c:v", name, "-f", "null", "-",
        ]
        try:
            ok = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    _usable[name] = ok
    return ok


def is_usable(name):
    """Software encoders trusted from the compiled list; hardware ones probed."""
    if name in _HW:
        return _probe(name)
    return name in _compiled_set()


def available():
    """Return the set of H.264 encoder names that actually work here."""
    return {e for e in _AUTO_ORDER if is_usable(e)}


def resolve(name):
    """Resolve a requested encoder to one that actually works on this machine."""
    if name == "auto":
        for cand in _AUTO_ORDER:
            if is_usable(cand):
                return cand
        return _FALLBACK
    if is_usable(name):
        return name
    return _FALLBACK


def video_args(name, bitrate):
    """Return the ffmpeg `-c:v ...` argument list for an encoder."""
    if name == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-b:v", bitrate]
    if name == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll"]
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    # libx264 (and the catch-all fallback).
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
