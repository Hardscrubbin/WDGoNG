"""SubGHz capture export — turn a raw I/Q recording (.cu8) into a Flipper Zero
RAW .sub file (and parse rtl_433's capture filenames).

Pure-Python (array module only) so it runs anywhere the game runs — no numpy
dependency. Files from rtl_433 -S are short (one detected packet), so a plain
Python envelope demod is fast enough.

Pipeline: .cu8 (complex uint8 I/Q) -> magnitude envelope -> hysteresis threshold
-> run-length of on/off -> signed microsecond durations -> Flipper RAW .sub.

The same converter works on .cu8 from rtl_433 OR (later) a CC1101 grab dumped as
I/Q — it only needs the sample rate and the centre frequency.
"""

import array
import math
import os
import re

FLIPPER_PRESET_OOK = "FuriHalSubGhzPresetOok650Async"
_FNAME_RE = re.compile(r"(\d+(?:\.\d+)?)M.*?(\d+)k", re.I)


def parse_rtl433_filename(name: str):
    """rtl_433 -S names files like 'g001_433.92M_250k.cu8' -> (freq_hz, sample_rate).
    Returns (None, None) if it doesn't match (caller then supplies defaults)."""
    base = os.path.basename(name)
    m = _FNAME_RE.search(base)
    if not m:
        return (None, None)
    try:
        return (int(float(m.group(1)) * 1e6), int(m.group(2)) * 1000)
    except ValueError:
        return (None, None)


def _read_cu8_magnitude(path: str):
    """Read a complex-uint8 (.cu8) file -> list of magnitudes (float)."""
    with open(path, "rb") as f:
        raw = f.read()
    n = len(raw) & ~1          # whole I/Q pairs only
    a = array.array("B")
    a.frombytes(raw[:n])
    mags = [0.0] * (n // 2)
    for k in range(0, n, 2):
        i = a[k] - 127.5
        q = a[k + 1] - 127.5
        mags[k >> 1] = math.hypot(i, q)
    return mags


def _threshold(mags):
    """Hysteresis thresholds from the magnitude distribution (floor + fraction)."""
    if not mags:
        return (1.0, 1.0)
    s = sorted(mags)
    n = len(s)
    floor = s[int(n * 0.20)]
    peak = s[int(n * 0.95)] if n > 1 else s[-1]
    span = max(1.0, peak - floor)
    hi = floor + 0.45 * span
    lo = floor + 0.30 * span
    return (hi, lo)


def cu8_to_pulses(path: str, sample_rate: int, min_us: float = 30.0):
    """Envelope-demod a .cu8 into Flipper RAW durations (signed int microseconds:
    + = carrier on, - = gap). Returns [] if nothing usable."""
    mags = _read_cu8_magnitude(path)
    if not mags:
        return []
    hi, lo = _threshold(mags)
    us_per_sample = 1_000_000.0 / float(sample_rate)
    # hysteresis state machine -> run lengths (samples) with their level
    runs = []
    level = False
    run = 0
    for m in mags:
        nl = level
        if level and m < lo:
            nl = False
        elif (not level) and m > hi:
            nl = True
        if nl == level:
            run += 1
        else:
            runs.append((level, run))
            level = nl
            run = 1
    runs.append((level, run))
    # to microseconds
    durs = [(lv, rl * us_per_sample) for lv, rl in runs]
    # coalesce sub-min_us glitches into the previous run (same sign flip noise)
    merged = []
    for lv, us in durs:
        if us < min_us and merged:
            # absorb glitch: extend previous run by this (ignore the flip)
            plv, pus = merged[-1]
            merged[-1] = (plv, pus + us)
        else:
            merged.append((lv, us))
    # drop a leading gap so the train starts with a mark (Flipper wants +first)
    while merged and not merged[0][0]:
        merged.pop(0)
    while merged and not merged[-1][0]:
        merged.pop()           # drop trailing gap too
    if not merged:
        return []
    pulses = []
    for lv, us in merged:
        d = int(round(us))
        if d <= 0:
            continue
        pulses.append(d if lv else -d)
    return pulses


def write_flipper_sub(pulses, freq_hz: int, out_path: str,
                      preset: str = FLIPPER_PRESET_OOK, per_line: int = 512):
    """Write a Flipper Zero RAW .sub from signed-us pulses. Returns out_path."""
    lines = [
        "Filetype: Flipper SubGhz RAW File",
        "Version: 1",
        "Frequency: %d" % int(freq_hz),
        "Preset: %s" % preset,
        "Protocol: RAW",
    ]
    for i in range(0, len(pulses), per_line):
        chunk = pulses[i:i + per_line]
        lines.append("RAW_Data: " + " ".join(str(p) for p in chunk))
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


def convert_cu8(path: str, freq_hz: int = None, sample_rate: int = None,
                out_path: str = None, min_us: float = 30.0):
    """Full convert: .cu8 -> Flipper RAW .sub. Freq/rate are read from the rtl_433
    filename when not given. Returns (out_path, n_pulses) or (None, 0)."""
    fhz, srate = parse_rtl433_filename(path)
    freq_hz = freq_hz or fhz or 433_920_000
    sample_rate = sample_rate or srate or 250_000
    pulses = cu8_to_pulses(path, sample_rate, min_us=min_us)
    if len(pulses) < 2:
        return (None, 0)
    if out_path is None:
        out_path = os.path.splitext(path)[0] + ".sub"
    write_flipper_sub(pulses, freq_hz, out_path)
    return (out_path, len(pulses))
