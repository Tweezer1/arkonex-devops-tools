#!/usr/bin/env bash
# prepare-persona.sh <persona>  (OPEN-125 / Issue #87)
#
# The single controlled entry point a Claude Code session is allowed to call before a
# Browser QA session, to recover from an ordinary session expiration WITHOUT a human
# reconnecting the browser. It never reads a credential and never touches a
# storageState file's content -- it only asks the systemd-owned refresher to do that
# work, and re-checks the resulting identity the same read-only way
# verify-browser-qa.sh already does.
#
# Contract:
#   1. Check first. If the persona's storageState already validates as the expected
#      identity, do nothing else -- an already-valid session is never renewed
#      (avoids unnecessary logins / unnecessary load on the refresher).
#   2. Only if not already valid: request exactly one renewal from the dedicated
#      service, via `sudo -n systemctl start browser-qa-refresh@<persona>.service`.
#      This is the ONLY privileged action in this script, scoped by a sudoers rule
#      (provision-sudoers.sh) to this exact command per enabled persona -- no shell,
#      no wildcard, no other command. `systemctl start` on this Type=oneshot unit
#      BLOCKS until the refresher finishes or its own TimeoutStartSec=120 elapses --
#      that bound is enforced by the unit itself, not reimplemented here.
#   3. Re-check once. Report a single status-shaped line and exit accordingly.
#
# Never retried automatically beyond this one bounded attempt -- a caller that gets
# EXPIRED/FAIL/SERVICE_UNAVAILABLE must stop and surface that, not loop.
#
# Concurrency: two sessions calling this for the SAME persona at the same time is safe
# by construction -- systemd merges a `start` request for a unit that already has a
# start job in flight into that same job (no second login attempt), and
# StartLimitIntervalSec/StartLimitBurst on the service unit itself throttles any real
# burst of distinct attempts. Nothing here adds a separate lock file.
#
# Explicit non-goals (out of scope for this script, by design):
#   - It never recreates the Playwright MCP server context itself -- after a PASS, the
#     CALLING session is responsible for reconnecting/restarting the corresponding
#     `playwright-<persona>` MCP server so it picks up the refreshed storageState, then
#     confirming identity with a real navigate/snapshot (a storageState written on disk
#     does not retroactively change a browser context that is already open).
#   - It never replays whatever browser/business action was interrupted by the
#     expiration -- that decision belongs to the calling session, only after it has
#     independently confirmed what state that interrupted action left behind.

set -uo pipefail

PERSONA="${1:?usage: prepare-persona.sh <persona>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSONAS_YAML="${SCRIPT_DIR}/personas.yaml"

STORAGE_STATE_DIR="${BROWSER_QA_STORAGE_STATE_DIR:-/var/lib/arkonex-browser-qa/storage-states}"
SITE_URL="${BROWSER_QA_SITE_URL:-https://deverp.arkonex.ca}"
SYSTEMCTL_BIN="${PREPARE_PERSONA_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
SUDO_BIN="${PREPARE_PERSONA_SUDO_BIN:-/usr/bin/sudo}"

log() { echo "prepare-persona[${PERSONA}]: $*" >&2; }

login_user_id="$(python3 "${SCRIPT_DIR}/lib/resolve_persona_field.py" \
    --personas "$PERSONAS_YAML" --persona "$PERSONA" --field login_user_id 2>/dev/null)"
if [[ -z "$login_user_id" ]]; then
    log "unknown persona -- not present (or not enabled) in personas.yaml"
    echo "PREPARE_${PERSONA}=UNKNOWN_PERSONA"
    exit 2
fi

storage_state_file="$(python3 "${SCRIPT_DIR}/lib/resolve_persona_field.py" \
    --personas "$PERSONAS_YAML" --persona "$PERSONA" --field storage_state_file 2>/dev/null)"
STATE_PATH="${STORAGE_STATE_DIR}/${storage_state_file}"

check_identity() {
    node "${SCRIPT_DIR}/auth/validate-storage-state.mjs" \
        --site-url "$SITE_URL" --storage-state "$STATE_PATH" 2>/dev/null
}

observed="$(check_identity)"
if [[ "$observed" == "$login_user_id" ]]; then
    log "already valid -- no renewal requested"
    echo "PREPARE_${PERSONA}=PASS (ALREADY_VALID)"
    exit 0
fi

log "not currently valid (observed='${observed:-<empty>}') -- requesting exactly one renewal"

if ! "$SUDO_BIN" -n "$SYSTEMCTL_BIN" start "browser-qa-refresh@${PERSONA}.service"; then
    start_rc=$?
    # Distinguish "the controlled entry point itself is not usable" (sudo refused the
    # command outright, e.g. missing/incomplete sudoers rule) from "the refresh ran but
    # failed" (systemctl start returns non-zero for a oneshot unit that exited non-zero
    # or hit TimeoutStartSec) -- both are reported without any secret, but only the
    # first is a pure infrastructure problem rather than an auth outcome.
    if ! "$SUDO_BIN" -n true 2>/dev/null; then
        log "STOP -- sudo refused this request non-interactively; the controlled entry" \
            "point is not correctly provisioned (see provision-sudoers.sh) -- this is an" \
            "infrastructure gap, not an ordinary session expiration"
        echo "PREPARE_${PERSONA}=SERVICE_UNAVAILABLE"
        exit "$start_rc"
    fi
    log "renewal request returned a failure (exit ${start_rc}) -- re-checking identity" \
        "anyway, since refresh-persona.sh never deletes a previously valid storageState" \
        "on failure"
fi

observed2="$(check_identity)"
if [[ "$observed2" == "$login_user_id" ]]; then
    log "renewal succeeded"
    echo "PREPARE_${PERSONA}=PASS (RENEWED)"
    exit 0
elif [[ "$observed2" == "Guest" || -z "$observed2" ]]; then
    log "STOP -- renewal did not produce a validated session; do not proceed with a" \
        "Browser QA session for this persona, and do not replay any interrupted" \
        "browser/business action blindly"
    echo "PREPARE_${PERSONA}=EXPIRED"
    exit 1
else
    log "STOP -- storageState now validates as an unexpected identity, not" \
        "'${login_user_id}'"
    echo "PREPARE_${PERSONA}=FAIL"
    exit 1
fi
