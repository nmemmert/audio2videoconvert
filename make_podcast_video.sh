#!/bin/bash
# ============================================================
#  Verse by Verse — Pulsing Glow Podcast Video Generator
#  Auto-transcribes .m4a files and burns in styled captions
#  Usage: ./make_podcast_video.sh  (from anywhere)
# ============================================================

# Set OpenMP fix
export KMP_DUPLICATE_LIB_OK=TRUE

# Change to the directory where this script lives
cd "$(dirname "$0")"

# ============================================================
#  BOOTSTRAP — installs all dependencies on first run
# ============================================================
PYTHON="$HOME/whisper-env/bin/python3"

bootstrap() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  First Run — Installing Dependencies"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  # Homebrew
  if ! command -v brew &>/dev/null; then
    echo "📦 Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  else
    echo "✅ Homebrew already installed"
  fi

  # ffmpeg with subtitle support
  if ! command -v ffmpeg &>/dev/null || ! ffmpeg -filters 2>&1 | grep -q subtitles; then
    echo "📦 Installing ffmpeg with subtitle support..."
    brew tap homebrew-ffmpeg/ffmpeg 2>/dev/null
    brew install homebrew-ffmpeg/ffmpeg/ffmpeg 2>/dev/null || brew install ffmpeg
  else
    echo "✅ ffmpeg already installed"
  fi

  # Python 3.11
  if ! command -v python3.11 &>/dev/null; then
    echo "📦 Installing Python 3.11..."
    brew install python@3.11
  else
    echo "✅ Python 3.11 already installed"
  fi

  # cmake
  if ! command -v cmake &>/dev/null; then
    echo "📦 Installing cmake..."
    brew install cmake
  else
    echo "✅ cmake already installed"
  fi

  # Python virtual environment and packages
  if [[ ! -f "$PYTHON" ]]; then
    echo "📦 Creating Python virtual environment..."
    python3.11 -m venv "$HOME/whisper-env"
    source "$HOME/whisper-env/bin/activate"
    pip install --upgrade pip setuptools -q
    echo "📦 Installing faster-whisper and dependencies..."
    pip install faster-whisper stable-ts --no-deps -q
    pip install numpy ctranslate2 tokenizers huggingface-hub tqdm av -q
    pip install onnxruntime -q
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu -q
    pip install "numpy<2" -q
    pip install openai-whisper --no-deps -q
    pip install tiktoken more-itertools -q
    echo "✅ Python packages installed"
  else
    echo "✅ Python virtual environment already exists"
  fi

  echo ""
  echo "✅ Bootstrap complete — continuing..."
  echo ""
}

# Check if bootstrap is needed
NEEDS_BOOTSTRAP=false
[[ ! -f "$PYTHON" ]] && NEEDS_BOOTSTRAP=true
! command -v ffmpeg &>/dev/null && NEEDS_BOOTSTRAP=true
if command -v ffmpeg &>/dev/null; then
  ffmpeg -filters 2>&1 | grep -q subtitles || NEEDS_BOOTSTRAP=true
fi

[[ "$NEEDS_BOOTSTRAP" == true ]] && bootstrap

# Activate virtual environment
source "$HOME/whisper-env/bin/activate" 2>/dev/null

# --- CONFIG (edit these) ------------------------------------
ART="podcast-art.jpeg"
ART_SIZE=700
BG_COLOR="black"
GLOW_COLOR="#c9a84c"
GLOW_SIGMA=220
PULSE_SPEED=3
BREATHE_AMOUNT=0.02
WIDTH=1920
HEIGHT=1080
FPS=30

# Caption styling
FONT_NAME="Cormorant Garamond"
FONT_FALLBACK="Georgia"
FONT_SIZE=72                      # Readable but not overwhelming
WORDS_PER_CHUNK=2                 # Max words shown at once
CAPTION_COLOR="&H50D8EAF0"        # Dim warm white (unspoken) — BGR format
CAPTION_HIGHLIGHT="&H004CA8C9"    # Antique gold (active word) — BGR format
CAPTION_BACK="&HBF000000"         # Dark pill background (75% opacity)
CAPTION_Y=900                     # Y position from top (below art)
# ------------------------------------------------------------

GLOW_LOOP="/tmp/vbvn_glow_loop.mp4"
PASS=true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Verse by Verse — Preflight Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# --- Check ffmpeg ---
if command -v ffmpeg &>/dev/null; then
  echo "✅ ffmpeg       : $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
else
  echo "❌ ffmpeg       : Not found — install with: brew install ffmpeg"
  PASS=false
fi

# --- Check ffprobe ---
if command -v ffprobe &>/dev/null; then
  echo "✅ ffprobe      : found"
else
  echo "❌ ffprobe      : Not found — install with: brew install ffmpeg"
  PASS=false
fi

# --- Check Python venv ---
if [[ -f "$PYTHON" ]]; then
  PYVER=$("$PYTHON" --version 2>&1 | awk '{print $2}')
  echo "✅ python3      : $PYVER (whisper-env)"
else
  echo "❌ python3      : whisper-env not found at $PYTHON"
  echo "   Run: python3.11 -m venv ~/whisper-env && source ~/whisper-env/bin/activate && pip install openai-whisper stable-ts --no-deps && pip install numpy torch tqdm more-itertools tiktoken regex"
  PASS=false
fi

# --- Check faster-whisper ---
if "$PYTHON" -W ignore -c "import faster_whisper" 2>/dev/null; then
  echo "✅ faster-whisper: found"
else
  echo "❌ faster-whisper: Not found — activate venv and run: pip install faster-whisper"
  PASS=false
fi

# --- Check podcast art ---
if [[ -f "$ART" ]]; then
  echo "✅ podcast art  : $ART"
else
  echo "❌ podcast art  : Not found ($ART)"
  PASS=false
fi

# --- Check audio files ---
AUDIO_FILES=()
for f in *.m4a *.mp3; do [[ -e "$f" ]] && AUDIO_FILES+=("$f"); done
if [[ ${#AUDIO_FILES[@]} -gt 0 ]]; then
  echo "✅ audio files  : ${#AUDIO_FILES[@]} found"
  for f in "${AUDIO_FILES[@]}"; do
    echo "    • $f"
  done
else
  echo "❌ audio files  : No .m4a or .mp3 files found"
  PASS=false
fi

# --- Check font ---
echo ""
FONT_AVAILABLE=$(fc-list 2>/dev/null | grep -i "cormorant" | head -1)
if [[ -n "$FONT_AVAILABLE" ]]; then
  echo "✅ font         : Cormorant Garamond found"
  FONT_NAME="Cormorant Garamond"
else
  echo "⚠️  font         : Cormorant Garamond not found — using $FONT_FALLBACK"
  echo "   Install from: https://fonts.google.com/specimen/Cormorant+Garamond"
  FONT_NAME="$FONT_FALLBACK"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ "$PASS" != true ]]; then
  echo ""
  echo "❌ Preflight failed — fix the issues above and try again."
  echo ""
  exit 1
fi

echo ""
echo "✅ All checks passed — starting conversion..."
echo ""

# ------------------------------------------------------------
# Generate glow loop once
# ------------------------------------------------------------
echo "🔆 Generating glow loop..."
HALF=$(echo "$PULSE_SPEED / 2" | bc -l)
GLOW_SRC=600
GLOW_PAD=700
CANVAS=$((GLOW_SRC + GLOW_PAD * 2))
ffmpeg \
  -f lavfi -i "color=c=${GLOW_COLOR}:s=${GLOW_SRC}x${GLOW_SRC}:r=${FPS}" \
  -filter_complex "[0:v]pad=${CANVAS}:${CANVAS}:${GLOW_PAD}:${GLOW_PAD}:black,gblur=sigma=${GLOW_SIGMA},fade=t=in:st=0:d=${HALF}:color=black,fade=t=out:st=${HALF}:d=${HALF}:color=black[out]" \
  -map "[out]" -c:v libx264 -preset fast -crf 18 \
  -t "$PULSE_SPEED" "$GLOW_LOOP" -y -loglevel error
echo ""

# ------------------------------------------------------------
# Process each audio file
# ------------------------------------------------------------
BREATHE_FRAMES=$((FPS * PULSE_SPEED))
SUCCESS=0
FAILED=0

# Build audio file list
AUDIO_FILES=()
for f in *.m4a *.mp3; do
  [[ -e "$f" ]] && AUDIO_FILES+=("$f")
done

if [[ ${#AUDIO_FILES[@]} -eq 0 ]]; then
  echo "❌ No .m4a or .mp3 files found."
  exit 1
fi

echo "🎙  Processing ${#AUDIO_FILES[@]} file(s)..."
echo ""

for AUDIO in "${AUDIO_FILES[@]}"; do
  BASE="${AUDIO%.m4a}"
  BASE="${BASE%.mp3}"
  OUTPUT="${BASE}.mp4"
  ASS="${BASE}.ass"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "🎙  Audio  : $AUDIO"
  echo "🎬  Output : $OUTPUT"

  # Get duration
  DURATION=$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$AUDIO" 2>/dev/null)
  if [[ -z "$DURATION" ]]; then
    DURATION=$(ffmpeg -i "$AUDIO" 2>&1 | grep "Duration" | \
      awk '{print $2}' | tr -d ',' | \
      awk -F: '{ print ($1 * 3600) + ($2 * 60) + $3 }')
  fi
  if [[ -z "$DURATION" ]]; then
    echo "❌ Could not read duration — skipping."
    FAILED=$((FAILED + 1))
    continue
  fi
  echo "⏱  Duration: $(printf '%.0f' $DURATION)s"
  echo ""

  # --- Transcribe with stable-ts (word-level timestamps) ---
  echo "📝 Transcribing with Whisper..."

  export KMP_DUPLICATE_LIB_OK=TRUE
  "$PYTHON" -W ignore << PYEOF
import warnings
warnings.filterwarnings("ignore")
from faster_whisper import WhisperModel

model = WhisperModel("base", device="cpu", compute_type="int8")
segments, info = model.transcribe("${AUDIO}", word_timestamps=True)
segments = list(segments)

CHUNK_SIZE = ${WORDS_PER_CHUNK}

def fmt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

# ASS header — BorderStyle=4 gives us the pill/box background
ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,${FONT_NAME},${FONT_SIZE},${CAPTION_HIGHLIGHT},${CAPTION_COLOR},&H00000000,${CAPTION_BACK},-1,0,0,0,100,100,1,0,4,0,0,2,60,60,${CAPTION_Y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

lines = [ass_header]

# Collect all words across all segments
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

# Split into chunks of CHUNK_SIZE words
chunks = [all_words[i:i+CHUNK_SIZE] for i in range(0, len(all_words), CHUNK_SIZE)]

for chunk in chunks:
    chunk_start = fmt_time(chunk[0].start)
    chunk_end = fmt_time(chunk[-1].end)

    # Build a single dialogue line with \k karaoke tags
    # \k duration is in centiseconds
    parts = []
    for w in chunk:
        duration_cs = max(1, int((w.end - w.start) * 100))
        # Gold highlight on active word via karaoke
        parts.append("{\k" + str(duration_cs) + "}" + w.word.strip())

    text = " ".join(parts)
    lines.append(f"Dialogue: 0,{chunk_start},{chunk_end},Default,,0,0,0,,{text}")

with open("${ASS}", "w") as f:
    f.write("\n".join(lines))

print("✅ Transcription complete — ${ASS} written")
PYEOF

  if [[ ! -f "$ASS" ]]; then
    echo "❌ Transcription failed — rendering without captions."
    SUB_FILTER=""
  else
    # Escape the path for ffmpeg filter (escape colons and backslashes)
    # Copy to /tmp with simple name to avoid escaping issues
    cp "$ASS" /tmp/vbvn_captions.ass
    SUB_FILTER=",subtitles=/tmp/vbvn_captions.ass"
  fi

  # --- Build video ---
  echo ""
  echo "🎬 Rendering video..."

  # Copy audio to safe temp path to avoid special character issues
  AUDIO_SAFE="/tmp/vbvn_audio_input.m4a"
  cp "$AUDIO" "$AUDIO_SAFE"

  ffmpeg \
    -loop 1 -framerate $FPS -i "$ART" \
    -stream_loop -1 -i "$GLOW_LOOP" \
    -i "$AUDIO_SAFE" \
    -filter_complex "
      [0:v]scale=$((ART_SIZE + 20)):$((ART_SIZE + 20)),zoompan=z='1+${BREATHE_AMOUNT}*sin(2*PI*on/${BREATHE_FRAMES})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=$((ART_SIZE + 20))x$((ART_SIZE + 20)):fps=${FPS},scale=${ART_SIZE}:${ART_SIZE},format=rgb24[art_sharp];
      [1:v]format=rgb24[glow_pulsed];
      color=c=${BG_COLOR}:s=${WIDTH}x${HEIGHT}:r=${FPS}[bg];
      [bg][glow_pulsed]overlay=(W-w)/2:(H-h)/2:format=auto[bg_glow];
      [bg_glow][art_sharp]overlay=(W-w)/2:(H-h)/2:format=auto${SUB_FILTER}[out]
    " \
    -map "[out]" \
    -map 2:a \
    -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
    -af "loudnorm=I=-16:TP=-1.5:LRA=11" \
    -c:a aac -b:a 192k -ar 44100 \
    -t "$DURATION" \
    -movflags +faststart \
    -loglevel warning \
    "$OUTPUT.tmp.mp4" -y 2>&1 | grep -v -E "deprecated pixel format|swscaler|timescale not set|Last message repeated"

  FFMPEG_EXIT=${PIPESTATUS[0]}
  if [[ $FFMPEG_EXIT -eq 0 ]]; then
    # Trim to exact audio duration to fix Spotify video/audio length mismatch
    AUDIO_DUR=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration \
      -of default=noprint_wrappers=1:nokey=1 "$OUTPUT.tmp.mp4" 2>/dev/null)
    if [[ -n "$AUDIO_DUR" ]] && ffmpeg -i "$OUTPUT.tmp.mp4" -t "$AUDIO_DUR" \
      -c copy -movflags +faststart \
      "$OUTPUT" -y -loglevel error; then
      rm -f "$OUTPUT.tmp.mp4"
      SIZE=$(du -sh "$OUTPUT" | cut -f1)
      echo "✅ Done! → $OUTPUT ($SIZE)"
      SUCCESS=$((SUCCESS + 1))
    else
      # Trim failed — fall back to the untrimmed render
      mv "$OUTPUT.tmp.mp4" "$OUTPUT"
      SIZE=$(du -sh "$OUTPUT" | cut -f1)
      echo "⚠️  Trim step failed — using untrimmed output → $OUTPUT ($SIZE)"
      SUCCESS=$((SUCCESS + 1))
    fi
  else
    echo ""
    echo "❌ Failed: $AUDIO"
    FAILED=$((FAILED + 1))
  fi
  echo ""
done

# Cleanup
rm -f "$GLOW_LOOP"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Completed : $SUCCESS"
if [[ $FAILED -gt 0 ]]; then
  echo "❌ Failed    : $FAILED"
fi
echo ""