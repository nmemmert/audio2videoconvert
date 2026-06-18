#!/usr/bin/env python3
"""
Verse by Verse — Podcast Video Generator GUI
A Tkinter front-end for the make_podcast_video pipeline:
  - queue of audio files with per-file progress/status
  - editable render settings with save/load presets (templates)
  - art image picker with thumbnail preview
  - quick low-res preview render (no transcription) of the visual style
  - full render (transcription + captions + video) for the whole queue
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from string import Template
from pathlib import Path
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

HERE = Path(__file__).resolve().parent
PRESETS_DIR = HERE / "presets"
PRESETS_DIR.mkdir(exist_ok=True)

CLIPS_DIR = HERE / "generated_clips"
CLIPS_DIR.mkdir(exist_ok=True)

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
}

BG_STYLES = ["solid", "gradient", "noise"]
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

FIELD_SPECS = [
    ("art", "Art image", "art"),
    ("art_size", "Art size (px)", "int"),
    ("bg_color", "Background color", "str"),
    ("glow_color", "Glow color (#hex)", "str"),
    ("glow_sigma", "Glow blur sigma", "int"),
    ("pulse_speed", "Pulse speed (sec)", "float"),
    ("breathe_amount", "Breathe amount", "float"),
    ("width", "Video width", "int"),
    ("height", "Video height", "int"),
    ("fps", "FPS", "int"),
    ("font_name", "Font name", "str"),
    ("font_fallback", "Font fallback", "str"),
    ("font_size", "Caption font size", "int"),
    ("words_per_chunk", "Words per caption chunk", "int"),
    ("caption_color", "Caption color (ASS &H..)", "str"),
    ("caption_highlight", "Caption highlight (ASS &H..)", "str"),
    ("caption_back", "Caption background (ASS &H..)", "str"),
    ("caption_y", "Caption Y position", "int"),
]

HEX_COLOR_FIELDS = {"bg_color", "glow_color", "bg_color2", "waveform_color", "title_color"}
ASS_COLOR_FIELDS = {"caption_color", "caption_highlight", "caption_back"}
FONT_FIELDS = {"font_name", "font_fallback", "title_font"}


def ass_to_rgb_hex(ass):
    """Convert an ASS '&HAABBGGRR' color to a '#RRGGBB' hex string. Falls back to black on parse error."""
    try:
        digits = ass.upper().replace("&H", "").zfill(8)
        bb, gg, rr = digits[2:4], digits[4:6], digits[6:8]
        return f"#{rr}{gg}{bb}"
    except Exception:
        return "#000000"


def rgb_hex_to_ass(rgb_hex, original_ass):
    """Convert '#RRGGBB' back to '&HAABBGGRR', preserving the alpha byte from original_ass."""
    try:
        alpha = original_ass.upper().replace("&H", "").zfill(8)[0:2]
    except Exception:
        alpha = "00"
    rgb_hex = rgb_hex.lstrip("#")
    rr, gg, bb = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"&H{alpha}{bb}{gg}{rr}".upper()


def to_tk_color(value):
    """Best-effort conversion of a color setting (hex, ASS, or named) to a Tk-displayable color."""
    if value.startswith("&H"):
        return ass_to_rgb_hex(value)
    return value


PYTHON_VENV = Path.home() / "whisper-env" / "bin" / "python3"


def find_python():
    if PYTHON_VENV.exists():
        return str(PYTHON_VENV)
    return sys.executable


def build_bg_chain(s, fps):
    """Return an ffmpeg filter_complex chain (source -> [bg]) for the chosen background style."""
    width, height, bg = s["width"], s["height"], s["bg_color"]
    style = s.get("bg_style", "solid")
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


def find_ffmpeg(name="ffmpeg"):
    p = shutil.which(name)
    return p or name


# ------------------------------------------------------------
# Transcription helper script (written to a temp file and run
# with the whisper-env python)
# ------------------------------------------------------------
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

all_words = []
for segment in segments:
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


class VideoJob:
    def __init__(self, path):
        self.path = Path(path)
        self.status = "Queued"
        self.progress = 0.0
        self.title = ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.dnd_available = False
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_available = True
            except Exception:
                self.dnd_available = False
        self.title("Verse by Verse — Podcast Video Generator")
        self.geometry("1180x800")
        self.minsize(1000, 700)

        self.settings = dict(DEFAULTS)
        self.color_swatches = {}
        self.jobs = []
        self.worker_thread = None
        self.cancel_flag = threading.Event()

        self._seed_builtin_presets()
        self._build_ui()
        self._load_default_preset_if_exists()
        self._refresh_art_preview()

    # ------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------
    def _build_ui(self):
        outer = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(outer)
        right = ttk.Frame(outer)
        outer.add(left, weight=3)
        outer.add(right, weight=2)

        self._build_queue_panel(left)
        self._build_settings_panel(right)

    # --- Queue / files panel -------------------------------------------------
    def _build_queue_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Audio Queue")
        frame.pack(fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, padx=6, pady=6)

        ttk.Button(btn_row, text="Add Files…", command=self.add_files).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Add From This Folder", command=self.add_from_folder).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_row, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Clear", command=self.clear_queue).pack(side=tk.LEFT, padx=6)

        out_row = ttk.Frame(frame)
        out_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Label(out_row, text="Output folder:").pack(side=tk.LEFT)
        self.output_dir_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(out_row, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(out_row, text="Browse…", command=self.choose_output_dir).pack(side=tk.LEFT)
        ttk.Button(out_row, text="Clear", command=lambda: self.output_dir_var.set("")).pack(side=tk.LEFT, padx=(4, 0))

        columns = ("file", "title", "status", "progress")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("file", text="Audio File")
        self.tree.heading("title", text="Episode Title (double-click to edit)")
        self.tree.heading("status", text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.column("file", width=260)
        self.tree.column("title", width=220)
        self.tree.column("status", width=140)
        self.tree.column("progress", width=90, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", self.edit_title)

        if self.dnd_available:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self.on_drop_files)
        else:
            ttk.Label(frame, text="(Install 'tkinterdnd2' to enable drag-and-drop)",
                      foreground="gray").pack(anchor="w", padx=6)

        run_row = ttk.Frame(frame)
        run_row.pack(fill=tk.X, padx=6, pady=6)

        self.start_btn = ttk.Button(run_row, text="▶ Start Render", command=self.start_render)
        self.start_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(run_row, text="■ Cancel", command=self.cancel_render, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=6)

        log_frame = ttk.LabelFrame(frame, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # --- Settings panel -------------------------------------------------------
    def _build_settings_panel(self, parent):
        # Presets row
        preset_frame = ttk.LabelFrame(parent, text="Templates / Presets")
        preset_frame.pack(fill=tk.X, pady=(0, 8))

        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(preset_frame, textvariable=self.preset_var, state="readonly")
        self.preset_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=6)
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self._refresh_preset_list()

        ttk.Button(preset_frame, text="Save As…", command=self.save_preset_as).pack(side=tk.LEFT, padx=4)
        ttk.Button(preset_frame, text="Delete", command=self.delete_preset).pack(side=tk.LEFT, padx=(0, 6))

        # Scrollable settings area
        settings_frame = ttk.LabelFrame(parent, text="Render Settings")
        settings_frame.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(settings_frame, highlightthickness=0)
        scroll = ttk.Scrollbar(settings_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Art picker + preview thumbnail
        art_row = ttk.Frame(inner)
        art_row.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        ttk.Label(art_row, text="Art Image:").pack(side=tk.LEFT)
        self.art_label = ttk.Label(art_row, text=self._short_path(self.settings["art"]))
        self.art_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(art_row, text="Browse…", command=self.choose_art).pack(side=tk.RIGHT)

        preview_holder = ttk.Frame(inner, width=180, height=180)
        preview_holder.grid(row=1, column=0, columnspan=2, pady=(0, 8))
        preview_holder.grid_propagate(False)
        self.art_preview = ttk.Label(preview_holder)
        self.art_preview.place(relx=0.5, rely=0.5, anchor="center")

        self.vars = {}
        row = 2
        font_families = sorted(set(tkfont.families()))
        for key, label, kind in FIELD_SPECS:
            if key == "art":
                continue
            ttk.Label(inner, text=label + ":").grid(row=row, column=0, sticky="w", padx=6, pady=3)
            var = tk.StringVar(value=str(self.settings[key]))

            if key in FONT_FIELDS:
                ttk.Combobox(inner, textvariable=var, values=font_families, width=20).grid(
                    row=row, column=1, sticky="w", padx=6, pady=3)
            else:
                entry = ttk.Entry(inner, textvariable=var, width=22)
                entry.grid(row=row, column=1, sticky="w", padx=6, pady=3)

            if key in HEX_COLOR_FIELDS or key in ASS_COLOR_FIELDS:
                self._add_color_swatch(inner, row, var, is_ass=(key in ASS_COLOR_FIELDS), key=key)

            self.vars[key] = (var, kind)
            row += 1

        # Background style
        ttk.Label(inner, text="Background style:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        bg_var = tk.StringVar(value=self.settings["bg_style"])
        ttk.Combobox(inner, textvariable=bg_var, values=BG_STYLES, state="readonly", width=20).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)
        self.vars["bg_style"] = (bg_var, "choice")
        row += 1

        ttk.Label(inner, text="Background color 2 (gradient):").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        bg2_var = tk.StringVar(value=self.settings["bg_color2"])
        ttk.Entry(inner, textvariable=bg2_var, width=22).grid(row=row, column=1, sticky="w", padx=6, pady=3)
        self._add_color_swatch(inner, row, bg2_var, is_ass=False, key="bg_color2")
        self.vars["bg_color2"] = (bg2_var, "str")
        row += 1

        # Caption style
        ttk.Label(inner, text="Caption animation:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        cap_var = tk.StringVar(value=self.settings["caption_style"])
        ttk.Combobox(inner, textvariable=cap_var, values=CAPTION_STYLES, state="readonly", width=20).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)
        self.vars["caption_style"] = (cap_var, "choice")
        row += 1

        # Waveform
        wave_var = tk.BooleanVar(value=self.settings["waveform_enabled"])
        ttk.Checkbutton(inner, text="Show audio waveform overlay", variable=wave_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        self.vars["waveform_enabled"] = (wave_var, "bool")
        row += 1

        ttk.Label(inner, text="Waveform color:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        wave_color_var = tk.StringVar(value=self.settings["waveform_color"])
        ttk.Entry(inner, textvariable=wave_color_var, width=22).grid(row=row, column=1, sticky="w", padx=6, pady=3)
        self._add_color_swatch(inner, row, wave_color_var, is_ass=False, key="waveform_color")
        self.vars["waveform_color"] = (wave_color_var, "str")
        row += 1

        # Intro / outro clips
        intro_row = ttk.Frame(inner)
        intro_row.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=3)
        ttk.Label(intro_row, text="Intro clip:").pack(side=tk.LEFT)
        self.intro_label = ttk.Label(intro_row, text=self._short_path(self.settings["intro_path"]) or "(none)")
        self.intro_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(intro_row, text="Browse…", command=self.choose_intro).pack(side=tk.RIGHT)
        ttk.Button(intro_row, text="Generate…", command=lambda: self.generate_clip("intro")).pack(side=tk.RIGHT, padx=4)
        ttk.Button(intro_row, text="Clear", command=self.clear_intro).pack(side=tk.RIGHT, padx=(0, 4))
        row += 1

        outro_row = ttk.Frame(inner)
        outro_row.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=3)
        ttk.Label(outro_row, text="Outro clip:").pack(side=tk.LEFT)
        self.outro_label = ttk.Label(outro_row, text=self._short_path(self.settings["outro_path"]) or "(none)")
        self.outro_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(outro_row, text="Browse…", command=self.choose_outro).pack(side=tk.RIGHT)
        ttk.Button(outro_row, text="Generate…", command=lambda: self.generate_clip("outro")).pack(side=tk.RIGHT, padx=4)
        ttk.Button(outro_row, text="Clear", command=self.clear_outro).pack(side=tk.RIGHT, padx=(0, 4))
        row += 1

        # Title overlay styling
        ttk.Label(inner, text="Title font:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        title_font_var = tk.StringVar(value=self.settings["title_font"])
        ttk.Combobox(inner, textvariable=title_font_var, values=font_families, width=20).grid(
            row=row, column=1, sticky="w", padx=6, pady=3)
        self.vars["title_font"] = (title_font_var, "str")
        row += 1

        ttk.Label(inner, text="Title font size:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        title_size_var = tk.StringVar(value=str(self.settings["title_font_size"]))
        ttk.Entry(inner, textvariable=title_size_var, width=22).grid(row=row, column=1, sticky="w", padx=6, pady=3)
        self.vars["title_font_size"] = (title_size_var, "int")
        row += 1

        ttk.Label(inner, text="Title color:").grid(row=row, column=0, sticky="w", padx=6, pady=3)
        title_color_var = tk.StringVar(value=self.settings["title_color"])
        ttk.Entry(inner, textvariable=title_color_var, width=22).grid(row=row, column=1, sticky="w", padx=6, pady=3)
        self._add_color_swatch(inner, row, title_color_var, is_ass=False, key="title_color")
        self.vars["title_color"] = (title_color_var, "str")
        row += 1

        ttk.Button(inner, text="Preview Style (3s, no captions)", command=self.run_preview).grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=(12, 6)
        )

    # ------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio", "*.m4a *.mp3 *.wav *.aac *.flac"), ("All files", "*.*")],
        )
        for p in paths:
            self._add_job(p)

    def add_from_folder(self):
        for ext in ("*.m4a", "*.mp3"):
            for p in sorted(HERE.glob(ext)):
                self._add_job(str(p))

    def _add_job(self, path):
        if any(str(j.path) == path for j in self.jobs):
            return
        job = VideoJob(path)
        self.jobs.append(job)
        self.tree.insert("", tk.END, iid=str(len(self.jobs) - 1),
                          values=(job.path.name, job.title, job.status, ""))

    def choose_output_dir(self):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_dir_var.set(path)

    def on_drop_files(self, event):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        for path in self.tk.splitlist(event.data):
            p = Path(path)
            if p.is_dir():
                for ext in ("*.m4a", "*.mp3"):
                    for f in sorted(p.glob(ext)):
                        self._add_job(str(f))
            elif p.suffix.lower() in (".m4a", ".mp3", ".wav", ".aac", ".flac"):
                self._add_job(str(p))

    def edit_title(self, event):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id or col != "#2":  # title column
            return
        idx = int(row_id)
        import tkinter.simpledialog as sd
        new_title = sd.askstring("Episode Title", "Title overlay for this episode (blank = none):",
                                  initialvalue=self.jobs[idx].title)
        if new_title is not None:
            self.jobs[idx].title = new_title.strip()
            self._update_job_row(idx)

    def remove_selected(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Cannot edit the queue while rendering.")
            return
        sel = self.tree.selection()
        indices = sorted((int(i) for i in sel), reverse=True)
        for i in indices:
            del self.jobs[i]
        self._rebuild_tree()

    def clear_queue(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Cannot edit the queue while rendering.")
            return
        self.jobs.clear()
        self._rebuild_tree()

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        for idx, job in enumerate(self.jobs):
            self.tree.insert("", tk.END, iid=str(idx),
                              values=(job.path.name, job.title, job.status, f"{job.progress:.0f}%"))

    def _update_job_row(self, idx, status=None, progress=None):
        job = self.jobs[idx]
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        self.tree.item(str(idx), values=(job.path.name, job.title, job.status, f"{job.progress:.0f}%"))

    # ------------------------------------------------------------
    # Presets / templates
    # ------------------------------------------------------------
    def _seed_builtin_presets(self):
        for name, data in BUILTIN_PRESETS.items():
            path = PRESETS_DIR / f"{name}.json"
            if not path.exists():
                path.write_text(json.dumps(data, indent=2))

    def _refresh_preset_list(self):
        names = sorted(p.stem for p in PRESETS_DIR.glob("*.json"))
        self.preset_combo["values"] = names

    def _load_default_preset_if_exists(self):
        default = PRESETS_DIR / "default.json"
        if default.exists():
            self._apply_preset_file(default)
            self.preset_var.set("default")

    def on_preset_selected(self, event=None):
        name = self.preset_var.get()
        if not name:
            return
        self._apply_preset_file(PRESETS_DIR / f"{name}.json")

    def _apply_preset_file(self, file_path):
        try:
            data = json.loads(Path(file_path).read_text())
        except Exception as e:
            messagebox.showerror("Preset error", f"Could not load preset:\n{e}")
            return
        self.settings.update(data)
        self.art_label.config(text=self._short_path(self.settings.get("art", "")))
        for key, (var, kind) in self.vars.items():
            if key in self.settings:
                if kind == "bool":
                    var.set(bool(self.settings[key]))
                else:
                    var.set(str(self.settings[key]))
        self.intro_label.config(text=self._short_path(self.settings.get("intro_path", "")) or "(none)")
        self.outro_label.config(text=self._short_path(self.settings.get("outro_path", "")) or "(none)")
        for key, (swatch, is_ass) in self.color_swatches.items():
            value = self.settings.get(key, "")
            swatch.config(bg=ass_to_rgb_hex(value) if is_ass else to_tk_color(value))
        self._refresh_art_preview()

    def save_preset_as(self):
        if not self._sync_settings_from_ui():
            return
        name = tk.simpledialog.askstring("Save Preset", "Preset name:") if hasattr(tk, "simpledialog") else None
        if name is None:
            import tkinter.simpledialog as sd
            name = sd.askstring("Save Preset", "Preset name:")
        if not name:
            return
        path = PRESETS_DIR / f"{name}.json"
        path.write_text(json.dumps(self.settings, indent=2))
        self._refresh_preset_list()
        self.preset_var.set(name)
        self._log(f"Saved preset '{name}'")

    def delete_preset(self):
        name = self.preset_var.get()
        if not name:
            return
        if not messagebox.askyesno("Delete Preset", f"Delete preset '{name}'?"):
            return
        path = PRESETS_DIR / f"{name}.json"
        path.unlink(missing_ok=True)
        self.preset_var.set("")
        self._refresh_preset_list()

    # ------------------------------------------------------------
    # Art picker / preview
    # ------------------------------------------------------------
    @staticmethod
    def _short_path(path, max_len=40):
        if len(path) <= max_len:
            return path
        return "…" + path[-(max_len - 1):]

    def choose_art(self):
        path = filedialog.askopenfilename(
            title="Choose podcast art",
            filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
        )
        if not path:
            return
        self.settings["art"] = path
        self.art_label.config(text=self._short_path(path))
        self._refresh_art_preview()

    def choose_intro(self):
        path = filedialog.askopenfilename(title="Choose intro clip",
                                           filetypes=[("Video", "*.mp4 *.mov *.mkv"), ("All files", "*.*")])
        if path:
            self.settings["intro_path"] = path
            self.intro_label.config(text=self._short_path(path))

    def clear_intro(self):
        self.settings["intro_path"] = ""
        self.intro_label.config(text="(none)")

    def choose_outro(self):
        path = filedialog.askopenfilename(title="Choose outro clip",
                                           filetypes=[("Video", "*.mp4 *.mov *.mkv"), ("All files", "*.*")])
        if path:
            self.settings["outro_path"] = path
            self.outro_label.config(text=self._short_path(path))

    def clear_outro(self):
        self.settings["outro_path"] = ""
        self.outro_label.config(text="(none)")

    def _add_color_swatch(self, parent, row, var, is_ass, key=None):
        """Place a color-swatch button next to a color field at (row, col 2) that opens a color picker."""
        swatch = tk.Button(parent, width=3, relief="raised",
                            bg=to_tk_color(var.get()) if not is_ass else ass_to_rgb_hex(var.get()))
        if key:
            self.color_swatches[key] = (swatch, is_ass)

        def pick(event=None):
            current = ass_to_rgb_hex(var.get()) if is_ass else to_tk_color(var.get())
            try:
                _, hex_color = colorchooser.askcolor(color=current, parent=self)
            except tk.TclError:
                _, hex_color = colorchooser.askcolor(parent=self)
            if hex_color:
                if is_ass:
                    var.set(rgb_hex_to_ass(hex_color, var.get()))
                else:
                    var.set(hex_color)
                swatch.config(bg=hex_color)

        swatch.config(command=pick)
        swatch.grid(row=row, column=2, sticky="w", padx=(0, 6), pady=3)

    def generate_clip(self, kind):
        """Open a small dialog to generate an intro/outro clip using the current glow/background style (no art image)."""
        if not self._sync_settings_from_ui():
            return
        s = self.settings

        dialog = tk.Toplevel(self)
        dialog.title(f"Generate {kind.title()} Clip")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Text to display (multiple lines ok):").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        text_widget = tk.Text(dialog, width=40, height=4)
        text_widget.insert("1.0", "Verse by Verse" if kind == "intro" else "Thanks for listening!")
        text_widget.grid(row=1, column=0, padx=8, pady=2)

        ttk.Label(dialog, text="Duration (seconds):").grid(row=2, column=0, sticky="w", padx=8, pady=(8, 2))
        dur_var = tk.StringVar(value="4")
        ttk.Entry(dialog, textvariable=dur_var, width=10).grid(row=3, column=0, sticky="w", padx=8, pady=2)

        status_label = ttk.Label(dialog, text="")
        status_label.grid(row=5, column=0, padx=8, pady=(4, 0))

        def do_generate():
            try:
                duration = float(dur_var.get())
            except ValueError:
                messagebox.showerror("Invalid duration", "Enter a number of seconds.")
                return
            text = text_widget.get("1.0", "end-1c").strip()
            generate_btn.config(state=tk.DISABLED)
            status_label.config(text="Generating…")

            def work():
                ffmpeg = find_ffmpeg()
                out_path = CLIPS_DIR / f"{kind}_{int(__import__('time').time())}.mp4"

                glow_loop = Path(tempfile.gettempdir()) / "vbvn_clipgen_glow.mp4"
                half = s["pulse_speed"] / 2
                glow_src, glow_pad = 600, 700
                canvas = glow_src + glow_pad * 2
                cmd1 = [
                    ffmpeg, "-f", "lavfi",
                    "-i", f"color=c={s['glow_color']}:s={glow_src}x{glow_src}:r={s['fps']}",
                    "-filter_complex",
                    f"[0:v]pad={canvas}:{canvas}:{glow_pad}:{glow_pad}:black,"
                    f"gblur=sigma={s['glow_sigma']},"
                    f"fade=t=in:st=0:d={half}:color=black,"
                    f"fade=t=out:st={half}:d={half}:color=black[out]",
                    "-map", "[out]", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-t", str(s["pulse_speed"]), str(glow_loop), "-y", "-loglevel", "error",
                ]
                r = subprocess.run(cmd1, capture_output=True, text=True)
                if r.returncode != 0:
                    self.after(0, lambda: status_label.config(text="Failed (glow) — see log"))
                    self._log(f"Clip generation failed (glow):\n{r.stderr[-1500:]}")
                    self.after(0, lambda: generate_btn.config(state=tk.NORMAL))
                    return

                bg_chain = build_bg_chain(s, s["fps"])

                text_filter = ""
                if text:
                    escaped = text.replace("\\", "\\\\\\\\").replace(":", "\\:").replace("'", "\\'")
                    text_filter = (
                        f",drawtext=text='{escaped}':font={s['title_font']}:"
                        f"fontsize={s['title_font_size']}:fontcolor={s['title_color']}:"
                        f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.4:boxborderw=12"
                    )

                filter_complex = (
                    f"[0:v]format=rgb24[glow_pulsed];"
                    f"{bg_chain};"
                    f"[bg][glow_pulsed]overlay=(W-w)/2:(H-h)/2:format=auto"
                    f"{text_filter},"
                    f"fade=t=in:st=0:d=0.5,fade=t=out:st={duration - 0.5}:d=0.5[out]"
                )

                cmd2 = [
                    ffmpeg,
                    "-stream_loop", "-1", "-i", str(glow_loop),
                    "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-filter_complex", filter_complex,
                    "-map", "[out]", "-map", "1:a",
                    "-t", str(duration),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                    "-movflags", "+faststart",
                    str(out_path), "-y", "-loglevel", "error",
                ]
                r = subprocess.run(cmd2, capture_output=True, text=True)
                glow_loop.unlink(missing_ok=True)
                if r.returncode != 0:
                    self.after(0, lambda: status_label.config(text="Failed — see log"))
                    self._log(f"Clip generation failed:\n{r.stderr[-1500:]}")
                    self.after(0, lambda: generate_btn.config(state=tk.NORMAL))
                    return

                def finish():
                    s[f"{kind}_path"] = str(out_path)
                    label = self.intro_label if kind == "intro" else self.outro_label
                    label.config(text=self._short_path(str(out_path)))
                    self._log(f"Generated {kind} clip: {out_path.name}")
                    dialog.destroy()

                self.after(0, finish)

            threading.Thread(target=work, daemon=True).start()

        generate_btn = ttk.Button(dialog, text="Generate", command=do_generate)
        generate_btn.grid(row=4, column=0, pady=8)

    def _refresh_art_preview(self):
        if Image is None:
            return
        art_path = self.settings.get("art", "")
        candidate = Path(art_path)
        if not candidate.is_absolute():
            candidate = HERE / art_path
        if not candidate.exists():
            self.art_preview.config(image="", text="(art not found)")
            return
        try:
            img = Image.open(candidate)
            img.thumbnail((180, 180))
            self._art_imgtk = ImageTk.PhotoImage(img)
            self.art_preview.config(image=self._art_imgtk, text="")
        except Exception:
            self.art_preview.config(image="", text="(preview unavailable)")

    # ------------------------------------------------------------
    # Settings sync / validation
    # ------------------------------------------------------------
    def _sync_settings_from_ui(self):
        for key, (var, kind) in self.vars.items():
            if kind == "bool":
                self.settings[key] = bool(var.get())
                continue
            raw = var.get().strip()
            try:
                if kind == "int":
                    self.settings[key] = int(raw)
                elif kind == "float":
                    self.settings[key] = float(raw)
                else:
                    self.settings[key] = raw
            except ValueError:
                messagebox.showerror("Invalid value", f"'{raw}' is not valid for {key}")
                return False
        return True

    # ------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------
    def _log(self, msg):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.after(0, append)

    # ------------------------------------------------------------
    # Preview render (quick, no captions/transcription)
    # ------------------------------------------------------------
    def run_preview(self):
        if not self._sync_settings_from_ui():
            return
        s = self.settings
        art_path = Path(s["art"])
        if not art_path.is_absolute():
            art_path = HERE / art_path
        if not art_path.exists():
            messagebox.showerror("Art missing", f"Art image not found:\n{art_path}")
            return

        def work():
            self._log("Generating style preview…")
            ffmpeg = find_ffmpeg()
            tmpdir = Path(tempfile.mkdtemp(prefix="vbvn_preview_"))
            glow_loop = tmpdir / "glow.mp4"
            out_path = tmpdir / "preview.mp4"

            half = s["pulse_speed"] / 2
            glow_src, glow_pad = 600, 700
            canvas = glow_src + glow_pad * 2
            breathe_frames = max(1, int(s["fps"] * s["pulse_speed"]))

            cmd1 = [
                ffmpeg, "-f", "lavfi",
                "-i", f"color=c={s['glow_color']}:s={glow_src}x{glow_src}:r={s['fps']}",
                "-filter_complex",
                f"[0:v]pad={canvas}:{canvas}:{glow_pad}:{glow_pad}:black,"
                f"gblur=sigma={s['glow_sigma']},"
                f"fade=t=in:st=0:d={half}:color=black,"
                f"fade=t=out:st={half}:d={half}:color=black[out]",
                "-map", "[out]", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-t", str(s["pulse_speed"]), str(glow_loop), "-y", "-loglevel", "error",
            ]
            r = subprocess.run(cmd1, capture_output=True, text=True)
            if r.returncode != 0:
                self._log("Preview failed (glow):\n" + r.stderr[-2000:])
                return

            art_size = s["art_size"]
            bg_chain = build_bg_chain(s, s["fps"])
            cmd2 = [
                ffmpeg,
                "-loop", "1", "-framerate", str(s["fps"]), "-i", str(art_path),
                "-stream_loop", "-1", "-i", str(glow_loop),
                "-filter_complex",
                f"[0:v]scale={art_size+20}:{art_size+20},"
                f"zoompan=z='1+{s['breathe_amount']}*sin(2*PI*on/{breathe_frames})':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                f"s={art_size+20}x{art_size+20}:fps={s['fps']},"
                f"scale={art_size}:{art_size},format=rgb24[art_sharp];"
                f"[1:v]format=rgb24[glow_pulsed];"
                f"{bg_chain};"
                f"[bg][glow_pulsed]overlay=(W-w)/2:(H-h)/2:format=auto[bg_glow];"
                f"[bg_glow][art_sharp]overlay=(W-w)/2:(H-h)/2:format=auto[out]",
                "-map", "[out]", "-t", str(s["pulse_speed"]),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                str(out_path), "-y", "-loglevel", "error",
            ]
            r = subprocess.run(cmd2, capture_output=True, text=True)
            if r.returncode != 0:
                self._log("Preview failed (compose):\n" + r.stderr[-2000:])
                return

            self._log(f"Preview ready: {out_path}")
            self._open_file(out_path)

        threading.Thread(target=work, daemon=True).start()

    def _open_file(self, path):
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)])
            elif sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(f"Could not open preview automatically: {e}")

    # ------------------------------------------------------------
    # Full render pipeline
    # ------------------------------------------------------------
    def start_render(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not self.jobs:
            messagebox.showinfo("No files", "Add some audio files to the queue first.")
            return
        if not self._sync_settings_from_ui():
            return

        art_path = Path(self.settings["art"])
        if not art_path.is_absolute():
            art_path = HERE / art_path
        if not art_path.exists():
            messagebox.showerror("Art missing", f"Art image not found:\n{art_path}")
            return

        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            output_dir_path = Path(output_dir)
            output_dir_path.mkdir(parents=True, exist_ok=True)
        else:
            output_dir_path = None

        self.cancel_flag.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.worker_thread = threading.Thread(target=self._render_all, args=(art_path, output_dir_path), daemon=True)
        self.worker_thread.start()

    def cancel_render(self):
        self.cancel_flag.set()
        self._log("Cancel requested — will stop after current file.")

    def _finish_render(self):
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

    def _run_ffmpeg_with_progress(self, cmd, total_seconds, idx, stage, base_pct, span_pct):
        """Run an ffmpeg command with -progress pipe:1, updating job progress live."""
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
                    self._update_job_row(idx, status=stage, progress=base_pct + span_pct * frac)
                except (ValueError, ZeroDivisionError):
                    pass

        proc.wait()
        stderr_thread.join()
        return proc.returncode, "".join(stderr_chunks)

    def _run_transcription_with_progress(self, python_bin, env, script, total_seconds, idx, base_pct, span_pct):
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
                    self._update_job_row(idx, status="Transcribing…", progress=base_pct + span_pct * frac)
                except (ValueError, ZeroDivisionError):
                    pass

        proc.wait()
        stderr_thread.join()
        stderr = "".join(stderr_chunks)
        return proc.returncode, stderr

    def _reencode_clip(self, ffmpeg, ffprobe, src, dst, s):
        """Re-encode an intro/outro clip to match the main render's spec so concat works.
        Adds a silent audio track if the source has none, so the concat output keeps audio."""
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True,
        )
        has_audio = bool(probe.stdout.strip())

        vf = (f"scale={s['width']}:{s['height']}:force_original_aspect_ratio=decrease,"
              f"pad={s['width']}:{s['height']}:(ow-iw)/2:(oh-ih)/2,fps={s['fps']}")

        if has_audio:
            cmd = [
                ffmpeg, "-i", str(src),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                str(dst), "-y", "-loglevel", "error",
            ]
        else:
            cmd = [
                ffmpeg, "-i", str(src),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", vf, "-shortest",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                str(dst), "-y", "-loglevel", "error",
            ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0, r.stderr

    def _add_intro_outro(self, ffmpeg, ffprobe, s, output):
        tmpdir = Path(tempfile.gettempdir())
        parts = []

        if s.get("intro_path"):
            intro_re = tmpdir / "vbvn_intro.mp4"
            success, err = self._reencode_clip(ffmpeg, ffprobe, s["intro_path"], intro_re, s)
            if success:
                parts.append(intro_re)
            else:
                self._log(f"Intro clip failed to re-encode, skipping:\n{err[-800:]}")

        main_re = tmpdir / "vbvn_main.mp4"
        success, err = self._reencode_clip(ffmpeg, ffprobe, output, main_re, s)
        if not success:
            self._log(f"Main clip re-encode for concat failed, skipping intro/outro:\n{err[-800:]}")
            return
        parts.append(main_re)

        if s.get("outro_path"):
            outro_re = tmpdir / "vbvn_outro.mp4"
            success, err = self._reencode_clip(ffmpeg, ffprobe, s["outro_path"], outro_re, s)
            if success:
                parts.append(outro_re)
            else:
                self._log(f"Outro clip failed to re-encode, skipping:\n{err[-800:]}")

        if len(parts) < 2:
            return

        final = output.with_suffix(".concat.mp4")
        cmd = [ffmpeg]
        for p in parts:
            cmd += ["-i", str(p)]
        n = len(parts)
        concat_inputs = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
        filter_complex = f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]"
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(final), "-y", "-loglevel", "error",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            shutil.move(str(final), str(output))
        else:
            self._log(f"Concat with intro/outro failed:\n{r.stderr[-1000:]}")

        for p in parts:
            p.unlink(missing_ok=True)

    def _render_all(self, art_path, output_dir_path=None):
        s = self.settings
        ffmpeg = find_ffmpeg()
        ffprobe = find_ffmpeg("ffprobe")
        python_bin = find_python()

        glow_loop = Path(tempfile.gettempdir()) / "vbvn_glow_loop.mp4"
        self._log("Generating glow loop…")
        half = s["pulse_speed"] / 2
        glow_src, glow_pad = 600, 700
        canvas = glow_src + glow_pad * 2
        cmd = [
            ffmpeg, "-f", "lavfi",
            "-i", f"color=c={s['glow_color']}:s={glow_src}x{glow_src}:r={s['fps']}",
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
            self._log("Failed to build glow loop:\n" + r.stderr[-2000:])
            self.after(0, self._finish_render)
            return

        breathe_frames = max(1, int(s["fps"] * s["pulse_speed"]))

        for idx, job in enumerate(self.jobs):
            if self.cancel_flag.is_set():
                self._update_job_row(idx, status="Cancelled")
                continue

            audio = job.path
            base = audio.stem
            out_dir = output_dir_path if output_dir_path is not None else audio.parent
            output = out_dir / (base + ".mp4")
            ass_path = audio.with_name(base + ".ass")

            # duration (needed up front to estimate progress)
            dur_r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
                capture_output=True, text=True,
            )
            duration = dur_r.stdout.strip()
            if not duration:
                self._log(f"[{audio.name}] Could not read duration — skipping.")
                self._update_job_row(idx, status="Failed (duration)", progress=0)
                continue
            duration_f = float(duration)

            self._update_job_row(idx, status="Transcribing…", progress=0)
            self._log(f"[{audio.name}] Transcribing…")

            script = TRANSCRIBE_TEMPLATE.substitute(
                AUDIO=str(audio), CHUNK_SIZE=s["words_per_chunk"],
                WIDTH=s["width"], HEIGHT=s["height"],
                FONT_NAME=s["font_name"], FONT_SIZE=s["font_size"],
                CAPTION_HIGHLIGHT=s["caption_highlight"], CAPTION_COLOR=s["caption_color"],
                CAPTION_BACK=s["caption_back"], CAPTION_Y=s["caption_y"],
                CAPTION_STYLE=s["caption_style"],
                ASS_PATH=str(ass_path),
            )
            env = dict(os.environ)
            env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            rc, stderr = self._run_transcription_with_progress(
                python_bin, env, script, duration_f, idx, base_pct=0, span_pct=50)
            if rc != 0 or not ass_path.exists():
                self._log(f"[{audio.name}] Transcription failed:\n{stderr[-1500:]}")
                sub_filter = ""
            else:
                tmp_ass = Path(tempfile.gettempdir()) / "vbvn_captions.ass"
                shutil.copy(ass_path, tmp_ass)
                sub_filter = f",subtitles={tmp_ass.as_posix()}"

            self._update_job_row(idx, status="Rendering…", progress=50)
            self._log(f"[{audio.name}] Rendering video…")

            tmp_audio = Path(tempfile.gettempdir()) / ("vbvn_audio_input" + audio.suffix)
            shutil.copy(audio, tmp_audio)

            art_size, height = s["art_size"], s["height"]
            tmp_out = output.with_suffix(".mp4.tmp.mp4")
            bg_chain = build_bg_chain(s, s["fps"])

            title_filter = ""
            if job.title:
                escaped = job.title.replace("\\", "\\\\\\\\").replace(":", "\\:").replace("'", "\\'")
                title_filter = (
                    f",drawtext=text='{escaped}':font={s['title_font']}:"
                    f"fontsize={s['title_font_size']}:fontcolor={s['title_color']}:"
                    f"x=(w-text_w)/2:y=60:box=1:boxcolor=black@0.4:boxborderw=12"
                )

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
                    f"[bg][glow_pulsed]overlay=(W-w)/2:(H-h)/2:format=auto[bg_glow];"
                    f"[bg_glow][art_sharp]overlay=(W-w)/2:(H-h)/2:format=auto[bg_art];"
                    f"[bg_art][wave]overlay=(W-w)/2:H-h-40:format=auto{sub_filter}{title_filter}[out]"
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
                    f"[bg][glow_pulsed]overlay=(W-w)/2:(H-h)/2:format=auto[bg_glow];"
                    f"[bg_glow][art_sharp]overlay=(W-w)/2:(H-h)/2:format=auto{sub_filter}{title_filter}[out]"
                )
            cmd = [
                ffmpeg,
                "-loop", "1", "-framerate", str(s["fps"]), "-i", str(art_path),
                "-stream_loop", "-1", "-i", str(glow_loop),
                "-i", str(tmp_audio),
                "-filter_complex", filter_complex,
                "-map", "[out]", "-map", "2:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-t", duration,
                "-movflags", "+faststart",
                "-loglevel", "warning",
                str(tmp_out), "-y",
            ]
            rc, stderr = self._run_ffmpeg_with_progress(
                cmd, duration_f, idx, "Rendering…", base_pct=50, span_pct=40)
            if rc != 0:
                self._log(f"[{audio.name}] Render failed:\n{stderr[-1500:]}")
                self._update_job_row(idx, status="Failed (render)", progress=0)
                tmp_out.unlink(missing_ok=True)
                continue

            self._update_job_row(idx, status="Trimming…", progress=90)

            adur_r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(tmp_out)],
                capture_output=True, text=True,
            )
            audio_dur = adur_r.stdout.strip()

            trimmed_ok = False
            if audio_dur:
                rc, stderr = self._run_ffmpeg_with_progress(
                    [ffmpeg, "-i", str(tmp_out), "-t", audio_dur,
                     "-c", "copy", "-movflags", "+faststart",
                     str(output), "-y", "-loglevel", "error"],
                    float(audio_dur), idx, "Trimming…", base_pct=90, span_pct=10)
                trimmed_ok = rc == 0

            if trimmed_ok:
                tmp_out.unlink(missing_ok=True)
            else:
                shutil.move(str(tmp_out), str(output))
                self._log(f"[{audio.name}] Trim step failed — using untrimmed output.")

            if s.get("intro_path") or s.get("outro_path"):
                self._add_intro_outro(ffmpeg, ffprobe, s, output)

            size_mb = output.stat().st_size / (1024 * 1024)
            self._update_job_row(idx, status="Done", progress=100)
            self._log(f"[{audio.name}] Done → {output.name} ({size_mb:.1f} MB)")

        glow_loop.unlink(missing_ok=True)
        self._log("All done." if not self.cancel_flag.is_set() else "Stopped.")
        self.after(0, self._finish_render)


if __name__ == "__main__":
    import tkinter.simpledialog  # noqa: F401  (ensure available for save_preset_as)
    app = App()
    app.mainloop()
