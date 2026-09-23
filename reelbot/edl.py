"""EDL (edit decision list) loading and validation.

The EDL format is video-use's (see vendor/video-use/SKILL.md "EDL format") plus
one optional bot-specific key, ``captions``, that styles the burned-in captions:

    "captions": {"enabled": true, "color": "yellow", "font_size": 88}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import MAX_OUTPUT_SECONDS

# Scribe timestamps drift a little; a cut this close to a word edge is not "inside" it.
WORD_EDGE_TOLERANCE = 0.02

NAMED_COLORS = {
    "white": "FFFFFF", "yellow": "FFE600", "red": "FF3B30", "green": "34C759",
    "cyan": "00E5FF", "blue": "3D8BFF", "orange": "FF9500", "pink": "FF4FA3",
    "purple": "AF52DE", "black": "000000",
}
FONT_SIZE_RANGE = (48, 130)  # in 1080x1920 pixels


class EDLError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid EDL:\n- " + "\n- ".join(errors))


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
        color = str(raw.get("color", "white")).strip().lower()
        rgb = NAMED_COLORS.get(color) or color.lstrip("#").upper()
        if len(rgb) != 6 or any(c not in "0123456789ABCDEF" for c in rgb):
            rgb = "FFFFFF"
        try:
            size = int(raw.get("font_size", 88))
        except (TypeError, ValueError):
            size = 88
        size = max(FONT_SIZE_RANGE[0], min(FONT_SIZE_RANGE[1], size))
        return cls(enabled=bool(raw.get("enabled", True)), color_rgb=rgb, font_size=size)


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


def total_duration(edl: dict) -> float:
    return sum(float(r["end"]) - float(r["start"]) for r in edl.get("ranges", []))


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


def validate_edl(
    edl: dict,
    edit_dir: Path,
    *,
    max_seconds: float = MAX_OUTPUT_SECONDS,
    allow_overlays: bool = True,
    check_files: bool = True,
) -> list[str]:
    """Return a list of human-readable problems (empty list == valid)."""
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
    for i, r in enumerate(ranges):
        label = f"range[{i}]"
        try:
            src = r["source"]
            start, end = float(r["start"]), float(r["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} needs 'source', numeric 'start' and 'end'")
            continue
        if src not in sources:
            errors.append(f"{label} source {src!r} is not in 'sources'")
            continue
        if start < 0 or end <= start:
            errors.append(f"{label} has start={start} end={end}; need 0 <= start < end")
            continue
        if src not in word_cache:
            word_cache[src] = _load_words(transcripts_dir, src)
        words = word_cache[src]
        if words is None:
            continue  # no transcript (e.g. silent b-roll) -> nothing to cut inside
        for edge_name, t in (("start", start), ("end", end)):
            w = find_mid_word_cut(words, t)
            if w is not None:
                errors.append(
                    f"{label} {edge_name}={t:.3f} cuts inside the word {w['text']!r} "
                    f"[{float(w['start']):.3f}-{float(w['end']):.3f}] (Hard Rule 6: snap to a "
                    f"word boundary and pad 30-200 ms)"
                )

    if not errors:
        total = total_duration(edl)
        if total > max_seconds:
            errors.append(f"total duration {total:.1f}s exceeds the {max_seconds:.0f}s maximum")

    if not allow_overlays and edl.get("overlays"):
        errors.append("animation overlays are disabled in LEAN_MODE; set 'overlays' to []")
    return errors


def check_edl(edl_path: Path, **kwargs) -> dict:
    """Load + validate; raise EDLError with every problem found."""
    edl = load_edl(edl_path)
    errors = validate_edl(edl, Path(edl_path).resolve().parent, **kwargs)
    if errors:
        raise EDLError(errors)
    return edl
