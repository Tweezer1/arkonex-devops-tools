#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Tests des garde-fous OPEN-161 (guard_bash, guard_paths).

Chaque règle a au moins un cas qui doit déclencher et un cas voisin qui ne doit pas
déclencher. Les cas « REJEU » reproduisent des blocages à tort trouvés en rejouant
l'historique réel des sessions : ils ne doivent jamais revenir.

Lancer : /usr/bin/python3 -m unittest discover -s claude-code-hooks/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import guard_bash  # noqa: E402
import guard_paths  # noqa: E402

ALLOW, DENY, ASK = None, "deny", "ask"

CASES = [
    # --- Travail normal : rien à signaler
    ("ls -la && git status --short", ALLOW),
    ("bench --site deverp.arkonex.ca migrate", ALLOW),
    ("bench --site deverp.arkonex.ca console", ALLOW),
    ("bench build --app frappe", ALLOW),
    ("bench --site deverp.arkonex.ca execute frappe.db.count --args '[\"User\"]'", ALLOW),
    ("gh pr merge 12 --merge", ALLOW),
    ("git push -u origin lot/open-161-x", ALLOW),
    ("curl -s https://deverp.arkonex.ca/api/method/ping", ALLOW),
    ("S=deverp.arkonex.ca; bench --site $S migrate", ALLOW),
    # --- Sites de test isolés (.local)
    ("cd /home/frappe/l2repro-bench && bench --site l2mdb.local migrate", ALLOW),
    ("bench --site l2r5.local console", ALLOW),
    ("bench --site=l2repro.local list-apps", ALLOW),
    # --- sudo en lecture
    ("sudo -n -l", ALLOW),
    ("sudo -n -l 2>&1 | head -20", ALLOW),
    ("sudo -n true 2>&1 && echo OUI || echo NON", ALLOW),
    ("sudo -n supervisorctl status", ALLOW),
    ("sudo -n systemctl status browser-qa-refresh@sales_user.timer", ALLOW),
    # REJEU : sudo -l suivi d'une commande ne fait que vérifier le droit
    ("sudo -n -l /usr/bin/supervisorctl restart frappe-bench-web:", ALLOW),
    # --- Le texte n'est pas une commande
    ("echo \"essai --force\"", ALLOW),
    ("echo a#b && ls # sudo rm -rf /", ALLOW),
    ("grep -rn sudo script.sh", ALLOW),
    ("echo \"jamais desk.arkonex.ca\"", ALLOW),
    ("test -d sites/desk.arkonex.ca && echo PRESENT || echo ABSENT", ALLOW),
    ("echo '$(mysql -e 1)'", ALLOW),
    ("cat > /tmp/x <<'EOF'\nsudo rm -rf /\nbench drop-site x\nmysql -e 1\nEOF\necho ok", ALLOW),
    ("cat > /tmp/x <<\\EOF\n$(mysql -e 1)\nEOF", ALLOW),
    # Heredoc à délimiteur non protégé : le texte est ignoré, pas les $( ) ni les ` `
    ("cat > /tmp/x <<EOF\nPrix : $PRIX \"cité\" sudo rm -rf / ; mysql -e 1\nEOF", ALLOW),
    # RELECTURE : données envoyées au serveur DEV citant un autre hôte (pas une connexion)
    ("curl -s https://deverp.arkonex.ca/api/method/x --data-urlencode 'value=jamais desk.arkonex.ca'",
     ALLOW),
    ("curl -s https://deverp.arkonex.ca/api/resource/Note --data '{\"content\": \"voir old.arkonex.ca\"}'",
     ALLOW),
    ("curl -e https://desk.arkonex.ca/page -o desk.arkonex.ca.html https://deverp.arkonex.ca/api",
     ALLOW),
    ("python3 -c \"print('jamais desk.arkonex.ca')\"", ALLOW),
    ("bench --site DEVERP.ARKONEX.CA migrate", ALLOW),
    # REJEU : corps de PR rédigé par heredoc dans une substitution entre guillemets
    ("gh pr create --base main --head lot/x --title t --body \"$(cat <<'EOF'\n"
     "Aucun `bench migrate`, aucun `sudo systemctl restart`.\nEOF\n)\"", ALLOW),
    # REJEU : nom de branche contenant « master » et --base main d'une PR
    ("git push origin lot/open-130-tranche-b-master-spec-delta-plan", ALLOW),
    ("gh pr create --repo Tweezer1/arkonex-ops-docs --base main --head lot/x --title t", ALLOW),
    # REJEU : aide et version
    ("bench --site test restore --help", ALLOW),
    ("bench update --help", ALLOW),
    ("bench build --help 2>&1 | head -18", ALLOW),
    ("mysql --version 2>/dev/null || mariadb --version", ALLOW),

    # --- Base de données en direct : refus
    ("mysql -u root -e \"SELECT 1\"", DENY),
    ("timeout 20 mysql -u root -e \"select 1\" 2>&1 | head -3", DENY),
    ("sudo mysql -u root -e \"SELECT CURRENT_USER();\"", DENY),
    ("bench --site deverp.arkonex.ca mariadb -e \"SELECT @@global.transaction_isolation\"", DENY),
    ("echo 'select 1' | bench --site deverp.arkonex.ca mariadb", DENY),
    ("bench --site deverp.arkonex.ca execute frappe.db.sql --kwargs '{\"query\": \"select 1\"}'", DENY),
    ("bash -c \"mysql -e 'select 1'\"", DENY),
    ("echo \"$(mysql -e 1)\"", DENY),
    ("X=$(mysql -e 1); echo $X", DENY),
    ("echo `mysql -e 1`", DENY),
    ("bash <<'EOF'\nmysql -e 1\nEOF", DENY),
    # RELECTURE : heredoc non protégé donné à un programme quelconque — bash exécute $( )
    ("cat > /tmp/out.txt <<EOF\n$(mysql -u root -e 'select 1')\nEOF", DENY),
    ("cat > /tmp/out.txt <<EOF\n$(bench update)\nEOF", DENY),
    ("cat > /tmp/out.md <<EOF\nRésultat : `bench build`\nEOF", DENY),
    ("tee /tmp/out.txt <<EOF\n$(sudo reboot)\nEOF", ASK),
    ("xargs -n1 mysql -e < requetes.txt", DENY),
    # --- Serveur Arkonex autre que DEV : refus
    ("curl https://desk.arkonex.ca/api/method/ping", DENY),
    ("ssh frappe@desk.arkonex.ca ls", DENY),
    ("rsync -a ./x autre.arkonex.ca:/tmp/", DENY),
    ("curl --url=https://desk.arkonex.ca/x", DENY),
    # RELECTURE : git vers un autre serveur Arkonex
    ("git push https://desk.arkonex.ca/repo.git lot/x", DENY),
    ("git remote add autre ssh://frappe@desk.arkonex.ca/repo.git", DENY),
    ("git clone frappe@desk.arkonex.ca:repo.git", DENY),
    # RELECTURE : code (python, heredoc donné à python) visant un autre serveur Arkonex
    ("python3 -c \"import urllib.request; urllib.request.urlopen('http://desk.arkonex.ca/x')\"", ASK),
    ("python3 -c \"import smtplib; smtplib.SMTP('mail.arkonex.ca')\"", ASK),
    ("python3 - <<'EOF'\nimport requests\nrequests.get('https://desk.arkonex.ca/api')\nEOF", ASK),
    # --- Sites bench non autorisés : refus
    ("bench --site all migrate", DENY),
    ("bench --site prod.example.com migrate", DENY),
    ("bench --site $INCONNU migrate", ASK),
    # --- bench migrate / update / build global : refus
    ("bench migrate", DENY),
    ("bench update", DENY),
    ("bench update --reset", DENY),
    ("bench build", DENY),
    # --- Validation humaine
    ("sudo supervisorctl restart all", ASK),
    ("echo '127.0.0.1 deverp.arkonex.ca' | sudo tee -a /etc/hosts", ASK),
    ("sudo -n apt-get install -y gh", ASK),
    ("sudo -u browserqa-refresh node -e 1", ASK),
    ("eval \"sudo reboot\"", ASK),
    ("git push origin main", ASK),
    ("git push origin HEAD:main", ASK),
    ("git push origin refs/heads/master", ASK),
    ("git push --all origin", ASK),
    ("git push --force-with-lease origin lot/x", ASK),
    ("git push -fu origin lot/x", ASK),
    ("git push origin +lot/x", ASK),
    ("cd /home/frappe/l2repro-bench && bench drop-site l2repro.local --force --no-backup", ASK),
]


class GuardBashCases(unittest.TestCase):
    def test_cases(self):
        for command, expected in CASES:
            with self.subTest(command=command):
                verdict = guard_bash.evaluate(command, "/tmp")
                self.assertEqual(verdict[0] if verdict else None, expected, verdict)

    def test_reasons_name_their_rule(self):
        self.assertEqual(guard_bash.evaluate("mysql -e 1", "/tmp")[1], "R-SQL")
        self.assertEqual(guard_bash.evaluate("bench build", "/tmp")[1], "R-BUILD")
        self.assertEqual(guard_bash.evaluate("git push origin main", "/tmp")[1], "R-PUSH-MAIN")

    def test_deny_wins_over_ask(self):
        verdict = guard_bash.evaluate("sudo supervisorctl restart all; mysql -e 1", "/tmp")
        self.assertEqual(verdict[:2], ("deny", "R-SQL"))

    def test_uncertain_split_downgrades_deny_to_ask(self):
        verdict = guard_bash.evaluate("mysql -e 1 'guillemet non ferme", "/tmp")
        self.assertEqual(verdict[0], "ask")
        self.assertIn("incertain", verdict[2])


class GitPushCurrentBranch(unittest.TestCase):
    """git push sans destination explicite : la branche courante décide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        run = lambda *a: subprocess.run(["git", "-C", self.repo] + list(a), check=True,
                                        capture_output=True)
        run("init", "-q", "-b", "main")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "init")
        self.run_git = run

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_from_main_asks(self):
        self.assertEqual(guard_bash.evaluate("git push", self.repo)[0], "ask")
        self.assertEqual(guard_bash.evaluate("git push -q origin HEAD", self.repo)[0], "ask")

    def test_push_from_lot_branch_passes(self):
        self.run_git("switch", "-q", "-c", "lot/x")
        self.assertIsNone(guard_bash.evaluate("git push", self.repo))
        self.assertIsNone(guard_bash.evaluate("git push -u origin HEAD", self.repo))

    def test_cd_then_push_uses_target_directory(self):
        self.assertEqual(guard_bash.evaluate("cd {} && git push".format(self.repo), "/")[0],
                         "ask")
        self.assertEqual(guard_bash.evaluate("git -C {} push".format(self.repo), "/")[0], "ask")


class HookProtocol(unittest.TestCase):
    """Le script lit l'entrée standard et répond au format attendu par Claude Code."""

    def call(self, script, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        r = subprocess.run(["/usr/bin/python3", os.path.join(ROOT, script)], input=raw,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def test_allow_is_silent(self):
        self.assertIsNone(self.call("guard_bash.py", {"tool_input": {"command": "ls"},
                                                      "cwd": "/tmp"}))

    def test_deny_format(self):
        out = self.call("guard_bash.py", {"tool_input": {"command": "mysql -e 1"}, "cwd": "/tmp"})
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith("[OPEN-161 R-SQL]"))

    def test_never_allow(self):
        for cmd in ("ls", "mysql -e 1", "sudo reboot"):
            out = self.call("guard_bash.py", {"tool_input": {"command": cmd}, "cwd": "/tmp"})
            self.assertNotEqual((out or {}).get("permissionDecision"), "allow")

    def test_internal_error_asks_never_silent(self):
        out = self.call("guard_bash.py", "ceci n'est pas du JSON")
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("R-ERREUR", out["permissionDecisionReason"])
        out = self.call("guard_paths.py", "{")
        self.assertEqual(out["permissionDecision"], "ask")


class GuardPaths(unittest.TestCase):
    def test_secrets(self):
        for p in ("/home/frappe/frappe-bench/sites/deverp.arkonex.ca/site_config.json",
                  "/x/.env", "/x/.env.local", ".env"):
            with self.subTest(path=p):
                self.assertEqual(guard_paths.evaluate(p)[0], "deny")

    def test_neighbours_pass(self):
        for p in ("/x/sites/common_site_config.json.md", "/x/site_config.json.bak",
                  "/x/envfile", "/x/.envrc.md", "/x/docs/site_config.md"):
            with self.subTest(path=p):
                self.assertIsNone(guard_paths.evaluate(p))


if __name__ == "__main__":
    unittest.main()
