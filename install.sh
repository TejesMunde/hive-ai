#!/bin/sh
# Hive Mind installer (POSIX sh).
#
#   curl -fsSL https://raw.githubusercontent.com/TejesMunde/hive-ai/main/install.sh | sh
#
# Phase A distribution: installs the Python package (requires Python >= 3.10).
# Prefers pipx (isolated), falls back to `pip install --user`.
set -e

PKG="hive-ai"
MIN_MAJOR=3
MIN_MINOR=10

err() { printf '%s\n' "$@" >&2; }

# --- locate a Python >= 3.10 -------------------------------------------------
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= ('"$MIN_MAJOR"','"$MIN_MINOR"') else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    err "[hive] Python ${MIN_MAJOR}.${MIN_MINOR}+ is required but was not found." \
        "       Install it from https://www.python.org/downloads/ and re-run."
    exit 1
fi

printf '[hive] Using %s (%s)\n' "$PY" "$($PY --version 2>&1)"

# --- install ----------------------------------------------------------------
if command -v pipx >/dev/null 2>&1; then
    printf '[hive] Installing %s with pipx...\n' "$PKG"
    pipx install "$PKG"
else
    printf '[hive] pipx not found; installing %s with pip --user...\n' "$PKG"
    "$PY" -m pip install --user --upgrade "$PKG"
    err "" \
        "[hive] Note: if 'hive' is not found, add your user scripts dir to PATH:" \
        "       $($PY -c 'import site,os;print(os.path.join(site.getuserbase(),\"bin\"))' 2>/dev/null || echo '~/.local/bin')"
fi

printf '\n[hive] Installed. Try:  hive --help\n'
