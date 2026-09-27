#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Installe (ou retire) les garde-fous OPEN-161 dans la configuration locale de Claude Code
du bench. Sans --apply, n'écrit rien : affiche seulement ce qui serait fait.

  install.py [--bench-root DIR]                      plan d'installation
  install.py [--bench-root DIR] --apply              installation
  install.py [--bench-root DIR] --rollback SAUVEGARDE [--apply]
                                                     retour à l'état sauvegardé

Installation :
  1. sauvegarde de .claude/settings.json et de tout .claude/hooks/ dans
     .claude/hooks-backup/<horodatage UTC>/ (avec un manifeste sha256) ;
  2. copie de guard_bash.py, guard_paths.py et session_start.py dans .claude/hooks/
     (mode 0755), écriture atomique ;
  3. les anciens scripts block-forbidden-*.sh passent dans la sauvegarde ;
  4. remplacement de la seule section "hooks" de .claude/settings.json par
     settings.hooks.json (chemins absolus) ; les autres réglages sont conservés ;
  5. contrôle après installation : chaque garde-fou configuré existe, est exécutable et
     refuse réellement un cas connu (même contrôle que session_start.py).

Effet immédiat : Claude Code relit ses réglages en cours de session ; les sessions
ouvertes appliquent les nouveaux garde-fous dès l'installation.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ("guard_bash.py", "guard_paths.py", "session_start.py")
LEGACY = ("block-forbidden-commands.sh", "block-forbidden-paths.sh")


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def atomic_write(path, data, mode=None):
    tmp = path + ".tmp-open161"
    with open(tmp, "wb") as fh:
        fh.write(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def render_hooks(bench_root):
    with open(os.path.join(HERE, "settings.hooks.json"), encoding="utf-8") as fh:
        text = fh.read().replace("{{BENCH_ROOT}}", bench_root.rstrip("/"))
    return json.loads(text)["hooks"]


def load_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def self_test(bench_root):
    """Même contrôle que session_start.py, sur les réglages qui viennent d'être écrits."""
    sys.path.insert(0, HERE)
    import session_start
    session_start.SETTINGS = os.path.join(bench_root, ".claude", "settings.json")
    return session_start.check_guards()


def plan_install(bench_root):
    claude = os.path.join(bench_root, ".claude")
    hooks_dir = os.path.join(claude, "hooks")
    settings_path = os.path.join(claude, "settings.json")
    actions = []
    for name in SCRIPTS:
        dst = os.path.join(hooks_dir, name)
        state = "nouveau" if not os.path.exists(dst) else (
            "identique" if sha256(dst) == sha256(os.path.join(HERE, name)) else "modifié")
        actions.append(("copie", name, state))
    for name in LEGACY:
        if os.path.exists(os.path.join(hooks_dir, name)):
            actions.append(("retrait vers la sauvegarde", name, "ancien script"))
    before = load_settings(settings_path).get("hooks")
    after = render_hooks(bench_root)
    return actions, before, after


def install(bench_root, apply):
    claude = os.path.join(bench_root, ".claude")
    hooks_dir = os.path.join(claude, "hooks")
    settings_path = os.path.join(claude, "settings.json")
    actions, before, after = plan_install(bench_root)
    print("Plan d'installation dans {} :".format(claude))
    for a in actions:
        print("  - {} : {} ({})".format(*a))
    print("  - section \"hooks\" de settings.json :")
    print("      avant : " + json.dumps(before, ensure_ascii=False))
    print("      après : " + json.dumps(after, ensure_ascii=False))
    if not apply:
        print("Aucune écriture (ajouter --apply pour installer).")
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = os.path.join(claude, "hooks-backup", stamp)
    os.makedirs(os.path.join(backup, "hooks"))
    manifest = {"created_utc": stamp, "settings": None, "hooks": {}}
    if os.path.exists(settings_path):
        shutil.copy2(settings_path, os.path.join(backup, "settings.json"))
        manifest["settings"] = sha256(settings_path)
    os.makedirs(hooks_dir, exist_ok=True)
    for name in sorted(os.listdir(hooks_dir)):
        src = os.path.join(hooks_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(backup, "hooks", name))
            manifest["hooks"][name] = {"sha256": sha256(src),
                                       "mode": oct(os.stat(src).st_mode & 0o777)}
    with open(os.path.join(backup, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print("Sauvegarde : {}".format(backup))

    for name in SCRIPTS:
        with open(os.path.join(HERE, name), "rb") as fh:
            atomic_write(os.path.join(hooks_dir, name), fh.read(), 0o755)
    for name in LEGACY:
        path = os.path.join(hooks_dir, name)
        if os.path.exists(path):
            os.remove(path)          # déjà copié dans la sauvegarde
    settings = load_settings(settings_path)
    settings["hooks"] = after
    atomic_write(settings_path,
                 (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    problems = self_test(bench_root)
    if problems:
        print("CONTRÔLE APRÈS INSTALLATION : ÉCHEC — " + " ; ".join(problems))
        print("Retour arrière : install.py --bench-root {} --rollback {} --apply"
              .format(bench_root, backup))
        return 1
    print("Contrôle après installation : PASS (garde-fous présents, exécutables, refus "
          "réels constatés).")
    print("Retour arrière si besoin : install.py --bench-root {} --rollback {} --apply"
          .format(bench_root, backup))
    return 0


def rollback(bench_root, backup, apply):
    claude = os.path.join(bench_root, ".claude")
    hooks_dir = os.path.join(claude, "hooks")
    settings_path = os.path.join(claude, "settings.json")
    with open(os.path.join(backup, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    current = sorted(os.listdir(hooks_dir)) if os.path.isdir(hooks_dir) else []
    to_remove = [n for n in current if n not in manifest["hooks"]]
    print("Retour arrière depuis {} :".format(backup))
    print("  - settings.json : {}".format("restauré" if manifest["settings"] else "supprimé"))
    print("  - hooks restaurés : {}".format(", ".join(sorted(manifest["hooks"])) or "aucun"))
    print("  - hooks retirés : {}".format(", ".join(to_remove) or "aucun"))
    if not apply:
        print("Aucune écriture (ajouter --apply pour restaurer).")
        return 0
    for name, meta in manifest["hooks"].items():
        with open(os.path.join(backup, "hooks", name), "rb") as fh:
            atomic_write(os.path.join(hooks_dir, name), fh.read(), int(meta["mode"], 8))
        if sha256(os.path.join(hooks_dir, name)) != meta["sha256"]:
            print("ÉCHEC : empreinte différente après restauration de {}".format(name))
            return 1
    for name in to_remove:
        os.remove(os.path.join(hooks_dir, name))
    if manifest["settings"]:
        with open(os.path.join(backup, "settings.json"), "rb") as fh:
            atomic_write(settings_path, fh.read())
        if sha256(settings_path) != manifest["settings"]:
            print("ÉCHEC : empreinte différente après restauration de settings.json")
            return 1
    elif os.path.exists(settings_path):
        os.remove(settings_path)
    print("Retour arrière : PASS (empreintes identiques à la sauvegarde).")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench-root", default="/home/frappe/frappe-bench")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="SAUVEGARDE")
    opts = ap.parse_args()
    root = os.path.abspath(opts.bench_root)
    if opts.rollback:
        return rollback(root, os.path.abspath(opts.rollback), opts.apply)
    return install(root, opts.apply)


if __name__ == "__main__":
    sys.exit(main())
