# WatchDogsGo NG extras hub

Reviewed mini-apps ("extras") for **WatchDogsGo NG**. Everything here was submitted by pull
request, passed the automated checks, was read by a maintainer, and is listed in a
**signed** catalogue. The game installs an extra only if the catalogue's signature
is valid and the files match the checksum in it — and then still runs it sandboxed.

**Browse the store:** <https://locosp.github.io/WDGoNG-HUB/>

## Submit an extra

1. Write it against the author guide ([`docs/AUTHORING.md`](docs/AUTHORING.md)), start from
   [`examples/`](examples/), and test it: `python3 tools/run_in_sim.py <your-extra> 30 --sandbox`
   and `python3 tools/hub_check.py <your-extra>` (the same check CI runs).
2. Fork this repo, add your folder as `extras/<id>/` (`extra.json` + `main.py` ...).
3. Open a pull request. CI runs `hub_check.py` on it:
   manifest, file types, import allowlist, forbidden calls, declared permissions,
   and a smoke run inside the real sandbox. Fix whatever it reports.
4. A maintainer reviews the code and merges. The catalogue is then signed by the
   maintainer (offline, never by CI); once that release is published, players see
   your extra in **EXTRAS > Hub**.

Updating = a new PR that changes your folder and bumps `version` in `extra.json`.

## Rules

- stdlib (sandbox allowlist) + `ctx` only. No files, network, processes, threads,
  native code or pip packages — they do not exist inside the sandbox anyway.
- Ask only for the permissions you use. `description` and `about` must say what the
  extra does, in plain words.
- One folder per extra, id == folder name, source/docs/PNG files only.
- Sign with your own name. The author name `LOCOSP` is reserved for the hub's own
  extras (`official.json`); those are the ones shown in green in the game and the store.
- Images are optional and small: `icon.png` (store icon) up to 64x64 px / 16 KB; other
  PNGs your extra draws up to 512x512 px / 256 KB each, 8 files and 512 KB in total.

## Ratings

Every extra has a vote issue titled `rating: <id>`. React with 👍 for a **banana**
(yes, good extra) or 👎 for a **peel** (no, it has problems) — one vote per GitHub
account, change it any time, comments welcome. The counts appear on the website and
in the game's Hub screen within a few hours (`ratings.json`, refreshed by CI).
Votes are not a security review: an extra with many bananas still runs sandboxed.

## Website

The store at <https://locosp.github.io/WDGoNG-HUB/> (`index.html` in this repo) reads the
signed `index.json` and lists every extra with its ratings and a link to its reviewed
source here. It is a shop window only: the game does not use the website - it installs
via git, the signature and the checksums.

## Layout

    extras/<id>/        reviewed source:  extra.json, main.py, optional icon.png + other small PNGs
    index.html          public storefront (GitHub Pages)
    ratings.json        banana / peel counts (generated - do not edit)
    official.json       the maintainer's own extras (maintainer only)
    index.json          catalogue (generated - do not edit by hand)
    index.json.sig      OpenSSH signature of index.json (maintainer, offline)
    pending/index.json  candidate built by CI after a merge, waiting to be signed
    docs/AUTHORING.md   how to write an extra
    examples/           reference extras to start from (not listed in the store)
    tools/              run_in_sim.py, hub_check.py, hub_build_index.py + the SDK
