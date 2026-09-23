"""End-to-end render: fixture clip -> EDL -> 9:16 reel with burned captions."""

import json
import subprocess

import numpy as np
import pytest

from conftest import clean_edl, make_clip, needs_media
from reelbot.media import fit_to_size, probe
from reelbot.render import SAFE_BOTTOM, SAFE_RIGHT, _parse_srt, render_edl

pytestmark = needs_media


def gray_frame(video, t, w, h):
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w)


@pytest.fixture(scope="module")
def dark_job(tmp_path_factory):
    """Short clip on a flat dark background so caption pixels are easy to find."""
    d = tmp_path_factory.mktemp("dark")
    clip = make_clip(d, "--background", "0x203040", "--size", "1920x1080")
    edl = clean_edl(d, clip)
    edl["captions"] = {"color": "white"}
    (d / "edit" / "edl.json").write_text(json.dumps(edl))
    return d


@pytest.fixture(scope="module")
def final_render(dark_job):
    return render_edl(dark_job / "edit" / "edl.json", dark_job / "edit" / "final.mp4",
                      preview=False, allow_overlays=False)


def test_final_spec(final_render, dark_job):
    info = probe(final_render.path)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["fps"] == "30/1"
    assert info["has_audio"]
    assert 0 < info["duration"] <= 180
    edl = json.loads((dark_job / "edit" / "edl.json").read_text())
    expected = sum(r["end"] - r["start"] for r in edl["ranges"])
    assert abs(info["duration"] - expected) < 0.25


def test_captions_burned_in_and_inside_safe_zone(final_render, dark_job):
    cues = _parse_srt((dark_job / "edit" / "master.srt").read_text())
    assert len(cues) >= 4
    # the filler words were cut, so they must not be captioned
    assert not any(t.startswith(("UM", "UH")) for _, _, t in cues)
    bg = gray_frame(final_render.path, 0.01, 1080, 1920)  # before the first cue
    captioned = 0
    for a, b, _ in cues:
        f = gray_frame(final_render.path, (a + b) / 2, 1080, 1920)
        bright = f > 200
        if bright.sum() > 500:
            captioned += 1
        ys, xs = np.nonzero(bright)
        if len(ys):
            assert ys.max() < 1920 * (1 - SAFE_BOTTOM), "caption in the bottom UI zone"
            assert xs.max() < 1080 * (1 - SAFE_RIGHT), "caption in the right-rail UI zone"
    assert captioned >= len(cues) - 1
    assert (bg > 200).sum() == 0


def test_preview_is_720p(dark_job):
    res = render_edl(dark_job / "edit" / "edl.json", dark_job / "edit" / "preview.mp4",
                     preview=True)
    assert (res.width, res.height) == (720, 1280)


def test_centre_crop_from_portrait_and_captions_off(tmp_path):
    clip = make_clip(tmp_path, "--size", "720x1280")
    edl = clean_edl(tmp_path, clip)
    edl["captions"] = False
    (tmp_path / "edit" / "edl.json").write_text(json.dumps(edl))
    res = render_edl(tmp_path / "edit" / "edl.json", tmp_path / "out.mp4", preview=True)
    assert (res.width, res.height) == (720, 1280)
    assert not (tmp_path / "edit" / "captions.ass").exists()


def test_fit_to_size(tmp_path):
    big = tmp_path / "big.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1080x1920:rate=30:duration=6", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=6", "-c:v", "libx264", "-crf", "8",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(big)], check=True)
    size = big.stat().st_size
    assert fit_to_size(big, tmp_path / "x.mp4", size + 1) == big
    target = 1_500_000
    assert size > target
    out = fit_to_size(big, tmp_path / "small.mp4", target)
    assert out.stat().st_size <= target
    info = probe(out)
    assert info["has_audio"] and info["height"] in (1280, 1920)
