"""
Shared, Tk-free render pipeline for the podcast video generator.
Used by both the desktop GUI (podcast_video_gui.py) and the web UI (web_app.py).

Call render_job(settings, audio_path, output_path, title="", art_path=..., progress_cb=..., log_cb=...)
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
    "art": "podcast-art.jpeg",
    "art_size": 700,
    "bg_color": "black",
    "glow_color": "#c9a84c",
    "glow_sigma": 220,
    "pulse_speed": 3,
    "breathe_amount": 0.02,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "font_name": "Cormorant Garamond",
    "font_fallback": "Georgia",
    "font_size": 72,
    "words_per_chunk": 2,
    "caption_color": "&H50D8EAF0",
    "caption_highlight": "&H004CA8C9",
    "caption_back": "&HBF000000",
    "caption_y": 900,
    "bg_style": "solid",
    "bg_color2": "#1a1a2e",
    "waveform_enabled": False,
    "waveform_color": "#c9a84c",
    "caption_style": "karaoke",
    "output_dir": "",
    "intro_path": "",
    "outro_path": "",
    "title_font": "Georgia",
    "title_font_size": 48,
    "title_color": "white",
    "outline_enabled": False,
    "art_x_offset": 0,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1",
    "outline_font": "Georgia",
    "outline_font_size": 36,
    "outline_color": "#ffffff",
    "bg_video_path": "",
    "chapter_cards_enabled": False,
    "endcard_enabled": False,
    "endcard_text": "",
    "endcard_seconds": 6,
    "watermark_enabled": False,
    "watermark_path": "",
    "watermark_size": 120,
}

BG_STYLES = ["solid", "gradient", "noise", "video"]
CAPTION_STYLES = ["karaoke", "fade", "bounce"]

BUILTIN_PRESETS = {
    "Gold Glow (Default)": dict(DEFAULTS),
    "Cool Blue": {
        **DEFAULTS,
        "glow_color": "#4c8ec9",
        "bg_style": "gradient",
        "bg_color": "#0a0e1a",
        "bg_color2": "#1a2a4a",
        "caption_highlight": "&H00C98E4C",
        "caption_color": "&H50F0EAD8",
    },
    "Minimal White": {
        **DEFAULTS,
        "bg_style": "solid",
        "bg_color": "#f5f5f0",
        "glow_color": "#dddddd",
        "glow_sigma": 160,
        "caption_color": "&H50202020",
        "caption_highlight": "&H00505050",
        "caption_back": "&HBFF5F5F0",
        "caption_style": "fade",
    },
    "Neon Bounce": {
        **DEFAULTS,
        "glow_color": "#ff2ec4",
        "bg_style": "noise",
        "bg_color": "#0a0014",
        "caption_highlight": "&H00C42EFF",
        "caption_color": "&H50F0F0F0",
        "caption_style": "bounce",
        "waveform_enabled": True,
        "waveform_color": "#ff2ec4",
    },
    "Calm Gradient + Waveform": {
        **DEFAULTS,
        "glow_color": "#7c9c8c",
        "bg_style": "gradient",
        "bg_color": "#0f1410",
        "bg_color2": "#2a3a30",
        "caption_style": "fade",
        "waveform_enabled": True,
        "waveform_color": "#7c9c8c",
    },
}

PYTHON_VENV = Path.home() / "whisper-env" / "bin" / "python3"


def find_python():
    if PYTHON_VENV.exists():
        return str(PYTHON_VENV)
    return sys.executable


def find_ffmpeg(name="ffmpeg"):
    p = shutil.which(name)
    return p or name


VAAPI_DEVICE = "/dev/dri/renderD128"
_vaapi_cache = None


def vaapi_available(ffmpeg):
    """Detect whether ffmpeg can use VAAPI hardware H.264 encoding on this machine
    (e.g. Intel iGPU on Rocky Linux). Cached after first check."""
    global _vaapi_cache
    if _vaapi_cache is not None:
        return _vaapi_cache
    if not os.path.exists(VAAPI_DEVICE):
        _vaapi_cache = False
        return False
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                            capture_output=True, text=True, timeout=10)
        if "h264_vaapi" not in r.stdout:
            _vaapi_cache = False
            return False
        # Confirm the device actually initializes (driver/permission issues
        # can make it "available" but unusable).
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-vaapi_device", VAAPI_DEVICE,
             "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1",
             "-vf", "format=nv12,hwupload",
             "-c:v", "h264_vaapi", "-qp", "20",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=15,
        )
        _vaapi_cache = r.returncode == 0
    except Exception:
        _vaapi_cache = False
    return _vaapi_cache


def _video_encode_args(ffmpeg, use_vaapi):
    """Return (global_args, codec_args, filter_suffix) for video encoding.
    filter_suffix is appended to the final video filter output (before its label)
    to upload frames to the VAAPI surface when hardware encoding is used."""
    if use_vaapi:
        return (
            ["-vaapi_device", VAAPI_DEVICE],
            ["-c:v", "h264_vaapi", "-qp", "20"],
            ",format=nv12,hwupload",
        )
    return (
        [],
        ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
        "",
    )


def detect_glow_color(art_path, fallback="#c9a84c"):
    """Sample the corner pixels of the cover art to pick a glow color matching
    its background. Falls back to gold if the background is black/near-black."""
    try:
        from PIL import Image
        img = Image.open(art_path).convert("RGB")
        w, h = img.size
        m = max(2, min(w, h) // 50)
        corners = [(m, m), (w - m, m), (m, h - m), (w - m, h - m)]
        r, g, b = 0, 0, 0
        for x, y in corners:
            pr, pg, pb = img.getpixel((x, y))
            r += pr
            g += pg
            b += pb
        n = len(corners)
        r, g, b = r // n, g // n, b // n
        if max(r, g, b) < 24:
            return fallback
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return fallback


_NAMED_COLORS = {"white": "#ffffff", "black": "#000000"}


def _color_to_hex(value, default="#ffffff"):
    if not value:
        return default
    if value.startswith("#"):
        return value
    return _NAMED_COLORS.get(value, default)


def hex_to_ass(hex_color, alpha="00"):
    """Convert '#RRGGBB' to ASS '&HAABBGGRR'."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        hex_color = "ffffff"
    rr, gg, bb = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H{alpha}{bb}{gg}{rr}".upper()


_HEADER_RE = re.compile(r"^(?:[^A-Za-z0-9]+\s*)?([A-Z][A-Za-z0-9 ,:;'\-—]+)$")
_SPEAKER_RE = re.compile(r"^[A-Z][A-Z ]*:\s*")


def _normalize(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_outline_from_script(docx_path):
    """Parse a .docx episode script for section headers (lines like
    '📖  SEGMENT 1 — A LETTER FROM PRISON') and the spoken text that follows
    each one. Returns a list of (title, anchor_text) tuples in document order."""
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
    """Given (title, anchor_text) outline points and Whisper segments
    ([{start,end,text}]), find the timestamp where each point's anchor text
    starts being spoken. Returns a list of (start_seconds, title)."""
    if not points or not segments:
        return []

    seg_norms = [_normalize(seg["text"]) for seg in segments]
    offsets = []
    full_parts = []
    pos = 0
    for norm in seg_norms:
        offsets.append(pos)
        full_parts.append(norm)
        pos += len(norm) + 1
    full_text = " ".join(full_parts)

    result = []
    search_from = 0
    last_time = 0.0
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
        t = segments[seg_idx]["start"]
        if t < last_time:
            t = last_time
        result.append((t, title))
        last_time = t
        search_from = idx + 1
    return result


def generate_outline(segments, s, log_cb=None):
    """Ask a local Ollama model for a timed outline of the transcript.
    Returns a list of (start_seconds, text) tuples, or [] on failure."""
    import urllib.request

    def log(msg):
        if log_cb:
            log_cb(msg)

    transcript_lines = "\n".join(f"[{seg['start']:.1f}s] {seg['text']}" for seg in segments)
    prompt = (
        "You are given a transcript of a podcast episode, where each line is "
        "prefixed with the timestamp (in seconds) at which it was spoken.\n\n"
        "Produce a short outline of the main points/topics discussed, in the order "
        "they occur. For each point, pick the timestamp where that topic begins.\n\n"
        "Respond ONLY with a JSON array of objects, each with keys \"time\" (number, "
        "seconds) and \"text\" (string, a short phrase, max ~6 words). "
        "Aim for 4-8 points total.\n\n"
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
        outline_raw = data.get("response", "")
        points = json.loads(outline_raw)
        result = []
        for p in points:
            t = float(p.get("time", 0))
            text = str(p.get("text", "")).strip()
            if text:
                result.append((t, text))
        result.sort(key=lambda x: x[0])
        return result
    except Exception as e:
        log(f"Outline generation failed (is Ollama running at {s.get('ollama_url')}?): {e}")
        return []


def build_outline_ass(points, s, duration_f, out_path):
    """Write an ASS file with one persistent left-side line per outline point,
    each appearing at its timestamp and remaining until the end of the video."""
    color = hex_to_ass(s.get("outline_color", "#ffffff"))
    font = s.get("outline_font", "Georgia")
    size = s.get("outline_font_size", 36)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['width']}
PlayResY: {s['height']}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{color},{color},&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,2,0,7,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        sec = t % 60
        return f"{h:d}:{m:02d}:{sec:05.2f}"

    import textwrap

    # Keep outline text from extending under the cover art: wrap to the space
    # between the text's left margin and the art's left edge.
    art_size = s.get("art_size", 700)
    art_x_offset = s.get("art_x_offset", 0)
    art_left = (s["width"] - art_size) / 2 + art_x_offset
    available_width = max(100, art_left - 60 - 20)  # 60 = text x, 20 = padding
    max_chars = max(10, int(available_width / (size * 0.55)))

    # Pre-wrap all items to know total height needed
    top_offset = max(0, (s["height"] - art_size) // 2)
    available_height = s["height"] - top_offset - 20
    wrapped_items = []
    for _, text in points:
        escaped = text.replace("\\", "\\\\").replace("{", "").replace("}", "")
        wrapped_items.append(textwrap.wrap(escaped, max_chars) or [""])
    total_lines = sum(len(w) for w in wrapped_items)
    base_line_height = size + 24
    # Compress spacing if needed, but never go below size + 4
    line_height = max(size + 4, min(base_line_height, available_height // max(total_lines, 1)))

    lines = [header]
    y = top_offset
    dim_hex = "888888"

    for i, (start, _) in enumerate(points):
        if start >= duration_f:
            continue
        wrapped = wrapped_items[i]
        ass_text = r"\N".join(wrapped)
        pos = f"\\pos(60,{y})\\an7"

        next_start = points[i + 1][0] if i + 1 < len(points) else duration_f
        active_end = min(next_start, duration_f)

        if active_end > start:
            tag = f"{{{pos}\\fad(500,0)}}"
            lines.append(f"Dialogue: 0,{fmt(start)},{fmt(active_end)},Default,,0,0,0,,{tag}{ass_text}")

        if active_end < duration_f:
            tag = f"{{{pos}\\c&H{dim_hex}&}}"
            lines.append(f"Dialogue: 0,{fmt(active_end)},{fmt(duration_f)},Default,,0,0,0,,{tag}{ass_text}")

        y += line_height * len(wrapped)

    out_path.write_text("\n".join(lines))


def _ass_fmt(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    sec = t % 60
    return f"{h:d}:{m:02d}:{sec:05.2f}"


def build_chapter_cards_ass(points, s, duration_f, out_path):
    """Write an ASS file with a large centered title card that briefly appears
    at the start of each outline section (chapter transition)."""
    color = hex_to_ass(_color_to_hex(s.get("title_color"), "#ffffff"))
    font = s.get("title_font", "Georgia")
    size = int(s.get("title_font_size", 48) * 1.6)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['width']}
PlayResY: {s['height']}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{color},{color},&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,3,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    cx, cy = s["width"] // 2, s["height"] // 2
    for i, (start, text) in enumerate(points):
        if start >= duration_f:
            continue
        next_start = points[i + 1][0] if i + 1 < len(points) else duration_f
        end = min(start + 3.5, next_start, duration_f)
        if end <= start:
            continue
        escaped = text.replace("\\", "\\\\").replace("{", "").replace("}", "")
        tag = f"{{\\pos({cx},{cy})\\an5\\fad(400,400)}}"
        lines.append(f"Dialogue: 0,{_ass_fmt(start)},{_ass_fmt(end)},Default,,0,0,0,,{tag}{escaped}")

    out_path.write_text("\n".join(lines))


def build_endcard_ass(text, s, duration_f, out_path):
    """Write an ASS file showing a centered end-card message for the last
    portion of the video (e.g. 'Thanks for listening — subscribe for more!')."""
    color = hex_to_ass(_color_to_hex(s.get("outline_color"), "#ffffff"))
    font = s.get("outline_font", "Georgia")
    size = int(s.get("outline_font_size", 36) * 1.3)
    seconds = max(1, float(s.get("endcard_seconds", 6)))
    start = max(0, duration_f - seconds)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['width']}
PlayResY: {s['height']}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{color},{color},&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,3,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    cx, cy = s["width"] // 2, s["height"] // 2
    text = text.replace(r"\n", "\n")
    escaped = text.replace("\\", "\\\\").replace("{", "").replace("}", "").replace("\n", r"\N")
    tag = f"{{\\pos({cx},{cy})\\an5\\fad(500,0)}}"
    lines = [header, f"Dialogue: 0,{_ass_fmt(start)},{_ass_fmt(duration_f)},Default,,0,0,0,,{tag}{escaped}"]
    out_path.write_text("\n".join(lines))


def build_bg_chain(s, fps, bg_input_index=None):
    """Return an ffmpeg filter_complex chain (source -> [bg]) for the chosen background style.
    For style "video", bg_input_index must be the ffmpeg input index of the looping background video."""
    width, height, bg = s["width"], s["height"], s["bg_color"]
    style = s.get("bg_style", "solid")
    if style == "video" and bg_input_index is not None:
        return (
            f"[{bg_input_index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},format=rgb24[bg]"
        )
    if style == "gradient":
        c2 = s.get("bg_color2", "#1a1a2e")
        return (
            f"gradients=s={width}x{height}:r={fps}:c0={bg}:c1={c2}:"
            f"x0=0:y0=0:x1=0:y1={height}[bg]"
        )
    if style == "noise":
        return (
            f"color=c={bg}:s={width}x{height}:r={fps},"
            f"noise=alls=15:allf=t+u[bg]"
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
        start = fmt_time(w.start)
        end = fmt_time(w.end)
        text = "{\\\\fscx60\\\\fscy60\\\\t(0,120,\\\\fscx100\\\\fscy100)}" + w.word
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
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
    """Run an ffmpeg command with -progress pipe:1, calling progress_cb(fraction) live."""
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)

    stderr_chunks = []

    def drain_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

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
    stderr_thread.join()
    return proc.returncode, "".join(stderr_chunks)


def _run_transcription_with_progress(python_bin, env, script, total_seconds, progress_cb):
    proc = subprocess.Popen([python_bin, "-W", "ignore", "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1, env=env)

    stderr_chunks = []

    def drain_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

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
    stderr_thread.join()
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


def _reencode_clip(ffmpeg, ffprobe, src, dst, s, progress_cb=None):
    """Re-encode an intro/outro clip to match the main render's spec so concat works.
    Adds a silent audio track if the source has none, so the concat output keeps audio."""
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    has_audio = bool(probe.stdout.strip())

    use_vaapi = vaapi_available(ffmpeg)
    global_args, codec_args, vf_suffix = _video_encode_args(ffmpeg, use_vaapi)

    vf = (f"scale={s['width']}:{s['height']}:force_original_aspect_ratio=decrease,"
          f"pad={s['width']}:{s['height']}:(ow-iw)/2:(oh-ih)/2,fps={s['fps']}{vf_suffix}")

    if has_audio:
        cmd = [
            ffmpeg, *global_args, "-i", str(src),
            "-vf", vf,
            *codec_args,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(dst), "-y", "-loglevel", "error",
        ]
    else:
        cmd = [
            ffmpeg, *global_args, "-i", str(src),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", vf, "-shortest",
            "-map", "0:v", "-map", "1:a",
            *codec_args,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(dst), "-y", "-loglevel", "error",
        ]
    if progress_cb:
        cmd = cmd[:-2]  # strip trailing "-y", "-loglevel", "error" before adding -progress
        total = _probe_duration(ffprobe, src)
        rc, err = _run_ffmpeg_with_progress(cmd + ["-y", "-loglevel", "error"], total, progress_cb)
        return rc == 0, err

    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def _add_intro_outro(ffmpeg, ffprobe, s, output, log_cb, progress_cb=None):
    tmpdir = Path(tempfile.gettempdir())
    parts = []

    def sub_progress(frac):
        if progress_cb:
            progress_cb(frac)

    if s.get("intro_path"):
        intro_re = tmpdir / "vbvn_intro.mp4"
        success, err = _reencode_clip(ffmpeg, ffprobe, s["intro_path"], intro_re, s,
                                       progress_cb=lambda f: sub_progress(f * 0.2))
        if success:
            parts.append(intro_re)
        elif log_cb:
            log_cb(f"Intro clip failed to re-encode, skipping:\n{err[-800:]}")

    main_re = tmpdir / "vbvn_main.mp4"
    success, err = _reencode_clip(ffmpeg, ffprobe, output, main_re, s,
                                   progress_cb=lambda f: sub_progress(0.2 + f * 0.6))
    if not success:
        if log_cb:
            log_cb(f"Main clip re-encode for concat failed, skipping intro/outro:\n{err[-800:]}")
        return
    parts.append(main_re)

    if s.get("outro_path"):
        outro_re = tmpdir / "vbvn_outro.mp4"
        success, err = _reencode_clip(ffmpeg, ffprobe, s["outro_path"], outro_re, s,
                                       progress_cb=lambda f: sub_progress(0.8 + f * 0.1))
        if success:
            parts.append(outro_re)
        elif log_cb:
            log_cb(f"Outro clip failed to re-encode, skipping:\n{err[-800:]}")

    if len(parts) < 2:
        sub_progress(1.0)
        return

    use_vaapi = vaapi_available(ffmpeg)
    global_args, codec_args, vf_suffix = _video_encode_args(ffmpeg, use_vaapi)

    final = output.with_suffix(".concat.mp4")
    cmd = [ffmpeg, *global_args]
    for p in parts:
        cmd += ["-i", str(p)]
    n = len(parts)
    concat_inputs = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    if vf_suffix:
        filter_complex = f"{concat_inputs}concat=n={n}:v=1:a=1[vc][a];[vc]{vf_suffix.lstrip(',')}[v]"
    else:
        filter_complex = f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]"
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *codec_args,
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(final),
    ]
    total = sum(_probe_duration(ffprobe, p) for p in parts)
    rc, err = _run_ffmpeg_with_progress(cmd + ["-y", "-loglevel", "error"], total,
                                         lambda f: sub_progress(0.9 + f * 0.1))
    if rc == 0:
        shutil.move(str(final), str(output))
    elif log_cb:
        log_cb(f"Concat with intro/outro failed:\n{err[-1000:]}")

    for p in parts:
        p.unlink(missing_ok=True)


def render_job(s, audio_path, output_path, art_path, title="", script_path=None,
                progress_cb=None, log_cb=None, preview_seconds=None, outline_titles=None):
    """
    Render one podcast video.

    s: settings dict (see DEFAULTS)
    audio_path: Path to source audio
    output_path: Path for the final .mp4
    art_path: Path to the cover art image
    title: optional episode title overlay text
    script_path: optional .docx episode script — if given and outline_enabled,
        outline points/timings are derived from its section headers instead of Ollama
    progress_cb: callable(stage: str, fraction: float 0..1) called periodically
    log_cb: callable(str) for log messages
    preview_seconds: if set, only render this many seconds (fast sanity check)
    outline_titles: optional list of replacement strings for the script-derived
        outline section titles, in document order

    Returns True on success, False on failure.
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
    art_path = Path(art_path)

    glow_color = detect_glow_color(art_path, s.get("glow_color", "#c9a84c"))

    glow_loop = Path(tempfile.gettempdir()) / f"vbvn_glow_loop_{os.getpid()}_{threading.get_ident()}.mp4"
    log("Generating glow loop…")
    half = s["pulse_speed"] / 2
    glow_src, glow_pad = 600, 700
    canvas = glow_src + glow_pad * 2
    cmd = [
        ffmpeg, "-f", "lavfi",
        "-i", f"color=c={glow_color}:s={glow_src}x{glow_src}:r={s['fps']}",
        "-filter_complex",
        f"[0:v]pad={canvas}:{canvas}:{glow_pad}:{glow_pad}:black,"
        f"gblur=sigma={s['glow_sigma']},"
        f"fade=t=in:st=0:d={half}:color=black,"
        f"fade=t=out:st={half}:d={half}:color=black[out]",
        "-map", "[out]", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-t", str(s["pulse_speed"]), str(glow_loop), "-y", "-loglevel", "error",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("Failed to build glow loop:\n" + r.stderr[-2000:])
        return False

    breathe_frames = max(1, int(s["fps"] * s["pulse_speed"]))

    ass_path = None
    outline_ass_path = None
    chapter_ass_path = None
    endcard_ass_path = None
    preview_audio = None
    try:
        if preview_seconds:
            preview_audio = Path(tempfile.gettempdir()) / f"vbvn_preview_audio_{os.getpid()}_{threading.get_ident()}{audio.suffix}"
            r = subprocess.run(
                [ffmpeg, "-i", str(audio), "-t", str(preview_seconds),
                 "-c", "copy", str(preview_audio), "-y", "-loglevel", "error"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                audio = preview_audio
            else:
                preview_audio = None

        # duration
        dur_r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            capture_output=True, text=True,
        )
        duration = dur_r.stdout.strip()
        if not duration:
            log(f"[{audio.name}] Could not read duration.")
            return False
        duration_f = float(duration)

        progress("Transcribing", 0)
        log(f"[{audio.name}] Transcribing…")

        ass_path = Path(tempfile.gettempdir()) / f"vbvn_captions_{os.getpid()}_{threading.get_ident()}.ass"
        segments_path = Path(tempfile.gettempdir()) / f"vbvn_segments_{os.getpid()}_{threading.get_ident()}.json"
        script = TRANSCRIBE_TEMPLATE.substitute(
            AUDIO=str(audio), CHUNK_SIZE=s["words_per_chunk"],
            WIDTH=s["width"], HEIGHT=s["height"],
            FONT_NAME=s["font_name"], FONT_SIZE=s["font_size"],
            CAPTION_HIGHLIGHT=s["caption_highlight"], CAPTION_COLOR=s["caption_color"],
            CAPTION_BACK=s["caption_back"], CAPTION_Y=s["caption_y"],
            CAPTION_STYLE=s["caption_style"],
            ASS_PATH=str(ass_path),
            SEGMENTS_PATH=str(segments_path),
        )
        env = dict(os.environ)
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        rc, stderr = _run_transcription_with_progress(
            python_bin, env, script, duration_f,
            lambda frac: progress("Transcribing", frac))
        if rc != 0 or not ass_path.exists():
            log(f"[{audio.name}] Transcription failed:\n{stderr[-1500:]}")
            sub_filter = ""
        else:
            sub_filter = f",subtitles={ass_path.as_posix()}"

        if (s.get("outline_enabled") or s.get("chapter_cards_enabled")) and segments_path.exists():
            progress("Generating outline", 0)
            segments_data = json.loads(segments_path.read_text())

            points = []
            if script_path:
                script_points = extract_outline_from_script(script_path)
                if script_points and outline_titles:
                    # outline_titles is [{title, original}] — keep only selected items,
                    # matched by original title, with user's display title substituted.
                    if isinstance(outline_titles[0], dict):
                        orig_map = {entry["original"]: entry["title"] for entry in outline_titles}
                        script_points = [
                            (orig_map[t], a)
                            for t, a in script_points
                            if t in orig_map
                        ]
                    else:
                        # legacy plain-string list — rename by index
                        script_points = [
                            (outline_titles[i] if i < len(outline_titles) and outline_titles[i] else t, a)
                            for i, (t, a) in enumerate(script_points)
                        ]
                if script_points:
                    points = align_outline_to_transcript(script_points, segments_data)
                    log(f"[{audio.name}] Outline from script: {len(points)}/{len(script_points)} sections aligned.")
                else:
                    log(f"[{audio.name}] No section headers found in script — falling back to Ollama.")

            if not points:
                log(f"[{audio.name}] Generating outline via Ollama…")
                points = generate_outline(segments_data, s, log_cb=log)

            if points:
                if s.get("outline_enabled"):
                    outline_ass_path = Path(tempfile.gettempdir()) / f"vbvn_outline_{os.getpid()}_{threading.get_ident()}.ass"
                    build_outline_ass(points, s, duration_f, outline_ass_path)
                    sub_filter += f",subtitles={outline_ass_path.as_posix()}"
                    log(f"[{audio.name}] Outline: {len(points)} points.")
                if s.get("chapter_cards_enabled"):
                    chapter_ass_path = Path(tempfile.gettempdir()) / f"vbvn_chapters_{os.getpid()}_{threading.get_ident()}.ass"
                    build_chapter_cards_ass(points, s, duration_f, chapter_ass_path)
                    sub_filter += f",subtitles={chapter_ass_path.as_posix()}"
                    log(f"[{audio.name}] Chapter cards: {len(points)} points.")
            else:
                log(f"[{audio.name}] No outline points generated — skipping outline/chapter cards.")
        segments_path.unlink(missing_ok=True)

        if s.get("endcard_enabled") and s.get("endcard_text"):
            endcard_ass_path = Path(tempfile.gettempdir()) / f"vbvn_endcard_{os.getpid()}_{threading.get_ident()}.ass"
            build_endcard_ass(s["endcard_text"], s, duration_f, endcard_ass_path)
            sub_filter += f",subtitles={endcard_ass_path.as_posix()}"

        progress("Rendering", 0)
        log(f"[{audio.name}] Rendering video…")

        tmp_audio = Path(tempfile.gettempdir()) / f"vbvn_audio_input_{os.getpid()}_{threading.get_ident()}{audio.suffix}"
        shutil.copy(audio, tmp_audio)

        art_size = s["art_size"]
        art_x_offset = int(s.get("art_x_offset", 0))
        tmp_out = output.with_suffix(".tmp.mp4")

        extra_inputs = []
        bg_input_index = None
        watermark_index = None
        if s.get("bg_style") == "video" and s.get("bg_video_path") and Path(s["bg_video_path"]).exists():
            bg_input_index = 3 + len(extra_inputs)
            extra_inputs += ["-stream_loop", "-1", "-i", str(s["bg_video_path"])]
        if s.get("watermark_enabled") and s.get("watermark_path") and Path(s["watermark_path"]).exists():
            watermark_index = 3 + len(extra_inputs)
            extra_inputs += ["-i", str(s["watermark_path"])]

        bg_chain = build_bg_chain(s, s["fps"], bg_input_index=bg_input_index)


        title_filter = ""
        if title:
            escaped = title.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
            font_name = s['title_font'].replace("'", "\\'")
            title_filter = (
                f",drawtext=text={escaped}:font='{font_name}':"
                f"fontsize={s['title_font_size']}:fontcolor={s['title_color']}:"
                f"x=(w-text_w)/2:y=60:box=1:boxcolor=black@0.4:boxborderw=12"
            )

        use_vaapi = vaapi_available(ffmpeg)
        global_args, codec_args, vf_suffix = _video_encode_args(ffmpeg, use_vaapi)

        if watermark_index is not None:
            wm_size = s.get("watermark_size", 120)
            tail = f"{sub_filter}{title_filter}[pre_wm];[{watermark_index}:v]scale={wm_size}:-1[wm];[pre_wm][wm]overlay=W-w-30:H-h-30{vf_suffix}[out]"
        else:
            tail = f"{sub_filter}{title_filter}{vf_suffix}[out]"

        if s.get("waveform_enabled"):
            wave_h = 160
            filter_complex = (
                f"[0:v]scale={art_size+20}:{art_size+20},"
                f"zoompan=z='1+{s['breathe_amount']}*sin(2*PI*on/{breathe_frames})':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                f"s={art_size+20}x{art_size+20}:fps={s['fps']},"
                f"scale={art_size}:{art_size},format=rgb24[art_sharp];"
                f"[1:v]format=rgb24[glow_pulsed];"
                f"{bg_chain};"
                f"[2:a]showwaves=s={s['width']}x{wave_h}:mode=cline:colors={s['waveform_color']},"
                f"format=rgba[wave];"
                f"[bg][glow_pulsed]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto[bg_glow];"
                f"[bg_glow][art_sharp]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto[bg_art];"
                f"[bg_art][wave]overlay=(W-w)/2+{art_x_offset}:H-h-40:format=auto{tail}"
            )
        else:
            filter_complex = (
                f"[0:v]scale={art_size+20}:{art_size+20},"
                f"zoompan=z='1+{s['breathe_amount']}*sin(2*PI*on/{breathe_frames})':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                f"s={art_size+20}x{art_size+20}:fps={s['fps']},"
                f"scale={art_size}:{art_size},format=rgb24[art_sharp];"
                f"[1:v]format=rgb24[glow_pulsed];"
                f"{bg_chain};"
                f"[bg][glow_pulsed]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto[bg_glow];"
                f"[bg_glow][art_sharp]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto{tail}"
            )
        cmd = [
            ffmpeg, *global_args,
            "-loop", "1", "-framerate", str(s["fps"]), "-i", str(art_path),
            "-stream_loop", "-1", "-i", str(glow_loop),
            "-i", str(tmp_audio),
            *extra_inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]", "-map", "2:a",
            *codec_args,
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
            log(f"[{audio.name}] Render failed:\n{stderr[-1500:]}")
            tmp_out.unlink(missing_ok=True)
            return False

        progress("Trimming", 0)

        adur_r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(tmp_out)],
            capture_output=True, text=True,
        )
        audio_dur = adur_r.stdout.strip()

        trimmed_ok = False
        if audio_dur:
            rc, stderr = _run_ffmpeg_with_progress(
                [ffmpeg, "-i", str(tmp_out), "-t", audio_dur,
                 "-c", "copy", "-movflags", "+faststart",
                 str(output), "-y", "-loglevel", "error"],
                float(audio_dur), lambda frac: progress("Trimming", frac))
            trimmed_ok = rc == 0

        if trimmed_ok:
            tmp_out.unlink(missing_ok=True)
        else:
            shutil.move(str(tmp_out), str(output))
            log(f"[{audio.name}] Trim step failed — using untrimmed output.")

        if (s.get("intro_path") or s.get("outro_path")) and not preview_seconds:
            progress("Adding intro/outro", 0)
            _add_intro_outro(ffmpeg, ffprobe, s, output, log,
                             progress_cb=lambda frac: progress("Adding intro/outro", frac))

        size_mb = output.stat().st_size / (1024 * 1024)
        progress("Done", 1.0)
        log(f"[{audio.name}] Done → {output.name} ({size_mb:.1f} MB)")
        return True
    finally:
        glow_loop.unlink(missing_ok=True)
        if ass_path is not None:
            ass_path.unlink(missing_ok=True)
        if outline_ass_path is not None:
            outline_ass_path.unlink(missing_ok=True)
        if chapter_ass_path is not None:
            chapter_ass_path.unlink(missing_ok=True)
        if endcard_ass_path is not None:
            endcard_ass_path.unlink(missing_ok=True)
        if preview_audio is not None:
            preview_audio.unlink(missing_ok=True)
