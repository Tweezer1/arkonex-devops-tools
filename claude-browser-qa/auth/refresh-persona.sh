#!/usr/bin/env bash
# refresh-persona.sh <persona-name>
#
# OPEN-125 / Issue #87 -- generic, data-driven storageState refresher. Invoked by the
# systemd TEMPLATE unit browser-qa-refresh@.service as `refresh-persona.sh %i`. This
# script is the ONLY component in the whole claude-browser-qa/ design authorized to read
# real credentials. It is designed to run as the dedicated OS user `browserqa-refresh`,
# never as `frappe` (which runs the MCP server and Claude Code).
#
# NEVER RUN THIS SCRIPT AS A HUMAN ATTACHED TO A CLAUDE CODE SESSION. It is meant to be
# invoked only by systemd, or by a human operator directly at a real terminal outside any
# AI-driven session -- exactly the same rule that already governs
# sites/deverp.arkonex.ca/private/e2e-playwright/auth-setup.mjs.
#
# Contract (OPEN-125 section 8 "AUTH REFRESH ATOMIQUE"):
#   login -> temporary candidate file -> authenticated validation -> PASS: atomic rename
#   over the active storageState ; FAIL: delete the candidate, leave the previous active
#   storageState untouched. Never delete-then-login.
#
# Secrets discipline: credential values are sourced into this process's environment only
# (never passed as argv, never echoed, never logged). Only PASS/EXPIRED/FAIL-shaped
# status lines are ever printed or journaled.
#
# NOT EXECUTED IN PHASE A (OPEN-125). No systemd unit installed, no credentials
# directory exists yet, no browser installed. This file is source-only in this pass.

set -euo pipefail

PERSONA="${1:?usage: refresh-persona.sh <persona-name>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_BROWSER_QA_DIR="$(dirname "$SCRIPT_DIR")"
PERSONAS_YAML="${CLAUDE_BROWSER_QA_DIR}/personas.yaml"

# Resolved at bootstrap time (OPEN-125 D5/D6), not hardcoded here -- kept as env-overridable
# defaults so tests can point them at a temporary directory instead.
CREDENTIALS_DIR="${BROWSER_QA_CREDENTIALS_DIR:-/etc/arkonex/browser-qa/credentials}"
STORAGE_STATE_DIR="${BROWSER_QA_STORAGE_STATE_DIR:-/var/lib/arkonex-browser-qa/storage-states}"
SITE_URL="${BROWSER_QA_SITE_URL:-https://deverp.arkonex.ca}"
# Shared group frappe belongs to so it can READ storageStates that only browserqa-refresh
# WRITES (canary-prep correction, section 1 "STORAGESTATE READABILITY"). Group ownership
# of a freshly created file is only reliably guaranteed by the directory's setgid bit
# (provision-system-identity.sh / the sudo-gated directory setup do not set this alone --
# this script also explicitly enforces it on the candidate, defense in depth) OR this
# explicit chgrp -- never left to inheritance alone.
STORAGE_STATE_GROUP="${BROWSER_QA_STORAGE_STATE_GROUP:-browserqa-storage}"

log() {
    # Status-only logging -- never pass a variable that could contain a secret/cookie.
    echo "refresh-persona[${PERSONA}]: $*"
}

# --- Resolve this persona's file names from personas.yaml (data-driven, no hardcoded
#     persona names anywhere in this script). ---
resolve_field() {
    python3 "${CLAUDE_BROWSER_QA_DIR}/lib/resolve_persona_field.py" \
        --personas "$PERSONAS_YAML" --persona "$PERSONA" --field "$1"
}

CREDENTIALS_FILE="$(resolve_field credentials_file)"
STORAGE_STATE_FILE="$(resolve_field storage_state_file)"
LOGIN_USER_ID="$(resolve_field login_user_id)"

CREDENTIALS_PATH="${CREDENTIALS_DIR}/${CREDENTIALS_FILE}"
ACTIVE_STATE_PATH="${STORAGE_STATE_DIR}/${STORAGE_STATE_FILE}"
CANDIDATE_STATE_PATH="${ACTIVE_STATE_PATH}.candidate.$$"

if [[ ! -r "$CREDENTIALS_PATH" ]]; then
    log "FAIL — credentials file not readable (expected only by browserqa-refresh)"
    exit 1
fi

cleanup() {
    rm -f "$CANDIDATE_STATE_PATH"
}
trap cleanup EXIT

# Read the two keys as LITERAL strings -- deliberately NOT `source`, which interprets
# the file as bash and would silently mis-parse (or truncate) any password value
# containing a space, $, backtick, quote, or other shell-special character (real bug,
# found during Phase B activation canary: a real password broke playwright-login.mjs
# with an opaque PlaywrightError downstream, traced back to this exact mechanism -- a
# space in the password caused `source` to interpret the remainder as a second shell
# command). `cut -d= -f2-` on a grep-matched line never re-interprets the value.
BROWSER_QA_USER_ID="$(grep -m1 '^BROWSER_QA_USER_ID=' "$CREDENTIALS_PATH" | cut -d= -f2-)"
BROWSER_QA_PASSWORD="$(grep -m1 '^BROWSER_QA_PASSWORD=' "$CREDENTIALS_PATH" | cut -d= -f2-)"
export BROWSER_QA_USER_ID BROWSER_QA_PASSWORD

if [[ -z "$BROWSER_QA_USER_ID" || -z "$BROWSER_QA_PASSWORD" ]]; then
    log "FAIL — credentials file present but BROWSER_QA_USER_ID or BROWSER_QA_PASSWORD could not be parsed"
    exit 1
fi

# --- Real login + storageState capture happens in a dedicated Playwright helper,
#     intentionally NOT bash (bash cannot drive a real browser). See
#     auth/playwright-login.mjs for the exact design -- not executed in Phase A. ---
if ! node "${SCRIPT_DIR}/playwright-login.mjs" \
        --site-url "$SITE_URL" \
        --output "$CANDIDATE_STATE_PATH"; then
    log "FAIL — login did not produce a candidate storageState"
    exit 1
fi

# --- Authenticated validation (never reads/prints the cookie value itself). ---
OBSERVED_USER="$(node "${SCRIPT_DIR}/validate-storage-state.mjs" \
    --site-url "$SITE_URL" --storage-state "$CANDIDATE_STATE_PATH" || true)"

if [[ "$OBSERVED_USER" == "$LOGIN_USER_ID" ]]; then
    mkdir -p "$STORAGE_STATE_DIR"
    # Set final mode/group on the CANDIDATE first, THEN rename -- so the atomic rename
    # itself carries already-correct permissions. There is never a window, even
    # momentarily, where the file at ACTIVE_STATE_PATH has the wrong (e.g. 0600,
    # owner-only) mode -- fixing correctness gap flagged in canary-prep correction,
    # section 1: "la simple appartenance du répertoire au groupe ne suffit pas si les
    # fichiers finissent en 0600."
    chmod 0640 "$CANDIDATE_STATE_PATH"
    chgrp "$STORAGE_STATE_GROUP" "$CANDIDATE_STATE_PATH"
    mv -f "$CANDIDATE_STATE_PATH" "$ACTIVE_STATE_PATH"
    log "PASS — storageState refreshed and validated (mode 0640, group ${STORAGE_STATE_GROUP})"
    echo "AUTH_${PERSONA}=PASS"
    exit 0
elif [[ "$OBSERVED_USER" == "Guest" || -z "$OBSERVED_USER" ]]; then
    log "EXPIRED — login flow completed but session did not validate as authenticated"
    echo "AUTH_${PERSONA}=EXPIRED"
    exit 1
else
    log "FAIL — validated as an unexpected identity, not ${LOGIN_USER_ID}"
    echo "AUTH_${PERSONA}=FAIL"
    exit 1
fi
