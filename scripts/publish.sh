#!/bin/sh
# Publish Hive Mind to PyPI and npm.
#
# Prerequisites:
#   PyPI:  python -m pip install twine  &&  set API token in ~/.pypirc or TWINE_PASSWORD
#   npm:   npm login
#
# Usage:
#   ./scripts/publish.sh        # dry-run (--dry-run)
#   ./scripts/publish.sh --real # actually publish

set -e
MODE="${1:---dry-run}"
REAL=false
[ "$MODE" = "--real" ] && REAL=true

echo "=== Hive Mind Publisher ==="
echo "Mode: $MODE"

# ── 1. Build Python package ──────────────────────────────────────────────────
echo ""
echo "--- Building Python package ---"
cd "$(dirname "$0")/.."
rm -rf dist/
python -m build
echo "  dist/ contents:"
ls -lh dist/

# ── 2. Publish to PyPI ───────────────────────────────────────────────────────
if $REAL; then
    echo ""
    echo "--- Publishing to PyPI ---"
    python -m twine upload dist/*
else
    echo ""
    echo "[dry-run] Would run: twine upload dist/*"
    echo "           To publish: twine upload dist/*"
fi

# ── 3. Publish to npm ────────────────────────────────────────────────────────
NPM_DIR="packaging/npm"
if $REAL; then
    echo ""
    echo "--- Publishing to npm ---"
    cd "$NPM_DIR"
    npm publish
else
    echo ""
    echo "[dry-run] Would run: cd $NPM_DIR && npm publish"
    echo "           To publish: cd $NPM_DIR && npm publish"
fi

echo ""
echo "=== Done ==="
