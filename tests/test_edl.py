import json

import pytest

from conftest import clean_edl, needs_media, words_of
from reelbot.edl import CaptionStyle, EDLError, check_edl, find_mid_word_cut, validate_edl

WORDS = [
    {"text": "Hello", "start": 1.0, "end": 1.5, "type": "word"},
    {"text": "world.", "start": 1.6, "end": 2.2, "type": "word"},
]


def _edl(ranges, **kw):
    return {"version": 1, "sources": {"c": "c.mp4"}, "ranges": ranges, **kw}


def _job(tmp_path, words=WORDS):
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "c.json").write_text(json.dumps({"words": words}))
    return tmp_path


def test_find_mid_word_cut():
    assert find_mid_word_cut(WORDS, 1.2)["text"] == "Hello"
    assert find_mid_word_cut(WORDS, 1.55) is None        # in the gap
    assert find_mid_word_cut(WORDS, 1.49) is None        # within drift tolerance
    assert find_mid_word_cut(WORDS, 0.95) is None


def test_valid_edl(tmp_path):
    d = _job(tmp_path)
    assert validate_edl(_edl([{"source": "c", "start": 0.95, "end": 2.28}]), d,
                        check_files=False) == []


def test_mid_word_cut_rejected(tmp_path):
    d = _job(tmp_path)
    errs = validate_edl(_edl([{"source": "c", "start": 1.2, "end": 1.9}]), d, check_files=False)
    assert len(errs) == 2
    assert "inside the word 'Hello'" in errs[0] and "inside the word 'world.'" in errs[1]


def test_over_180_seconds_rejected(tmp_path):
    words = [{"text": "x", "start": 0.0, "end": 0.2, "type": "word"}]
    d = _job(tmp_path, words)
    ranges = [{"source": "c", "start": 1.0, "end": 91.0}, {"source": "c", "start": 100, "end": 191}]
    errs = validate_edl(_edl(ranges), d, check_files=False)
    assert errs == ["total duration 181.0s exceeds the 180s maximum"]
    assert validate_edl(_edl(ranges[:1]), d, check_files=False) == []


@pytest.mark.parametrize("ranges,msg", [
    ([], "non-empty list"),
    ([{"source": "zz", "start": 0, "end": 1}], "not in 'sources'"),
    ([{"source": "c", "start": 2, "end": 1}], "need 0 <= start < end"),
    ([{"source": "c", "start": "a", "end": 1}], "numeric"),
])
def test_structural_errors(tmp_path, ranges, msg):
    errs = validate_edl(_edl(ranges), _job(tmp_path), check_files=False)
    assert any(msg in e for e in errs), errs


def test_missing_source_file_and_overlays_in_lean(tmp_path):
    d = _job(tmp_path)
    edl = _edl([{"source": "c", "start": 0.9, "end": 2.3}],
               overlays=[{"file": "x.mp4", "start_in_output": 0, "duration": 1}])
    errs = validate_edl(edl, d, allow_overlays=False)
    assert any("file not found" in e for e in errs)
    assert any("LEAN_MODE" in e for e in errs)


def test_check_edl_raises_with_all_errors(tmp_path):
    p = tmp_path / "edl.json"
    p.write_text("{not json")
    with pytest.raises(EDLError, match="not valid JSON"):
        check_edl(p)


def test_caption_style_parsing():
    assert CaptionStyle.from_edl({}).color_rgb == "FFFFFF"
    s = CaptionStyle.from_edl({"captions": {"color": "Yellow", "font_size": 500}})
    assert s.color_rgb == "FFE600" and s.font_size == 130
    assert CaptionStyle.from_edl({"captions": {"color": "#00ff00"}}).color_rgb == "00FF00"
    assert CaptionStyle.from_edl({"captions": {"color": "nope"}}).color_rgb == "FFFFFF"
    assert CaptionStyle.from_edl({"captions": False}).enabled is False


@needs_media
def test_fixture_clip_edl_is_valid(job_dir):
    edl = clean_edl(job_dir, job_dir / "clip01.mp4")
    assert validate_edl(edl, job_dir / "edit") == []
    # every filler word from the fixture is outside the kept ranges
    fillers = [w for w in words_of(job_dir / "edit" / "transcripts" / "clip01.json")
               if w["text"].lower().startswith(("um", "uh"))]
    assert fillers
    for f in fillers:
        assert not any(r["start"] < f["start"] < r["end"] for r in edl["ranges"])
