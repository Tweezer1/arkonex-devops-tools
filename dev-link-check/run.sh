#!/usr/bin/env bash
# Lance le contrôle des liens cassés (dev-link-check, OPEN-164, Issue #173) via
# `bench console`, en lecture seule (voir link_check.py).
#
# Usage : run.sh <site> [baseline] [report]
#   site     : site Frappe cible (obligatoire), ex. deverp.arkonex.ca
#   baseline : chemin de la référence à comparer (défaut : dev-link-check/baseline.json)
#   report   : chemin où écrire le rapport JSON (optionnel)
#
# Code de sortie : 0 = PASS, 1 = FAIL, 2 = erreur ou statut introuvable (les 40
# dernières lignes de sortie de `bench console` sont alors affichées pour diagnostic).
#
# Propagation explicite des codes de retour, jamais `set -e` (règle du workspace,
# voir CLAUDE.md § Construction des assets).

set -u

usage() {
    echo "Usage: $0 <site> [baseline] [report]" >&2
}

SITE="${1:-}"
if [ -z "$SITE" ]; then
    usage
    exit 2
fi
BASELINE="${2:-}"
REPORT="${3:-}"

DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"

export LINK_CHECK_DIR="$DIR"
if [ -n "$BASELINE" ]; then
    export LINK_CHECK_BASELINE="$BASELINE"
fi
if [ -n "$REPORT" ]; then
    export LINK_CHECK_REPORT="$REPORT"
fi

cd "$BENCH_DIR" || exit 2

out=$(echo "exec(open('$DIR/link_check.py').read(), globals())" | bench --site "$SITE" console 2>&1)
rc=$?

# Retire les codes couleur ANSI éventuels de la sortie de la console.
clean=$(printf '%s\n' "$out" | sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g')

# Ne garde que les lignes utiles. Pas d'ancre ^ : IPython peut préfixer la toute
# première ligne (ex. "In [1]: LINK_CHECK_STATUS=PASS").
lines=$(printf '%s\n' "$clean" | grep -E 'LINK_CHECK_')

status_token=$(printf '%s\n' "$lines" | grep -oE 'LINK_CHECK_STATUS=(PASS|FAIL|ERROR)' | head -1)
status=${status_token#LINK_CHECK_STATUS=}

printf '%s\n' "$lines"

if [ "$status" = "PASS" ]; then
    exit 0
elif [ "$status" = "FAIL" ]; then
    exit 1
else
    echo "--- statut introuvable ou ERROR (bench console rc=$rc) : 40 dernières lignes de sortie ---" >&2
    printf '%s\n' "$clean" | tail -40 >&2
    exit 2
fi
