#!/usr/bin/env python3
"""Hub ratings: banana (yes) / peel (no) votes, counted from GitHub reactions.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python3 hub_ratings.py <hub-root>

Every extra in index.json gets one GitHub issue titled "rating: <id>" carrying the
label `rating`. People vote on that issue: thumbs-up = banana, thumbs-down = peel.
One vote per GitHub account, changeable at any time. Only people with write access
can put a label on an issue, so a look-alike issue opened by someone else is never
counted.

This script (run by the hub workflow on publish and on a schedule) makes sure every
extra has its vote issue, reads the reaction totals and writes <hub-root>/ratings.json.
The output is deterministic, so the workflow only commits when a count changed.

ratings.json is DISPLAY data: not signed, not part of the install trust chain. The
game and the website treat it as untrusted input and re-validate every value.
Pure stdlib.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
LABEL = "rating"
PREFIX = "rating: "
BODY = """Vote for **{name}** (`{id}`) by reacting to this issue:

- :+1: = **banana** — yes, a good extra
- :-1: = **peel** — no, it has problems

One vote per GitHub account; you can change it any time. Comments are welcome for
bug reports and feedback. Counts show up on the store and in the game's Hub screen
within a few hours.

Official extras (author LOCOSP, marked ✓) come from the hub maintainer. All others are
made by their own authors, not by LOCOSP. Install and run only what you know and trust,
at your own risk.
"""


def api(method, path, token, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "wdgo-extras-hub")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return r.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def run(root, repo, call) -> dict:
    """call(method, path, body=None) -> (status, json). Returns {id: {banana, peel, url}}."""
    # Votes are for RELEASED extras (the signed index.json), not for candidates.
    # Before the first release there is no index.json: nothing to vote on yet.
    try:
        with open(os.path.join(root, "index.json"), "r", encoding="utf-8") as f:
            extras = (json.load(f) or {}).get("extras") or {}
    except FileNotFoundError:
        extras = {}
    st, _ = call("GET", "/repos/%s/labels/%s" % (repo, LABEL))
    if st == 404:
        call("POST", "/repos/%s/labels" % repo,
             {"name": LABEL, "color": "f5d90a", "description": "banana / peel votes for an extra"})
    issues, page = {}, 1
    while True:
        st, lst = call("GET", "/repos/%s/issues?labels=%s&state=all&per_page=100&page=%d"
                       % (repo, LABEL, page))
        if st != 200 or not lst:
            break
        for it in lst:
            if not isinstance(it, dict) or "pull_request" in it:
                continue
            title = str(it.get("title", ""))
            if not title.startswith(PREFIX):
                continue
            eid = title[len(PREFIX):].strip()
            # the oldest issue per id wins, so a duplicate can never split the vote
            if eid not in issues or it.get("number", 0) < issues[eid].get("number", 0):
                issues[eid] = it
        if len(lst) < 100:
            break
        page += 1
    for eid in sorted(extras):
        if eid in issues:
            continue
        e = extras[eid] if isinstance(extras[eid], dict) else {}
        st, it = call("POST", "/repos/%s/issues" % repo,
                      {"title": PREFIX + eid, "labels": [LABEL],
                       "body": BODY.format(name=e.get("name", eid), id=eid)})
        if st in (200, 201) and isinstance(it, dict):
            issues[eid] = it
    out = {}
    for eid in sorted(extras):
        it = issues.get(eid)
        if not it:
            continue
        r = it.get("reactions") or {}
        out[eid] = {"banana": int(r.get("+1", 0) or 0), "peel": int(r.get("-1", 0) or 0),
                    "url": str(it.get("html_url", ""))}
    return out


def write_ratings(root, ratings) -> str:
    path = os.path.join(root, "ratings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "extras": ratings}, f, indent=1, sort_keys=True, ensure_ascii=True)
        f.write("\n")
    return path


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    root = os.path.abspath(sys.argv[1])
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or "/" not in repo:
        print("need GITHUB_TOKEN and GITHUB_REPOSITORY")
        sys.exit(2)
    ratings = run(root, repo, lambda m, p, b=None: api(m, p, token, b))
    print("ratings for %d extra(s) -> %s" % (len(ratings), write_ratings(root, ratings)))


if __name__ == "__main__":
    main()
