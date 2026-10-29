#!/usr/bin/env bash
# verify-browser-qa.sh (OPEN-125 / Issue #87)
#
# READ-ONLY preflight/status verifier. Never writes, never corrects, never attempts a
# re-authentication itself -- a FAIL/EXPIRED/DRIFT result here means STOP and hand off to
# the appropriate separate mechanism (the auth refresher for AUTH_*, a human/bootstrap
# --apply run for ACTIVE_CONFIG/DNS drift). Exits non-zero if ANY check is not PASS, so
# it can gate a Browser QA session with a single command.
#
# All output is status-shaped (PASS/FAIL/EXPIRED/DRIFT) -- never a secret, cookie, or SID.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSONAS_YAML="${SCRIPT_DIR}/personas.yaml"

CLAUDE_BENCH_ROOT="${CLAUDE_BENCH_ROOT:-/home/frappe/frappe-bench}"
ACTIVE_MCP_JSON="${CLAUDE_BENCH_ROOT}/.mcp.json"
STORAGE_STATE_DIR="${BROWSER_QA_STORAGE_STATE_DIR:-/var/lib/arkonex-browser-qa/storage-states}"
SITE_URL="${BROWSER_QA_SITE_URL:-https://deverp.arkonex.ca}"

OVERALL_RC=0
fail() { OVERALL_RC=1; }

echo "=== SOURCE_CONFIG ==="
if [[ -f "${SCRIPT_DIR}/personas.yaml" && -f "${SCRIPT_DIR}/generate-mcp-config.py" ]]; then
    echo "SOURCE_CONFIG=PASS"
else
    echo "SOURCE_CONFIG=FAIL"; fail
fi

echo "=== ACTIVE_CONFIG (drift check against current personas.yaml) ==="
if [[ ! -f "$ACTIVE_MCP_JSON" ]]; then
    echo "ACTIVE_CONFIG=FAIL (not activated yet)"; fail
else
    candidate="$(mktemp)"
    if python3 "${SCRIPT_DIR}/generate-mcp-config.py" \
            --personas "$PERSONAS_YAML" \
            --storage-state-dir "$STORAGE_STATE_DIR" \
            --output "$candidate" >/dev/null 2>&1; then
        if diff -q "$candidate" "$ACTIVE_MCP_JSON" >/dev/null 2>&1; then
            echo "ACTIVE_CONFIG=PASS"
        elif python3 "${SCRIPT_DIR}/generate-mcp-config.py" --direct \
                --personas "$PERSONAS_YAML" --storage-state-dir "$STORAGE_STATE_DIR" \
                --output "$candidate" >/dev/null 2>&1 \
             && diff -q "$candidate" "$ACTIVE_MCP_JSON" >/dev/null 2>&1; then
            # OPEN-209: direct launch, activated on purpose as the rollback (generate-config --direct).
            echo "ACTIVE_CONFIG=PASS (direct launch, without the lazy proxy)"
        else
            echo "ACTIVE_CONFIG=DRIFT"; fail
        fi
    else
        echo "ACTIVE_CONFIG=FAIL (candidate generation itself failed)"; fail
    fi
    rm -f "$candidate"
fi

echo "=== MCP_VERSION ==="
if [[ -f "$ACTIVE_MCP_JSON" ]] && grep -q '"@playwright/mcp@0\.0\.80"' "$ACTIVE_MCP_JSON" 2>/dev/null \
   && ! grep -q '@latest' "$ACTIVE_MCP_JSON" 2>/dev/null; then
    echo "MCP_VERSION=PASS"
else
    echo "MCP_VERSION=FAIL"; fail
fi

echo "=== LAZY_PROXY (OPEN-209: deployed proxy + cache usable for the active config) ==="
lazy_line="$(python3 - "$ACTIVE_MCP_JSON" 2>/dev/null <<'PY'
import json, os, subprocess, sys
servers = json.load(open(sys.argv[1]))["mcpServers"]
entry = servers[sorted(servers)[0]]
argv = [entry["command"], *entry["args"]]
if "--" not in argv:
    print("LAZY_PROXY=N/A (direct launch, no proxy)")
    raise SystemExit(0)
proxy, real = argv[1], argv[argv.index("--") + 1:]
cache = argv[argv.index("--cache") + 1]
if not os.path.isfile(proxy):
    print("LAZY_PROXY=FAIL (proxy not deployed)")
    raise SystemExit(1)
rc = subprocess.run(["python3", proxy, "--check-cache", cache, "--", *real], capture_output=True).returncode
print("LAZY_PROXY=PASS" if rc == 0 else "LAZY_PROXY=FAIL (cache missing or stale: bootstrap-browser-qa.sh lazy-cache --apply)")
raise SystemExit(rc)
PY
)"
lazy_rc=$?
echo "${lazy_line:-LAZY_PROXY=FAIL (active config unreadable)}"
[[ $lazy_rc -eq 0 && -n "$lazy_line" ]] || fail

echo "=== BROWSER_VERSION ==="
echo "BROWSER_VERSION=PENDING (BROWSER_INSTALL_EXACT_COMMAND not yet contracted, see OPEN-125 section 12)"

echo "=== DNS ==="
if getent hosts deverp.arkonex.ca >/dev/null 2>&1; then
    echo "DNS=PASS"
else
    echo "DNS=FAIL"; fail
fi

echo "=== HTTPS ==="
http_code="$(curl -s -o /dev/null -w '%{http_code}' https://deverp.arkonex.ca/ 2>/dev/null || echo 000)"
if [[ "$http_code" == "200" ]]; then
    echo "HTTPS=PASS"
else
    echo "HTTPS=FAIL (${http_code})"; fail
fi

echo "=== PERSONAS ==="
while IFS= read -r persona; do
    [[ -z "$persona" ]] && continue
    echo "PERSONA_${persona}=KNOWN"

    login_user_id="$(python3 "${SCRIPT_DIR}/lib/resolve_persona_field.py" \
        --personas "$PERSONAS_YAML" --persona "$persona" --field login_user_id 2>/dev/null)"
    storage_state_file="$(python3 "${SCRIPT_DIR}/lib/resolve_persona_field.py" \
        --personas "$PERSONAS_YAML" --persona "$persona" --field storage_state_file 2>/dev/null)"
    state_path="${STORAGE_STATE_DIR}/${storage_state_file}"

    if [[ ! -f "$state_path" ]]; then
        echo "AUTH_${persona}=EXPIRED (no storageState present)"; fail
        continue
    fi

    observed="$(node "${SCRIPT_DIR}/auth/validate-storage-state.mjs" \
        --site-url "$SITE_URL" --storage-state "$state_path" 2>/dev/null)"

    if [[ "$observed" == "$login_user_id" ]]; then
        echo "AUTH_${persona}=PASS"
    elif [[ "$observed" == "Guest" || -z "$observed" ]]; then
        echo "AUTH_${persona}=EXPIRED"; fail
    else
        echo "AUTH_${persona}=FAIL (unexpected identity)"; fail
    fi
done < <(python3 "${SCRIPT_DIR}/lib/list_enabled_personas.py" --personas "$PERSONAS_YAML")

echo "=== OVERALL ==="
if [[ "$OVERALL_RC" -eq 0 ]]; then
    echo "OVERALL=PASS"
else
    echo "OVERALL=FAIL -- STOP, do not proceed with a Browser QA session"
fi

exit "$OVERALL_RC"
