#!/usr/bin/env python3
"""
make_montage.py — create a highlights montage from one or more source videos.

Clips 5 s before → 3 s after each goal timestamp and joins everything into
a single montage with a cross-fade transition between clips.

Usage:
    python3 make_montage.py VIDEO [TIMESTAMPS] [VIDEO [TIMESTAMPS] …] [options]

    VIDEO        source video file
    TIMESTAMPS   path to a .txt file (one timestamp per line, or _goals.txt
                 format).  If omitted, <video_stem>_goals.txt is auto-discovered.

Examples:
    # Single video, auto-discover semis-green-segment-1_goals.txt
    python3 make_montage.py semis-green-segment-1.mp4

    # Single video with explicit timestamps file
    python3 make_montage.py game1.mp4 game1_goals.txt

    # Multiple videos
    python3 make_montage.py game1.mp4 game1_goals.txt game2.mp4 game2_goals.txt

Options:
    -o FILE          output file  (default: <stem>_montage.mp4 or montage.mp4)
    --pre SECS       seconds before each timestamp  (default: 5)
    --post SECS      seconds after each timestamp   (default: 3)
    --transition T   cross-fade duration in seconds (default: 0.5)
                     set to 0 for hard cuts
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def parse_timestamp(ts: str) -> float:
    """'H:MM:SS', 'M:SS', or plain seconds → float seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def fmt_ts(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def parse_timestamps_arg(arg: str) -> list:
    """Accept comma-separated timestamps or a path to a _goals.txt file."""
    if os.path.isfile(arg):
        return _parse_goals_file(arg)
    return [parse_timestamp(t) for t in arg.split(",") if t.strip()]


def _parse_goals_file(path: str) -> list:
    """Parse a timestamps file.

    Accepts two formats (can be mixed in the same file):
      • Plain timestamps, one per line:   0:20  /  1:18  /  6:49  /  11:54
      • detect_goals_ball.py goals.txt:   Goal  1  [00:06:49]  …

    Lines starting with '#' and blank lines are ignored.
    """
    timestamps = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # detect_goals_ball.py bracketed format: [HH:MM:SS]
            m = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", line)
            if m:
                timestamps.append(parse_timestamp(m.group(1)))
                continue
            # Plain timestamp line: M:SS  /  H:MM:SS  /  seconds
            m = re.fullmatch(r"(\d+:\d{2}(?::\d{2}(?:\.\d+)?)?|\d+(?:\.\d+)?)", line)
            if m:
                timestamps.append(parse_timestamp(m.group(1)))
    if not timestamps:
        sys.exit(f"ERROR: no timestamps found in {path!r}")
    return timestamps


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def _resolve_sources(tokens: list) -> list:
    """Parse interleaved VIDEO [TIMESTAMPS] tokens into [(video, ts_source), ...].

    Accepts:
      game1.mp4 game1_goals.txt game2.mp4 game2_goals.txt  (explicit)
      game1.mp4 game2.mp4                                   (auto-discover)
      game1.mp4 game1_goals.txt game2.mp4                   (mixed)
    """
    pairs = []
    i = 0
    while i < len(tokens):
        video = tokens[i]
        i += 1
        if i < len(tokens) and tokens[i].lower().endswith(".txt"):
            ts_source = tokens[i]
            i += 1
        else:
            stem = Path(video).stem
            ts_source = str(Path(video).with_name(f"{stem}_goals.txt"))
            if not os.path.isfile(ts_source):
                sys.exit(
                    f"ERROR: no timestamps file for '{video}' "
                    f"and '{ts_source}' not found.\n"
                    f"Pass a .txt file explicitly or create '{ts_source}'."
                )
        pairs.append((video, ts_source))
    return pairs


# ---------------------------------------------------------------------------
# FFmpeg wrappers
# ---------------------------------------------------------------------------

def _check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if subprocess.run([tool, "-version"], capture_output=True).returncode != 0:
            sys.exit(f"ERROR: '{tool}' not found.  Install it with: brew install ffmpeg")


def _clip_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def extract_clip(src: str, start: float, duration: float, out: str) -> None:
    """Cut and re-encode a clip so all clips share a clean, common timebase."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",        # seek before opening (fast)
        "-i", src,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-ar", "44100",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        out,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def build_montage_hard_cut(clips: list, output: str) -> None:
    """Concatenate clips with hard cuts, re-encoding for consistent quality."""
    n = len(clips)
    inputs: list = []
    for c in clips:
        inputs += ["-i", c]

    # concat filter: chains all segments, 1 video + 1 audio stream out
    filter_complex = (
        "".join(f"[{i}:v][{i}:a]" for i in range(n))
        + f"concat=n={n}:v=1:a=1[vout][aout]"
    )

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", "44100",
            "-movflags", "+faststart",
            output,
        ]
    )
    subprocess.run(cmd, check=True, capture_output=True)


def build_montage_xfade(clips: list, output: str, transition: float) -> None:
    """Join clips with video xfade + audio acrossfade transitions.

    xfade offset for transition i (0-indexed):
        offset_i = sum(d_0 … d_{i-1}) − i × transition

    acrossfade automatically detects end-of-stream for each input (overlap=1),
    so no explicit offset is needed there; chaining works by using the merged
    stream from the previous step as the next left-hand input.
    """
    n = len(clips)
    durations = [_clip_duration(c) for c in clips]

    inputs: list = []
    for c in clips:
        inputs += ["-i", c]

    v_parts: list = []
    a_parts: list = []
    v_prev, a_prev = "[0:v]", "[0:a]"
    offset = 0.0

    for i in range(1, n):
        offset += durations[i - 1] - transition
        v_out = "[vout]" if i == n - 1 else f"[v{i}]"
        a_out = "[aout]" if i == n - 1 else f"[a{i}]"

        v_parts.append(
            f"{v_prev}[{i}:v]xfade=transition=fade:"
            f"duration={transition:.3f}:offset={offset:.4f}{v_out}"
        )
        a_parts.append(
            f"{a_prev}[{i}:a]acrossfade=d={transition:.3f}:overlap=1{a_out}"
        )

        v_prev = v_out
        a_prev = a_out

    filter_complex = "; ".join(v_parts + a_parts)

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac",
            output,
        ]
    )
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create a highlights montage from one or more source videos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "sources",
        nargs="+",
        metavar="VIDEO_OR_TIMESTAMPS",
        help="Alternating video + optional timestamps pairs (see usage above)",
    )
    ap.add_argument("-o", "--output",
                    help="Output file (default: <stem>_montage.mp4 or montage.mp4)")
    ap.add_argument("--pre", type=float, default=5.0, metavar="SECS",
                    help="Seconds before each goal to include (default: 5)")
    ap.add_argument("--post", type=float, default=3.0, metavar="SECS",
                    help="Seconds after each goal to include (default: 3)")
    ap.add_argument("--transition", type=float, default=0.5, metavar="T",
                    help="Cross-fade duration in seconds (default: 0.5); 0 = hard cut")
    ap.add_argument("--jobs", type=int,
                    default=min(os.cpu_count() or 2, 4),
                    metavar="N",
                    help="Parallel clip-extraction workers (default: min(CPU count, 4))")
    args = ap.parse_args()

    pairs = _resolve_sources(args.sources)

    # Resolve output filename
    if args.output:
        output = args.output
    elif len(pairs) == 1:
        output = f"{Path(pairs[0][0]).stem}_montage.mp4"
    else:
        output = "montage.mp4"

    # Load all timestamps up-front for the summary
    all_sources = [(video, parse_timestamps_arg(ts_src)) for video, ts_src in pairs]
    total_clips = sum(len(ts) for _, ts in all_sources)
    clip_len = args.pre + args.post

    print(f"Videos    : {len(pairs)}")
    for video, timestamps in all_sources:
        print(f"  {Path(video).name}: {len(timestamps)} goal(s)  "
              f"({', '.join(fmt_ts(t) for t in timestamps)})")
    print(f"Clips     : {total_clips} total  ({clip_len}s each)")
    if args.transition:
        print(f"Transition: {args.transition}s cross-fade")
    else:
        print("Transition: hard cut")
    print(f"Output    : {output}")
    print()

    _check_ffmpeg()

    with tempfile.TemporaryDirectory(prefix="montage_") as tmpdir:
        # Build the full task list up-front so we can parallelise extraction.
        tasks: list = []
        clip_idx = 0
        for video, timestamps in all_sources:
            for ts in timestamps:
                clip_idx += 1
                start = max(0.0, ts - args.pre)
                duration = (ts + args.post) - start
                clip_path = os.path.join(tmpdir, f"clip_{clip_idx:04d}.mp4")
                tasks.append((clip_idx, video, ts, start, duration, clip_path))

        done_count = 0
        print_lock = threading.Lock()
        clips_by_idx: dict = {}

        def _extract(task):
            nonlocal done_count
            n, video, ts, start, duration, clip_path = task
            extract_clip(video, start, duration, clip_path)
            with print_lock:
                done_count += 1
                print(
                    f"  [{done_count}/{total_clips}]  goal {fmt_ts(ts)}"
                    f"  →  {fmt_ts(start)}–{fmt_ts(start + duration)}  done"
                )
            return n, clip_path

        print(f"Extracting {total_clips} clips with {args.jobs} parallel worker(s)…")
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for n, clip_path in pool.map(_extract, tasks):
                clips_by_idx[n] = clip_path

        clips = [clips_by_idx[n] for n in sorted(clips_by_idx)]

        print()
        print("Assembling montage …", end=" ", flush=True)
        if args.transition == 0 or len(clips) == 1:
            build_montage_hard_cut(clips, output)
        else:
            build_montage_xfade(clips, output, args.transition)
        print("done")

    print(f"\n→ {output}")


if __name__ == "__main__":
    main()
