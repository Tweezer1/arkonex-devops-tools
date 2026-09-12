#!/usr/bin/env bash
# tests/canary-timer-recurrence.sh (OPEN-125 / Issue #87)
#
# MANUAL, TEMPORARY, ROOT-REQUIRED proof procedure -- NOT part of bootstrap-browser-qa.sh,
# never wired into `all`, never run automatically. Its ONLY purpose is to demonstrate that
# the corrected browser-qa-refresh@.timer (OnCalendar=*-*-* 00/6:00:00, Issue #87 correctif
# 2026-09-12) genuinely re-arms itself repeatedly, by temporarily running under a much
# shorter cadence, observing TWO successive real firings driven by the timer itself (not
# by a manual restart), then restoring the real 6h cadence and proving its next elapse is
# populated again. Exactly the "intervalle temporairement raccourci, accepted a condition
# de rétablir la configuration finale et d'en vérifier la prochaine échéance" the human
# GO explicitly allows.
#
# Mechanism: a systemd drop-in on the TEMPLATE unit (browser-qa-refresh@.timer.d/), which
# applies to every persona instance at once, temporarily overriding OnCalendar to fire
# every minute. Removed unconditionally on exit (trap), whatever the outcome -- this
# script never leaves the host in the shortened-cadence state, even on error/Ctrl-C.
#
# Usage: sudo bash canary-timer-recurrence.sh
#
# Never touches a credential, a storageState's content, or /etc/sudoers.d. Only reads
# systemd unit state and file mtimes.

set -uo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "canary-timer-recurrence: must run as root (systemd unit drop-in + restart)" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CBQ_DIR="$(dirname "$SCRIPT_DIR")"

DROPIN_DIR="/etc/systemd/system/browser-qa-refresh@.timer.d"
DROPIN_FILE="${DROPIN_DIR}/90-open125-canary-temp.conf"
# Data-driven from personas.yaml, like every other common script here -- never a
# hardcoded persona list, so this canary automatically covers whichever personas are
# enabled at the time it is run, including any added after this file was written.
mapfile -t PERSONAS < <(python3 "${CBQ_DIR}/lib/list_enabled_personas.py" --personas "${CBQ_DIR}/personas.yaml")
POLL_INTERVAL_SEC=10
MAX_WAIT_SEC=240   # bounded: this is a canary, not an indefinite wait

log() { echo "canary-timer-recurrence: $*"; }

cleanup() {
    log "restoring the real 6h cadence (removing temporary drop-in, unconditionally)"
    rm -f "$DROPIN_FILE"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    for p in "${PERSONAS[@]}"; do
        systemctl restart "browser-qa-refresh@${p}.timer"
    done
    log "final state (must show a populated NextElapseUSecRealtime for each persona):"
    for p in "${PERSONAS[@]}"; do
        echo "--- ${p} ---"
        systemctl show "browser-qa-refresh@${p}.timer" -p NextElapseUSecRealtime -p LastTriggerUSec -p ActiveState
    done
}
trap cleanup EXIT

mkdir -p "$DROPIN_DIR"
# Reset the inherited OnCalendar/OnBootSec/Persistent lists to empty first (systemd
# directives that take repeatable values APPEND across drop-ins unless explicitly reset
# with a bare `Key=` line) before setting the short test cadence -- otherwise this would
# add a second schedule alongside the real one rather than replacing it for the test.
cat > "$DROPIN_FILE" <<'EOF'
[Timer]
OnCalendar=
OnBootSec=
Persistent=
OnCalendar=*-*-* *:*:00
# Real bug hit live on the first run of this exact script (2026-09-12): without this
# reset, RandomizedDelaySec=5min is inherited unchanged from the base unit and applies
# independently to EACH computed per-minute occurrence -- observed result: only 1 fire
# in 240s for one persona and 0 for the other, instead of one roughly every minute.
RandomizedDelaySec=0
EOF
systemctl daemon-reload

declare -A baseline
for p in "${PERSONAS[@]}"; do
    systemctl restart "browser-qa-refresh@${p}.timer"
    # LastTriggerUSec lives on the TIMER unit and is reliably populated on every real
    # fire (confirmed during this correctif's own runtime canary) -- unlike the
    # oneshot .service's own ActiveEnterTimestamp, which this same canary observed
    # staying blank even immediately after a successful, Result=success run.
    baseline["$p"]="$(systemctl show "browser-qa-refresh@${p}.timer" -p LastTriggerUSec --value)"
    log "${p}: baseline LastTriggerUSec (post-restart, counts as fire #0, not a planned recurrence) = ${baseline[$p]:-<empty>}"
done

declare -A fire_count
for p in "${PERSONAS[@]}"; do fire_count["$p"]=0; done

elapsed=0
while [[ "$elapsed" -lt "$MAX_WAIT_SEC" ]]; do
    sleep "$POLL_INTERVAL_SEC"
    elapsed=$((elapsed + POLL_INTERVAL_SEC))
    all_done=1
    for p in "${PERSONAS[@]}"; do
        [[ "${fire_count[$p]}" -ge 2 ]] && continue
        current="$(systemctl show "browser-qa-refresh@${p}.timer" -p LastTriggerUSec --value)"
        if [[ -n "$current" && "$current" != "${baseline[$p]}" ]]; then
            fire_count["$p"]=$((fire_count["$p"] + 1))
            baseline["$p"]="$current"
            log "${p}: PLANNED fire #${fire_count[$p]} observed at ${current} (timer-driven, no manual restart)"
        fi
        [[ "${fire_count[$p]}" -lt 2 ]] && all_done=0
    done
    [[ "$all_done" -eq 1 ]] && break
done

echo
echo "=================================================="
overall_rc=0
for p in "${PERSONAS[@]}"; do
    if [[ "${fire_count[$p]}" -ge 2 ]]; then
        echo "PASS  ${p}: 2 successive timer-driven firings observed"
    else
        echo "FAIL  ${p}: only ${fire_count[$p]} firing(s) observed within ${MAX_WAIT_SEC}s"
        overall_rc=1
    fi
done
echo "=================================================="

exit "$overall_rc"
