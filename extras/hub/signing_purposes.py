"""What the signing dongle can sign, as the dashboard sees it.

Each PURPOSE is one kind of signature with its own card and its own "publish"
button. Today there is one: the extras hub catalogue. A new kind (a firmware
image, a map pack ...) is a new class here with the same four members, plus the
matching namespace in the dongle's firmware - the dongle refuses namespaces it
does not know, so nothing here can widen what the key will sign.

    id         short stable name
    title      what the card says
    check()    -> {"ready": bool, "summary": [(mark, text)], "fp": str, "warn": str}
    publish(dongle, log) -> str (a short result for the person); raises on failure
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "dongle", ".dashboard.json")       # gitignored, per machine


def _config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class HubCatalogue:
    """The WatchDogsGo NG extras hub: sign pending/index.json and publish it."""
    id = "hub"
    title = "Hub dodatków (katalog)"
    namespace = "wdgo-extras-hub"

    def __init__(self):
        clone = _config().get("hub_clone") or "~/python/WDGoNG-HUB"
        self.root = os.path.abspath(os.path.expanduser(clone))
        self._prep = None

    def check(self) -> dict:
        import hub_release as hr
        self._prep = None
        try:
            prep = hr.prepare(self.root, pull=True)
        except hr.ReleaseError as exc:
            return {"ready": False, "summary": [], "fp": "", "warn": str(exc)}
        self._prep = prep
        summary = [(m, "%s  %s" % (eid, text)) for m, eid, text, _files in prep["changes"]]
        warn = ("Są aktualizacje z NOWYMI UPRAWNIENIAMI - gracze dostaną ponownie kartę zgody."
                if prep["new_perms"] else "")
        return {"ready": prep["ready"], "summary": summary, "fp": prep["fp"], "warn": warn,
                "where": self.root}

    def publish(self, dongle, log) -> str:
        import hub_release as hr
        prep = self._prep
        if prep is None or not prep["ready"]:
            raise hr.ReleaseError("najpierw sprawdź, co czeka na podpis")
        commit = hr.publish(self.root, prep, dongle=dongle, log=log)
        self._prep = None
        return "opublikowano, commit %s" % commit


PURPOSES = [HubCatalogue()]


def by_id(pid):
    return next((p for p in PURPOSES if p.id == pid), None)
