"""Turn a short string into a QR module matrix for on-screen rendering.

Uses segno (pure-python, no C deps) when it is installed, and returns None when
it is not — so a caller can fall back to showing the text line only and never
crashes for a missing optional dependency. The matrix is a list of rows, each a
list of bool (True = dark module); the caller draws each module as a filled cell.
"""


def matrix(text, error="l"):
    """QR module matrix (list[list[bool]]) for `text`, or None if segno is absent
    or the text cannot be encoded. `error` is the EC level: 'l' keeps the symbol
    smallest (easiest to fit on a tiny screen for a close-up phone scan)."""
    try:
        import segno
    except Exception:
        return None
    try:
        qr = segno.make(text, error=error, micro=False)
        return [[bool(c) for c in row] for row in qr.matrix]
    except Exception:
        return None
