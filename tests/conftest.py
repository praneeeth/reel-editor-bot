import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HAVE_MEDIA_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "espeak-ng"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA_TOOLS, reason="needs ffmpeg + espeak-ng")


def make_clip(out_dir: Path, *extra: str) -> Path:
    subprocess.run([sys.executable, str(REPO / "scripts" / "make_test_clip.py"), str(out_dir),
                    "--short", *extra], check=True, capture_output=True)
    return out_dir / "clip01.mp4"


def words_of(transcript: Path) -> list[dict]:
    return [w for w in json.loads(transcript.read_text())["words"] if w["type"] == "word"]


def clean_edl(job_dir: Path, clip: Path, pad=(0.05, 0.08)) -> dict:
    """EDL keeping each sentence of the short fixture clip, dropping 'um'/'uh'."""
    words = words_of(job_dir / "edit" / "transcripts" / "clip01.json")
    fillers = {"um,", "uh,", "um", "uh"}
    ranges, cur = [], []
    for w in words:
        if w["text"].lower() in fillers:
            if cur:
                ranges.append(cur)
            cur = []
            continue
        cur.append(w)
        if w["text"][-1] in ".!?":
            ranges.append(cur)
            cur = []
    if cur:
        ranges.append(cur)
    return {
        "version": 1,
        "sources": {"clip01": str(clip)},
        "ranges": [{"source": "clip01", "start": round(r[0]["start"] - pad[0], 3),
                    "end": round(r[-1]["end"] + pad[1], 3)} for r in ranges],
        "grade": "none",
        "overlays": [],
    }


@pytest.fixture(scope="session")
def short_clip_dir(tmp_path_factory) -> Path:
    if not HAVE_MEDIA_TOOLS:
        pytest.skip("needs ffmpeg + espeak-ng")
    d = tmp_path_factory.mktemp("clipsrc")
    make_clip(d)
    return d


@pytest.fixture
def job_dir(tmp_path, short_clip_dir) -> Path:
    """A fresh job folder containing the short clip + its cached fixture transcript."""
    d = tmp_path / "job"
    shutil.copytree(short_clip_dir, d)
    return d
