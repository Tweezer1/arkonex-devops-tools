#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests du rappel de suivi GitHub du contrôle de démarrage (OPEN-169, Issue #189).

Un faux `gh` (ARKONEX_GH) renvoie des réponses préparées : aucun appel réseau. Le cas
OPEN-124 est reconstitué avec ses dates réelles (PR arkonex-estimate#13 fusionnée le
2026-09-04 à 21:31:41, Issue #86 mise à jour pour la dernière fois à 21:29:33)."""

import json
import os
import subprocess
import time
import unittest

from test_session_install import Bench, git

FAKE_GH = """#!/usr/bin/python3
import os, sys, time
mode = os.environ.get("FAKE_GH_MODE", "ok")
if mode == "fail":
    sys.stderr.write("gh: erreur simulée\\n")
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode == "garbage":
    print("pas du json")
    sys.exit(0)
if any("trace-open170" in a for a in sys.argv):  # suivi des décisions (OPEN-170) : rien
    print('{"data": {"repository": {"issues": {"nodes": []}}}}')
    sys.exit(0)
key = "FAKE_GH_PAGE2" if any(a.startswith("after=") for a in sys.argv) else "FAKE_GH_PAGE1"
with open(os.environ[key], encoding="utf-8") as fh:
    sys.stdout.write(fh.read())
"""


def pr(repo, number, body="", merged_at=None, created_at="2026-09-28T12:00:00Z",
       draft=False):
    return {"number": number, "body": body, "mergedAt": merged_at, "createdAt": created_at,
            "isDraft": draft, "repository": {"name": repo}}


def response(open_prs=(), merged=(), lots=(), next_cursor=None):
    return {"data": {
        "open": {"nodes": list(open_prs)},
        "merged": {"pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor},
                   "nodes": list(merged)},
        "lots": {"issues": {"nodes": [{"number": n, "updatedAt": u} for n, u in lots]}},
    }}


class Followup(Bench):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install("--apply").returncode, 0)
        self.gh = os.path.join(self.tmp, "fake-gh")
        with open(self.gh, "w") as fh:
            fh.write(FAKE_GH)
        os.chmod(self.gh, 0o755)

    def page(self, name, data):
        path = os.path.join(self.tmp, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def run_start(self, page1=None, page2=None, **extra):
        env = dict(os.environ, HOME=self.tmp, ARKONEX_BENCH_ROOT=self.bench,
                   ARKONEX_DOCS_DIR=self.docs, ARKONEX_GH=self.gh,
                   ARKONEX_CLAUDE_SETTINGS=os.path.join(self.claude, "settings.json"),
                   FAKE_GH_PAGE1=self.page("p1", page1 or response()),
                   FAKE_GH_PAGE2=self.page("p2", page2 or response()), **extra)
        r = subprocess.run([self.claude + "/hooks/session_start.py"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        return out, out["hookSpecificOutput"]["additionalContext"]

    def test_open_prs_and_forgotten_lot_reach_the_agent_only(self):
        data = response(
            open_prs=[pr("arkonex-ops-docs", 191),
                      pr("arkonex-estimate", 7, created_at="2026-08-03T10:00:00Z", draft=True)],
            merged=[
                # cas OPEN-124 : forme courte dans un dépôt de code, fusion après la mise à jour
                pr("arkonex-estimate", 13, "Summary…\n\nRefs #86\n", "2026-09-04T21:31:41Z"),
                # forme complète, mot-clé Fixes
                pr("arkonex-devops-tools", 12, "Fixes Tweezer1/arkonex-ops-docs#99",
                   "2026-09-02T08:00:00Z"),
                # dossier mis à jour après la fusion : rien à signaler
                pr("arkonex-ops-docs", 133, "Refs #41", "2026-09-27T11:03:05Z"),
                # simple lien : une mention, pas une référence
                pr("arkonex-ops-docs", 151,
                   "voir https://github.com/Tweezer1/arkonex-ops-docs/issues/50",
                   "2026-09-10T08:00:00Z"),
                # référence à une Issue fermée ou hors open-lot : ignorée
                pr("arkonex-ops-docs", 152, "Refs #168", "2026-09-27T01:00:00Z"),
            ],
            lots=[(86, "2026-09-04T21:29:33Z"), (99, "2026-09-01T00:00:00Z"),
                  (41, "2026-09-28T19:00:00Z"), (50, "2026-08-01T00:00:00Z")])
        out, ctx = self.run_start(data)
        self.assertNotIn("systemMessage", out)
        self.assertIn("garde-fous actifs", ctx)
        self.assertIn("2 PR ouverte(s)", ctx)
        self.assertIn("arkonex-estimate#7 (", ctx)
        self.assertIn("brouillon", ctx)
        self.assertIn("arkonex-ops-docs#191 (", ctx)
        self.assertIn("2 dossier(s) à mettre à jour", ctx)
        self.assertIn("#86 (arkonex-estimate#13 fusionnée le 2026-09-04)", ctx)
        self.assertIn("#99 (arkonex-devops-tools#12 fusionnée le 2026-09-02)", ctx)
        self.assertNotIn("#41 (", ctx)
        self.assertNotIn("#50 (", ctx)
        self.assertNotIn("#168", ctx)
        self.assertIn("À signaler au propriétaire", ctx)

    def test_second_page_of_merged_prs_is_read(self):
        page1 = response(lots=[(86, "2026-09-04T21:29:33Z")], next_cursor="C1")
        page2 = response(merged=[pr("arkonex-estimate", 13, "Refs #86",
                                    "2026-09-04T21:31:41Z")])
        _, ctx = self.run_start(page1, page2)
        self.assertIn("#86 (arkonex-estimate#13", ctx)

    def test_nothing_pending(self):
        _, ctx = self.run_start(response(lots=[(86, "2026-09-28T00:00:00Z")]))
        self.assertIn("aucune PR ouverte ni dossier à mettre à jour", ctx)

    def test_gh_failure_is_one_line_and_other_checks_intact(self):
        out, ctx = self.run_start(FAKE_GH_MODE="fail")
        self.assertNotIn("systemMessage", out)
        self.assertIn("garde-fous actifs", ctx)
        self.assertIn("Suivi GitHub (OPEN-169) indisponible (gh a échoué, code 1)", ctx)

    def test_unreadable_answer_is_one_line(self):
        _, ctx = self.run_start(FAKE_GH_MODE="garbage")
        self.assertIn("Suivi GitHub (OPEN-169) indisponible (JSONDecodeError)", ctx)

    def test_slow_github_never_holds_the_session(self):
        started = time.monotonic()
        out, ctx = self.run_start(FAKE_GH_MODE="sleep", ARKONEX_FOLLOWUP_TIMEOUT="1")
        self.assertLess(time.monotonic() - started, 15)
        self.assertNotIn("systemMessage", out)
        self.assertIn("indisponible (délai dépassé)", ctx)

    def test_alert_for_the_person_never_carries_the_followup(self):
        git("switch", "-q", "-c", "lot/x", cwd=self.docs)
        out, ctx = self.run_start(response(open_prs=[pr("arkonex-ops-docs", 191)]))
        self.assertIn("pas sur main", out["systemMessage"])
        self.assertNotIn("Suivi GitHub", out["systemMessage"])
        self.assertIn("pas sur main", ctx)
        self.assertIn("1 PR ouverte(s)", ctx)

    def test_can_be_switched_off(self):
        _, ctx = self.run_start(response(open_prs=[pr("arkonex-ops-docs", 191)]),
                                ARKONEX_FOLLOWUP="off")
        self.assertNotIn("Suivi GitHub", ctx)


if __name__ == "__main__":
    unittest.main()
