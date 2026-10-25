"""Essais de la boucle de corrections U2 de la tranche T2 (OPEN-202, Issue #351).

Même doublure de Frappe que test_mail_qa_t2.py. Aucun réseau, aucun site.
"""

import contextlib
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mail_qa  # noqa: E402
import mail_qa_parties as parties  # noqa: E402
from test_mail_qa_t2 import CONF, ORIGINAL, FakeFrappe, FakeMailpit, Link, PreflightCase, msg, runner  # noqa: E402


@contextlib.contextmanager
def stub_modules(**mods):
	"""Installe des modules factices (et leurs paquets parents) dans sys.modules, puis les retire."""
	saved = {}

	def put(name, module):
		saved.setdefault(name, sys.modules.get(name))
		sys.modules[name] = module

	for full, attrs in mods.items():
		parts = full.split(".")
		for i in range(1, len(parts)):
			parent = ".".join(parts[:i])
			if parent not in sys.modules and parent not in mods:
				pkg = types.ModuleType(parent)
				pkg.__path__ = []
				put(parent, pkg)
		module = types.ModuleType(full)
		for k, v in attrs.items():
			setattr(module, k, v)
		put(full, module)
	try:
		yield
	finally:
		for name, old in saved.items():
			if old is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = old


QA_ROW = {"name": "QA Arkonex", "email_id": "qa@arkonex.ca", "email_server": "mail.arkonex.ca", "service": "",
	"enable_incoming": 0, "enable_outgoing": 0, "use_imap": 0, "smtp_server": "", "smtp_port": ""}


# --- M1 : ensemble exact d'identifiants d'essai -------------------------------------


class TestExactEssaiSet(PreflightCase):
	def setUp(self):
		super().setUp()
		self.mp = FakeMailpit({
			"A1": msg(to=["client+lota@essai.invalid"]),
			"AB": msg(to=["x+lota@essai.invalid", "y+lotb@essai.invalid"]),
			"B1": msg(to=["client+lotb@essai.invalid"]),
			"H1": msg(to=["client@essai.invalid"]),          # adresses nues, en-tête de l'essai A
			"HB": msg(to=["client+lotb@essai.invalid"]),     # en-tête A mais adresse B
			"N1": msg(to=["client@essai.invalid"]),
		})
		self.mp.headers = {"H1": {"X-Arkonex-Essai-Id": ["lota"]}, "HB": {"X-Arkonex-Essai-Id": ["lota"]}}
		self.mp.install()

	def tearDown(self):
		self.mp.restore()
		super().tearDown()

	def test_message_with_two_essai_ids_is_deleted_by_neither(self):
		for essai in ("lota", "lotb"):
			out = mail_qa.purge_essai(CONF, essai)
			self.assertNotIn("AB", out["messages"], essai)
			self.assertNotIn("HB", out["messages"], essai)
		self.assertNotIn("AB", self.mp.deleted)
		self.assertNotIn("HB", self.mp.deleted)
		self.assertNotIn("N1", self.mp.deleted)

	def test_purge_a_takes_its_own_including_header_only_messages(self):
		out = mail_qa.purge_essai(CONF, "lota", dry_run=True)
		self.assertEqual(sorted(out["messages"]), ["A1", "H1"])
		self.assertEqual(sorted(mail_qa.purge_essai(CONF, "lotb", dry_run=True)["messages"]), ["B1"])

	def test_captured_uses_the_same_exact_logic(self):
		self.assertEqual(sorted(i["id"] for i in mail_qa.captured(CONF, essai="lota")), ["A1", "H1"])
		self.assertEqual(sorted(i["id"] for i in mail_qa.captured(CONF, essai="lotb")), ["B1"])

	def test_fabricated_message_with_bare_addresses_carries_the_essai_header(self):
		message = mail_qa.build_reply(ORIGINAL, from_addr="fournisseur@essai.invalid", essai="lot7")
		self.assertEqual(message["X-Arkonex-Essai-Id"], "lot7")
		item = mail_qa.summarize_message({"From": {"Address": "fournisseur@essai.invalid"}}, headers={"X-Arkonex-Essai-Id": ["lot7"]})
		self.assertEqual(item["essais"], ["lot7"])
		self.assertTrue(mail_qa.belongs_only_to(item, "lot7"))


# --- M2 : câblage de guard_qa_account -----------------------------------------------


class TestQaGuardWiring(PreflightCase):
	def run_case(self, entry, row, site):
		calls = []

		def boom(*a, **k):
			calls.append(a)
			raise AssertionError("accès réseau ou relève appelé malgré la garde")

		frappe = FakeFrappe(site=site, tables={"Email Account": [row]})
		message = mail_qa.build_reply(ORIGINAL)
		mods = {
			"imaplib": {"IMAP4_SSL": boom, "Time2Internaldate": boom},
			"frappe.utils.password": {"get_decrypted_password": boom},
			"frappe.email.doctype.email_account.email_account": {"pull_from_email_account": boom},
		}
		with stub_modules(**mods):
			with self.assertRaises(SystemExit) as ctx:
				if entry == "imap":
					mail_qa.imap_append(frappe, CONF, message, row["name"])
				else:
					mail_qa.pull_account(frappe, CONF, row["name"])
		self.assertEqual(calls, [], "aucun imaplib, aucun mot de passe, aucune relève avant la garde")
		return str(ctx.exception)

	def test_non_qa_account_is_refused_before_any_network(self):
		for entry in ("imap", "pull"):
			with self.subTest(entry=entry):
				row = dict(QA_ROW, name="Autre", email_id="autre@essai.invalid")
				self.assertIn("ne vise pas", self.run_case(entry, row, "deverp.arkonex.ca"))

	def test_qa_account_on_a_bench_site_is_refused_before_any_network(self):
		for entry in ("imap", "pull"):
			with self.subTest(entry=entry):
				self.assertIn("réservée", self.run_case(entry, dict(QA_ROW), "l2repro.local"))

	def test_imap_refuses_a_heavy_part_before_any_network(self):
		calls = []
		boom = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("réseau"))
		frappe = FakeFrappe(site="deverp.arkonex.ca", tables={"Email Account": [dict(QA_ROW)]})
		heavy = mail_qa.build_reply(ORIGINAL, variants=("piece-lourde",), heavy_bytes=2 * 1024 * 1024)
		with stub_modules(imaplib={"IMAP4_SSL": boom, "Time2Internaldate": boom}, **{"frappe.utils.password": {"get_decrypted_password": boom}}):
			with self.assertRaises(ValueError) as ctx:
				mail_qa.imap_append(frappe, CONF, heavy, "QA Arkonex", ("piece-lourde",))
		self.assertIn("1 Mio", str(ctx.exception))
		self.assertEqual(calls, [])


class TestImapAttachments(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		self.dir = os.path.join(self.tmp, "pieces")
		os.makedirs(self.dir)
		self.ok = os.path.join(self.dir, "petite.txt")
		with open(self.ok, "wb") as fh:
			fh.write(b"x" * 100)
		self.outside = os.path.join(self.tmp, "dehors.txt")
		with open(self.outside, "wb") as fh:
			fh.write(b"x")

	def tearDown(self):
		shutil.rmtree(self.tmp)

	def test_only_small_files_under_the_declared_directory(self):
		mail_qa.check_attachment_paths([self.ok], self.dir)
		with self.assertRaises(ValueError):
			mail_qa.check_attachment_paths([self.outside], self.dir)
		with self.assertRaises(ValueError):
			mail_qa.check_attachment_paths([self.dir + "/../dehors.txt"], self.dir)
		os.symlink(self.outside, os.path.join(self.dir, "lien.txt"))
		with self.assertRaises(ValueError):
			mail_qa.check_attachment_paths([os.path.join(self.dir, "lien.txt")], self.dir)
		big = os.path.join(self.dir, "grosse.bin")
		with open(big, "wb") as fh:
			fh.write(b"0" * (1024 * 1024 + 1))
		with self.assertRaises(ValueError):
			mail_qa.check_attachment_paths([big], self.dir)
		with self.assertRaises(ValueError):
			mail_qa.check_attachment_paths([self.ok], "")  # dossier non déclaré
		mail_qa.check_attachment_paths([], "")  # rien à joindre : rien à refuser

	def test_builders_apply_the_rule_when_a_directory_is_given(self):
		mail_qa.build_reply(ORIGINAL, attachments=[self.ok], attach_dir=self.dir)
		with self.assertRaises(ValueError):
			mail_qa.build_reply(ORIGINAL, attachments=[self.outside], attach_dir=self.dir)
		with self.assertRaises(ValueError):
			mail_qa.build_inbound(to_addr="b@essai.invalid", from_addr="c@essai.invalid", attachments=[self.outside], attach_dir=self.dir)
		mail_qa.build_reply(ORIGINAL, attachments=[self.outside])  # --via inject : libre

	def test_conf_declares_the_directory(self):
		self.assertEqual(CONF["IMAP_ATTACH_DIR"], "/home/frappe/mail-qa-pieces")


# --- Mineurs ------------------------------------------------------------------------


class TestMinorFixes(PreflightCase):
	def test_corrupt_or_non_dict_cache_is_ignored(self):
		run = mail_qa._VERIFY_RUNNER
		mail_qa.require_preflight(CONF, now=1000.0)
		for content in ("[]", "garbage{", "null", '"x"', "42"):
			with self.subTest(content=content):
				with open(mail_qa.PREFLIGHT_CACHE_PATH, "w") as fh:
					fh.write(content)
				os.chmod(mail_qa.PREFLIGHT_CACHE_PATH, 0o600)
				before = len(run.calls)
				self.assertEqual(mail_qa.require_preflight(CONF, now=1000.0 + 5), "verify")
				self.assertEqual(len(run.calls), before + 1)

	def contact_site(self):
		return FakeFrappe(tables={
			"Contact Email": [{"parent": "Marie", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [{"parent": "Marie", "parenttype": "Contact", "link_doctype": "Customer", "link_name": "ESSAI — C"}],
		})

	def test_signal_also_covers_to_and_cc(self):
		site = self.contact_site()
		to_contact = mail_qa.build_inbound(to_addr="moa-a-1@essai.invalid", from_addr="inconnu@essai.invalid", conf=CONF)
		out = mail_qa.message_signals(site, to_contact)
		self.assertEqual([(s["adresse"], s["role"]) for s in out], [("moa-a-1@essai.invalid", "destinataire")])
		cc_contact = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="inconnu@essai.invalid", conf=CONF)
		cc_contact["Cc"] = "moa-a-1@essai.invalid"
		self.assertEqual([s["role"] for s in mail_qa.message_signals(site, cc_contact)], ["destinataire"])
		sender = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="moa-a-1@essai.invalid", conf=CONF)
		self.assertEqual([s["role"] for s in mail_qa.message_signals(site, sender)], ["expéditeur"])
		none = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="inconnu@essai.invalid", conf=CONF)
		self.assertEqual(mail_qa.message_signals(site, none), [])

	def test_flush_needs_an_explicit_choice(self):
		frappe = FakeFrappe(tables={"Email Account": []})
		with self.assertRaises(mail_qa.GuardError) as ctx:
			mail_qa.flush_queue(frappe, CONF)
		self.assertIn("--all", str(ctx.exception))
		self.assertEqual(frappe.touched, [])
		for kw in ({"all_": True}, {"names": ["Q1"]}, {"since": "2026-10-25"}):
			with self.subTest(kw=kw):
				self.assertEqual(mail_qa.flush_queue(FakeFrappe(tables={"Email Account": []}), CONF, **kw), [])

	def test_inbound_target_is_checked_by_the_api_builder(self):
		for target in ("direction@arkonex.ca", "x@essai.invalid.com", ""):
			with self.subTest(target=target):
				with self.assertRaises(ValueError):
					mail_qa.build_inbound(to_addr=target, from_addr="c@essai.invalid")
		mail_qa.build_inbound(to_addr="qa@arkonex.ca", from_addr="c@essai.invalid")
		mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="c@essai.invalid")

	def test_unknown_doctype_in_the_window_is_an_error_not_an_empty_list(self):
		frappe = FakeFrappe()
		frappe.doctypes = {"Communication", "Email Queue", "File", "ToDo", "Notification Log"}
		with self.assertRaises(mail_qa.GuardError) as ctx:
			mail_qa.collect_effects(frappe, "2026-10-25", doctypes=("Inconnu",))
		self.assertIn("Inconnu", str(ctx.exception))
		self.assertEqual(mail_qa.collect_effects(frappe, "2026-10-25")["autres"], {})

	def test_no_site_is_not_an_empty_pass(self):
		tmp = tempfile.mkdtemp()
		try:
			out = mail_qa.check_sites(dict(CONF, BENCH_GLOB=os.path.join(tmp, "*", "sites", "apps.txt")))
		finally:
			shutil.rmtree(tmp)
		self.assertEqual(out[0][0], "FAIL")
		self.assertIn("aucun site contrôlé", out[0][1])


class TestInboxIsNeverActive(PreflightCase):
	def dev_site(self, **extra):
		row = {"name": "Boîte", "email_id": "boite@essai.invalid", "service": "", "enable_incoming": 0, "enable_outgoing": 0,
			"use_imap": 0, "smtp_server": "", "smtp_port": "", "login_id": "qa@arkonex.ca", "login_id_is_different": 1,
			"email_server": "mail.arkonex.ca"}
		row.update(extra)
		return FakeFrappe(site="deverp.arkonex.ca", tables={"Email Account": [row]})

	def test_existing_account_with_incoming_enabled_is_not_reused(self):
		frappe = self.dev_site(enable_incoming=1)
		with self.assertRaises(mail_qa.GuardError) as ctx:
			mail_qa.ensure_inbox(frappe, CONF, "boite@essai.invalid")
		self.assertIn("pas une boîte simulée", str(ctx.exception))

	def test_existing_account_with_outgoing_enabled_is_not_reused(self):
		frappe = self.dev_site(enable_outgoing=1, smtp_server="127.0.0.1", smtp_port=1025)
		with self.assertRaises(mail_qa.GuardError) as ctx:
			mail_qa.ensure_inbox(frappe, CONF, "boite@essai.invalid")
		self.assertIn("pas une boîte simulée", str(ctx.exception))


class TestWindowActionList(unittest.TestCase):
	def test_window_refuses_non_mail_commands_before_touching_the_site(self):
		import io
		from contextlib import redirect_stdout

		connected = []
		original = mail_qa._frappe_connect
		mail_qa._frappe_connect = lambda *a, **k: connected.append(a) or (_ for _ in ()).throw(AssertionError("connexion"))
		try:
			for action in (["purge", "--essai", "x"], ["verify"], ["parties", "ensure"], ["visibility"], ["window"], []):
				with self.subTest(action=action):
					err = io.StringIO()
					old = sys.stderr
					sys.stderr = err
					try:
						with redirect_stdout(io.StringIO()):
							code = mail_qa.main(["window", "--site", "l2repro.local", "--", *action])
					finally:
						sys.stderr = old
					self.assertEqual(code, 1)
					self.assertIn("REFUS", err.getvalue())
		finally:
			mail_qa._frappe_connect = original
		self.assertEqual(connected, [])


# --- #347 : le comportement, pas seulement l'absence des noms -----------------------


class TestAgreementGuardLifted(PreflightCase):
	def test_attachment_to_a_formerly_protected_doctype_is_not_refused_and_is_signalled(self):
		frappe = FakeFrappe(tables={
			"Email Account": [{"name": "Boîte", "email_id": "boite@essai.invalid", "service": "", "enable_incoming": 0,
				"enable_outgoing": 0, "use_imap": 0, "smtp_server": "", "smtp_port": ""}],
			"Contact Email": [{"parent": "Marie", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [{"parent": "Marie", "parenttype": "Contact", "link_doctype": "Customer", "link_name": "ESSAI — C"}],
		})
		for doctype, name in (("Purchase Order", "PO-1"), ("Project", "P-1"), ("Contract", "C-1")):
			with self.subTest(doctype=doctype):
				communication = types.SimpleNamespace(name="COM-1", reference_doctype=doctype, reference_name=name,
					get=lambda key, default=None: [Link(link_doctype="Project", link_name="P-9")])

				class Inbound:
					def __init__(self, raw, account):
						pass

					def process(self):
						return communication

				with stub_modules(**{"frappe.email.receive": {"InboundMail": Inbound}}):
					message = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="moa-a-1@essai.invalid", conf=CONF)
					result = mail_qa.deliver(frappe, CONF, message, "inject", "Boîte")
				self.assertEqual(result["reference"], [doctype, name])
				self.assertEqual(result["signalements"][0]["role"], "expéditeur")
				self.assertTrue(result["signalements"][0]["piece_d_accord_possible"])
				self.assertEqual(result["signalements"][0]["contacts"][0]["liens"][0]["nom"], "ESSAI — C")
		self.assertIn("commit", frappe.events)
		for name in ("PROTECTED_REFERENCE_DOCTYPES", "protected_refs", "guard_not_protected", "parent_refs"):
			self.assertFalse(hasattr(mail_qa, name), name)


# --- C11 : tout ou rien, marqueur, messages ----------------------------------------


class TestC11Hardening(PreflightCase):
	def site(self, **kw):
		tables = {"Email Account": []}
		tables.update(kw.pop("tables", {}))
		existing = {("Customer Group", g) for g in ("Commercial", "Government")} | {("Supplier Group", g) for g in ("Local", "Services", "Hardware")} | {("Territory", "Canada")}
		return FakeFrappe(tables=tables, existing=existing, **kw)

	def test_all_or_nothing(self):
		frappe = self.site()
		frappe.fail_after = 3  # la quatrième écriture échoue, au milieu du catalogue
		before = {k: len(v) for k, v in frappe.tables.items()}
		with self.assertRaises(RuntimeError):
			parties.ensure_parties(frappe, CONF)
		self.assertGreater(frappe.inserts, 3)
		self.assertEqual({k: len(v) for k, v in frappe.tables.items()}, before, "écritures partielles restées")
		self.assertIn("rollback:None", frappe.events)
		self.assertNotIn("commit", frappe.events)

	def test_created_parties_carry_the_marker(self):
		frappe = self.site()
		parties.ensure_parties(frappe, CONF)
		for row in frappe.tables["Customer"]:
			self.assertEqual(row["customer_details"], parties.MARKER)
		for row in frappe.tables["Supplier"]:
			self.assertEqual(row["supplier_details"], parties.MARKER)

	def test_a_real_party_with_a_catalog_name_is_never_completed(self):
		for doctype, field in (("Customer", "customer_name"), ("Supplier", "supplier_name")):
			with self.subTest(doctype=doctype):
				name = "ESSAI — Arkonex — Maître de l'ouvrage A" if doctype == "Customer" else "ESSAI — Arkonex — Fournisseur F"
				frappe = self.site(tables={doctype: [{"name": name, field: name}]})  # sans marqueur
				with self.assertRaises(mail_qa.GuardError) as ctx:
					parties.ensure_parties(frappe, CONF)
				self.assertIn("marqueur", str(ctx.exception))
				self.assertIn("À faire", str(ctx.exception))
				self.assertEqual(frappe.writes, [])

	def test_contact_already_linked_to_an_unmarked_party_is_refused(self):
		name = "ESSAI — Client réel renommé"  # préfixe mais pas créé par l'outil : pas de marqueur
		frappe = self.site(tables={
			"Customer": [{"name": name, "customer_name": name}],
			"Contact Email": [{"parent": "C-X", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [{"parent": "C-X", "parenttype": "Contact", "link_doctype": "Customer", "link_name": name}],
		})
		with self.assertRaises(mail_qa.GuardError) as ctx:
			parties.ensure_parties(frappe, CONF, company="Arkonex")
		self.assertEqual(frappe.writes, [])
		self.assertIn("C-X", str(ctx.exception))

	def test_refusal_message_says_what_to_do(self):
		frappe = self.site(tables={
			"Customer": [{"name": "Vrai Client", "customer_name": "Vrai Client"}],
			"Contact Email": [{"parent": "C-REEL", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [{"parent": "C-REEL", "parenttype": "Contact", "link_doctype": "Customer", "link_name": "Vrai Client"}],
		})
		with self.assertRaises(mail_qa.GuardError) as ctx:
			parties.ensure_parties(frappe, CONF)
		text = str(ctx.exception)
		for needle in ("C-REEL", "Customer", "Vrai Client", "À faire", "moa-a-1@essai.invalid"):
			self.assertIn(needle, text)

	def test_a_contact_without_any_link_gets_its_link(self):
		frappe = self.site(tables={
			"Contact": [{"name": "C-SANS-LIEN"}],
			"Contact Email": [{"parent": "C-SANS-LIEN", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
		})
		out = parties.ensure_parties(frappe, CONF, company="Arkonex")
		row = [c for c in out["contacts"] if c["adresse"] == "moa-a-1@essai.invalid"][0]
		self.assertFalse(row["cree"])
		self.assertTrue(row["lien_ajoute"])
		links = [l for l in frappe.tables["Dynamic Link"] if l["parent"] == "C-SANS-LIEN"]
		self.assertEqual([l["link_name"] for l in links], ["ESSAI — Arkonex — Maître de l'ouvrage A"])


if __name__ == "__main__":
	unittest.main()
