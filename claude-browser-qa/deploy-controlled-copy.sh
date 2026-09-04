#!/usr/bin/env bash
# deploy-controlled-copy.sh --target <dir> [--apply]
#
# OPEN-125 / Issue #87 -- deploys a DETERMINISTIC, ghost-free controlled runtime copy of
# this source-maître checkout to --target, then installs the exact pinned Node
# dependency (npm ci, never `npm install`, so the lockfile is authoritative). Consistent
# with the repository README principle: "le serveur Frappe peut exécuter une copie
# contrôlée, mais ne doit pas être la source maître."
#
# Uses `rsync -a --delete` (mirror semantics) rather than `cp -r` into a possibly
# pre-existing directory -- a stale file left over from a previous deploy is a real
# correctness risk (e.g. an old refresh-persona.sh still lying around and picked up by a
# systemd unit pointed at the directory instead of at the exact file it should use).
#
# Without --apply: prints exactly what would be synced (rsync --dry-run) and exits 0.
# node_modules/ under the source tree is deliberately excluded from the sync -- it is
# installed fresh in the TARGET via `npm ci`, from the versioned lockfile, never copied.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"

apply=0
target=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) apply=1; shift ;;
        --target) target="$2"; shift 2 ;;
        *) echo "deploy-controlled-copy: unknown argument $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$target" ]]; then
    echo "deploy-controlled-copy: --target is required" >&2
    exit 2
fi

RSYNC_ARGS=(
    -a
    --delete
    --exclude=".git/"
    --exclude="node_modules/"
    --exclude="__pycache__/"
    --exclude="*.pyc"
    --exclude="package-lock.json.tmp"
)

if [[ "$apply" -eq 0 ]]; then
    echo "deploy-controlled-copy: PLAN -- rsync --dry-run ${SOURCE_DIR}/ -> ${target}/"
    rsync "${RSYNC_ARGS[@]}" --dry-run --itemize-changes "${SOURCE_DIR}/" "${target}/"
    echo "deploy-controlled-copy: PLAN -- would then run 'npm ci' in ${target}/auth"
    exit 0
fi

mkdir -p "$target"
rsync "${RSYNC_ARGS[@]}" "${SOURCE_DIR}/" "${target}/"
echo "deploy-controlled-copy: mirrored ${SOURCE_DIR}/ -> ${target}/ (stale files removed, if any)"

(
    cd "${target}/auth"
    npm ci --omit=dev
)
echo "deploy-controlled-copy: npm ci complete in ${target}/auth (reproducible from package-lock.json, pinned playwright version)"
