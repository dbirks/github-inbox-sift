#!/usr/bin/env bash
# Visual review matrix: drives `inbox-sift --demo` with the Playwright agent CLI (`pnpx @playwright/cli`) and saves a
# screenshot of every view and state in light and dark themes, plus side-by-side light|dark montages for review.
#
#   scripts/screenshots.sh [OUT_DIR]      # default OUT_DIR: ./screenshots (gitignored)
#
# Demo mode only (fixtures + fake GitHub). Needs pnpm; ImageMagick `montage` is optional (for the pairs).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-screenshots}")
mkdir -p "$OUT"
PORT=$((9600 + RANDOM % 300))
DATA=$(mktemp -d)
S=shots$$
cli() { timeout 90 pnpx @playwright/cli -s=$S "$@" > /dev/null 2>&1 || echo "  (cli $1 failed)"; }
ev() { cli eval "$1"; }
api() { curl -sf -X "$1" -H 'X-Inbox-Sift: 1' -H 'Content-Type: application/json' -d "${3:-{\}}" "http://127.0.0.1:$PORT$2" > /dev/null; }

uv run inbox-sift --demo --no-browser --port "$PORT" --data-dir "$DATA" > "$DATA/server.log" 2>&1 &
SERVER=$!
trap 'cli close; kill $SERVER 2>/dev/null; rm -rf "$DATA"' EXIT
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/api/meta" > /dev/null && break; sleep 0.2; done

cli open --browser=chromium "http://127.0.0.1:$PORT/"
cli resize 1400 860

A='window.__inboxSift.actions'
shot() { sleep "${2:-0.6}"; cli screenshot --filename="$OUT/$THEME-$1.png"; echo "  $THEME-$1"; }
reset() { # scenario
  api POST /api/demo/reset
  api POST /api/demo/scenario "{\"host_problem\":\"$1\",\"detail_fail\":false}"
  api PUT /api/settings "{\"theme\":\"$THEME\",\"density\":\"compact\",\"poll\":\"adaptive\",\"read_on_open\":false,\"hidden_hosts\":[],\"layout\":{\"navW\":212,\"drawerW\":360,\"navCollapsed\":false}}"
  cli reload
  sleep 1.5
}

for THEME in light dark; do
  echo "theme: $THEME"
  reset expired
  shot 01-inbox
  cli press j; shot 02-panel
  ev "() => $A.toggleMenu('filter')"; shot 03-filter-menu
  ev "() => $A.closeMenus()"; ev "() => $A.toggleMenu('more')"; shot 04-more-menu
  ev "() => $A.closeMenus()"
  cli hover "[data-testid^=row-github]:nth-child(3)"; shot 05-row-hover
  cli press x; cli press J; cli press J; shot 06-multiselect
  cli press Escape; cli press Escape
  ev "() => $A.setSetting('density','balanced')"; shot 07-balanced
  ev "() => $A.setSetting('density','compact')"
  ev "() => $A.toggleNav()"; shot 08-nav-collapsed
  ev "() => $A.toggleNav()"
  ev "() => $A.toggleMenu('host')"; shot 09-host-menu
  ev "() => $A.toggleMenu('hostStatus:ghe.corp')"; shot 10-host-pop-expired
  ev "() => $A.toggleMenu('hostStatus:github.com')"; shot 11-host-pop-healthy
  ev "() => $A.closeMenus()"
  ev "() => $A.go('filtered')"; shot 12-filtered
  ev "() => $A.setGroup('repo')"; shot 13-filtered-grouped
  ev "() => $A.startBulkFiltered()"; shot 14-bulk-dialog 1
  ev "() => $A.confirmBulk()"; shot 15-bulk-running 1.2
  sleep 8; shot 16-bulk-finished
  ev "() => $A.closeBulk()"
  ev "() => $A.toggleHelp()"; shot 17-help
  ev "() => $A.toggleHelp()"
  ev "() => $A.go('repos')"; shot 18-repos 1
  ev "() => $A.go('people')"; shot 19-people 1
  ev "() => $A.go('settings')"; shot 20-settings
  reset offline
  ev "() => $A.toggleMenu('hostStatus:ghe.corp')"; shot 21-host-pop-offline
  reset none
  shot 22-hosts-healthy
  api POST /api/demo/scenario '{"host_problem":"expired","detail_fail":true}'
  cli reload; sleep 1.5; cli press j; shot 23-detail-error 1
  cli resize 924 700; shot 24-narrow-924 1
  cli resize 1400 860
done

if command -v montage > /dev/null; then
  mkdir -p "$OUT/pairs"
  for f in "$OUT"/light-*.png; do
    n=$(basename "$f" .png); n=${n#light-}
    montage "$f" "$OUT/dark-$n.png" -tile 2x1 -geometry +4+0 -resize 50% "$OUT/pairs/$n.png" 2> /dev/null || true
  done
fi
echo "screenshots in $OUT"
