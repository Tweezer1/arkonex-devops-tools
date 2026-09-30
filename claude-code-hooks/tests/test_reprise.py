#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests de la fiche de reprise et du suivi des décisions (OPEN-170, Issue #195).

Un faux `gh` (ARKONEX_GH) renvoie des réponses préparées selon la requête (marqueur en
tête de la requête GraphQL) : aucun appel réseau. Le cas de #181 est reconstitué en petit :
un tableau des décisions (en vigueur, remplacée, reçue à intégrer), une décision consignée
après la dernière mise à jour du résumé, une mention venue d'un autre dossier."""

import json
import os
import subprocess
import time
import unittest

from test_session_install import Bench

FAKE_GH = """#!/usr/bin/python3
import os, sys, time
mode = os.environ.get("FAKE_GH_MODE", "ok")
if mode == "fail":
    sys.stderr.write("gh: erreur simulée\\n")
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
query = " ".join(sys.argv)
key = "FAKE_GH_PAGE1"
for marker, name in (("reprise-open170", "FAKE_GH_REPRISE"),
                     ("fraicheur-open170", "FAKE_GH_FRESH"),
                     ("trace-open170", "FAKE_GH_TRACE")):
    if marker in query:
        key = name
path = os.environ.get(key)
if not path:
    print('{"data": {}}')
    sys.exit(0)
with open(path, encoding="utf-8") as fh:
    sys.stdout.write(fh.read())
"""

U = "https://github.com/Tweezer1/arkonex-ops-docs/issues/"

BODY = """### Objectif / objet

Lot fictif pour éprouver la fiche de reprise.

**Phase : PLAN_ONLY.** La création n'autorise rien.

### Remaining gates

- [x] `CREATE_POSTCHECK`
- [ ] **B2-3, propositions et analyse** : contrat, GO, réalisation

### Decision / gate evidence

| Date | Décision (une ligne) | Portée | Source | État |
|---|---|---|---|---|
| 2026-09-27 | Réserve R-1 : option A | B1 | [décision]({u}999#issuecomment-100) | remplacée par [#15]({u}999#issuecomment-101) |
| 2026-09-27 | Réserve R-1 : option B | B1 | [décision]({u}999#issuecomment-101) | en vigueur |
| 2026-09-29 | Base des billets : BQ3 retenue | B2-2 | [décision]({u}999#issuecomment-200) | en partie remplacée : types fixés par Q-A |
| 2026-09-29 | Contrat ratifié ; GO de B2-2a | B2-2 | [décision]({u}999#issuecomment-300) | en vigueur |
| 2026-09-29 | Reçu de #195 (2026-09-29) — à intégrer : BT-003 (E3) retenu | lot | [décision]({u}195#issuecomment-1) | à intégrer |

### Residual risks

N/A
""".format(u=U)

REGISTRE = """# Besoins transverses — registre

### BT-003 — Effet des reprises sur les quantités et les heures acquises

- **Origine** : Audit, écart E3.
- **Qualification** : **besoin retenu** ([décision](https://example.invalid/d)).
"""

DECISION = "## Décision du propriétaire — {} `[DÉCIDÉ-MÉTIER — 2026-09-29]`\n\nTexte."


def issue_response(body=BODY, last_edited="2026-09-29T20:00:00Z",
                   updated="2026-09-29T21:00:00Z"):
    comments = [
        {"createdAt": "2026-09-29T19:00:00Z", "url": U + "999#issuecomment-300",
         "body": DECISION.format("ratification du contrat")},
        {"createdAt": "2026-09-29T20:30:00Z", "url": U + "999#issuecomment-400",
         "body": DECISION.format("ordre d'installation")},
        {"createdAt": "2026-09-29T20:40:00Z", "url": U + "999#issuecomment-401",
         "body": "## Compte rendu d'essai\n\nRésultats."},
    ]
    mentions = [
        {"createdAt": "2026-09-29T20:50:00Z",
         "source": {"__typename": "Issue", "number": 42, "title": "Autre lot"}},
        {"createdAt": "2026-09-28T08:00:00Z",
         "source": {"__typename": "Issue", "number": 7, "title": "Ancienne mention"}},
    ]
    return {"data": {"repository": {
        "issue": {"number": 999, "title": "OPEN-999-TEST-001 — Lot fictif", "url": U + "999",
                  "state": "OPEN", "createdAt": "2026-09-27T10:00:00Z", "updatedAt": updated,
                  "lastEditedAt": last_edited, "body": body,
                  "labels": {"nodes": [{"name": "open-lot"}, {"name": "status:in-progress"},
                                       {"name": "besoin:bt-003"}]},
                  "comments": {"nodes": comments},
                  "timelineItems": {"nodes": mentions}},
        "registre": {"text": REGISTRE}}}}


def fresh_response(updated):
    return {"data": {"repository": {"issue": {"updatedAt": updated}}}}


def trace_lot(number, last_edited, comments=(), body=""):
    return {"number": number, "createdAt": "2026-09-01T00:00:00Z", "lastEditedAt": last_edited,
            "body": body,
            "comments": {"nodes": [{"createdAt": at, "body": text} for at, text in comments]}}


def trace_response(*lots):
    return {"data": {"repository": {"issues": {"nodes": list(lots)}}}}


EMPTY_FOLLOWUP = {"data": {"open": {"nodes": []},
                           "merged": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                      "nodes": []},
                           "lots": {"issues": {"nodes": []}}}}


class Reprise(Bench):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install("--apply").returncode, 0)
        self.gh = os.path.join(self.tmp, "fake-gh")
        with open(self.gh, "w") as fh:
            fh.write(FAKE_GH)
        os.chmod(self.gh, 0o755)
        self.state_dir = os.path.join(self.tmp, "reprise")

    def data(self, name, content):
        path = os.path.join(self.tmp, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(content, fh, ensure_ascii=False)
        return path

    def env(self, **extra):
        return dict(os.environ, HOME=self.tmp, ARKONEX_BENCH_ROOT=self.bench,
                    ARKONEX_DOCS_DIR=self.docs, ARKONEX_GH=self.gh,
                    ARKONEX_CLAUDE_SETTINGS=os.path.join(self.claude, "settings.json"),
                    ARKONEX_REPRISE_DIR=self.state_dir, **extra)

    def fiche(self, response=None, session="S-1", **extra):
        env = self.env(FAKE_GH_REPRISE=self.data("reprise", response or issue_response()),
                       CLAUDE_CODE_SESSION_ID=session, **extra)
        r = subprocess.run([self.claude + "/hooks/reprendre_lot.py", "999"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def compact(self, stdin, **extra):
        r = subprocess.run([self.claude + "/hooks/session_start.py", "--compact"], input=stdin,
                           env=self.env(**extra), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout.strip():
            return ""
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def start(self, trace, **extra):
        env = self.env(FAKE_GH_TRACE=self.data("trace", trace),
                       FAKE_GH_PAGE1=self.data("p1", EMPTY_FOLLOWUP), **extra)
        r = subprocess.run([self.claude + "/hooks/session_start.py"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        return out, out["hookSpecificOutput"]["additionalContext"]

    # --- fiche de reprise (reprendre_lot.py) -------------------------------------------

    def test_fiche_shows_decisions_received_items_and_what_is_late(self):
        out = self.fiche()
        for expected in ("lot: OPEN-999-TEST-001", "issue: 999", "recus_non_integres: 1",
                         "a_reconcilier: true", "Décisions en vigueur (3 ; 1 remplacée(s))",
                         "Contrat ratifié ; GO de B2-2a", "Reçu de #195 (2026-09-29) — à intégrer",
                         "BQ3 retenue", "(en partie remplacée : types fixés par Q-A)",
                         "ordre d'installation", "#42 Autre lot",
                         "BT-003 — Effet des reprises", "besoin retenu",
                         "B2-3, propositions et analyse", "phrase de statut",
                         "résumé en retard sur 1 décision(s)", "Prochain travail permis"):
            self.assertIn(expected, out)
        for absent in ("Réserve R-1 : option A", "#7 Ancienne mention", "Compte rendu d'essai",
                       "CREATE_POSTCHECK"):
            self.assertNotIn(absent, out)

    def test_up_to_date_summary_is_not_flagged(self):
        out = self.fiche(issue_response(last_edited="2026-09-29T20:45:00Z"))
        self.assertIn("a_reconcilier: false", out)
        self.assertNotIn("résumé en retard", out)

    def test_body_without_decision_table_is_flagged(self):
        body = ("### Objectif / objet\n\nAncien format.\n\n### Decision / gate evidence\n\n"
                "- GO rendu le 2026-09-01.\n")
        out = self.fiche(issue_response(body=body))
        self.assertIn("Tableau des décisions absent du corps", out)
        self.assertIn("ordre d'installation", out)

    def test_github_failure_never_blocks_and_saves_nothing(self):
        out = self.fiche(FAKE_GH_MODE="fail")
        self.assertIn("Fiche de reprise indisponible (gh a échoué, code 1)", out)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "S-1.json")))

    def test_slow_github_never_holds_the_session(self):
        started = time.monotonic()
        out = self.fiche(FAKE_GH_MODE="sleep", ARKONEX_REPRISE_TIMEOUT="1")
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("délai dépassé", out)

    def test_usage_without_a_number(self):
        r = subprocess.run([self.claude + "/hooks/reprendre_lot.py"], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage", r.stdout)

    # --- réinjection après compaction (session_start.py --compact) ----------------------

    def test_compaction_reinjects_the_declared_lot(self):
        self.fiche(session="S-1")
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "S-1.json")))
        stdin = json.dumps({"session_id": "S-1", "source": "compact"})
        ctx = self.compact(stdin, FAKE_GH_FRESH=self.data(
            "fresh", fresh_response("2026-09-29T21:00:00Z")))
        self.assertIn("Reprise après compaction (OPEN-170)", ctx)
        self.assertIn("lot: OPEN-999-TEST-001", ctx)
        self.assertIn("l'Issue fait foi", ctx)
        self.assertNotIn("a changé depuis la fiche", ctx)
        ctx = self.compact(stdin, FAKE_GH_FRESH=self.data(
            "fresh2", fresh_response("2026-09-29T23:00:00Z")))
        self.assertIn("a changé depuis la fiche", ctx)
        self.assertIn("relancer /reprendre-lot 999", ctx)

    def test_compaction_without_declared_lot_adds_nothing(self):
        self.assertEqual(self.compact(json.dumps({"session_id": "inconnue"})), "")
        self.assertEqual(self.compact("pas du json"), "")

    def test_session_id_from_environment_and_github_down(self):
        self.fiche(session="S-2")
        ctx = self.compact("", CLAUDE_CODE_SESSION_ID="S-2", FAKE_GH_MODE="fail")
        self.assertIn("Reprise après compaction", ctx)
        self.assertIn("Fraîcheur non vérifiée", ctx)

    # --- suivi des décisions au démarrage (session_start.py) ----------------------------

    def test_late_summary_and_received_items_reach_the_agent_only(self):
        decision = DECISION.format("x")
        data = trace_response(
            trace_lot(181, "2026-09-29T20:00:00Z",
                      comments=[("2026-09-29T20:30:00Z", decision),
                                ("2026-09-29T20:40:00Z", "## Compte rendu")]),
            trace_lot(158, "2026-09-29T22:00:00Z",
                      comments=[("2026-09-29T21:00:00Z", decision)],
                      body="| 2026-09-29 | Reçu de #195 (2026-09-29) — à intégrer : BT-003 "
                           "| lot | [d](https://example.invalid) | à intégrer |"),
            trace_lot(41, "2026-09-29T23:00:00Z",
                      body="| 2026-09-29 | Reçu de #195 — BT-003 | lot | "
                           "[d](https://example.invalid) | intégré le 2026-09-30 |"))
        out, ctx = self.start(data)
        self.assertNotIn("systemMessage", out)
        self.assertIn("garde-fous actifs", ctx)
        self.assertIn("1 résumé(s) en retard sur une décision : #181 (1 décision(s), la "
                      "dernière le 2026-09-29)", ctx)
        self.assertIn("éléments reçus à intégrer : #158 (1)", ctx)
        self.assertNotIn("#41 (", ctx)
        self.assertIn("§12", ctx)

    def test_nothing_to_report(self):
        _, ctx = self.start(trace_response())
        self.assertIn("aucun résumé en retard ni élément reçu à intégrer", ctx)

    def test_trace_failure_is_one_line(self):
        _, ctx = self.start(trace_response(), FAKE_GH_MODE="fail")
        self.assertIn("Suivi des décisions (OPEN-170) indisponible (gh a échoué, code 1)", ctx)
        self.assertIn("garde-fous actifs", ctx)

    def test_trace_can_be_switched_off(self):
        _, ctx = self.start(trace_response(), ARKONEX_TRACE="off")
        self.assertNotIn("Suivi des décisions", ctx)

    # --- installation --------------------------------------------------------------------

    def test_install_deploys_the_skill_and_the_compact_entry(self):
        skill = os.path.join(self.claude, "skills", "reprendre-lot", "SKILL.md")
        with open(skill, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(self.bench + "/.claude/hooks/reprendre_lot.py $ARGUMENTS", text)
        self.assertNotIn("{{BENCH_ROOT}}", text)
        self.assertTrue(os.access(os.path.join(self.claude, "hooks", "reprendre_lot.py"),
                                  os.X_OK))
        with open(os.path.join(self.claude, "settings.json"), encoding="utf-8") as fh:
            starts = json.load(fh)["hooks"]["SessionStart"]
        self.assertIn({"matcher": "compact", "hooks": [
            {"type": "command", "command": self.claude + "/hooks/session_start.py --compact",
             "timeout": 15}]}, starts)


if __name__ == "__main__":
    unittest.main()
