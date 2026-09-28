#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Chemins et référence introuvable (OPEN-168, Issue arkonex-ops-docs#186).

Constat du 28/09/2026 : lancé depuis la racine du dépôt avec un chemin relatif de
référence, le contrôle a comparé DEV à une référence vide (faux FAIL, 41 liens
« nouveaux »). `run.sh` change de dossier avant `bench console`, qui tourne dans
`sites/`, et `link_check.py` traitait une référence absente comme vide.

Ne dépend pas de frappe/bench : `link_check.py` est exécuté avec un faux `frappe`
(tests/fake_bench_console.py), soit dans ce processus, soit par `run.sh` à travers
un faux exécutable `bench`.

Lancer : /usr/bin/python3 -m unittest discover -s dev-link-check/tests -v
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fake_bench_console  # noqa: E402
import link_check_core as core  # noqa: E402

LINK_CHECK_SCRIPT = os.path.join(ROOT, "link_check.py")
RUN_SH = os.path.join(ROOT, "run.sh")
LINK_CHECK_VARS = (
    "LINK_CHECK_DIR", "LINK_CHECK_BASELINE", "LINK_CHECK_REPORT",
    "LINK_CHECK_WRITE_BASELINE", "LINK_CHECK_LOT",
)


def write_baseline(path, count):
    """Écrit une référence de `count` liens inventés et la retourne."""
    entries = [
        {"field": "DocA.link_field", "target": "DocB", "doctype": "DocA",
         "name": "DOCA-%04d" % i, "value": "DOCB-%04d" % i}
        for i in range(count)
    ]
    baseline = core.baseline_from_entries(
        entries, site="fake.site", generated_at="2026-09-28T00:00:00",
        lot="OPEN-168", reasons={"DocA.link_field->DocB": "essai"},
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    core.dump_json(baseline, path)
    return baseline


def status_lines(output):
    return [line for line in output.splitlines() if line.startswith("LINK_CHECK_STATUS=")]


class TestReferenceIntrouvable(unittest.TestCase):
    """link_check.py exécuté dans ce processus, avec un faux frappe."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="open168-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_script(self, env):
        fake = fake_bench_console.FakeFrappe()
        clean_env = {k: v for k, v in os.environ.items() if k not in LINK_CHECK_VARS}
        clean_env.update(env)
        out = io.StringIO()
        with mock.patch.dict(os.environ, clean_env, clear=True), contextlib.redirect_stdout(out):
            with open(LINK_CHECK_SCRIPT, encoding="utf-8") as handle:
                exec(handle.read(), {"frappe": fake, "__name__": "__console__"})
        return out.getvalue(), fake.calls

    def test_reference_explicite_absente_rend_error_sans_parcours(self):
        missing = os.path.join(self.tmp, "absente.json")
        output, calls = self.run_script({"LINK_CHECK_DIR": ROOT, "LINK_CHECK_BASELINE": missing})
        self.assertEqual(status_lines(output), ["LINK_CHECK_STATUS=ERROR"], output)
        self.assertIn("LINK_CHECK_ERROR=référence introuvable : %s" % missing, output)
        self.assertIn("LINK_CHECK_BASELINE", output.split("LINK_CHECK_ERROR=", 1)[1])
        self.assertEqual(calls, [], "le site ne doit pas être parcouru")

    def test_reference_par_defaut_absente_rend_error_sans_parcours(self):
        # Dossier d'outil sans baseline.json : la référence par défaut est introuvable.
        output, calls = self.run_script({"LINK_CHECK_DIR": self.tmp})
        expected = os.path.join(self.tmp, "baseline.json")
        self.assertEqual(status_lines(output), ["LINK_CHECK_STATUS=ERROR"], output)
        self.assertIn("LINK_CHECK_ERROR=référence introuvable : %s" % expected, output)
        self.assertIn("par défaut", output.split("LINK_CHECK_ERROR=", 1)[1])
        self.assertEqual(calls, [], "le site ne doit pas être parcouru")

    def test_reference_presente_annonce_chemin_et_nombre_d_entrees(self):
        path = os.path.join(self.tmp, "ref.json")
        write_baseline(path, 3)
        output, calls = self.run_script({"LINK_CHECK_DIR": ROOT, "LINK_CHECK_BASELINE": path})
        self.assertIn("LINK_CHECK_BASELINE_LOADED=%s (3 entrée(s))" % path, output)
        # Le faux site n'a aucun lien cassé : les 3 entrées sont « résolues », PASS.
        self.assertEqual(status_lines(output), ["LINK_CHECK_STATUS=PASS"], output)
        self.assertIn(("get_all", "DocField"), calls)


class TestRunShChemins(unittest.TestCase):
    """run.sh lancé depuis un dossier « appelant », avec un faux `bench`.

    Le faux `bench` se place dans "$BENCH_DIR/sites" (comme le vrai) puis exécute la
    ligne reçue avec un faux frappe. Un chemin relatif non converti par run.sh y
    désignerait donc un autre fichier que celui de l'appelant.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="open168-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.caller = os.path.join(self.tmp, "appelant")
        self.bench_dir = os.path.join(self.tmp, "bench")
        self.bin_dir = os.path.join(self.tmp, "bin")
        for path in (self.caller, os.path.join(self.bench_dir, "sites"), self.bin_dir):
            os.makedirs(path)
        stub = os.path.join(self.bin_dir, "bench")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                "cd sites || exit 3\n"
                'exec "%s" "%s"\n' % (sys.executable, os.path.join(HERE, "fake_bench_console.py"))
            )
        os.chmod(stub, 0o755)

    def run_sh(self, *args, env=None):
        full_env = {k: v for k, v in os.environ.items() if k not in LINK_CHECK_VARS}
        full_env["PATH"] = self.bin_dir + os.pathsep + full_env.get("PATH", "")
        full_env["BENCH_DIR"] = self.bench_dir
        full_env.update(env or {})
        return subprocess.run(
            ["bash", RUN_SH, "fake.site"] + list(args),
            cwd=self.caller, env=full_env, capture_output=True, text=True, timeout=60,
        )

    def test_reference_relative_resolue_depuis_le_dossier_appelant(self):
        write_baseline(os.path.join(self.caller, "rel", "ref.json"), 2)
        result = self.run_sh("rel/ref.json")
        expected = os.path.join(self.caller, "rel", "ref.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Première ligne, sans l'invite IPython "In [1]: " : lisible par un grep ancré.
        self.assertEqual(
            result.stdout.splitlines()[0],
            "LINK_CHECK_BASELINE_LOADED=%s (2 entrée(s))" % expected,
        )
        self.assertIn("LINK_CHECK_STATUS=PASS", result.stdout)

    def test_reference_explicite_absente_code_2(self):
        result = self.run_sh("rel/absente.json")
        expected = os.path.join(self.caller, "rel", "absente.json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("LINK_CHECK_STATUS=ERROR", result.stdout)
        self.assertIn("LINK_CHECK_ERROR=référence introuvable : %s" % expected, result.stdout)

    def test_sans_reference_utilise_la_reference_versionnee(self):
        expected_count = len(core.baseline_keys(core.load_json(os.path.join(ROOT, "baseline.json"))))
        result = self.run_sh()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "LINK_CHECK_BASELINE_LOADED=%s (%d entrée(s))" % (os.path.join(ROOT, "baseline.json"), expected_count),
            result.stdout,
        )

    def test_rapport_relatif_ecrit_dans_le_dossier_appelant(self):
        # Référence en chemin absolu : seul l'emplacement du rapport est en jeu ici.
        ref = os.path.join(self.caller, "ref.json")
        write_baseline(ref, 1)
        # Le même sous-dossier existe sous sites/ : sans conversion, le rapport y
        # serait écrit sans erreur, loin de l'appelant.
        for base in (self.caller, os.path.join(self.bench_dir, "sites")):
            os.makedirs(os.path.join(base, "out"))
        result = self.run_sh(ref, "out/rapport.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.caller, "out", "rapport.json")))
        self.assertFalse(os.path.exists(os.path.join(self.bench_dir, "sites", "out", "rapport.json")))

    def test_reference_fraiche_relative_ecrite_dans_le_dossier_appelant(self):
        ref = os.path.join(self.caller, "ref.json")
        write_baseline(ref, 1)
        for base in (self.caller, os.path.join(self.bench_dir, "sites")):
            os.makedirs(os.path.join(base, "out"))
        result = self.run_sh(ref, env={"LINK_CHECK_WRITE_BASELINE": "out/fraiche.json"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.caller, "out", "fraiche.json")))
        self.assertFalse(os.path.exists(os.path.join(self.bench_dir, "sites", "out", "fraiche.json")))


if __name__ == "__main__":
    unittest.main()
