"""Creative features: transitions, speed/zoom, b-roll, images, text, blur, music."""

import json
import subprocess

import numpy as np
import pytest
from PIL import Image

from conftest import make_clip, needs_media
from reelbot.edl import total_duration, validate_edl
from reelbot.media import probe
from reelbot.render import render_edl

pytestmark = needs_media
W, H = 720, 1280  # preview size keeps the test fast


def rgb_frame(video, t):
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3).astype(int)


def rms_db(video, t, dur=0.15):
    out = subprocess.run(
        ["ffmpeg", "-ss", f"{t:.3f}", "-t", f"{dur}", "-i", str(video), "-af",
         "astats=metadata=0:reset=0", "-vn", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    vals = [float(line.split(":")[-1]) for line in out.splitlines()
            if "RMS level dB" in line and "inf" not in line]
    return max(vals) if vals else -120.0


@pytest.fixture(scope="module")
def job(tmp_path_factory):
    d = tmp_path_factory.mktemp("features")
    make_clip(d, "--background", "0x203040", "--size", "1920x1080")
    # silent b-roll clip (colourful test pattern, no transcript)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(d / "clip02.mp4")], check=True)
    assets = d / "assets"
    assets.mkdir()
    Image.new("RGB", (200, 200), (255, 0, 0)).save(assets / "image01.png")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=4", str(assets / "music01.mp3")], check=True)
    return d


def features_edl(d):
    clip, broll = str(d / "clip01.mp4"), str(d / "clip02.mp4")
    return {
        "version": 1,
        "sources": {"clip01": clip, "clip02": broll},
        "ranges": [
            {"source": "clip01", "start": 0.45, "end": 1.45, "zoom": 1.3},
            {"source": "clip01", "start": 1.99, "end": 3.78, "speed": 1.25},
            {"source": "clip01", "start": 5.05, "end": 7.2, "zoom": 1.0, "zoom_to": 1.2},
            {"source": "clip01", "start": 7.4, "end": 9.15,
             "transition_in": {"type": "fade", "duration": 0.3}},
        ],
        "grade": "none",
        "overlays": [],
        "captions": {"color": "white"},
        "texts": [{"text": "QUICK TEST", "at": 0.0, "duration": 1.5, "position": "top",
                   "color": "yellow", "size": 110}],
        "broll": [{"source": "clip02", "start": 0.5, "end": 1.5, "at": 1.2, "mode": "full"},
                  {"source": "clip02", "start": 2.0, "end": 3.0, "at": 3.0, "mode": "pip"}],
        "images": [{"file": str(d / "assets" / "image01.png"), "at": 4.0, "duration": 1.2,
                    "x": 0.3, "y": 0.3, "width": 0.25}],
        "blur": [{"x": 0.1, "y": 0.4, "w": 0.3, "h": 0.1, "at": 0.0, "duration": 1.0}],
        "music": {"file": str(d / "assets" / "music01.mp3"), "volume": 0.3, "duck": True},
    }


@pytest.fixture(scope="module")
def rendered(job):
    edl = features_edl(job)
    (job / "edit" / "edl.json").write_text(json.dumps(edl))
    res = render_edl(job / "edit" / "edl.json", job / "edit" / "preview.mp4", preview=True,
                     allow_overlays=False)
    return res, edl


def test_features_edl_is_valid_in_lean_mode(job):
    assert validate_edl(features_edl(job), job / "edit", allow_overlays=False) == []


def test_duration_accounts_for_speed_and_transition(rendered):
    res, edl = rendered
    expected = 1.0 + 1.79 / 1.25 + 2.15 + 1.75 - 0.3
    assert abs(total_duration(edl) - expected) < 1e-6
    assert (res.width, res.height) == (W, H)
    assert abs(res.duration - expected) < 0.35
    assert probe(res.path)["has_audio"]


def test_text_title_is_drawn_at_the_top(rendered):
    res, _ = rendered
    f = rgb_frame(res.path, 0.7)
    band = f[int(H * 0.08):int(H * 0.20)]
    yellow = (band[..., 0] > 200) & (band[..., 1] > 180) & (band[..., 2] < 90)
    assert yellow.sum() > 300
    later = rgb_frame(res.path, 2.4)[int(H * 0.08):int(H * 0.20)]
    assert ((later[..., 0] > 200) & (later[..., 1] > 180) & (later[..., 2] < 90)).sum() < 50


def test_full_screen_broll_replaces_the_picture(rendered):
    res, _ = rendered
    main = rgb_frame(res.path, 0.9)
    cut = rgb_frame(res.path, 1.7)
    assert cut.std() > main.std() * 2  # colourful test pattern vs flat dark background


def test_pip_broll_sits_in_a_box_at_the_top(rendered):
    res, _ = rendered
    f = rgb_frame(res.path, 3.5)
    top, bottom = f[int(H * 0.13):int(H * 0.28)], f[int(H * 0.60):int(H * 0.75)]
    assert top.std() > bottom.std() * 2


def test_image_overlay_position(rendered):
    res, _ = rendered
    f = rgb_frame(res.path, 4.5)
    red = (f[..., 0] > 200) & (f[..., 1] < 60) & (f[..., 2] < 60)
    ys, xs = np.nonzero(red)
    assert len(ys) > 1000
    assert abs(xs.mean() / W - 0.3) < 0.03 and abs(ys.mean() / H - 0.3) < 0.03


def test_music_fills_silence(rendered, tmp_path):
    res, edl = rendered
    # the fade transition sits in silence; with music it must not be silent
    t_transition = total_duration(edl) - 1.75 + 0.1
    assert rms_db(res.path, t_transition) > -45
    edl_no_music = {**edl, "music": None}
    p = tmp_path / "edl.json"
    edit = res.path.parent
    (edit / "edl_nomusic.json").write_text(json.dumps(edl_no_music))
    quiet = render_edl(edit / "edl_nomusic.json", tmp_path / "nomusic.mp4", preview=True,
                       loudnorm=False)
    assert rms_db(quiet.path, t_transition) < rms_db(res.path, t_transition) - 10
