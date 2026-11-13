#!/bin/bash
# DenSemNotes installer for macOS.
# Copies the app into ~/Library/Application Support, sets up Python and the
# speech model, and adds "DenSemNotes" to your Applications folder.
# Your courses, notes, settings and speech model from DenSem Class (and older versions) carry over.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support"
HOME_DIR="$SUPPORT/DenSemNotes"
APP_DIR="$HOME_DIR/app"
APP="$HOME/Applications/DenSemNotes.app"

say_step(){ printf "\n\033[1;32m==>\033[0m %s\n" "$1"; }
UPDATE=0
[ "${1:-}" = "--update" ] && UPDATE=1   # run by the app's own updater: no Finder window, no model download

echo "DenSemNotes installer"
echo "This takes about 5 minutes and downloads 1 to 2.5 GB the first time."

# Bring data from earlier versions (the app turns the last lecture into a class on first start)
for OLD in "$SUPPORT/DenSem Class" "$SUPPORT/DenSem Lecture" "$SUPPORT/Lecture Catch-Up"; do
  if [ -d "$OLD" ] && [ ! -d "$HOME_DIR" ]; then
    say_step "Moving your settings and speech model from $(basename "$OLD")"
    mv "$OLD" "$HOME_DIR"
    rm -rf "$HOME_DIR/app"
  fi
done
rm -rf "$HOME/Applications/DenSem Class.app" "$HOME/Applications/DenSem Lecture.app" "$HOME/Applications/Lecture Catch-Up.app"
if [ -d "$HOME/Documents/DenSem Class" ] && [ ! -e "$HOME/Documents/DenSemNotes" ]; then
  mv "$HOME/Documents/DenSem Class" "$HOME/Documents/DenSemNotes"   # lessons and exports you saved
fi

say_step "Copying app files"
mkdir -p "$APP_DIR"
rm -f "$APP_DIR"/*.py
cp "$SRC"/*.py "$SRC/ui.html" "$SRC/requirements.txt" "$APP_DIR/"
cp "$SRC/assets/logo.svg" "$SRC/assets/AppIcon.icns" "$APP_DIR/"
mkdir -p "$APP_DIR/vendor"
cp "$SRC/template.md" "$APP_DIR/"
[ -f "$SRC/update.json" ] && cp "$SRC/update.json" "$APP_DIR/"
[ -f "$SRC/version.json" ] && cp "$SRC/version.json" "$APP_DIR/"
cp "$SRC/vendor/mermaid.min.js" "$APP_DIR/vendor/"

say_step "Getting Python (via uv)"
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
"$UV" venv --python 3.12 --allow-existing "$APP_DIR/.venv"

say_step "Installing speech recognition, slide viewer and window libraries"
"$UV" pip install --python "$APP_DIR/.venv/bin/python" -r "$APP_DIR/requirements.txt"

# Apple Silicon: Whisper on the GPU (MLX). Optional: if it can't be installed (older macOS, no internet),
# the app keeps using the CPU engine. Installed without its "torch" dependency, which is only for converting models.
if [ "$(uname -m)" = "arm64" ]; then
  say_step "Installing the Apple GPU speech engine"
  if "$UV" pip install --python "$APP_DIR/.venv/bin/python" "mlx>=0.11" numba scipy tiktoken more-itertools tqdm huggingface_hub \
     && "$UV" pip install --python "$APP_DIR/.venv/bin/python" --no-deps "mlx-whisper>=0.4.2"; then
    echo "Apple GPU speech engine installed."
  else
    echo "Couldn't install the Apple GPU speech engine; DenSemNotes will use the CPU engine instead."
  fi
fi

if [ "$UPDATE" = "0" ]; then
  say_step "Downloading the speech model (one time)"
  "$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" --download-model
fi

# Remember which version from GitHub this is, so the app can tell when main has something newer
if [ ! -f "$SRC/version.json" ] && [ -f "$SRC/update.json" ]; then
  "$APP_DIR/.venv/bin/python" - "$SRC" "$APP_DIR" <<'PY' || true
import json, os, subprocess, sys, urllib.request, ssl
src, app = sys.argv[1], sys.argv[2]
u = json.load(open(os.path.join(src, "update.json")))
sha = ""
try:
    sha = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
except Exception:
    pass
if not sha:
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(f"https://api.github.com/repos/{u['repo']}/commits/{u.get('branch', 'main')}",
                                     headers={"User-Agent": "DenSemNotes-installer"})
        sha = json.load(urllib.request.urlopen(req, context=ctx, timeout=20))["sha"]
    except Exception:
        sha = ""
json.dump({"repo": u["repo"], "sha": sha}, open(os.path.join(app, "version.json"), "w"))
PY
fi

say_step "Adding DenSemNotes to your Applications folder"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DenSemNotes</string>
  <key>CFBundleDisplayName</key><string>DenSemNotes</string>
  <key>CFBundleIdentifier</key><string>local.densemnotes</string>
  <key>CFBundleVersion</key><string>2.0</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>DenSemNotes</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>DenSemNotes listens to the lecture so it can transcribe and summarize it on your Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>DenSemNotes asks Keynote or Pages to turn your slides and documents into pages it can show next to your notes.</string>
</dict>
</plist>
PLIST
cat > "$APP/Contents/MacOS/DenSemNotes" <<LAUNCH
#!/bin/bash
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" >> "$HOME_DIR/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/DenSemNotes"
cp "$SRC/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
touch "$APP"
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true

say_step "Done"
echo "Open DenSemNotes from your Applications folder (or Spotlight: Cmd+Space, type 'DenSemNotes')."
echo "The first time you press Start listening, macOS asks for microphone access: click Allow."
echo "The first time you upload PowerPoint slides, macOS may ask to let DenSemNotes use Keynote: click OK."
# Show the app in Finder after a normal install. (Written as an if, not "[ ] && …": as the last line,
# a false test made the whole installer exit with 1 in update mode, so the app reported it had stopped.)
if [ "$UPDATE" = "0" ]; then
  open -R "$APP" || true
fi
exit 0
