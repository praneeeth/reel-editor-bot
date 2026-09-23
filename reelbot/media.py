"""ffprobe helpers, source inventory and fitting files under Telegram's upload limit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus"}
ASSETS_DIR = "assets"
NOTES_FILE = "notes.json"


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,avg_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0.0),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": v.get("avg_frame_rate"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": bool(v),
    }


def inventory(job_dir: Path) -> list[dict]:
    """ffprobe every source clip in the job folder (step 1 of video-use's process)."""
    items = []
    for p in sorted(job_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            info = probe(p)
            info.update(name=p.stem, path=str(p.resolve()),
                        orientation="landscape" if info["width"] > info["height"] else "portrait",
                        note=load_notes(job_dir).get(p.name, ""))
            items.append(info)
    return items


def media_kind(name: str, mime: str | None) -> str | None:
    """'clip', 'image', 'music' or None, from a file name and/or MIME type."""
    ext, mime = Path(name or "").suffix.lower(), (mime or "").lower()
    if ext in VIDEO_EXTS or mime.startswith("video/"):
        return "clip"
    if ext in IMAGE_EXTS or mime.startswith("image/"):
        return "image"
    if ext in AUDIO_EXTS or mime.startswith("audio/"):
        return "music"
    return None


def load_notes(job_dir: Path) -> dict[str, str]:
    """Captions the user attached to media, keyed by file name."""
    try:
        return json.loads((job_dir / ASSETS_DIR / NOTES_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def add_note(job_dir: Path, file: Path, note: str) -> None:
    notes = load_notes(job_dir)
    notes[file.name] = note
    (job_dir / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    (job_dir / ASSETS_DIR / NOTES_FILE).write_text(json.dumps(notes, indent=2))


def asset_inventory(job_dir: Path) -> dict[str, list[dict]]:
    """Images (logos, stickers, photos) and music the user sent, with their notes."""
    from PIL import Image

    notes = load_notes(job_dir)
    out: dict[str, list[dict]] = {"images": [], "music": []}
    adir = job_dir / ASSETS_DIR
    if not adir.is_dir():
        return out
    for p in sorted(adir.iterdir()):
        ext = p.suffix.lower()
        entry = {"file": str(p.resolve()), "note": notes.get(p.name, "")}
        if ext in IMAGE_EXTS:
            try:
                with Image.open(p) as img:
                    entry.update(width=img.width, height=img.height,
                                 transparent=img.mode in ("RGBA", "LA", "P"))
            except OSError:
                continue
            out["images"].append(entry)
        elif ext in AUDIO_EXTS:
            try:
                entry["duration"] = round(probe(p)["duration"], 2)
            except (subprocess.CalledProcessError, ValueError):
                continue
            out["music"].append(entry)
    return out


def fit_to_size(src: Path, dst: Path, max_bytes: int, *, audio_kbps: int = 128) -> Path:
    """Return ``src`` if it already fits, else re-encode to ``dst`` under ``max_bytes``.

    Two-pass-free ABR with a VBV cap; retried with a lower target if the first
    attempt still overshoots. Resolution is dropped to 720x1280 only when the
    bitrate would be too low for 1080p to look good.
    """
    if src.stat().st_size <= max_bytes:
        return src
    info = probe(src)
    duration = max(info["duration"], 0.1)
    budget = 0.92
    for _ in range(4):
        total_kbps = max_bytes * 8 * budget / duration / 1000
        video_kbps = int(total_kbps - audio_kbps)
        if video_kbps < 150:
            raise ValueError(f"cannot fit {duration:.0f}s of video into {max_bytes / 1e6:.0f} MB")
        vf = ["-vf", "scale=720:1280:flags=lanczos"] if video_kbps < 2500 and info["height"] > 1280 else []
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *vf,
             "-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k",
             "-maxrate", f"{int(video_kbps * 1.3)}k", "-bufsize", f"{video_kbps * 2}k",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
             "-movflags", "+faststart", str(dst)],
            check=True,
        )
        if dst.stat().st_size <= max_bytes:
            return dst
        budget *= 0.85
    raise ValueError(f"could not fit {src.name} under {max_bytes / 1e6:.0f} MB")
