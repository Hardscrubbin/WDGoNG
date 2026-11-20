"""The hub's list of OFFICIAL extras (written by the hub's maintainer).

official.json at the hub root:  {"author": "<name>", "extras": ["id", ...]}

Only the maintainer edits it (pull requests that touch it fail CI). The index
builder marks the listed extras "official": true in the signed catalogue, and the
check refuses any other extra that uses the maintainer's name as its author - so
"this one is from the maintainer" is something the game can show from signed data,
not from a string anyone may type into a manifest.
"""
import json
import os
import re


def norm(name) -> str:
    """Compare author names loosely: case, spaces and punctuation do not matter, and
    the usual digit look-alikes fold to letters (L0C0SP, LOC0SP ...)."""
    s = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return s.translate(str.maketrans("0135", "oies"))


def load(*candidates) -> dict:
    """First readable official.json among the candidates -> {"author", "extras": set}."""
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return {"author": str(d.get("author", "")),
                    "extras": {str(x) for x in d.get("extras", []) if isinstance(x, str)},
                    "path": p}
        except (OSError, ValueError, AttributeError):
            continue
    return {"author": "", "extras": set(), "path": None}


def is_official(off: dict, eid: str, author: str) -> bool:
    return bool(off["author"]) and eid in off["extras"] and norm(author) == norm(off["author"])


def reserved_name_misuse(off: dict, eid: str, author: str) -> bool:
    """True when an extra that is NOT on the official list signs as the maintainer."""
    return bool(off["author"]) and norm(author) == norm(off["author"]) and eid not in off["extras"]
