#!/usr/bin/env bash
# Install-smoke check: `uv tool install .` into a throwaway tool dir, then run the installed `inbox-sift --demo`
# with an EMPTY PATH (so no node, pnpm, gh or anything else can be picked up) and hit the API and static assets.
#
#   scripts/smoke_install.sh            # uses a temp dir, cleans up afterwards
#
# Never touches your real uv tools, your gh login or GitHub: demo mode uses fixtures and a fake client.
set -euo pipefail
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'kill "${pid:-0}" 2>/dev/null || true; rm -rf "$work"' EXIT
export UV_TOOL_DIR="$work/tools" UV_TOOL_BIN_DIR="$work/bin"

uv tool install --quiet .
bin="$work/bin/inbox-sift"
port=$((9500 + RANDOM % 400))

env -i HOME="$work/home" PATH="/nonexistent" "$bin" --version
env -i HOME="$work/home" PATH="/nonexistent" "$bin" --demo --no-browser --port "$port" --data-dir "$work/data" \
  > "$work/server.log" 2>&1 &
pid=$!

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$port/api/meta" > /dev/null && break
  sleep 0.2
done

fail=0
check() { # url expected-substring
  if curl -sf "http://127.0.0.1:$port$1" | grep -q -- "$2"; then echo "ok    $1"; else echo "FAIL  $1"; fail=1; fi
}
check /api/counts '"filtered":713'
check / '<title>Inbox Sift</title>'
check /static/js/main.js 'computeLayout'
check /static/css/tokens.css '--accent'
check /static/brand/mark.svg '<svg'
check /static/icons/inbox.svg '<svg'
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/static/fonts/red-hat-text-latin-400-normal.woff2")
[ "$code" = 200 ] && echo "ok    fonts" || { echo "FAIL  fonts ($code)"; fail=1; }

echo "--- banner ---"
head -4 "$work/server.log"
[ $fail = 0 ] && echo "SMOKE OK: installed tool runs with an empty PATH (no Node, no gh)" || { echo "SMOKE FAILED"; exit 1; }
