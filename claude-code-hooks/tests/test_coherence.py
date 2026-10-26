#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests du contrôle de cohérence « ERPNext d'abord » (OPEN-204, Issue #359).

Couvre : chaque famille déclenche sur un texte du code et pas sur un commentaire ni un
fichier d'essai ; la marque « arkonex-coherence: » ; la liste de référence (compte,
raison obligatoire) ; même verdict à chaque passage ; la règle R-COHERENCE du garde-fou
Bash avant `gh pr create`.

Lancer : /usr/bin/python3 -m unittest discover -s claude-code-hooks/tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import coherence_check  # noqa: E402
import guard_bash  # noqa: E402

PY_BAD = '''\
# La TPS et la TVQ viennent du modèle de la société (commentaire : jamais signalé)
RATE = 0.09975
FACTOR = 0.05
LABEL = "TVQ"
ACCOUNT = "Debtors - A"
COMPANY = "Arkonex"
CUR = 'CAD'
MAIL = "p.bruno@arkonex.ca"
OK_MAIL = "client@essai.invalid"
WANTED = "USD"  # arkonex-coherence: devise imposée par le contrat du fournisseur
MSG = f"{name} - A"
'''
JS_BAD = '''\
// currency: "CAD" dans un commentaire
/* "TPS" dans un bloc */
const c = {currency: "CAD"};
'''


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.base = os.path.join(self.root, "baseline.json")
        with open(self.base, "w") as fh:
            json.dump({"repos": {}}, fh)

    def tearDown(self):
        shutil.rmtree(self.root)

    def families(self, result):
        return sorted((c["family"], c["value"]) for c in result["new"])

    def test_each_family_found_in_code_not_in_comments(self):
        write(self.root, "app/app/x.py", PY_BAD)
        write(self.root, "app/app/public/js/y.js", JS_BAD)
        r = coherence_check.evaluate(self.root, self.base, repo="app")
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(self.families(r), [
            ("compte-societe", "Debtors - A"), ("courriel", "p.bruno@arkonex.ca"),
            ("devise", "CAD"), ("devise", "CAD"), ("nom-taxe", "TVQ"),
            ("societe", "Arkonex"), ("taux-taxe", "0.09975")])
        self.assertEqual(r["known"], 1)     # la ligne marquée « arkonex-coherence: »

    def test_test_files_are_ignored(self):
        write(self.root, "app/app/tests/test_x.py", PY_BAD)
        write(self.root, "app/app/doctype/d/test_d.py", PY_BAD)
        write(self.root, "app/app/public/js/y.test.js", JS_BAD)
        r = coherence_check.evaluate(self.root, self.base, repo="app")
        self.assertEqual(r["verdict"], "PASS")

    def test_baseline_counts_occurrences(self):
        write(self.root, "app/app/public/js/y.js", JS_BAD + 'const d = "CAD";\n')
        entry = {"path": "app/app/public/js/y.js", "family": "devise", "value": "CAD",
                 "raison": "existant", "count": 1}
        with open(self.base, "w") as fh:
            json.dump({"repos": {"app": [entry]}}, fh)
        r = coherence_check.evaluate(self.root, self.base, repo="app")
        self.assertEqual((r["verdict"], len(r["new"]), r["known"]), ("FAIL", 1, 1))
        entry["count"] = 2
        with open(self.base, "w") as fh:
            json.dump({"repos": {"app": [entry]}}, fh)
        self.assertEqual(coherence_check.evaluate(self.root, self.base, repo="app")["verdict"],
                         "PASS")

    def test_baseline_without_reason_is_refused(self):
        with open(self.base, "w") as fh:
            json.dump({"repos": {"app": [{"path": "a.py", "family": "devise",
                                          "value": "CAD", "raison": " "}]}}, fh)
        with self.assertRaisesRegex(ValueError, "sans raison"):
            coherence_check.evaluate(self.root, self.base, repo="app")
        self.assertEqual(coherence_check.main([self.root, "--baseline", self.base,
                                               "--repo", "app"]), 2)

    def test_same_verdict_on_every_run(self):
        write(self.root, "app/app/x.py", PY_BAD)
        write(self.root, "app/app/public/js/y.js", JS_BAD)
        runs = [json.dumps(coherence_check.evaluate(self.root, self.base, repo="app"),
                           sort_keys=True) for _ in range(3)]
        self.assertEqual(len(set(runs)), 1)

    def test_shipped_baseline_is_valid(self):
        for repo in ("arkonex_estimate", "arkonex_qit", "arkonex_mail"):
            coherence_check.load_baseline(coherence_check.DEFAULT_BASELINE, repo)


class GuardRuleTests(unittest.TestCase):
    """R-COHERENCE : `gh pr create` dans une copie d'application."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def repo(self, name, origin_repo, files):
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "remote", "add", "origin",
                        "https://github.com/Tweezer1/{}.git".format(origin_repo)], check=True)
        for rel, text in files.items():
            write(root, rel, text)
        return root

    def test_new_case_refuses_pr(self):
        # Copie de travail nommée autrement (worktree) : le dépôt est lu sur origin.
        root = self.repo("wt-estimate-o999", "arkonex_estimate",
                         {"arkonex_estimate/z_open204.py": 'CUR = "CAD"\n'})
        v = guard_bash.evaluate('gh pr create --title "x" --body "y"', root)
        self.assertEqual(v[0], "deny")
        self.assertEqual(v[1], "R-COHERENCE")
        self.assertIn("z_open204.py:1 [devise]", v[2])

    def test_clean_app_passes(self):
        root = self.repo("wt-qit", "arkonex_qit", {"arkonex_qit/a.py": 'X = "rien"\n'})
        self.assertIsNone(guard_bash.evaluate("gh pr create --fill", root))

    def test_marked_line_passes(self):
        root = self.repo("wt-qit2", "arkonex_qit",
                         {"arkonex_qit/a.py": 'C = "CAD"  # arkonex-coherence: contrat\n'})
        self.assertIsNone(guard_bash.evaluate("gh pr create --fill", root))

    def test_other_repos_are_not_checked(self):
        root = self.repo("wt-docs", "arkonex-ops-docs", {"x.py": 'CUR = "CAD"\n'})
        self.assertIsNone(guard_bash.evaluate("gh pr create --fill", root))

    def test_pr_for_another_app_asks(self):
        root = self.repo("wt-docs2", "arkonex-ops-docs", {})
        v = guard_bash.evaluate("gh pr create -R Tweezer1/arkonex_qit --fill", root)
        self.assertEqual(v[:2], ("ask", "R-COHERENCE-NV"))

    def test_nested_and_help(self):
        root = self.repo("wt-est", "arkonex_estimate",
                         {"arkonex_estimate/z.py": 'CUR = "CAD"\n'})
        self.assertEqual(guard_bash.evaluate("bash -c 'gh pr create --fill'", root)[1],
                         "R-COHERENCE")
        self.assertEqual(guard_bash.evaluate("cd {} && gh pr create --fill".format(root),
                                             self.tmp)[1], "R-COHERENCE")
        self.assertIsNone(guard_bash.evaluate("gh pr create --help", root))
        self.assertIsNone(guard_bash.evaluate("gh pr view 12", root))


if __name__ == "__main__":
    unittest.main()
