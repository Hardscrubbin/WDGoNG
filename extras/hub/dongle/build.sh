#!/bin/bash
# Build helper: ESP-IDF v5.5 with the already-installed Python 3.13 environment.
# usage: ./build.sh [idf.py args...]   (default: build)
export PATH="$HOME/.espressif/python_env/idf5.5_py3.13_env/bin:$PATH"
. "$HOME/esp/esp-idf/export.sh" >/dev/null 2>&1 || { echo "ESP-IDF export failed"; exit 1; }
cd "$(dirname "$0")" && idf.py "${@:-build}"
