#!/usr/bin/env bash
# update.sh — Install or update the Podcast Video Generator from git.
#
# First install (fresh machine):
#   bash update.sh
#
# Subsequent updates (pull latest code + pip upgrade):
#   bash ~/podcast-video-gui/update.sh
#
# Override defaults:
#   APP_DIR=/srv/podcast-video  bash update.sh
#   REPO_URL=https://github.com/yourname/repo  bash update.sh

set -euo pipefail

# ── Configurable defaults ──────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-$HOME/podcast-video-gui}"
VENV_DIR="${VENV_DIR:-$HOME/whisper-env}"   # must match render_engine.py's PYTHON_VENV
SERVICE_NAME="podcast-video-web"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" && pwd 2>/dev/null || pwd)"

# ── Helpers ───────────────────────────────────────────────────────────────────
bar()  { echo ""; echo "══════════════════════════════════════════════════════"; }
ok()   { echo "  ✓ $*"; }
err()  { echo "  ✗ $*"; exit 1; }
info() { echo "  · $*"; }

bar
echo "  Podcast Video Generator — Install / Update"
bar
echo ""

# ── Resolve REPO_URL ──────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-}"

# Prefer the remote of the git repo this script lives in
if [[ -z "$REPO_URL" ]]; then
    REPO_URL="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)"
fi

# If APP_DIR already has a git repo, use that remote (in-place update path)
if [[ -z "$REPO_URL" && -d "$APP_DIR/.git" ]]; then
    REPO_URL="$(git -C "$APP_DIR" remote get-url origin 2>/dev/null || true)"
fi

# Still nothing? Ask.
if [[ -z "$REPO_URL" ]]; then
    echo ""
    read -rp "  Enter git repo URL (e.g. https://github.com/yourname/repo): " REPO_URL
    [[ -z "$REPO_URL" ]] && err "No repo URL given — cannot continue."
fi

info "Repo    : $REPO_URL"
info "App dir : $APP_DIR"
info "Venv    : $VENV_DIR"

# ── Clone or pull ─────────────────────────────────────────────────────────────
bar
if [[ -d "$APP_DIR/.git" ]]; then
    echo "  Pulling latest code…"
    bar
    git -C "$APP_DIR" fetch --prune
    git -C "$APP_DIR" pull --ff-only
    ok "Code up to date."
else
    echo "  Setting up $APP_DIR…"
    bar
    if [[ -d "$APP_DIR" && -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
        # Non-empty directory — previous non-git install exists.
        # Clone into a temp location then overlay just the code files,
        # leaving user data (uploads/, output/, art/, generated_clips/) untouched.
        info "Existing install found — overlaying new code onto $APP_DIR…"
        TMP="$(mktemp -d)"
        git clone --quiet "$REPO_URL" "$TMP"
        for f in web_app.py render_engine.py podcast_video_gui.py update.sh \
                  install_rocky.sh make_podcast_video.sh; do
            [[ -f "$TMP/$f" ]] && cp -f "$TMP/$f" "$APP_DIR/$f" && info "  updated: $f"
        done
        for d in templates; do
            [[ -d "$TMP/$d" ]] && cp -rf "$TMP/$d" "$APP_DIR/"
        done
        # Initialise as a proper git worktree for future updates
        cp -rf "$TMP/.git" "$APP_DIR/"
        rm -rf "$TMP"
        ok "Existing install converted to git repo."
    else
        mkdir -p "$APP_DIR"
        git clone "$REPO_URL" "$APP_DIR"
        ok "Cloned to $APP_DIR."
    fi
fi

# Ensure data dirs (excluded from git) always exist
mkdir -p "$APP_DIR/uploads" "$APP_DIR/output" \
         "$APP_DIR/art" "$APP_DIR/generated_clips" "$APP_DIR/presets"

GIT_REV="$(git -C "$APP_DIR" log -1 --format='%h — %s' 2>/dev/null || echo 'unknown')"
ok "Version: $GIT_REV"

# ── System packages ───────────────────────────────────────────────────────────
# Linux
if [[ "$OSTYPE" == "linux"* ]]; then
    bar
    echo "  Checking system packages…"
    bar

    _install_pkgs() {
        if command -v dnf &>/dev/null; then
            sudo dnf install -y "$@"
        elif command -v apt-get &>/dev/null; then
            sudo apt-get install -y "$@"
        else
            echo "  ⚠  Cannot auto-install packages. Please install manually: $*"
        fi
    }

    command -v git      &>/dev/null || _install_pkgs git
    command -v python3  &>/dev/null || _install_pkgs python3 python3-pip
    # venv module check
    python3 -c "import venv" 2>/dev/null || _install_pkgs python3-venv || true

    if ! command -v ffmpeg &>/dev/null; then
        info "ffmpeg not found — trying RPM Fusion…"
        if command -v dnf &>/dev/null; then
            RHEL_VER="$(rpm -E %rhel 2>/dev/null || echo 9)"
            sudo dnf install -y \
                "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-${RHEL_VER}.noarch.rpm" \
                "https://download1.rpmfusion.org/nonfree/el/rpmfusion-nonfree-release-${RHEL_VER}.noarch.rpm" \
                2>/dev/null || true
            sudo dnf config-manager --set-enabled crb 2>/dev/null || \
                sudo dnf config-manager --set-enabled powertools 2>/dev/null || true
            sudo dnf install -y --allowerasing ffmpeg 2>/dev/null || true
        fi
    fi

    # Static build last resort
    if ! command -v ffmpeg &>/dev/null; then
        info "Falling back to static ffmpeg from johnvansickle.com…"
        TMP_FF="$(mktemp -d)"
        curl -L -o "$TMP_FF/ff.tar.xz" \
            https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
        tar -xJf "$TMP_FF/ff.tar.xz" -C "$TMP_FF"
        FF_DIR="$(find "$TMP_FF" -maxdepth 1 -type d -name "ffmpeg-*" | head -1)"
        sudo install -m 755 "$FF_DIR/ffmpeg" "$FF_DIR/ffprobe" /usr/local/bin/
        rm -rf "$TMP_FF"
        ok "ffmpeg installed (static build)."
    fi

    ok "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
fi

# macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
    bar
    echo "  Checking macOS dependencies…"
    bar
    if ! command -v brew &>/dev/null; then
        info "Homebrew not found — installing…"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    if ! command -v ffmpeg &>/dev/null; then
        info "Installing ffmpeg via Homebrew…"
        brew install ffmpeg
    fi
    ok "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
bar
echo "  Setting up Python venv at $VENV_DIR…"
bar

PYTHON_BIN=""
for c in python3.11 python3.12 python3.10 python3; do
    if command -v "$c" &>/dev/null; then
        PYTHON_BIN="$(command -v "$c")"
        break
    fi
done
[[ -z "$PYTHON_BIN" ]] && err "Python 3.10+ not found. Install it and re-run."
info "Using $PYTHON_BIN ($(${PYTHON_BIN} --version 2>&1))"

if [[ ! -f "$VENV_DIR/bin/python3" ]]; then
    info "Creating venv…"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip -q

info "Installing / upgrading Python packages…"
"$VENV_DIR/bin/pip" install --upgrade \
    faster-whisper \
    pillow \
    flask \
    gunicorn \
    python-docx \
    -q

ok "Python packages up to date."

# ── Launcher scripts ──────────────────────────────────────────────────────────
bar
echo "  Writing launcher scripts…"
bar

cat > "$APP_DIR/run_web.sh" <<LAUNCHER
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/gunicorn" -w 1 -b 0.0.0.0:8000 --timeout 3600 web_app:app
LAUNCHER
chmod +x "$APP_DIR/run_web.sh"

cat > "$APP_DIR/run.sh" <<LAUNCHER
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/python3" podcast_video_gui.py
LAUNCHER
chmod +x "$APP_DIR/run.sh"

ok "run.sh and run_web.sh written."

# ── Systemd service (Linux only) ──────────────────────────────────────────────
if command -v systemctl &>/dev/null; then
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    bar
    echo "  Configuring systemd service '$SERVICE_NAME'…"
    bar

    if [[ ! -f "$SERVICE_FILE" ]]; then
        info "Installing service unit…"
        sudo tee "$SERVICE_FILE" > /dev/null <<UNIT
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
UNIT

        # SELinux: relabel so systemd can exec the launcher and venv binaries
        if command -v getenforce &>/dev/null && [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
            info "Configuring SELinux file contexts…"
            command -v semanage &>/dev/null || sudo dnf install -y policycoreutils-python-utils -q
            sudo semanage fcontext -a -t bin_t "$APP_DIR/run_web.sh" 2>/dev/null || \
                sudo semanage fcontext -m -t bin_t "$APP_DIR/run_web.sh"
            sudo semanage fcontext -a -t bin_t "$VENV_DIR/bin(/.*)?" 2>/dev/null || \
                sudo semanage fcontext -m -t bin_t "$VENV_DIR/bin(/.*)?"
            sudo restorecon -Rv "$APP_DIR" >/dev/null
        fi

        sudo systemctl enable "$SERVICE_NAME"
        ok "Service installed and enabled."
    fi

    sudo systemctl daemon-reload
    sudo systemctl restart "$SERVICE_NAME"
    SVC_STATUS="$(sudo systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"
    ok "Service status: $SVC_STATUS"

    # Open firewall port if firewalld is running
    if command -v firewall-cmd &>/dev/null && \
       sudo firewall-cmd --state &>/dev/null 2>&1; then
        sudo firewall-cmd --permanent --add-port=8000/tcp --quiet 2>/dev/null || true
        sudo firewall-cmd --reload --quiet
        ok "Firewall: port 8000 open."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
bar
echo ""
# macOS uses `hostname`, Linux uses `hostname -I`
if [[ "$OSTYPE" == "darwin"* ]]; then
    IPADDR="$(ipconfig getifaddr en0 2>/dev/null || hostname)"
else
    IPADDR="$(hostname -I 2>/dev/null | awk '{print $1}' || hostname)"
fi

echo "  ✓  All done."
echo ""
echo "  Version  : $GIT_REV"
echo "  Web UI   : http://${IPADDR}:8000"
echo "  Desktop  : $APP_DIR/run.sh"
echo ""
if command -v systemctl &>/dev/null && \
   sudo systemctl is-enabled "$SERVICE_NAME" &>/dev/null 2>&1; then
    echo "  Service  : sudo systemctl status|restart|stop $SERVICE_NAME"
fi
echo ""
