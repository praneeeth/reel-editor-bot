"""EDL (edit decision list) loading and validation.

The EDL format is video-use's (see vendor/video-use/SKILL.md "EDL format"),
extended with optional, renderer-supported creative keys. All times called
``at`` / ``duration`` are on the OUTPUT timeline (seconds from the start of the
finished reel); all positions are fractions of the 1080x1920 frame.

Per range (on top of source/start/end):
    "speed": 1.0                      0.5-2.0 (audio pitch preserved)
    "zoom": 1.0, "zoom_to": 1.3       static punch-in, or a slow zoom from zoom to zoom_to
    "focus_x": 0.5, "focus_y": 0.5    where the 9:16 crop sits in the source (0 = left/top)
    "transition_in": {"type": "fade", "duration": 0.4}   transition from the previous range

Top level:
    "captions": {"enabled": true, "color": "yellow", "font_size": 88}
    "texts":  [{"text": "3 TIPS", "at": 0, "duration": 2.5, "position": "top",
                "color": "white", "size": 96, "box": false}]
    "broll":  [{"source": "clip02", "start": 3.0, "end": 6.0, "at": 4.2,
                "mode": "full" | "pip", "focus_x": 0.5}]        video only; main audio continues
    "images": [{"file": "/abs/assets/image01.png", "at": 0, "duration": 3,
                "x": 0.5, "y": 0.2, "width": 0.4}]              x/y = centre of the image
    "music":  {"file": "/abs/assets/music01.mp3", "volume": 0.15, "duck": true, "offset": 0,
               "replace_audio": false, "source_volume": 1.0}
               replace_audio=true: the song replaces the clips' own sound (volume defaults to 1)
               source_volume: turn the clips' own sound down (0-1) under the music
    "blur":   [{"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1, "at": 2, "duration": 3}]  hide something
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import MAX_OUTPUT_SECONDS

# Scribe timestamps drift a little; a cut this close to a word edge is not "inside" it.
WORD_EDGE_TOLERANCE = 0.02

# Platform UI safe zone, as fractions of the frame (bottom 20 %, right 15 %).
SAFE_BOTTOM = 0.20
SAFE_RIGHT = 0.15

NAMED_COLORS = {
    "white": "FFFFFF", "yellow": "FFE600", "red": "FF3B30", "green": "34C759",
    "cyan": "00E5FF", "blue": "3D8BFF", "orange": "FF9500", "pink": "FF4FA3",
    "purple": "AF52DE", "black": "000000",
}
FONT_SIZE_RANGE = (48, 130)  # captions, in 1080x1920 pixels
TEXT_SIZE_RANGE = (48, 160)
SPEED_RANGE = (0.5, 2.0)
ZOOM_RANGE = (1.0, 2.0)
TRANSITION_DURATION = (0.15, 1.0)
TRANSITIONS = {
    "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "wipeup",
    "wipedown", "slideleft", "slideright", "slideup", "slidedown", "smoothleft",
    "smoothright", "circleopen", "circleclose", "radial", "zoomin", "pixelize", "hblur",
}
TEXT_POSITIONS = {"top": 0.14, "upper": 0.30, "middle": 0.50, "lower": 0.66}
MAX_TEXT_CHARS = 80


class EDLError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid EDL:\n- " + "\n- ".join(errors))


def parse_color(value, default: str = "FFFFFF") -> str:
    color = str(value if value is not None else "").strip().lower()
    rgb = NAMED_COLORS.get(color) or color.lstrip("#").upper()
    if len(rgb) != 6 or any(c not in "0123456789ABCDEF" for c in rgb):
        return default
    return rgb


@dataclass(frozen=True)
class CaptionStyle:
    enabled: bool = True
    color_rgb: str = "FFFFFF"
    font_size: int = 88

    @classmethod
    def from_edl(cls, edl: dict) -> "CaptionStyle":
        raw = edl.get("captions", {})
        if raw is False:
            return cls(enabled=False)
        if not isinstance(raw, dict):
            raw = {}
        try:
            size = int(raw.get("font_size", 88))
        except (TypeError, ValueError):
            size = 88
        size = max(FONT_SIZE_RANGE[0], min(FONT_SIZE_RANGE[1], size))
        return cls(enabled=bool(raw.get("enabled", True)),
                   color_rgb=parse_color(raw.get("color", "white")), font_size=size)


def load_edl(path: Path) -> dict:
    try:
        edl = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise EDLError([f"{path} does not exist"]) from None
    except json.JSONDecodeError as e:
        raise EDLError([f"{path} is not valid JSON: {e}"]) from None
    if not isinstance(edl, dict):
        raise EDLError(["EDL must be a JSON object"])
    return edl


def resolve_source(path: str, edit_dir: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (edit_dir / p).resolve()


# ---- timeline maths (shared with the renderer) ------------------------------------------


def range_speed(r: dict) -> float:
    return float(r.get("speed") or 1.0)


def range_out_duration(r: dict) -> float:
    return (float(r["end"]) - float(r["start"])) / range_speed(r)


def transition_of(r: dict) -> tuple[str, float] | None:
    t = r.get("transition_in")
    if not t:
        return None
    if isinstance(t, str):
        return t, 0.4
    return str(t.get("type", "fade")), float(t.get("duration", 0.4))


def range_offsets(edl: dict) -> list[float]:
    """Output-timeline start of every range, accounting for speed and transition overlaps."""
    offsets, cur = [], 0.0
    for i, r in enumerate(edl["ranges"]):
        tr = transition_of(r) if i else None
        start = cur - (tr[1] if tr else 0.0)
        offsets.append(start)
        cur = start + range_out_duration(r)
    return offsets


def total_duration(edl: dict) -> float:
    ranges = edl.get("ranges", [])
    if not ranges:
        return 0.0
    offs = range_offsets(edl)
    return offs[-1] + range_out_duration(ranges[-1])


# ---- validation ------------------------------------------------------------------------------


def _load_words(transcripts_dir: Path, source: str) -> list[dict] | None:
    tr = transcripts_dir / f"{source}.json"
    if not tr.exists():
        return None
    words = json.loads(tr.read_text()).get("words", [])
    return [w for w in words if w.get("type") == "word"
            and w.get("start") is not None and w.get("end") is not None]


def find_mid_word_cut(words: list[dict], t: float) -> dict | None:
    """Return the word that time ``t`` falls strictly inside, if any."""
    for w in words:
        if float(w["start"]) + WORD_EDGE_TOLERANCE < t < float(w["end"]) - WORD_EDGE_TOLERANCE:
            return w
    return None


def words_between(words: list[dict], a: float, b: float) -> list[dict]:
    return [w for w in words if float(w["end"]) > a + WORD_EDGE_TOLERANCE
            and float(w["start"]) < b - WORD_EDGE_TOLERANCE]


def _num(obj: dict, key: str, default=None):
    v = obj.get(key, default)
    if v is None:
        return None
    return float(v)


def _check_range_value(errors, label, value, lo, hi, name):
    if value is not None and not (lo <= value <= hi):
        errors.append(f"{label} {name}={value} must be between {lo} and {hi}")


def _check_box_in_safe_zone(errors, label, left, top, right, bottom):
    if left < 0 or top < 0 or right > 1.0001 or bottom > 1.0001:
        errors.append(f"{label} goes outside the frame")
    elif right > 1 - SAFE_RIGHT + 1e-4 or bottom > 1 - SAFE_BOTTOM + 1e-4:
        errors.append(f"{label} overlaps the platform UI zone (keep it left of x={1 - SAFE_RIGHT:.2f}"
                      f" and above y={1 - SAFE_BOTTOM:.2f})")


def image_aspect(path: Path) -> float:
    """Height/width of an image file (1.0 if it can't be read)."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.height / img.width
    except (OSError, ValueError, ZeroDivisionError):
        return 1.0


def _check_window(errors, label, obj, total):
    try:
        at, dur = float(obj["at"]), float(obj["duration"])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{label} needs numeric 'at' and 'duration' (output-timeline seconds)")
        return None
    if at < 0 or dur <= 0:
        errors.append(f"{label} has at={at} duration={dur}; need at >= 0 and duration > 0")
    elif total and at + dur > total + 0.1:
        errors.append(f"{label} ends at {at + dur:.2f}s but the reel is only {total:.2f}s long")
    return at, dur


def validate_edl(
    edl: dict,
    edit_dir: Path,
    *,
    max_seconds: float = MAX_OUTPUT_SECONDS,
    allow_overlays: bool = True,
    check_files: bool = True,
) -> list[str]:
    """Return a list of human-readable problems (empty list == valid).

    ``allow_overlays=False`` (LEAN_MODE) only rejects video-use's generated
    animation ``overlays``; b-roll, images, text, music and transitions are
    deterministic renderer features and always allowed.
    """
    errors: list[str] = []
    sources = edl.get("sources")
    ranges = edl.get("ranges")
    if not isinstance(sources, dict) or not sources:
        errors.append("'sources' must be a non-empty object {name: path}")
        sources = {}
    if not isinstance(ranges, list) or not ranges:
        errors.append("'ranges' must be a non-empty list")
        return errors

    if check_files:
        for name, p in sources.items():
            if not resolve_source(str(p), edit_dir).exists():
                errors.append(f"source {name!r} file not found: {p}")

    transcripts_dir = edit_dir / "transcripts"
    word_cache: dict[str, list[dict] | None] = {}

    def words_for(src):
        if src not in word_cache:
            word_cache[src] = _load_words(transcripts_dir, src)
        return word_cache[src]

    ranges_ok = True
    for i, r in enumerate(ranges):
        label = f"range[{i}]"
        try:
            src = r["source"]
            start, end = float(r["start"]), float(r["end"])
            speed = range_speed(r)
            _check_range_value(errors, label, speed, *SPEED_RANGE, "speed")
            for key in ("zoom", "zoom_to"):
                _check_range_value(errors, label, _num(r, key), *ZOOM_RANGE, key)
            for key in ("focus_x", "focus_y"):
                _check_range_value(errors, label, _num(r, key), 0.0, 1.0, key)
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} needs 'source', numeric 'start'/'end' (and numeric "
                          "speed/zoom/focus if given)")
            ranges_ok = False
            continue
        if src not in sources:
            errors.append(f"{label} source {src!r} is not in 'sources'")
            ranges_ok = False
            continue
        if start < 0 or end <= start:
            errors.append(f"{label} has start={start} end={end}; need 0 <= start < end")
            ranges_ok = False
            continue
        words = words_for(src)
        if words is not None:
            for edge_name, t in (("start", start), ("end", end)):
                w = find_mid_word_cut(words, t)
                if w is not None:
                    errors.append(
                        f"{label} {edge_name}={t:.3f} cuts inside the word {w['text']!r} "
                        f"[{float(w['start']):.3f}-{float(w['end']):.3f}] (Hard Rule 6: snap to "
                        f"a word boundary and pad 30-200 ms)")

        tr = None
        try:
            tr = transition_of(r)
        except (TypeError, ValueError):
            errors.append(f"{label} transition_in must be {{'type': ..., 'duration': seconds}}")
        if tr and i == 0:
            errors.append("range[0] cannot have transition_in (nothing to transition from)")
        elif tr:
            kind, d = tr
            if kind not in TRANSITIONS:
                errors.append(f"{label} transition {kind!r} unknown; use one of "
                              f"{', '.join(sorted(TRANSITIONS))}")
            if not (TRANSITION_DURATION[0] <= d <= TRANSITION_DURATION[1]):
                errors.append(f"{label} transition duration {d} must be "
                              f"{TRANSITION_DURATION[0]}-{TRANSITION_DURATION[1]}s")
            try:
                prev = ranges[i - 1]
                if d >= min(range_out_duration(prev), (end - start) / speed) / 2:
                    errors.append(f"{label} transition ({d}s) is longer than half of a "
                                  "neighbouring range")
                # The two ranges overlap for d seconds: speech there would be garbled.
                pw = words_for(prev["source"])
                pe, ps = float(prev["end"]), range_speed(prev)
                if pw and words_between(pw, pe - d * ps, pe):
                    errors.append(f"{label} transition overlaps speech at the end of range[{i-1}]"
                                  f" — extend range[{i-1}] past the last word by >= {d}s of "
                                  "silence or drop the transition")
                if words and words_between(words, start, start + d * speed):
                    errors.append(f"{label} transition overlaps speech at its start — begin the "
                                  f"range >= {d}s before the first word or drop the transition")
            except (KeyError, TypeError, ValueError):
                pass

    total = 0.0
    if ranges_ok:
        try:
            total = total_duration(edl)
        except (KeyError, TypeError, ValueError):
            total = 0.0
    if total > max_seconds:
        errors.append(f"total duration {total:.1f}s exceeds the {max_seconds:.0f}s maximum")

    if not allow_overlays and edl.get("overlays"):
        errors.append("animation overlays are disabled in LEAN_MODE; set 'overlays' to [] "
                      "(b-roll, images, texts, music and transitions are still allowed)")

    _validate_additions(errors, edl, edit_dir, sources, total, check_files)
    return errors


def _validate_additions(errors, edl, edit_dir, sources, total, check_files) -> None:
    for i, t in enumerate(edl.get("texts") or []):
        label = f"texts[{i}]"
        if not isinstance(t, dict) or not str(t.get("text", "")).strip():
            errors.append(f"{label} needs a non-empty 'text'")
            continue
        if len(str(t["text"])) > MAX_TEXT_CHARS:
            errors.append(f"{label} is longer than {MAX_TEXT_CHARS} characters")
        _check_window(errors, label, t, total)
        pos = t.get("position", "top")
        if isinstance(pos, str) and pos not in TEXT_POSITIONS:
            errors.append(f"{label} position must be one of {', '.join(TEXT_POSITIONS)} or a "
                          "number 0-0.75 (fraction of frame height)")
        elif not isinstance(pos, str) and not (0.05 <= float(pos) <= 1 - SAFE_BOTTOM - 0.05):
            errors.append(f"{label} position {pos} is outside 0.05-0.75")

    for i, b in enumerate(edl.get("broll") or []):
        label = f"broll[{i}]"
        try:
            src, start, end, at = b["source"], float(b["start"]), float(b["end"]), float(b["at"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} needs 'source', 'start', 'end' (source seconds) and 'at' "
                          "(output seconds)")
            continue
        if src not in sources:
            errors.append(f"{label} source {src!r} is not in 'sources'")
        if end <= start or start < 0:
            errors.append(f"{label} needs 0 <= start < end")
            continue
        _check_window(errors, label, {"at": at, "duration": end - start}, total)
        if b.get("mode", "full") not in ("full", "pip"):
            errors.append(f"{label} mode must be 'full' or 'pip'")

    for i, im in enumerate(edl.get("images") or []):
        label = f"images[{i}]"
        if not isinstance(im, dict) or not im.get("file"):
            errors.append(f"{label} needs 'file'")
            continue
        if check_files and not resolve_source(str(im["file"]), edit_dir).exists():
            errors.append(f"{label} file not found: {im['file']}")
        _check_window(errors, label, im, total)
        try:
            x, y, w = float(im.get("x", 0.5)), float(im.get("y", 0.3)), float(im.get("width", 0.4))
        except (TypeError, ValueError):
            errors.append(f"{label} x/y/width must be numbers")
            continue
        if not 0.05 <= w <= 1.0:
            errors.append(f"{label} width must be 0.05-1.0 of the frame width")
        h = w * image_aspect(resolve_source(str(im["file"]), edit_dir)) * 1080 / 1920
        _check_box_in_safe_zone(errors, label, x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    for i, bl in enumerate(edl.get("blur") or []):
        label = f"blur[{i}]"
        try:
            x, y, w, h = (float(bl[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} needs numeric x, y, w, h (fractions of the frame, top-left "
                          "corner + size)")
            continue
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.0001 or y + h > 1.0001:
            errors.append(f"{label} box must lie inside the frame")
        _check_window(errors, label, bl, total)

    music = edl.get("music")
    if music:
        if not isinstance(music, dict) or not music.get("file"):
            errors.append("music needs 'file'")
        else:
            if check_files and not resolve_source(str(music["file"]), edit_dir).exists():
                errors.append(f"music file not found: {music['file']}")
            try:
                if not 0.0 < float(music.get("volume", 0.15)) <= 1.0:
                    errors.append("music volume must be > 0 and <= 1.0")
                if not 0.0 <= float(music.get("source_volume", 1.0)) <= 1.0:
                    errors.append("music source_volume must be 0-1")
            except (TypeError, ValueError):
                errors.append("music volume/source_volume must be numbers")


def check_edl(edl_path: Path, **kwargs) -> dict:
    """Load + validate; raise EDLError with every problem found."""
    edl = load_edl(edl_path)
    errors = validate_edl(edl, Path(edl_path).resolve().parent, **kwargs)
    if errors:
        raise EDLError(errors)
    return edl
