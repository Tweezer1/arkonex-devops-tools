#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests du contrôle des règles du mode automatique (OPEN-194, Issue #307).

Chaque règle d'écriture est cassée volontairement une fois : le contrôle doit le voir.

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


def effectif_conforme():
    """Ce que `auto-mode config` rend quand automode.json est appliqué : "$defaults"
    remplacé par les règles par défaut, à sa place."""
    eff = {}
    for s in av.SECTIONS:
        eff[s] = []
        for r in REF["autoMode"][s]:
            eff[s].extend(DEFAUTS[s] if r == "$defaults" else [r])
    return eff


class ReferenceTest(unittest.TestCase):
    def test_reference_versionnee_conforme(self):
        self.assertEqual(av.verifier_reference(REF), [])

    def test_sans_defaults_vu(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["soft_deny"] = ref["autoMode"]["soft_deny"][1:]
        self.assertTrue(any("soft_deny" in e and "$defaults" in e for e in av.verifier_reference(ref)))

    def test_production_dans_une_autorisation_vue(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["allow"].append("Reading desk.arkonex.ca dashboards is fine.")
        self.assertTrue(any(e.startswith("allow : cite la production") for e in av.verifier_reference(ref)))

    def test_production_de_confiance_vue(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["environment"].append("**Trusted**: desk.arkonex.ca")
        self.assertIn("environment : production citée sans être déclarée interdite",
                      av.verifier_reference(ref))

    def test_interdit_absolu_retire_vu(self):
        ref = copy.deepcopy(REF)
        ref["autoMode"]["hard_deny"] = ["$defaults"]
        self.assertIn("hard_deny : l'interdit absolu de la production est absent",
                      av.verifier_reference(ref))

    def test_refus_fixe_retire_vu(self):
        ref = copy.deepcopy(REF)
        ref["permissions"]["deny"] = ["WebFetch(domain:arkonex.ca)"]
        self.assertIn("permissions.deny : WebFetch(domain:*.arkonex.ca) absent",
                      av.verifier_reference(ref))


class EffectifTest(unittest.TestCase):
    def test_conforme(self):
        self.assertEqual(av.verifier_effectif(REF, effectif_conforme(), DEFAUTS), [])

    def test_regle_inactive_vue(self):
        eff = effectif_conforme()
        eff["hard_deny"] = DEFAUTS["hard_deny"]
        self.assertTrue(any(e.startswith("hard_deny : règle inactive") for e in
                            av.verifier_effectif(REF, eff, DEFAUTS)))

    def test_defaut_perdu_vu(self):
        eff = effectif_conforme()
        eff["soft_deny"] = [r for r in eff["soft_deny"] if r != "défaut soft_deny 2"]
        self.assertTrue(any(e.startswith("soft_deny : règle par défaut perdue") for e in
                            av.verifier_effectif(REF, eff, DEFAUTS)))

    def test_reglages_sans_refus_fixe_vus(self):
        self.assertEqual(len(av.verifier_reglages({"agentPushNotifEnabled": True})), 2)
        self.assertEqual(av.verifier_reglages({"permissions": {"deny": list(av.REFUS_FIXES)}}), [])

    def test_main_pass_et_fail(self):
        with tempfile.TemporaryDirectory() as d:
            chemins = {}
            for nom, contenu in (("eff", effectif_conforme()), ("def", DEFAUTS),
                                 ("reg", {"permissions": {"deny": list(av.REFUS_FIXES)}}),
                                 ("reg_vide", {})):
                chemins[nom] = os.path.join(d, nom + ".json")
                with open(chemins[nom], "w", encoding="utf-8") as fh:
                    json.dump(contenu, fh)
            self.assertEqual(av.main([chemins["eff"], chemins["def"], "--reglages", chemins["reg"]]), 0)
            self.assertEqual(av.main([chemins["eff"], chemins["def"], "--reglages", chemins["reg_vide"]]), 1)


if __name__ == "__main__":
    unittest.main()
