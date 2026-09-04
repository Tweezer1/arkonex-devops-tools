#!/usr/bin/env bash
# provision-system-identity.sh [--apply]
#
# OPEN-125 / Issue #87 -- idempotent, rejouable provisioning of the OS user/group
# identity the Browser QA infrastructure depends on:
#   - system user  browserqa-refresh  (no login shell, no home) -- the ONLY component
#     ever authorized to read real credentials.
#   - system group browserqa-storage  -- shared so `frappe` can READ storageStates that
#     browserqa-refresh WRITES, without frappe ever gaining credential access.
#   - membership: frappe in browserqa-storage, browserqa-refresh in browserqa-storage.
#
# Contract (explicit human requirement, OPEN-125 canary-prep correction, section 5):
#   already present AND conformant -> PASS / no-op
#   already present but DIFFERENT from the expected shape -> STOP, never silently "fix"
#   absent -> create (--apply only; plan-only by default)
#
# Requires root (sudo) for the --apply path. The read-only conformance checks
# themselves do not require root and can be re-run at any time (e.g. from
# verify-browser-qa.sh in a future extension) to detect drift.

set -euo pipefail

# Overridable only for non-destructive testing against pre-existing accounts (see
# tests/run-phase-a-tests.sh) -- the real deployment always uses the defaults below.
EXPECTED_USER="${PROVISION_TEST_USER:-browserqa-refresh}"
EXPECTED_GROUP="${PROVISION_TEST_GROUP:-browserqa-storage}"
EXPECTED_SHELL_SUFFIX="nologin"  # accepts /usr/sbin/nologin or /sbin/nologin

apply=0
for arg in "$@"; do
    case "$arg" in
        --apply) apply=1 ;;
        *) echo "provision-system-identity: unknown argument $arg" >&2; exit 2 ;;
    esac
done

log() { echo "provision-system-identity: $*"; }
stop() { echo "provision-system-identity: STOP -- $*" >&2; exit 1; }

# --- user ---------------------------------------------------------------------------
if id "$EXPECTED_USER" >/dev/null 2>&1; then
    shell="$(getent passwd "$EXPECTED_USER" | cut -d: -f7)"
    home="$(getent passwd "$EXPECTED_USER" | cut -d: -f6)"
    if [[ "$shell" != *"$EXPECTED_SHELL_SUFFIX" ]]; then
        stop "user '${EXPECTED_USER}' already exists but with shell '${shell}' " \
             "(expected a nologin shell) -- refusing to silently change it"
    fi
    if [[ -d "$home" && "$home" != "/nonexistent" && "$home" != "/" ]]; then
        # A home directory existing is not inherently wrong, but this user was designed
        # with --no-create-home; surface it rather than assume it is fine.
        log "NOTE: user '${EXPECTED_USER}' has a home directory '${home}' -- not " \
            "the expected --no-create-home shape, but not stopped on this alone; " \
            "review manually if unexpected."
    fi
    log "user '${EXPECTED_USER}': PASS (already present, shell conforms)"
else
    if [[ "$apply" -eq 1 ]]; then
        useradd --system --no-create-home --shell "/usr/sbin/${EXPECTED_SHELL_SUFFIX}" "$EXPECTED_USER"
        log "user '${EXPECTED_USER}': created"
    else
        log "user '${EXPECTED_USER}': PLAN -- would create (system, no-create-home, nologin)"
    fi
fi

# --- group --------------------------------------------------------------------------
if getent group "$EXPECTED_GROUP" >/dev/null 2>&1; then
    log "group '${EXPECTED_GROUP}': PASS (already present)"
else
    if [[ "$apply" -eq 1 ]]; then
        groupadd --system "$EXPECTED_GROUP"
        log "group '${EXPECTED_GROUP}': created"
    else
        log "group '${EXPECTED_GROUP}': PLAN -- would create (system group)"
    fi
fi

# --- membership -----------------------------------------------------------------------
ensure_member() {
    local user="$1"
    if ! id "$user" >/dev/null 2>&1; then
        log "membership '${user}' in '${EXPECTED_GROUP}': SKIPPED (user '${user}' does not exist yet)"
        return
    fi
    if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qxF "$EXPECTED_GROUP"; then
        log "membership '${user}' in '${EXPECTED_GROUP}': PASS (already a member)"
    else
        if [[ "$apply" -eq 1 ]]; then
            usermod -aG "$EXPECTED_GROUP" "$user"
            log "membership '${user}' in '${EXPECTED_GROUP}': added"
        else
            log "membership '${user}' in '${EXPECTED_GROUP}': PLAN -- would add"
        fi
    fi
}

ensure_member "frappe"
ensure_member "$EXPECTED_USER"

if [[ "$apply" -eq 1 ]]; then
    log "done. NOTE: group membership changes only take effect in NEW processes/sessions " \
        "for already-logged-in users (frappe) -- a real new session/process is required " \
        "before frappe can read group-readable storageStates."
fi
