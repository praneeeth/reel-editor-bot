"""Generate a reproducible landscape test clip with speech, pauses and filler words.

Each word is synthesised separately with espeak-ng, trimmed to its audible
extent and placed on a timeline, so exact word timestamps are known. That lets
us also write a transcript in ElevenLabs Scribe JSON format, which can pre-seed
video-use's transcript cache (<videos_dir>/edit/transcripts/<stem>.json) when
Scribe isn't reachable.

Usage:
    python scripts/make_test_clip.py OUT_DIR            # ~40 s clip01.mp4 + fixture
    python scripts/make_test_clip.py OUT_DIR --short    # ~10 s version for tests
    python scripts/make_test_clip.py OUT_DIR --no-transcript

Requires ffmpeg and espeak-ng on PATH.
"""

from __future__ import annotations

import argparse
import array
import json
import subprocess
import tempfile
import wave
from pathlib import Path

RATE = 22050  # espeak-ng native sample rate

# (text, pause_after_seconds). Words get a 0.05 s gap unless a pause is given.
SCRIPT_FULL: list[tuple[str, float]] = [
    ("Hey everyone, um, today I want to show you something.", 1.2),
    ("Uh, so, most people edit their videos by hand.", 0.8),
    ("And, um, that takes forever.", 1.5),
    ("What if an agent could, uh, cut out the filler words for you?", 1.0),
    ("Um, it reads the transcript, and it finds the pauses.", 1.2),
    ("Then it renders a vertical reel with, uh, bold captions.", 1.0),
    ("Let me show you how it works.", 0.8),
    ("Um, okay. So, uh, first you send a clip.", 1.2),
    ("Then you approve the plan.", 1.0),
    ("That's it. Thanks for watching!", 0.6),
]
SCRIPT_SHORT: list[tuple[str, float]] = [
    ("Hey everyone, um, this is a quick test.", 1.0),
    ("Uh, we cut the filler words.", 0.9),
    ("Thanks for watching!", 0.5),
]
WORD_GAP = 0.05
LEAD_IN = 0.5


def synth_word(word: str, tmp: Path) -> array.array:
    wav = tmp / "w.wav"
    subprocess.run(
        ["espeak-ng", "-v", "en-us", "-s", "195", "-w", str(wav), word],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with wave.open(str(wav), "rb") as w:
        assert w.getframerate() == RATE and w.getsampwidth() == 2
        samples = array.array("h", w.readframes(w.getnframes()))
    # Trim leading/trailing near-silence so timestamps are the audible word.
    thresh = 300
    idx = [i for i, s in enumerate(samples) if abs(s) > thresh]
    if not idx:
        return array.array("h")
    return samples[idx[0]: idx[-1] + 1]


def build(script: list[tuple[str, float]], tmp: Path) -> tuple[array.array, list[dict], float]:
    audio = array.array("h", [0] * int(LEAD_IN * RATE))
    words: list[dict] = []
    t = LEAD_IN
    for sentence, pause in script:
        tokens = sentence.split()
        for i, tok in enumerate(tokens):
            spoken = tok.strip(",.!?")
            samples = synth_word(spoken, tmp)
            start = t
            end = t + len(samples) / RATE
            words.append({"text": tok, "start": round(start, 3), "end": round(end, 3),
                          "type": "word", "speaker_id": "speaker_0", "logprob": 0.0})
            audio.extend(samples)
            gap = pause if i == len(tokens) - 1 else WORD_GAP
            # Commas get a little breathing room, like natural speech.
            if i < len(tokens) - 1 and tok.endswith(","):
                gap = 0.2
            audio.extend([0] * int(gap * RATE))
            t = end + int(gap * RATE) / RATE
    return audio, words, t


def scribe_json(words: list[dict]) -> dict:
    """Interleave 'spacing' entries like Scribe does."""
    out: list[dict] = []
    for i, w in enumerate(words):
        if i:
            prev = words[i - 1]
            out.append({"text": " ", "start": prev["end"], "end": w["start"],
                        "type": "spacing", "speaker_id": "speaker_0", "logprob": 0.0})
        out.append(w)
    return {
        "language_code": "eng",
        "language_probability": 1.0,
        "text": " ".join(w["text"] for w in words),
        "words": out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--short", action="store_true", help="~10 s clip for fast tests")
    ap.add_argument("--name", default="clip01", help="output stem (default clip01)")
    ap.add_argument("--no-transcript", action="store_true",
                    help="don't pre-seed edit/transcripts/<name>.json")
    ap.add_argument("--size", default="1280x720", help="WxH (default 1280x720 landscape)")
    ap.add_argument("--background", default="testsrc2",
                    help="'testsrc2' (moving pattern) or an ffmpeg colour like 0x203040")
    args = ap.parse_args()

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    script = SCRIPT_SHORT if args.short else SCRIPT_FULL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio, words, total = build(script, tmp)
        wav = tmp / "speech.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio.tobytes())

        dur = f"{total:.3f}"
        if args.background == "testsrc2":
            video_src = f"testsrc2=size={args.size}:rate=30:duration={dur}"
        else:
            video_src = f"color=c={args.background}:size={args.size}:rate=30:duration={dur}"
        out = out_dir / f"{args.name}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", video_src, "-i", str(wav),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-shortest",
             "-movflags", "+faststart", str(out)],
            check=True,
        )

    if not args.no_transcript:
        tr = out_dir / "edit" / "transcripts" / f"{args.name}.json"
        tr.parent.mkdir(parents=True, exist_ok=True)
        tr.write_text(json.dumps(scribe_json(words), indent=2))
        print(f"transcript fixture: {tr}")
    print(f"clip: {out} ({total:.1f}s, {len(words)} words)")


if __name__ == "__main__":
    main()
