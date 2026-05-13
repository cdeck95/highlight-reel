"""
Hockey Goal Detector — Gemini Video API
----------------------------------------
Analyzes a GoPro hockey game recording and timestamps every goal.

Videos larger than 1.9 GB are automatically split into segments using ffmpeg,
processed individually, and timestamps are merged into a single output.

Usage:
    python detect_goals_gemini.py game.mp4
    python detect_goals_gemini.py game.mp4 --output goals.txt
    python detect_goals_gemini.py game.mp4 --model gemini-2.5-pro-preview-05-06

Requirements:
    pip install -r requirements.txt
    brew install ffmpeg          # needed for videos > 1.9 GB
    # Set GEMINI_API_KEY in a .env file or export it
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai

load_dotenv()

MAX_UPLOAD_BYTES = 1_900_000_000  # 1.9 GB — stay under the 2 GB File API limit

# ── Prompt ──────────────────────────────────────────────────────────────────

GOAL_DETECTION_PROMPT = """
You are analyzing footage from an outdoor ball hockey game recorded on a GoPro.

Your task: identify every moment a GOAL is scored and return the timestamp.

The ball is ORANGE or FLUORESCENT PINK. Keep this in mind when tracking it.

A GOAL requires ALL of the following:
- The ball physically enters and stays inside the net (crosses the goal line)
- The referee signals by pointing at/toward the net with their arm
- Play stops — players react, substitutions may happen, then a faceoff restarts play

Do NOT flag these as goals:
- A shot where the goalie makes a SAVE (ball is blocked and does not enter the net)
- A near-miss (ball hits the post or goes wide)
- Any play stoppage that is NOT followed by a faceoff

IMPORTANT: The timestamp should be the moment the BALL ENTERS THE NET —
not when the referee signals, not when players celebrate. The ball entry
comes first; the celebration and ref signal follow a few seconds later.

Return a JSON array of goal events. Each object must have:
  - "timestamp_seconds": number (seconds into the video when ball enters net)
  - "timestamp_hms": string in HH:MM:SS format
  - "confidence": "high" | "medium" | "low"
  - "notes": what you observed (ball color, which side of net, ref signal, faceoff)

If there are NO goals in the video, return an empty array: []

Return ONLY valid JSON — no markdown fences, no extra text.

Example output:
[
  {"timestamp_seconds": 312.5, "timestamp_hms": "00:05:12", "confidence": "high", "notes": "Orange ball enters right side of net, ref points, faceoff follows"},
  {"timestamp_seconds": 1847.0, "timestamp_hms": "00:30:47", "confidence": "medium", "notes": "Play stops after shot, ref gestures toward net, faceoff restarts"}
]
"""

REFINEMENT_PROMPT = """
You are analyzing a short video clip (~60 seconds) from a ball hockey game.

A goal MAY have occurred somewhere in this clip. Find the EXACT moment the ball
crosses the goal line and enters the net.

The ball is ORANGE or FLUORESCENT PINK.

Critical distinctions:
- GOAL: ball fully crosses into the net and stays — this is what you are looking for
- SAVE: goalie blocks or catches the ball before it enters — this is NOT a goal
- NEAR-MISS: ball hits post or goes wide — this is NOT a goal

The timestamp_seconds must be when the ball enters the net,
measured from second 0 (the very start of this clip).

Return JSON only:
{
  "goal_found": true,
  "timestamp_seconds": 14.5,
  "timestamp_hms": "00:00:14",
  "confidence": "high" | "medium" | "low",
  "notes": "orange ball enters left side of net, goalie dives but misses"
}

Or if no goal occurred in the clip:
{"goal_found": false, "notes": "goalie made save" }

Return ONLY valid JSON — no markdown fences.
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def seconds_to_hms(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def check_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit(
            "Error: ffmpeg/ffprobe not found — required to split large videos.\n"
            "Install with:  brew install ffmpeg"
        )


def get_video_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def compress_video(video_path: Path) -> Path:
    """
    Transcode to 720p, drop audio — shrinks GoPro 4K footage ~10-15x.
    Saves the compressed file next to the original as <name>_720p.mp4.
    Skips compression if that file already exists.
    Returns the path to the compressed file.
    """
    check_ffmpeg()
    out_path = video_path.with_name(video_path.stem + "_720p.mp4")

    if out_path.exists():
        print(f"Found existing compressed file: {out_path.name} ({out_path.stat().st_size / 1e6:.0f} MB) — skipping compression.")
        return out_path

    original_mb = video_path.stat().st_size / 1e6
    print(f"Compressing {video_path.name} ({original_mb:.0f} MB) to 720p — this may take a few minutes...")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", "scale=-2:720",
            "-c:v", "libx264",
            "-crf", "28",
            "-preset", "fast",
            "-an",  # no audio — visual analysis only
            str(out_path),
        ],
        check=True,
    )

    compressed_mb = out_path.stat().st_size / 1e6
    print(f"Compressed to {compressed_mb:.0f} MB ({original_mb / compressed_mb:.1f}x smaller) — saved as {out_path.name}")
    return out_path


def split_video(
    video_path: Path, total_duration: float, file_size: int
) -> tuple[list[tuple[Path, float]], Path]:
    """
    Split video into segments that fit within MAX_UPLOAD_BYTES.
    Returns ([(segment_path, start_offset_seconds), ...], tmp_dir).
    Caller is responsible for deleting tmp_dir when done.
    """
    num_segments = math.ceil(file_size / MAX_UPLOAD_BYTES)
    seg_duration = total_duration / num_segments

    tmp_dir = Path(tempfile.mkdtemp(prefix="goal_detector_"))
    segments: list[tuple[Path, float]] = []

    for i in range(num_segments):
        start = i * seg_duration
        out_path = tmp_dir / f"segment_{i:03d}{video_path.suffix}"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(seg_duration),
                "-c", "copy",
                str(out_path),
            ],
            check=True, capture_output=True,
        )
        segments.append((out_path, start))

    return segments, tmp_dir


# ── Gemini API ────────────────────────────────────────────────────────────────

def upload_and_wait(client: genai.Client, video_path: Path) -> object:
    """Upload a video file and block until Gemini finishes processing it."""
    print(f"  Uploading {video_path.name} ({video_path.stat().st_size / 1e6:.1f} MB)...")
    video_file = client.files.upload(file=str(video_path))
    print("  Waiting for Gemini to process", end="", flush=True)

    while "PROCESSING" in str(video_file.state):
        print(".", end="", flush=True)
        time.sleep(5)
        video_file = client.files.get(name=video_file.name)

    print()
    if "FAILED" in str(video_file.state):
        raise RuntimeError(f"Video processing failed: {video_file.state}")

    return video_file


def _generate_with_retry(client: genai.Client, model_name: str, video_file: object, max_retries: int = 5) -> object:
    """Call generate_content with exponential backoff on 429 rate-limit errors."""
    delay = 60  # start at 60 s; the free-tier retry hint is ~40 s
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model_name,
                contents=[video_file, GOAL_DETECTION_PROMPT],
            )
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
            if attempt == max_retries:
                raise
            jitter = random.uniform(0, 10)
            wait = delay + jitter
            print(f"  Rate limit hit — retrying in {wait:.0f}s (attempt {attempt}/{max_retries})...")
            time.sleep(wait)
            delay = min(delay * 2, 300)  # cap at 5 minutes


def refine_goal_timestamp(
    client: genai.Client,
    video_path: Path,
    rough_seconds: float,
    model_name: str,
    window: int = 30,
) -> tuple[float | None, str]:
    """
    Extract a ±window second clip around rough_seconds and re-analyze for a
    precise timestamp. Returns (absolute_seconds, notes) or (None, notes) if
    the rough candidate turns out to be a false positive.
    """
    duration = get_video_duration(video_path)
    start = max(0.0, rough_seconds - window)
    end = min(duration, rough_seconds + window)
    clip_duration = end - start

    tmp_dir = Path(tempfile.mkdtemp(prefix="goal_refine_"))
    clip_path = tmp_dir / "clip.mp4"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(clip_duration),
                "-c", "copy",
                str(clip_path),
            ],
            check=True, capture_output=True,
        )

        video_file = upload_and_wait(client, clip_path)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[video_file, REFINEMENT_PROMPT],
            )
        finally:
            try:
                client.files.delete(name=video_file.name)
            except Exception:
                pass

        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)

        if result.get("goal_found") and "timestamp_seconds" in result:
            precise_abs = round(start + float(result["timestamp_seconds"]), 2)
            return precise_abs, result.get("notes", "")
        else:
            return None, result.get("notes", "not confirmed as a goal")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def analyze_segment(
    client: genai.Client,
    video_path: Path,
    model_name: str,
    time_offset: float = 0.0,
) -> list[dict]:
    """Upload, analyze, and delete one video segment. Adjusts timestamps by time_offset."""
    video_file = upload_and_wait(client, video_path)

    try:
        print(f"  Running goal detection with {model_name}...")
        response = _generate_with_retry(client, model_name, video_file)
    finally:
        try:
            client.files.delete(name=video_file.name)
        except Exception:
            pass  # Non-fatal

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    goals: list[dict] = json.loads(raw)

    # Shift timestamps to absolute video time
    if time_offset > 0:
        for goal in goals:
            goal["timestamp_seconds"] = round(goal["timestamp_seconds"] + time_offset, 2)
            goal["timestamp_hms"] = seconds_to_hms(goal["timestamp_seconds"])

    return goals


def detect_goals(client: genai.Client, video_path: Path, model_name: str, compress: bool = False, refine: bool = False) -> list[dict]:
    analysis_path = video_path
    if compress:
        analysis_path = compress_video(video_path)

    file_size = analysis_path.stat().st_size

    if file_size <= MAX_UPLOAD_BYTES:
        print("Video is within the upload size limit — processing in one pass.")
        goals = analyze_segment(client, analysis_path, model_name)
    else:
        # Still too large: split
        check_ffmpeg()
        size_gb = file_size / 1e9
        print(f"Video is {size_gb:.1f} GB — splitting into segments (limit: 1.9 GB each)...")

        total_duration = get_video_duration(analysis_path)
        segments, split_tmp = split_video(analysis_path, total_duration, file_size)

        goals = []
        try:
            for idx, (seg_path, offset) in enumerate(segments):
                print(f"\nSegment {idx + 1}/{len(segments)}  (starts at {seconds_to_hms(offset)})")
                seg_goals = analyze_segment(client, seg_path, model_name, time_offset=offset)
                print(f"  → {len(seg_goals)} goal(s) found in this segment")
                goals.extend(seg_goals)
        finally:
            shutil.rmtree(split_tmp, ignore_errors=True)

        goals.sort(key=lambda g: g["timestamp_seconds"])

    if refine and goals:
        print(f"\nRefining {len(goals)} candidate timestamp(s) with ±30s precision clips...")
        refined: list[dict] = []
        for i, goal in enumerate(goals):
            rough_hms = goal["timestamp_hms"]
            print(f"  [{i + 1}/{len(goals)}] Rough timestamp {rough_hms} — extracting clip...")
            precise_seconds, notes = refine_goal_timestamp(
                client, analysis_path, goal["timestamp_seconds"], model_name
            )
            if precise_seconds is not None:
                goal["timestamp_seconds"] = precise_seconds
                goal["timestamp_hms"] = seconds_to_hms(precise_seconds)
                goal["notes"] = notes
                goal["confidence"] = "high"
                refined.append(goal)
                print(f"       → Confirmed at {goal['timestamp_hms']}")
            else:
                print(f"       → Removed (false positive: {notes})")
        goals = refined

    return goals


# ── Output formatting ─────────────────────────────────────────────────────────

def format_results(goals: list[dict], video_name: str) -> str:
    lines = [f"Goal timestamps for: {video_name}", "=" * 50]

    if not goals:
        lines.append("No goals detected.")
        return "\n".join(lines)

    lines.append(f"Total goals detected: {len(goals)}\n")
    for i, goal in enumerate(goals, 1):
        conf = goal.get("confidence", "?")
        hms = goal.get("timestamp_hms", "??:??:??")
        notes = goal.get("notes", "")
        lines.append(f"Goal {i:>2}  [{hms}]  ({conf} confidence)")
        if notes:
            lines.append(f"         {notes}")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Detect hockey goals in a video using Gemini.")
    parser.add_argument("video", help="Path to the video file (MP4, MOV, etc.)")
    parser.add_argument(
        "--output", "-o",
        help="Save results to this file. Defaults to <video_name>_goals.txt",
    )
    parser.add_argument(
        "--model", "-m",
        default="gemini-2.5-flash",
        choices=[
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-pro-preview-05-06",
        ],
        help="Gemini model to use (default: gemini-2.5-flash)",
    )
    parser.add_argument(
        "--compress", "-c",
        action="store_true",
        help="Transcode to 720p before uploading (recommended for GoPro 4K footage — ~10x smaller, much faster)",
    )
    parser.add_argument(
        "--refine", "-r",
        action="store_true",
        help="Run a second pass on each candidate: extracts a ±30s clip and re-analyzes for precise timestamp and false-positive removal",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Also save raw JSON output alongside the text file",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit(
            "Error: GEMINI_API_KEY not set.\n"
            "Add it to a .env file or run:  export GEMINI_API_KEY=your_key\n"
            "Get a free key at: https://aistudio.google.com/app/apikey"
        )

    client = genai.Client(api_key=api_key)

    video_path = Path(args.video).resolve()
    if not video_path.exists():
        sys.exit(f"Error: Video file not found: {video_path}")

    try:
        goals = detect_goals(client, video_path, args.model, compress=args.compress, refine=args.refine)
    except json.JSONDecodeError as e:
        sys.exit(f"Error: Gemini returned invalid JSON — {e}\nTry running again; this is usually transient.")
    except Exception as e:
        sys.exit(f"Error: {e}")

    result_text = format_results(goals, video_path.name)
    print("\n" + result_text)

    out_path = (
        Path(args.output)
        if args.output
        else video_path.with_name(video_path.stem + "_goals.txt")
    )
    out_path.write_text(result_text, encoding="utf-8")
    print(f"\nSaved to: {out_path}")

    # Optionally save JSON
    if args.json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(json.dumps(goals, indent=2), encoding="utf-8")
        print(f"JSON saved to: {json_path}")


if __name__ == "__main__":
    main()
