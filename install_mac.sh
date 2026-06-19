#!/usr/bin/env bash
# install_mac.sh — Install Podcast Video Generator on macOS
# Installs ffmpeg, Python dependencies, and creates a launchable .app bundle.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/podcast-video-gui}"
VENV_DIR="$APP_DIR/whisper-env"
APP_BUNDLE="$HOME/Applications/PodcastVideoGUI.app"

echo "============================================"
echo "  Podcast Video Generator — macOS Installer"
echo "============================================"

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
    echo ""
    echo "==> Homebrew not found. Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Homebrew on Apple Silicon writes to /opt/homebrew; on Intel to /usr/local
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi

echo ""
echo "==> Updating Homebrew…"
brew update --quiet

# ── 2. ffmpeg ─────────────────────────────────────────────────────────────────
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "==> Installing ffmpeg…"
    brew install ffmpeg
else
    echo "==> ffmpeg already installed: $(ffmpeg -version 2>&1 | head -1)"
fi

# ── 3. Python 3 ───────────────────────────────────────────────────────────────
# Prefer brew Python for a consistent venv that includes Tk.
if ! brew list python3 &>/dev/null; then
    echo "==> Installing python3 via Homebrew…"
    brew install python3
fi

BREW_PYTHON="$(brew --prefix python3)/bin/python3"
if [ ! -x "$BREW_PYTHON" ]; then
    # Fallback: find any python3.x binary
    BREW_PYTHON="$(brew --prefix)/bin/python3"
fi
echo "==> Using Python: $BREW_PYTHON  ($(${BREW_PYTHON} --version))"

# ── 4. python-tk (Tk support inside the Homebrew Python) ────────────────────
# Homebrew ships python-tk as a separate formula on macOS.
PYVER="$($BREW_PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! brew list "python-tk@${PYVER}" &>/dev/null 2>&1; then
    echo "==> Installing python-tk@${PYVER} for Tkinter support…"
    brew install "python-tk@${PYVER}" || echo "!! Could not install python-tk@${PYVER} — Tkinter may not work."
fi

# ── 5. App directory & files ──────────────────────────────────────────────────
echo ""
echo "==> Creating app directory at $APP_DIR"
mkdir -p "$APP_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> Copying app files from $SCRIPT_DIR to $APP_DIR"
cp -v "$SCRIPT_DIR/podcast_video_gui.py" "$APP_DIR/"
cp -v "$SCRIPT_DIR/render_engine.py"     "$APP_DIR/"
cp -v "$SCRIPT_DIR/web_app.py"           "$APP_DIR/"
[ -f "$SCRIPT_DIR/make_podcast_video.sh" ] && { cp -v "$SCRIPT_DIR/make_podcast_video.sh" "$APP_DIR/"; chmod +x "$APP_DIR/make_podcast_video.sh"; }
[ -d "$SCRIPT_DIR/presets"   ] && cp -rv "$SCRIPT_DIR/presets"   "$APP_DIR/"
[ -d "$SCRIPT_DIR/templates" ] && cp -rv "$SCRIPT_DIR/templates" "$APP_DIR/"
[ -d "$SCRIPT_DIR/art"       ] && cp -rv "$SCRIPT_DIR/art"       "$APP_DIR/"
mkdir -p "$APP_DIR/generated_clips" "$APP_DIR/uploads" "$APP_DIR/output" "$APP_DIR/art"

# ── 6. Python virtual environment ─────────────────────────────────────────────
echo ""
echo "==> Creating Python virtual environment at $VENV_DIR"
"$BREW_PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet

echo "==> Installing Python packages (this downloads AI models the first run — may take a while)…"
"$VENV_DIR/bin/pip" install \
    faster-whisper \
    pillow \
    tkinterdnd2 \
    flask \
    gunicorn \
    python-docx

# ── 7. Launcher scripts ───────────────────────────────────────────────────────
cat > "$APP_DIR/run.sh" <<LAUNCHER
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/python3" podcast_video_gui.py
LAUNCHER
chmod +x "$APP_DIR/run.sh"

cat > "$APP_DIR/run_web.sh" <<LAUNCHER
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/gunicorn" -w 1 -b 0.0.0.0:8000 --timeout 3600 web_app:app
LAUNCHER
chmod +x "$APP_DIR/run_web.sh"

# ── 8. macOS .app bundle (built with osacompile so macOS trusts it natively) ──
echo ""
echo "==> Creating macOS app bundle at $APP_BUNDLE"
mkdir -p "$HOME/Applications"
rm -rf "$APP_BUNDLE"

# Write a temporary AppleScript that launches the Python GUI in the background
_TMPAS=$(mktemp /tmp/pvg_XXXXXX.applescript)
cat > "$_TMPAS" <<APPLEFLOW
do shell script "$VENV_DIR/bin/python3 $APP_DIR/podcast_video_gui.py > /tmp/podcast_gui.log 2>&1 &"
APPLEFLOW

osacompile -o "$APP_BUNDLE" "$_TMPAS"
rm "$_TMPAS"

# Give it a friendly display name
/usr/libexec/PlistBuddy -c "Set :CFBundleName 'Podcast Video GUI'"        "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName 'Podcast Video GUI'" "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || true

# ── 9. Ollama (optional — for the AI outline feature) ─────────────────────────
echo ""
if ! command -v ollama >/dev/null 2>&1; then
    read -r -p "==> Install Ollama for the AI outline feature? (y/N) " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        if brew list ollama &>/dev/null 2>&1; then
            echo "==> Ollama already installed via brew."
        else
            echo "==> Installing Ollama…"
            brew install ollama
        fi
        echo "==> Starting Ollama service…"
        brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
        sleep 3
        echo "==> Pulling llama3.1 model (this may take several minutes)…"
        ollama pull llama3.1 || echo "!! Pull failed — run 'ollama pull llama3.1' later."
    else
        echo "==> Skipping Ollama install. You can install it later with: brew install ollama"
    fi
else
    echo "==> Ollama already installed: $(ollama --version 2>/dev/null || echo 'found')"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Install complete!"
echo "============================================"
echo ""
echo "Desktop GUI app: open $APP_BUNDLE"
echo "  Or run directly: $APP_DIR/run.sh"
echo ""
echo "Web UI:  cd $APP_DIR && ./run_web.sh"
echo "  Then open http://localhost:8000 in a browser"
echo ""
echo "Tip: Drag 'Podcast Video GUI' from ~/Applications to your Dock."
echo ""

# Offer to open the app now
read -r -p "==> Launch the desktop app now? (y/N) " launch_ans
if [[ "$launch_ans" =~ ^[Yy]$ ]]; then
    open "$APP_BUNDLE"
fi
