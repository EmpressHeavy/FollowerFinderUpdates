#!/usr/bin/env bash
# Build follower-finder.tgz next to this package folder plus a .sha256 that names only the file.
# Verify after download (in the folder holding both files):  sha256sum -c follower-finder.tgz.sha256
set -euo pipefail
PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="$(dirname "$PKG_DIR")"; NAME="$(basename "$PKG_DIR")"
OUT="${1:-$PARENT/follower-finder.tgz}"
OUT_DIR="$(cd "$(dirname "$OUT")" && pwd)"; FILE="$(basename "$OUT")"
tar czf "$OUT_DIR/$FILE" --exclude=__pycache__ --exclude=.pytest_cache --exclude="$NAME/data" -C "$PARENT" "$NAME"
( cd "$OUT_DIR" && sha256sum "$FILE" > "$FILE.sha256" && sha256sum -c "$FILE.sha256" )
