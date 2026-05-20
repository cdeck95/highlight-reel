#!/usr/bin/env python3
"""
downsize.py — re-encode existing video(s) to 1080p H.264/yuv420p for Instagram.

Usage:
    python3 downsize.py VIDEO [VIDEO …] [-o OUTPUT_DIR]

Examples:
    python3 downsize.py semis-blue-pink-with-saves.mp4
    python3 downsize.py semis-blue-pink-with-saves.mp4 semis-green-orange-with-saves.mp4
    python3 downsize.py *.mp4 -o instagram/
"""

import argparse
import subprocess
import sys
from pathlib import Path


def _check_ffmpeg() -> None:
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        sys.exit("ERROR: ffmpeg not found. Install it with: brew install ffmpeg")


def downsize(src: Path, dst: Path) -> None:
    print(f"  {src.name}  →  {dst.name} … ", end="", flush=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        # scale to 1080p max + force limited-range yuv420p (not yuvj420p/full-range)
        # which is what causes silent Instagram rejections
        "-vf", "scale=-2:min(ih\\,1080),format=yuv420p",
        "-c:v", "libx264", "-preset", "slow", "-crf", "20",
        "-maxrate", "5M", "-bufsize", "10M",  # cap at 5 Mbps; Instagram recommends ≤5 Mbps
        "-profile:v", "high", "-level", "4.0",
        "-color_range", "tv",                 # explicitly mark as limited range
        "-colorspace", "bt709",
        "-color_trc", "bt709",
        "-color_primaries", "bt709",
        "-c:a", "aac", "-ar", "44100", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    src_mb = src.stat().st_size / 1_048_576
    dst_mb = dst.stat().st_size / 1_048_576
    print(f"done  ({src_mb:.0f} MB → {dst_mb:.0f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-encode video(s) to 1080p yuv420p H.264 for Instagram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("videos", nargs="+", metavar="VIDEO", help="Input video file(s)")
    ap.add_argument(
        "-o", "--output-dir",
        metavar="DIR",
        help="Directory for output files (default: same folder as input)",
    )
    args = ap.parse_args()

    _check_ffmpeg()

    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    errors = []
    for video_str in args.videos:
        src = Path(video_str)
        if not src.exists():
            print(f"WARNING: '{src}' not found, skipping.")
            errors.append(src)
            continue

        dest_dir = output_dir if output_dir else src.parent
        dst = dest_dir / f"{src.stem}_1080p{src.suffix}"

        try:
            downsize(src, dst)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else ""
            print(f"ERROR\n{stderr}")
            errors.append(src)

    print()
    if errors:
        print(f"Finished with {len(errors)} error(s).")
        sys.exit(1)
    else:
        total = len(args.videos)
        print(f"Done. {total} file{'s' if total != 1 else ''} processed.")


if __name__ == "__main__":
    main()
