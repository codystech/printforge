#!/usr/bin/env bash
set -u

# Read-only health snapshot for the live PrintForge service.
# It uses GET requests only. It does not restart or modify the service.

BASE_URL="${PRINTFORGE_URL:-http://localhost:8093}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/printforge-uv-cache}"
TMP_CONFIG="$(mktemp /tmp/printforge-config.XXXXXX.json)"
TMP_MODELS="$(mktemp /tmp/printforge-models.XXXXXX.json)"
cleanup() {
  rm -f "$TMP_CONFIG" "$TMP_MODELS"
}
trap cleanup EXIT

echo "PrintForge healthcheck"
echo "url: $BASE_URL"

if systemctl --user is-active --quiet printforge; then
  echo "systemd: active"
else
  rc=$?
  echo "systemd: NOT active (systemctl rc=$rc)"
fi

if curl -fsS "$BASE_URL/config" -o "$TMP_CONFIG"; then
  echo -n "config: "
  uv run python -c 'import json,sys; d=json.load(open(sys.argv[1])); print("bambuddy=%s organic=%s" % (d.get("bambuddy"), d.get("organic")))' "$TMP_CONFIG"
else
  echo "config: GET failed"
fi

if curl -fsS "$BASE_URL/models" -o "$TMP_MODELS"; then
  uv run python -c 'import json,sys,time; rows=json.load(open(sys.argv[1])); print("models: %d" % len(rows)); m=rows[0] if rows else {}; dt=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("created", 0))) if m else "n/a"; print("newest: id=%s date=%s name=%s qa=%s backend=%s" % (m.get("id","n/a"), dt, str(m.get("name",""))[:60], m.get("qa","missing"), m.get("backend","missing")))' "$TMP_MODELS"
else
  echo "models: GET failed"
fi

if pgrep -x codex >/dev/null; then
  echo "generation: codex process present (generation or edit may be in progress)"
else
  echo "generation: no exact-name codex process"
fi
