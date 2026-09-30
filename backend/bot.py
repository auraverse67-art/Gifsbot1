#!/usr/bin/env python3
"""
MediaForge — one bot for every GIF / sticker / video / image / audio conversion.

Requirements in PATH: ffmpeg, ffprobe
Python deps:          see requirements.txt

Performance: every tool uses the fastest available ffmpeg settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Set the BOT_TOKEN environment variable.")

# Your bot's public username — change if needed
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Mediaforg3_bot").lstrip("@")

WORK = Path(tempfile.gettempdir()) / "mediaforge"
WORK.mkdir(parents=True, exist_ok=True)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_PATH = DATA_DIR / "users.json"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

ANIM_STICKER_MAX = 256 * 1024
STATIC_STICKER_MAX = 512 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_BATCH_FILES = 20

BG_CHOICES = {
    "white": ("White", "white"),
    "black": ("Black", "black"),
    "gray":  ("Gray",  "0x808080"),
    "green": ("Green", "0x00B140"),
    "blue":  ("Blue",  "0x1E90FF"),
    "pink":  ("Pink",  "0xFFB6C1"),
}

FIT512 = "scale=w='if(gte(a,1),512,-2)':h='if(gte(a,1),-2,512)'"

# FAST presets
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
X264_THREADS = ["-threads", "0"]
AAC = ["-c:a", "aac", "-b:a", "128k"]

VP9_FAST = ["-c:v", "libvpx-vp9",
            "-deadline", "realtime", "-cpu-used", "8",
            "-row-mt", "1", "-threads", "4"]

GIF_GRAPH = (f"fps=12,{FIT512},format=rgba,"
             "split[a][b];"
             "[a]palettegen=max_colors=180:reserve_transparent=1:stats_mode=single[p];"
             "[b][p]paletteuse=dither=none:alpha_threshold=128")

logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mediaforge")

if not ADMIN_TOKEN:
    ADMIN_TOKEN = "mf_" + secrets.token_hex(16)
    log.warning("ADMIN_TOKEN not set — generated: %s", ADMIN_TOKEN)
if not ADMIN_ID:
    log.warning("ADMIN_ID not set — /admin and /stats disabled.")


# ──────────────────────────────────────────────────────────────────────────────
#  Runtime stats
# ──────────────────────────────────────────────────────────────────────────────

STATS = {
    "start_time": time.time(),
    "jobs_done": 0,
    "jobs_failed": 0,
    "bytes_out": 0,
    "seconds_spent": 0.0,
}


class ConversionError(Exception):
    pass


# ──────────────────────────────────────────────────────────────────────────────
#  User tracking
# ──────────────────────────────────────────────────────────────────────────────

USERS: dict[str, dict] = {}


def load_users() -> None:
    if not USERS_PATH.exists():
        return
    try:
        data = json.loads(USERS_PATH.read_text())
        if isinstance(data, dict):
            USERS.update(data)
            log.info("Loaded %d tracked users.", len(USERS))
    except Exception:
        log.warning("Could not read users.json")


def save_users() -> None:
    try:
        tmp = USERS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(USERS, indent=2))
        tmp.replace(USERS_PATH)
    except Exception:
        log.warning("Could not write users.json")


def track_seen(user) -> None:
    if user is None:
        return
    uid = str(user.id)
    now = time.time()
    u = USERS.get(uid)
    if not u:
        u = {
            "id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "first_seen": now,
            "last_seen": now,
            "jobs": 0,
            "fails": 0,
            "cmds": 0,
            "bytes_out": 0,
            "tools": {},
        }
        USERS[uid] = u
    if user.username:
        u["username"] = user.username
    if user.first_name:
        u["first_name"] = user.first_name
    u["last_seen"] = now


def track_cmd(user) -> None:
    if user is None:
        return
    track_seen(user)
    USERS[str(user.id)]["cmds"] = USERS[str(user.id)].get("cmds", 0) + 1
    save_users()


def track_job(user_id, tool_key: str, bytes_out: int, ok: bool = True) -> None:
    if not user_id:
        return
    uid = str(user_id)
    u = USERS.get(uid)
    if not u:
        return
    if ok:
        u["jobs"] = u.get("jobs", 0) + 1
        u["tools"][tool_key] = u["tools"].get(tool_key, 0) + 1
        u["bytes_out"] = u.get("bytes_out", 0) + bytes_out
    else:
        u["fails"] = u.get("fails", 0) + 1
    save_users()


def aggregate_stats() -> dict:
    now = time.time()
    day, week = 86400, 7 * 86400
    tools: dict[str, int] = {}
    users_today = users_week = 0
    for u in USERS.values():
        last = u.get("last_seen", 0)
        if now - last < day:
            users_today += 1
        if now - last < week:
            users_week += 1
        for k, v in (u.get("tools") or {}).items():
            tools[k] = tools.get(k, 0) + v
    return {
        "uptime_seconds": round(now - STATS["start_time"], 1),
        "jobs_done": STATS["jobs_done"],
        "jobs_failed": STATS["jobs_failed"],
        "bytes_out": STATS["bytes_out"],
        "users_total": len(USERS),
        "users_today": users_today,
        "users_week": users_week,
        "tools": tools,
        "generated_at": now,
    }


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(ADMIN_ID) and u is not None and u.id == ADMIN_ID


# ──────────────────────────────────────────────────────────────────────────────
#  Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _work_size() -> int:
    total = 0
    for p in WORK.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")


def parse_time(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("empty")
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"bad time: {s}")
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    frac = float("0." + m.group(4)) if m.group(4) else 0.0
    return h * 3600 + mm * 60 + ss + frac


def first_name_of(user) -> str:
    if user is None:
        return "there"
    return (user.first_name or user.username or "there").split()[0]


# ──────────────────────────────────────────────────────────────────────────────
#  ffmpeg / ffprobe helpers
# ──────────────────────────────────────────────────────────────────────────────

def check_binaries() -> None:
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise SystemExit(f"Missing binaries in PATH: {', '.join(missing)}")


async def _ffmpeg(args: list[str], timeout: int = 300) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ConversionError("That took too long. Try a shorter clip.")
    if proc.returncode != 0:
        text = err.decode("utf-8", "ignore").strip()
        tail = text.splitlines()[-1] if text else "ffmpeg failed"
        raise ConversionError(tail[:300])


async def _probe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ConversionError(err.decode("utf-8", "ignore")[:300] or "Couldn't read that file.")
    return json.loads(out)


def _has_audio(meta: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in meta.get("streams", []))


def _duration(meta: dict) -> float:
    try:
        return float(meta.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _media_info(path: Path) -> str:
    try:
        meta = await _probe(path)
        v = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
        a = next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), None)
        parts: list[str] = []
        if v:
            w, h = v.get("width"), v.get("height")
            if w and h:
                parts.append(f"{w}×{h}")
            dur = float(v.get("duration") or _duration(meta) or 0)
            if dur:
                parts.append(f"{dur:.1f}s")
        elif a:
            dur = float(a.get("duration") or _duration(meta) or 0)
            if dur:
                parts.append(f"{dur:.1f}s")
        return " · ".join(parts)
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
#  Converters (all tuned for max speed)
# ──────────────────────────────────────────────────────────────────────────────

async def _animated_sticker(src: Path, out: Path, opts: dict) -> None:
    profiles = [(3.0, 24, 36), (3.0, 20, 44), (2.0, 16, 52)]
    vf_tail = ("format=yuva420p,"
               "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1")
    best = 0
    for dur, fps, crf in profiles:
        await _ffmpeg([
            "-t", str(dur), "-i", str(src), "-an",
            "-vf", f"fps={fps},{FIT512},{vf_tail}",
            *VP9_FAST,
            "-pix_fmt", "yuva420p",
            "-b:v", "0", "-crf", str(crf),
            "-auto-alt-ref", "0", "-lag-in-frames", "0",
            str(out),
        ])
        best = out.stat().st_size
        if best <= ANIM_STICKER_MAX:
            return
    raise ConversionError(
        f"Couldn't get it under 256 KB (best {best/1024:.0f} KB). "
        "Try a shorter or simpler clip."
    )


async def _static_sticker(src: Path, out: Path, opts: dict) -> None:
    for q in (88, 70, 48):
        await _ffmpeg([
            "-i", str(src), "-frames:v", "1",
            "-vf", f"{FIT512},format=rgba,"
                   "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0",
            "-c:v", "libwebp", "-pix_fmt", "yuva420p",
            "-lossless", "0", "-q:v", str(q),
            "-compression_level", "0", "-preset", "picture", "-an",
            str(out),
        ])
        if out.stat().st_size <= STATIC_STICKER_MAX:
            return
    raise ConversionError("Couldn't fit that into a static sticker. Try a simpler image.")


async def _gif(src: Path, out: Path, opts: dict) -> None:
    await _ffmpeg([
        "-t", str(min(10, opts.get("max_seconds", 10))), "-i", str(src), "-an",
        "-filter_complex", GIF_GRAPH, "-loop", "0", str(out),
    ])


async def _mp4(src: Path, out: Path, opts: dict) -> None:
    bg = opts.get("bg", "white")
    graph = (f"[1:v]fps=24,{FIT512},format=rgba[fg];"
             f"color=c={bg}:s=512x512:r=24[base];"
             "[base][fg]overlay=(W-w)/2:(H-h)/2:eof_action=endall,format=yuv420p[v]")
    await _ffmpeg([
        "-f", "lavfi", "-i", f"color=c={bg}:s=512x512:r=24",
        "-i", str(src),
        "-filter_complex", graph, "-map", "[v]", "-an",
        *X264, *X264_THREADS,
        str(out),
    ])


async def _png(src: Path, out: Path, opts: dict) -> None:
    await _ffmpeg([
        "-i", str(src), "-frames:v", "1",
        "-vf", "format=rgba", "-c:v", "png", "-f", "image2", str(out),
    ])


async def _jpg(src: Path, out: Path, opts: dict) -> None:
    bg = opts.get("bg", "white")
    await _ffmpeg([
        "-i", str(src), "-frames:v", "1",
        "-vf", f"{FIT512},format=rgb24,"
               f"pad=512:512:(ow-iw)/2:(oh-ih)/2:color={bg}",
        "-c:v", "mjpeg", "-q:v", "3", "-f", "image2", str(out),
    ])


async def _webp(src: Path, out: Path, opts: dict) -> None:
    await _ffmpeg([
        "-i", str(src), "-frames:v", "1",
        "-vf", f"{FIT512},format=rgba",
        "-c:v", "libwebp", "-lossless", "0", "-q:v", "85",
        "-compression_level", "0", "-an",
        str(out),
    ])


async def _mp3(src: Path, out: Path, opts: dict) -> None:
    meta = await _probe(src)
    if not _has_audio(meta):
        raise ConversionError("That file has no audio to extract.")
    await _ffmpeg([
        "-i", str(src), "-vn",
        "-c:a", "libmp3lame", "-q:a", "4",
        str(out),
    ])


async def _frames_zip(src: Path, out: Path, opts: dict) -> None:
    count = int(opts.get("frames", 12))
    meta = await _probe(src)
    dur = _duration(meta) or 3.0
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmpdir = Path(tmp)
        await _ffmpeg([
            "-i", str(src),
            "-vf", f"fps={count}/{dur},{FIT512}",
            "-frames:v", str(count),
            "-q:v", "4",
            str(tmpdir / "f%03d.jpg"),
        ])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            for p in sorted(tmpdir.glob("*.jpg")):
                zf.write(p, p.name)


async def _trim(src: Path, out: Path, opts: dict) -> None:
    start = float(opts.get("start", 0))
    dur = float(opts.get("duration", 5))

    try:
        await _ffmpeg([
            "-ss", str(start), "-t", str(dur), "-i", str(src),
            "-c", "copy",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
            str(out),
        ])
        if out.exists() and out.stat().st_size > 0:
            return
    except ConversionError:
        pass

    await _ffmpeg([
        "-ss", str(start), "-t", str(dur), "-i", str(src),
        *X264, *AAC, *X264_THREADS,
        str(out),
    ])


async def _cut_range(src: Path, out: Path, opts: dict) -> None:
    start = float(opts.get("cut_start", 0))
    end = float(opts.get("cut_end", 0))
    if end <= start:
        raise ConversionError("End time must be after start time.")
    meta = await _probe(src)
    total = _duration(meta)
    if end > total + 0.1:
        raise ConversionError(
            f"Video is only {total:.1f}s long. End time must be within it.")
    has_a = _has_audio(meta)

    if has_a:
        fc = (
            f"[0:v]trim=0:{start},setpts=PTS-STARTPTS[v0];"
            f"[0:v]trim={end},setpts=PTS-STARTPTS[v1];"
            f"[0:a]atrim=0:{start},asetpts=PTS-STARTPTS[a0];"
            f"[0:a]atrim={end},asetpts=PTS-STARTPTS[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        await _ffmpeg([
            "-i", str(src),
            "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]",
            *X264, *AAC, *X264_THREADS,
            str(out),
        ])
    else:
        fc = (
            f"[0:v]trim=0:{start},setpts=PTS-STARTPTS[v0];"
            f"[0:v]trim={end},setpts=PTS-STARTPTS[v1];"
            "[v0][v1]concat=n=2:v=1:a=0[v]"
        )
        await _ffmpeg([
            "-i", str(src),
            "-filter_complex", fc,
            "-map", "[v]", "-an",
            *X264, *X264_THREADS,
            str(out),
        ])


async def _speed(src: Path, out: Path, opts: dict) -> None:
    factor = float(opts.get("factor", 2.0))
    pts = 1.0 / factor
    atempo = factor
    chain = []
    while atempo > 2.0:
        chain.append("atempo=2.0"); atempo /= 2.0
    while atempo < 0.5:
        chain.append("atempo=0.5"); atempo *= 2.0
    chain.append(f"atempo={atempo:.4f}")
    await _ffmpeg([
        "-i", str(src),
        "-filter_complex", f"[0:v]setpts={pts}*PTS[v];[0:a]{','.join(chain)}[a]",
        "-map", "[v]", "-map", "[a]",
        *X264, *AAC, *X264_THREADS,
        str(out),
    ])


async def _reverse(src: Path, out: Path, opts: dict) -> None:
    meta = await _probe(src)
    if _duration(meta) > 60:
        raise ConversionError("Reverse works best on clips under 60 s.")
    has_a = _has_audio(meta)
    if has_a:
        await _ffmpeg([
            "-i", str(src),
            "-filter_complex", "[0:v]reverse[v];[0:a]areverse[a]",
            "-map", "[v]", "-map", "[a]",
            *X264, *AAC, *X264_THREADS,
            str(out),
        ])
    else:
        await _ffmpeg([
            "-i", str(src), "-vf", "reverse", "-an",
            *X264, *X264_THREADS,
            str(out),
        ])


async def _mute(src: Path, out: Path, opts: dict) -> None:
    await _ffmpeg([
        "-i", str(src), "-an", "-c:v", "copy",
        "-movflags", "+faststart",
        str(out),
    ])


async def _rotate(src: Path, out: Path, opts: dict) -> None:
    deg = int(opts.get("degrees", 90))
    if deg == 180:
        try:
            await _ffmpeg([
                "-i", str(src),
                "-c", "copy",
                "-metadata:s:v", "rotate=180",
                "-movflags", "+faststart",
                str(out),
            ])
            if out.exists() and out.stat().st_size > 0:
                return
        except ConversionError:
            pass

    transpose = {90: "transpose=1", 180: "transpose=1,transpose=1",
                 270: "transpose=2"}[deg]
    await _ffmpeg([
        "-i", str(src), "-vf", transpose,
        *X264, "-c:a", "copy", *X264_THREADS,
        str(out),
    ])


async def _flip(src: Path, out: Path, opts: dict) -> None:
    direction = opts.get("direction", "h")
    vf = "hflip" if direction == "h" else "vflip"
    await _ffmpeg([
        "-i", str(src), "-vf", vf,
        *X264, "-c:a", "copy", *X264_THREADS,
        str(out),
    ])


async def _compress(src: Path, out: Path, opts: dict) -> None:
    level = opts.get("level", "medium")
    crf = {"high": "22", "medium": "28", "low": "34"}[level]
    await _ffmpeg([
        "-i", str(src),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", crf,
        "-c:a", "aac", "-b:a", "96k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-threads", "0",
        str(out),
    ])


async def _watermark(src: Path, out: Path, opts: dict) -> None:
    text = (opts.get("text") or "").strip()
    if not text:
        raise ConversionError("I need some text to stamp.")
    safe = (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\\'").replace("%", "\\%"))[:80]
    vf = (f"drawtext=text='{safe}':"
          "fontcolor=white:fontsize=28:"
          "x=(w-text_w)/2:y=h-th-20:"
          "box=1:boxcolor=black@0.6:boxborderw=10")
    await _ffmpeg([
        "-i", str(src), "-vf", vf,
        *X264, "-c:a", "copy", *X264_THREADS,
        str(out),
    ])


async def _resize(src: Path, out: Path, opts: dict) -> None:
    size = int(opts.get("size", 512))
    await _ffmpeg([
        "-i", str(src),
        "-vf", f"scale='if(gte(a,1),{size},-2)':'if(gte(a,1),-2,{size})'",
        *X264, "-c:a", "copy", *X264_THREADS,
        str(out),
    ])


async def _crop(src: Path, out: Path, opts: dict) -> None:
    pct = float(opts.get("pct", 10))
    pct = max(0.5, min(45.0, pct))
    f = pct / 100.0
    meta = await _probe(src)
    v = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ConversionError("That file has no video to crop.")
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if w <= 0 or h <= 0:
        raise ConversionError("Couldn't detect video dimensions.")
    nw = max(2, int(w * (1 - 2 * f)) // 2 * 2)
    nh = max(2, int(h * (1 - 2 * f)) // 2 * 2)
    vf = f"crop={nw}:{nh}"
    await _ffmpeg([
        "-i", str(src), "-vf", vf,
        *X264, "-c:a", "copy", *X264_THREADS,
        str(out),
    ])


async def _remove_bg(src: Path, out: Path, opts: dict) -> None:
    vf = "format=rgba,colorkey=0xFFFFFF:0.30:0.10,format=rgba"
    await _ffmpeg([
        "-i", str(src), "-frames:v", "1",
        "-vf", f"scale='if(gte(a,1),512,-2)':'if(gte(a,1),-2,512)',{vf}",
        "-c:v", "png", "-f", "image2",
        str(out),
    ])


async def _detach_audio(src: Path, out: Path, opts: dict) -> None:
    meta = await _probe(src)
    if not _has_audio(meta):
        raise ConversionError("That file has no audio to detach.")
    a = next(s for s in meta.get("streams", []) if s.get("codec_type") == "audio")
    codec = a.get("codec_name", "")

    if codec == "mp3":
        try:
            await _ffmpeg([
                "-i", str(src), "-vn", "-c:a", "copy",
                str(out),
            ])
            if out.exists() and out.stat().st_size > 0:
                return
        except ConversionError:
            pass

    await _ffmpeg([
        "-i", str(src), "-vn",
        "-c:a", "libmp3lame", "-q:a", "4",
        str(out),
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Tool registry — cleaner labels with action words
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    category: str
    kinds: tuple[str, ...]
    handler: Callable[[Path, Path, dict], Awaitable[None]]
    ext: str
    options: tuple[tuple[str, str, str], ...] = ()
    needs_text: bool = False
    needs_time: bool = False
    hint: str = ""


TOOLS: dict[str, Tool] = {t.key: t for t in [
    # 📦 Convert
    Tool("gif",      "Make a GIF",           "convert", ("anim",),          _gif,              ".gif",
         hint="Looping animation, 512px"),
    Tool("mp4",      "Make an MP4",          "convert", ("static", "anim"), _mp4,              ".mp4",
         hint="Plays everywhere"),
    Tool("asticker", "Make a moving sticker","convert", ("anim",),          _animated_sticker, ".webm",
         hint="≤3s, transparent"),
    Tool("ssticker", "Make a still sticker", "convert", ("static", "anim"), _static_sticker,   ".webp",
         hint="512×512, transparent"),
    Tool("png",      "Make a PNG",           "convert", ("static", "anim"), _png,              ".png",
         hint="Full quality, transparent"),
    Tool("jpg",      "Make a JPG",           "convert", ("static", "anim"), _jpg,              ".jpg",
         hint="Small photo"),
    Tool("webp",     "Make a WebP",          "convert", ("static", "anim"), _webp,             ".webp",
         hint="Modern, small"),
    Tool("mp3",      "Extract MP3",          "convert", ("anim",),          _mp3,              ".mp3",
         hint="Get the audio"),

    # ✂️ Cut & Trim
    Tool("trim",     "Keep first N seconds","cut", ("anim",), _trim, ".mp4",
         hint="Cut the end off (instant)",
         options=(("5", "First 5 seconds", ""), ("10", "First 10 seconds", ""),
                  ("30", "First 30 seconds", ""), ("60", "First 60 seconds", ""))),
    Tool("cut_range","Delete a section",     "cut", ("anim",), _cut_range, ".mp4",
         hint="Remove the middle of a video",
         needs_time=True),

    # 🎛 Transform
    Tool("speed",    "Change speed",         "transform", ("anim",), _speed, ".mp4",
         hint="Slow down or speed up",
         options=(("0.5", "Half speed (0.5×)", ""), ("1.5", "Faster (1.5×)", ""),
                  ("2", "Double speed (2×)", ""), ("3", "Triple speed (3×)", ""))),
    Tool("reverse",  "Play backwards",       "transform", ("anim",), _reverse, ".mp4",
         hint="Reverse the video"),
    Tool("rotate",   "Rotate",               "transform", ("anim", "static"), _rotate, ".mp4",
         hint="Turn the video",
         options=(("90", "Turn 90° right", ""), ("180", "Upside down (180°)", ""),
                  ("270", "Turn 90° left (270°)", ""))),
    Tool("flip",     "Mirror",               "transform", ("anim", "static"), _flip, ".mp4",
         hint="Flip horizontally or vertically",
         options=(("h", "Mirror left to right", ""), ("v", "Flip top to bottom", ""))),
    Tool("resize",   "Change resolution",    "transform", ("anim",), _resize, ".mp4",
         hint="Change the video size",
         options=(("256", "Small (256 px)", ""), ("512", "Medium (512 px)", ""),
                  ("1024", "Large (1024 px)", ""))),

    # 🧼 Cleanup
    Tool("mute",     "Remove audio",         "cleanup", ("anim",), _mute, ".mp4",
         hint="Silent video (instant)"),
    Tool("detach",   "Split out the audio",  "cleanup", ("anim",), _detach_audio, ".mp3",
         hint="Save audio as a separate file"),
    Tool("remove_bg","Clean up background",  "cleanup", ("static", "anim"), _remove_bg, ".png",
         hint="Works best on flat colors"),
    Tool("crop",     "Trim off the edges",   "cleanup", ("anim", "static"), _crop, ".mp4",
         hint="Remove borders, logos, watermarks",
         options=(("5", "Trim 5% off each side", ""),
                  ("10", "Trim 10% off each side", ""),
                  ("20", "Trim 20% off each side", ""))),

    # 🎨 Enhance
    Tool("compress", "Shrink the file",      "enhance", ("anim",), _compress, ".mp4",
         hint="Smaller file, same content",
         options=(("high", "High quality", ""), ("medium", "Balanced", ""),
                  ("low", "Smallest file", ""))),
    Tool("watermark","Add text on top",      "enhance", ("anim",), _watermark, ".mp4",
         hint="Stamp your text on the video",
         needs_text=True),
    Tool("frames",   "Save still frames",    "enhance", ("anim",), _frames_zip, ".zip",
         hint="Extract pictures from the video",
         options=(("6", "6 frames", ""), ("12", "12 frames", ""), ("24", "24 frames", ""))),
]}

CATEGORIES = {
    "convert":   "📦 Convert format",
    "cut":       "✂️ Cut & trim",
    "transform": "🎛 Transform",
    "cleanup":   "🧼 Clean up",
    "enhance":   "🎨 Enhance",
}

CATEGORY_HINT = {
    "convert":   "Change the file type",
    "cut":       "Remove or keep parts of the video",
    "transform": "Rotate, speed, resize",
    "cleanup":   "Strip audio, backgrounds, borders",
    "enhance":   "Compress, watermark, extract frames",
}


def tools_for(kind: str, category: str) -> list[Tool]:
    return [t for t in TOOLS.values() if t.category == category and kind in t.kinds]


def tools_for_kinds(kinds: set[str], category: str) -> list[Tool]:
    return [t for t in TOOLS.values()
            if t.category == category and all(k in kinds for k in t.kinds)]


# ──────────────────────────────────────────────────────────────────────────────
#  Session state keys
# ──────────────────────────────────────────────────────────────────────────────

K_JOB        = "job"
K_BG         = "bg"
K_PENDING    = "pending"
K_PENDING_T  = "pending_time"
K_COLLECT    = "collect"
K_BATCH      = "batch"


# ──────────────────────────────────────────────────────────────────────────────
#  Keyboards — clean labels, no emoji spam
# ──────────────────────────────────────────────────────────────────────────────

def menu_root(kind: str, jid: str, kinds_set: set[str] | None = None) -> InlineKeyboardMarkup:
    rows = []
    for c, title in CATEGORIES.items():
        avail = (tools_for_kinds(kinds_set, c) if kinds_set else tools_for(kind, c))
        if avail:
            rows.append([InlineKeyboardButton(title, callback_data=f"cat|{c}|{jid}")])
    return InlineKeyboardMarkup(rows)


def menu_category(kind: str, category: str, jid: str,
                  kinds_set: set[str] | None = None) -> InlineKeyboardMarkup:
    tools = (tools_for_kinds(kinds_set, category) if kinds_set
             else tools_for(kind, category))
    rows, row = [], []
    for t in tools:
        row.append(InlineKeyboardButton(t.label, callback_data=f"t|{t.key}|{jid}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Back", callback_data=f"back|{jid}")])
    return InlineKeyboardMarkup(rows)


def menu_options(tool: Tool, jid: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for value, label, _ in tool.options:
        row.append(InlineKeyboardButton(label, callback_data=f"opt|{tool.key}|{jid}|{value}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Back",
                 callback_data=f"cat|{tool.category}|{jid}")])
    return InlineKeyboardMarkup(rows)


def menu_bg() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, (label, _v) in BG_CHOICES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"bg|{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def menu_collect(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Build GIF ({count})", callback_data="coll|build"),
        InlineKeyboardButton("Cancel", callback_data="coll|cancel"),
    ]])


def menu_batch(count: int) -> InlineKeyboardMarkup:
    rows = []
    if count >= 1:
        rows.append([InlineKeyboardButton(
            f"Choose a tool for {count} file{'s' if count != 1 else ''}",
            callback_data="batch|start")])
    rows.append([InlineKeyboardButton("Clear batch", callback_data="batch|clear")])
    return InlineKeyboardMarkup(rows)


def menu_group_share() -> InlineKeyboardMarkup:
    """Share-the-bot CTA used in /start and /group."""
    url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add me to a group", url=url)],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Source detection
# ──────────────────────────────────────────────────────────────────────────────

def extract_source(msg) -> tuple[str, str] | None:
    if msg.sticker:
        s = msg.sticker
        if s.is_animated:
            return ("tgs", s.file_id)
        if s.is_video:
            return ("anim", s.file_id)
        return ("static", s.file_id)
    if msg.animation:
        return ("anim", msg.animation.file_id)
    if msg.video:
        return ("anim", msg.video.file_id)
    if msg.video_note:
        return ("anim", msg.video_note.file_id)
    if msg.photo:
        return ("static", msg.photo[-1].file_id)
    doc = msg.document
    if doc:
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        if mime == "image/gif" or name.endswith(".gif"):
            return ("anim", doc.file_id)
        if mime.startswith("video/") or name.endswith((".mp4", ".webm", ".mkv", ".mov", ".avi")):
            return ("anim", doc.file_id)
        if mime.startswith("audio/") or name.endswith((".mp3", ".m4a", ".wav", ".ogg")):
            return ("anim", doc.file_id)
        if mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            return ("static", doc.file_id)
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  Help & welcome
# ──────────────────────────────────────────────────────────────────────────────

def welcome_text(user) -> str:
    name = first_name_of(user)
    return (
        f"Hey <b>{name}</b>, welcome to <b>MediaForge</b>.\n"
        "\n"
        "I turn any photo, GIF, video or sticker into almost any other "
        "format — usually in a second or two.\n"
        "\n"
        "<b>How to use me</b>\n"
        "1. Send a photo, GIF, video, video note, sticker or file\n"
        "2. Tap a category, then tap what you want\n"
        "3. Get the result back — instantly\n"
        "\n"
        "<b>What I can do</b>\n"
        "📦 <b>Convert format</b> — GIF, MP4, PNG, JPG, WebP, MP3, stickers\n"
        "✂️ <b>Cut &amp; trim</b> — keep the start, or delete a middle section\n"
        "🎛 <b>Transform</b> — rotate, mirror, speed up, slow down, resize\n"
        "🧼 <b>Clean up</b> — remove audio, clean background, trim edges\n"
        "🎨 <b>Enhance</b> — compress, add text, pull still frames\n"
        "\n"
        "<b>Useful shortcuts</b>\n"
        "/gif — turn a stack of photos into a slideshow GIF\n"
        "/batch — convert many files at once\n"
        "/bg — pick a background colour\n"
        "/cancel — stop what you're doing\n"
        "\n"
        "<b>Better in groups</b>\n"
        "Add me to a group and everyone can convert media without leaving "
        "the chat. Tap the button below."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    await update.effective_message.reply_text(
        welcome_text(update.effective_user),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=menu_group_share(),
    )


async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    await update.effective_message.reply_text(
        "<b>Bring MediaForge to your group</b>\n"
        "\n"
        "In a group, anyone can send a video, GIF, sticker or photo and "
        "I'll convert it right there — no need to leave the chat or forward "
        "files around.\n"
        "\n"
        "<b>How to add me</b>\n"
        "1. Tap the button below\n"
        "2. Pick the group\n"
        "3. Confirm the invite\n"
        "\n"
        "<b>Tip for admins</b>\n"
        "After adding, remove and re-add me once so I can read all media "
        "in the group — otherwise I'll only see messages that mention me.",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_group_share(),
    )


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    lines = ["<b>Everything I can do</b>"]
    for cat, title in CATEGORIES.items():
        lines.append(f"\n<b>{title}</b>")
        lines.append(f"<i>{CATEGORY_HINT.get(cat, '')}</i>")
        for t in TOOLS.values():
            if t.category == cat:
                lines.append(f"• {t.label} — {t.hint}" if t.hint else f"• {t.label}")
    lines.append("\nSend any media to get started.")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_bg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    current = context.user_data.get(K_BG, "white")
    await update.effective_message.reply_text(
        "<b>Background colour</b>\n"
        f"Currently set to <b>{current}</b>.\n"
        "\n"
        "Used when a transparent image or sticker has to be flattened "
        "into an MP4 or JPG.",
        parse_mode=ParseMode.HTML, reply_markup=menu_bg(),
    )


async def cmd_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    context.user_data[K_COLLECT] = []
    await update.effective_message.reply_text(
        "<b>Slideshow GIF builder</b>\n"
        "\n"
        "Send me photos, one at a time, and I'll stitch them into a GIF. "
        "Tap Build when you're done.",
        parse_mode=ParseMode.HTML, reply_markup=menu_collect(0),
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    await _build_collection(update.effective_message, context,
                            update.effective_user)


async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    context.user_data[K_BATCH] = []
    await update.effective_message.reply_text(
        "<b>Batch mode</b>\n"
        "\n"
        f"Send me up to {MAX_BATCH_FILES} files. When you're done, "
        "I'll ask which tool to apply to all of them at once.",
        parse_mode=ParseMode.HTML, reply_markup=menu_batch(0),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    if not is_admin(update):
        await update.effective_message.reply_text("This command is admin-only.")
        return
    uptime = time.time() - STATS["start_time"]
    try:
        total, used, free = shutil.disk_usage(WORK)
    except Exception:
        total = used = free = 0
    work = _work_size()
    avg = (STATS["seconds_spent"] / STATS["jobs_done"]) if STATS["jobs_done"] else 0

    await update.effective_message.reply_text(
        "<b>Bot stats</b>\n"
        "\n"
        f"Uptime — {_fmt_duration(uptime)}\n"
        f"Users — {len(USERS)}\n"
        f"Jobs done — {STATS['jobs_done']}\n"
        f"Failures — {STATS['jobs_failed']}\n"
        f"Bytes sent — {_fmt_size(STATS['bytes_out'])}\n"
        f"Avg per job — {avg:.1f}s\n"
        "\n"
        f"WORK dir — {_fmt_size(work)}\n"
        f"Disk free — {_fmt_size(free)} / {_fmt_size(total)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    if not is_admin(update):
        await update.effective_message.reply_text("This command is admin-only.")
        return
    if not USERS:
        await update.effective_message.reply_text("No users tracked yet.")
        return
    top = sorted(USERS.values(), key=lambda u: u.get("jobs", 0), reverse=True)[:15]
    lines = [
        "<b>Top users</b>",
        f"<i>{len(USERS)} users total</i>",
        "",
    ]
    for i, u in enumerate(top, 1):
        name = (f"@{u['username']}" if u.get("username")
                else (u.get("first_name") or f"id{u['id']}"))
        lines.append(
            f"{i}. <b>{name}</b> — {u.get('jobs', 0)} jobs · "
            f"{_fmt_size(u.get('bytes_out', 0))} · "
            f"{_fmt_duration(time.time() - u.get('last_seen', time.time()))} ago"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_cmd(update.effective_user)
    for k in (K_JOB, K_PENDING, K_PENDING_T, K_COLLECT, K_BATCH):
        context.user_data.pop(k, None)
    await update.effective_message.reply_text("Cancelled. Send a new file anytime.")


# ──────────────────────────────────────────────────────────────────────────────
#  Media handler
# ──────────────────────────────────────────────────────────────────────────────

async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    track_seen(update.effective_user)

    if K_BATCH in context.user_data:
        parsed = extract_source(msg)
        if parsed and parsed[0] == "tgs":
            await msg.reply_text(
                "Animated <code>.tgs</code> stickers can't be converted. "
                "Try a video sticker instead.",
                parse_mode=ParseMode.HTML)
            return
        if parsed:
            _, fid = parsed
            batch = context.user_data[K_BATCH]
            if len(batch) >= MAX_BATCH_FILES:
                await msg.reply_text(
                    f"Batch is full ({MAX_BATCH_FILES} files). "
                    "Tap a button to continue or clear.")
                return
            batch.append(fid)
            n = len(batch)
            await msg.reply_text(
                f"Added. <b>{n}</b> file{'s' if n != 1 else ''} in batch.",
                parse_mode=ParseMode.HTML,
                reply_markup=menu_batch(n),
            )
            return

    if K_COLLECT in context.user_data and msg.photo:
        fid = msg.photo[-1].file_id
        context.user_data[K_COLLECT].append(fid)
        n = len(context.user_data[K_COLLECT])
        try:
            await msg.reply_text(f"Frame {n} added.",
                                 reply_markup=menu_collect(n))
        except BadRequest:
            pass
        return

    parsed = extract_source(msg)
    if parsed is None:
        return
    kind, file_id = parsed

    if kind == "tgs":
        await msg.reply_text(
            "Animated <code>.tgs</code> stickers are vector files — "
            "I can't convert those. A video sticker works great though.",
            parse_mode=ParseMode.HTML,
        )
        return

    jid = uuid.uuid4().hex[:12]
    context.user_data[K_JOB] = {
        "id": jid,
        "file_id": file_id,
        "kind": kind,
        "uid": update.effective_user.id if update.effective_user else 0,
    }
    context.user_data.pop(K_PENDING, None)
    context.user_data.pop(K_PENDING_T, None)
    await msg.reply_text(
        "<b>Got it.</b> What would you like to do?\n"
        "<i>Pick a category below.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_root(kind, jid),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Callback router
# ──────────────────────────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    if update.effective_user:
        track_seen(update.effective_user)
    data = q.data or ""

    if data.startswith("bg|"):
        key = data.split("|", 1)[1]
        if key in BG_CHOICES:
            context.user_data[K_BG] = BG_CHOICES[key][1]
            await q.answer(f"Background: {BG_CHOICES[key][0]}")
            await q.edit_message_text(
                f"Background set to <b>{BG_CHOICES[key][0]}</b>.",
                parse_mode=ParseMode.HTML)
        else:
            await q.answer()
        return

    if data == "batch|clear":
        context.user_data.pop(K_BATCH, None)
        await q.answer("Cleared.")
        await q.edit_message_text("Batch cleared.")
        return
    if data == "batch|start":
        await q.answer()
        await q.edit_message_text(
            "<b>Pick a tool for every file</b>\n"
            "<i>Only tools that work on all your sources are shown.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_root("", "batch", kinds_set={"anim", "static"}),
        )
        return

    if data == "coll|cancel":
        context.user_data.pop(K_COLLECT, None)
        await q.answer("Cancelled.")
        await q.edit_message_text("GIF builder closed.")
        return
    if data == "coll|build":
        await q.answer()
        await _build_collection(q.message, context, update.effective_user)
        return

    if data.startswith("cat|"):
        _, cat, jid = data.split("|", 2)
        if jid == "batch":
            await q.edit_message_reply_markup(
                menu_category("", cat, "batch", kinds_set={"anim", "static"}))
            await q.answer()
            return
        job = context.user_data.get(K_JOB)
        if not job or job["id"] != jid:
            await q.answer("That expired — send the media again.", show_alert=True)
            return
        await q.edit_message_reply_markup(menu_category(job["kind"], cat, jid))
        await q.answer()
        return

    if data.startswith("back|"):
        _, jid = data.split("|", 1)
        if jid == "batch":
            await q.edit_message_reply_markup(
                menu_root("", "batch", kinds_set={"anim", "static"}))
            await q.answer()
            return
        job = context.user_data.get(K_JOB)
        if not job or job["id"] != jid:
            await q.answer("That expired.", show_alert=True)
            return
        await q.edit_message_reply_markup(menu_root(job["kind"], jid))
        await q.answer()
        return

    if data.startswith("t|"):
        _, key, jid = data.split("|", 2)
        tool = TOOLS.get(key)
        if not tool:
            await q.answer("Unknown tool.")
            return

        if jid == "batch":
            if tool.needs_text:
                context.user_data[K_PENDING] = {"tool": key, "jid": "batch"}
                await q.edit_message_text("Send the text you'd like to stamp.")
                await q.answer()
                return
            if tool.needs_time:
                await q.answer("Time tools aren't available in batch mode.",
                               show_alert=True)
                return
            if tool.options:
                await q.edit_message_reply_markup(menu_options(tool, "batch"))
                await q.answer()
                return
            await q.answer()
            await _run_batch(q.message, context, key, {},
                             update.effective_user)
            return

        job = context.user_data.get(K_JOB)
        if not job or job["id"] != jid:
            await q.answer("That expired.", show_alert=True)
            return

        if tool.needs_text:
            context.user_data[K_PENDING] = {"tool": key, "jid": jid}
            await q.edit_message_text("Send the text you'd like to stamp.")
            await q.answer()
            return
        if tool.needs_time:
            context.user_data[K_PENDING_T] = {"tool": key, "jid": jid, "step": "start"}
            await q.edit_message_text(
                "<b>Delete a section</b>\n"
                "\n"
                "Send the <b>start</b> time of the part to remove.\n"
                "<i>Examples:</i> <code>15</code> · <code>0:15</code> · <code>1:02:30</code>",
                parse_mode=ParseMode.HTML)
            await q.answer()
            return
        if tool.options:
            await q.edit_message_reply_markup(menu_options(tool, jid))
            await q.answer()
            return
        await q.answer()
        await _run_job(q.message, context, key, {})
        return

    if data.startswith("opt|"):
        _, key, jid, value = data.split("|", 3)
        tool = TOOLS.get(key)
        if not tool:
            await q.answer("Unknown tool.")
            return
        opts = _opts_from_value(tool, value)

        if jid == "batch":
            await q.answer()
            await _run_batch(q.message, context, key, opts,
                             update.effective_user)
            return

        job = context.user_data.get(K_JOB)
        if not job or job["id"] != jid:
            await q.answer("That expired.", show_alert=True)
            return
        await q.answer()
        await _run_job(q.message, context, key, opts)
        return

    await q.answer()


def _opts_from_value(tool: Tool, value: str) -> dict:
    if tool.key == "speed":
        return {"factor": float(value)}
    if tool.key == "rotate":
        return {"degrees": int(value)}
    if tool.key == "flip":
        return {"direction": value}
    if tool.key == "compress":
        return {"level": value}
    if tool.key == "resize":
        return {"size": int(value)}
    if tool.key == "frames":
        return {"frames": int(value)}
    if tool.key == "trim":
        return {"start": 0, "duration": float(value)}
    if tool.key == "crop":
        return {"pct": float(value)}
    return {}


# ──────────────────────────────────────────────────────────────────────────────
#  Text input handler
# ──────────────────────────────────────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    if update.effective_user:
        track_seen(update.effective_user)
    text = (msg.text or "").strip()

    ptime = context.user_data.get(K_PENDING_T)
    if ptime:
        try:
            seconds = parse_time(text)
        except ValueError:
            await msg.reply_text(
                "I couldn't read that time.\n"
                "Try <code>15</code>, <code>0:15</code> or <code>1:02:30</code>.",
                parse_mode=ParseMode.HTML)
            return

        if ptime["step"] == "start":
            ptime["cut_start"] = seconds
            ptime["step"] = "end"
            await msg.reply_text(
                f"Start = <b>{_fmt_duration(seconds)}</b>\n"
                "\n"
                "Now send the <b>end</b> time of the part to remove.",
                parse_mode=ParseMode.HTML)
            return

        if seconds <= ptime["cut_start"]:
            await msg.reply_text(
                "End must be after start. Send the end time again.")
            return

        opts = {"cut_start": ptime["cut_start"], "cut_end": seconds}
        tool_key = ptime["tool"]
        context.user_data.pop(K_PENDING_T, None)

        if ptime["jid"] == "batch":
            await _run_batch(msg, context, tool_key, opts,
                             update.effective_user)
            return
        job = context.user_data.get(K_JOB)
        if not job or job["id"] != ptime["jid"]:
            await msg.reply_text("That expired — send the media again.")
            return
        await _run_job(msg, context, tool_key, opts)
        return

    pending = context.user_data.get(K_PENDING)
    if not pending:
        return
    if not text:
        await msg.reply_text("Send some text please.")
        return
    context.user_data.pop(K_PENDING, None)

    if pending["jid"] == "batch":
        await _run_batch(msg, context, pending["tool"], {"text": text},
                         update.effective_user)
        return

    job = context.user_data.get(K_JOB)
    if not job or job["id"] != pending["jid"]:
        return
    await _run_job(msg, context, pending["tool"], {"text": text})


# ──────────────────────────────────────────────────────────────────────────────
#  Job runner (single)
# ──────────────────────────────────────────────────────────────────────────────

async def _run_job(status_msg, context: ContextTypes.DEFAULT_TYPE,
                   tool_key: str, extra_opts: dict) -> None:
    job = context.user_data.get(K_JOB)
    if not job:
        await status_msg.reply_text("That expired — send the media again.")
        return
    tool = TOOLS.get(tool_key)
    if not tool:
        await status_msg.reply_text("Unknown tool.")
        return

    src: Path | None = None
    out: Path | None = None
    t0 = time.monotonic()
    try:
        chat_id = status_msg.chat_id

        await _safe_edit(status_msg, f"Working on: <b>{tool.label}</b>")
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        tg_file = await context.bot.get_file(job["file_id"])
        if tg_file.file_size and tg_file.file_size > MAX_INPUT_BYTES:
            raise ConversionError("That file is over Telegram's 20 MB bot limit.")

        suffix = Path(tg_file.file_path or "").suffix or ".bin"
        if len(suffix) > 6:
            suffix = ".bin"
        src = WORK / f"{job['id']}_src{suffix}"
        await tg_file.download_to_drive(custom_path=str(src))

        opts = {**extra_opts}
        if K_BG in context.user_data:
            opts["bg"] = context.user_data[K_BG]
        if tool.key == "gif":
            opts["max_seconds"] = 10

        await _safe_edit(status_msg, f"Converting: <b>{tool.label}</b>")
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

        out = WORK / f"{job['id']}_{tool.key}{tool.ext}"
        await tool.handler(src, out, opts)

        kb = out.stat().st_size / 1024
        elapsed = time.monotonic() - t0
        info = await _media_info(out)

        await _safe_edit(status_msg, "Sending your file...")
        await deliver(status_msg, tool.key, out, tool.label, info, kb, elapsed)

        STATS["jobs_done"] += 1
        STATS["bytes_out"] += out.stat().st_size
        STATS["seconds_spent"] += elapsed
        track_job(job.get("uid"), tool_key, out.stat().st_size, ok=True)

        await _safe_edit(
            status_msg,
            f"Done — <b>{tool.label}</b>\n"
            f"<i>{info + ' · ' if info else ''}{kb:.0f} KB · {elapsed:.1f}s</i>",
        )

    except ConversionError as exc:
        STATS["jobs_failed"] += 1
        track_job(job.get("uid"), tool_key, 0, ok=False)
        await _safe_edit(status_msg, f"Couldn't finish: {exc}")
    except BadRequest as exc:
        STATS["jobs_failed"] += 1
        track_job(job.get("uid"), tool_key, 0, ok=False)
        await _safe_edit(status_msg, f"Telegram said: {exc.message}")
    except Exception:
        STATS["jobs_failed"] += 1
        track_job(job.get("uid"), tool_key, 0, ok=False)
        log.exception("job failed")
        await _safe_edit(status_msg, "Something went wrong on my end.")
    finally:
        for p in (src, out):
            if p:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


# ──────────────────────────────────────────────────────────────────────────────
#  Job runner (batch)
# ──────────────────────────────────────────────────────────────────────────────

async def _run_batch(status_msg, context: ContextTypes.DEFAULT_TYPE,
                     tool_key: str, extra_opts: dict, user) -> None:
    file_ids = context.user_data.get(K_BATCH) or []
    if not file_ids:
        await status_msg.reply_text("Batch is empty. Use /batch to start.")
        return
    tool = TOOLS.get(tool_key)
    if not tool:
        await status_msg.reply_text("Unknown tool.")
        return

    total = len(file_ids)
    ok = 0
    failed = 0
    total_bytes = 0
    t_all = time.monotonic()
    uid = user.id if user else 0

    status = await status_msg.reply_text(
        f"<b>Batch start</b> — {tool.label}\n"
        f"<i>0 / {total}</i>", parse_mode=ParseMode.HTML)

    for i, fid in enumerate(file_ids, 1):
        src: Path | None = None
        out: Path | None = None
        jid = uuid.uuid4().hex[:10]
        try:
            await _safe_edit(status,
                f"<b>Batch {i} / {total}</b> — {tool.label}\n"
                "<i>Downloading...</i>")

            tg_file = await context.bot.get_file(fid)
            if tg_file.file_size and tg_file.file_size > MAX_INPUT_BYTES:
                raise ConversionError("Over 20 MB.")
            suffix = Path(tg_file.file_path or "").suffix or ".bin"
            if len(suffix) > 6:
                suffix = ".bin"
            src = WORK / f"{jid}_src{suffix}"
            await tg_file.download_to_drive(custom_path=str(src))

            await _safe_edit(status,
                f"<b>Batch {i} / {total}</b> — {tool.label}\n"
                "<i>Converting...</i>")

            opts = {**extra_opts}
            if K_BG in context.user_data:
                opts["bg"] = context.user_data[K_BG]
            if tool.key == "gif":
                opts["max_seconds"] = 10

            out = WORK / f"{jid}_{tool.key}{tool.ext}"
            await tool.handler(src, out, opts)

            kb = out.stat().st_size / 1024
            info = await _media_info(out)
            await deliver(status_msg, tool.key, out, tool.label, info, kb, 0)
            ok += 1
            total_bytes += out.stat().st_size

        except ConversionError as exc:
            failed += 1
            await _safe(status_msg.reply_text, f"File {i}: {exc}")
        except Exception:
            failed += 1
            log.exception("batch item failed")
        finally:
            for p in (src, out):
                if p:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass

    elapsed = time.monotonic() - t_all
    STATS["jobs_done"] += ok
    STATS["jobs_failed"] += failed
    STATS["bytes_out"] += total_bytes
    STATS["seconds_spent"] += elapsed
    if uid:
        for _ in range(ok):
            track_job(uid, tool_key, 0, ok=True)
        for _ in range(failed):
            track_job(uid, tool_key, 0, ok=False)
        u = USERS.get(str(uid))
        if u:
            u["bytes_out"] = u.get("bytes_out", 0) + total_bytes
            save_users()

    context.user_data.pop(K_BATCH, None)

    await _safe_edit(
        status,
        f"<b>Batch complete</b>\n"
        f"<i>{ok} / {total} done · {_fmt_size(total_bytes)} · {elapsed:.1f}s</i>",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Utilities
# ──────────────────────────────────────────────────────────────────────────────

async def _safe(fn, *a, **kw):
    try:
        return await fn(*a, **kw)
    except Exception:
        pass


async def _safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            pass
    except Exception:
        pass


async def deliver(status_msg, tool_key: str, path: Path, label: str,
                  info: str, kb: float, elapsed: float) -> None:
    parts = [f"Done — {label}"]
    if info:
        parts.append(info)
    parts.append(f"{kb:.0f} KB")
    if elapsed:
        parts.append(f"{elapsed:.1f}s")
    caption = " · ".join(parts)

    with open(path, "rb") as fh:
        upload = InputFile(fh, filename=path.name)
        if tool_key in ("asticker", "ssticker"):
            await status_msg.reply_sticker(sticker=upload)
        elif tool_key == "gif":
            await status_msg.reply_animation(
                animation=upload, filename=path.name, caption=caption)
        elif tool_key == "mp4":
            await status_msg.reply_video(
                video=upload, filename=path.name,
                supports_streaming=True, caption=caption)
        elif tool_key in ("mp3", "detach"):
            await status_msg.reply_audio(
                audio=upload, filename=path.name, caption=caption)
        else:
            await status_msg.reply_document(
                document=upload, filename=path.name, caption=caption)


# ──────────────────────────────────────────────────────────────────────────────
#  Images → GIF
# ──────────────────────────────────────────────────────────────────────────────

async def _build_collection(status_msg, context: ContextTypes.DEFAULT_TYPE,
                            user) -> None:
    file_ids = context.user_data.get(K_COLLECT) or []
    if len(file_ids) < 2:
        await status_msg.reply_text(
            "Send me at least 2 photos first (or /gif to start over).")
        return
    tmp_dir = WORK / f"coll_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out: Path | None = None
    t0 = time.monotonic()
    uid = user.id if user else 0
    try:
        await status_msg.reply_text(f"Building your GIF from {len(file_ids)} images...")

        async def _fetch(i: int, fid: str) -> Path:
            tgf = await context.bot.get_file(fid)
            p = tmp_dir / f"{i:04d}.jpg"
            await tgf.download_to_drive(custom_path=str(p))
            return p

        paths = await asyncio.gather(*[_fetch(i, fid) for i, fid in enumerate(file_ids)])

        listfile = tmp_dir / "list.txt"
        with open(listfile, "w") as fh:
            for p in paths:
                fh.write(f"file '{p.as_posix()}'\n")
                fh.write("duration 0.6\n")

        out = WORK / f"coll_{uuid.uuid4().hex}.gif"
        await _ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-filter_complex", GIF_GRAPH, "-loop", "0", str(out),
        ])

        kb = out.stat().st_size / 1024
        info = await _media_info(out)
        elapsed = time.monotonic() - t0
        caption = f"Done — GIF · {len(paths)} frames"
        if info:
            caption += f" · {info}"
        caption += f" · {kb:.0f} KB · {elapsed:.1f}s"

        with open(out, "rb") as fh:
            await status_msg.reply_animation(
                animation=InputFile(fh, filename=out.name),
                filename=out.name,
                caption=caption,
            )
        context.user_data.pop(K_COLLECT, None)

        STATS["jobs_done"] += 1
        STATS["bytes_out"] += out.stat().st_size
        STATS["seconds_spent"] += elapsed
        track_job(uid, "gif_builder", out.stat().st_size, ok=True)

        await _safe(status_msg.reply_text,
                    f"GIF ready — {len(paths)} frames, "
                    f"{kb:.0f} KB, {elapsed:.1f}s.",
                    parse_mode=ParseMode.HTML)
    except ConversionError as exc:
        STATS["jobs_failed"] += 1
        track_job(uid, "gif_builder", 0, ok=False)
        await status_msg.reply_text(f"Couldn't finish: {exc}")
    except Exception:
        STATS["jobs_failed"] += 1
        track_job(uid, "gif_builder", 0, ok=False)
        log.exception("gif build failed")
        await status_msg.reply_text(
            "Couldn't build that GIF. Try fewer or smaller images.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if out:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
#  Health + API server
# ──────────────────────────────────────────────────────────────────────────────

async def _health_server() -> None:
    port = int(os.environ.get("PORT", 10000))

    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    def _check(request) -> bool:
        return request.query.get("token") == ADMIN_TOKEN

    async def ok(_request):
        return _cors(web.Response(text="MediaForge is alive."))

    async def healthz(_request):
        return _cors(web.json_response({
            "status": "ok",
            "uptime_seconds": round(time.time() - STATS["start_time"], 1),
            "jobs_done": STATS["jobs_done"],
            "jobs_failed": STATS["jobs_failed"],
        }))

    async def api_stats(request):
        if not _check(request):
            return _cors(web.json_response({"error": "unauthorized"}, status=401))
        return _cors(web.json_response(aggregate_stats()))

    async def api_users(request):
        if not _check(request):
            return _cors(web.json_response({"error": "unauthorized"}, status=401))
        users = sorted(USERS.values(),
                       key=lambda u: u.get("last_seen", 0), reverse=True)
        return _cors(web.json_response({"users": users, "count": len(users)}))

    async def opts(_request):
        return _cors(web.Response(text=""))

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/users", api_users)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", opts)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health + API server on :%d", port)


# ──────────────────────────────────────────────────────────────────────────────
#  Error hook + bootstrap
# ──────────────────────────────────────────────────────────────────────────────

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update %s caused error", update, exc_info=context.error)


MEDIA_FILTER = (
    filters.PHOTO | filters.VIDEO | filters.ANIMATION
    | filters.Sticker.ALL | filters.VIDEO_NOTE | filters.Document.ALL
)


def purge_workdir() -> None:
    for entry in WORK.iterdir():
        try:
            if entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome & how to use me"),
        BotCommand("help", "Same as /start"),
        BotCommand("tools", "See every available tool"),
        BotCommand("group", "Add me to a group"),
        BotCommand("bg", "Pick a background colour"),
        BotCommand("gif", "Photos → slideshow GIF"),
        BotCommand("done", "Build the collected GIF"),
        BotCommand("batch", "Convert many files at once"),
        BotCommand("cancel", "Cancel current operation"),
    ])


async def _run_all() -> None:
    await _health_server()

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("group", cmd_group))
    app.add_handler(CommandHandler("tools", cmd_tools))
    app.add_handler(CommandHandler("bg", cmd_bg))
    app.add_handler(CommandHandler("gif", cmd_gif))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("batch", cmd_batch))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(MEDIA_FILTER, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("MediaForge running.")
    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()


def main() -> None:
    load_users()
    check_binaries()
    purge_workdir()
    try:
        asyncio.run(_run_all())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
