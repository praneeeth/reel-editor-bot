"""Vertical 9:16 rendering on top of video-use's helpers/render.py.

We reuse video-use's pipeline and its hard rules — per-segment extract with
30 ms audio fades, lossless concat, output-timeline master SRT, subtitles LAST,
loudness normalisation — and only change what the reel format needs:

* every segment is centre-cropped to 9:16 at source resolution, then scaled to
  1080x1920 (final) or 720x1280 (preview) at 30 fps;
* captions use video-use's bold-overlay style (2-word UPPERCASE chunks, bold,
  white-on-outline) but are written as an ASS file with a 1080x1920 canvas so
  they sit clear of the platform UI: above the bottom 20 % and left of the
  right 15 %.

CLI (used by the agent during EXECUTE/REVISE and by the bot for FINAL):

    python -m reelbot.render edit/edl.json -o edit/preview.mp4 --preview
    python -m reelbot.render edit/edl.json -o edit/final.mp4
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import VIDEO_USE_DIR
from .config import OUTPUT_FPS, OUTPUT_H, OUTPUT_W, PREVIEW_H, PREVIEW_W
from .edl import EDLError, CaptionStyle, check_edl, resolve_source
from .media import probe

sys.path.insert(0, str(VIDEO_USE_DIR / "helpers"))
import render as vu  # noqa: E402  (vendor/video-use/helpers/render.py)

# Platform UI safe zone, as fractions of the frame.
SAFE_BOTTOM = 0.20
SAFE_RIGHT = 0.15

CENTRE_CROP_9x16 = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'"


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration: float
    size_bytes: int
    wall_s: float


# -------- segment extraction (replaces vu.extract_segment's scaling) ----------


def extract_vertical_segment(
    source: Path, start: float, duration: float, grade_filter: str, out_path: Path,
    *, width: int, height: int, final: bool,
) -> None:
    """Like video-use's extract_segment, but crops to 9:16 before scaling."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = []
    if vu.is_hdr_source(source):
        vf.append(vu.TONEMAP_CHAIN)
    vf += [CENTRE_CROP_9x16, f"scale={width}:{height}:flags=lanczos", "setsar=1"]
    if grade_filter:
        vf.append(grade_filter)
    fade_out = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out:.3f}:d=0.03"  # Hard Rule 3
    preset, crf = ("fast", "20") if final else ("veryfast", "23")

    has_audio = probe(source)["has_audio"]
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(source)]
    if not has_audio:  # keep an audio track so concat + -map 0:a always work
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
            "-vf", ",".join(vf), "-af", af,
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p", "-r", str(OUTPUT_FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out_path)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg segment extract failed: {proc.stderr[-800:]}")


# -------- captions ---------------------------------------------------------------


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    pat = re.compile(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)")
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = pat.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues.append((a, b, " ".join(lines[i + 1:]).strip()))
                break
    return cues


def caption_style_fields(style: CaptionStyle) -> dict[str, str | int]:
    rgb = style.color_rgb
    bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]
    margin_r = int(OUTPUT_W * SAFE_RIGHT) + 40
    return {
        "Fontname": "Helvetica",  # video-use bold-overlay; fontconfig falls back to Arial/Liberation
        "Fontsize": style.font_size,
        "PrimaryColour": f"&H00{bgr}",
        "OutlineColour": "&H00000000",
        "BackColour": "&H00000000",
        "Bold": -1,
        "BorderStyle": 1,
        "Outline": 6,
        "Shadow": 0,
        "Alignment": 2,
        # symmetric side margins keep the text centred and clear of the right rail
        "MarginL": margin_r,
        "MarginR": margin_r,
        # bottom of the text sits well above the bottom-20% UI band
        "MarginV": int(OUTPUT_H * SAFE_BOTTOM) + 150,
    }


def force_style_string(style: CaptionStyle) -> str:
    f = caption_style_fields(style)
    keys = ["Fontname", "Fontsize", "PrimaryColour", "OutlineColour", "BackColour", "Bold",
            "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV"]
    return ",".join(f"{'FontName' if k == 'Fontname' else k}={f[k]}" for k in keys)


def srt_to_ass(srt_path: Path, ass_path: Path, style: CaptionStyle) -> int:
    f = caption_style_fields(style)
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUTPUT_W}", f"PlayResY: {OUTPUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{f['Fontname']},{f['Fontsize']},{f['PrimaryColour']},&H000000FF,"
        f"{f['OutlineColour']},{f['BackColour']},{f['Bold']},0,0,0,100,100,0,0,"
        f"{f['BorderStyle']},{f['Outline']},{f['Shadow']},{f['Alignment']},"
        f"{f['MarginL']},{f['MarginR']},{f['MarginV']},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    cues = _parse_srt(srt_path.read_text())
    events = [
        f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,"
        + t.replace("{", "(").replace("}", ")")
        for a, b, t in cues
    ]
    ass_path.write_text("\n".join(header + events) + "\n", encoding="utf-8")
    return len(cues)


@contextlib.contextmanager
def _force_style(style: str):
    old = vu.SUB_FORCE_STYLE
    vu.SUB_FORCE_STYLE = style
    try:
        yield
    finally:
        vu.SUB_FORCE_STYLE = old


# -------- main entry ------------------------------------------------------------


def render_edl(
    edl_path: Path, out_path: Path, *, preview: bool, allow_overlays: bool = True,
    loudnorm: bool = True,
) -> RenderResult:
    t0 = time.monotonic()
    edl_path = Path(edl_path).resolve()
    out_path = Path(out_path).resolve()
    edit_dir = edl_path.parent
    edl = check_edl(edl_path, allow_overlays=allow_overlays)
    width, height = (PREVIEW_W, PREVIEW_H) if preview else (OUTPUT_W, OUTPUT_H)

    # 1. per-segment extract (grade + fades baked in; Hard Rules 2 and 3)
    grade = vu.resolve_grade_filter(edl.get("grade"))
    clips_dir = edit_dir / ("clips_preview" if preview else "clips_graded")
    clips_dir.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, r in enumerate(edl["ranges"]):
        src = resolve_source(edl["sources"][r["source"]], edit_dir)
        start, end = float(r["start"]), float(r["end"])
        seg_grade = grade
        if grade == "__AUTO__":
            seg_grade, _ = vu.auto_grade_for_clip(src, start=start, duration=end - start)
        seg = clips_dir / f"seg_{i:02d}_{r['source']}.mp4"
        extract_vertical_segment(src, start, end - start, seg_grade, seg,
                                 width=width, height=height, final=not preview)
        segs.append(seg)

    # 2. lossless concat
    base = edit_dir / ("base_preview.mp4" if preview else "base.mp4")
    vu.concat_segments(segs, base, edit_dir)

    # 3. captions: video-use's master SRT (output-timeline offsets, Hard Rule 5) -> ASS
    style = CaptionStyle.from_edl(edl)
    subs: Path | None = None
    if style.enabled:
        srt = edit_dir / "master.srt"
        vu.build_master_srt(edl, edit_dir, srt)
        subs = edit_dir / "captions.ass"
        if srt_to_ass(srt, subs, style) == 0:
            subs = None

    # 4. overlays then subtitles LAST (Hard Rule 1), then loudness normalisation
    overlays = edl.get("overlays") or []
    target = out_path.with_suffix(".prenorm.mp4") if loudnorm else out_path
    with _force_style(force_style_string(style)):
        vu.build_final_composite(base, overlays, subs, target, edit_dir)
    if loudnorm:
        vu.apply_loudnorm_two_pass(target, out_path, preview=preview)
        target.unlink(missing_ok=True)

    info = probe(out_path)
    return RenderResult(out_path, info["width"], info["height"], info["duration"],
                        out_path.stat().st_size, time.monotonic() - t0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render a video-use EDL as a 9:16 reel")
    ap.add_argument("edl", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--preview", action="store_true", help="720x1280 fast preview")
    ap.add_argument("--no-overlays", action="store_true", help="reject EDLs with overlays")
    args = ap.parse_args(argv)
    try:
        res = render_edl(args.edl, args.output, preview=args.preview,
                         allow_overlays=not args.no_overlays)
    except EDLError as e:
        print(f"EDL REJECTED — fix edl.json and re-run:\n{e}", file=sys.stderr)
        return 2
    print(f"rendered {res.path} {res.width}x{res.height} {res.duration:.2f}s "
          f"{res.size_bytes / 1e6:.1f} MB in {res.wall_s:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
