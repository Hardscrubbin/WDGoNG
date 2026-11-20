#!/bin/bash
# LSP KEY manager - double-click in Finder (or run in a terminal).
# Starts the local dashboard and opens it in the browser. Close this window
# (or press Ctrl-C) to stop it - it must be stopped before flashing firmware
# or running hub_release.py, because only one program can use the USB port.
cd "$(dirname "$0")" || exit 1
for PY in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 python3; do
  if "$PY" -c "import serial, cryptography" 2>/dev/null; then
    exec "$PY" hub_dongle_web.py
  fi
done
echo "Brak Pythona z modulami pyserial i cryptography."
echo "Zainstaluj:  python3 -m pip install pyserial cryptography"
read -r -p "Enter, aby zamknac..." _
