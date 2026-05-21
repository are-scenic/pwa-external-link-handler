#!/usr/bin/env bash
# Builds the Chrome Web Store submission ZIP.
# Strips the 'key' field from manifest.json — the store does not accept it.
set -euo pipefail

VERSION=$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")
ZIPFILE="pwa-elh-v${VERSION}.zip"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Copy extension files
cp -r src options icons LICENSE "$TMPDIR/"
# Strip 'key' field from manifest
python3 -c "
import json, sys
m = json.load(open('manifest.json'))
m.pop('key', None)
json.dump(m, open('$TMPDIR/manifest.json', 'w'), indent=2)
"

(cd "$TMPDIR" && zip -r - .) > "$ZIPFILE"
echo "Built: $ZIPFILE ($(du -sh "$ZIPFILE" | cut -f1))"
