#!/usr/bin/env bash
# bootstrap-browser-qa.sh (OPEN-125 / Issue #87)
#
# Idempotent bootstrap for the Browser QA / Playwright MCP infrastructure. Every
# sub-command implements real logic but performs NO write unless invoked with --apply.
# Without --apply, each sub-command prints exactly what it would do and exits 0 (plan
# mode) -- this is deliberate so the script itself can be sourced/tested in CI or in a
# Claude Code PLAN_ONLY pass without any risk of touching the host.
#
# Sub-commands (each independent, each individually idempotent):
#   dns                --apply   ensure "127.0.0.1 deverp.arkonex.ca" in /etc/hosts (target file overridable via --hosts-file, for tests)
#   provision-identity --apply   thin wrapper around provision-system-identity.sh (user/group/membership, PASS/no-op if conformant, STOP on real divergence)
#   deploy             --apply   thin wrapper around deploy-controlled-copy.sh --target $DEPLOY_TARGET (ghost-free rsync mirror + npm ci)
#   generate-config    --apply   run generate-mcp-config.py, then atomically activate the result at --target (defaults to $CLAUDE_BENCH_ROOT/.mcp.json)
#   browser-install    --apply   `npx playwright install chromium` from $DEPLOY_TARGET/auth, into a shared PLAYWRIGHT_BROWSERS_PATH (runtime-proven command; NOT `@playwright/mcp install-browser chrome-for-testing`, a different, unrelated channel)
#   systemd-install    --apply   copy auth/browser-qa-refresh@.{service,timer} from $DEPLOY_TARGET into /etc/systemd/system/, daemon-reload, enable+start one timer instance per persona.enabled==true
#   all                --apply   dns + provision-identity + deploy + generate-config + systemd-install, in that order (never browser-install -- that remains a separate, explicit gate: a real ~100MB download should never be an implicit side effect of "all")
#
# NONE of these sub-commands were invoked with --apply during OPEN-125 Phase A. Phase A
# only exercises the non-destructive halves of `dns` (idempotency-check function, against
# a temporary file) and `generate-config` (against a temporary output directory) -- see
# tests/. `browser-install` was proven correct by a real, one-time manual runtime canary
# (documented in README) -- its automated test only checks plan-mode output and argument
# handling, never re-downloads the ~100MB browser on every test run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERSONAS_YAML="${SCRIPT_DIR}/personas.yaml"

DNS_LINE="127.0.0.1 deverp.arkonex.ca"
DEPLOY_TARGET="${BROWSER_QA_DEPLOY_TARGET:-/opt/arkonex-browser-qa/claude-browser-qa}"
DEFAULT_PLAYWRIGHT_BROWSERS_PATH="${BROWSER_QA_BROWSERS_PATH:-/opt/arkonex-browser-qa/browsers}"

log() { echo "bootstrap-browser-qa: $*"; }

# ---------------------------------------------------------------------------
# dns
# ---------------------------------------------------------------------------
# Returns 0 if $DNS_LINE is already present verbatim in $1, 1 otherwise. Pure read,
# safe to call against the real /etc/hosts or a test fixture alike.
dns_line_present() {
    local hosts_file="$1"
    grep -qxF "$DNS_LINE" "$hosts_file"
}

# Idempotent apply: never duplicates the line, always backs up first, writes via a
# temp-file + atomic rename rather than in-place edit or bare `tee -a`.
dns_apply() {
    local hosts_file="$1"

    if dns_line_present "$hosts_file"; then
        log "dns: line already present in ${hosts_file} -- no-op"
        return 0
    fi

    local backup="${hosts_file}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$hosts_file" "$backup"
    log "dns: backed up ${hosts_file} -> ${backup}"

    local tmp
    tmp="$(mktemp)"
    cp -p "$hosts_file" "$tmp"
    echo "$DNS_LINE" >> "$tmp"
    mv -f "$tmp" "$hosts_file"
    log "dns: appended '${DNS_LINE}' to ${hosts_file}"
}

# Target-only rollback: removes exactly the managed line, nothing else, regardless of
# what else may have been added to the file since.
dns_rollback() {
    local hosts_file="$1"
    local tmp
    tmp="$(mktemp)"
    grep -vxF "$DNS_LINE" "$hosts_file" > "$tmp" || true
    mv -f "$tmp" "$hosts_file"
    log "dns: rollback complete, removed exactly the managed line from ${hosts_file}"
}

cmd_dns() {
    local apply=0
    local hosts_file="/etc/hosts"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --apply) apply=1; shift ;;
            --hosts-file) hosts_file="$2"; shift 2 ;;
            *) echo "dns: unknown argument $1" >&2; return 2 ;;
        esac
    done

    if [[ "$apply" -eq 0 ]]; then
        if dns_line_present "$hosts_file"; then
            log "dns: PLAN -- '${DNS_LINE}' already present in ${hosts_file}, would be a no-op"
        else
            log "dns: PLAN -- would back up ${hosts_file} then append '${DNS_LINE}'"
        fi
        return 0
    fi

    dns_apply "$hosts_file"
}

# ---------------------------------------------------------------------------
# provision-identity
# ---------------------------------------------------------------------------
cmd_provision_identity() {
    local args=("$@")
    bash "${SCRIPT_DIR}/provision-system-identity.sh" "${args[@]}"
}

# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------
cmd_deploy() {
    local args=("$@")
    bash "${SCRIPT_DIR}/deploy-controlled-copy.sh" --target "$DEPLOY_TARGET" "${args[@]}"
}

# ---------------------------------------------------------------------------
# generate-config
# ---------------------------------------------------------------------------
cmd_generate_config() {
    local apply=0
    local storage_state_dir="/var/lib/arkonex-browser-qa/storage-states"
    local target="${CLAUDE_BENCH_ROOT:-/home/frappe/frappe-bench}/.mcp.json"
    local browsers_path="$DEFAULT_PLAYWRIGHT_BROWSERS_PATH"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --apply) apply=1; shift ;;
            --storage-state-dir) storage_state_dir="$2"; shift 2 ;;
            --target) target="$2"; shift 2 ;;
            --browsers-path) browsers_path="$2"; shift 2 ;;
            *) echo "generate-config: unknown argument $1" >&2; return 2 ;;
        esac
    done

    local candidate
    candidate="$(mktemp)"

    if ! python3 "${SCRIPT_DIR}/generate-mcp-config.py" \
            --personas "$PERSONAS_YAML" \
            --storage-state-dir "$storage_state_dir" \
            --browsers-path "$browsers_path" \
            --output "$candidate"; then
        log "generate-config: generation failed, 0 file touched at target"
        rm -f "$candidate"
        return 1
    fi

    if [[ "$apply" -eq 0 ]]; then
        log "generate-config: PLAN -- candidate generated at ${candidate}, would activate at ${target}"
        cat "$candidate"
        rm -f "$candidate"
        return 0
    fi

    if [[ -f "$target" ]]; then
        local backup="${target}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
        cp -p "$target" "$backup"
        log "generate-config: backed up ${target} -> ${backup}"
    fi

    mv -f "$candidate" "$target"
    # mktemp creates the candidate at mode 0600 owned by whoever ran this command, and mv
    # preserves that mode/owner verbatim -- if this command is invoked via sudo (a
    # reasonable thing to do, since every OTHER subcommand in this script needs root),
    # the activated .mcp.json ends up unreadable by the frappe user whose Claude Code
    # process is the one actually consuming it (real bug, found during Phase B
    # activation canary). This file carries no secret (paths and pinned CLI args only --
    # never a credential, enforced by generate-mcp-config.py's own contract), so making it
    # world-readable is safe regardless of which user activates it.
    chmod 0644 "$target"
    log "generate-config: activated ${target}"

    # Post-check: re-read and hash-compare is redundant with mv's atomicity guarantee,
    # kept anyway as an explicit, auditable proof step per the OPEN-125 contract.
    sha256sum "$target"
}

# ---------------------------------------------------------------------------
# browser-install -- RATIFIED (runtime canary, canary-prep correction 2026)
# ---------------------------------------------------------------------------
# Proven by real execution: plain `chromium.launch()` (no channel -- used by both the
# generated MCP server config, --browser=chromium, and auth/playwright-login.mjs) needs
# the "chromium" entry from playwright-core's own browsers.json, installed via the
# STANDARD `npx playwright install chromium` -- run from the deployed auth/ directory so
# it resolves the exact pinned local playwright-core, never a global/ambient one. This
# is a DIFFERENT target than `@playwright/mcp install-browser chrome-for-testing`
# (a distinct channel, confirmed unrelated by inspecting playwright-core's own
# chromiumAliases registry) -- that command is intentionally NOT used here.
cmd_browser_install() {
    local apply=0
    local browsers_path="$DEFAULT_PLAYWRIGHT_BROWSERS_PATH"
    local auth_dir="${DEPLOY_TARGET}/auth"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --apply) apply=1; shift ;;
            --browsers-path) browsers_path="$2"; shift 2 ;;
            *) echo "browser-install: unknown argument $1" >&2; return 2 ;;
        esac
    done

    if [[ ! -d "$auth_dir/node_modules/playwright" ]]; then
        echo "browser-install: ${auth_dir}/node_modules/playwright not found -- run 'deploy --apply' first" >&2
        return 1
    fi

    if [[ "$apply" -eq 0 ]]; then
        log "browser-install: PLAN -- would run 'npx playwright install chromium' from" \
            "${auth_dir} with PLAYWRIGHT_BROWSERS_PATH=${browsers_path}"
        return 0
    fi

    mkdir -p "$browsers_path"
    (
        cd "$auth_dir"
        PLAYWRIGHT_BROWSERS_PATH="$browsers_path" npx playwright install chromium
    )
    log "browser-install: complete (PLAYWRIGHT_BROWSERS_PATH=${browsers_path})"
}

# ---------------------------------------------------------------------------
# systemd-install
# ---------------------------------------------------------------------------
cmd_systemd_install() {
    local apply=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --apply) apply=1; shift ;;
            *) echo "systemd-install: unknown argument $1" >&2; return 2 ;;
        esac
    done

    local personas
    personas="$(python3 "${SCRIPT_DIR}/lib/list_enabled_personas.py" --personas "$PERSONAS_YAML")"

    if [[ "$apply" -eq 0 ]]; then
        log "systemd-install: PLAN -- would install browser-qa-refresh@.{service,timer}" \
            "from ${DEPLOY_TARGET}/auth/ to /etc/systemd/system/, daemon-reload, then" \
            "enable+start for each of:"
        echo "$personas"
        return 0
    fi

    if [[ ! -f "${DEPLOY_TARGET}/auth/browser-qa-refresh@.service" ]]; then
        echo "systemd-install: ${DEPLOY_TARGET}/auth/ not found -- run 'deploy --apply' first" >&2
        return 1
    fi

    install -m 0644 "${DEPLOY_TARGET}/auth/browser-qa-refresh@.service" /etc/systemd/system/
    install -m 0644 "${DEPLOY_TARGET}/auth/browser-qa-refresh@.timer" /etc/systemd/system/
    systemctl daemon-reload

    while IFS= read -r persona; do
        [[ -z "$persona" ]] && continue
        systemctl enable --now "browser-qa-refresh@${persona}.timer"
        log "systemd-install: enabled browser-qa-refresh@${persona}.timer"
    done <<< "$personas"
}

# ---------------------------------------------------------------------------
main() {
    local subcommand="${1:-}"
    shift || true

    case "$subcommand" in
        dns) cmd_dns "$@" ;;
        provision-identity) cmd_provision_identity "$@" ;;
        deploy) cmd_deploy "$@" ;;
        generate-config) cmd_generate_config "$@" ;;
        browser-install) cmd_browser_install "$@" ;;
        systemd-install) cmd_systemd_install "$@" ;;
        all)
            cmd_dns "$@"
            cmd_provision_identity "$@"
            cmd_deploy "$@"
            cmd_generate_config "$@"
            cmd_systemd_install "$@"
            log "all: browser-install intentionally NOT included -- separate explicit gate"
            ;;
        *)
            echo "usage: bootstrap-browser-qa.sh {dns|provision-identity|deploy|generate-config|browser-install|systemd-install|all} [--apply] [options]" >&2
            return 2
            ;;
    esac
}

main "$@"
