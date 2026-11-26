#!/usr/bin/env python3
"""Upload contract test against the LIVE portal, in its dry-run mode.

    sudo -E .venv/bin/python3 extras/sim/test_portal_contract.py      (on the device)

Uses the game's own uploader (portal_sync) and its API key, but with ?dry_run=1:
the portal checks key, signature, nonce and the data, then stops before
anti-cheat, idempotency and import - nothing reaches the game database, no XP.
Run it before every release: the September bug (job status answered with a login
page) would have failed here on day one.

Checks, for the sync and the async path (see the API contract in the docs
):
  - every answer is JSON, carries X-WDGWars-Upload-API and Cache-Control: no-store
  - sync 200 says dry_run, saved:false, and counts what it received / found invalid
  - async 202 carries job_id, status, poll_url, retry_after; the job goes
    processing -> done with a result, through the game's own polling code
  - a bad signature and a missing job are clear JSON errors
Each run spends a few nonces and counts towards the 120/min limit, like a real upload.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "wdgong_ui"))
sys.path.insert(0, REPO)
import portal_sync as ps                                  # noqa: E402

CONTRACT = "2026-11-19"          # this test was written against this contract; later
                                 # additive revisions are "2026-11-19.N" and must still pass
_passed = _failed = 0


def check(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   " + what)
    else:
        _failed += 1
        print("  FAIL " + what)


def call(p, req):
    """(status, headers, json-or-None, raw) for one request, errors included."""
    try:
        with p._open(req, timeout=30) as r:
            raw = r.read()
            return r.status, r.headers, _js(raw), raw
    except HTTPError as e:
        raw = e.read()
        return e.code, e.headers, _js(raw), raw


def _js(raw):
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
        return j if isinstance(j, dict) else None
    except ValueError:
        return None


def contract_headers(h, what):
    v = h.get("X-WDGWars-Upload-API") if h is not None else None
    check(bool(v) and (v == CONTRACT or v.startswith(CONTRACT + ".")),
          "%s: X-WDGWars-Upload-API is %s or a revision of it (got %r)" % (what, CONTRACT, v))
    check(h is not None and "no-store" in (h.get("Cache-Control") or ""), "%s: Cache-Control no-store" % what)


def upload_req(p, body, extra="", sign=True):
    url = p._api_url + ("&" if "?" in p._api_url else "?") + "dry_run=1" + extra
    payload = p._sign_payload(body)
    if not sign:
        d = json.loads(payload)
        d["sig"] = "0" * 64
        payload = json.dumps(d).encode()
    req = Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", p._api_key)
    req.add_header("X-Idempotency-Key", "contract-test-%d" % time.time())
    return req


def main():
    p = ps.PortalSync(version=__import__("watchdogs").__version__, loot_base=Path(REPO) / "loot",
                      state_path=Path("/nonexistent/contract-test-state.json"),
                      active_session=lambda: "", log=lambda m, l="": print("   console: " + m.strip()))
    if not p._api_key:
        print("no WARDRIVE_API_KEY in secrets.conf - cannot test")
        sys.exit(2)
    print("portal: %s   client: %s" % (p._api_url, p._ua))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    good = {"bssid": "02:00:5E:10:00:01", "ssid": "contract-test", "auth": "WPA2", "first_seen": now,
            "channel": 6, "rssi": -60, "lat": 50.0, "lon": 19.0, "alt": 200.0, "type": "WIFI"}
    bad = dict(good, bssid="not-a-mac", lat=999.0)
    body = {"networks": [good, bad], "aircraft": [{"icao": "ABCDEF", "callsign": "TEST1", "lat": 50.1, "lon": 19.1,
                                                     "alt_ft": 30000, "speed_kt": 400, "heading": 90,
                                                     "first_seen": str(int(time.time())), "type": "ADSB"}],
            "meshcore_nodes": []}

    print("\n[1] sync dry run")
    st, h, j, raw = call(p, upload_req(p, body))
    check(st == 200 and j is not None, "HTTP 200 with a JSON body (got %s %r)" % (st, raw[:80]))
    contract_headers(h, "sync")
    j = j or {}
    check(j.get("dry_run") is True and j.get("saved") is False, "dry_run:true, saved:false")
    rec, inv = j.get("received") or {}, j.get("invalid") or {}
    check(rec.get("networks") == 2 and rec.get("aircraft") == 1,
          "received counts the batch (networks 2, aircraft 1): %s" % rec)
    check(sum(v for v in inv.values() if isinstance(v, int)) >= 1, "invalid flags the broken row: %s" % inv)
    check(bool(j.get("message")), "a human message: %r" % j.get("message"))

    print("\n[2] async dry run through the game's own polling")
    st, h, j, raw = call(p, upload_req(p, body, "&async=1"))
    check(st == 202 and j is not None, "HTTP 202 with a JSON body (got %s %r)" % (st, raw[:80]))
    contract_headers(h, "async")
    j = j or {}
    for k in ("job_id", "status", "poll_url", "retry_after"):
        check(k in j, "202 carries %s (%r)" % (k, j.get(k)))
    check(j.get("dry_run") is True, "202 says dry_run")
    if j.get("job_id"):
        st2, h2, j2, _ = call(p, _job_req(p, j["job_id"]))
        check(j2 is not None and j2.get("status") in ("queued", "processing", "done"),
              "job status is JSON with a known status (%s)" % (j2 or {}).get("status"))
        contract_headers(h2, "job status")
        t0 = time.time()
        try:
            res = p._poll_job(j["job_id"], first_wait=float(j.get("retry_after") or 2))
            check(isinstance(res, dict), "the game's poller reaches done in %.0f s" % (time.time() - t0))
        except Exception as exc:
            check(False, "the game's poller reaches done (%s: %s)" % (type(exc).__name__, exc))

    print("\n[3] errors are JSON and plain")
    st, h, j, raw = call(p, upload_req(p, body, sign=False))
    if j is not None and "message" in j:
        print("     portal says: %r" % j.get("message"))
    check(st in (400, 401, 403) and j is not None and (j.get("error") or j.get("message")),
          "bad signature -> %s JSON %r" % (st, (j or {}).get("error") or raw[:60]))
    st, h, j, raw = call(p, _job_req(p, 1))
    check(st == 404 and j is not None and j.get("error"), "unknown job -> 404 JSON %r" % (j or {}).get("error"))
    req = Request(p._job_url(1), method="GET")
    st, h, j, raw = call(p, req)
    check(st == 401 and j is not None, "no API key -> 401 JSON (got %s, %r)" % (st, raw[:60]))

    print("\n%d passed, %d failed" % (_passed, _failed))
    sys.exit(1 if _failed else 0)


def _job_req(p, job_id):
    req = Request(p._job_url(job_id), method="GET")
    req.add_header("X-API-Key", p._api_key)
    return req


if __name__ == "__main__":
    main()
