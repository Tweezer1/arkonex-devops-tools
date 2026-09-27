#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Contrôle de démarrage de session (SessionStart) du harnais Claude Code — instance DEV.

Source versionnée : arkonex-devops-tools/claude-code-hooks/ (OPEN-161, Issue #168).
Copie déployée    : /home/frappe/frappe-bench/.claude/hooks/ (voir install.py).

Lecture seule. Deux vérifications :
  1. la copie principale de docs (qui porte CLAUDE.md, chargé par chaque session) est sur
     main, sans modification suivie, et égale à main sur GitHub (git ls-remote : aucune
     écriture locale, pas même de fetch) ;
  2. les garde-fous configurés dans .claude/settings.json existent, sont exécutables et
     refusent réellement un cas connu, appelés exactement comme Claude Code les appelle.
     C'est ce contrôle qui aurait révélé la panne silencieuse constatée par OPEN-161.

Sortie : un message visible par la personne (systemMessage) et un contexte pour l'agent
(additionalContext) en cas de problème ; une ligne de contexte positive sinon.
"""

import json
import os
import shlex
import subprocess
import sys

BENCH_ROOT = os.environ.get("ARKONEX_BENCH_ROOT", "/home/frappe/frappe-bench")
DOCS_DIR = os.environ.get("ARKONEX_DOCS_DIR", os.path.join(BENCH_ROOT, "docs"))
SETTINGS = os.environ.get("ARKONEX_CLAUDE_SETTINGS",
                          os.path.join(BENCH_ROOT, ".claude", "settings.json"))

SELF_TESTS = [
    # (matcher attendu, charge d'essai, décision attendue)
    ("Bash", {"tool_name": "Bash", "cwd": "/tmp",
              "tool_input": {"command": "mysql -e 'select 1'"}}, "deny"),
    ("Write", {"tool_name": "Write", "cwd": "/tmp",
               "tool_input": {"file_path": "/x/sites/essai/site_config.json"}}, "deny"),
]


def git(*args, timeout=5):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "-C", DOCS_DIR] + list(args), capture_output=True,
                           text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def check_docs():
    problems, notes = [], []
    if not os.path.isdir(os.path.join(DOCS_DIR, ".git")) and \
            not os.path.isfile(os.path.join(DOCS_DIR, ".git")):
        return ["copie principale de docs introuvable ({})".format(DOCS_DIR)], notes
    _, branch = git("symbolic-ref", "--short", "-q", "HEAD")
    _, head = git("rev-parse", "HEAD")
    if branch != "main":
        problems.append("la copie principale de docs est sur « {} », pas sur main : les "
                        "sessions chargent le CLAUDE.md de cette branche"
                        .format(branch or "HEAD détachée"))
    _, dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        problems.append("la copie principale de docs contient des modifications non "
                        "commitées ({} fichier(s))".format(len(dirty.splitlines())))
    rc, remote = git("ls-remote", "origin", "refs/heads/main", timeout=10)
    remote_sha = remote.split()[0] if rc == 0 and remote else None
    if remote_sha is None:
        notes.append("comparaison avec main sur GitHub impossible (réseau ou accès)")
    elif branch == "main" and head != remote_sha:
        problems.append("la copie principale de docs n'est pas à jour avec main sur GitHub "
                        "(locale {} ≠ GitHub {})".format(head[:7], remote_sha[:7]))
    return problems, notes


def configured_hooks():
    with open(SETTINGS, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in (data.get("hooks") or {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            if hook.get("type") == "command":
                yield entry.get("matcher", ""), hook["command"]


def check_guards():
    problems = []
    try:
        hooks = list(configured_hooks())
    except (OSError, ValueError) as exc:
        return ["réglages illisibles ({} : {})".format(SETTINGS, type(exc).__name__)]
    for matcher, payload, expected in SELF_TESTS:
        commands = [c for m, c in hooks if matcher in m.split("|")]
        if not commands:
            problems.append("aucun garde-fou configuré pour l'outil {}".format(matcher))
            continue
        for command in commands:
            path = os.path.expanduser(shlex.split(command)[0])
            if not os.path.isfile(path):
                problems.append("garde-fou introuvable : {}".format(path))
                continue
            if not os.access(path, os.X_OK):
                problems.append("garde-fou non exécutable : {}".format(path))
                continue
            try:
                r = subprocess.run(command, shell=True, executable="/bin/bash",
                                   input=json.dumps(payload), capture_output=True, text=True,
                                   timeout=15)
                decision = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] \
                    if r.stdout.strip() else None
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                decision = "erreur"
            if decision != expected:
                problems.append("garde-fou {} : essai de refus non concluant (réponse : {})"
                                .format(os.path.basename(path), decision or "aucune"))
    return problems


def main():
    try:
        guard_problems = check_guards()
        docs_problems, notes = check_docs()
    except Exception as exc:  # le contrôle ne doit jamais empêcher une session de démarrer
        guard_problems, docs_problems, notes = [], [], []
        guard_problems.append("contrôle de démarrage en erreur interne ({})"
                              .format(type(exc).__name__))
    lines = []
    if guard_problems:
        lines.append("GARDE-FOUS INACTIFS OU DÉFAILLANTS : " + " ; ".join(guard_problems)
                     + ". Réinstaller : /usr/bin/python3 /home/frappe/frappe-bench/"
                       "arkonex-devops-tools/claude-code-hooks/install.py --apply")
    if docs_problems:
        lines.append("COPIE DOCS : " + " ; ".join(docs_problems) + ". Mettre à jour quand "
                     "aucune session n'en dépend : git -C {0} checkout main && git -C {0} "
                     "pull --ff-only".format(DOCS_DIR))
    if lines:
        text = "Contrôle de démarrage (OPEN-161) — " + " | ".join(lines)
        if notes:
            text += " (" + " ; ".join(notes) + ")"
        out = {"systemMessage": text,
               "hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": text}}
    else:
        text = ("Contrôle de démarrage (OPEN-161) : garde-fous actifs (essais de refus "
                "concluants) ; copie principale de docs sur main"
                + (", à jour avec GitHub." if not notes else " (" + " ; ".join(notes) + ")."))
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
