"""Vertical 9:16 rendering on top of video-use's helpers/render.py.

We reuse video-use's building blocks and keep its hard rules — per-segment
extract with 30 ms audio fades (Rules 2, 3), lossless concat when there are no
transitions, output-timeline captions (Rule 5), overlays PTS-shifted to their
window (Rule 4) and subtitles LAST (Rule 1), loudness normalisation — and add
what reels need (see reelbot/edl.py for the EDL keys):

* every range is cropped to 9:16 at source resolution (focus_x/focus_y pick the
  crop position), optionally zoomed / sped up, then scaled to 1080x1920
  (final) or 720x1280 (preview) at 30 fps;
* transitions between ranges (xfade + acrossfade);
* b-roll cutaways (full-screen or picture-in-picture), images/logos, blur
  boxes, text titles and background music ducked under speech;
* captions in video-use's bold-overlay style, as an ASS file on a 1080x1920
  canvas, clear of the platform UI (bottom 20 %, right 15 %).

CLI (used by the agent during EXECUTE/REVISE and by the bot for FINAL):

    python -m reelbot.render edit/edl.json -o edit/preview.mp4 --preview
    python -m reelbot.render edit/edl.json -o edit/final.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import VIDEO_USE_DIR
from .config import OUTPUT_FPS, OUTPUT_H, OUTPUT_W, PREVIEW_H, PREVIEW_W
from .edl import (
    SAFE_BOTTOM, SAFE_RIGHT, TEXT_POSITIONS, TEXT_SIZE_RANGE, CaptionStyle, EDLError,
    check_edl, parse_color, range_offsets, range_out_duration, range_speed, resolve_source,
    transition_of,
)
from .media import probe

sys.path.insert(0, str(VIDEO_USE_DIR / "helpers"))
import render as vu  # noqa: E402  (vendor/video-use/helpers/render.py)

PIP_WIDTH = 0.70  # picture-in-picture b-roll width, fraction of frame
PIP_TOP = 0.12
PUNCT_BREAK = set(".,!?;:")


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration: float
    size_bytes: int
    wall_s: float


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {what} failed: {proc.stderr[-1200:]}")


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


# -------- segment extraction (replaces vu.extract_segment's scaling) -------------------


def vertical_filters(source: Path, *, width: int, height: int, duration: float,
                     focus: tuple[float, float] = (0.5, 0.5), zoom: float = 1.0,
                     zoom_to: float | None = None, speed: float = 1.0) -> list[str]:
    fx, fy = focus
    vf = []
    if vu.is_hdr_source(source):
        vf.append(vu.TONEMAP_CHAIN)
    vf.append(f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)':'(iw-ow)*{fx}':'(ih-oh)*{fy}'")
    if zoom_to is not None and abs(zoom_to - zoom) > 1e-3:
        frames = max(1, round(duration * OUTPUT_FPS))
        # Upscale first so zoompan's integer crop steps don't make the image jitter.
        vf += [f"fps={OUTPUT_FPS}", f"scale={width * 2}:{height * 2}:flags=lanczos",
               f"zoompan=z='{zoom}+({zoom_to}-{zoom})*on/{frames}'"
               f":x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps={OUTPUT_FPS}"]
    else:
        if zoom > 1.0:
            vf.append(f"crop='iw/{zoom}':'ih/{zoom}'")
        vf.append(f"scale={width}:{height}:flags=lanczos")
    vf.append("setsar=1")
    if abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed}")
    return vf


def extract_vertical_segment(
    source: Path, start: float, duration: float, grade_filter: str, out_path: Path,
    *, width: int, height: int, final: bool, speed: float = 1.0, zoom: float = 1.0,
    zoom_to: float | None = None, focus: tuple[float, float] = (0.5, 0.5),
    audio: bool = True,
) -> None:
    """Like video-use's extract_segment, but crops to 9:16 (and zooms/speeds) before scaling."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = vertical_filters(source, width=width, height=height, duration=duration, focus=focus,
                          zoom=zoom, zoom_to=zoom_to, speed=speed)
    if grade_filter:
        vf.append(grade_filter)
    out_dur = duration / speed
    preset, crf = ("fast", "20") if final else ("veryfast", "23")

    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    has_audio = audio and probe(source)["has_audio"]
    if audio and not has_audio:  # keep an audio track so concat / mixing always work
        cmd += ["-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-map", "0:v:0", "-vf", ",".join(vf)]
    if audio:
        af = []
        if has_audio and abs(speed - 1.0) > 1e-3:
            af.append(f"atempo={speed}")
        fade_out = max(0.0, out_dur - 0.03)
        af.append(f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out:.3f}:d=0.03")  # Rule 3
        cmd += ["-map", "0:a:0" if has_audio else "1:a:0", "-af", ",".join(af),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
            "-r", str(OUTPUT_FPS), "-t", f"{out_dur:.3f}", "-movflags", "+faststart",
            str(out_path)]
    _run(cmd, "segment extract")


# -------- base assembly: lossless concat, or xfade chain when there are transitions ------


def build_base(edl: dict, segs: list[Path], out: Path, edit_dir: Path, *, final: bool) -> None:
    transitions = [transition_of(r) if i else None for i, r in enumerate(edl["ranges"])]
    if not any(transitions):
        vu.concat_segments(segs, out, edit_dir)  # Rule 2: -c copy, no re-encode
        return
    inputs: list[str] = []
    parts: list[str] = []
    for k, seg in enumerate(segs):
        inputs += ["-i", str(seg)]
        parts.append(f"[{k}:v]fps={OUTPUT_FPS},settb=AVTB,setpts=PTS-STARTPTS[v{k}]")
        parts.append(f"[{k}:a]aresample=48000,asetpts=PTS-STARTPTS[a{k}]")
    cur_v, cur_a = "[v0]", "[a0]"
    cur_len = probe(segs[0])["duration"]
    for k in range(1, len(segs)):
        seg_len = probe(segs[k])["duration"]
        tr = transitions[k]
        if tr:
            kind, d = tr
            off = max(0.0, cur_len - d)
            parts.append(f"{cur_v}[v{k}]xfade=transition={kind}:duration={d}:offset={off:.3f}"
                         f"[xv{k}]")
            parts.append(f"{cur_a}[a{k}]acrossfade=d={d}:c1=tri:c2=tri[xa{k}]")
            cur_len = off + seg_len
        else:
            parts.append(f"{cur_v}{cur_a}[v{k}][a{k}]concat=n=2:v=1:a=1[xv{k}][xa{k}]")
            cur_len += seg_len
        cur_v, cur_a = f"[xv{k}]", f"[xa{k}]"
    preset, crf = ("fast", "20") if final else ("veryfast", "23")
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(parts), "-map", cur_v,
          "-map", cur_a, "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt",
          "yuv420p", "-r", str(OUTPUT_FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
          "-movflags", "+faststart", str(out)], "transitions")


# -------- captions + text titles (ASS on a 1080x1920 canvas) ---------------------------


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(t: str) -> str:
    return t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _bgr(rgb: str) -> str:
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


def caption_cues(edl: dict, edit_dir: Path) -> list[tuple[float, float, str]]:
    """video-use bold-overlay chunks (2 words, UPPERCASE, break on punctuation) placed on the
    output timeline: out = range_offset + (word.start - range.start) / speed  (Rule 5)."""
    cues: list[tuple[float, float, str]] = []
    offsets = range_offsets(edl)
    for r, off in zip(edl["ranges"], offsets):
        tr_path = edit_dir / "transcripts" / f"{r['source']}.json"
        if not tr_path.exists():
            continue
        start, end, speed = float(r["start"]), float(r["end"]), range_speed(r)
        words = [w for w in vu._words_in_range(json.loads(tr_path.read_text()), start, end)
                 if (w.get("text") or "").strip()]
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) >= 2 or w["text"].strip()[-1] in PUNCT_BREAK:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
        seg_out_end = off + range_out_duration(r)
        for ch in chunks:
            a = off + (max(start, float(ch[0]["start"])) - start) / speed
            b = min(seg_out_end, off + (min(end, float(ch[-1]["end"])) - start) / speed)
            if b <= a:
                b = a + 0.4
            text = re.sub(r"\s+", " ", " ".join(w["text"].strip() for w in ch)).strip()
            cues.append((a, b, text.rstrip(",;:").upper()))
    cues.sort(key=lambda c: c[0])
    return cues


def write_ass(edl: dict, edit_dir: Path, ass_path: Path) -> int:
    """Captions + text titles into one ASS file. Returns the number of events."""
    cap = CaptionStyle.from_edl(edl)
    side = int(OUTPUT_W * SAFE_RIGHT) + 40
    styles = [
        f"Style: Caption,Helvetica,{cap.font_size},&H00{_bgr(cap.color_rgb)},&H000000FF,"
        f"&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,{side},{side},"
        f"{int(OUTPUT_H * SAFE_BOTTOM) + 150},1",
    ]
    events = []
    if cap.enabled:
        events += [f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Caption,,0,0,0,,{_ass_text(t)}"
                   for a, b, t in caption_cues(edl, edit_dir)]
    for i, t in enumerate(edl.get("texts") or []):
        size = int(max(TEXT_SIZE_RANGE[0], min(TEXT_SIZE_RANGE[1], int(t.get("size", 96)))))
        color = _bgr(parse_color(t.get("color", "white")))
        box = bool(t.get("box", False))
        # BorderStyle 3 = opaque box behind the text (BackColour, semi-transparent black)
        border = f"3,18,0" if box else "1,6,2"
        styles.append(f"Style: Text{i},Helvetica,{size},&H00{color},&H000000FF,&H80000000,"
                      f"&H80000000,-1,0,0,0,100,100,0,0,{border},5,{side},{side},0,1")
        pos = t.get("position", "top")
        y = TEXT_POSITIONS[pos] if isinstance(pos, str) else float(pos)
        at, dur = float(t["at"]), float(t["duration"])
        x_centre = OUTPUT_W // 2  # the symmetric side margins keep it clear of the right rail
        events.append(f"Dialogue: 1,{_ass_time(at)},{_ass_time(at + dur)},Text{i},,0,0,0,,"
                      f"{{\\an5\\pos({x_centre},{int(y * OUTPUT_H)})\\fad(150,150)}}"
                      f"{_ass_text(str(t['text']))}")
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUTPUT_W}", f"PlayResY: {OUTPUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ass_path.write_text("\n".join(header + events) + "\n", encoding="utf-8")
    return len(events)


def _write_srt(cues, path: Path) -> None:
    lines = []
    for i, (a, b, t) in enumerate(cues, 1):
        lines += [str(i), f"{vu._srt_timestamp(a)} --> {vu._srt_timestamp(b)}", t, ""]
    path.write_text("\n".join(lines))


# -------- final composite ---------------------------------------------------------------------


def compose(edl: dict, edit_dir: Path, base: Path, out: Path, *, width: int, height: int,
            final: bool, clips_dir: Path) -> None:
    total = probe(base)["duration"]
    sx, sy = width / OUTPUT_W, height / OUTPUT_H
    inputs: list[str] = ["-i", str(base)]
    parts: list[str] = []
    cur = "[0:v]"
    n = 1

    def overlay(src_label: str, x: str, y: str, at: float, dur: float) -> None:
        nonlocal cur
        parts.append(f"{src_label}setpts=PTS-STARTPTS+{at:.3f}/TB[s{n}]")  # Rule 4
        parts.append(f"{cur}[s{n}]overlay=x={x}:y={y}:eof_action=pass:"
                     f"enable='between(t,{at:.3f},{at + dur:.3f})'[c{n}]")
        cur = f"[c{n}]"

    # b-roll cutaways: extracted to the frame size (full) or a PiP box, video only
    for i, b in enumerate(edl.get("broll") or []):
        src = resolve_source(edl["sources"][b["source"]], edit_dir)
        start, end, at = float(b["start"]), float(b["end"]), float(b["at"])
        pip = b.get("mode", "full") == "pip"
        clip = clips_dir / f"broll_{i:02d}.mp4"
        if pip:
            w = _even(width * PIP_WIDTH)
            vf = f"scale={w}:-2:flags=lanczos,setsar=1"
            _run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i",
                  str(src), "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf",
                  "18", "-pix_fmt", "yuv420p", "-r", str(OUTPUT_FPS), str(clip)], "b-roll")
            # centred in the area left of the right-rail UI
            x, y = str(max(0, (int(width * (1 - SAFE_RIGHT)) - w) // 2)), str(int(height * PIP_TOP))
        else:
            extract_vertical_segment(src, start, end - start, "", clip, width=width,
                                     height=height, final=final, audio=False,
                                     focus=(float(b.get("focus_x", 0.5)),
                                            float(b.get("focus_y", 0.5))))
            x, y = "0", "0"
        inputs += ["-i", str(clip)]
        overlay(f"[{n}:v]", x, y, at, end - start)
        n += 1

    # video-use animation overlays (non-lean only; validated upstream)
    for ov in edl.get("overlays") or []:
        inputs += ["-i", str(resolve_source(ov["file"], edit_dir))]
        parts.append(f"[{n}:v]scale={width}:{height},setsar=1[ovs{n}]")
        overlay(f"[ovs{n}]", "0", "0", float(ov["start_in_output"]), float(ov["duration"]))
        n += 1

    # blur boxes ("remove" something from the frame)
    for i, bl in enumerate(edl.get("blur") or []):
        bw, bh = _even(float(bl["w"]) * width), _even(float(bl["h"]) * height)
        bx, by = int(float(bl["x"]) * width), int(float(bl["y"]) * height)
        bw, bh = min(bw, width - bx), min(bh, height - by)
        radius = max(2, min(40, min(bw, bh) // 4))
        at, dur = float(bl["at"]), float(bl["duration"])
        parts.append(f"{cur}split[bm{i}][bc{i}]")
        parts.append(f"[bc{i}]crop={bw}:{bh}:{bx}:{by},boxblur={radius}:3[bb{i}]")
        parts.append(f"[bm{i}][bb{i}]overlay=x={bx}:y={by}:"
                     f"enable='between(t,{at:.3f},{at + dur:.3f})'[bo{i}]")
        cur = f"[bo{i}]"

    # images / logos: looped stills, centred at (x, y)
    for im in edl.get("images") or []:
        at, dur = float(im["at"]), float(im["duration"])
        inputs += ["-loop", "1", "-framerate", str(OUTPUT_FPS), "-t", f"{dur:.3f}", "-i",
                   str(resolve_source(im["file"], edit_dir))]
        w = _even(float(im.get("width", 0.4)) * width)
        parts.append(f"[{n}:v]scale={w}:-2:flags=lanczos,format=rgba[im{n}]")
        cx, cy = float(im.get("x", 0.5)) * width, float(im.get("y", 0.3)) * height
        overlay(f"[im{n}]", f"'{cx:.1f}-w/2'", f"'{cy:.1f}-h/2'", at, dur)
        n += 1

    # captions + texts LAST (Rule 1)
    ass = edit_dir / "captions.ass"
    if write_ass(edl, edit_dir, ass):
        path = str(ass.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        parts.append(f"{cur}subtitles='{path}'[vout]")
        cur = "[vout]"
    elif ass.exists():
        ass.unlink()

    # audio: speech (+ music ducked under it)
    audio_map = "0:a"
    music = edl.get("music")
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(resolve_source(music["file"], edit_dir))]
        replace = bool(music.get("replace_audio", False))
        vol = float(music.get("volume", 1.0 if replace else 0.15))
        offset = float(music.get("offset", 0.0))
        fade_out = max(0.0, total - 1.5)
        parts.append(f"[{n}:a]atrim=start={offset:.3f},asetpts=PTS-STARTPTS,"
                     f"atrim=duration={total:.3f},aresample=48000,"
                     f"aformat=channel_layouts=stereo,volume={vol},"
                     f"afade=t=in:d=0.8,afade=t=out:st={fade_out:.3f}:d=1.5[mu]")
        src_vol = float(music.get("source_volume", 1.0))
        speech = "[0:a]"
        if not replace and abs(src_vol - 1.0) > 1e-3:
            parts.append(f"[0:a]volume={src_vol}[spv]")
            speech = "[spv]"
        if replace:
            # the song replaces the clip's own sound entirely
            parts.append("[mu]anull[aout]")
        elif music.get("duck", True):
            parts.append(f"{speech}asplit=2[sp][sc]")
            parts.append("[mu][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:"
                         "release=400[mud]")
            parts.append("[sp][mud]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            parts.append(f"{speech}[mu]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
        n += 1

    if not parts:
        shutil.copyfile(base, out)
        return
    if cur == "[0:v]":
        parts.append("[0:v]null[vout]")
        cur = "[vout]"
    preset, crf = ("fast", "18") if final else ("veryfast", "23")
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(parts), "-map", cur,
          "-map", audio_map, "-c:v", "libx264", "-preset", preset, "-crf", crf,
          "-pix_fmt", "yuv420p", "-r", str(OUTPUT_FPS),
          *(["-c:a", "copy"] if audio_map == "0:a" else
            ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]),
          "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)], "composite")


# -------- main entry ------------------------------------------------------------------------


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
    final = not preview

    # 1. per-segment extract (crop/zoom/speed + grade + fades baked in; Rules 2 and 3)
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
        zoom_to = r.get("zoom_to")
        extract_vertical_segment(
            src, start, end - start, seg_grade, seg, width=width, height=height, final=final,
            speed=range_speed(r), zoom=float(r.get("zoom") or 1.0),
            zoom_to=float(zoom_to) if zoom_to is not None else None,
            focus=(float(r.get("focus_x", 0.5)), float(r.get("focus_y", 0.5))))
        segs.append(seg)

    # 2. base: lossless concat, or one xfade pass when transitions are used
    base = edit_dir / ("base_preview.mp4" if preview else "base.mp4")
    build_base(edl, segs, base, edit_dir, final=final)

    # 3. keep a plain master.srt for reference / self-eval
    _write_srt(caption_cues(edl, edit_dir), edit_dir / "master.srt")

    # 4. b-roll, blur, images, then captions/texts LAST; music mix; loudness
    target = out_path.with_suffix(".prenorm.mp4") if loudnorm else out_path
    compose(edl, edit_dir, base, target, width=width, height=height, final=final,
            clips_dir=clips_dir)
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
    ap.add_argument("--no-overlays", action="store_true",
                    help="reject video-use animation overlays (LEAN_MODE)")
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
