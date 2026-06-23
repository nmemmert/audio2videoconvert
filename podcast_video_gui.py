#!/usr/bin/env python3
"""
Podcast Video Generator — desktop GUI
Run: python3 podcast_video_gui.py
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import render_engine as eng

HERE = Path(__file__).resolve().parent
ART_DIR = HERE / "art"
ART_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
PRESETS_DIR = HERE / "presets"
PRESETS_DIR.mkdir(exist_ok=True)

# ── Color palette ────────────────────────────────────────────────────────────
BG = "#1a1a1a"
BG2 = "#242424"
BG3 = "#2e2e2e"
FG = "#e8e8e8"
FG2 = "#a0a0a0"
ACCENT = "#c9a84c"
ACCENT2 = "#e8c56a"
BTN_BG = "#333333"
BTN_FG = "#e8e8e8"
SEP = "#3a3a3a"
RED = "#e05050"
GREEN = "#50c050"

FONT_LABEL = ("Helvetica", 10)
FONT_BOLD = ("Helvetica", 11, "bold")
FONT_SMALL = ("Helvetica", 9)


def _style(root):
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, font=FONT_LABEL,
                 troughcolor=BG3, borderwidth=0, relief="flat")
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=FG, font=FONT_LABEL)
    s.configure("TCheckbutton", background=BG, foreground=FG, font=FONT_LABEL)
    s.configure("TRadiobutton", background=BG, foreground=FG, font=FONT_LABEL)
    s.configure("TCombobox", fieldbackground=BG3, background=BG3, foreground=FG,
                 selectbackground=BG3, selectforeground=FG, arrowcolor=FG2)
    s.map("TCombobox", fieldbackground=[("readonly", BG3)])
    s.configure("TEntry", fieldbackground=BG3, foreground=FG, insertcolor=FG)
    s.configure("Horizontal.TProgressbar", troughcolor=BG3, background=ACCENT,
                 borderwidth=0, thickness=8)
    s.configure("TScrollbar", background=BG3, troughcolor=BG, arrowcolor=FG2)


def _btn(parent, text, command, accent=False, danger=False, small=False):
    bg = ACCENT if accent else (RED if danger else BTN_BG)
    fg = BG if accent else BTN_FG
    fnt = FONT_SMALL if small else FONT_LABEL
    return tk.Button(parent, text=text, command=command,
                     bg=bg, fg=fg, font=fnt, relief="flat",
                     activebackground=ACCENT2 if accent else "#444",
                     activeforeground=BG if accent else FG,
                     padx=8 if small else 12, pady=3 if small else 6,
                     cursor="hand2", bd=0)


def _lbl(parent, text, small=False, color=None, bg=None):
    return tk.Label(parent, text=text, bg=bg or BG, fg=color or (FG2 if small else FG),
                    font=FONT_SMALL if small else FONT_LABEL)


def _section(parent, text):
    tk.Label(parent, text=text, bg=BG, fg=ACCENT,
             font=("Helvetica", 12, "bold")).pack(anchor="w", padx=28, pady=(20, 5))
    tk.Frame(parent, bg=SEP, height=1).pack(fill="x", padx=28, pady=(0, 12))


def _card(parent, bg=None):
    return tk.Frame(parent, bg=bg or BG2, padx=20, pady=16)


def _entry(parent, textvariable=None, width=30):
    return tk.Entry(parent, textvariable=textvariable, width=width,
                    bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                    font=FONT_LABEL, highlightthickness=1,
                    highlightbackground=SEP, highlightcolor=ACCENT)


def _combo(parent, values, textvariable=None, width=14):
    return ttk.Combobox(parent, values=values, textvariable=textvariable,
                        width=width, state="readonly")


def _check(parent, text, variable, bg=None):
    return ttk.Checkbutton(parent, text=text, variable=variable)


def _spin(parent, from_, to, textvariable, width=4):
    return tk.Spinbox(parent, from_=from_, to=to, width=width,
                      textvariable=textvariable, bg=BG3, fg=FG,
                      relief="flat", font=FONT_SMALL, buttonbackground=BG3)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Podcast Video Generator")
        self.configure(bg=BG)
        self.minsize(920, 680)
        _style(self)

        self._log_q = queue.Queue()
        self._build_vars()
        self._build_ui()
        self._poll_log()
        self.after(200, self._refresh_presets)

    # ── Variables ────────────────────────────────────────────────────────────

    def _build_vars(self):
        d = eng.DEFAULTS
        self.v_audio = tk.StringVar()
        self.v_title = tk.StringVar()
        self.v_output = tk.StringVar()

        self.v_art_enabled = tk.BooleanVar(value=d["art_enabled"])
        self.v_art = tk.StringVar(value=d["art"])
        self.v_art_size = tk.IntVar(value=d.get("art_size", 460))
        self.v_art_x_offset = tk.IntVar(value=d["art_x_offset"])

        self.v_glow_enabled = tk.BooleanVar(value=d["glow_enabled"])
        self.v_glow_color = tk.StringVar(value=d["glow_color"])
        self.v_glow_sigma = tk.IntVar(value=d["glow_sigma"])

        self.v_bg_style = tk.StringVar(value=d["bg_style"])
        self.v_bg_color = tk.StringVar(value=d["bg_color"])
        self.v_bg_color2 = tk.StringVar(value=d["bg_color2"])

        self.v_wave_enabled = tk.BooleanVar(value=d["waveform_enabled"])
        self.v_wave_color = tk.StringVar(value=d["waveform_color"])
        self.v_wave_height = tk.IntVar(value=d["waveform_height"])

        self.v_caption_style = tk.StringVar(value=d["caption_style"])
        self.v_words_per_chunk = tk.IntVar(value=d["words_per_chunk"])
        self.v_font_size = tk.IntVar(value=d["font_size"])

        self.v_outline_enabled = tk.BooleanVar(value=d["outline_enabled"])
        self.v_outline_style = tk.StringVar(value=d["outline_style"])
        self.v_script = tk.StringVar()

        self.v_question_cards = tk.BooleanVar(value=d["question_cards_enabled"])

        self.v_qr_enabled = tk.BooleanVar(value=d["qr_enabled"])
        self.v_qr_path = tk.StringVar(value=d["qr_path"])
        self.v_qr_size = tk.IntVar(value=d["qr_size"])
        self.v_qr_corner = tk.StringVar(value=d["qr_corner"])

        self.v_title_enabled = tk.BooleanVar(value=d["title_enabled"])
        self.v_width = tk.IntVar(value=d["width"])
        self.v_height = tk.IntVar(value=d["height"])
        self.v_fps = tk.IntVar(value=d["fps"])
        self.v_preview = tk.BooleanVar(value=False)
        self.v_preset = tk.StringVar()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top header
        top = tk.Frame(self, bg="#111111", padx=20, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="🎙  Podcast Video Generator",
                 bg="#111111", fg=ACCENT, font=("Helvetica", 15, "bold")).pack(side="left")

        # Preset bar
        pbar = tk.Frame(self, bg=BG2, padx=24, pady=9)
        pbar.pack(fill="x")
        _lbl(pbar, "Preset:", small=True, bg=BG2).pack(side="left", padx=(0, 8))
        self._preset_cb = _combo(pbar, [], textvariable=self.v_preset, width=24)
        self._preset_cb.pack(side="left")
        _btn(pbar, "Load", self._load_preset, small=True).pack(side="left", padx=4)
        _btn(pbar, "Save", self._save_preset, small=True).pack(side="left", padx=2)
        _btn(pbar, "Reset to Defaults", self._reset_defaults, small=True).pack(side="left", padx=(16, 0))

        # Main split: scrollable form | log panel
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True)

        # ── Left: scrollable form ────────────────────────────────────────────
        left_wrap = tk.Frame(mid, bg=BG, width=530)
        left_wrap.pack(side="left", fill="both", expand=True)
        left_wrap.pack_propagate(False)

        canvas = tk.Canvas(left_wrap, bg=BG, highlightthickness=0)
        vscroll = ttk.Scrollbar(left_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._form = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=self._form, anchor="nw")

        self._form.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        self._build_form(self._form)

        # ── Right: log + controls ────────────────────────────────────────────
        right = tk.Frame(mid, bg=BG2, width=360)
        right.pack(side="right", fill="both")
        right.pack_propagate(False)
        self._build_right(right)

    def _build_form(self, p):

        # ── Files ────────────────────────────────────────────────────────────
        _section(p, "Files")
        card = _card(p)
        card.pack(fill="x", padx=28, pady=(0, 14))

        _lbl(card, "Audio file", small=True, bg=BG2).grid(row=0, column=0, sticky="w")
        _entry(card, self.v_audio, width=36).grid(row=1, column=0, sticky="ew", pady=(4, 10))
        _btn(card, "Browse…", self._pick_audio, small=True).grid(row=1, column=1, padx=(8, 0), pady=(4, 10))

        _lbl(card, "Episode title (optional)", small=True, bg=BG2).grid(row=2, column=0, sticky="w")
        rf = tk.Frame(card, bg=BG2)
        rf.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        _entry(rf, self.v_title, width=30).pack(side="left")
        _check(rf, "Show in video", self.v_title_enabled).pack(side="left", padx=12)

        _lbl(card, "Output file", small=True, bg=BG2).grid(row=4, column=0, sticky="w")
        _entry(card, self.v_output, width=36).grid(row=5, column=0, sticky="ew", pady=(4, 0))
        _btn(card, "Browse…", self._pick_output, small=True).grid(row=5, column=1, padx=(8, 0))
        card.columnconfigure(0, weight=1)

        # ── Podcast Art ──────────────────────────────────────────────────────
        _section(p, "Podcast Art")
        ac = _card(p)
        ac.pack(fill="x", padx=28, pady=(0, 14))

        r1 = tk.Frame(ac, bg=BG2)
        r1.pack(fill="x", pady=(0, 12))
        _check(r1, "Show podcast art in video", self.v_art_enabled).pack(side="left")

        r2 = tk.Frame(ac, bg=BG2)
        r2.pack(fill="x", pady=(0, 10))
        _lbl(r2, "Image:", small=True, bg=BG2).pack(side="left")
        self._art_cb = _combo(r2, self._list_art(), textvariable=self.v_art, width=28)
        self._art_cb.pack(side="left", padx=8)
        _btn(r2, "Upload…", self._upload_art, small=True).pack(side="left")

        r3 = tk.Frame(ac, bg=BG2)
        r3.pack(fill="x")
        _lbl(r3, "Size:", small=True, bg=BG2).pack(side="left")
        _spin(r3, 200, 900, self.v_art_size, 5).pack(side="left", padx=6)
        _lbl(r3, "px     H-offset:", small=True, bg=BG2).pack(side="left", padx=(4, 0))
        _spin(r3, -400, 400, self.v_art_x_offset, 5).pack(side="left", padx=6)
        _lbl(r3, "px", small=True, bg=BG2).pack(side="left")

        # ── Background & Glow ────────────────────────────────────────────────
        _section(p, "Background & Glow")
        bg_c = _card(p)
        bg_c.pack(fill="x", padx=28, pady=(0, 14))

        r_bg = tk.Frame(bg_c, bg=BG2)
        r_bg.pack(fill="x", pady=(0, 12))
        _lbl(r_bg, "Style:", small=True, bg=BG2).pack(side="left")
        _combo(r_bg, eng.BG_STYLES, textvariable=self.v_bg_style, width=10).pack(side="left", padx=8)
        _lbl(r_bg, "Color 1:", small=True, bg=BG2).pack(side="left", padx=(8, 0))
        _entry(r_bg, self.v_bg_color, width=9).pack(side="left", padx=6)
        _lbl(r_bg, "Color 2:", small=True, bg=BG2).pack(side="left", padx=(8, 0))
        _entry(r_bg, self.v_bg_color2, width=9).pack(side="left", padx=6)

        r_glow = tk.Frame(bg_c, bg=BG2)
        r_glow.pack(fill="x")
        _check(r_glow, "Glow", self.v_glow_enabled).pack(side="left")
        _lbl(r_glow, "Color:", small=True, bg=BG2).pack(side="left", padx=(16, 0))
        _entry(r_glow, self.v_glow_color, width=9).pack(side="left", padx=6)
        _lbl(r_glow, "Strength:", small=True, bg=BG2).pack(side="left", padx=(10, 0))
        _spin(r_glow, 5, 120, self.v_glow_sigma).pack(side="left", padx=6)

        # ── Animation — Waveform Bar ─────────────────────────────────────────
        _section(p, "Animation — Waveform Bar")
        wc = _card(p)
        wc.pack(fill="x", padx=28, pady=(0, 14))

        _check(wc, "Show waveform bar at bottom  (never overlaps art or outline)",
               self.v_wave_enabled).pack(anchor="w", pady=(0, 12))

        r_wave = tk.Frame(wc, bg=BG2)
        r_wave.pack(fill="x")
        _lbl(r_wave, "Color:", small=True, bg=BG2).pack(side="left")
        _entry(r_wave, self.v_wave_color, width=9).pack(side="left", padx=6)
        _lbl(r_wave, "Height:", small=True, bg=BG2).pack(side="left", padx=(12, 0))
        _spin(r_wave, 40, 200, self.v_wave_height).pack(side="left", padx=6)
        _lbl(r_wave, "px", small=True, bg=BG2).pack(side="left")

        # ── Outline ──────────────────────────────────────────────────────────
        _section(p, "Outline")
        oc = _card(p)
        oc.pack(fill="x", padx=28, pady=(0, 14))

        _check(oc, "Show outline in video", self.v_outline_enabled).pack(anchor="w", pady=(0, 12))

        r_ostyle = tk.Frame(oc, bg=BG2)
        r_ostyle.pack(fill="x", pady=(0, 12))
        _lbl(r_ostyle, "Style:", small=True, bg=BG2).pack(side="left")
        for val, label in [("sidebar", "Left sidebar"), ("ticker", "Chapter ticker")]:
            ttk.Radiobutton(r_ostyle, text=label, variable=self.v_outline_style,
                            value=val).pack(side="left", padx=8)

        r_script = tk.Frame(oc, bg=BG2)
        r_script.pack(fill="x", pady=(0, 12))
        _lbl(r_script, "Script (.docx):", small=True, bg=BG2).pack(side="left")
        _entry(r_script, self.v_script, width=22).pack(side="left", padx=8)
        _btn(r_script, "Browse…", self._pick_script, small=True).pack(side="left")

        # Checklist — populated when a script is loaded
        _lbl(oc, "Items to include in outline (uncheck to remove):", small=True, bg=BG2).pack(anchor="w", pady=(0, 6))
        self._outline_list_frame = tk.Frame(oc, bg=BG2)
        self._outline_list_frame.pack(fill="x")
        self._outline_checks = []  # list of (title_str, BooleanVar)
        _lbl(self._outline_list_frame, "Load a script above to pick outline items.",
             small=True, bg=BG2).pack(anchor="w")

        # ── Discussion Questions ──────────────────────────────────────────────
        _section(p, "Discussion Questions")
        dq = _card(p)
        dq.pack(fill="x", padx=28, pady=(0, 14))

        _check(dq, "Show discussion question cards (fades over art when each question is spoken)",
               self.v_question_cards).pack(anchor="w", pady=(0, 6))
        _lbl(dq, "Requires a script — questions are detected from the docx automatically.",
             small=True, bg=BG2).pack(anchor="w")

        # ── QR Code ──────────────────────────────────────────────────────────
        _section(p, "QR Code Watermark")
        qr = _card(p)
        qr.pack(fill="x", padx=28, pady=(0, 14))

        _check(qr, "Show QR code in corner", self.v_qr_enabled).pack(anchor="w", pady=(0, 10))

        r_qr1 = tk.Frame(qr, bg=BG2)
        r_qr1.pack(fill="x", pady=(0, 8))
        _lbl(r_qr1, "Image:", small=True, bg=BG2).pack(side="left")
        _entry(r_qr1, self.v_qr_path, width=24).pack(side="left", padx=8)
        _btn(r_qr1, "Browse…", self._pick_qr, small=True).pack(side="left")

        r_qr2 = tk.Frame(qr, bg=BG2)
        r_qr2.pack(fill="x")
        _lbl(r_qr2, "Size:", small=True, bg=BG2).pack(side="left")
        _spin(r_qr2, 80, 400, self.v_qr_size, 4).pack(side="left", padx=6)
        _lbl(r_qr2, "px   Corner:", small=True, bg=BG2).pack(side="left", padx=(10, 0))
        _combo(r_qr2, ["bottom-right", "bottom-left", "top-right", "top-left"],
               textvariable=self.v_qr_corner, width=14).pack(side="left", padx=8)

        # ── Captions ─────────────────────────────────────────────────────────
        _section(p, "Captions")
        cc = _card(p)
        cc.pack(fill="x", padx=28, pady=(0, 14))

        r_cap = tk.Frame(cc, bg=BG2)
        r_cap.pack(fill="x")
        _lbl(r_cap, "Style:", small=True, bg=BG2).pack(side="left")
        _combo(r_cap, eng.CAPTION_STYLES, textvariable=self.v_caption_style, width=10).pack(side="left", padx=8)
        _lbl(r_cap, "Words/chunk:", small=True, bg=BG2).pack(side="left", padx=(12, 0))
        _spin(r_cap, 1, 6, self.v_words_per_chunk, 3).pack(side="left", padx=6)
        _lbl(r_cap, "Font size:", small=True, bg=BG2).pack(side="left", padx=(12, 0))
        _spin(r_cap, 24, 120, self.v_font_size, 4).pack(side="left", padx=6)

        # ── Output settings ──────────────────────────────────────────────────
        _section(p, "Output Settings")
        rc = _card(p)
        rc.pack(fill="x", padx=28, pady=(0, 28))

        r_res = tk.Frame(rc, bg=BG2)
        r_res.pack(fill="x")
        _lbl(r_res, "Width:", small=True, bg=BG2).pack(side="left")
        _spin(r_res, 640, 3840, self.v_width, 5).pack(side="left", padx=6)
        _lbl(r_res, "Height:", small=True, bg=BG2).pack(side="left", padx=(10, 0))
        _spin(r_res, 360, 2160, self.v_height, 5).pack(side="left", padx=6)
        _lbl(r_res, "FPS:", small=True, bg=BG2).pack(side="left", padx=(10, 0))
        _spin(r_res, 24, 60, self.v_fps, 3).pack(side="left", padx=6)

    def _build_right(self, p):
        ctrl = tk.Frame(p, bg=BG2, padx=24, pady=20)
        ctrl.pack(fill="x")

        _check(ctrl, "Preview only (first 12 seconds)", self.v_preview).pack(anchor="w", pady=(0, 14))

        self._render_btn = _btn(ctrl, "▶  Render Video", self._start_render, accent=True)
        self._render_btn.pack(fill="x", pady=(0, 8))
        self._batch_btn = _btn(ctrl, "⏭  Batch Render…", self._start_batch, small=False)
        self._batch_btn.pack(fill="x", pady=(0, 8))
        self._cancel_btn = _btn(ctrl, "✕  Cancel", self._cancel_render, danger=True)
        self._cancel_btn.pack(fill="x")
        self._cancel_btn.config(state="disabled")

        prog_frame = tk.Frame(p, bg=BG2, padx=24, pady=14)
        prog_frame.pack(fill="x")
        self._stage_lbl = tk.Label(prog_frame, text="Ready", bg=BG2, fg=FG2, font=FONT_SMALL, anchor="w")
        self._stage_lbl.pack(fill="x")
        self._progress = ttk.Progressbar(prog_frame, mode="determinate",
                                          style="Horizontal.TProgressbar")
        self._progress.pack(fill="x", pady=(6, 0))

        log_hdr = tk.Frame(p, bg=BG3, padx=24, pady=7)
        log_hdr.pack(fill="x")
        tk.Label(log_hdr, text="Log", bg=BG3, fg=FG2, font=FONT_SMALL).pack(anchor="w")

        log_frame = tk.Frame(p, bg=BG)
        log_frame.pack(fill="both", expand=True)
        self._log_text = tk.Text(log_frame, bg="#111111", fg=FG2, font=("Courier", 9),
                                  relief="flat", wrap="word", state="disabled",
                                  padx=16, pady=12)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self._log_text.pack(side="left", fill="both", expand=True)
        self._log_text.tag_configure("good", foreground=GREEN)
        self._log_text.tag_configure("bad", foreground=RED)
        self._log_text.tag_configure("accent", foreground=ACCENT)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _list_art(self):
        exts = {".jpg", ".jpeg", ".png"}
        return sorted(p.name for p in ART_DIR.iterdir() if p.suffix.lower() in exts)

    def _list_presets(self):
        return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))

    def _refresh_presets(self):
        choices = self._list_presets()
        self._preset_cb.configure(values=choices)
        if choices and not self.v_preset.get():
            self.v_preset.set(choices[0])

    def _pick_audio(self):
        f = filedialog.askopenfilename(
            title="Select audio file",
            filetypes=[("Audio", "*.mp3 *.m4a *.wav *.aac *.flac *.ogg"), ("All", "*.*")])
        if not f:
            return
        self.v_audio.set(f)
        if not self.v_output.get():
            self.v_output.set(str(OUTPUT_DIR / (Path(f).stem + ".mp4")))
        if not self.v_title.get():
            self.v_title.set(Path(f).stem.replace("_", " ").replace("-", " ").title())

    def _pick_output(self):
        f = filedialog.asksaveasfilename(
            title="Save video as", defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")])
        if f:
            self.v_output.set(f)

    def _pick_script(self):
        f = filedialog.askopenfilename(
            title="Select script", filetypes=[("Word document", "*.docx"), ("All", "*.*")])
        if f:
            self.v_script.set(f)
            self._load_outline_checklist(f)

    def _load_outline_checklist(self, path):
        """Extract outline titles from script and populate the checkbox list."""
        try:
            points = eng.extract_outline_from_script(path)
        except Exception as e:
            self._append_log(f"Could not read script outline: {e}", "bad")
            return

        # Clear existing checkboxes
        for w in self._outline_list_frame.winfo_children():
            w.destroy()
        self._outline_checks.clear()

        if not points:
            _lbl(self._outline_list_frame, "No section headers found in script.",
                 small=True, bg=BG2).pack(anchor="w")
            return

        for title, _anchor in points:
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(self._outline_list_frame, text=title, variable=var)
            cb.pack(anchor="w", pady=1)
            self._outline_checks.append((title, var))

        self._append_log(f"Script loaded: {len(points)} outline items found — uncheck any you don't want.", "accent")

    def _pick_qr(self):
        f = filedialog.askopenfilename(
            title="Select QR code image",
            filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All", "*.*")])
        if f:
            self.v_qr_path.set(f)

    def _upload_art(self):
        f = filedialog.askopenfilename(
            title="Select art image",
            filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All", "*.*")])
        if not f:
            return
        dest = ART_DIR / Path(f).name
        if not dest.exists():
            shutil.copy(f, dest)
        self.v_art.set(dest.name)
        self._art_cb.configure(values=self._list_art())
        self._append_log(f"Art uploaded: {dest.name}", "accent")

    def _collect_settings(self):
        return {
            "art_enabled": self.v_art_enabled.get(),
            "art": self.v_art.get(),
            "art_size": self.v_art_size.get(),
            "art_x_offset": self.v_art_x_offset.get(),
            "glow_enabled": self.v_glow_enabled.get(),
            "glow_color": self.v_glow_color.get(),
            "glow_sigma": self.v_glow_sigma.get(),
            "bg_style": self.v_bg_style.get(),
            "bg_color": self.v_bg_color.get(),
            "bg_color2": self.v_bg_color2.get(),
            "waveform_enabled": self.v_wave_enabled.get(),
            "waveform_color": self.v_wave_color.get(),
            "waveform_height": self.v_wave_height.get(),
            "caption_style": self.v_caption_style.get(),
            "words_per_chunk": self.v_words_per_chunk.get(),
            "font_size": self.v_font_size.get(),
            "font_name": eng.DEFAULTS["font_name"],
            "caption_color": eng.DEFAULTS["caption_color"],
            "caption_highlight": eng.DEFAULTS["caption_highlight"],
            "caption_back": eng.DEFAULTS["caption_back"],
            "outline_enabled": self.v_outline_enabled.get(),
            "outline_style": self.v_outline_style.get(),
            "outline_color": eng.DEFAULTS["outline_color"],
            "outline_font": eng.DEFAULTS["outline_font"],
            "outline_font_size": eng.DEFAULTS["outline_font_size"],
            "title_enabled": self.v_title_enabled.get(),
            "title_font": eng.DEFAULTS["title_font"],
            "title_font_size": eng.DEFAULTS["title_font_size"],
            "title_color": eng.DEFAULTS["title_color"],
            "width": self.v_width.get(),
            "height": self.v_height.get(),
            "fps": self.v_fps.get(),
            "question_cards_enabled": self.v_question_cards.get(),
            "qr_enabled": self.v_qr_enabled.get(),
            "qr_path": self.v_qr_path.get(),
            "qr_size": self.v_qr_size.get(),
            "qr_corner": self.v_qr_corner.get(),
            "ollama_url": eng.DEFAULTS["ollama_url"],
            "ollama_model": eng.DEFAULTS["ollama_model"],
        }

    def _apply_settings(self, d):
        for var, key in [
            (self.v_art_enabled, "art_enabled"),
            (self.v_art, "art"),
            (self.v_art_size, "art_size"),
            (self.v_art_x_offset, "art_x_offset"),
            (self.v_glow_enabled, "glow_enabled"),
            (self.v_glow_color, "glow_color"),
            (self.v_glow_sigma, "glow_sigma"),
            (self.v_bg_style, "bg_style"),
            (self.v_bg_color, "bg_color"),
            (self.v_bg_color2, "bg_color2"),
            (self.v_wave_enabled, "waveform_enabled"),
            (self.v_wave_color, "waveform_color"),
            (self.v_wave_height, "waveform_height"),
            (self.v_caption_style, "caption_style"),
            (self.v_words_per_chunk, "words_per_chunk"),
            (self.v_font_size, "font_size"),
            (self.v_outline_enabled, "outline_enabled"),
            (self.v_outline_style, "outline_style"),
            (self.v_question_cards, "question_cards_enabled"),
            (self.v_qr_enabled, "qr_enabled"),
            (self.v_qr_path, "qr_path"),
            (self.v_qr_size, "qr_size"),
            (self.v_qr_corner, "qr_corner"),
            (self.v_width, "width"),
            (self.v_height, "height"),
            (self.v_fps, "fps"),
        ]:
            if key in d:
                try:
                    var.set(d[key])
                except Exception:
                    pass

    # ── Preset ───────────────────────────────────────────────────────────────

    def _load_preset(self):
        name = self.v_preset.get().strip()
        if not name:
            return
        f = PRESETS_DIR / f"{name}.json"
        if not f.exists():
            messagebox.showwarning("Preset", f"'{name}' not found.")
            return
        self._apply_settings(json.loads(f.read_text()))
        self._append_log(f"Loaded preset: {name}", "accent")

    def _save_preset(self):
        name = self.v_preset.get().strip()
        if not name:
            messagebox.showwarning("Preset", "Enter a name first.")
            return
        (PRESETS_DIR / f"{name}.json").write_text(
            json.dumps(self._collect_settings(), indent=2))
        self._refresh_presets()
        self._append_log(f"Saved preset: {name}", "accent")

    def _reset_defaults(self):
        if not messagebox.askyesno("Reset", "Reset all settings to defaults?"):
            return
        self._apply_settings(eng.DEFAULTS)
        # Clear file paths too
        self.v_audio.set("")
        self.v_output.set("")
        self.v_title.set("")
        self.v_script.set("")
        self.v_qr_path.set("")
        for w in self._outline_list_frame.winfo_children():
            w.destroy()
        self._outline_checks.clear()
        self._append_log("Settings reset to defaults.", "accent")

    # ── Render ───────────────────────────────────────────────────────────────

    def _start_render(self):
        audio = self.v_audio.get().strip()
        if not audio or not Path(audio).exists():
            messagebox.showerror("Error", "Select a valid audio file.")
            return
        output = self.v_output.get().strip()
        if not output:
            messagebox.showerror("Error", "Set an output file path.")
            return

        s = self._collect_settings()
        art_path = None
        if s["art_enabled"]:
            p = ART_DIR / s["art"]
            if not p.exists():
                messagebox.showerror("Error", f"Art image not found: {s['art']}")
                return
            art_path = str(p)

        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self._render_btn.config(state="disabled")
        self._cancel_btn.config(state="normal")
        self._progress["value"] = 0
        self._stage_lbl.config(text="Starting…")
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

        # Build outline_titles from the checked items
        outline_titles = None
        script_path = self.v_script.get().strip() or None
        if script_path and self._outline_checks:
            selected = [title for title, var in self._outline_checks if var.get()]
            if selected:
                outline_titles = [{"title": t, "original": t} for t in selected]

        def run():
            ok = eng.render_job(
                s, audio, str(output_path),
                art_path=art_path,
                title=self.v_title.get().strip(),
                script_path=script_path,
                outline_titles=outline_titles,
                progress_cb=lambda stage, frac: self._log_q.put(("progress", stage, frac)),
                log_cb=lambda msg: self._log_q.put(("log", msg)),
                preview_seconds=12 if self.v_preview.get() else None,
            )
            self._log_q.put(("done", ok, str(output_path)))

        threading.Thread(target=run, daemon=True).start()

    def _start_batch(self):
        """Open the Batch Manager dialog."""
        BatchDialog(self, self._collect_settings(), self._get_art_path(),
                    on_start=self._run_batch_jobs)

    def _get_art_path(self):
        s = self._collect_settings()
        if not s["art_enabled"]:
            return None
        p = ART_DIR / s["art"]
        return str(p) if p.exists() else None

    def _run_batch_jobs(self, jobs):
        """Called by BatchDialog when the user clicks Start. jobs = list of dicts."""
        if not jobs:
            return
        s = self._collect_settings()
        art_path = self._get_art_path()
        if s["art_enabled"] and not art_path:
            messagebox.showerror("Error", f"Art image not found: {s['art']}")
            return

        self._render_btn.config(state="disabled")
        self._batch_btn.config(state="disabled")
        self._cancel_btn.config(state="normal")
        self._progress["value"] = 0
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

        total = len(jobs)

        def run_batch():
            for idx, job in enumerate(jobs, 1):
                audio      = job["audio"]
                title      = job["title"]
                script_p   = job["script"] or None
                out        = job["output"]
                Path(out).parent.mkdir(parents=True, exist_ok=True)

                self._log_q.put(("log", f"── Job {idx}/{total}: {Path(audio).name}"))
                self._log_q.put(("progress", f"Job {idx}/{total}", (idx - 1) / total))

                ok = eng.render_job(
                    s, audio, out,
                    art_path=art_path,
                    title=title,
                    script_path=script_p,
                    progress_cb=lambda stage, frac, i=idx: self._log_q.put((
                        "progress", f"Job {i}/{total} — {stage}",
                        ((i - 1) + frac) / total)),
                    log_cb=lambda msg: self._log_q.put(("log", msg)),
                )
                tag = "good" if ok else "bad"
                self._log_q.put(("log",
                    f"{'✓' if ok else '✗'} {Path(audio).name} → {Path(out).name}", tag))

            self._log_q.put(("done_batch", total))

        threading.Thread(target=run_batch, daemon=True).start()

    def _cancel_render(self):
        self._append_log("Cancel requested — the current step will finish then stop.", "bad")
        self._cancel_btn.config(state="disabled")

    # ── Log polling ──────────────────────────────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                item = self._log_q.get_nowait()
                if item[0] == "log":
                    # item = ("log", msg) or ("log", msg, tag)
                    tag = item[2] if len(item) > 2 else None
                    self._append_log(item[1], tag)
                elif item[0] == "progress":
                    _, stage, frac = item
                    self._stage_lbl.config(text=f"{stage}  {int(frac * 100)}%")
                    self._progress["value"] = frac * 100
                elif item[0] == "done":
                    _, ok, path = item
                    if ok:
                        self._append_log(f"✓ Done → {path}", "good")
                        self._stage_lbl.config(text="Done")
                        self._progress["value"] = 100
                    else:
                        self._append_log("✗ Render failed — see log above.", "bad")
                        self._stage_lbl.config(text="Failed")
                    self._render_btn.config(state="normal")
                    self._batch_btn.config(state="normal")
                    self._cancel_btn.config(state="disabled")
                elif item[0] == "done_batch":
                    _, total = item
                    self._append_log(f"✓ Batch complete — {total} file(s) rendered.", "good")
                    self._stage_lbl.config(text="Batch done")
                    self._progress["value"] = 100
                    self._render_btn.config(state="normal")
                    self._batch_btn.config(state="normal")
                    self._cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _append_log(self, msg, tag=None):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n", tag or "")
        self._log_text.see("end")
        self._log_text.config(state="disabled")


class BatchDialog(tk.Toplevel):
    """Batch manager — one row per episode, each with its own audio, title, and script."""

    COL_AUDIO  = 0
    COL_TITLE  = 1
    COL_SCRIPT = 2
    COL_OUTPUT = 3

    def __init__(self, parent, settings, art_path, on_start):
        super().__init__(parent)
        self.title("Batch Render Manager")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(900, 420)
        self._on_start = on_start
        self._rows = []   # list of dicts with tk vars

        self._build(settings)
        self.grab_set()

    def _build(self, settings):
        # Header
        hdr = tk.Frame(self, bg="#111111", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Batch Render Manager", bg="#111111", fg=ACCENT,
                 font=("Helvetica", 13, "bold")).pack(side="left")
        tk.Label(hdr, text="Visual settings are shared from the main window.",
                 bg="#111111", fg=FG2, font=FONT_SMALL).pack(side="left", padx=16)

        # Column headers
        col_hdr = tk.Frame(self, bg=BG3, padx=10, pady=6)
        col_hdr.pack(fill="x", padx=12, pady=(8, 0))
        for text, w in [("Audio File", 28), ("Episode Title", 20), ("Script (.docx)", 20), ("Output File", 22)]:
            tk.Label(col_hdr, text=text, bg=BG3, fg=FG2, font=FONT_SMALL,
                     width=w, anchor="w").pack(side="left", padx=4)

        # Scrollable job list
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=4)

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._list_frame = tk.Frame(canvas, bg=BG)
        self._list_win = canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        self._list_frame.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(self._list_win, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        # Bottom bar
        bar = tk.Frame(self, bg=BG2, padx=14, pady=12)
        bar.pack(fill="x")
        _btn(bar, "+ Add Files…", self._add_files, accent=True, small=True).pack(side="left", padx=(0, 6))
        _btn(bar, "Remove Selected", self._remove_selected, danger=True, small=True).pack(side="left")
        _btn(bar, "▶  Start Batch", self._start, accent=True).pack(side="right")
        _btn(bar, "Cancel", self.destroy, small=True).pack(side="right", padx=(0, 8))

    def _add_row(self, audio="", title="", script="", output=""):
        row_idx = len(self._rows)
        frame = tk.Frame(self._list_frame, bg=BG2 if row_idx % 2 == 0 else BG, pady=3)
        frame.pack(fill="x", pady=1)

        sel_var = tk.BooleanVar(value=False)
        audio_var  = tk.StringVar(value=audio)
        title_var  = tk.StringVar(value=title)
        script_var = tk.StringVar(value=script)
        output_var = tk.StringVar(value=output)

        bg = BG2 if row_idx % 2 == 0 else BG

        def pick_audio(av=audio_var, tv=title_var, ov=output_var):
            f = filedialog.askopenfilename(
                filetypes=[("Audio", "*.mp3 *.m4a *.wav *.aac *.flac *.ogg"), ("All", "*.*")])
            if f:
                av.set(f)
                if not tv.get():
                    tv.set(Path(f).stem.replace("_", " ").replace("-", " ").title())
                if not ov.get():
                    ov.set(str(OUTPUT_DIR / (Path(f).stem + ".mp4")))

        def pick_script(sv=script_var):
            f = filedialog.askopenfilename(filetypes=[("Word doc", "*.docx"), ("All", "*.*")])
            if f:
                sv.set(f)

        def pick_output(ov=output_var):
            f = filedialog.asksaveasfilename(defaultextension=".mp4",
                                              filetypes=[("MP4", "*.mp4")])
            if f:
                ov.set(f)

        ttk.Checkbutton(frame, variable=sel_var).pack(side="left", padx=(6, 2))

        for var, pick_fn, w in [
            (audio_var,  pick_audio,  26),
            (title_var,  None,        18),
            (script_var, pick_script, 18),
            (output_var, pick_output, 20),
        ]:
            inner = tk.Frame(frame, bg=bg)
            inner.pack(side="left", padx=3)
            e = tk.Entry(inner, textvariable=var, width=w, bg=BG3, fg=FG,
                         insertbackground=FG, relief="flat", font=FONT_SMALL,
                         highlightthickness=1, highlightbackground=SEP, highlightcolor=ACCENT)
            e.pack(side="left")
            if pick_fn:
                _btn(inner, "…", pick_fn, small=True).pack(side="left", padx=2)

        self._rows.append({
            "frame": frame, "sel": sel_var,
            "audio": audio_var, "title": title_var,
            "script": script_var, "output": output_var,
        })

    def _add_files(self):
        files = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio", "*.mp3 *.m4a *.wav *.aac *.flac *.ogg"), ("All", "*.*")])
        for f in files:
            stem = Path(f).stem
            self._add_row(
                audio=f,
                title=stem.replace("_", " ").replace("-", " ").title(),
                output=str(OUTPUT_DIR / f"{stem}.mp4"),
            )

    def _remove_selected(self):
        keep = []
        for row in self._rows:
            if row["sel"].get():
                row["frame"].destroy()
            else:
                keep.append(row)
        self._rows = keep

    def _start(self):
        jobs = []
        for row in self._rows:
            audio = row["audio"].get().strip()
            if not audio or not Path(audio).exists():
                messagebox.showwarning("Batch", f"Audio file not found:\n{audio}")
                return
            out = row["output"].get().strip()
            if not out:
                out = str(OUTPUT_DIR / (Path(audio).stem + ".mp4"))
            jobs.append({
                "audio":  audio,
                "title":  row["title"].get().strip(),
                "script": row["script"].get().strip() or None,
                "output": out,
            })
        if not jobs:
            messagebox.showinfo("Batch", "No jobs to render.")
            return
        self.destroy()
        self._on_start(jobs)


if __name__ == "__main__":
    App().mainloop()
