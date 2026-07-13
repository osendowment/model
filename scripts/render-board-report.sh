#!/usr/bin/env bash
# Render data/preview/board-preview-report.md → .pdf
#
# The report is a hand-written read of the preview table, not a pipeline output —
# both the .md and the .pdf are gitignored. Re-run this after editing the report.
#
#   scripts/render-board-report.sh
#
# Needs pandoc + weasyprint (brew install pandoc; uv tool install weasyprint).
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="data/preview/board-preview-report.md"
OUT="${SRC%.md}.pdf"
CSS="scripts/board-report.css"

for bin in pandoc weasyprint; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done
[[ -f "$SRC" ]] || { echo "missing: $SRC" >&2; exit 1; }

# weasyprint loads pango/glib through cffi's dlopen, which does not search
# Homebrew's prefix on macOS — without this it dies with "cannot load library
# libgobject-2.0-0" even though the dylibs are installed.
if [[ -d /opt/homebrew/lib ]]; then
  export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"
fi

pandoc "$SRC" \
  --from=gfm \
  --to=html5 \
  --standalone \
  --metadata title="OSE Board Preview Report" \
  --pdf-engine=weasyprint \
  --css="$CSS" \
  --output="$OUT"

# weasyprint writes page objects into compressed object streams, so grepping the
# raw bytes for /Type /Page finds nothing — count the form feeds pdftotext emits.
pages="?"
if command -v pdftotext >/dev/null; then
  pages=$(pdftotext "$OUT" - | tr -cd '\f' | wc -c | tr -d ' ')
fi

printf '%s — %s, %s pages\n' "$OUT" "$(du -h "$OUT" | cut -f1 | tr -d ' ')" "$pages"
