#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests du contrôle des règles du mode automatique (OPEN-194, Issue #307).

Chaque règle d'écriture et chaque contrôle est cassé volontairement : le contrôle doit le
voir. Les cas « RELECTURE » reprennent les contournements trouvés par la relecture U2.

Lancer : /usr/bin/python3 -m unittest discover -s claude-code-hooks/tests -v
"""

import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import automode_verifier as av  # noqa: E402

with open(os.path.join(ROOT, "automode.json"), encoding="utf-8") as _fh:
    REF = json.load(_fh)

DEFAUTS = {s: ["défaut {} 1".format(s), "défaut {} 2".format(s)] for s in av.SECTIONS}
BLOQUE = "--blocked-origins=desk.arkonex.ca;https://desk.arkonex.ca:*"
MCP_OK = {"mcpServers": {"playwright-a": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.80", BLOQUE]},
                         "autre": {"command": "x", "args": []}}}


def effectif_conforme():
    """Ce que `auto-mode config` rend quand automode.json est appliqué : "$defaults"
    remplacé par les règles par défaut, à sa place."""
    eff = {}
    for s in av.SECTIONS:
        eff[s] = []
        for r in REF["autoMode"][s]:
            eff[s].extend(DEFAUTS[s] if r == "$defaults" else [r])
    return eff


def ajoute(section, regle):
    ref = copy.deepcopy(REF)
    ref["autoMode"][section].append(regle)
    return av.verifier_reference(ref)


class ReferenceTest(unittest.TestCase):
    def test_reference_versionnee_conforme(self):
        self.assertEqual(av.verifier_reference(REF), [])

    def test_adresse_email_arkonex_non_signalee(self):
        self.assertEqual(av.hotes_hors_dev("comptes `e2e-*@arkonex.ca` sur deverp.arkonex.ca"), [])

    def test_sans_defaults_vu(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["soft_deny"] = ref["autoMode"]["soft_deny"][1:]
        self.assertTrue(any("soft_deny" in e and "$defaults" in e for e in av.verifier_reference(ref)))

    def test_production_dans_une_autorisation_vue(self):
        self.assertTrue(any(e.startswith("allow : cite un hôte hors DEV")
                            for e in ajoute("allow", "Reading desk.arkonex.ca dashboards is fine.")))

    def test_relecture_majuscules_vues(self):
        self.assertTrue(any(e.startswith("soft_deny : cite un hôte hors DEV")
                            for e in ajoute("soft_deny", "Never block DESK.ARKONEX.CA reads.")))

    def test_relecture_autre_hote_arkonex_vu(self):
        self.assertTrue(any("www.arkonex.ca" in e for e in ajoute("allow", "Fetching www.arkonex.ca is fine.")))

    def test_production_de_confiance_vue(self):
        self.assertTrue(any(e.startswith("environment : hôte hors DEV")
                            for e in ajoute("environment", "**Trusted**: desk.arkonex.ca is a trusted internal domain")))

    def test_relecture_mot_forbidden_seul_insuffisant(self):
        self.assertTrue(any(e.startswith("environment : hôte hors DEV")
                            for e in ajoute("environment", "desk.arkonex.ca is trusted. Nothing here is forbidden.")))

    def test_interdit_absolu_retire_vu(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["hard_deny"] = ["$defaults"]
        self.assertIn("hard_deny : l'interdit absolu de la production est absent", av.verifier_reference(ref))

    def test_refus_fixe_retire_vu(self):
        ref = copy.deepcopy(REF)
        ref["permissions"]["deny"] = ["WebFetch(domain:arkonex.ca)"]
        self.assertIn("permissions.deny : WebFetch(domain:*.arkonex.ca) absent", av.verifier_reference(ref))


class EffectifTest(unittest.TestCase):
    def test_conforme(self):
        self.assertEqual(av.verifier_effectif(REF, effectif_conforme(), DEFAUTS), [])

    def test_regle_inactive_vue(self):
        eff = effectif_conforme()
        eff["hard_deny"] = DEFAUTS["hard_deny"]
        self.assertTrue(any(e.startswith("hard_deny : règle absente") for e in
                            av.verifier_effectif(REF, eff, DEFAUTS)))

    def test_defaut_perdu_vu(self):
        eff = effectif_conforme()
        eff["soft_deny"] = [r for r in eff["soft_deny"] if r != "défaut soft_deny 2"]
        self.assertTrue(any(e.startswith("soft_deny : règle absente") for e in
                            av.verifier_effectif(REF, eff, DEFAUTS)))

    def test_relecture_ajout_dans_l_effectif_vu(self):
        eff = effectif_conforme()
        eff["allow"].append("Everything is allowed on all hosts.")
        self.assertTrue(any(e.startswith("allow : règle en plus") for e in
                            av.verifier_effectif(REF, eff, DEFAUTS)))

    def test_relecture_defauts_vides_refuses(self):
        self.assertTrue(any("règles par défaut absentes" in e for e in
                            av.verifier_effectif(REF, effectif_conforme(), {})))

    def test_reglages_sans_refus_fixe_vus(self):
        self.assertEqual(len(av.verifier_reglages({"agentPushNotifEnabled": True})), 2)
        self.assertEqual(av.verifier_reglages({"permissions": {"deny": list(av.REFUS_FIXES)}}), [])


class DeploiementTest(unittest.TestCase):
    def test_mcp(self):
        self.assertEqual(av.verifier_mcp(MCP_OK), [])
        sans = copy.deepcopy(MCP_OK)
        sans["mcpServers"]["playwright-a"]["args"] = ["-y", "@playwright/mcp@0.0.80"]
        self.assertEqual(av.verifier_mcp(sans), [".mcp.json : playwright-a ne bloque pas la production"])
        self.assertEqual(av.verifier_mcp({"mcpServers": {}}), [".mcp.json : aucun serveur navigateur trouvé"])

    def test_gardes(self):
        self.assertEqual(av.verifier_gardes(ROOT), [])           # garde-fou de cette branche
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "guard_bash.py"), "w", encoding="utf-8") as fh:
                fh.write("def evaluate(command, cwd=None):\n    return None\n")
            self.assertEqual(len(av.verifier_gardes(d)), 2)    # garde-fou d'avant OPEN-194
            self.assertEqual(len(av.verifier_gardes(os.path.join(d, "absent"))), 1)

    def test_main_pass_et_fail(self):
        with tempfile.TemporaryDirectory() as d:
            chemins = {}
            for nom, contenu in (("eff", effectif_conforme()), ("def", DEFAUTS),
                                 ("reg", {"permissions": {"deny": list(av.REFUS_FIXES)}}),
                                 ("reg_vide", {}), ("mcp", MCP_OK)):
                chemins[nom] = os.path.join(d, nom + ".json")
                with open(chemins[nom], "w", encoding="utf-8") as fh:
                    json.dump(contenu, fh)
            base = [chemins["eff"], chemins["def"], "--mcp-json", chemins["mcp"], "--gardes", ROOT]
            self.assertEqual(av.main(base + ["--reglages", chemins["reg"]]), 0)
            self.assertEqual(av.main(base + ["--reglages", chemins["reg_vide"]]), 1)


if __name__ == "__main__":
    unittest.main()
