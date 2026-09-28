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

Rappel de suivi GitHub (OPEN-169, Issue #189), indépendant des deux vérifications : PR
ouvertes, et Issues open-lot qu'une PR fusionnée référence (Refs/Fixes/Closes) après leur
dernière mise à jour. Une seule requête GraphQL en lecture (gh), durée bornée ; en cas
d'échec, une ligne « indisponible ». Contexte pour l'agent seulement, jamais systemMessage
(choix du propriétaire, 2026-09-28) : l'agent le signale au propriétaire.
"""

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

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


OWNER = "Tweezer1"
DOCS_REPO = "arkonex-ops-docs"
FOLLOWUP = os.environ.get("ARKONEX_FOLLOWUP", "on")
FOLLOWUP_DAYS = 30
FOLLOWUP_PAGES = 3
FOLLOWUP_MAX_ITEMS = 8
# Les dépôts de code n'ont aucune Issue propre (vérifié le 2026-09-28) : un « Refs #N »
# y désigne une Issue d'arkonex-ops-docs. Les simples liens et les titres sont des
# mentions, pas des références : ils ne sont pas retenus.
REF = re.compile(r"\b(?:refs|fixes|closes)\s+(?:" + OWNER + "/" + DOCS_REPO + r")?#(\d+)\b",
                 re.IGNORECASE)
FOLLOWUP_QUERY = """
query($open: String!, $merged: String!, $after: String) {
  open: search(query: $open, type: ISSUE, first: 50) {
    nodes { ... on PullRequest { number isDraft createdAt repository { name } } }
  }
  merged: search(query: $merged, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number body mergedAt repository { name } } }
  }
  lots: repository(owner: "%s", name: "%s") {
    issues(states: OPEN, labels: ["open-lot"], first: 100) {
      nodes { number updatedAt }
    }
  }
}
""" % (OWNER, DOCS_REPO)


def gh_binary():
    return (os.environ.get("ARKONEX_GH") or shutil.which("gh")
            or os.path.expanduser("~/.local/bin/gh"))


def gh_graphql(variables, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    cmd = [gh_binary(), "api", "graphql", "-f", "query=" + FOLLOWUP_QUERY]
    for key, value in variables.items():
        cmd += ["-f", "{}={}".format(key, value)]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining, env=env)
    except subprocess.TimeoutExpired:
        raise TimeoutError
    if r.returncode != 0:
        raise RuntimeError("gh a échoué, code {}".format(r.returncode))
    return json.loads(r.stdout)["data"]


def followup_items(now):
    """Lecture seule. Renvoie (PR ouvertes, {Issue: (PR fusionnée, date de fusion)})."""
    deadline = time.monotonic() + float(os.environ.get("ARKONEX_FOLLOWUP_TIMEOUT", "8"))
    since = (now - datetime.timedelta(days=FOLLOWUP_DAYS)).strftime("%Y-%m-%d")
    variables = {"open": "user:{} is:pr is:open".format(OWNER),
                 "merged": "user:{} is:pr is:merged merged:>={}".format(OWNER, since)}
    data = gh_graphql(variables, deadline)
    open_prs = [p for p in data["open"]["nodes"] if p.get("number")]
    merged = list(data["merged"]["nodes"])
    page = data["merged"]["pageInfo"]
    for _ in range(FOLLOWUP_PAGES - 1):
        if not page.get("hasNextPage"):
            break
        more = gh_graphql(dict(variables, after=page["endCursor"]), deadline)["merged"]
        merged += more["nodes"]
        page = more["pageInfo"]
    # Dernière mise à jour du dossier (commentaire, texte, étiquette) : une fusion seule ne
    # la déplace pas (vérifié sur #86 : fusion le 2026-09-04 21:31, mise à jour 21:29).
    last_update = {lot["number"]: lot["updatedAt"] for lot in data["lots"]["issues"]["nodes"]}
    stale = {}
    for pr in merged:
        if not pr.get("number"):
            continue
        for number in {int(n) for n in REF.findall(pr.get("body") or "")}:
            if number in last_update and pr["mergedAt"] > last_update[number]:
                if number not in stale or pr["mergedAt"] > stale[number][1]:
                    stale[number] = ("{}#{}".format(pr["repository"]["name"], pr["number"]),
                                     pr["mergedAt"])
    return open_prs, stale


def followup_text():
    if FOLLOWUP == "off":
        return ""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        open_prs, stale = followup_items(now)
    except TimeoutError:
        return "Suivi GitHub (OPEN-169) indisponible (délai dépassé)."
    except Exception as exc:  # jamais bloquant
        return "Suivi GitHub (OPEN-169) indisponible ({}).".format(
            str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
    if not open_prs and not stale:
        return "Suivi GitHub (OPEN-169) : aucune PR ouverte ni dossier à mettre à jour."

    def cap(items):
        extra = len(items) - FOLLOWUP_MAX_ITEMS
        return items[:FOLLOWUP_MAX_ITEMS] + (["+{}".format(extra)] if extra > 0 else [])

    parts = []
    if open_prs:
        prs = []
        for pr in sorted(open_prs, key=lambda p: p["createdAt"]):
            created = datetime.datetime.strptime(pr["createdAt"], "%Y-%m-%dT%H:%M:%SZ")
            age = (now.replace(tzinfo=None) - created).days
            prs.append("{}#{} ({} j{})".format(pr["repository"]["name"], pr["number"], age,
                                               ", brouillon" if pr.get("isDraft") else ""))
        parts.append("{} PR ouverte(s) : {}".format(len(prs), ", ".join(cap(prs))))
    if stale:
        lots = ["#{} ({} fusionnée le {})".format(n, pr, merged_at[:10])
                for n, (pr, merged_at) in sorted(stale.items())]
        parts.append("{} dossier(s) à mettre à jour, fusion postérieure à leur dernière "
                     "mise à jour : {}".format(len(lots), ", ".join(cap(lots))))
    return ("Suivi GitHub (OPEN-169) : " + " ; ".join(parts) + ". À signaler au "
            "propriétaire en une ligne au début de la conversation ; mettre à jour les "
            "dossiers concernés (CLAUDE.md, « Fusion accompagnée », point 4).")


def main():
    try:
        guard_problems = check_guards()
        docs_problems, notes = check_docs()
    except Exception as exc:  # le contrôle ne doit jamais empêcher une session de démarrer
        guard_problems, docs_problems, notes = [], [], []
        guard_problems.append("contrôle de démarrage en erreur interne ({})"
                              .format(type(exc).__name__))
    try:
        followup = followup_text()
    except Exception as exc:  # indépendant : n'affecte jamais les deux autres contrôles
        followup = "Suivi GitHub (OPEN-169) indisponible ({}).".format(type(exc).__name__)
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
                                      "additionalContext": (text + " " + followup).strip()}}
    else:
        text = ("Contrôle de démarrage (OPEN-161) : garde-fous actifs (essais de refus "
                "concluants) ; copie principale de docs sur main"
                + (", à jour avec GitHub." if not notes else " (" + " ; ".join(notes) + ")."))
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": (text + " " + followup).strip()}}
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
