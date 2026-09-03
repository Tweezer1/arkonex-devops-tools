#!/usr/bin/env bash
# tests/run-phase-a-tests.sh (OPEN-125 / Issue #87, Phase A)
#
# Non-destructive test suite. Never touches /etc/hosts, never installs a browser, never
# creates a systemd unit, never reads/creates a real credential, never performs a real
# login. Everything runs against temporary directories or read-only against the real
# host where that is itself non-destructive (DNS/HTTPS checks, which only read state
# that already exists on this instance from prior OPEN-125 diagnostics).

set -uo pipefail

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CBQ_DIR="$(dirname "$SUITE_DIR")"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

PASS=0
FAIL=0
declare -a FAILED_TESTS=()

check() {
    local name="$1"
    local rc="$2"
    if [[ "$rc" -eq 0 ]]; then
        echo "PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL  $name"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

echo "### T01 -- bash -n on every shell script"
rc=0
while IFS= read -r -d '' f; do
    bash -n "$f" || rc=1
done < <(find "$CBQ_DIR" -name '*.sh' -print0)
check "T01_bash_syntax" "$rc"

echo "### T02 -- personas.yaml parses via the restricted YAML loader"
python3 -c "
import sys
sys.path.insert(0, '$CBQ_DIR/lib')
from simple_yaml import load_personas
p = load_personas('$CBQ_DIR/personas.yaml')
assert 'estimate_user' in p and 'estimate_manager' in p, p.keys()
assert p['estimate_user']['enabled'] is True
print('OK', sorted(p.keys()))
"
check "T02_personas_yaml_parses" "$?"

echo "### T03 -- MCP generation in a temp dir + JSON validity + exact server-name match"
CAND1="$TMP_ROOT/mcp.candidate1.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/personas.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND1"
rc=$?
if [[ $rc -eq 0 ]]; then
    python3 -c "
import json
d = json.load(open('$CAND1'))
names = sorted(d['mcpServers'].keys())
assert names == ['playwright-estimate_manager', 'playwright-estimate_user'], names
print('server names match exactly the enabled personas:', names)
"
    rc=$?
fi
check "T03_mcp_generation_and_exact_match" "$rc"

echo "### T04 -- no @latest, no --secrets in generated config"
if grep -q '@latest' "$CAND1" 2>/dev/null; then rc=1; else rc=0; fi
if grep -q -- '--secrets' "$CAND1" 2>/dev/null; then rc=1; fi
check "T04_no_latest_no_secrets_in_output" "$rc"

echo "### T05 -- no credential-shaped value anywhere in versioned artifacts"
rc=0
while IFS= read -r -d '' f; do
    # Case-insensitive search for the KEY names used by real credential files -- the
    # manifest and generated config must never contain these at all.
    if grep -Eqi '(^|[^A-Za-z_])(E2E_PASSWORD|BROWSER_QA_PASSWORD|password\s*[:=])' "$f" 2>/dev/null; then
        echo "  unexpected credential-shaped content in: $f"
        rc=1
    fi
done < <(find "$CBQ_DIR" -type f \( -name '*.yaml' -o -name '*.json' -o -name '*.md' \) -print0)
check "T05_no_credential_values_in_artifacts" "$rc"

echo "### T06 -- no hardcoded persona name in common (non-manifest, non-fixture) scripts"
rc=0
COMMON_SCRIPTS=(
    "$CBQ_DIR/bootstrap-browser-qa.sh"
    "$CBQ_DIR/verify-browser-qa.sh"
    "$CBQ_DIR/generate-mcp-config.py"
    "$CBQ_DIR/credentials-configure.sh"
    "$CBQ_DIR/auth/refresh-persona.sh"
    "$CBQ_DIR/auth/playwright-login.mjs"
    "$CBQ_DIR/auth/validate-storage-state.mjs"
    "$CBQ_DIR/lib/simple_yaml.py"
    "$CBQ_DIR/lib/resolve_persona_field.py"
    "$CBQ_DIR/lib/list_enabled_personas.py"
)
for f in "${COMMON_SCRIPTS[@]}"; do
    if grep -Eq 'estimate_user|estimate_manager' "$f"; then
        echo "  hardcoded persona name found in: $f"
        rc=1
    fi
done
check "T06_no_hardcoded_persona_in_common_scripts" "$rc"

echo "### T07 -- invalid persona entry aborts generation totally (0 file written)"
CAND_INVALID="$TMP_ROOT/mcp.should-not-exist.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-invalid.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_INVALID" 2>/tmp/t07_stderr.$$
gen_rc=$?
if [[ "$gen_rc" -ne 0 && ! -f "$CAND_INVALID" ]]; then
    rc=0
else
    rc=1
fi
rm -f "/tmp/t07_stderr.$$"
check "T07_invalid_persona_aborts_generation_entirely" "$rc"

echo "### T08 -- atomic activation in a temp target dir (bootstrap generate-config --apply)"
TARGET_DIR="$TMP_ROOT/bench-root"
mkdir -p "$TARGET_DIR"
CLAUDE_BENCH_ROOT="$TARGET_DIR" bash "$CBQ_DIR/bootstrap-browser-qa.sh" generate-config --apply \
    --storage-state-dir "$TMP_ROOT/storage-states" --target "$TARGET_DIR/.mcp.json" >/dev/null
rc=$?
[[ -f "$TARGET_DIR/.mcp.json" ]] || rc=1
check "T08_atomic_activation_in_temp_target" "$rc"

echo "### T09 -- drift detection: re-activation with an unmodified manifest = PASS, with a modified one = DRIFT"
CLAUDE_BENCH_ROOT="$TARGET_DIR" BROWSER_QA_STORAGE_STATE_DIR="$TMP_ROOT/storage-states" \
    bash "$CBQ_DIR/verify-browser-qa.sh" > "$TMP_ROOT/verify1.out" 2>&1
grep -q '^ACTIVE_CONFIG=PASS$' "$TMP_ROOT/verify1.out"
rc_pass=$?

# Simulate drift: hand-edit the activated file so it no longer matches what the
# generator would produce from the real manifest.
python3 -c "
import json
p = '$TARGET_DIR/.mcp.json'
d = json.load(open(p))
d['mcpServers']['playwright-drift-canary-only'] = {'command': 'npx', 'args': []}
json.dump(d, open(p, 'w'))
"
CLAUDE_BENCH_ROOT="$TARGET_DIR" BROWSER_QA_STORAGE_STATE_DIR="$TMP_ROOT/storage-states" \
    bash "$CBQ_DIR/verify-browser-qa.sh" > "$TMP_ROOT/verify2.out" 2>&1
grep -q '^ACTIVE_CONFIG=DRIFT$' "$TMP_ROOT/verify2.out"
rc_drift=$?

if [[ "$rc_pass" -eq 0 && "$rc_drift" -eq 0 ]]; then rc=0; else rc=1; fi
check "T09_active_config_drift_detection" "$rc"

echo "### T10 -- PERSONA_EXTENSION_TEST (3rd fictional persona, enabled:true, fixture-only, no real credentials/ERPNext account)"
CAND_EXT="$TMP_ROOT/mcp.extended.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-extended.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_EXT"
rc=$?
if [[ $rc -eq 0 ]]; then
    python3 -c "
import json
d = json.load(open('$CAND_EXT'))
names = sorted(d['mcpServers'].keys())
expected = ['playwright-estimate_manager', 'playwright-estimate_user', 'playwright-project_manager']
assert names == expected, names
print('3-persona generation succeeded with zero script modification:', names)
"
    rc=$?
fi
check "T10_persona_extension_test" "$rc"

echo "### T11 -- git diff --check (whitespace hygiene) on the working tree"
( cd "$CBQ_DIR/.." && git diff --check )
check "T11_git_diff_check" "$?"

echo "### T12 -- U2 finding F1 regression: duplicate storage_state_file across enabled personas aborts entirely"
CAND_DUP1="$TMP_ROOT/mcp.should-not-exist-dup1.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-duplicate-storage-state.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_DUP1" 2>/dev/null
if [[ $? -ne 0 && ! -f "$CAND_DUP1" ]]; then rc=0; else rc=1; fi
check "T12_duplicate_storage_state_rejected" "$rc"

echo "### T13 -- U2 finding F2 regression: duplicate mcp_server_name across enabled personas aborts entirely"
CAND_DUP2="$TMP_ROOT/mcp.should-not-exist-dup2.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-duplicate-server-name.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_DUP2" 2>/dev/null
if [[ $? -ne 0 && ! -f "$CAND_DUP2" ]]; then rc=0; else rc=1; fi
check "T13_duplicate_server_name_rejected" "$rc"

echo "### T14 -- U2 finding F3 regression: path traversal in storage_state_file aborts entirely"
CAND_TRAV="$TMP_ROOT/mcp.should-not-exist-traversal.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-path-traversal.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_TRAV" 2>/dev/null
if [[ $? -ne 0 && ! -f "$CAND_TRAV" ]]; then rc=0; else rc=1; fi
check "T14_path_traversal_rejected" "$rc"

echo "### T15 -- U2 finding F4 regression: enabled: yes (non-strict-bool) is NOT treated as active"
CAND_NONSTRICT="$TMP_ROOT/mcp.should-not-exist-nonstrict.json"
err_out="$(python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/tests/fixtures/personas-non-strict-enabled.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_NONSTRICT" 2>&1)"
gen_rc=$?
if [[ "$gen_rc" -ne 0 && ! -f "$CAND_NONSTRICT" ]] && echo "$err_out" | grep -q 'no persona with enabled: true'; then
    rc=0
else
    rc=1
fi
check "T15_strict_boolean_enabled" "$rc"

echo "### T16 -- generated config for the real personas.yaml is still exactly the 2 expected servers (no duplicate/traversal regression on real data)"
CAND_REAL="$TMP_ROOT/mcp.real-recheck.json"
python3 "$CBQ_DIR/generate-mcp-config.py" \
    --personas "$CBQ_DIR/personas.yaml" \
    --storage-state-dir "$TMP_ROOT/storage-states" \
    --output "$CAND_REAL"
rc=$?
if [[ $rc -eq 0 ]]; then
    python3 -c "
import json
d = json.load(open('$CAND_REAL'))
names = sorted(d['mcpServers'].keys())
assert names == ['playwright-estimate_manager', 'playwright-estimate_user'], names
print('real personas.yaml still generates exactly:', names)
"
    rc=$?
fi
check "T16_real_manifest_unaffected_by_fix" "$rc"

echo "### T17 -- provision-system-identity.sh: conformant existing group = PASS/no-op"
out="$(PROVISION_TEST_GROUP="sudo" bash "$CBQ_DIR/provision-system-identity.sh" 2>&1)"
echo "$out" | grep -q "group 'sudo': PASS (already present)"
check "T17_provision_identity_conformant_noop" "$?"

echo "### T18 -- provision-system-identity.sh: real divergence (frappe has a login shell) = STOP, exit non-zero"
set +e
PROVISION_TEST_USER="frappe" bash "$CBQ_DIR/provision-system-identity.sh" >/tmp/t18_out.$$ 2>&1
rc=$?
set -e
grep -q "STOP -- user 'frappe' already exists but with shell" /tmp/t18_out.$$
grep_rc=$?
rm -f "/tmp/t18_out.$$"
if [[ "$rc" -ne 0 && "$grep_rc" -eq 0 ]]; then rc=0; else rc=1; fi
check "T18_provision_identity_divergence_stops" "$rc"

echo "### T19 -- deploy-controlled-copy.sh: ghost/stale file removed on redeploy (anti-ghost, target-only)"
DEPLOY_TEST_DIR="$TMP_ROOT/deploy-ghost-test"
bash "$CBQ_DIR/deploy-controlled-copy.sh" --apply --target "$DEPLOY_TEST_DIR" >/dev/null 2>&1
touch "$DEPLOY_TEST_DIR/STALE_GHOST_FILE.txt"
bash "$CBQ_DIR/deploy-controlled-copy.sh" --apply --target "$DEPLOY_TEST_DIR" >/dev/null 2>&1
if [[ -f "$DEPLOY_TEST_DIR/STALE_GHOST_FILE.txt" ]]; then rc=1; else rc=0; fi
check "T19_deploy_anti_ghost" "$rc"

echo "### T20 -- deploy-controlled-copy.sh: npm ci installs the exact pinned playwright version"
if [[ -f "$DEPLOY_TEST_DIR/auth/node_modules/playwright/package.json" ]]; then
    installed_version="$(python3 -c "import json; print(json.load(open('$DEPLOY_TEST_DIR/auth/node_modules/playwright/package.json'))['version'])")"
    [[ "$installed_version" == "1.63.0-alpha-2026-08-31" ]] && rc=0 || rc=1
else
    rc=1
fi
check "T20_deploy_pinned_playwright_version" "$rc"

echo "### T21 -- credentials-configure.sh writes correct file (test mode, fake value) and never prints it"
CRED_TEST_DIR="$TMP_ROOT/credentials-test"
out21="$(echo "not-a-real-secret-fixture-value" | BROWSER_QA_CREDENTIALS_DIR="$CRED_TEST_DIR" \
    CREDENTIALS_CONFIGURE_TEST_MODE=1 bash "$CBQ_DIR/credentials-configure.sh" estimate_user 2>&1)"
rc=0
echo "$out21" | grep -q "not-a-real-secret-fixture-value" && rc=1
[[ -f "$CRED_TEST_DIR/estimate_user.env" ]] || rc=1
[[ "$(stat -c '%a' "$CRED_TEST_DIR/estimate_user.env" 2>/dev/null)" == "600" ]] || rc=1
grep -qF "BROWSER_QA_USER_ID=e2e-tests@arkonex.ca" "$CRED_TEST_DIR/estimate_user.env" || rc=1
check "T21_credentials_configure_writes_and_never_prints_value" "$rc"

echo "### T22 -- credentials-configure.sh --verify re-confirms without re-prompting or reading back the value"
out22="$(BROWSER_QA_CREDENTIALS_DIR="$CRED_TEST_DIR" bash "$CBQ_DIR/credentials-configure.sh" estimate_user --verify)"
echo "$out22" | grep -q "VERIFY=PASS"
check "T22_credentials_configure_verify_standalone" "$?"

echo "### T23 -- credentials-configure.sh: static check, no path where \$password reaches argv/echo/log"
rc=0
# The only acceptable uses of $password: the printf that writes it to the target file,
# and the read that captures it. Any grep hit for $password outside those two contexts
# (e.g. inside an echo/log/printf-to-stdout) is a leak.
if grep -n 'password' "$CBQ_DIR/credentials-configure.sh" | grep -Ev '^\s*[0-9]+:(\s*#|.*read -rs|.*read -r password|.*printf .BROWSER_QA_PASSWORD=%s.*password|.*-z "\$password"|.*unset password|.*Password for)' | grep -q '\$password'; then
    rc=1
fi
check "T23_credentials_configure_no_password_leak_path" "$rc"

echo "### T24 -- refresh-persona.sh: candidate gets mode/group set BEFORE atomic rename (no window with wrong perms)"
rc=0
grep -n 'chmod 0640 "\$CANDIDATE_STATE_PATH"' "$CBQ_DIR/auth/refresh-persona.sh" >/dev/null || rc=1
grep -n 'chgrp "\$STORAGE_STATE_GROUP" "\$CANDIDATE_STATE_PATH"' "$CBQ_DIR/auth/refresh-persona.sh" >/dev/null || rc=1
# The chmod/chgrp lines must appear BEFORE the mv -f line (line-number ordering).
chmod_line=$(grep -n 'chmod 0640 "\$CANDIDATE_STATE_PATH"' "$CBQ_DIR/auth/refresh-persona.sh" | head -1 | cut -d: -f1)
mv_line=$(grep -n 'mv -f "\$CANDIDATE_STATE_PATH" "\$ACTIVE_STATE_PATH"' "$CBQ_DIR/auth/refresh-persona.sh" | head -1 | cut -d: -f1)
[[ -n "$chmod_line" && -n "$mv_line" && "$chmod_line" -lt "$mv_line" ]] || rc=1
check "T24_refresh_persona_permissions_before_atomic_rename" "$rc"

echo "### T25 -- post-deploy operations are self-contained in the controlled copy (never silently reference the checkout)"
DEPLOY_ISOLATION_DIR="$TMP_ROOT/deploy-isolation-test"
bash "$CBQ_DIR/deploy-controlled-copy.sh" --apply --target "$DEPLOY_ISOLATION_DIR" >/dev/null 2>&1
# Mutate ONLY the deployed copy's manifest (disable estimate_manager) -- if any script
# invoked from the deployed copy silently fell back to $CBQ_DIR's personas.yaml, this
# divergence would go undetected. Proving the opposite: output must reflect the
# DEPLOYED (mutated) manifest, not the checkout's original one.
python3 -c "
path = '$DEPLOY_ISOLATION_DIR/personas.yaml'
content = open(path).read()
header, sep, tail = content.partition('  estimate_manager:')
assert sep, 'estimate_manager: block not found in deployed personas.yaml'
tail = tail.replace('    enabled: true', '    enabled: false', 1)
open(path, 'w').write(header + sep + tail)
"
deployed_out="$(bash "$DEPLOY_ISOLATION_DIR/bootstrap-browser-qa.sh" generate-config \
    --storage-state-dir "$TMP_ROOT/storage-states" 2>&1)"
rc=0
echo "$deployed_out" | grep -q "playwright-estimate_manager" && rc=1   # must be ABSENT (disabled in the deployed copy)
echo "$deployed_out" | grep -q "playwright-estimate_user" || rc=1     # must be PRESENT
check "T25_post_deploy_operations_isolated_from_checkout" "$rc"

echo
echo "=================================================="
echo "TOTAL: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
    echo "FAILED: ${FAILED_TESTS[*]}"
fi
echo "=================================================="

exit $(( FAIL > 0 ? 1 : 0 ))
