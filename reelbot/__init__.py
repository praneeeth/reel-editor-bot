"""Telegram bot that edits clips into vertical Reels/Shorts using the video-use skill."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VIDEO_USE_DIR = REPO_ROOT / "vendor" / "video-use"
