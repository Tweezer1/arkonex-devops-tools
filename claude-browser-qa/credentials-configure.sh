#!/usr/bin/env bash
# credentials-configure.sh <persona> [--verify]
#
# OPEN-125 / Issue #87 -- the ONE versioned, tested tool for provisioning a persona's
# credentials file. Replaces any ad-hoc shell loop typed directly into a terminal.
# Must be run as (or via `sudo -u browserqa-refresh`) the dedicated OS user -- the
# credentials directory is owned by that user, mode 0700, so this is enforced by the
# filesystem itself, not just convention.
#
# Contract (canary-prep correction, section 3A):
#   - password requested via a masked TTY prompt (`read -rs`), refuses to run
#     non-interactively against a real terminal;
#   - NEVER passed as a CLI argument (not visible via `ps`);
#   - NEVER echoed, NEVER logged, NEVER written to shell history (only the prompt
#     command itself, never the typed value, is ever recorded by a shell's history);
#   - written to the credentials file directly from the shell variable, file mode 0600
#     applied before any data is written (umask-independent);
#   - verified afterward WITHOUT reading back BROWSER_QA_PASSWORD -- only presence,
#     permissions, and that BROWSER_QA_USER_ID matches the manifest are checked.
#
# --verify alone (no password prompt) re-runs only the postcheck against an existing
# file -- useful for verify-browser-qa.sh or a human re-confirming state later.
#
# Test mode: set CREDENTIALS_CONFIGURE_TEST_MODE=1 to allow a non-TTY stdin read (a
# FAKE value piped in, e.g. "not-a-real-secret") so the file-handling mechanics
# (permissions, ownership, verify-without-reading-back) can be proven by an automated
# test without ever touching a real credential or requiring real TTY interaction.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSONAS_YAML="${SCRIPT_DIR}/personas.yaml"
CREDENTIALS_DIR="${BROWSER_QA_CREDENTIALS_DIR:-/etc/arkonex/browser-qa/credentials}"

PERSONA="${1:-}"
VERIFY_ONLY=0
for arg in "${@:2}"; do
    case "$arg" in
        --verify) VERIFY_ONLY=1 ;;
        *) echo "credentials-configure: unknown argument $arg" >&2; exit 2 ;;
    esac
done

if [[ -z "$PERSONA" ]]; then
    echo "usage: credentials-configure.sh <persona> [--verify]" >&2
    exit 2
fi

resolve_field() {
    python3 "${SCRIPT_DIR}/lib/resolve_persona_field.py" \
        --personas "$PERSONAS_YAML" --persona "$PERSONA" --field "$1"
}

LOGIN_USER_ID="$(resolve_field login_user_id)"
CREDENTIALS_FILE="$(resolve_field credentials_file)"
TARGET_PATH="${CREDENTIALS_DIR}/${CREDENTIALS_FILE}"

verify_result() {
    # Never reads/prints BROWSER_QA_PASSWORD -- only checks it is PRESENT and non-empty,
    # by line-matching the key name, never capturing or echoing its value.
    if [[ ! -f "$TARGET_PATH" ]]; then
        echo "credentials-configure[${PERSONA}]: VERIFY=FAIL (file does not exist: ${TARGET_PATH})"
        return 1
    fi

    local mode
    mode="$(stat -c '%a' "$TARGET_PATH")"
    if [[ "$mode" != "600" ]]; then
        echo "credentials-configure[${PERSONA}]: VERIFY=FAIL (mode ${mode}, expected 600)"
        return 1
    fi

    if ! grep -qF "BROWSER_QA_USER_ID=${LOGIN_USER_ID}" "$TARGET_PATH"; then
        echo "credentials-configure[${PERSONA}]: VERIFY=FAIL (BROWSER_QA_USER_ID does not match manifest login_user_id)"
        return 1
    fi

    # Presence-only check for the password line -- the value itself is never captured.
    if ! grep -qE '^BROWSER_QA_PASSWORD=.+' "$TARGET_PATH"; then
        echo "credentials-configure[${PERSONA}]: VERIFY=FAIL (BROWSER_QA_PASSWORD line missing or empty)"
        return 1
    fi

    echo "credentials-configure[${PERSONA}]: VERIFY=PASS (file present, mode 600, user_id matches manifest, password line present)"
}

if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    verify_result
    exit $?
fi

if [[ -t 0 ]]; then
    read -rs -p "Password for ${LOGIN_USER_ID} (persona: ${PERSONA}): " password
    echo
elif [[ "${CREDENTIALS_CONFIGURE_TEST_MODE:-0}" == "1" ]]; then
    IFS= read -r password
else
    echo "credentials-configure[${PERSONA}]: refusing to run -- stdin is not a TTY " \
        "(real password entry must be interactive; set " \
        "CREDENTIALS_CONFIGURE_TEST_MODE=1 only for automated tests with a fake value)" >&2
    exit 1
fi

if [[ -z "$password" ]]; then
    echo "credentials-configure[${PERSONA}]: refusing to write an empty password" >&2
    exit 1
fi

mkdir -p "$CREDENTIALS_DIR"
umask 077
tmp="$(mktemp "${CREDENTIALS_DIR}/.${CREDENTIALS_FILE}.XXXXXX")"
{
    printf 'BROWSER_QA_USER_ID=%s\n' "$LOGIN_USER_ID"
    printf 'BROWSER_QA_PASSWORD=%s\n' "$password"
} > "$tmp"
unset password
chmod 600 "$tmp"
mv -f "$tmp" "$TARGET_PATH"

echo "credentials-configure[${PERSONA}]: written (${TARGET_PATH}, mode 600) -- value never printed"
verify_result
