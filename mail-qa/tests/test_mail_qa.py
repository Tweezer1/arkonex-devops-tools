"""Essais de mail_qa.py et des fichiers d'installation (OPEN-202, Issue #351).

Lancement : python3 -m unittest discover -s mail-qa/tests -v
Aucun réseau extérieur, aucune écriture hors des dossiers temporaires.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import mail_qa  # noqa: E402

CONF = mail_qa.load_conf(os.path.join(ROOT, "mail-qa.conf"))

ORIGINAL = {
	"MessageID": "abc123@deverp.arkonex.ca",
	"Subject": "Demande de prix",
	"From": {"Address": "qa@arkonex.ca"},
	"To": [{"Address": "fournisseur@essai.invalid"}],
}


class TestSyntheticMarks(unittest.TestCase):
	def test_reply_carries_all_marks_and_thread(self):
		msg = mail_qa.build_reply(ORIGINAL, "Notre prix : 100 $")
		self.assertEqual(msg["X-Arkonex-Essai"], "synthetique")
		self.assertTrue(msg["Subject"].startswith("[ESSAI SYNTHÉTIQUE] Re: Demande de prix"))
		self.assertIn("fournisseur@essai.invalid", msg["From"])
		self.assertEqual(msg["To"], "qa@arkonex.ca")
		self.assertEqual(msg["In-Reply-To"], "<abc123@deverp.arkonex.ca>")
		self.assertEqual(msg["References"], "<abc123@deverp.arkonex.ca>")
		body = msg.get_body(preferencelist=("plain",)).get_content()
		self.assertEqual(body.splitlines()[0], mail_qa.MARK_BODY_FIRST_LINE)
		self.assertTrue(msg["Message-ID"].endswith("@essai.invalid>"))

	def _mutated(self, field):
		msg = mail_qa.build_reply(ORIGINAL)
		if field == "header":
			del msg["X-Arkonex-Essai"]
		elif field == "subject":
			msg.replace_header("Subject", "Re: Demande de prix")
		elif field == "from":
			msg.replace_header("From", "fournisseur@exemple.com")
		elif field == "body":
			msg.set_content("Bonjour\n")
		return msg

	def test_each_missing_mark_is_refused_with_its_name(self):
		expected = {
			"header": "en-tête X-Arkonex-Essai",
			"subject": "préfixe du sujet",
			"from": "expéditeur @essai.invalid",
			"body": "première ligne du corps",
		}
		for field, reason in expected.items():
			with self.subTest(field=field):
				with self.assertRaises(mail_qa.MarkError) as ctx:
					mail_qa.check_marks(self._mutated(field))
				self.assertIn(reason, str(ctx.exception))

	def test_from_must_be_reserved_domain(self):
		with self.assertRaises(ValueError) as ctx:
			mail_qa.build_reply(ORIGINAL, from_addr="client@arkonex.ca")
		self.assertIn("@essai.invalid", str(ctx.exception))

	def test_original_without_reserved_recipient_requires_from(self):
		original = dict(ORIGINAL, To=[{"Address": "personne@arkonex.ca"}])
		with self.assertRaises(ValueError) as ctx:
			mail_qa.build_reply(original)
		self.assertIn("--from", str(ctx.exception))
		msg = mail_qa.build_reply(original, from_addr="client@essai.invalid")
		self.assertIn("client@essai.invalid", msg["From"])

	def test_attachment_is_kept(self):
		with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as fh:
			fh.write(b"%PDF-1.4 essai")
		try:
			msg = mail_qa.build_reply(ORIGINAL, attachments=[fh.name])
			names = [part.get_filename() for part in msg.iter_attachments()]
			self.assertEqual(names, [os.path.basename(fh.name)])
		finally:
			os.unlink(fh.name)


def site(accounts=(), site_conf=None, hooks=(), signals=None, name="deverp.arkonex.ca"):
	return mail_qa.evaluate_site(name, CONF, site_conf or {}, list(accounts), list(hooks), signals or {})


def statuses(results):
	return [status for status, _ in results]


QA_ACCOUNT = {
	"name": "QA Arkonex", "service": "", "email_id": "qa@arkonex.ca", "enable_outgoing": 1,
	"enable_incoming": 1, "smtp_server": "127.0.0.1", "smtp_port": 1025, "email_server": "mail.arkonex.ca",
}


class TestSiteRules(unittest.TestCase):
	def test_compliant_dev_site(self):
		res = site([QA_ACCOUNT])
		self.assertEqual(statuses(res), ["PASS"])

	def test_outgoing_to_external_server_fails(self):
		acc = dict(QA_ACCOUNT, smtp_server="mail.arkonex.ca", smtp_port=587)
		res = site([acc])
		self.assertIn("FAIL", statuses(res))
		self.assertIn("pas vers Mailpit", res[0][1])

	def test_frappe_mail_service_fails_even_disabled(self):
		acc = dict(QA_ACCOUNT, service="Frappe Mail", enable_outgoing=0, enable_incoming=0)
		res = site([acc])
		self.assertIn("Frappe Mail", res[0][1])
		self.assertEqual(res[0][0], "FAIL")

	def test_incoming_professional_mailbox_fails(self):
		acc = dict(QA_ACCOUNT, name="Pro", email_id="direction@arkonex.ca", enable_outgoing=0)
		res = site([acc])
		self.assertEqual(res[0][0], "FAIL")
		self.assertIn("seule la boîte QA", res[0][1])

	def test_incoming_login_id_is_checked(self):
		acc = dict(QA_ACCOUNT, login_id="direction@arkonex.ca", login_id_is_different=1, enable_outgoing=0)
		self.assertEqual(site([acc])[0][0], "FAIL")

	def test_qa_inbox_reserved_to_dev(self):
		res = site([QA_ACCOUNT], name="l2mdb.local")
		self.assertEqual(res[0][0], "FAIL")
		self.assertIn("réservée à deverp.arkonex.ca", res[0][1])

	def test_mail_server_in_site_config(self):
		self.assertEqual(site(site_conf={"mail_server": "mail.arkonex.ca"})[0][0], "FAIL")
		self.assertEqual(statuses(site(site_conf={"mail_server": "127.0.0.1", "mail_port": 1025})), ["PASS"])

	def test_override_email_send_hook_fails(self):
		res = site(hooks=["app.x.send"])
		self.assertEqual(res[0][0], "FAIL")
		self.assertIn("override_email_send", res[0][1])

	def test_https_channels_are_signalled_not_passed_silently(self):
		res = site(signals={"Webhook": 2, "SMS Settings": 0})
		self.assertIn(("SIGNAL"), statuses(res))
		self.assertTrue(any("Webhook" in text for _, text in res))


UNIT_OK = {
	"ExecStart": "{ path=/opt/arkonex-mail-qa/bin/mailpit ; argv[]=/opt/arkonex-mail-qa/bin/mailpit --listen 127.0.0.1:8025 --smtp 127.0.0.1:1025 ; }",
	"Environment": "", "EnvironmentFiles": "", "ActiveState": "active", "UnitFileState": "enabled",
	"IPAddressDeny": "0.0.0.0/0 ::/0",
}


class TestMailpitRules(unittest.TestCase):
	def test_compliant(self):
		res = mail_qa.evaluate_mailpit_unit(UNIT_OK, {"MessageRelay": {"Enabled": False}}, [])
		self.assertEqual(set(statuses(res)), {"PASS"})

	# Liste écrite en dur, indépendante du code : retirer un drapeau du code doit échouer ici.
	EXPECTED_FORBIDDEN = ("--smtp-relay-config", "--smtp-relay-all", "--smtp-relay-matching", "--smtp-forward-config", "--webhook-url")

	def test_each_forbidden_flag_fails(self):
		for flag in self.EXPECTED_FORBIDDEN:
			with self.subTest(flag=flag):
				unit = dict(UNIT_OK, ExecStart=UNIT_OK["ExecStart"].replace(" ; }", " " + flag + " x ; }"))
				res = mail_qa.evaluate_mailpit_unit(unit, {"MessageRelay": {"Enabled": False}}, [])
				self.assertTrue(any(s == "FAIL" and flag in t for s, t in res))

	def test_option_forms_and_duplicates_are_parsed_not_substring_matched(self):
		cases = {
			"--smtp-relay-all=true": "relais",
			"--smtp 0.0.0.0:1025": "local",
			"-s [::]:1025": "local",
			"--pop3 127.0.0.1:1110": "local",
		}
		for extra, word in cases.items():
			with self.subTest(extra=extra):
				unit = dict(UNIT_OK, ExecStart=UNIT_OK["ExecStart"].replace(" ; }", " " + extra + " ; }"))
				res = mail_qa.evaluate_mailpit_unit(unit, {"MessageRelay": {"Enabled": False}}, [])
				self.assertTrue(any(s == "FAIL" and word in t for s, t in res), res)

	def test_network_confinement_of_unit_required(self):
		res = mail_qa.evaluate_mailpit_unit(dict(UNIT_OK, IPAddressDeny=""), {"MessageRelay": {"Enabled": False}}, [])
		self.assertTrue(any(s == "FAIL" and "IPAddressDeny" in t for s, t in res))

	def test_mp_environment_fails(self):
		unit = dict(UNIT_OK, Environment="MP_SMTP_RELAY_ALL=true")
		res = mail_qa.evaluate_mailpit_unit(unit, {"MessageRelay": {"Enabled": False}}, [])
		self.assertTrue(any(s == "FAIL" and "MP_" in t for s, t in res))

	def test_relay_enabled_in_api_fails(self):
		res = mail_qa.evaluate_mailpit_unit(UNIT_OK, {"MessageRelay": {"Enabled": True}}, [])
		self.assertTrue(any(s == "FAIL" and "relais" in t for s, t in res))

	def test_api_unreachable_fails(self):
		res = mail_qa.evaluate_mailpit_unit(UNIT_OK, None, [])
		self.assertTrue(any(s == "FAIL" and "injoignable" in t for s, t in res))

	def test_stray_mailpit_process_fails(self):
		res = mail_qa.evaluate_mailpit_unit(UNIT_OK, {"MessageRelay": {"Enabled": False}}, ["/tmp/mailpit --smtp-relay-all"])
		self.assertTrue(any(s == "FAIL" and "processus" in t for s, t in res))

	def test_shipped_unit_has_no_forbidden_flag_and_listens_locally(self):
		with open(os.path.join(ROOT, "systemd", "mailpit-qa.service"), encoding="utf-8") as fh:
			text = fh.read()
		exec_line = [line for line in text.splitlines() if line.startswith("ExecStart=")][0]
		for flag in TestMailpitRules.EXPECTED_FORBIDDEN:
			self.assertNotIn(flag, exec_line)
		self.assertIn("--smtp 127.0.0.1:1025", exec_line)
		self.assertIn("--listen 127.0.0.1:8025", exec_line)
		self.assertIn("UnsetEnvironment=", text)
		self.assertIn("IPAddressDeny=any", text)
		self.assertIn("IPAddressAllow=localhost", text)
		for var in ("MP_SMTP_RELAY_HOST", "MP_SMTP_FORWARD_TO", "MP_WEBHOOK_URL"):
			self.assertIn(var, text)
		self.assertIn("User=mailqa", text)


class TestFirewallFiles(unittest.TestCase):
	def test_rule_blocks_smtp_ports_and_spares_loopback(self):
		with open(os.path.join(ROOT, "nftables", "arkonex-mailqa.nft"), encoding="utf-8") as fh:
			text = fh.read()
		self.assertIn("table inet arkonex_mailqa", text)
		self.assertIn("tcp dport { 25, 465, 587, 2525 } counter reject with tcp reset", text)
		self.assertIn('oifname "lo" accept', text)
		self.assertIn("hook output", text)

	def test_persistence_checks(self):
		tmp = tempfile.mkdtemp()
		try:
			conf = os.path.join(tmp, "nftables.conf")
			rule = os.path.join(tmp, "rule.nft")
			shutil.copy(os.path.join(ROOT, "nftables", "arkonex-mailqa.nft"), rule)
			with open(conf, "w", encoding="utf-8") as fh:
				fh.write("flush ruleset\n" + mail_qa.NFT_INCLUDE_LINE + "\n")
			ok = mail_qa.check_persistence(conf, rule, systemctl=lambda a: "enabled\n")
			self.assertEqual(set(statuses(ok)), {"PASS"})
			disabled = mail_qa.check_persistence(conf, rule, systemctl=lambda a: "disabled\n")
			self.assertEqual(disabled[0][0], "FAIL")
			with open(conf, "w", encoding="utf-8") as fh:
				fh.write(mail_qa.NFT_INCLUDE_LINE + "\nflush ruleset\n")
			flushed_after = mail_qa.check_persistence(conf, rule, systemctl=lambda a: "enabled\n")
			self.assertEqual(flushed_after[1][0], "FAIL")
			with open(rule, "a", encoding="utf-8") as fh:
				fh.write("# modifié\n")
			drift = mail_qa.check_persistence(conf, rule, systemctl=lambda a: "enabled\n")
			self.assertEqual(drift[2][0], "FAIL")
		finally:
			shutil.rmtree(tmp)


class TestTcpOpen(unittest.TestCase):
	def test_open_and_closed_ports_without_sending_data(self):
		server = socket.socket()
		server.bind(("127.0.0.1", 0))
		server.listen(1)
		port = server.getsockname()[1]
		received = []

		def accept():
			conn, _ = server.accept()
			conn.settimeout(2)
			try:
				received.append(conn.recv(10))
			except OSError:
				received.append(b"?")
			conn.close()

		thread = threading.Thread(target=accept)
		thread.start()
		self.assertEqual(mail_qa.tcp_open("127.0.0.1", port), [("127.0.0.1", socket.AF_INET, mail_qa.OPEN)])
		thread.join(5)
		server.close()
		self.assertEqual(received, [b""])  # rien n'a été envoyé avant la fermeture
		self.assertEqual(mail_qa.tcp_open("127.0.0.1", port, timeout=1), [("127.0.0.1", socket.AF_INET, mail_qa.REFUSED)])

	def test_error_classification(self):
		import errno
		self.assertEqual(mail_qa.classify_connect_error(ConnectionRefusedError()), mail_qa.REFUSED)
		self.assertEqual(mail_qa.classify_connect_error(ConnectionResetError()), mail_qa.REFUSED)
		self.assertEqual(mail_qa.classify_connect_error(TimeoutError()), mail_qa.TIMEOUT)
		self.assertEqual(mail_qa.classify_connect_error(OSError(errno.ENETUNREACH, "x")), mail_qa.NO_ROUTE)

	def test_only_active_refusal_proves_p1(self):
		v4, v6 = socket.AF_INET, socket.AF_INET6
		probe = mail_qa.evaluate_smtp_probe
		self.assertEqual(probe("h", 587, "1.2.3.4", v4, mail_qa.REFUSED)[0], "PASS")
		self.assertEqual(probe("h", 587, "1.2.3.4", v4, mail_qa.OPEN)[0], "FAIL")
		self.assertEqual(probe("h", 25, "1.2.3.4", v4, mail_qa.TIMEOUT)[0], "FAIL")
		self.assertIn("non probant", probe("h", 25, "1.2.3.4", v4, mail_qa.TIMEOUT)[1])
		self.assertEqual(probe("h", 25, "1.2.3.4", v4, mail_qa.NO_ROUTE)[0], "FAIL")
		self.assertEqual(probe("h", 465, "::1", v6, mail_qa.NO_ROUTE)[0], "INFO")
		self.assertEqual(probe("h", 465, "::1", v6, mail_qa.TIMEOUT)[0], "FAIL")


class TestIterSites(unittest.TestCase):
	def test_every_site_of_every_bench(self):
		tmp = tempfile.mkdtemp()
		try:
			for bench, sites in (("b1", ["a.local", "b.local"]), ("b2", ["c.local"])):
				os.makedirs(os.path.join(tmp, bench, "sites"))
				open(os.path.join(tmp, bench, "sites", "apps.txt"), "w").close()
				for s in sites:
					os.makedirs(os.path.join(tmp, bench, "sites", s))
					open(os.path.join(tmp, bench, "sites", s, "site_config.json"), "w").close()
			os.makedirs(os.path.join(tmp, "b1", "sites", "assets"))
			found = [(os.path.basename(b), s) for b, s in mail_qa.iter_sites(os.path.join(tmp, "*", "sites", "apps.txt"))]
			self.assertEqual(found, [("b1", "a.local"), ("b1", "b.local"), ("b2", "c.local")])
		finally:
			shutil.rmtree(tmp)


class TestInstallPlanMode(unittest.TestCase):
	def run_install(self, *args, env_extra=None):
		env = dict(os.environ, **(env_extra or {}))
		return subprocess.run(["bash", os.path.join(ROOT, "install.sh"), *args], capture_output=True, text=True, env=env)

	def test_plan_mode_writes_nothing(self):
		tmp = tempfile.mkdtemp()
		try:
			conf = os.path.join(tmp, "nftables.conf")
			with open(conf, "w") as fh:
				fh.write("flush ruleset\n")
			env = {"MAILQA_NFT_CONF": conf, "MAILQA_NFT_DIR": os.path.join(tmp, "d")}
			for cmd in ("firewall", "mailpit", "mailpit-stop"):
				with self.subTest(cmd=cmd):
					proc = self.run_install(cmd, env_extra=env)
					self.assertEqual(proc.returncode, 0, proc.stderr)
					self.assertIn("[plan]", proc.stdout)
			with open(conf) as fh:
				self.assertEqual(fh.read(), "flush ruleset\n")
			self.assertFalse(os.path.exists(os.path.join(tmp, "d")))
		finally:
			shutil.rmtree(tmp)

	def test_apply_requires_root(self):
		if os.geteuid() == 0:
			self.skipTest("lancé en root")
		proc = self.run_install("firewall", "--apply")
		self.assertEqual(proc.returncode, 1)
		self.assertIn("exige root", proc.stderr)

	def test_unknown_command(self):
		self.assertEqual(self.run_install("relay").returncode, 2)

	def test_no_firewall_removal_command(self):
		with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as fh:
			text = fh.read()
		self.assertNotIn("firewall-remove", text)
		self.assertNotIn("delete table inet arkonex_mailqa\n\t\tlog", text)


if __name__ == "__main__":
	unittest.main()


class TestGuards(unittest.TestCase):
	def test_imap_tls_verifies_certificate_and_hostname(self):
		import ssl
		ctx = mail_qa.imap_ssl_context()
		self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
		self.assertTrue(ctx.check_hostname)

	def test_qa_account_guard(self):
		ok = {"name": "QA Arkonex", "email_id": "qa@arkonex.ca", "email_server": "mail.arkonex.ca"}
		mail_qa.guard_qa_account(ok, CONF, "deverp.arkonex.ca")
		refused = {
			"boîte professionnelle": (dict(ok, email_id="direction@arkonex.ca"), "deverp.arkonex.ca"),
			"login différent": (dict(ok, login_id="direction@arkonex.ca", login_id_is_different=1), "deverp.arkonex.ca"),
			"autre serveur": (dict(ok, email_server="imap.gmail.com"), "deverp.arkonex.ca"),
			"banc": (ok, "l2mdb.local"),
		}
		for label, (acc, site_name) in refused.items():
			with self.subTest(label=label):
				with self.assertRaises(SystemExit) as ctx:
					mail_qa.guard_qa_account(acc, CONF, site_name)
				self.assertIn("refus", str(ctx.exception))

	def test_gather_site_fails_when_a_required_field_is_missing(self):
		class Meta:
			def has_field(self, f):
				return f != "service"

		class Fake:
			def get_meta(self, dt):
				return Meta()

		with self.assertRaises(RuntimeError) as ctx:
			mail_qa.gather_site(Fake())
		self.assertIn("service", str(ctx.exception))
