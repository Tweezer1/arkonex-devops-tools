#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Rejoue les commandes Bash réellement exécutées par les sessions Claude Code contre
guard_bash.evaluate() et les chemins Write/Edit contre guard_paths.evaluate().

Lecture seule : lit les transcriptions locales (JSONL) et n'écrit que le rapport détaillé
demandé par --out (à placer hors du dépôt : les commandes peuvent contenir des chemins ou
des données de travail). Rien n'est exécuté : seules les décisions du garde sont calculées.

Usage :
  replay_corpus.py [--transcripts DIR] [--exclude SESSION_ID ...] [--out FICHIER.json]
"""

import argparse
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import guard_bash  # noqa: E402
import guard_paths  # noqa: E402

DEFAULT_DIR = os.path.expanduser("~/.claude/projects/-home-frappe-frappe-bench")


def iter_tool_uses(directory, exclude):
    for path in glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True):
        if any(x in path for x in exclude):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "tool_use":
                        yield entry, block


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", default=DEFAULT_DIR)
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--out")
    opts = ap.parse_args()

    commands = collections.OrderedDict()   # commande -> (occurrences, cwd, horodatage)
    paths = collections.Counter()
    for entry, block in iter_tool_uses(opts.transcripts, opts.exclude):
        data = block.get("input") or {}
        if block.get("name") == "Bash" and data.get("command"):
            c = data["command"]
            n, cwd, ts = commands.get(c, (0, entry.get("cwd"), entry.get("timestamp")))
            commands[c] = (n + 1, cwd, ts)
        elif block.get("name") in ("Write", "Edit", "NotebookEdit") and data.get("file_path"):
            paths[data["file_path"]] += 1

    by_rule = collections.defaultdict(list)
    for c, (n, cwd, ts) in commands.items():
        verdict = guard_bash.evaluate(c, cwd)
        if verdict:
            by_rule[(verdict[0], verdict[1])].append({"command": c, "occurrences": n,
                                                       "cwd": cwd, "first_seen": ts,
                                                       "reason": verdict[2]})
    path_denied = [p for p in paths if guard_paths.evaluate(p)]

    total = sum(n for n, _, _ in commands.values())
    print("Commandes Bash uniques : {} (occurrences : {})".format(len(commands), total))
    for (decision, rule), items in sorted(by_rule.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        print("  {:<5} {:<14} {:>5} commandes uniques".format(decision, rule, len(items)))
    flagged = sum(len(v) for v in by_rule.values())
    print("  sans signalement : {} commandes uniques".format(len(commands) - flagged))
    print("Chemins Write/Edit uniques : {} ; refusés : {}".format(len(paths), len(path_denied)))
    if opts.out:
        with open(opts.out, "w", encoding="utf-8") as fh:
            json.dump({"by_rule": {"{} {}".format(*k): v for k, v in by_rule.items()},
                       "paths_denied": path_denied}, fh, ensure_ascii=False, indent=1)
        print("Détail : {}".format(opts.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
