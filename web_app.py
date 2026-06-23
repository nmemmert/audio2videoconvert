#!/usr/bin/env python3
"""
Web interface for the podcast video generator.
Run with: python3 web_app.py
Then open http://<server-ip>:8000 in a browser.
"""

import json
import shutil
import tempfile
import threading
import time
import uuid
import queue
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file

import render_engine as eng

HERE = Path(__file__).resolve().parent
PRESETS_DIR = HERE / "presets"
PRESETS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = HERE / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
ART_DIR = HERE / "art"
ART_DIR.mkdir(exist_ok=True)
JOBS_FILE = HERE / "jobs.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE = queue.Queue()


def _load_jobs():
    if JOBS_FILE.exists():
        try:
            data = json.loads(JOBS_FILE.read_text())
        except Exception:
            return
        for job_id, job in data.items():
            if job.get("status") in ("running", "queued"):
                job["status"] = "failed"
                job["stage"] = "Interrupted (server restarted)"
            JOBS[job_id] = job

def _save_jobs():
    with JOBS_LOCK:
        slim = {}
        for job_id, job in JOBS.items():
            j = dict(job)
            j["log"] = j.get("log", [])[-30:]
            slim[job_id] = j
        try:
            JOBS_FILE.write_text(json.dumps(slim, indent=2))
        except Exception:
            pass

_load_jobs()


def list_presets():
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))

def list_art():
    exts = {".jpg", ".jpeg", ".png"}
    return sorted(p.name for p in ART_DIR.iterdir()
                  if p.suffix.lower() in exts and not p.name.startswith("wm_"))

def list_qr():
    exts = {".jpg", ".jpeg", ".png"}
    return sorted(p.name for p in ART_DIR.iterdir()
                  if p.suffix.lower() in exts)


def _job_worker():
    while True:
        job_id = JOB_QUEUE.get()
        try:
            _run_job(job_id)
        except Exception as e:
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["stage"] = "Error"
                    JOBS[job_id]["log"].append(f"Unexpected error: {e}")
            _save_jobs()

threading.Thread(target=_job_worker, daemon=True).start()


def _run_job(job_id):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["stage"] = "Starting"
        job["progress"] = 0.0
    _save_jobs()

    settings      = job["settings"]
    audio_path    = Path(job["audio_path"])
    art_path      = Path(job["art_path"]) if job.get("art_path") else None
    output_path   = Path(job["output_path"])
    script_path   = Path(job["script_path"]) if job.get("script_path") else None
    title         = job.get("title", "")
    preview_secs  = job.get("preview_seconds")
    outline_titles = job.get("outline_titles")

    def progress_cb(stage, frac):
        with JOBS_LOCK:
            job["stage"] = stage
            job["progress"] = frac
        _save_jobs()

    def log_cb(msg):
        with JOBS_LOCK:
            job["log"].append(msg)
        _save_jobs()

    ok = eng.render_job(
        settings, audio_path, output_path,
        art_path=str(art_path) if art_path else None,
        title=title, script_path=script_path,
        progress_cb=progress_cb, log_cb=log_cb,
        preview_seconds=preview_secs, outline_titles=outline_titles,
    )
    with JOBS_LOCK:
        job["status"] = "done" if ok else "failed"
        if ok:
            job["output"] = str(output_path)
            job["progress"] = 1.0
            job["stage"] = "Done"
    _save_jobs()


@app.route("/")
def index():
    return render_template(
        "index.html",
        presets=list_presets(),
        art_files=list_art(),
        qr_files=list_qr(),
        defaults=eng.DEFAULTS,
        bg_styles=eng.BG_STYLES,
        caption_styles=eng.CAPTION_STYLES,
        outline_styles=eng.OUTLINE_STYLES,
        available_fonts=eng.AVAILABLE_FONTS,
    )


@app.route("/preset/<name>")
def get_preset(name):
    fname = PRESETS_DIR / f"{secure_filename(name)}.json"
    if not fname.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(fname.read_text()))

@app.route("/preset/<name>", methods=["POST"])
def save_preset(name):
    settings = request.get_json()
    fname = PRESETS_DIR / f"{secure_filename(name)}.json"
    fname.write_text(json.dumps(settings, indent=2))
    return jsonify({"ok": True})

@app.route("/preset/<name>", methods=["DELETE"])
def delete_preset(name):
    fname = PRESETS_DIR / f"{secure_filename(name)}.json"
    fname.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/upload/art", methods=["POST"])
def upload_art():
    f = request.files["file"]
    fname = secure_filename(f.filename)
    f.save(ART_DIR / fname)
    return jsonify({"ok": True, "filename": fname})

@app.route("/upload/qr", methods=["POST"])
def upload_qr():
    f = request.files["file"]
    fname = secure_filename(f.filename)
    f.save(ART_DIR / fname)
    return jsonify({"ok": True, "filename": fname, "path": str(ART_DIR / fname)})


@app.route("/extract_outline", methods=["POST"])
def extract_outline():
    f = request.files["script"]
    tmp = Path(tempfile.gettempdir()) / f"vbvn_outline_{uuid.uuid4().hex[:8]}.docx"
    f.save(tmp)
    try:
        points = eng.extract_outline_from_script(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return jsonify({"titles": [t for t, _ in points]})

@app.route("/extract_questions", methods=["POST"])
def extract_questions():
    f = request.files["script"]
    tmp = Path(tempfile.gettempdir()) / f"vbvn_q_{uuid.uuid4().hex[:8]}.docx"
    f.save(tmp)
    try:
        questions = eng.extract_discussion_questions(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return jsonify({"questions": [q for q, _ in questions]})


@app.route("/render", methods=["POST"])
def render():
    settings       = json.loads(request.form["settings"])
    title          = request.form.get("title", "").strip()
    preview        = request.form.get("preview", "").lower() in ("1", "true", "yes")
    outline_titles = json.loads(request.form["outline_titles"]) if request.form.get("outline_titles") else None

    audio_file  = request.files["audio"]
    audio_name  = secure_filename(audio_file.filename)
    job_id      = uuid.uuid4().hex[:12]
    job_dir     = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path  = job_dir / audio_name
    audio_file.save(audio_path)

    # Art
    art_path = None
    if settings.get("art_enabled", True):
        art_name = settings.get("art", "")
        if art_name:
            candidate = ART_DIR / art_name
            if candidate.exists():
                art_path = candidate
            else:
                return jsonify({"error": f"Art image not found: {art_name}"}), 400

    # QR code — resolve path
    qr_path_val = settings.get("qr_path", "")
    if qr_path_val and not Path(qr_path_val).is_absolute():
        candidate = ART_DIR / qr_path_val
        if candidate.exists():
            settings["qr_path"] = str(candidate)

    # Script
    script_path = None
    if "script" in request.files:
        sf = request.files["script"]
        if sf and sf.filename:
            script_path = job_dir / secure_filename(sf.filename)
            sf.save(script_path)

    out_name    = Path(audio_name).stem + (".preview.mp4" if preview else ".mp4")
    output_path = OUTPUT_DIR / job_id / out_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _enqueue_job(job_id, settings, audio_path, art_path, output_path,
                 title=title, script_path=script_path,
                 audio_name=audio_name, preview=preview,
                 outline_titles=outline_titles)
    return jsonify({"job_id": job_id})


def _enqueue_job(job_id, settings, audio_path, art_path, output_path,
                 title="", script_path=None, audio_name="",
                 preview=False, outline_titles=None):
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status":       "queued",
            "stage":        "Queued" + (f" (position {JOB_QUEUE.qsize() + 1})" if JOB_QUEUE.qsize() else ""),
            "progress":     0.0,
            "log":          [],
            "output":       None,
            "output_name":  output_path.name,
            "output_path":  str(output_path),
            "audio_path":   str(audio_path),
            "art_path":     str(art_path) if art_path else None,
            "script_path":  str(script_path) if script_path else None,
            "settings":     settings,
            "title":        title,
            "audio_name":   audio_name,
            "preview":      preview,
            "preview_seconds": 12 if preview else None,
            "outline_titles": outline_titles,
            "created":      time.time(),
        }
    _save_jobs()
    JOB_QUEUE.put(job_id)


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "status":   job["status"],
            "stage":    job["stage"],
            "progress": job["progress"],
            "log":      job["log"][-30:],
        })


@app.route("/history")
def history():
    with JOBS_LOCK:
        items = []
        for job_id, job in JOBS.items():
            items.append({
                "job_id":      job_id,
                "status":      job["status"],
                "stage":       job.get("stage", ""),
                "progress":    job.get("progress", 0),
                "title":       job.get("title") or job.get("audio_name", ""),
                "preview":     job.get("preview", False),
                "created":     job.get("created", 0),
                "downloadable": bool(job.get("output")),
            })
        items.sort(key=lambda j: j["created"], reverse=True)
    return jsonify(items)


@app.route("/rerun/<job_id>", methods=["POST"])
def rerun(job_id):
    with JOBS_LOCK:
        old = JOBS.get(job_id)
        if not old:
            return jsonify({"error": "not found"}), 404
        old = dict(old)

    audio_path = Path(old["audio_path"])
    if not audio_path.exists():
        return jsonify({"error": "Original audio file no longer available"}), 400

    new_id  = uuid.uuid4().hex[:12]
    new_dir = UPLOAD_DIR / new_id
    new_dir.mkdir(parents=True, exist_ok=True)
    new_audio = new_dir / audio_path.name
    shutil.copy(audio_path, new_audio)

    new_script = None
    if old.get("script_path") and Path(old["script_path"]).exists():
        src = Path(old["script_path"])
        new_script = new_dir / src.name
        shutil.copy(src, new_script)

    out_path = OUTPUT_DIR / new_id / Path(old["output_name"]).name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    art_path = Path(old["art_path"]) if old.get("art_path") else None
    _enqueue_job(new_id, old["settings"], new_audio, art_path, out_path,
                 title=old.get("title", ""), script_path=new_script,
                 audio_name=old.get("audio_name", ""), preview=old.get("preview", False),
                 outline_titles=old.get("outline_titles"))
    return jsonify({"job_id": new_id})


@app.route("/delete/<job_id>", methods=["POST"])
def delete_job(job_id):
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["status"] in ("running", "queued"):
        with JOBS_LOCK:
            JOBS[job_id] = job
        return jsonify({"error": "Cannot delete a running or queued job"}), 400
    shutil.rmtree(UPLOAD_DIR / job_id, ignore_errors=True)
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    _save_jobs()
    return jsonify({"ok": True})


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or not job.get("output"):
            return "Not ready", 404
        path = job["output"]
        name = job["output_name"]
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/art/<path:filename>")
def serve_art(filename):
    return send_file(ART_DIR / secure_filename(filename))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
