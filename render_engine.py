"""
Shared render pipeline for the podcast video generator.
Call render_job(settings, audio_path, output_path, ...) to produce a video.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from string import Template

try:
    from docx import Document
except ImportError:
    Document = None

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    # Art
    "art_enabled": True,
    "art": "podcast-art.jpeg",
    "art_size": 460,
    "art_x_offset": 0,
    # Glow
    "glow_enabled": True,
    "glow_color": "#c9a84c",
    "glow_sigma": 40,
    # Background
    "bg_style": "solid",
    "bg_color": "#111111",
    "bg_color2": "#1a1a2e",
    # Waveform animation
    "waveform_enabled": True,
    "waveform_color": "#c9a84c",
    "waveform_height": 100,
    # Captions
    "font_name": "Georgia",
    "font_size": 64,
    "words_per_chunk": 2,
    "caption_color": "&H50D8EAF0",
    "caption_highlight": "&H004CA8C9",
    "caption_back": "&HBF000000",
    "caption_style": "karaoke",
    # Outline
    "outline_enabled": False,
    "outline_style": "sidebar",  # "sidebar" or "ticker"
    "outline_color": "#ffffff",
    "outline_font": "Georgia",
    "outline_font_size": 32,
    # Title overlay
    "title_enabled": False,
    "title_font": "Georgia",
    "title_font_size": 48,
    "title_color": "white",
    # Output
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "output_dir": "",
    # Ollama (outline generation fallback)
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1",
}

BG_STYLES = ["solid", "gradient"]
CAPTION_STYLES = ["karaoke", "fade", "bounce"]
OUTLINE_STYLES = ["sidebar", "ticker"]

PYTHON_VENV = Path.home() / "whisper-env" / "bin" / "python3"


def find_python():
    if PYTHON_VENV.exists():
        return str(PYTHON_VENV)
    return sys.executable


def find_ffmpeg(name="ffmpeg"):
    p = shutil.which(name)
    return p or name


def detect_glow_color(art_path, fallback="#c9a84c"):
    try:
        from PIL import Image
        img = Image.open(art_path).convert("RGB")
        w, h = img.size
        m = max(2, min(w, h) // 50)
        corners = [(m, m), (w - m, m), (m, h - m), (w - m, h - m)]
        r, g, b = 0, 0, 0
        for x, y in corners:
            pr, pg, pb = img.getpixel((x, y))
            r += pr; g += pg; b += pb
        n = len(corners)
        r, g, b = r // n, g // n, b // n
        if max(r, g, b) < 24:
            return fallback
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return fallback


def hex_to_ass(hex_color, alpha="00"):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        hex_color = "ffffff"
    rr, gg, bb = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H{alpha}{bb}{gg}{rr}".upper()


# ── Outline / transcript helpers ────────────────────────────────────────────

_HEADER_RE = re.compile(r"^(?:[^A-Za-z0-9]+\s*)?([A-Z][A-Za-z0-9 ,:;'\-—]+)$")
_SPEAKER_RE = re.compile(r"^[A-Z][A-Z ]*:\s*")


def _normalize(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_outline_from_script(docx_path):
    if Document is None:
        return []
    doc = Document(str(docx_path))
    paragraphs = [p.text.strip() for p in doc.paragraphs]
    points = []
    i = 0
    while i < len(paragraphs):
        text = paragraphs[i]
        m = _HEADER_RE.match(text) if text else None
        if m:
            title = m.group(1).strip()
            anchor = ""
            for j in range(i + 1, len(paragraphs)):
                cand = paragraphs[j].strip()
                if not cand:
                    continue
                if _HEADER_RE.match(cand):
                    break
                if cand.startswith("[") and cand.endswith("]"):
                    continue
                if set(cand) <= set("— ✦-—  "):
                    continue
                anchor = _SPEAKER_RE.sub("", cand)
                break
            points.append((title, anchor))
        i += 1
    return points


def align_outline_to_transcript(points, segments):
    if not points or not segments:
        return []
    seg_norms = [_normalize(seg["text"]) for seg in segments]
    offsets, full_parts, pos = [], [], 0
    for norm in seg_norms:
        offsets.append(pos)
        full_parts.append(norm)
        pos += len(norm) + 1
    full_text = " ".join(full_parts)
    result, search_from, last_time = [], 0, 0.0
    for title, anchor in points:
        anchor_norm = _normalize(anchor)
        words = anchor_norm.split()
        idx = -1
        for n in (6, 4, 3):
            if len(words) >= n:
                needle = " ".join(words[:n])
                idx = full_text.find(needle, search_from)
                if idx != -1:
                    break
        if idx == -1:
            continue
        seg_idx = 0
        for k, off in enumerate(offsets):
            if off <= idx:
                seg_idx = k
            else:
                break
        t = max(segments[seg_idx]["start"], last_time)
        result.append((t, title))
        last_time = t
        search_from = idx + 1
    return result


def generate_outline(segments, s, log_cb=None):
    import urllib.request

    def log(msg):
        if log_cb:
            log_cb(msg)

    transcript_lines = "\n".join(f"[{seg['start']:.1f}s] {seg['text']}" for seg in segments)
    prompt = (
        "You are given a transcript of a podcast episode, where each line is "
        "prefixed with the timestamp (in seconds) at which it was spoken.\n\n"
        "Produce a short outline of the main points/topics discussed, in order. "
        "For each point, pick the timestamp where that topic begins.\n\n"
        "Respond ONLY with a JSON array of objects, each with keys \"time\" (number, "
        "seconds) and \"text\" (string, max ~6 words). Aim for 4-8 points total.\n\n"
        f"Transcript:\n{transcript_lines}\n"
    )
    url = s.get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = json.dumps({
        "model": s.get("ollama_model", "llama3.1"),
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        points = json.loads(data.get("response", ""))
        result = [(float(p["time"]), str(p["text"]).strip()) for p in points if p.get("text")]
        result.sort(key=lambda x: x[0])
        return result
    except Exception as e:
        log(f"Outline generation failed: {e}")
        return []


def _ass_fmt(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    sec = t % 60
    return f"{h:d}:{m:02d}:{sec:05.2f}"


def build_sidebar_ass(points, s, duration_f, out_path, start_y=None, end_y=None, art_x=None):
    """Left-side outline confined to the sidebar column.

    Text wraps at the sidebar edge (art_x - padding) so it never overlaps the art.
    Font size is chosen dynamically so all wrapped lines fill start_y → end_y.

    start_y  — top of the outline region (below captions)
    end_y    — bottom of the outline region (above waveform)
    art_x    — left edge of the art image; text stays left of this
    """
    import textwrap as _textwrap

    color = hex_to_ass(s.get("outline_color", "#ffffff"))
    font = s.get("outline_font", "Georgia")
    wave_h = s.get("waveform_height", 100) if s.get("waveform_enabled") else 0
    height = s["height"]

    if start_y is None:
        start_y = 24
    if end_y is None:
        end_y = height - wave_h - 12
    if art_x is None:
        art_x = s["width"] // 2

    available_h = max(1, end_y - start_y)
    # Usable pixel width for text: from x=24 to art_x - 24 (padding each side)
    text_px_w = max(80, art_x - 48)

    # Find the largest font size (between 18 and 54) where all wrapped lines fit
    # Character width approximation: size * 0.58 pixels per char (proportional font)
    def _wrap_all(size):
        chars = max(4, int(text_px_w / (size * 0.58)))
        wrapped = []
        for _, text in points:
            escaped = text.replace("\\", "\\\\").replace("{", "").replace("}", "")
            wrapped.append(_textwrap.wrap(escaped, chars) or [escaped])
        return wrapped

    size = 18
    for candidate in range(54, 17, -1):
        wrapped_items = _wrap_all(candidate)
        total_lines = sum(len(w) for w in wrapped_items)
        line_px = candidate + 8  # font size + inter-line gap
        if total_lines * line_px <= available_h:
            size = candidate
            wrapped_items_final = wrapped_items
            total_lines_final = total_lines
            break
    else:
        wrapped_items_final = _wrap_all(size)
        total_lines_final = sum(len(w) for w in wrapped_items_final)

    # Distribute total vertical space evenly across items (not individual lines)
    n_items = len(points)
    item_slot = available_h // max(n_items, 1)  # pixels per item slot
    line_h = size + 8  # pixels per wrapped line within a slot

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['width']}
PlayResY: {s['height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{color},{color},&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,2,0,7,24,24,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    dim_hex = "888888"
    for i, (start, _) in enumerate(points):
        if start >= duration_f:
            continue
        wrapped = wrapped_items_final[i]
        ass_text = r"\N".join(wrapped)
        # Centre the item's lines vertically within its slot
        slot_top = start_y + i * item_slot
        text_block_h = len(wrapped) * line_h
        y = slot_top + (item_slot - text_block_h) // 2
        pos = f"\\pos(24,{y})\\an7"
        next_start = points[i + 1][0] if i + 1 < len(points) else duration_f
        active_end = min(next_start, duration_f)
        if active_end > start:
            tag = f"{{{pos}\\fad(400,0)}}"
            lines.append(f"Dialogue: 0,{_ass_fmt(start)},{_ass_fmt(active_end)},Default,,0,0,0,,{tag}{ass_text}")
        if active_end < duration_f:
            tag = f"{{{pos}\\c&H{dim_hex}&}}"
            lines.append(f"Dialogue: 0,{_ass_fmt(active_end)},{_ass_fmt(duration_f)},Default,,0,0,0,,{tag}{ass_text}")
    out_path.write_text("\n".join(lines))


def build_ticker_ass(points, s, duration_f, out_path):
    """Bottom chapter ticker: show current chapter name in lower-left."""
    color = hex_to_ass(s.get("outline_color", "#ffffff"))
    font = s.get("outline_font", "Georgia")
    size = s.get("outline_font_size", 32)
    wave_h = s.get("waveform_height", 100) if s.get("waveform_enabled") else 0
    y = s["height"] - wave_h - size - 16

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['width']}
PlayResY: {s['height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{color},{color},&H00000000,&HA0000000,0,0,0,0,100,100,1,0,4,2,0,1,30,30,{y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for i, (start, text) in enumerate(points):
        if start >= duration_f:
            continue
        end = points[i + 1][0] if i + 1 < len(points) else duration_f
        escaped = text.replace("\\", "\\\\").replace("{", "").replace("}", "")
        tag = "{\\fad(300,300)}"
        lines.append(f"Dialogue: 0,{_ass_fmt(start)},{_ass_fmt(end)},Default,,0,0,0,,{tag}{escaped}")
    out_path.write_text("\n".join(lines))


def build_bg_filter(s, fps):
    width, height, bg = s["width"], s["height"], s["bg_color"]
    if s.get("bg_style") == "gradient":
        c2 = s.get("bg_color2", "#1a1a2e")
        return (
            f"gradients=s={width}x{height}:r={fps}:c0={bg}:c1={c2}:"
            f"x0=0:y0=0:x1=0:y1={height}[bg]"
        )
    return f"color=c={bg}:s={width}x{height}:r={fps}[bg]"


TRANSCRIBE_TEMPLATE = Template('''
import warnings
warnings.filterwarnings("ignore")
from faster_whisper import WhisperModel

model = WhisperModel("base", device="cpu", compute_type="int8")
segments, info = model.transcribe(r"$AUDIO", word_timestamps=True)

CHUNK_SIZE = $CHUNK_SIZE
CAPTION_STYLE = "$CAPTION_STYLE"

def fmt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: $WIDTH
PlayResY: $HEIGHT
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,$FONT_NAME,$FONT_SIZE,$CAPTION_HIGHLIGHT,$CAPTION_COLOR,&H00000000,$CAPTION_BACK,-1,0,0,0,100,100,1,0,4,0,0,2,60,60,$CAPTION_Y,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

lines = [ass_header]
import json as _json
all_words = []
seg_list = []
for segment in segments:
    seg_list.append({"start": segment.start, "end": segment.end, "text": segment.text.strip()})
    words = list(segment.words) if segment.words else []
    if not words:
        class FakeWord:
            pass
        w = FakeWord()
        w.word = segment.text.strip()
        w.start = segment.start
        w.end = segment.end
        all_words.append(w)
    else:
        for w in words:
            w.word = w.word.strip()
            all_words.append(w)
    print(f"PROGRESS {segment.end:.2f}", flush=True)

with open(r"$SEGMENTS_PATH", "w") as f:
    _json.dump(seg_list, f)

if CAPTION_STYLE == "bounce":
    for w in all_words:
        if not w.word:
            continue
        text = "{\\\\fscx60\\\\fscy60\\\\t(0,120,\\\\fscx100\\\\fscy100)}" + w.word
        lines.append(f"Dialogue: 0,{fmt_time(w.start)},{fmt_time(w.end)},Default,,0,0,0,,{text}")
else:
    chunks = [all_words[i:i+CHUNK_SIZE] for i in range(0, len(all_words), CHUNK_SIZE)]
    for chunk in chunks:
        if not chunk:
            continue
        chunk_start = fmt_time(chunk[0].start)
        chunk_end = fmt_time(chunk[-1].end)
        if CAPTION_STYLE == "fade":
            text = "{\\\\fad(150,150)}" + " ".join(w.word for w in chunk)
        else:
            parts = []
            for w in chunk:
                duration_cs = max(1, int((w.end - w.start) * 100))
                parts.append("{\\\\k" + str(duration_cs) + "}" + w.word.strip())
            text = " ".join(parts)
        lines.append(f"Dialogue: 0,{chunk_start},{chunk_end},Default,,0,0,0,,{text}")

with open(r"$ASS_PATH", "w") as f:
    f.write("\\n".join(lines))

print("DONE")
''')


def _run_ffmpeg_with_progress(cmd, total_seconds, progress_cb):
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
    stderr_chunks = []

    def drain_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000
                frac = min(1.0, seconds / total_seconds) if total_seconds else 0
                if progress_cb:
                    progress_cb(frac)
            except (ValueError, ZeroDivisionError):
                pass
    proc.wait()
    t.join()
    return proc.returncode, "".join(stderr_chunks)


def _run_transcription_with_progress(python_bin, env, script, total_seconds, progress_cb):
    proc = subprocess.Popen([python_bin, "-W", "ignore", "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1, env=env)
    stderr_chunks = []

    def drain_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("PROGRESS "):
            try:
                seconds = float(line.split(" ", 1)[1])
                frac = min(1.0, seconds / total_seconds) if total_seconds else 0
                if progress_cb:
                    progress_cb(frac)
            except (ValueError, ZeroDivisionError):
                pass
    proc.wait()
    t.join()
    return proc.returncode, "".join(stderr_chunks)


def _probe_duration(ffprobe, src):
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0


def render_job(s, audio_path, output_path, art_path=None,
               title="", script_path=None,
               progress_cb=None, log_cb=None,
               preview_seconds=None, outline_titles=None):
    """
    Render one podcast video.

    s               : settings dict (see DEFAULTS)
    audio_path      : Path to source audio
    output_path     : Path for the final .mp4
    art_path        : Path to cover art image (None if art disabled)
    title           : optional episode title overlay text
    script_path     : optional .docx episode script for outline
    progress_cb     : callable(stage: str, fraction: float)
    log_cb          : callable(str)
    preview_seconds : render only this many seconds (quick sanity check)
    outline_titles  : optional list of replacement title strings
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    def progress(stage, frac):
        if progress_cb:
            progress_cb(stage, frac)

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffmpeg("ffprobe")
    python_bin = find_python()

    audio = Path(audio_path)
    output = Path(output_path)

    art_enabled = s.get("art_enabled", True) and art_path is not None
    if art_enabled:
        art_path = Path(art_path)
        if not art_path.exists():
            art_enabled = False

    width = s["width"]
    height = s["height"]
    fps = s["fps"]
    art_size = s.get("art_size", 460)
    wave_enabled = s.get("waveform_enabled", True)
    wave_h = s.get("waveform_height", 100) if wave_enabled else 0
    outline_enabled = s.get("outline_enabled", False)
    outline_style = s.get("outline_style", "sidebar")

    # Sidebar wide enough that typical segment titles fit on one line
    sidebar_w = 480 if (outline_enabled and outline_style == "sidebar") else 0

    # Vertical: center art in usable space, but leave room above for captions
    usable_h = height - wave_h
    caption_reserve = 90  # pixels above art reserved for captions
    art_y = max(caption_reserve, (usable_h - art_size) // 2)
    # Ensure art doesn't bleed past the waveform strip
    if art_y + art_size > usable_h - 10:
        art_y = max(caption_reserve, usable_h - art_size - 10)

    # Horizontal: center art in the space to the right of the sidebar
    art_area_x = sidebar_w
    art_area_w = width - sidebar_w
    art_x = art_area_x + (art_area_w - art_size) // 2 + s.get("art_x_offset", 0)

    # Captions sit above the art (bottom of caption text = top of art - padding)
    # ASS MarginV for alignment=2 (bottom-center): text bottom = height - MarginV
    caption_y_ass = height - art_y + 16

    glow_color = detect_glow_color(art_path, s.get("glow_color", "#c9a84c")) if art_enabled else s.get("glow_color", "#c9a84c")

    ass_path = None
    outline_ass_path = None
    preview_audio = None

    try:
        if preview_seconds:
            preview_audio = Path(tempfile.gettempdir()) / f"vbvn_prev_{os.getpid()}{audio.suffix}"
            r = subprocess.run(
                [ffmpeg, "-i", str(audio), "-t", str(preview_seconds),
                 "-c", "copy", str(preview_audio), "-y", "-loglevel", "error"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                audio = preview_audio

        dur_r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            capture_output=True, text=True,
        )
        duration = dur_r.stdout.strip()
        if not duration:
            log(f"Could not read duration of {audio.name}")
            return False
        duration_f = float(duration)

        # ── Transcription ───────────────────────────────────────────────────
        progress("Transcribing", 0)
        log(f"Transcribing {audio.name}…")

        ass_path = Path(tempfile.gettempdir()) / f"vbvn_cap_{os.getpid()}.ass"
        segments_path = Path(tempfile.gettempdir()) / f"vbvn_seg_{os.getpid()}.json"

        script = TRANSCRIBE_TEMPLATE.substitute(
            AUDIO=str(audio),
            CHUNK_SIZE=s.get("words_per_chunk", 2),
            WIDTH=width, HEIGHT=height,
            FONT_NAME=s.get("font_name", "Georgia"),
            FONT_SIZE=s.get("font_size", 64),
            CAPTION_HIGHLIGHT=s.get("caption_highlight", "&H004CA8C9"),
            CAPTION_COLOR=s.get("caption_color", "&H50D8EAF0"),
            CAPTION_BACK=s.get("caption_back", "&HBF000000"),
            CAPTION_Y=caption_y_ass,
            CAPTION_STYLE=s.get("caption_style", "karaoke"),
            ASS_PATH=str(ass_path),
            SEGMENTS_PATH=str(segments_path),
        )
        env = dict(os.environ)
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        rc, stderr = _run_transcription_with_progress(
            python_bin, env, script, duration_f,
            lambda frac: progress("Transcribing", frac))

        sub_filter = ""
        if rc != 0 or not ass_path.exists():
            log(f"Transcription failed:\n{stderr[-1000:]}")
        else:
            sub_filter = f",subtitles={ass_path.as_posix()}"

        # ── Outline ─────────────────────────────────────────────────────────
        if outline_enabled and segments_path.exists():
            progress("Generating outline", 0)
            segments_data = json.loads(segments_path.read_text())
            points = []

            if script_path:
                script_points = extract_outline_from_script(script_path)
                if script_points and outline_titles:
                    if isinstance(outline_titles[0], dict):
                        orig_map = {e["original"]: e["title"] for e in outline_titles}
                        script_points = [(orig_map[t], a) for t, a in script_points if t in orig_map]
                    else:
                        script_points = [
                            (outline_titles[i] if i < len(outline_titles) and outline_titles[i] else t, a)
                            for i, (t, a) in enumerate(script_points)
                        ]
                if script_points:
                    points = align_outline_to_transcript(script_points, segments_data)
                    log(f"Outline from script: {len(points)} sections aligned.")

            if not points:
                log("Generating outline via Ollama…")
                points = generate_outline(segments_data, s, log_cb=log)

            if points:
                outline_ass_path = Path(tempfile.gettempdir()) / f"vbvn_out_{os.getpid()}.ass"
                if outline_style == "ticker":
                    build_ticker_ass(points, s, duration_f, outline_ass_path)
                else:
                    # Outline starts below captions, ends above waveform
                    outline_start_y = art_y + 16   # caption bottom is ~art_y-16; give a small gap
                    outline_end_y   = height - wave_h - 12
                    build_sidebar_ass(points, s, duration_f, outline_ass_path,
                                      start_y=outline_start_y, end_y=outline_end_y, art_x=art_x)
                sub_filter += f",subtitles={outline_ass_path.as_posix()}"
                log(f"Outline: {len(points)} points ({outline_style}).")
            else:
                log("No outline points — skipping outline overlay.")

        segments_path.unlink(missing_ok=True)

        # ── Title overlay ───────────────────────────────────────────────────
        title_filter = ""
        if title and s.get("title_enabled", True):
            escaped = title.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
            fn = s.get("title_font", "Georgia").replace("'", "\\'")
            title_filter = (
                f",drawtext=text={escaped}:font='{fn}':"
                f"fontsize={s.get('title_font_size', 48)}:fontcolor={s.get('title_color', 'white')}:"
                f"x=(w-text_w)/2:y=30:box=1:boxcolor=black@0.4:boxborderw=10"
            )

        # ── Build ffmpeg filter_complex ──────────────────────────────────────
        progress("Rendering", 0)
        log(f"Rendering video…")

        tmp_audio = Path(tempfile.gettempdir()) / f"vbvn_audio_{os.getpid()}{audio.suffix}"
        shutil.copy(audio, tmp_audio)
        tmp_out = output.with_suffix(".tmp.mp4")

        bg_filter = build_bg_filter(s, fps)

        # Input layout: [0:v]=art (if enabled), [1:a]=audio (or [0:a] if no art)
        inputs = []
        audio_stream = "0:a"

        if art_enabled:
            inputs += ["-loop", "1", "-framerate", str(fps), "-i", str(art_path)]
            audio_stream = "1:a"

        inputs += ["-i", str(tmp_audio)]

        # Build filter graph
        # art input index is 0 if art_enabled, otherwise n/a
        # audio input index is 1 if art_enabled, else 0

        glow_sigma = max(1, s.get("glow_sigma", 40))
        fp = []  # filter_complex parts

        if art_enabled:
            audio_idx = 1
            fp.append(f"[0:v]scale={art_size}:{art_size},format=rgb24[art_sharp]")
            fp.append(bg_filter)
            if s.get("glow_enabled", True):
                fp.append(f"[art_sharp]gblur=sigma={glow_sigma}[glow_soft]")
                fp.append(f"[bg][glow_soft]overlay={art_x}:{art_y}:format=auto[bg_glow]")
                fp.append(f"[bg_glow][art_sharp]overlay={art_x}:{art_y}:format=auto[bg_art]")
            else:
                fp.append(f"[bg][art_sharp]overlay={art_x}:{art_y}:format=auto[bg_art]")
            prev = "[bg_art]"
        else:
            audio_idx = 0
            fp.append(bg_filter)
            prev = "[bg]"

        if wave_enabled:
            wc = s.get("waveform_color", "#c9a84c")
            fp.append(f"[{audio_idx}:a]showwaves=s={width}x{wave_h}:mode=cline:colors={wc},format=rgba[wave]")
            fp.append(f"{prev}[wave]overlay=0:{height - wave_h}:format=auto[bg_wave]")
            prev = "[bg_wave]"

        # Subtitles and title drawtext are chained as trailing filters on the last label
        extra = sub_filter.lstrip(",") + title_filter
        if extra:
            fp.append(f"{prev}{extra}[out]")
        else:
            fp.append(f"{prev}null[out]")

        filter_str = ";".join(fp)

        cmd = [
            ffmpeg,
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[out]", "-map", f"{audio_idx}:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-t", duration,
            "-movflags", "+faststart",
            "-loglevel", "warning",
            str(tmp_out), "-y",
        ]

        rc, stderr = _run_ffmpeg_with_progress(
            cmd, duration_f, lambda frac: progress("Rendering", frac))
        tmp_audio.unlink(missing_ok=True)

        if rc != 0:
            log(f"Render failed:\n{stderr[-2000:]}")
            tmp_out.unlink(missing_ok=True)
            return False

        shutil.move(str(tmp_out), str(output))
        size_mb = output.stat().st_size / (1024 * 1024)
        progress("Done", 1.0)
        log(f"Done → {output.name} ({size_mb:.1f} MB)")
        return True

    finally:
        if ass_path is not None:
            ass_path.unlink(missing_ok=True)
        if outline_ass_path is not None:
            outline_ass_path.unlink(missing_ok=True)
        if preview_audio is not None:
            preview_audio.unlink(missing_ok=True)
