#!/usr/bin/env bash
# Install Podcast Video GUI and all dependencies on Rocky Linux.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/podcast-video-gui}"
VENV_DIR="$APP_DIR/whisper-env"

echo "==> Installing system packages (requires sudo)"
sudo dnf install -y epel-release
sudo dnf install -y \
    python3 python3-pip python3-tkinter \
    gcc gcc-c++ make \
    dejavu-sans-fonts dejavu-serif-fonts liberation-fonts \
    git

# Intel iGPU VAAPI hardware video encoding (offloads ffmpeg's H.264 encode from the CPU).
# Harmless to install even if there's no usable iGPU - render_engine.py detects
# /dev/dri/renderD128 + h264_vaapi support at runtime and falls back to libx264.
echo "==> Installing VAAPI drivers (Intel iGPU hardware encoding, if present)"
sudo dnf install -y libva libva-utils intel-media-driver libva-intel-driver 2>/dev/null || true

# Allow access to /dev/dri/renderD128 for VAAPI
if [ -e /dev/dri/renderD128 ]; then
    sudo usermod -aG render,video "$USER" || true
fi

# ffmpeg is not in EPEL/AppStream — it comes from RPM Fusion.
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "==> ffmpeg not found, enabling RPM Fusion"
    RHEL_VER="$(rpm -E %rhel)"
    sudo dnf install -y \
        "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-${RHEL_VER}.noarch.rpm" \
        "https://download1.rpmfusion.org/nonfree/el/rpmfusion-nonfree-release-${RHEL_VER}.noarch.rpm" || true
    sudo dnf config-manager --set-enabled crb 2>/dev/null || sudo dnf config-manager --set-enabled powertools 2>/dev/null || true
    sudo dnf install -y --allowerasing ffmpeg ffmpeg-devel || sudo dnf install -y --allowerasing ffmpeg

    if ! command -v ffmpeg >/dev/null 2>&1; then
        echo ""
        echo "!! RPM Fusion does not yet have packages for RHEL ${RHEL_VER} (common for new major releases)."
        echo "!! Falling back to a static ffmpeg build from johnvansickle.com."
        TMP_FF=$(mktemp -d)
        curl -L -o "$TMP_FF/ffmpeg.tar.xz" https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
        tar -xJf "$TMP_FF/ffmpeg.tar.xz" -C "$TMP_FF"
        FF_DIR=$(find "$TMP_FF" -maxdepth 1 -type d -name "ffmpeg-*")
        sudo install -m 755 "$FF_DIR/ffmpeg" "$FF_DIR/ffprobe" /usr/local/bin/
        rm -rf "$TMP_FF"
    fi
fi

# Ollama (for the optional left-side AI-generated outline feature)
if ! command -v ollama >/dev/null 2>&1; then
    echo "==> Installing Ollama"
    curl -fsSL https://ollama.com/install.sh | sh
fi
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now ollama 2>/dev/null || true
fi
echo "==> Pulling Ollama model (llama3.1) — this may take a while"
ollama pull llama3.1 || echo "!! Could not pull llama3.1 yet — pull it later with: ollama pull llama3.1"

echo "==> Creating app directory at $APP_DIR"
mkdir -p "$APP_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> Copying app files from $SCRIPT_DIR to $APP_DIR"
cp -v "$SCRIPT_DIR/podcast_video_gui.py" "$APP_DIR/"
cp -v "$SCRIPT_DIR/render_engine.py" "$APP_DIR/"
cp -v "$SCRIPT_DIR/web_app.py" "$APP_DIR/"
cp -v "$SCRIPT_DIR/make_podcast_video.sh" "$APP_DIR/" 2>/dev/null || true
chmod +x "$APP_DIR/make_podcast_video.sh" 2>/dev/null || true
[ -d "$SCRIPT_DIR/presets" ] && cp -rv "$SCRIPT_DIR/presets" "$APP_DIR/"
[ -d "$SCRIPT_DIR/templates" ] && cp -rv "$SCRIPT_DIR/templates" "$APP_DIR/"
[ -d "$SCRIPT_DIR/art" ] && cp -rv "$SCRIPT_DIR/art" "$APP_DIR/"
mkdir -p "$APP_DIR/generated_clips" "$APP_DIR/uploads" "$APP_DIR/output" "$APP_DIR/art"

echo "==> Creating Python virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
echo "==> Installing Python dependencies (this may take a while - downloads CUDA/CPU torch)"
"$VENV_DIR/bin/pip" install faster-whisper pillow tkinterdnd2 flask gunicorn python-docx

cat > "$APP_DIR/run.sh" <<EOF
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/python3" podcast_video_gui.py
EOF
chmod +x "$APP_DIR/run.sh"

cat > "$APP_DIR/run_web.sh" <<EOF
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/gunicorn" -w 1 -b 0.0.0.0:8000 --timeout 3600 web_app:app
EOF
chmod +x "$APP_DIR/run_web.sh"

# Desktop launcher (for GUI desktop environments)
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/podcast-video-gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Podcast Video GUI
Exec=$APP_DIR/run.sh
Path=$APP_DIR
Terminal=false
Categories=AudioVideo;
EOF

# Optional systemd service for the web interface
if command -v systemctl >/dev/null 2>&1; then
    echo "==> Installing systemd service 'podcast-video-web' (port 8000)"
    sudo tee /etc/systemd/system/podcast-video-web.service > /dev/null <<EOF
[Unit]
Description=Podcast Video Generator Web UI
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run_web.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    # SELinux blocks systemd from executing scripts/binaries labeled user_home_t.
    # Relabel the venv's bin dir and the launcher so the service can exec them.
    if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
        echo "==> Configuring SELinux file contexts for $APP_DIR"
        if ! command -v semanage >/dev/null 2>&1; then
            sudo dnf install -y policycoreutils-python-utils
        fi
        sudo semanage fcontext -a -t bin_t "$APP_DIR/run_web.sh" 2>/dev/null || \
            sudo semanage fcontext -m -t bin_t "$APP_DIR/run_web.sh"
        sudo semanage fcontext -a -t bin_t "$VENV_DIR/bin(/.*)?" 2>/dev/null || \
            sudo semanage fcontext -m -t bin_t "$VENV_DIR/bin(/.*)?"
        sudo restorecon -Rv "$APP_DIR" >/dev/null
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable --now podcast-video-web.service
    sudo systemctl reset-failed podcast-video-web.service 2>/dev/null || true
    sudo systemctl restart podcast-video-web.service
    echo "==> Web UI service started. Opening firewall port 8000 (if firewalld is active)..."
    if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
        sudo firewall-cmd --permanent --add-port=8000/tcp
        sudo firewall-cmd --reload
    fi
fi

echo ""
echo "==> Install complete."
echo "Desktop GUI:  $APP_DIR/run.sh"
echo "Web UI:       http://<this-server-ip>:8000   (started automatically via systemd)"
echo "  - manage with: sudo systemctl status|restart|stop podcast-video-web"
echo "Or find 'Podcast Video GUI' in your application menu."
