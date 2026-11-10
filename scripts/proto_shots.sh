#!/usr/bin/env bash
# Prototype reference shots: screenshots of `design/Triage Prototype.dc.html` for side-by-side comparison with the
# app matrix in scripts/screenshots.sh (same viewport, same states: inbox, AGE oldest-first, dark theme).
#
#   scripts/proto_shots.sh [OUT_DIR]      # default OUT_DIR: ./screenshots/proto
#
# The prototype is a static .dc.html deck (React runtime in design/support.js); we just serve design/ over HTTP.
# Never touches the app server, the real GitHub API, or `gh`.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-screenshots/proto}")
mkdir -p "$OUT"
PORT=$((9300 + RANDOM % 200))
S=proto$$
cli() { timeout 120 pnpx @playwright/cli -s=$S "$@" > /dev/null 2>&1 || echo "  (cli $1 failed)"; }
ev() { # run JS in the page and echo just the returned value (a click helper reports what it hit)
  local out
  out=$(timeout 120 pnpx @playwright/cli -s=$S eval "$1" 2>/dev/null) \
    && echo "  eval: $(printf '%s\n' "$out" | sed -n '/### Result/{n;p}')" || echo "  (eval failed)"
}
clickText() { # click the first button whose text is $1 — nav items append a count badge, so allow a prefix match
  ev "() => { const bs = [...document.querySelectorAll('button')]; \
const b = bs.find(x => x.textContent.trim() === '$1') || bs.find(x => x.textContent.trim().startsWith('$1')); \
if (!b) return 'MISSING: $1'; b.click(); return 'clicked ' + b.textContent.trim().slice(0, 24); }"
}

python3 -m http.server "$PORT" --bind 127.0.0.1 --directory design > "/tmp/proto-http-$$.log" 2>&1 &
HTTP=$!
trap 'cli close; kill $HTTP 2>/dev/null; rm -f /tmp/proto-http-$$.log' EXIT
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/support.js" > /dev/null && break; sleep 0.2; done

URL="http://127.0.0.1:$PORT/Triage%20Prototype.dc.html"
cli open --browser=chromium "$URL"
cli resize 1400 860
sleep 4 # the DC runtime mounts React and generates the demo rows; too early and the list is empty

shot() { sleep "${2:-0.6}"; cli screenshot --filename="$OUT/$1.png"; echo "  $1"; }

# light: default inbox, then the AGE toggle (arrow flips to up = oldest first)
shot proto-01-inbox
ev "() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'AGE'); \
if (!b) return 'MISSING: AGE'; b.click(); return 'clicked AGE · ' + b.getAttribute('title'); }"
sleep 1
shot proto-27-sort-oldest
ev "() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'AGE'); \
if (!b) return 'MISSING: AGE'; b.click(); return 'restored newest first'; }"
sleep 1

# dark: the theme switch lives in the prototype's own Settings view; return to Inbox afterwards
clickText Settings
shot proto-02-settings-dark-switch 0.8
clickText Dark
clickText Inbox
shot proto-dark-01-inbox
ev "() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'AGE'); \
if (!b) return 'MISSING: AGE'; b.click(); return 'clicked AGE (dark)'; }"
sleep 1
shot proto-dark-27-sort-oldest

echo "prototype screenshots in $OUT"
