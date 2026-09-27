#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests du contrôle de démarrage (session_start.py) et de l'installation (install.py),
sur un bench fictif et un dépôt docs fictif (dépôt nu local servant de « GitHub »)."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = "/usr/bin/python3"

LEGACY_SETTINGS = {
    "permissions": {"defaultMode": "acceptEdits"},
    "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "~/.claude/hooks/block-forbidden-commands.sh"}]},
        {"matcher": "Write|Edit", "hooks": [
            {"type": "command", "command": "~/.claude/hooks/block-forbidden-paths.sh"}]}]},
}


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(args), cwd=cwd,
                   check=True, capture_output=True)


def tree_state(directory):
    state = {}
    for dirpath, _, files in os.walk(directory):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                state[os.path.relpath(p, directory)] = (hashlib.sha256(fh.read()).hexdigest(),
                                                        os.stat(p).st_mode & 0o777)
    return state


class Bench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="open161-")
        self.bench = os.path.join(self.tmp, "bench")
        self.claude = os.path.join(self.bench, ".claude")
        os.makedirs(os.path.join(self.claude, "hooks"))
        with open(os.path.join(self.claude, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(LEGACY_SETTINGS, fh, indent=2)
        for name, mode in (("block-forbidden-commands.sh", 0o775),
                           ("block-forbidden-paths.sh", 0o664)):
            p = os.path.join(self.claude, "hooks", name)
            with open(p, "w") as fh:
                fh.write("#!/bin/bash\nexit 0\n")
            os.chmod(p, mode)
        # « GitHub » : dépôt nu ; docs : clone sur main
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        self.docs = os.path.join(self.bench, "docs")
        subprocess.run(["git", "clone", "-q", self.origin, self.docs], check=True,
                       capture_output=True)
        git("switch", "-q", "-c", "main", cwd=self.docs)
        with open(os.path.join(self.docs, "CLAUDE.md"), "w") as fh:
            fh.write("v1\n")
        git("add", "CLAUDE.md", cwd=self.docs)
        git("commit", "-q", "-m", "v1", cwd=self.docs)
        git("push", "-q", "origin", "main", cwd=self.docs)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def install(self, *extra):
        return subprocess.run([PY, os.path.join(ROOT, "install.py"), "--bench-root", self.bench]
                              + list(extra), capture_output=True, text=True, timeout=60)

    def session_start(self):
        env = dict(os.environ, HOME=self.tmp, ARKONEX_BENCH_ROOT=self.bench,
                   ARKONEX_DOCS_DIR=self.docs,
                   ARKONEX_CLAUDE_SETTINGS=os.path.join(self.claude, "settings.json"))
        r = subprocess.run([self.claude + "/hooks/session_start.py"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class Install(Bench):
    def test_dry_run_writes_nothing(self):
        before = tree_state(self.claude)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Aucune écriture", r.stdout)
        self.assertEqual(tree_state(self.claude), before)

    def test_apply_then_rollback_is_byte_identical(self):
        before = tree_state(self.claude)
        r = self.install("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Contrôle après installation : PASS", r.stdout)
        with open(os.path.join(self.claude, "settings.json"), encoding="utf-8") as fh:
            settings = json.load(fh)
        self.assertEqual(settings["permissions"], {"defaultMode": "acceptEdits"})
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(commands, [self.claude + "/hooks/guard_bash.py",
                                    self.claude + "/hooks/guard_paths.py"])
        for name in ("guard_bash.py", "guard_paths.py", "session_start.py"):
            self.assertTrue(os.access(os.path.join(self.claude, "hooks", name), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "hooks",
                                                     "block-forbidden-commands.sh")))
        backups = os.listdir(os.path.join(self.claude, "hooks-backup"))
        self.assertEqual(len(backups), 1)
        backup = os.path.join(self.claude, "hooks-backup", backups[0])
        r = self.install("--rollback", backup, "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = {k: v for k, v in tree_state(self.claude).items()
                 if not k.startswith("hooks-backup/")}
        self.assertEqual(after, before)

    def test_invalid_settings_stops_without_writing(self):
        with open(os.path.join(self.claude, "settings.json"), "w") as fh:
            fh.write("{ pas du json")
        before = tree_state(self.claude)
        r = self.install("--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ARRÊT", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(tree_state(self.claude), before)

    def test_post_install_check_detects_a_broken_guard(self):
        self.assertEqual(self.install("--apply").returncode, 0)
        os.chmod(os.path.join(self.claude, "hooks", "guard_paths.py"), 0o644)
        out = self.session_start()
        self.assertIn("GARDE-FOUS INACTIFS", out["systemMessage"])
        self.assertIn("non exécutable", out["systemMessage"])


class SessionStart(Bench):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install("--apply").returncode, 0)

    def test_all_good_is_quiet_for_the_person(self):
        out = self.session_start()
        self.assertNotIn("systemMessage", out)
        self.assertIn("garde-fous actifs", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("à jour avec GitHub", out["hookSpecificOutput"]["additionalContext"])

    def test_docs_on_a_lot_branch(self):
        git("switch", "-q", "-c", "lot/x", cwd=self.docs)
        self.assertIn("pas sur main", self.session_start()["systemMessage"])

    def test_docs_behind_github(self):
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True,
                       capture_output=True)
        with open(os.path.join(other, "CLAUDE.md"), "w") as fh:
            fh.write("v2\n")
        git("commit", "-q", "-am", "v2", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        self.assertIn("pas à jour avec main", self.session_start()["systemMessage"])

    def test_docs_with_uncommitted_tracked_change(self):
        with open(os.path.join(self.docs, "CLAUDE.md"), "w") as fh:
            fh.write("modifié\n")
        self.assertIn("non commitées", self.session_start()["systemMessage"])

    def test_guard_that_no_longer_refuses_is_detected(self):
        """Un garde-fou présent et exécutable mais qui laisse tout passer."""
        p = os.path.join(self.claude, "hooks", "guard_bash.py")
        with open(p, "w") as fh:
            fh.write("#!/bin/bash\ncat >/dev/null\nexit 0\n")
        msg = self.session_start()["systemMessage"]
        self.assertIn("essai de refus non concluant", msg)

    def test_legacy_wrong_path_is_detected(self):
        """Reproduit la panne constatée : chemin ~/.claude/hooks inexistant."""
        with open(os.path.join(self.claude, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(LEGACY_SETTINGS, fh)
        msg = self.session_start()["systemMessage"]
        self.assertIn("GARDE-FOUS INACTIFS", msg)
        self.assertIn("introuvable", msg)


if __name__ == "__main__":
    unittest.main()
