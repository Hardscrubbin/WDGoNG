#!/bin/sh
# Assemble a ready-to-push, PUBLIC hub repository skeleton.
#   extras/hub/make_hub_repo.sh <target-dir>
# Copies the storefront + template (README, PR template, CI workflow), the author
# guide, reference examples, the tools and the SDK they need. Re-run after a game
# release to refresh docs/, examples/ and tools/ in the hub.
# Ends with a leak guard: the hub is public, so nothing in it may name the private
# game build. The script FAILS instead of producing a skeleton that would.
set -eu
[ $# -eq 1 ] || { echo "usage: $0 <target-dir>"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SDK=$(dirname "$(find "$REPO" -maxdepth 2 -name wdgo_extras.py -not -path '*/.*' | head -1)")
T=$1
mkdir -p "$T/extras" "$T/examples" "$T/docs" "$T/tools/sdk" "$T/.github/workflows"
cp "$HERE/template/README.md" "$T/README.md"
cp "$HERE/template/index.html" "$T/index.html"          # public storefront (GitHub Pages)
: > "$T/.nojekyll"                                       # serve files as-is
printf '__pycache__/\n' > "$T/.gitignore"
cp "$HERE/template/PULL_REQUEST_TEMPLATE.md" "$T/.github/PULL_REQUEST_TEMPLATE.md"
cp "$HERE/template/.github/workflows/hub.yml" "$T/.github/workflows/hub.yml"
cp "$HERE/hub_check.py" "$HERE/hub_build_index.py" "$HERE/hub_ratings.py" "$HERE/hub_official.py" "$REPO/extras/sim/run_in_sim.py" "$T/tools/"
[ -f "$T/official.json" ] || cp "$HERE/template/official.json" "$T/official.json"   # the hub's own copy wins
for f in wdgo_extras.py wdgo_extras_png.py wdgo_extras_ipc.py wdgo_extras_sandbox.py wdgo_extras_child.py; do
  cp "$SDK/$f" "$T/tools/sdk/$f"
done
for ex in "$REPO"/extras/examples/*/; do
  cp -R "${ex%/}" "$T/examples/"     # no trailing slash: BSD cp would copy the contents only
done
find "$T/examples" -name __pycache__ -type d -prune -exec rm -rf {} +
# author guide, with paths rewritten from the game repo layout to the hub layout
sed -e 's#extras/sim/run_in_sim.py#tools/run_in_sim.py#g' \
    -e 's#extras/hub/hub_check.py#tools/hub_check.py#g' \
    -e 's#run_in_sim.py extras/examples/#run_in_sim.py examples/#g' \
    -e 's#See `extras/examples/gps_speedo/`#See `examples/gps_speedo/`#g' \
    -e 's#`extras/examples/hello_wardriver/`#`examples/hello_wardriver/`#g' \
    -e '/Maintainer side: `docs\/EXTRAS_hub.md`/d' \
    "$REPO/docs/EXTRAS_authoring.md" > "$T/docs/AUTHORING.md"
[ -f "$T/extras/.gitkeep" ] || : > "$T/extras/.gitkeep"
# ---- leak guard ----------------------------------------------------------------
# The game's public name ("WatchDogsGo NG") and the hub's own name (WDGoNG-HUB) are
# allowed; what stays private - the game's repository and its package paths - is not.
if grep -rnI --exclude-dir=.git . "$T" | sed -e 's/WDGoNG-HUB//g' -e 's/WatchDogsGo NG//g' | grep -iE 'watchdogsgo-ng|wdgong|(^|[^A-Za-z])NG([^A-Za-z]|$)' ; then
  echo "LEAK GUARD: the lines above name the private build - fix them before publishing." >&2
  exit 3
fi
echo "hub skeleton ready in $T (leak guard: clean)"
