#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests de la logique pure du contrôle des liens cassés (OPEN-164, Issue #173).

Ne dépend pas de frappe/bench : ne teste que `link_check_core.py` (normalisation,
clé d'entrée, dédoublonnage, comparaison à une référence, aller-retour référence).

Lancer : /usr/bin/python3 -m unittest discover -s dev-link-check/tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import link_check_core as core  # noqa: E402


def make_entry(field="DocA.link_field", target="DocB", doctype="DocA",
                name="DOCA-0001", value="DOCB-0001"):
    """Fabrique une entrée de test avec des valeurs par défaut lisibles."""
    return {"field": field, "target": target, "doctype": doctype, "name": name, "value": value}


class TestNormalize(unittest.TestCase):

    def test_casse_ignoree(self):
        self.assertEqual(core.normalize("DOCB-0001"), core.normalize("docb-0001"))

    def test_espaces_de_fin_ignores(self):
        self.assertEqual(core.normalize("DOCB-0001   "), core.normalize("DOCB-0001"))

    def test_espaces_de_debut_non_ignores(self):
        # rstrip seulement : un espace de début reste significatif (comme MariaDB).
        self.assertNotEqual(core.normalize("  DOCB-0001"), core.normalize("DOCB-0001"))


class TestEntryKey(unittest.TestCase):

    def test_cle_stable_pour_valeurs_equivalentes(self):
        e1 = make_entry(value="DOCB-0001")
        e2 = make_entry(value="docb-0001  ")
        self.assertEqual(core.entry_key(e1), core.entry_key(e2))

    def test_cle_distincte_si_champ_different(self):
        e1 = make_entry(field="DocA.link_field")
        e2 = make_entry(field="DocA.autre_champ")
        self.assertNotEqual(core.entry_key(e1), core.entry_key(e2))

    def test_cle_distincte_si_document_different(self):
        e1 = make_entry(name="DOCA-0001")
        e2 = make_entry(name="DOCA-0002")
        self.assertNotEqual(core.entry_key(e1), core.entry_key(e2))


class TestDedupe(unittest.TestCase):

    def test_supprime_les_doublons_de_cle(self):
        e1 = make_entry(value="DOCB-0001")
        e2 = make_entry(value="docb-0001")  # même clé normalisée que e1
        e3 = make_entry(name="DOCA-0002", value="DOCB-0002")
        result = core.dedupe([e1, e2, e3])
        self.assertEqual(len(result), 2)

    def test_ordre_stable(self):
        e1 = make_entry(name="DOCA-0001")
        e2 = make_entry(name="DOCA-0002")
        e3 = make_entry(name="DOCA-0003")
        result = core.dedupe([e3, e1, e2, e1])
        self.assertEqual([e["name"] for e in result], ["DOCA-0003", "DOCA-0001", "DOCA-0002"])


class TestCompare(unittest.TestCase):

    def _baseline_with(self, entries, reasons=None):
        return core.baseline_from_entries(
            entries, site="deverp.arkonex.ca", generated_at="2026-09-27T00:00:00",
            lot="OPEN-164", reasons=reasons or {},
        )

    def test_pass_quand_tout_est_dans_la_reference(self):
        entry = make_entry()
        baseline = self._baseline_with([entry])
        report = core.compare([entry], baseline, fields_total=1, fields_checked=1, ignored=[])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["anomalies"], [])
        self.assertEqual(report["counters"]["broken_new"], 0)

    def test_fail_avec_entree_nouvelle(self):
        known = make_entry(name="DOCA-0001")
        nouvelle = make_entry(name="DOCA-0002")
        baseline = self._baseline_with([known])
        report = core.compare([known, nouvelle], baseline, fields_total=1, fields_checked=1, ignored=[])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["anomalies"]), 1)
        self.assertEqual(report["anomalies"][0]["name"], "DOCA-0002")
        self.assertEqual(report["counters"]["broken_new"], 1)
        self.assertEqual(report["counters"]["broken_known"], 1)
        self.assertEqual(report["counters"]["broken_total"], 2)

    def test_resolved_quand_une_entree_de_reference_disparait(self):
        toujours_la = make_entry(name="DOCA-0001")
        repare = make_entry(name="DOCA-0002")
        baseline = self._baseline_with([toujours_la, repare])
        # "repare" n'est plus trouvé lors de ce scan : le lien a été réparé.
        report = core.compare([toujours_la], baseline, fields_total=1, fields_checked=1, ignored=[])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["resolved"]), 1)
        self.assertEqual(report["counters"]["baseline_resolved"], 1)

    def test_valeur_de_casse_differente_reconnue_comme_connue(self):
        known = make_entry(value="DOCB-0001")
        baseline = self._baseline_with([known])
        vue_en_minuscules = make_entry(value="docb-0001")
        report = core.compare([vue_en_minuscules], baseline, fields_total=1, fields_checked=1, ignored=[])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["anomalies"], [])

    def test_warnings_reprend_la_liste_ignoree(self):
        ignored = ["DocA.champ -> DocB : meta introuvable (cible)"]
        report = core.compare([], {"fields": {}}, fields_total=1, fields_checked=0, ignored=ignored)
        self.assertEqual(report["warnings"], ignored)
        self.assertEqual(report["counters"]["fields_ignored"], 1)


class TestBaselineRoundTrip(unittest.TestCase):

    def test_aller_retour_baseline_from_entries_baseline_keys(self):
        entries = [
            make_entry(name="DOCA-0001", value="DOCB-0001"),
            make_entry(name="DOCA-0002", value="DOCB-0002"),
            make_entry(field="DocC.autre", target="DocD", doctype="DocC",
                       name="DOCC-0001", value="DOCD-0001"),
        ]
        baseline = core.baseline_from_entries(
            entries, site="deverp.arkonex.ca", generated_at="2026-09-27T00:00:00",
            lot="OPEN-164", reasons={},
        )
        attendu = {core.entry_key(e) for e in core.dedupe(entries)}
        self.assertEqual(core.baseline_keys(baseline), attendu)

    def test_raison_conservee(self):
        entry = make_entry()
        field_key = "%s->%s" % (entry["field"], entry["target"])
        baseline = core.baseline_from_entries(
            [entry], site="s", generated_at="t", lot="L",
            reasons={field_key: "Résidu connu, voir Issue #173"},
        )
        self.assertEqual(baseline["fields"][field_key]["reason"], "Résidu connu, voir Issue #173")

    def test_raison_par_defaut_a_justifier(self):
        entry = make_entry()
        field_key = "%s->%s" % (entry["field"], entry["target"])
        baseline = core.baseline_from_entries(
            [entry], site="s", generated_at="t", lot="L", reasons={},
        )
        self.assertEqual(baseline["fields"][field_key]["reason"], "À JUSTIFIER")

    def test_baseline_vide_ne_reconnait_rien(self):
        self.assertEqual(core.baseline_keys({}), set())
        self.assertEqual(core.baseline_keys(None), set())


if __name__ == "__main__":
    unittest.main()
