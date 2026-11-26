#!/usr/bin/env bash
# Pre-commit secret guard for the PUBLIC WatchDogsGo NG repo.
# Blocks a commit that would introduce a secret. This script itself is public,
# so it carries NO literal secret: device-specific values (a fleet-key
# fingerprint, a prox secret, an account path) live in
# scripts/.secret-patterns.local (gitignored), one extended-regex per line.
#
# Install as the repo hook:  ln -sf ../../scripts/secret-guard.sh .git/hooks/pre-commit
# Run manually:              bash scripts/secret-guard.sh
set -euo pipefail

staged=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true)
[ -z "$staged" ] && exit 0
fail=0

# 1) Secret FILES that must never be tracked.
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        secrets.conf|*/secrets.conf|*.key|fleet.key|*/fleet.key|prox.txt|*/prox.txt|\
        .wdgo/*|*/.wdgo/*|*.secret-patterns.local)
            echo "BLOCKED file: $f  (secret file — keep it gitignored)"; fail=1 ;;
    esac
done <<< "$staged"

# 2) Secret CONTENT in the staged additions only. Strip obvious placeholders so
#    templates and docs stay committable.
added=$(git diff --cached --diff-filter=ACMR -U0 2>/dev/null | grep '^+' | grep -v '^+++' || true)
real=$(echo "$added" | grep -viE 'your-[a-z0-9-]*(key|token|name|pass)|placeholder|<[a-z0-9_-]+>|xxxx|0{16,}|-here$' || true)

check() { if echo "$real" | grep -qE "$1"; then echo "BLOCKED content: $2"; fail=1; fi; }
check 'BEGIN ([A-Z0-9]+ )?PRIVATE KEY'              "private key material"
check 'WDGFLEET1:[0-9a-fA-F]{32}'                    "fleet key export line"
check '(WDG_WPASEC_KEY|WARDRIVE_API_KEY|WIGLE_[A-Z_]+|WDG_[A-Z_]*KEY)=[0-9a-fA-F]{16,}' "real API key value"
check '/(Users|home)/[a-z0-9_.-]+/\.wdgo' "private home secret dir"

# 3) Device-specific literal patterns (gitignored, never committed).
root=$(git rev-parse --show-toplevel 2>/dev/null || echo .)
pat="$root/scripts/.secret-patterns.local"
if [ -f "$pat" ]; then
    while IFS= read -r rx; do
        [ -z "$rx" ] && continue
        case "$rx" in \#*) continue ;; esac
        if echo "$real" | grep -qE "$rx"; then echo "BLOCKED content: matches a local secret pattern"; fail=1; fi
    done < "$pat"
fi

if [ "$fail" = 1 ]; then
    echo ""
    echo "Commit blocked by scripts/secret-guard.sh — a secret would reach the PUBLIC repo."
    echo "Fix the value, use a placeholder, or gitignore the file."
    echo "Override only if you are certain it is NOT a secret:  git commit --no-verify"
    exit 1
fi
exit 0
