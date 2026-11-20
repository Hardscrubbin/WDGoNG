# EXTRAS hub — maintainer guide (Phase 3)

The hub is how other people's extras reach players **without ever being trusted**:

    author PR -> CI gate (hub_check.py) -> maintainer review -> merge
              -> CI rebuilds the CANDIDATE (pending/index.json) - CI never signs
              -> maintainer signs offline (hub_release.py + signing dongle) -> index.json + .sig
    game:  fetch hub -> verify signature -> verify per-extra checksum -> install
           -> consent card -> run in the Phase-2 sandbox

Each layer stands on its own. Review and CI keep junk out of the catalogue; the
signature and checksums make sure a device installs exactly what was reviewed; the
sandbox means even a malicious extra that slipped through cannot read keys, touch
radios, reach the network or the filesystem.

## Pieces (all in this repo)

| File | Role |
|---|---|
| `wdgong_ui/wdgo_extras_hub.py` | game client: `sync` (git, as the login user), `verify_index` (`ssh-keygen -Y verify`), `install`/`uninstall`, the in-game **Hub** screen |
| `wdgong_ui/extras_hub_signers` | the hub's PUBLIC key (OpenSSH `allowed_signers` line). Not present = hub disabled |
| `extras/hub/hub_check.py` | CI gate for one extra (manifest, files, import allowlist, forbidden calls, permissions, sandbox smoke run) |
| `extras/hub/hub_build_index.py` | deterministic catalogue with each extra's content hash (`--out pending/index.json` in CI) |
| `extras/hub/hub_release.py` | maintainer: rebuild locally, compare with the candidate, show the changes, sign, publish |
| `extras/hub/dongle/`, `hub_dongle.py`, `hub_dongle_web.py` | the signing dongle: firmware, CLI, local key-manager dashboard |
| `extras/hub/make_hub_repo.sh` | assembles the hub repository skeleton (template + tools + SDK copy) |
| `extras/sim/test_hub.py` | end-to-end tests incl. forged index / wrong key / changed code |
| `extras/sim/test_hub_release.py` | offline-release tests incl. edited candidate / unknown key / stale candidate |

## One-time setup

1. **Signing key** lives in the signing dongle (see *Signing dongle* below) - never in
   git, never in chat, never on a CI runner. There is no `HUB_SIGNING_KEY` secret.
2. **Public half into the game**: one line per key in `wdgong_ui/extras_hub_signers`
   (`hub_dongle.py pubkey` prints it):
   ```
   wdgo-extras-hub ecdsa-sha2-nistp256 AAAA...
   ```
   Commit it. Ship TWO lines from day one - the main key and a spare - so losing one
   key does not need an urgent game update. Rotating = add the new line, ship a game
   update, then retire the old one.
3. **Hub repository**: `extras/hub/make_hub_repo.sh /path/to/hub`, `git init`, push.
   Protect `main` (PR + review required). The maintainer's release push
   (`index.json` + `index.json.sig`) must be allowed for the maintainer only.
4. **Public storefront**: `index.html` (copied by the script) renders the catalogue on
   GitHub Pages - enable *Settings > Pages > Deploy from a branch > main / root*.
   It only displays the signed index; installs never go through it.
5. **Point the game at it**: set `DEFAULT_HUB_URL` in `wdgo_extras_hub.py`, or per
   device `~/.wdgo/extras-hub.json` -> `{"url": "<git url>"}`. A private repository
   works: the fetch runs as the login user with their git credentials.

## Releasing (after a merge)

CI commits `pending/index.json` (and the portal DMs the maintainer via BEACON).
Players keep the last signed release until it is signed. Easiest: open the key
manager (desktop launcher "LSP KEY"), section **Do podpisu**: one card per kind of
signature, each with its own "Podpisz i opublikuj" button, plus "Podpisz i opublikuj
wszystkie" when more than one kind is waiting. The same from a terminal:

    extras/hub/hub_release.py /path/to/hub-clone

New kinds of signature are added in `extras/hub/signing_purposes.py` (one class per
kind) together with the matching namespace in the dongle firmware.

The terminal tool pulls, rebuilds the catalogue from the checked-out `extras/` and refuses unless
the candidate is byte-identical; prints new / updated / NEW PERMISSION / removed and
the changed files; asks for the dongle (PIN on the device); checks the signature
against `extras_hub_signers`; commits and pushes `index.json` + `index.json.sig`.
Close the key-manager dashboard first (one program per serial port). If the dongle
answers that the digest does not match right after a push, the file server has not
refreshed yet - wait a few minutes.

Emergency without the dongle: `--key <encrypted backup file>` signs with the backup.

## Signing dongle

A LilyGO T-Dongle-C5 running `extras/hub/dongle/` (ESP-IDF 5.5, `./build.sh --preview
-p <port> flash`). It builds the SSHSIG blob itself with the fixed namespace, so the
key signs hub catalogues and nothing else. It fetches `index.json` and
`pending/index.json` over Wi-Fi on its own and signs only the digest of the candidate
it downloaded. Optional PIN typed on the device (short press +1, long press accept);
with a PIN the key is stored wrapped under it. One-time encrypted export for a
backup, `import` to restore. `hub_dongle_web.py` is a dashboard on 127.0.0.1 for
name, Wi-Fi, hub address, PIN, backup. Current firmware is a DEV build (software key
in flash); an eFuse-backed build with secure boot is a later, irreversible step.

## Ratings (banana = yes, peel = no)

`tools/hub_ratings.py` (run by the publish job and every 6 h) opens one issue per
extra titled `rating: <id>` with the label `rating`, and counts its reactions:
thumbs-up = banana, thumbs-down = peel, one vote per GitHub account. Only people with
write access can label an issue, so look-alike issues are never counted; the oldest
labelled issue per id wins. The result is `ratings.json`, shown on the website and
in the game's Hub screen.

`ratings.json` is display data: unsigned, outside the install trust chain, and
re-validated by the game and the page. PRs that touch it (or `index.json` /
`index.json.sig` / `pending/`) fail CI. The format is source-agnostic (`{id: {banana, peel, url}}`),
so the producer can later move to the WDGWars portal - e.g. in-game voting with the
player's account - without changing the game or the page.

## Reviewing a PR

CI output is the starting point, not the verdict. Read the code for: what it does
with each permission, anything flagged `WARN`, and whether `description`/`about`
match the behaviour. The scan is deliberately strict (no `os`, no `open`, no dunder
tricks, no `ctx._…`); an author who needs an exception probably needs a new `ctx`
capability instead — discuss that, do not wave the check through.

## What the game guarantees

- No valid signature from a key in `extras_hub_signers` -> the catalogue is ignored.
- Files that do not hash to the signed `sha` -> not installed; an existing install is untouched.
- A hub extra is **not** "trusted": it always runs sandboxed and shows the consent
  card (once per code + permission set). Editing an installed copy turns it into
  "UNVERIFIED LOCAL CODE" and asks again.
- The hub listing links the **reviewed source** of each extra (its folder in this
  repo, derived from the hub URL) plus the author's optional `homepage`, so a player
  can read exactly the code that will run and cross-check the shown checksum.
- Uninstall removes the code, keeps the extra's saved data.
