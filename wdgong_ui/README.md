# WDGo // ctOS — new UI prototype (raylib)

Phase-1 renderer PoC: the faithful WDGo cockpit (HUD / MAP / TERMINAL / STATUS)
+ the real tab menu with the selected-tab character, in the new ctOS look.
Stubbed data — not wired to the ESP32 yet; this is about look + feel on-device.

## Run
    pip install raylib          # aarch64 wheel exists; falls back to build
    python3 main.py

## Keys
LEFT/RIGHT or TAB = switch tab · UP/DOWN = move item · G = ctOS/FIELD · ESC = quit

## Assets (assets/)
hacker.png (operator), ChakraPetch-SemiBold/Bold.ttf (display), JetBrainsMono-Regular/Bold.ttf (mono)
