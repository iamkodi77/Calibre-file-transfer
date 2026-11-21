#!/bin/sh
# Start Leafline from any working directory, installing dependencies as needed.
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    if [ -e "$SCRIPT_DIR/.venv" ]; then
        echo "Error: .venv exists but does not contain a usable Unix Python." >&2
        echo "Remove or rename .venv, then run this script again." >&2
        exit 1
    fi

    PYTHON=""
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
        if command -v "$candidate" >/dev/null 2>&1 && \
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            PYTHON=$candidate
            break
        fi
    done

    if [ -z "$PYTHON" ]; then
        echo "Error: Python 3.10 or newer is required. Install Python and try again." >&2
        exit 1
    fi

    echo "Creating the Leafline virtual environment..."
    if ! "$PYTHON" -m venv "$SCRIPT_DIR/.venv"; then
        echo "Error: could not create .venv. Make sure Python's venv module is installed" >&2
        echo "(on Debian/Ubuntu, install the python3-venv package)." >&2
        echo "If a partial .venv was created, remove or rename it before retrying." >&2
        exit 1
    fi
fi

if ! "$VENV_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    echo "Error: .venv needs a working Python 3.10 or newer." >&2
    echo "Remove or rename .venv, then run this script again." >&2
    exit 1
fi

echo "Installing/checking Leafline requirements..."
if ! "$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"; then
    echo "Error: requirements could not be installed. Check your internet connection" >&2
    echo "and Python/pip installation, then try again." >&2
    exit 1
fi

echo "Starting Leafline. Press Ctrl+C to stop."
exec "$VENV_PYTHON" "$SCRIPT_DIR/app.py" "$@"
