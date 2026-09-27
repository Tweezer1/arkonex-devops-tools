#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Garde-fou PreToolUse (outils Write, Edit, NotebookEdit) du harnais Claude Code —
instance DEV Arkonex.

Source versionnée : arkonex-devops-tools/claude-code-hooks/ (OPEN-161, Issue #168).
Copie déployée    : /home/frappe/frappe-bench/.claude/hooks/ (voir install.py).

Refuse l'écriture dans les fichiers qui portent des secrets (CLAUDE.md, « zéro secret ») :
  - sites/<site>/site_config.json ;
  - .env et .env.* .
Reprend à l'identique les deux règles du script d'origine (block-forbidden-paths.sh).
Ne répond jamais "allow" ; une erreur interne demande une validation humaine.
"""

import json
import os
import re
import sys

RULES = [
    (re.compile(r"/sites/[^/]+/site_config\.json$"),
     "Écriture interdite dans site_config.json (peut contenir des secrets)."),
    (re.compile(r"(?:^|/)\.env(?:\.[^/]*)?$"),
     "Écriture interdite dans un fichier .env."),
]


def evaluate(path):
    """Renvoie None ou (décision, règle, message)."""
    if not path:
        return None
    normalized = os.path.normpath(path)
    for pattern, message in RULES:
        if pattern.search(normalized):
            return ("deny", "R-SECRETS", message)
    return None


def main():
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") or {}
        verdict = evaluate(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    except Exception as exc:  # jamais silencieux, jamais de blocage total
        verdict = ("ask", "R-ERREUR", "Garde-fou en erreur interne ({}) : validation humaine "
                                      "par précaution.".format(type(exc).__name__))
    if verdict:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": verdict[0],
                                          "permissionDecisionReason":
                                              "[OPEN-161 {}] {}".format(verdict[1], verdict[2])}},
                  sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
