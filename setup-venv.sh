#!/usr/bin/env bash
# Rebuild the project virtual environment (.venv) on Linux / macOS.
# Prefers uv when available (uses uv.lock for reproducible installs); otherwise falls back to
# system python >= 3.11 with venv + pip. The existing .venv is moved aside as a backup first,
# unless --force is given.
set -euo pipefail
cd "$(dirname "$0")"

say() { echo "==> $*" >&2; }

if [ -d .venv ]; then
    if [ "${1:-}" = "--force" ]; then
        rm -rf .venv
        say "removed existing .venv (--force)"
    else
        bak=".venv.bak-$(date +%Y%m%d-%H%M%S)"
        mv .venv "$bak"
        say "moved existing .venv to $bak"
    fi
fi

if command -v uv >/dev/null 2>&1; then
    say "using uv: $(command -v uv)"
    uv sync --extra dev
else
    py="$(command -v python3 || command -v python || true)"
    if [ -z "$py" ]; then
        say "'python'/'python3' not found in PATH; install Python >= 3.11 (or uv)" >&2
        exit 1
    fi
    ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    ok="$("$py" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
    say "using python $ver at $py"
    if [ "$ok" != "1" ]; then
        say "python $ver is too old; need >= 3.11" >&2
        exit 1
    fi
    say "creating .venv and installing package + dev extras (pip)"
    "$py" -m venv .venv
    ./.venv/bin/python -m pip install --quiet -e ".[dev]"
fi

if [ ! -x .venv/bin/python ]; then
    say "expected .venv/bin/python not found after setup" >&2
    exit 1
fi
say "done: .venv is ready"
