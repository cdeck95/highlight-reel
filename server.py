#!/usr/bin/env python3
"""
server.py — local web UI for marking goal timestamps and generating a montage.

Usage:
    pip install flask
    python3 server.py
    # then open http://localhost:5000
"""

import os
import json
import re
import sys
import uuid
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from flask import Flask, render_template, request, jsonify, send_file, abort
except ImportError:
    sys.exit("ERROR: Flask not installed.  Run:  pip install flask")

import make_montage as mm

# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # no upload size limit (local tool)

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
PRECLIP_DIR = Path("preclips")
for _d in (UPLOAD_DIR, OUTPUT_DIR, PRECLIP_DIR):
    _d.mkdir(exist_ok=True)

SESSION_FILE = Path("session.json")

_uploads: dict  = {}  # upload_id  → status dict
_jobs: dict     = {}  # job_id     → status dict
_preclips: dict = {}  # preclip_key → status dict

ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".ts"}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _valid_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _preclip_key(uid: str, ts: float) -> str:
    """Stable filesystem-safe key for a (upload, timestamp) pair."""
    return f"{uid}_{ts:.3f}"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Upload & transcode
# ---------------------------------------------------------------------------

@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    f = request.files["video"]
    ext = Path(f.filename or "").suffix.lower()
    if ext not in ALLOWED_VIDEO_EXT:
        return jsonify({"error": f"unsupported file type: {ext or '(none)'}"}), 400

    uid = str(uuid.uuid4())
    orig_path = UPLOAD_DIR / f"orig_{uid}.mp4"
    f.save(str(orig_path))

    skip_transcode = request.form.get("skip_transcode") == "true"
    if skip_transcode:
        try:
            duration = mm._clip_duration(str(orig_path))
        except Exception:
            duration = None
        _uploads[uid] = {
            "status": "ready",
            "preview_url": f"/preview/{uid}",
            "duration": duration,
            "use_orig": True,
        }
    else:
        _uploads[uid] = {"status": "transcoding"}
        threading.Thread(target=_transcode, args=(uid, orig_path), daemon=True).start()
    return jsonify({"upload_id": uid})


def _transcode(uid: str, orig_path: Path) -> None:
    """Re-encode to 720p H.264 with faststart for smooth browser playback."""
    import subprocess
    preview_path = UPLOAD_DIR / f"preview_{uid}.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(orig_path),
            "-vf", "scale=-2:720",
            "-c:v", "libx264", "-preset", "fast", "-crf", "26",
            "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
            "-movflags", "+faststart",
            str(preview_path),
        ], check=True, capture_output=True)

        duration = mm._clip_duration(str(orig_path))
        _uploads[uid] = {
            "status": "ready",
            "preview_url": f"/preview/{uid}",
            "duration": duration,
        }
    except Exception as e:
        _uploads[uid] = {"status": "error", "error": str(e)}


@app.route("/session/clear", methods=["POST"])
def session_clear():
    """Delete all uploaded/output files and reset in-memory state."""
    for uid in list(_uploads):
        for pat in (f"orig_{uid}.mp4", f"preview_{uid}.mp4"):
            (UPLOAD_DIR / pat).unlink(missing_ok=True)
    _uploads.clear()
    for f in OUTPUT_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    _jobs.clear()
    for f in PRECLIP_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    _preclips.clear()
    SESSION_FILE.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/session/save", methods=["POST"])
def session_save():
    data = request.get_json(silent=True) or {}
    clean = []
    for v in data.get("videos", []):
        uid = v.get("uploadId", "")
        if uid and not _valid_uuid(uid):
            continue
        clean_ts = []
        for entry in v.get("timestamps", []):
            if isinstance(entry, (int, float)):
                clean_ts.append({"t": float(entry), "type": "goal"})
            elif isinstance(entry, dict):
                t_val = entry.get("t")
                clip_type = entry.get("type", "goal")
                if isinstance(t_val, (int, float)) and clip_type in ("goal", "save"):
                    clean_ts.append({"t": float(t_val), "type": clip_type})
        clean.append({
            "uploadId": uid,
            "name": str(v.get("name", ""))[:200],
            "timestamps": clean_ts,
        })
    clip_order = data.get("clipOrder")
    clean_order = None
    if isinstance(clip_order, list):
        clean_order = []
        for entry in clip_order:
            if isinstance(entry, dict):
                t_val = entry.get("t")
                clip_type = entry.get("type", "goal")
                uid = entry.get("uploadId", "")
                if isinstance(t_val, (int, float)) and clip_type in ("goal", "save"):
                    clean_order.append({"uploadId": uid, "t": float(t_val), "type": clip_type})
    saved_settings = data.get("settings")
    clean_settings = None
    if isinstance(saved_settings, dict):
        try:
            s_pre = max(0.5, min(float(saved_settings.get("pre", 4.5)), 30.0))
            s_post = max(0.5, min(float(saved_settings.get("post", 2.5)), 30.0))
            s_trans = max(0.0, min(float(saved_settings.get("transition", 0.5)), 5.0))
            clean_settings = {"pre": s_pre, "post": s_post, "transition": s_trans}
        except (TypeError, ValueError):
            pass
    SESSION_FILE.write_text(json.dumps({"videos": clean, "clipOrder": clean_order, "settings": clean_settings}))
    return jsonify({"ok": True})


@app.route("/session/load")
def session_load():
    if not SESSION_FILE.exists():
        return jsonify({"videos": []})
    try:
        data = json.loads(SESSION_FILE.read_text())
    except Exception:
        return jsonify({"videos": []})
    result = []
    for v in data.get("videos", []):
        uid = v.get("uploadId", "")
        if not uid or not _valid_uuid(uid):
            result.append({**v, "available": False})
            continue
        # Re-hydrate _uploads from disk if the server was restarted
        if uid not in _uploads:
            orig = UPLOAD_DIR / f"orig_{uid}.mp4"
            preview = UPLOAD_DIR / f"preview_{uid}.mp4"
            use_orig = (not preview.exists()) and orig.exists()
            src = orig if use_orig else preview
            if src.exists():
                try:
                    duration = mm._clip_duration(str(orig)) if orig.exists() else None
                except Exception:
                    duration = None
                entry = {"status": "ready", "preview_url": f"/preview/{uid}", "duration": duration}
                if use_orig:
                    entry["use_orig"] = True
                _uploads[uid] = entry
        available = _uploads.get(uid, {}).get("status") == "ready"
        result.append({**v, "available": available})
    return jsonify({"videos": result, "clipOrder": data.get("clipOrder"), "settings": data.get("settings")})


@app.route("/upload/status/<uid>")
def upload_status(uid: str):
    if not _valid_uuid(uid):
        abort(400)
    return jsonify(_uploads.get(uid, {"status": "not_found"}))


@app.route("/preview/<uid>")
def preview(uid: str):
    if not _valid_uuid(uid):
        abort(400)
    # If transcoding was skipped, serve the original file directly
    if _uploads.get(uid, {}).get("use_orig"):
        path = UPLOAD_DIR / f"orig_{uid}.mp4"
    else:
        path = UPLOAD_DIR / f"preview_{uid}.mp4"
    if not path.exists():
        abort(404)
    # conditional=True enables HTTP Range support so the browser can seek
    return send_file(str(path), mimetype="video/mp4", conditional=True)


# ---------------------------------------------------------------------------
# Pre-clip extraction
# ---------------------------------------------------------------------------

@app.route("/preclip", methods=["POST"])
def preclip():
    """Start background extraction of a single clip so Generate is instant."""
    data = request.get_json(force=True)
    uid = data.get("upload_id", "")
    ts = data.get("timestamp")
    if not _valid_uuid(uid) or not isinstance(ts, (int, float)):
        return jsonify({"error": "invalid params"}), 400
    if _uploads.get(uid, {}).get("status") != "ready":
        return jsonify({"skipped": True})  # upload not ready yet
    orig_path = UPLOAD_DIR / f"orig_{uid}.mp4"
    if not orig_path.exists():
        return jsonify({"error": "source not found"}), 404

    pre  = max(0.5, min(float(data.get("pre",  4.5)), 30.0))
    post = max(0.5, min(float(data.get("post", 2.5)), 30.0))

    key = _preclip_key(uid, float(ts))
    existing = _preclips.get(key, {})
    # Skip if already in-flight, or cached with the same pre/post dimensions
    if existing.get("status") == "extracting":
        return jsonify({"key": key})
    if (existing.get("status") == "ready"
            and existing.get("pre") == pre
            and existing.get("post") == post):
        return jsonify({"key": key})

    ts_f = float(ts)
    start = max(0.0, ts_f - pre)
    duration = (ts_f + post) - start
    out_path = PRECLIP_DIR / f"{key}.mp4"

    _preclips[key] = {"status": "extracting"}
    threading.Thread(
        target=_do_preclip,
        args=(key, str(orig_path), start, duration, str(out_path), pre, post),
        daemon=True,
    ).start()
    return jsonify({"key": key})


def _do_preclip(key: str, video_path: str, start: float, duration: float, out_path: str, pre: float, post: float) -> None:
    try:
        mm.extract_clip(video_path, start, duration, out_path)
        _preclips[key] = {"status": "ready", "path": out_path, "pre": pre, "post": post}
    except Exception as e:
        _preclips[key] = {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Montage generation
# ---------------------------------------------------------------------------

@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True)
    # Flat ordered format: [{upload_id, timestamp}, ...]
    clip_list = data.get("clips", [])

    if not clip_list:
        return jsonify({"error": "no clips provided"}), 400

    # Each entry becomes its own single-ts source to preserve custom clip order.
    sources = []
    for entry in clip_list:
        uid = entry.get("upload_id", "")
        ts = entry.get("timestamp")
        if not _valid_uuid(uid):
            return jsonify({"error": f"invalid upload_id: {uid}"}), 400
        if not isinstance(ts, (int, float)):
            return jsonify({"error": "invalid timestamp"}), 400
        if _uploads.get(uid, {}).get("status") != "ready":
            return jsonify({"error": f"upload {uid} not ready"}), 400
        orig_path = UPLOAD_DIR / f"orig_{uid}.mp4"
        if not orig_path.exists():
            return jsonify({"error": f"original video not found for {uid}"}), 404
        sources.append((uid, str(orig_path), [float(ts)]))

    pre        = max(0.5, min(float(data.get("pre",        4.5)), 30.0))
    post       = max(0.5, min(float(data.get("post",       2.5)), 30.0))
    transition = max(0.0, min(float(data.get("transition", 0.5)),  5.0))

    job_id = str(uuid.uuid4())
    output_path = OUTPUT_DIR / f"montage_{job_id}.mp4"
    _jobs[job_id] = {"status": "running", "progress": "Starting…", "pct": 0, "cancel": False}

    threading.Thread(
        target=_run_montage,
        args=(job_id, sources, str(output_path)),
        kwargs={"pre": pre, "post": post, "transition": transition},
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


def _run_montage(job_id: str, sources: list, output_path: str,
                 pre: float = 4.5, post: float = 2.5, transition: float = 0.5) -> None:
    """sources = [(uid, video_path, [timestamp_floats]), ...]"""
    try:
        all_parsed = [
            (uid, vpath, [mm.parse_timestamp(str(t)) for t in raw_ts])
            for uid, vpath, raw_ts in sources
        ]
        total = sum(len(ts) for _, _, ts in all_parsed)

        # Build flat task list so extraction can be parallelised.
        tasks = []
        clip_num = 0
        for uid, video_path, parsed in all_parsed:
            for ts in parsed:
                clip_num += 1
                start = max(0.0, ts - pre)
                duration = (ts + post) - start
                tasks.append((clip_num, video_path, start, duration, _preclip_key(uid, ts)))

        with tempfile.TemporaryDirectory(prefix="montage_") as tmpdir:
            clips_by_num: dict = {}
            done_lock = threading.Lock()
            done_count = [0]

            def _extract(task):
                if _jobs[job_id].get("cancel"):
                    return task[0], None
                n, video_path, start, duration, key = task
                # Re-use pre-extracted clip only if dimensions match
                pre_info = _preclips.get(key, {})
                if (pre_info.get("status") == "ready"
                        and pre_info.get("pre") == pre
                        and pre_info.get("post") == post):
                    cached = pre_info.get("path", "")
                    if cached and Path(cached).exists():
                        with done_lock:
                            done_count[0] += 1
                            _jobs[job_id]["progress"] = (
                                f"Assembling clip {done_count[0]} of {total}…"
                            )
                            _jobs[job_id]["pct"] = int(done_count[0] / total * 85)
                        return n, cached
                clip_path = os.path.join(tmpdir, f"clip_{n:04d}.mp4")
                mm.extract_clip(video_path, start, duration, clip_path)
                with done_lock:
                    done_count[0] += 1
                    _jobs[job_id]["progress"] = (
                        f"Extracting clip {done_count[0]} of {total}…"
                    )
                    _jobs[job_id]["pct"] = int(done_count[0] / total * 85)
                return n, clip_path

            max_workers = min(os.cpu_count() or 2, 4)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for n, clip_path in pool.map(_extract, tasks):
                    if clip_path is not None:
                        clips_by_num[n] = clip_path

            if _jobs[job_id].get("cancel"):
                _jobs[job_id].update({"status": "cancelled"})
                Path(output_path).unlink(missing_ok=True)
                return

            clips = [clips_by_num[n] for n in sorted(clips_by_num)]

            _jobs[job_id]["progress"] = "Assembling montage…"
            _jobs[job_id]["pct"] = 92
            if transition == 0 or len(clips) == 1:
                mm.build_montage_hard_cut(clips, output_path)
            else:
                mm.build_montage_xfade(clips, output_path, transition)

        _jobs[job_id].update({
            "status": "done",
            "pct": 100,
            "download_url": f"/download/montage_{job_id}.mp4",
        })
    except Exception as e:
        _jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/generate/status/<job_id>")
def generate_status(job_id: str):
    if not _valid_uuid(job_id):
        abort(400)
    return jsonify(_jobs.get(job_id, {"status": "not_found"}))


@app.route("/generate/cancel/<job_id>", methods=["POST"])
def generate_cancel(job_id: str):
    if not _valid_uuid(job_id):
        abort(400)
    job = _jobs.get(job_id)
    if not job or job.get("status") != "running":
        return jsonify({"ok": False})
    job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/download/<filename>")
def download_file(filename: str):
    name = Path(filename).name  # strip any path components
    if not name.startswith("montage_") or not name.endswith(".mp4"):
        abort(400)
    path = OUTPUT_DIR / name
    if not path.exists():
        abort(404)
    return send_file(str(path), as_attachment=True, download_name="montage.mp4")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import signal, socket
    PORT = 5001
    # Free the port if a previous instance is still holding it
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex(("127.0.0.1", PORT)) == 0:
            import subprocess as _sp
            pids = _sp.run(
                ["lsof", "-ti", f":{PORT}"], capture_output=True, text=True
            ).stdout.split()
            for _pid in pids:
                try:
                    os.kill(int(_pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            import time; time.sleep(0.5)
    print(f"\n  Montage Maker  →  http://localhost:{PORT}\n")
    app.run(debug=False, port=PORT, threaded=True)
