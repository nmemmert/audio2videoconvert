#!/usr/bin/env python3
"""
Verse by Verse — Podcast Video Generator GUI
Desktop front-end that matches the web app feature set:
  - All visual settings (art, background, glow, watermark, bg video)
  - All caption settings with presets
  - Intro / outro / chapter cards / end card / outline via Ollama or docx script
  - Queue of audio files with per-file progress; batch rendering via render_engine
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
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

import render_engine as eng

HERE = Path(__file__).resolve().parent
PRESETS_DIR = HERE / "presets"
PRESETS_DIR.mkdir(exist_ok=True)
CLIPS_DIR = HERE / "generated_clips"
CLIPS_DIR.mkdir(exist_ok=True)
ART_DIR = HERE / "art"
ART_DIR.mkdir(exist_ok=True)

# Re-export from render_engine so rest of file uses short names
DEFAULTS = eng.DEFAULTS
BUILTIN_PRESETS = eng.BUILTIN_PRESETS
BG_STYLES = eng.BG_STYLES
CAPTION_STYLES = eng.CAPTION_STYLES

HEX_COLOR_FIELDS = {"bg_color", "glow_color", "bg_color2", "waveform_color", "title_color", "outline_color"}
ASS_COLOR_FIELDS = {"caption_color", "caption_highlight", "caption_back"}
FONT_FIELDS = {"font_name", "font_fallback", "title_font", "outline_font"}

RESOLUTION_PRESETS = {
    "1080p — 1920×1080": (1920, 1080),
    "720p — 1280×720":   (1280, 720),
    "4K — 3840×2160":    (3840, 2160),
    "Vertical 9:16 — 1080×1920": (1080, 1920),
    "Square — 1080×1080": (1080, 1080),
    "Custom": None,
}

GLOW_PRESETS = {
    "Custom":  None,
    "Subtle":  {"glow_sigma": 80,  "pulse_speed": 3,   "breathe_amount": 0.01},
    "Soft":    {"glow_sigma": 140, "pulse_speed": 3,   "breathe_amount": 0.015},
    "Medium":  {"glow_sigma": 220, "pulse_speed": 3,   "breathe_amount": 0.02},
    "Strong":  {"glow_sigma": 300, "pulse_speed": 2,   "breathe_amount": 0.03},
    "Intense": {"glow_sigma": 400, "pulse_speed": 2,   "breathe_amount": 0.04},
}

FPS_OPTIONS = ["24", "25", "30", "60"]

ART_SIZE_PRESETS = {
    "Custom": None,
    "Small (400 px)":  400,
    "Medium (600 px)": 600,
    "Large (700 px)":  700,
    "XL (900 px)":     900,
}

CAPTION_Y_PRESETS = {
    "Custom":              None,
    "Bottom (900)":        900,
    "Lower-third (750)":   750,
    "Center (540)":        540,
}

ART_X_PRESETS = {
    "Center (0)":         0,
    "Slight right (+100)": 100,
    "Hard right (+200)":  200,
    "Slight left (−100)": -100,
    "Custom":             None,
}


# ─── Color helpers ───────────────────────────────────────────────────────────

def ass_to_rgb_hex(ass):
    try:
        digits = ass.upper().replace("&H", "").zfill(8)
        bb, gg, rr = digits[2:4], digits[4:6], digits[6:8]
        return f"#{rr}{gg}{bb}"
    except Exception:
        return "#000000"


def rgb_hex_to_ass(rgb_hex, original_ass):
    try:
        alpha = original_ass.upper().replace("&H", "").zfill(8)[0:2]
    except Exception:
        alpha = "00"
    rgb_hex = rgb_hex.lstrip("#")
    rr, gg, bb = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"&H{alpha}{bb}{gg}{rr}".upper()


def to_tk_color(value):
    if not value:
        return "#000000"
    if value.startswith("&H"):
        return ass_to_rgb_hex(value)
    if value.startswith("#"):
        return value
    return {"black": "#000000", "white": "#ffffff"}.get(value, "#000000")


def _short_path(path, max_len=42):
    s = str(path)
    return s if len(s) <= max_len else "…" + s[-(max_len - 1):]


# ─── Job data class ──────────────────────────────────────────────────────────

class VideoJob:
    def __init__(self, path):
        self.path = Path(path)
        self.status = "Queued"
        self.progress = 0.0
        self.title = ""
        self.script_path = None  # optional .docx for outline per job


# ─── Main app ────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.dnd_available = False
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_available = True
            except Exception:
                pass
        self.title("Verse by Verse — Podcast Video Generator")
        self.geometry("1500x880")
        self.minsize(1100, 720)

        self.settings = dict(DEFAULTS)
        self.color_swatches = {}
        self.jobs = []
        self.worker_thread = None
        self.cancel_flag = threading.Event()

        self._seed_builtin_presets()
        self._build_ui()
        self._load_default_preset_if_exists()
        self._refresh_art_preview()

    # ── Presets ──────────────────────────────────────────────────────────────

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
        elif (PRESETS_DIR / "Gold Glow (Default).json").exists():
            self._apply_preset_file(PRESETS_DIR / "Gold Glow (Default).json")
            self.preset_var.set("Gold Glow (Default)")

    def on_preset_selected(self, event=None):
        name = self.preset_var.get()
        if name:
            self._apply_preset_file(PRESETS_DIR / f"{name}.json")

    def _apply_preset_file(self, file_path):
        try:
            data = json.loads(Path(file_path).read_text())
        except Exception as e:
            messagebox.showerror("Preset error", f"Could not load preset:\n{e}")
            return
        self.settings.update(data)
        self._apply_settings_to_ui()

    def _apply_settings_to_ui(self):
        s = self.settings
        # Art
        self.art_label.config(text=_short_path(s.get("art", "")))
        self._refresh_art_preview()

        # Simple vars
        for key, (var, kind) in self.vars.items():
            if key not in s:
                continue
            if kind == "bool":
                var.set(bool(s[key]))
            else:
                var.set(str(s[key]))

        # Color swatches
        for key, (swatch, is_ass) in self.color_swatches.items():
            value = str(s.get(key, ""))
            try:
                swatch.config(bg=ass_to_rgb_hex(value) if is_ass else to_tk_color(value))
            except Exception:
                pass

        # Intro / outro / bg_video / watermark labels
        self.intro_label.config(text=_short_path(s.get("intro_path", "")) or "(none)")
        self.outro_label.config(text=_short_path(s.get("outro_path", "")) or "(none)")
        self.bgvideo_label.config(text=_short_path(s.get("bg_video_path", "")) or "(none)")
        self.watermark_label.config(text=_short_path(s.get("watermark_path", "")) or "(none)")

        # Compound preset dropdowns
        self._sync_resolution_preset()
        self._sync_glow_preset()
        self._sync_art_size_preset()
        self._sync_caption_y_preset()
        self._sync_art_x_preset()

    def save_preset_as(self):
        if not self._sync_settings_from_ui():
            return
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
        (PRESETS_DIR / f"{name}.json").unlink(missing_ok=True)
        self.preset_var.set("")
        self._refresh_preset_list()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        outer = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(outer)
        right = ttk.Frame(outer)
        outer.add(left, weight=2)
        outer.add(right, weight=3)

        self._build_queue_panel(left)
        self._build_settings_panel(right)

    # ── Queue / files panel ───────────────────────────────────────────────────

    def _build_queue_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Audio Queue")
        frame.pack(fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(btn_row, text="Add Files…", command=self.add_files).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Add From Folder", command=self.add_from_folder).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_row, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Clear", command=self.clear_queue).pack(side=tk.LEFT, padx=6)

        out_row = ttk.Frame(frame)
        out_row.pack(fill=tk.X, padx=6, pady=(0, 4))
        ttk.Label(out_row, text="Output folder:").pack(side=tk.LEFT)
        self.output_dir_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(out_row, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(out_row, text="Browse…", command=self.choose_output_dir).pack(side=tk.LEFT)
        ttk.Button(out_row, text="Clear", command=lambda: self.output_dir_var.set("")).pack(side=tk.LEFT, padx=(4, 0))

        script_row = ttk.Frame(frame)
        script_row.pack(fill=tk.X, padx=6, pady=(0, 4))
        ttk.Label(script_row, text="Episode script (.docx):").pack(side=tk.LEFT)
        self.script_label = ttk.Label(script_row, text="(none)", foreground="gray")
        self.script_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(script_row, text="Browse…", command=self.choose_script).pack(side=tk.LEFT)
        ttk.Button(script_row, text="Clear", command=self.clear_script).pack(side=tk.LEFT, padx=(4, 0))
        self._global_script_path = None

        columns = ("file", "title", "status", "progress")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("file",     text="Audio File")
        self.tree.heading("title",    text="Episode Title (dbl-click to edit)")
        self.tree.heading("status",   text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.column("file",     width=240)
        self.tree.column("title",    width=200)
        self.tree.column("status",   width=140)
        self.tree.column("progress", width=80, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
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
        self.preview_btn = ttk.Button(run_row, text="👁 Preview Style (3s)", command=self.run_preview)
        self.preview_btn.pack(side=tk.LEFT, padx=6)
        self.cancel_btn = ttk.Button(run_row, text="■ Cancel", command=self.cancel_render, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(frame, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Settings panel (tabbed) ───────────────────────────────────────────────

    def _build_settings_panel(self, parent):
        # Presets row at top
        preset_frame = ttk.LabelFrame(parent, text="Presets")
        preset_frame.pack(fill=tk.X, pady=(0, 6))
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(preset_frame, textvariable=self.preset_var, state="readonly")
        self.preset_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=6)
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self._refresh_preset_list()
        ttk.Button(preset_frame, text="Save As…", command=self.save_preset_as).pack(side=tk.LEFT, padx=4)
        ttk.Button(preset_frame, text="Delete", command=self.delete_preset).pack(side=tk.LEFT, padx=(0, 6))

        # Notebook for settings tabs
        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)

        self.vars = {}

        self._build_tab_visual(nb)
        self._build_tab_captions(nb)
        self._build_tab_clips(nb)
        self._build_tab_extras(nb)

    # ── Tab: Visual ───────────────────────────────────────────────────────────

    def _build_tab_visual(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text="Visual")

        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        font_families = sorted(set(tkfont.families()))
        row = 0

        # ── Art ──────────────────────────────────────────────────────────────
        art_fs = ttk.LabelFrame(inner, text="Art")
        art_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        inner.columnconfigure(0, weight=1)
        row += 1

        art_top = ttk.Frame(art_fs)
        art_top.pack(fill=tk.X, padx=4, pady=4)
        ttk.Label(art_top, text="Cover art image:").pack(side=tk.LEFT)
        self.art_label = ttk.Label(art_top, text=_short_path(self.settings["art"]))
        self.art_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(art_top, text="Browse…", command=self.choose_art).pack(side=tk.RIGHT)

        preview_holder = ttk.Frame(art_fs, width=160, height=160)
        preview_holder.pack(padx=4, pady=4)
        preview_holder.pack_propagate(False)
        self.art_preview = ttk.Label(preview_holder)
        self.art_preview.place(relx=0.5, rely=0.5, anchor="center")

        art_size_row = ttk.Frame(art_fs)
        art_size_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(art_size_row, text="Size:").pack(side=tk.LEFT)
        self.art_size_preset_var = tk.StringVar(value="Large (700 px)")
        art_size_preset_cb = ttk.Combobox(art_size_row, textvariable=self.art_size_preset_var,
                                           values=list(ART_SIZE_PRESETS.keys()), state="readonly", width=16)
        art_size_preset_cb.pack(side=tk.LEFT, padx=4)
        art_size_preset_cb.bind("<<ComboboxSelected>>", self._on_art_size_preset)
        art_size_var = tk.StringVar(value=str(self.settings["art_size"]))
        ttk.Entry(art_size_row, textvariable=art_size_var, width=6).pack(side=tk.LEFT)
        art_size_var.trace_add("write", lambda *_: self._sync_art_size_preset())
        self.vars["art_size"] = (art_size_var, "int")

        art_x_row = ttk.Frame(art_fs)
        art_x_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(art_x_row, text="Horizontal offset:").pack(side=tk.LEFT)
        self.art_x_preset_var = tk.StringVar(value="Center (0)")
        art_x_preset_cb = ttk.Combobox(art_x_row, textvariable=self.art_x_preset_var,
                                        values=list(ART_X_PRESETS.keys()), state="readonly", width=18)
        art_x_preset_cb.pack(side=tk.LEFT, padx=4)
        art_x_preset_cb.bind("<<ComboboxSelected>>", self._on_art_x_preset)
        art_x_var = tk.StringVar(value=str(self.settings.get("art_x_offset", 0)))
        ttk.Entry(art_x_row, textvariable=art_x_var, width=6).pack(side=tk.LEFT)
        art_x_var.trace_add("write", lambda *_: self._sync_art_x_preset())
        self.vars["art_x_offset"] = (art_x_var, "int")

        # ── Background ───────────────────────────────────────────────────────
        bg_fs = ttk.LabelFrame(inner, text="Background")
        bg_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        row += 1

        bg_style_row = ttk.Frame(bg_fs)
        bg_style_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(bg_style_row, text="Style:").pack(side=tk.LEFT)
        bg_style_var = tk.StringVar(value=self.settings["bg_style"])
        bg_style_cb = ttk.Combobox(bg_style_row, textvariable=bg_style_var, values=BG_STYLES,
                                    state="readonly", width=12)
        bg_style_cb.pack(side=tk.LEFT, padx=4)
        bg_style_cb.bind("<<ComboboxSelected>>", lambda e: self._toggle_bgvideo_row())
        self.vars["bg_style"] = (bg_style_var, "choice")

        bg_color_row = ttk.Frame(bg_fs)
        bg_color_row.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(bg_color_row, "bg_color", "Color 1:", "str")
        bg_color2_row = ttk.Frame(bg_fs)
        bg_color2_row.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(bg_color2_row, "bg_color2", "Color 2 (gradient):", "str")

        wave_row = ttk.Frame(bg_fs)
        wave_row.pack(fill=tk.X, padx=4, pady=2)
        wave_var = tk.BooleanVar(value=self.settings["waveform_enabled"])
        ttk.Checkbutton(wave_row, text="Show audio waveform overlay", variable=wave_var).pack(side=tk.LEFT)
        self.vars["waveform_enabled"] = (wave_var, "bool")
        wave_color_row = ttk.Frame(bg_fs)
        wave_color_row.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(wave_color_row, "waveform_color", "Waveform color:", "str")

        self.bgvideo_row = ttk.Frame(bg_fs)
        self.bgvideo_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(self.bgvideo_row, text="BG video file:").pack(side=tk.LEFT)
        self.bgvideo_label = ttk.Label(self.bgvideo_row,
                                        text=_short_path(self.settings.get("bg_video_path", "")) or "(none)")
        self.bgvideo_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(self.bgvideo_row, text="Browse…", command=self.choose_bgvideo).pack(side=tk.RIGHT)
        ttk.Button(self.bgvideo_row, text="Clear", command=self.clear_bgvideo).pack(side=tk.RIGHT, padx=(0, 4))
        self._toggle_bgvideo_row()

        # ── Glow ─────────────────────────────────────────────────────────────
        glow_fs = ttk.LabelFrame(inner, text="Glow")
        glow_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        row += 1

        glow_preset_row = ttk.Frame(glow_fs)
        glow_preset_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(glow_preset_row, text="Intensity:").pack(side=tk.LEFT)
        self.glow_preset_var = tk.StringVar(value="Custom")
        glow_preset_cb = ttk.Combobox(glow_preset_row, textvariable=self.glow_preset_var,
                                        values=list(GLOW_PRESETS.keys()), state="readonly", width=12)
        glow_preset_cb.pack(side=tk.LEFT, padx=4)
        glow_preset_cb.bind("<<ComboboxSelected>>", self._on_glow_preset)

        glow_color_row = ttk.Frame(glow_fs)
        glow_color_row.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(glow_color_row, "glow_color", "Glow color:", "str")

        glow_nums = ttk.Frame(glow_fs)
        glow_nums.pack(fill=tk.X, padx=4, pady=2)
        for label, key, kind in [("Blur sigma:", "glow_sigma", "int"),
                                   ("Pulse speed (s):", "pulse_speed", "float"),
                                   ("Breathe:", "breathe_amount", "float")]:
            ttk.Label(glow_nums, text=label).pack(side=tk.LEFT, padx=(8, 0))
            v = tk.StringVar(value=str(self.settings[key]))
            e = ttk.Entry(glow_nums, textvariable=v, width=7)
            e.pack(side=tk.LEFT, padx=(2, 4))
            v.trace_add("write", lambda *_: self._sync_glow_preset())
            self.vars[key] = (v, kind)

        # ── Video ─────────────────────────────────────────────────────────────
        vid_fs = ttk.LabelFrame(inner, text="Video")
        vid_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        row += 1

        res_row = ttk.Frame(vid_fs)
        res_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(res_row, text="Resolution:").pack(side=tk.LEFT)
        self.res_preset_var = tk.StringVar(value="1080p — 1920×1080")
        res_preset_cb = ttk.Combobox(res_row, textvariable=self.res_preset_var,
                                      values=list(RESOLUTION_PRESETS.keys()), state="readonly", width=24)
        res_preset_cb.pack(side=tk.LEFT, padx=4)
        res_preset_cb.bind("<<ComboboxSelected>>", self._on_resolution_preset)

        self.custom_res_row = ttk.Frame(vid_fs)
        self.custom_res_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(self.custom_res_row, text="Width:").pack(side=tk.LEFT, padx=(8, 0))
        w_var = tk.StringVar(value=str(self.settings["width"]))
        ttk.Entry(self.custom_res_row, textvariable=w_var, width=7).pack(side=tk.LEFT, padx=2)
        ttk.Label(self.custom_res_row, text="Height:").pack(side=tk.LEFT, padx=(8, 0))
        h_var = tk.StringVar(value=str(self.settings["height"]))
        ttk.Entry(self.custom_res_row, textvariable=h_var, width=7).pack(side=tk.LEFT, padx=2)
        self.vars["width"]  = (w_var, "int")
        self.vars["height"] = (h_var, "int")
        w_var.trace_add("write", lambda *_: self._sync_resolution_preset())
        h_var.trace_add("write", lambda *_: self._sync_resolution_preset())

        fps_row = ttk.Frame(vid_fs)
        fps_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(fps_row, text="FPS:").pack(side=tk.LEFT)
        fps_var = tk.StringVar(value=str(self.settings["fps"]))
        ttk.Combobox(fps_row, textvariable=fps_var, values=FPS_OPTIONS, state="readonly", width=6).pack(
            side=tk.LEFT, padx=4)
        self.vars["fps"] = (fps_var, "int")

        # ── Title overlay ─────────────────────────────────────────────────────
        title_fs = ttk.LabelFrame(inner, text="Title Overlay")
        title_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        row += 1

        tf_row = ttk.Frame(title_fs)
        tf_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(tf_row, text="Font:").pack(side=tk.LEFT)
        tf_var = tk.StringVar(value=self.settings["title_font"])
        ttk.Combobox(tf_row, textvariable=tf_var, values=font_families, width=20).pack(side=tk.LEFT, padx=4)
        self.vars["title_font"] = (tf_var, "str")
        ttk.Label(tf_row, text="Size:").pack(side=tk.LEFT, padx=(8, 0))
        tfs_var = tk.StringVar(value=str(self.settings["title_font_size"]))
        ttk.Entry(tf_row, textvariable=tfs_var, width=5).pack(side=tk.LEFT, padx=4)
        self.vars["title_font_size"] = (tfs_var, "int")
        title_color_row = ttk.Frame(title_fs)
        title_color_row.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(title_color_row, "title_color", "Color:", "str")

        ttk.Button(inner, text="👁  Preview Style (3s, no captions)", command=self.run_preview).grid(
            row=row, column=0, sticky="ew", padx=6, pady=(8, 4))

    # ── Tab: Captions ─────────────────────────────────────────────────────────

    def _build_tab_captions(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text="Captions")

        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        font_families = sorted(set(tkfont.families()))
        row = 0

        cap_fs = ttk.LabelFrame(inner, text="Caption Style")
        cap_fs.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        inner.columnconfigure(0, weight=1)
        row += 1

        r1 = ttk.Frame(cap_fs)
        r1.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(r1, text="Font:").pack(side=tk.LEFT)
        fn_var = tk.StringVar(value=self.settings["font_name"])
        ttk.Combobox(r1, textvariable=fn_var, values=font_families, width=22).pack(side=tk.LEFT, padx=4)
        self.vars["font_name"] = (fn_var, "str")
        ttk.Label(r1, text="Fallback:").pack(side=tk.LEFT, padx=(8, 0))
        ff_var = tk.StringVar(value=self.settings["font_fallback"])
        ttk.Combobox(r1, textvariable=ff_var, values=font_families, width=16).pack(side=tk.LEFT, padx=4)
        self.vars["font_fallback"] = (ff_var, "str")

        r2 = ttk.Frame(cap_fs)
        r2.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(r2, text="Font size:").pack(side=tk.LEFT)
        fs_var = tk.StringVar(value=str(self.settings["font_size"]))
        ttk.Entry(r2, textvariable=fs_var, width=6).pack(side=tk.LEFT, padx=4)
        self.vars["font_size"] = (fs_var, "int")
        ttk.Label(r2, text="Words/chunk:").pack(side=tk.LEFT, padx=(8, 0))
        wpc_var = tk.StringVar(value=str(self.settings["words_per_chunk"]))
        ttk.Combobox(r2, textvariable=wpc_var, values=["1","2","3","4","5"], state="readonly", width=4).pack(
            side=tk.LEFT, padx=4)
        self.vars["words_per_chunk"] = (wpc_var, "int")

        r3 = ttk.Frame(cap_fs)
        r3.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(r3, text="Caption Y:").pack(side=tk.LEFT)
        self.caption_y_preset_var = tk.StringVar(value="Bottom (900)")
        cap_y_preset_cb = ttk.Combobox(r3, textvariable=self.caption_y_preset_var,
                                        values=list(CAPTION_Y_PRESETS.keys()), state="readonly", width=18)
        cap_y_preset_cb.pack(side=tk.LEFT, padx=4)
        cap_y_preset_cb.bind("<<ComboboxSelected>>", self._on_caption_y_preset)
        cap_y_var = tk.StringVar(value=str(self.settings["caption_y"]))
        ttk.Entry(r3, textvariable=cap_y_var, width=6).pack(side=tk.LEFT, padx=4)
        cap_y_var.trace_add("write", lambda *_: self._sync_caption_y_preset())
        self.vars["caption_y"] = (cap_y_var, "int")

        r4 = ttk.Frame(cap_fs)
        r4.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(r4, text="Animation:").pack(side=tk.LEFT)
        cap_style_var = tk.StringVar(value=self.settings["caption_style"])
        ttk.Combobox(r4, textvariable=cap_style_var, values=CAPTION_STYLES, state="readonly", width=12).pack(
            side=tk.LEFT, padx=4)
        self.vars["caption_style"] = (cap_style_var, "choice")

        for label, key in [("Caption color:", "caption_color"),
                            ("Highlight color:", "caption_highlight"),
                            ("Box background:", "caption_back")]:
            cr = ttk.Frame(cap_fs)
            cr.pack(fill=tk.X, padx=4, pady=2)
            self._add_color_field(cr, key, label, "ass")

    # ── Tab: Clips (Intro / Outro) ────────────────────────────────────────────

    def _build_tab_clips(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text="Intro / Outro")

        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        io_fs = ttk.LabelFrame(inner, text="Intro / Outro Clips")
        io_fs.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        inner.columnconfigure(0, weight=1)

        for kind in ("intro", "outro"):
            fr = ttk.Frame(io_fs)
            fr.pack(fill=tk.X, padx=4, pady=4)
            ttk.Label(fr, text=f"{kind.title()} clip:").pack(side=tk.LEFT)
            lbl = ttk.Label(fr, text="(none)")
            lbl.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
            if kind == "intro":
                self.intro_label = lbl
            else:
                self.outro_label = lbl
            ttk.Button(fr, text="Browse…",
                        command=lambda k=kind: self.choose_clip(k)).pack(side=tk.RIGHT)
            ttk.Button(fr, text="Generate…",
                        command=lambda k=kind: self.generate_clip(k)).pack(side=tk.RIGHT, padx=4)
            ttk.Button(fr, text="Clear",
                        command=lambda k=kind: self.clear_clip(k)).pack(side=tk.RIGHT, padx=(0, 4))

    # ── Tab: Extras (Watermark, End Card, Outline) ────────────────────────────

    def _build_tab_extras(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text="Extras")

        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        inner.columnconfigure(0, weight=1)
        font_families = sorted(set(tkfont.families()))

        # ── Watermark ─────────────────────────────────────────────────────────
        wm_fs = ttk.LabelFrame(inner, text="Watermark / Logo")
        wm_fs.grid(row=0, column=0, sticky="ew", padx=6, pady=4)

        wm_en_var = tk.BooleanVar(value=self.settings.get("watermark_enabled", False))
        ttk.Checkbutton(wm_fs, text="Show watermark overlay", variable=wm_en_var).pack(
            anchor="w", padx=4, pady=2)
        self.vars["watermark_enabled"] = (wm_en_var, "bool")

        wm_file_row = ttk.Frame(wm_fs)
        wm_file_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(wm_file_row, text="Image file:").pack(side=tk.LEFT)
        self.watermark_label = ttk.Label(wm_file_row,
                                          text=_short_path(self.settings.get("watermark_path", "")) or "(none)")
        self.watermark_label.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(wm_file_row, text="Browse…", command=self.choose_watermark).pack(side=tk.RIGHT)
        ttk.Button(wm_file_row, text="Clear", command=self.clear_watermark).pack(side=tk.RIGHT, padx=(0, 4))

        wm_size_row = ttk.Frame(wm_fs)
        wm_size_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(wm_size_row, text="Size (px width):").pack(side=tk.LEFT)
        wm_size_var = tk.StringVar(value=str(self.settings.get("watermark_size", 120)))
        ttk.Entry(wm_size_row, textvariable=wm_size_var, width=7).pack(side=tk.LEFT, padx=4)
        self.vars["watermark_size"] = (wm_size_var, "int")

        # ── Chapter cards ─────────────────────────────────────────────────────
        ch_fs = ttk.LabelFrame(inner, text="Chapter Title Cards")
        ch_fs.grid(row=1, column=0, sticky="ew", padx=6, pady=4)

        ch_var = tk.BooleanVar(value=self.settings.get("chapter_cards_enabled", False))
        ttk.Checkbutton(ch_fs, text="Show chapter title card at section transitions (requires script / outline)",
                         variable=ch_var).pack(anchor="w", padx=4, pady=4)
        self.vars["chapter_cards_enabled"] = (ch_var, "bool")

        # ── End card ──────────────────────────────────────────────────────────
        ec_fs = ttk.LabelFrame(inner, text="End Card")
        ec_fs.grid(row=2, column=0, sticky="ew", padx=6, pady=4)

        ec_en_var = tk.BooleanVar(value=self.settings.get("endcard_enabled", False))
        ttk.Checkbutton(ec_fs, text="Show end-card message", variable=ec_en_var).pack(
            anchor="w", padx=4, pady=2)
        self.vars["endcard_enabled"] = (ec_en_var, "bool")

        ec_text_row = ttk.Frame(ec_fs)
        ec_text_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(ec_text_row, text="Text:").pack(side=tk.LEFT)
        ec_text_var = tk.StringVar(value=self.settings.get("endcard_text", ""))
        ttk.Entry(ec_text_row, textvariable=ec_text_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.vars["endcard_text"] = (ec_text_var, "str")

        ec_dur_row = ttk.Frame(ec_fs)
        ec_dur_row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(ec_dur_row, text="Duration (seconds):").pack(side=tk.LEFT)
        ec_dur_var = tk.StringVar(value=str(self.settings.get("endcard_seconds", 6)))
        ttk.Entry(ec_dur_row, textvariable=ec_dur_var, width=6).pack(side=tk.LEFT, padx=4)
        self.vars["endcard_seconds"] = (ec_dur_var, "float")

        # ── Outline / Ollama ──────────────────────────────────────────────────
        ol_fs = ttk.LabelFrame(inner, text="Progressive Outline (via Ollama)")
        ol_fs.grid(row=3, column=0, sticky="ew", padx=6, pady=4)

        ol_en_var = tk.BooleanVar(value=self.settings.get("outline_enabled", False))
        ttk.Checkbutton(ol_fs,
                         text="Show progressive topic outline (left side — uses Ollama or episode script)",
                         variable=ol_en_var).pack(anchor="w", padx=4, pady=2)
        self.vars["outline_enabled"] = (ol_en_var, "bool")

        ol_r1 = ttk.Frame(ol_fs)
        ol_r1.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(ol_r1, text="Ollama URL:").pack(side=tk.LEFT)
        ol_url_var = tk.StringVar(value=self.settings.get("ollama_url", "http://localhost:11434"))
        ttk.Entry(ol_r1, textvariable=ol_url_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.vars["ollama_url"] = (ol_url_var, "str")

        ol_r2 = ttk.Frame(ol_fs)
        ol_r2.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(ol_r2, text="Ollama model:").pack(side=tk.LEFT)
        ol_model_var = tk.StringVar(value=self.settings.get("ollama_model", "llama3.1"))
        ttk.Entry(ol_r2, textvariable=ol_model_var, width=20).pack(side=tk.LEFT, padx=4)
        self.vars["ollama_model"] = (ol_model_var, "str")

        ol_r3 = ttk.Frame(ol_fs)
        ol_r3.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(ol_r3, text="Font:").pack(side=tk.LEFT)
        ol_font_var = tk.StringVar(value=self.settings.get("outline_font", "Georgia"))
        ttk.Combobox(ol_r3, textvariable=ol_font_var, values=font_families, width=20).pack(side=tk.LEFT, padx=4)
        self.vars["outline_font"] = (ol_font_var, "str")
        ttk.Label(ol_r3, text="Size:").pack(side=tk.LEFT, padx=(8, 0))
        ol_size_var = tk.StringVar(value=str(self.settings.get("outline_font_size", 36)))
        ttk.Entry(ol_r3, textvariable=ol_size_var, width=5).pack(side=tk.LEFT, padx=4)
        self.vars["outline_font_size"] = (ol_size_var, "int")

        ol_r4 = ttk.Frame(ol_fs)
        ol_r4.pack(fill=tk.X, padx=4, pady=2)
        self._add_color_field(ol_r4, "outline_color", "Outline text color:", "str")

        ttk.Label(ol_fs, text="Outline uses the episode script (.docx) set above, or falls back to Ollama.",
                   foreground="gray").pack(anchor="w", padx=4, pady=(0, 4))

    # ── Color field helper ────────────────────────────────────────────────────

    def _add_color_field(self, parent_row, key, label, kind):
        """Pack a label + entry + color swatch into parent_row."""
        is_ass = (kind == "ass")
        ttk.Label(parent_row, text=label).pack(side=tk.LEFT)

        current = self.settings.get(key, "")
        var = tk.StringVar(value=str(current))
        ttk.Entry(parent_row, textvariable=var, width=18).pack(side=tk.LEFT, padx=4)

        try:
            swatch_color = ass_to_rgb_hex(current) if is_ass else to_tk_color(current)
        except Exception:
            swatch_color = "#000000"
        swatch = tk.Button(parent_row, width=3, relief="raised", bg=swatch_color)
        self.color_swatches[key] = (swatch, is_ass)

        def pick(event=None, _var=var, _swatch=swatch, _key=key, _is_ass=is_ass):
            cur = ass_to_rgb_hex(_var.get()) if _is_ass else to_tk_color(_var.get())
            try:
                _, hex_color = colorchooser.askcolor(color=cur, parent=self)
            except tk.TclError:
                _, hex_color = colorchooser.askcolor(parent=self)
            if hex_color:
                if _is_ass:
                    _var.set(rgb_hex_to_ass(hex_color, _var.get()))
                else:
                    _var.set(hex_color)
                _swatch.config(bg=hex_color)

        swatch.config(command=pick)
        swatch.pack(side=tk.LEFT)

        field_kind = "ass" if is_ass else "str"
        self.vars[key] = (var, field_kind)

    # ── Compound preset sync helpers ──────────────────────────────────────────

    def _on_resolution_preset(self, event=None):
        name = self.res_preset_var.get()
        dims = RESOLUTION_PRESETS.get(name)
        if dims:
            self.vars["width"][0].set(str(dims[0]))
            self.vars["height"][0].set(str(dims[1]))
            self.custom_res_row.pack_forget()
        else:
            self.custom_res_row.pack(fill=tk.X, padx=4, pady=2)

    def _sync_resolution_preset(self):
        try:
            w = int(self.vars["width"][0].get())
            h = int(self.vars["height"][0].get())
        except ValueError:
            return
        for name, dims in RESOLUTION_PRESETS.items():
            if dims and dims == (w, h):
                self.res_preset_var.set(name)
                self.custom_res_row.pack_forget()
                return
        self.res_preset_var.set("Custom")
        self.custom_res_row.pack(fill=tk.X, padx=4, pady=2)

    def _on_glow_preset(self, event=None):
        p = GLOW_PRESETS.get(self.glow_preset_var.get())
        if not p:
            return
        self.vars["glow_sigma"][0].set(str(p["glow_sigma"]))
        self.vars["pulse_speed"][0].set(str(p["pulse_speed"]))
        self.vars["breathe_amount"][0].set(str(p["breathe_amount"]))

    def _sync_glow_preset(self):
        try:
            sigma = float(self.vars["glow_sigma"][0].get())
            speed = float(self.vars["pulse_speed"][0].get())
            breathe = float(self.vars["breathe_amount"][0].get())
        except ValueError:
            return
        for name, p in GLOW_PRESETS.items():
            if p and p["glow_sigma"] == sigma and p["pulse_speed"] == speed and p["breathe_amount"] == breathe:
                self.glow_preset_var.set(name)
                return
        self.glow_preset_var.set("Custom")

    def _on_art_size_preset(self, event=None):
        v = ART_SIZE_PRESETS.get(self.art_size_preset_var.get())
        if v is not None:
            self.vars["art_size"][0].set(str(v))

    def _sync_art_size_preset(self):
        try:
            v = int(self.vars["art_size"][0].get())
        except ValueError:
            return
        for name, val in ART_SIZE_PRESETS.items():
            if val == v:
                self.art_size_preset_var.set(name)
                return
        self.art_size_preset_var.set("Custom")

    def _on_art_x_preset(self, event=None):
        v = ART_X_PRESETS.get(self.art_x_preset_var.get())
        if v is not None:
            self.vars["art_x_offset"][0].set(str(v))

    def _sync_art_x_preset(self):
        try:
            v = int(self.vars["art_x_offset"][0].get())
        except ValueError:
            return
        for name, val in ART_X_PRESETS.items():
            if val is not None and val == v:
                self.art_x_preset_var.set(name)
                return
        self.art_x_preset_var.set("Custom")

    def _on_caption_y_preset(self, event=None):
        v = CAPTION_Y_PRESETS.get(self.caption_y_preset_var.get())
        if v is not None:
            self.vars["caption_y"][0].set(str(v))

    def _sync_caption_y_preset(self):
        try:
            v = int(self.vars["caption_y"][0].get())
        except ValueError:
            return
        for name, val in CAPTION_Y_PRESETS.items():
            if val == v:
                self.caption_y_preset_var.set(name)
                return
        self.caption_y_preset_var.set("Custom")

    def _toggle_bgvideo_row(self):
        bg_style_var = self.vars.get("bg_style")
        if bg_style_var and bg_style_var[0].get() == "video":
            self.bgvideo_row.pack(fill=tk.X, padx=4, pady=2)
        else:
            self.bgvideo_row.pack_forget()

    # ── Queue management ──────────────────────────────────────────────────────

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

    def choose_script(self):
        path = filedialog.askopenfilename(title="Choose episode script",
                                           filetypes=[("Word documents", "*.docx"), ("All files", "*.*")])
        if path:
            self._global_script_path = Path(path)
            self.script_label.config(text=_short_path(path), foreground="")

    def clear_script(self):
        self._global_script_path = None
        self.script_label.config(text="(none)", foreground="gray")

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
        if not row_id or col != "#2":
            return
        idx = int(row_id)
        import tkinter.simpledialog as sd
        new_title = sd.askstring("Episode Title", "Title overlay (blank = none):",
                                  initialvalue=self.jobs[idx].title)
        if new_title is not None:
            self.jobs[idx].title = new_title.strip()
            self._update_job_row(idx)

    def remove_selected(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Cannot edit the queue while rendering.")
            return
        for i in sorted((int(i) for i in self.tree.selection()), reverse=True):
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

    # ── Art / clip pickers ────────────────────────────────────────────────────

    def choose_art(self):
        path = filedialog.askopenfilename(title="Choose cover art",
                                           filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
        if path:
            self.settings["art"] = path
            self.art_label.config(text=_short_path(path))
            self._refresh_art_preview()

    def choose_clip(self, kind):
        path = filedialog.askopenfilename(title=f"Choose {kind} clip",
                                           filetypes=[("Video", "*.mp4 *.mov *.mkv"), ("All files", "*.*")])
        if path:
            self.settings[f"{kind}_path"] = path
            lbl = self.intro_label if kind == "intro" else self.outro_label
            lbl.config(text=_short_path(path))

    def clear_clip(self, kind):
        self.settings[f"{kind}_path"] = ""
        lbl = self.intro_label if kind == "intro" else self.outro_label
        lbl.config(text="(none)")

    def choose_bgvideo(self):
        path = filedialog.askopenfilename(title="Choose background video",
                                           filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi"), ("All files", "*.*")])
        if path:
            self.settings["bg_video_path"] = path
            self.bgvideo_label.config(text=_short_path(path))

    def clear_bgvideo(self):
        self.settings["bg_video_path"] = ""
        self.bgvideo_label.config(text="(none)")

    def choose_watermark(self):
        path = filedialog.askopenfilename(title="Choose watermark image",
                                           filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
        if path:
            self.settings["watermark_path"] = path
            self.watermark_label.config(text=_short_path(path))

    def clear_watermark(self):
        self.settings["watermark_path"] = ""
        self.watermark_label.config(text="(none)")

    def _refresh_art_preview(self):
        if Image is None:
            return
        art_path = self.settings.get("art", "")
        candidate = Path(art_path)
        if not candidate.is_absolute():
            candidate = HERE / art_path
        if not candidate.exists():
            self.art_preview.config(image="", text="(not found)")
            return
        try:
            img = Image.open(candidate)
            img.thumbnail((160, 160))
            self._art_imgtk = ImageTk.PhotoImage(img)
            self.art_preview.config(image=self._art_imgtk, text="")
        except Exception:
            self.art_preview.config(image="", text="(unavailable)")

    # ── Generate intro/outro clip dialog ─────────────────────────────────────

    def generate_clip(self, kind):
        if not self._sync_settings_from_ui():
            return
        s = self.settings

        dialog = tk.Toplevel(self)
        dialog.title(f"Generate {kind.title()} Clip")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        ttk.Label(dialog, text="Text to display:").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
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
                ffmpeg = eng.find_ffmpeg()
                out_path = CLIPS_DIR / f"{kind}_{int(__import__('time').time())}.mp4"
                ok, err = _generate_clip_ffmpeg(s, kind, text, duration, out_path, ffmpeg)
                if not ok:
                    self.after(0, lambda: status_label.config(text="Failed — see log"))
                    self._log(f"Clip generation failed:\n{err[-1500:]}")
                    self.after(0, lambda: generate_btn.config(state=tk.NORMAL))
                    return

                def finish():
                    self.settings[f"{kind}_path"] = str(out_path)
                    lbl = self.intro_label if kind == "intro" else self.outro_label
                    lbl.config(text=_short_path(str(out_path)))
                    self._log(f"Generated {kind} clip: {out_path.name}")
                    dialog.destroy()

                self.after(0, finish)

            threading.Thread(target=work, daemon=True).start()

        generate_btn = ttk.Button(dialog, text="Generate", command=do_generate)
        generate_btn.grid(row=4, column=0, pady=8)

    # ── Settings sync ─────────────────────────────────────────────────────────

    def _sync_settings_from_ui(self):
        for key, (var, kind) in self.vars.items():
            raw = var.get()
            if kind == "bool":
                self.settings[key] = bool(raw)
                continue
            if kind == "ass":
                self.settings[key] = str(raw)
                continue
            raw = str(raw).strip()
            try:
                if kind == "int":
                    self.settings[key] = int(raw)
                elif kind == "float":
                    self.settings[key] = float(raw)
                else:
                    self.settings[key] = raw
            except ValueError:
                messagebox.showerror("Invalid value", f"'{raw}' is not valid for '{key}'")
                return False
        self.settings["output_dir"] = self.output_dir_var.get().strip()
        return True

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.after(0, append)

    # ── Style preview (quick, no captions) ───────────────────────────────────

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
            ffmpeg = eng.find_ffmpeg()
            tmpdir = Path(tempfile.mkdtemp(prefix="vbvn_preview_"))
            glow_loop = tmpdir / "glow.mp4"
            out_path = tmpdir / "preview.mp4"

            half = s["pulse_speed"] / 2
            glow_src, glow_pad = 600, 700
            canvas_sz = glow_src + glow_pad * 2
            breathe_frames = max(1, int(s["fps"] * s["pulse_speed"]))
            art_size = s["art_size"]
            art_x_offset = int(s.get("art_x_offset", 0))

            cmd1 = [
                ffmpeg, "-f", "lavfi",
                "-i", f"color=c={s['glow_color']}:s={glow_src}x{glow_src}:r={s['fps']}",
                "-filter_complex",
                f"[0:v]pad={canvas_sz}:{canvas_sz}:{glow_pad}:{glow_pad}:black,"
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

            bg_chain = eng.build_bg_chain(s, s["fps"])
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
                f"[bg][glow_pulsed]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto[bg_glow];"
                f"[bg_glow][art_sharp]overlay=(W-w)/2+{art_x_offset}:(H-h)/2:format=auto[out]",
                "-map", "[out]", "-t", str(s["pulse_speed"]),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                str(out_path), "-y", "-loglevel", "error",
            ]
            r = subprocess.run(cmd2, capture_output=True, text=True)
            if r.returncode != 0:
                self._log("Preview failed:\n" + r.stderr[-2000:])
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
            self._log(f"Could not open file: {e}")

    # ── Full render ───────────────────────────────────────────────────────────

    def start_render(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if not self.jobs:
            messagebox.showinfo("No files", "Add audio files to the queue first.")
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
        output_dir_path = Path(output_dir) if output_dir else None
        if output_dir_path:
            output_dir_path.mkdir(parents=True, exist_ok=True)

        self.cancel_flag.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.preview_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.worker_thread = threading.Thread(
            target=self._render_all, args=(art_path, output_dir_path), daemon=True)
        self.worker_thread.start()

    def cancel_render(self):
        self.cancel_flag.set()
        self._log("Cancel requested — will stop after current file.")

    def _finish_render(self):
        self.start_btn.config(state=tk.NORMAL)
        self.preview_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

    def _render_all(self, art_path, output_dir_path):
        s = self.settings

        for idx, job in enumerate(self.jobs):
            if self.cancel_flag.is_set():
                self._update_job_row(idx, status="Cancelled")
                continue

            audio = job.path
            out_dir = output_dir_path if output_dir_path else audio.parent
            output = out_dir / (audio.stem + ".mp4")

            script_path = job.script_path or self._global_script_path

            def progress_cb(stage, frac, _idx=idx):
                if stage == "Transcribing":
                    pct = frac * 50
                elif "Generating" in stage:
                    pct = 50
                elif stage == "Rendering":
                    pct = 50 + frac * 45
                else:
                    pct = 95 + frac * 5
                self._update_job_row(_idx, status=stage + "…", progress=pct)

            def log_cb(msg):
                self._log(msg)

            self._update_job_row(idx, status="Running…", progress=0)

            ok = eng.render_job(
                s, audio, output, art_path,
                title=job.title,
                script_path=script_path,
                progress_cb=progress_cb,
                log_cb=log_cb,
            )

            if ok:
                size_mb = output.stat().st_size / (1024 * 1024) if output.exists() else 0
                self._update_job_row(idx, status="Done ✓", progress=100)
                self._log(f"[{audio.name}] Done → {output.name} ({size_mb:.1f} MB)")
            else:
                self._update_job_row(idx, status="Failed ✗", progress=0)

        self._log("All done." if not self.cancel_flag.is_set() else "Stopped.")
        self.after(0, self._finish_render)


# ─── Clip generation helper (shared with generate_clip dialog) ───────────────

def _generate_clip_ffmpeg(s, kind, text, duration, out_path, ffmpeg):
    import subprocess as sp
    glow_loop = Path(tempfile.gettempdir()) / f"vbvn_clipgen_{kind}.mp4"
    half = s["pulse_speed"] / 2
    # Use a smaller canvas for clip generation — the gblur is the main CPU cost
    # and the visual result is identical at half the canvas size.
    glow_src, glow_pad = 300, 350
    canvas_sz = glow_src + glow_pad * 2
    sigma = max(1, int(s["glow_sigma"] / 2))
    cmd1 = [
        ffmpeg, "-f", "lavfi",
        "-i", f"color=c={s['glow_color']}:s={glow_src}x{glow_src}:r={s['fps']}",
        "-filter_complex",
        f"[0:v]pad={canvas_sz}:{canvas_sz}:{glow_pad}:{glow_pad}:black,"
        f"gblur=sigma={sigma},"
        f"fade=t=in:st=0:d={half}:color=black,"
        f"fade=t=out:st={half}:d={half}:color=black[out]",
        "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-t", str(s["pulse_speed"]), str(glow_loop), "-y", "-loglevel", "error",
    ]
    r = sp.run(cmd1, capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr

    bg_chain = eng.build_bg_chain(s, s["fps"])
    text_filter = ""
    if text:
        escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
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
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filter_complex,
        "-map", "[out]", "-map", "1:a",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out_path), "-y", "-loglevel", "error",
    ]
    r = sp.run(cmd2, capture_output=True, text=True)
    glow_loop.unlink(missing_ok=True)
    return r.returncode == 0, r.stderr


if __name__ == "__main__":
    import tkinter.simpledialog  # noqa: F401
    app = App()
    app.mainloop()
