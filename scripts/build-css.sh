#!/usr/bin/env bash
# Build the Tailwind CSS bundle. Uses the standalone CLI binary (no npm).
# - Downloads tailwindcss to ./tools/tailwindcss on first run.
# - Reads tailwind.config.js + static/css/app.input.css.
# - Writes minified output to static/css/app.css (committed).
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="v3.4.13"   # last 3.x release — config-file friendly, no postcss needed
PLATFORM=""
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  PLATFORM="linux-x64" ;;
  Linux-aarch64) PLATFORM="linux-arm64" ;;
  Darwin-arm64)  PLATFORM="macos-arm64" ;;
  Darwin-x86_64) PLATFORM="macos-x64" ;;
  *) echo "Unsupported platform: $(uname -s) $(uname -m)" >&2; exit 1 ;;
esac

BIN="tools/tailwindcss"
if [[ ! -x "$BIN" ]]; then
  echo "Fetching tailwindcss $VERSION ($PLATFORM)…"
  mkdir -p tools
  URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/tailwindcss-$PLATFORM"
  curl -sL --fail -o "$BIN" "$URL"
  chmod +x "$BIN"
fi

echo "Building static/css/app.css…"
"./$BIN" \
  -c tailwind.config.js \
  -i static/css/app.input.css \
  -o static/css/app.css \
  --minify

echo "Done — $(wc -c < static/css/app.css) bytes."
