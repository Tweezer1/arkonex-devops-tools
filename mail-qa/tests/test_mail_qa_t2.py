"""Essais de la tranche T2 (OPEN-202, Issue #351, contrat §13) : capacité générique d'essai de courriel.

Lancement : python3 -m unittest discover -s mail-qa/tests -v
Aucun réseau extérieur, aucun Frappe : les accès au site sont remplacés par une doublure.
"""

import ast
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
import types
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import mail_qa  # noqa: E402
import mail_qa_parties as parties  # noqa: E402

CONF = mail_qa.load_conf(os.path.join(ROOT, "mail-qa.conf"))

ORIGINAL = {
	"MessageID": "abc123@deverp.arkonex.ca",
	"Subject": "Demande de prix",
	"From": {"Address": "qa@arkonex.ca"},
	"To": [{"Address": "fournisseur@essai.invalid"}],
}


# --- Doublure de Frappe -------------------------------------------------------------


class FakeMeta:
	def __init__(self, title_field=None):
		self.title_field = title_field

	def has_field(self, field):
		return True


class FakeDoc:
	def __init__(self, frappe, values):
		self.frappe, self.values = frappe, dict(values)
		self.doctype = values.get("doctype")
		self.name = values.get("name")
		for key, value in values.items():
			if key not in ("name",):
				setattr(self, key, value)

	def get(self, key, default=None):
		return self.values.get(key, default)

	def as_dict(self):
		return dict(self.values, name=self.name)

	def append(self, key, row):
		self.values.setdefault(key, []).append(row)

	def _store(self):
		f = self.frappe
		rows = f.tables.setdefault(self.doctype, [])
		title = {"Customer": "customer_name", "Supplier": "supplier_name"}.get(self.doctype)
		if self.name is None:
			self.name = self.values.get(title) or ("%s-%d" % (self.doctype, len(rows) + 1))
		rows[:] = [r for r in rows if r.get("name") != self.name]
		rows.append(dict(self.values, name=self.name))
		if self.doctype == "Contact":
			f.tables["Contact Email"][:] = [r for r in f.tables.setdefault("Contact Email", []) if r["parent"] != self.name]
			f.tables["Dynamic Link"][:] = [r for r in f.tables.setdefault("Dynamic Link", []) if r["parent"] != self.name]
			for e in self.values.get("email_ids", []):
				f.tables["Contact Email"].append({"parent": self.name, "parenttype": "Contact", "email_id": e["email_id"]})
			for l in self.values.get("links", []):
				f.tables["Dynamic Link"].append({"parent": self.name, "parenttype": "Contact", "link_doctype": l["link_doctype"], "link_name": l["link_name"]})

	def insert(self, **_kw):
		f = self.frappe
		if f.fail_after is not None:
			f.inserts += 1
			if f.inserts > f.fail_after:
				raise RuntimeError("panne simulée")
		self._store()
		self.frappe.writes.append(("insert", self.doctype, self.name))
		return self

	def save(self, **_kw):
		self._store()
		self.frappe.writes.append(("save", self.doctype, self.name))


class Link(types.SimpleNamespace):
	pass


class FakeDB:
	def __init__(self, frappe):
		self.f = frappe
		self.counts = {}

	def exists(self, doctype, name=None):
		self.f.touched.append("exists")
		if doctype == "DocType":
			return name in self.f.doctypes
		return (doctype, name) in self.f.existing or any(r.get("name") == name for r in self.f.tables.get(doctype, []))

	def get_value(self, doctype, filters, fieldname="name", as_dict=False):
		self.f.touched.append("get_value")
		for row in self.f.tables.get(doctype, []):
			if isinstance(filters, str):
				ok = row.get("name") == filters
			else:
				ok = all(row.get(k) == v for k, v in filters.items())
			if ok:
				return row.get(fieldname)
		return None

	def get_single_value(self, *_a):
		self.f.touched.append("single")
		return None

	def count(self, doctype, *_a):
		self.f.touched.append("count")
		return self.counts.get(doctype, len(self.f.tables.get(doctype, [])))

	def commit(self):
		self.f.events.append("commit")
		self.f.snapshot = copy.deepcopy(self.f.tables)

	def rollback(self, save_point=None):
		self.f.events.append("rollback:%s" % save_point)
		if save_point is None:
			self.f.tables = copy.deepcopy(self.f.snapshot)

	def savepoint(self, point):
		self.f.events.append("savepoint:%s" % point)


class FakeCallbacks:
	def __init__(self):
		self._functions = []


def _match(row, filters):
	for key, cond in (filters or {}).items():
		value = row.get(key)
		if isinstance(cond, (list, tuple)):
			op, arg = cond
			if op == "in" and value not in arg:
				return False
			if op == ">=" and not str(value) >= str(arg):
				return False
			if op == "like" and not str(value).endswith(str(arg).lstrip("%")):
				return False
		elif value != cond:
			return False
	return True


class FakeFrappe:
	class PermissionError(Exception):
		pass

	def __init__(self, site="l2repro.local", tables=None, existing=()):
		self.local = types.SimpleNamespace(site=site)
		self.conf = {}
		self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
		self.existing = set(existing)
		self.touched, self.writes, self.events = [], [], []
		self.snapshot = copy.deepcopy(self.tables)
		self.doctypes = set()
		self.fail_after, self.inserts = None, 0
		self.db = FakeDB(self)
		self.db.after_commit, self.db.before_commit = FakeCallbacks(), FakeCallbacks()
		self.session = types.SimpleNamespace(user="Administrator")
		self.utils = types.SimpleNamespace(now_datetime=lambda: "2026-10-25 12:00:00.000000")
		self.perms = {}

	def get_meta(self, doctype):
		self.touched.append("meta")
		return FakeMeta({"Customer": "customer_name", "Supplier": "supplier_name"}.get(doctype))

	def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None):
		self.touched.append("get_all")
		rows = [r for r in self.tables.get(doctype, []) if _match(r, filters)]
		if pluck:
			return [r.get(pluck) for r in rows]
		return [types.SimpleNamespace(**r) if False else _Row(r) for r in rows]

	def get_hooks(self, _name):
		return []

	def get_doc(self, doctype_or_values, name=None):
		self.touched.append("get_doc")
		if isinstance(doctype_or_values, dict):
			return FakeDoc(self, doctype_or_values)
		for row in self.tables.get(doctype_or_values, []):
			if row.get("name") == name:
				doc = FakeDoc(self, dict(row, doctype=doctype_or_values))
				if doctype_or_values == "Contact":
					doc.values["links"] = [Link(**l) for l in self.tables.get("Dynamic Link", []) if l["parent"] == name]
				else:
					doc.values["links"] = [Link(**l) if isinstance(l, dict) else l for l in doc.values.get("links", [])]
				return doc
		raise KeyError(name)

	def set_user(self, user):
		self.session.user = user

	def has_permission(self, doctype, ptype=None, doc=None, user=None, **_kw):
		return self.perms.get((doctype, ptype, user), False)


class _Row(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			raise AttributeError(key)


def runner(status, text="x"):
	calls = []

	def run(conf):
		calls.append(1)
		return [(status, text)]

	run.calls = calls
	return run


class PreflightCase(unittest.TestCase):
	"""Chaque essai a son verrou de préflight et son cache dans un dossier temporaire."""

	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		os.chmod(self.tmp, 0o700)
		self._old = (mail_qa._VERIFY_RUNNER, mail_qa.PREFLIGHT_CACHE_PATH)
		mail_qa.PREFLIGHT_CACHE_PATH = os.path.join(self.tmp, "cache", "verify.json")
		mail_qa._VERIFY_RUNNER = runner("PASS")

	def tearDown(self):
		mail_qa._VERIFY_RUNNER, mail_qa.PREFLIGHT_CACHE_PATH = self._old
		shutil.rmtree(self.tmp)


# --- C1 : parties fictives ----------------------------------------------------------


class TestC1PartyAddresses(unittest.TestCase):
	def test_generation(self):
		self.assertEqual(mail_qa.party_address("client-a"), "client-a@essai.invalid")
		self.assertEqual(mail_qa.party_address("Client-A", "lot7"), "client-a+lot7@essai.invalid")
		self.assertEqual(mail_qa.address_essai_tag("client-a+lot7@essai.invalid"), "lot7")
		self.assertIsNone(mail_qa.address_essai_tag("client-a@essai.invalid"))
		self.assertTrue(mail_qa.new_essai_id().startswith("e"))
		mail_qa.check_essai_id(mail_qa.new_essai_id())

	def test_invalid_names_and_ids_are_refused(self):
		for bad in ("", "a+b", "a b", "a@b", "-x", "é"):
			with self.subTest(name=bad):
				with self.assertRaises(ValueError):
					mail_qa.party_address(bad)
		for bad in ("A B", "x" * 40, "-a", "a_b"):
			with self.subTest(essai=bad):
				with self.assertRaises(ValueError):
					mail_qa.check_essai_id(bad)

	def test_only_bare_reserved_domain_is_a_party(self):
		good = ["a@essai.invalid", "a+lot1@essai.invalid", "A@ESSAI.INVALID"]
		bad = ["a@essai.invalid.com", "a@b@essai.invalid", "a@exemple.com", "x <a@essai.invalid>", "a@essai.invalid,b@x.com",
			"@essai.invalid", "", None, "a@sub.essai.invalid", "a@essai.invalid>"]
		for addr in good:
			self.assertTrue(mail_qa.is_party_address(addr), addr)
		for addr in bad:
			self.assertFalse(mail_qa.is_party_address(addr), addr)

	def test_no_fabricated_message_can_have_a_sender_outside_the_domain(self):
		for sender in ("client@arkonex.ca", "qa@arkonex.ca", "x@essai.invalid.com"):
			with self.subTest(sender=sender):
				with self.assertRaises(ValueError):
					mail_qa.build_reply(ORIGINAL, from_addr=sender)
				with self.assertRaises(ValueError):
					mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr=sender)

	def test_check_marks_refuses_foreign_sender_headers(self):
		for header, value in (("Sender", "x@arkonex.ca"), ("Reply-To", "x@exemple.com")):
			with self.subTest(header=header):
				msg = mail_qa.build_reply(ORIGINAL)
				msg[header] = value
				with self.assertRaises(mail_qa.MarkError) as ctx:
					mail_qa.check_marks(msg)
				self.assertIn(header, str(ctx.exception))
		msg = mail_qa.build_reply(ORIGINAL)
		msg.replace_header("From", "a@essai.invalid, autre@arkonex.ca")  # deuxième expéditeur
		with self.assertRaises(mail_qa.MarkError):
			mail_qa.check_marks(msg)

	def test_essai_id_is_carried_by_header_and_tagged_sender(self):
		msg = mail_qa.build_reply(ORIGINAL, from_addr=mail_qa.party_address("fournisseur", "lot7"), essai="lot7")
		self.assertEqual(msg["X-Arkonex-Essai-Id"], "lot7")
		self.assertIn("fournisseur+lot7@essai.invalid", msg["From"])


# --- C5 / C6 : message indépendant et variantes -------------------------------------


class TestC5Inbound(unittest.TestCase):
	def test_independent_message_replies_to_nothing_and_is_marked(self):
		msg = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="client-a@essai.invalid", subject="Bonjour")
		self.assertIsNone(msg["In-Reply-To"])
		self.assertIsNone(msg["References"])
		self.assertTrue(msg["Subject"].startswith("[ESSAI SYNTHÉTIQUE] Bonjour"))
		self.assertEqual(msg["X-Arkonex-Essai"], "synthetique")
		mail_qa.check_marks(msg)

	def test_sender_is_required(self):
		with self.assertRaises(ValueError):
			mail_qa.build_inbound(to_addr="boite@essai.invalid")

	def test_attachments_are_kept(self):
		with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as fh:
			fh.write(b"piece")
		try:
			msg = mail_qa.build_inbound(to_addr="boite@essai.invalid", from_addr="c@essai.invalid", attachments=[fh.name])
			self.assertEqual([p.get_filename() for p in msg.iter_attachments()], [os.path.basename(fh.name)])
		finally:
			os.unlink(fh.name)

	def test_target_is_a_simulated_box_or_the_qa_box_only(self):
		self.assertEqual(mail_qa.check_inbound_target(CONF, "boite@essai.invalid"), "boite@essai.invalid")
		self.assertEqual(mail_qa.check_inbound_target(CONF, "qa@arkonex.ca"), "qa@arkonex.ca")
		with self.assertRaises(ValueError):
			mail_qa.check_inbound_target(CONF, "direction@arkonex.ca")


class TestC6Variants(unittest.TestCase):
	def reply(self, *variants, **kw):
		return mail_qa.build_reply(ORIGINAL, "Corps", variants=variants, heavy_bytes=2048, **kw)

	def test_every_variant_builds_and_passes_check_marks(self):
		for variant in mail_qa.VARIANTS:
			with self.subTest(variant=variant):
				msg = self.reply(variant)
				mail_qa.check_marks(msg, (variant,))
				self.assertEqual(msg["X-Arkonex-Essai"], "synthetique")
				self.assertTrue(mail_qa.is_party_address(msg["From"].split("<")[1].rstrip(">")))

	def test_variant_effects(self):
		self.assertEqual(str(self.reply("sans-sujet")["Subject"]), "")
		self.assertEqual(self.reply("sans-corps").get_body(("plain",)).get_content().strip(), "")
		self.assertTrue(str(self.reply("sans-corps")["Subject"]).startswith(mail_qa.MARK_SUBJECT))
		self.assertEqual(list(self.reply("sans-piece").iter_attachments()), [])
		unknown = self.reply("expediteur-inconnu")
		self.assertIn("inconnu-", unknown["From"])
		heavy = list(self.reply("piece-lourde").iter_attachments())
		self.assertEqual(len(heavy), 1)
		self.assertGreaterEqual(len(heavy[0].get_content()), 2048)
		ghost = self.reply("reponse-inconnue")
		self.assertNotIn("abc123", ghost["In-Reply-To"])
		self.assertTrue(ghost["In-Reply-To"].endswith("@essai.invalid>"))

	def test_unknown_variant_is_refused(self):
		with self.assertRaises(mail_qa.MarkError) as ctx:
			self.reply("sans-signature")
		self.assertIn("variante inconnue", str(ctx.exception))
		with self.assertRaises(mail_qa.MarkError):
			mail_qa.check_marks(mail_qa.build_reply(ORIGINAL), ("inventee",))

	def test_incompatible_variants_and_options(self):
		with self.assertRaises(mail_qa.MarkError):
			self.reply("sans-piece", "piece-lourde")
		with self.assertRaises(ValueError):
			self.reply("expediteur-inconnu", from_addr="x@essai.invalid")
		with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
			fh.write(b"x")
		try:
			with self.assertRaises(ValueError):
				mail_qa.build_reply(ORIGINAL, attachments=[fh.name], variants=("sans-piece",))
		finally:
			os.unlink(fh.name)

	def test_marks_not_removed_by_the_variant_stay_mandatory(self):
		"""Une marque manquante qui n'est pas justifiée par la variante déclarée est refusée."""
		def broken(variant, how):
			msg = self.reply(variant)
			if how == "header":
				del msg["X-Arkonex-Essai"]
			elif how == "from":
				msg.replace_header("From", "x@exemple.com")
			elif how == "subject":
				msg.replace_header("Subject", "Re: sans préfixe")
			elif how == "body":
				msg.set_content("Bonjour\n")
			return msg

		cases = [
			("sans-sujet", "header"), ("sans-sujet", "from"), ("sans-sujet", "body"),
			("sans-corps", "header"), ("sans-corps", "from"), ("sans-corps", "subject"),
			("sans-piece", "header"), ("piece-lourde", "from"),
		]
		for variant, how in cases:
			with self.subTest(variant=variant, how=how):
				msg = broken(variant, how)
				if how == "subject" and variant == "sans-sujet":
					continue
				with self.assertRaises(mail_qa.MarkError):
					mail_qa.check_marks(msg, (variant,))

	def test_a_declared_variant_must_really_be_applied(self):
		"""Déclarer une variante ne dispense pas : sujet non vide avec sans-sujet, corps non vide avec sans-corps."""
		normal = mail_qa.build_reply(ORIGINAL, "Corps")
		for variant in ("sans-sujet", "sans-corps", "piece-lourde"):
			with self.subTest(variant=variant):
				with self.assertRaises(mail_qa.MarkError):
					mail_qa.check_marks(normal, (variant,))
		with_file = self.reply("piece-lourde")
		with self.assertRaises(mail_qa.MarkError):
			mail_qa.check_marks(with_file, ("sans-piece",))
		bypass = mail_qa.build_reply(ORIGINAL, variants=("sans-corps",))
		bypass.set_content("Texte libre sans marque\n")
		with self.assertRaises(mail_qa.MarkError):
			mail_qa.check_marks(bypass, ("sans-corps",))

	def test_without_declared_variant_marks_are_all_required(self):
		msg = self.reply("sans-sujet")
		with self.assertRaises(mail_qa.MarkError):
			mail_qa.check_marks(msg)  # variante non déclarée : le sujet vide est refusé


# --- Préflight imposé (§13.4) -------------------------------------------------------


class TestPreflight(PreflightCase):
	def test_fail_refuses_and_is_never_cached(self):
		mail_qa._VERIFY_RUNNER = runner("FAIL", "P1 absent")
		with self.assertRaises(mail_qa.PreflightError) as ctx:
			mail_qa.require_preflight(CONF)
		self.assertIn("P1 absent", str(ctx.exception))
		self.assertFalse(os.path.exists(mail_qa.PREFLIGHT_CACHE_PATH))

	def test_pass_is_cached_five_minutes_at_most(self):
		run = mail_qa._VERIFY_RUNNER
		self.assertEqual(mail_qa.require_preflight(CONF, now=1000.0), "verify")
		self.assertEqual(mail_qa.require_preflight(CONF, now=1000.0 + 299), "cache")
		self.assertEqual(len(run.calls), 1)
		self.assertEqual(mail_qa.require_preflight(CONF, now=1000.0 + 301), "verify")
		self.assertEqual(len(run.calls), 2)
		self.assertLessEqual(mail_qa.PREFLIGHT_TTL, 300)

	def test_cache_does_not_hide_a_later_failure(self):
		mail_qa.require_preflight(CONF, now=1000.0)
		mail_qa._VERIFY_RUNNER = runner("FAIL")
		with self.assertRaises(mail_qa.PreflightError):
			mail_qa.require_preflight(CONF, now=1000.0 + 301)

	def test_untrusted_cache_is_ignored(self):
		mail_qa.require_preflight(CONF, now=1000.0)
		path = mail_qa.PREFLIGHT_CACHE_PATH
		os.chmod(path, 0o666)  # inscriptible par d'autres
		mail_qa._VERIFY_RUNNER = runner("FAIL")
		with self.assertRaises(mail_qa.PreflightError):
			mail_qa.require_preflight(CONF, now=1000.0 + 10)
		os.chmod(path, 0o600)
		with open(path, "w") as fh:
			json.dump({"result": "PASS", "at": 5000.0}, fh)  # horodatage dans le futur
		with self.assertRaises(mail_qa.PreflightError):
			mail_qa.require_preflight(CONF, now=1000.0)

	def test_every_write_entry_point_refuses_when_verify_fails(self):
		"""Aucune écriture (ni même un accès au site) quand verify n'est pas PASS."""
		mail_qa._VERIFY_RUNNER = runner("FAIL", "pare-feu")
		msg = mail_qa.build_reply(ORIGINAL)
		calls = {
			"ensure_inbox": lambda f: mail_qa.ensure_inbox(f, CONF, "boite@essai.invalid"),
			"flush_queue": lambda f: mail_qa.flush_queue(f, CONF, all_=True),
			"inject_message": lambda f: mail_qa.inject_message(f, CONF, msg, "Boîte"),
			"imap_append": lambda f: mail_qa.imap_append(f, CONF, msg, "QA Arkonex"),
			"deliver": lambda f: mail_qa.deliver(f, CONF, msg, "inject", "Boîte"),
			"pull_account": lambda f: mail_qa.pull_account(f, CONF, "QA Arkonex"),
			"can_send": lambda f: mail_qa.can_send(f, CONF, "X", "Y", "u@essai.invalid"),
			"ensure_parties": lambda f: parties.ensure_parties(f, CONF),
		}
		for name, call in calls.items():
			with self.subTest(entry=name):
				frappe = FakeFrappe()
				with self.assertRaises(mail_qa.PreflightError):
					call(frappe)
				self.assertEqual(frappe.touched, [], "le site a été touché avant le préflight")
				self.assertEqual(frappe.writes, [])
		with self.assertRaises(mail_qa.PreflightError):
			mail_qa.purge_essai(CONF, "lot7")  # supprimer dans Mailpit est une écriture

	def test_dry_run_purge_does_not_need_the_preflight(self):
		mail_qa._VERIFY_RUNNER = runner("FAIL")
		original = mail_qa.mailpit_list
		mail_qa.mailpit_list = lambda conf, query=None, limit=2000: []
		try:
			self.assertEqual(mail_qa.purge_essai(CONF, "lot7", dry_run=True)["messages"], [])
		finally:
			mail_qa.mailpit_list = original

	def test_site_is_limited_to_dev_and_benches(self):
		for site in ("desk.arkonex.ca", "prod.example.com", "", "l2.local.evil.com"):
			with self.subTest(site=site):
				frappe = FakeFrappe(site=site)
				with self.assertRaises(mail_qa.GuardError):
					mail_qa.write_guard(frappe, CONF)
				self.assertEqual(frappe.touched, [])
		for site in ("deverp.arkonex.ca", "l2repro.local"):
			with self.subTest(site=site):
				self.assertIn(mail_qa.write_guard(FakeFrappe(site=site), CONF), ("verify", "cache"))

	def test_connect_refuses_a_foreign_site_before_any_import(self):
		with self.assertRaises(mail_qa.GuardError):
			mail_qa._frappe_connect("/nonexistent", "desk.arkonex.ca", CONF)

	def test_noncompliant_site_is_refused(self):
		bad = {"name": "X", "service": "Frappe Mail", "email_id": "a@essai.invalid", "enable_outgoing": 0, "enable_incoming": 0,
			"smtp_server": "", "smtp_port": ""}
		frappe = FakeFrappe(tables={"Email Account": [bad]})
		with self.assertRaises(SystemExit):
			mail_qa.write_guard(frappe, CONF)


# --- C2 : boîtes simulées -----------------------------------------------------------


class TestC2Inbox(PreflightCase):
	def test_existing_active_account_is_never_reused_as_simulated(self):
		row = {"name": "Vraie", "email_id": "boite@essai.invalid", "service": "", "enable_incoming": 1, "enable_outgoing": 0,
			"use_imap": 1, "smtp_server": "", "smtp_port": ""}
		row["enable_incoming"] = 0  # IMAP actif seul : compatible avec verify, mais ce n'est pas une boîte simulée
		frappe = FakeFrappe(tables={"Email Account": [row]})
		with self.assertRaises(mail_qa.GuardError):
			mail_qa.ensure_inbox(frappe, CONF, "boite@essai.invalid")

	def test_address_must_be_a_party(self):
		with self.assertRaises(ValueError):
			mail_qa.ensure_inbox(FakeFrappe(), CONF, "direction@arkonex.ca")


# --- C9 : signalement #347 et rapport ------------------------------------------------


class TestC9Reports(unittest.TestCase):
	def contact_site(self):
		return FakeFrappe(tables={
			"Contact Email": [{"parent": "Marie-ESSAI", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [
				{"parent": "Marie-ESSAI", "parenttype": "Contact", "link_doctype": "Customer", "link_name": "ESSAI — Client"},
				{"parent": "Marie-ESSAI", "parenttype": "Contact", "link_doctype": "Project", "link_name": "P-1"},
			],
		})

	def test_registered_contact_sender_is_signalled_with_its_links(self):
		out = mail_qa.agreement_signals(self.contact_site(), ["MOA-A-1@essai.invalid"])
		self.assertEqual(len(out), 1)
		self.assertTrue(out[0]["piece_d_accord_possible"])
		self.assertEqual(out[0]["adresse"], "moa-a-1@essai.invalid")
		self.assertEqual(out[0]["contacts"][0]["contact"], "Marie-ESSAI")
		self.assertEqual(
			[(l["doctype"], l["nom"]) for l in out[0]["contacts"][0]["liens"]],
			[("Customer", "ESSAI — Client"), ("Project", "P-1")],
		)

	def test_unknown_or_tagged_sender_is_not_signalled(self):
		site = self.contact_site()
		self.assertEqual(mail_qa.agreement_signals(site, ["inconnu@essai.invalid", "moa-a-1+lot7@essai.invalid", None]), [])

	def test_report_content_and_merge(self):
		tmp = tempfile.mkdtemp()
		try:
			sig = mail_qa.agreement_signals(self.contact_site(), ["moa-a-1@essai.invalid"])
			path = mail_qa.report_update(tmp, "lot7", "l2repro.local", messages=[{"genre": "réponse", "signalements": sig}],
				controles=[mail_qa.check_entry("marques", "PASS")])
			mail_qa.report_update(tmp, "lot7", "l2repro.local", effets={"email_queue": []}, controles=[mail_qa.check_entry("x", "PASS")])
			with open(path, encoding="utf-8") as fh:
				data = json.load(fh)
			self.assertEqual(os.path.basename(path), "rapport-lot7.json")
			self.assertEqual(data["essai"], "lot7")
			self.assertEqual(len(data["controles"]), 2)
			self.assertIn("effets", data)
			self.assertIn(mail_qa.HTTPS_LIMIT, data["limite_https"])
			self.assertTrue(data["etablit"] and data["n_etablit_pas"])
			self.assertTrue(any("échange externe" in t for t in data["n_etablit_pas"]))
			self.assertEqual(data["signalements_347"][0]["adresse"], "moa-a-1@essai.invalid")
			self.assertTrue(data["signalements_347"][0]["piece_d_accord_possible"])
		finally:
			shutil.rmtree(tmp)

	def test_report_requires_an_essai_id(self):
		with self.assertRaises(ValueError):
			mail_qa.report_update(tempfile.gettempdir(), None)


# --- C11 : catalogue de parties -----------------------------------------------------


def shipped():
	return parties.load_catalog()


class TestC11Catalog(unittest.TestCase):
	def test_shipped_catalog_is_valid(self):
		self.assertEqual(parties.validate_catalog(shipped()), [])

	def test_shipped_catalog_covers_both_companies(self):
		cat = shipped()
		self.assertEqual(cat["societes"], ["Arkonex", "Arkonex Conseil"])
		for societe in cat["societes"]:
			orgs = [o for o in cat["organisations"] if o["societe"] == societe]
			self.assertTrue(any(o["role"] == "maitre-ouvrage" and len(o["contacts"]) >= 2 for o in orgs))
			self.assertTrue(any(o["role"] == "fournisseur" for o in orgs))
			self.assertTrue(any(o["role"] == "sous-traitant" for o in orgs))

	def test_internes_point_to_existing_personas_without_secrets(self):
		path = os.path.join(ROOT, shipped()["personas_fichier"])
		if not os.path.exists(path):
			self.skipTest("personas.yaml absent de cette copie")
		personas = parties.read_personas(path)
		self.assertEqual(parties.validate_catalog(shipped(), personas), [])
		for item in parties.resolve_internes(shipped(), personas):
			self.assertTrue(item["utilisateur"], item)
		raw = json.dumps(shipped())
		self.assertNotIn("password", raw.lower())
		self.assertNotIn("credentials", raw.lower())

	def mutated(self, fn):
		cat = json.loads(json.dumps(shipped()))
		fn(cat)
		return parties.validate_catalog(cat)

	def test_each_rule_is_enforced(self):
		def rule(fn, needle):
			errors = self.mutated(fn)
			self.assertTrue(any(needle in e for e in errors), (needle, errors))

		rule(lambda c: c["organisations"][0].__setitem__("nom", "Maître de l'ouvrage A"), "doit commencer par")
		rule(lambda c: c["organisations"][0]["contacts"][0].__setitem__("adresse", "moa@exemple.com"), "invalide")
		rule(lambda c: c["organisations"][0]["contacts"][0].__setitem__("adresse", "moa+lot7@essai.invalid"), "invalide")
		rule(lambda c: c["organisations"][0]["contacts"][0].__setitem__("adresse", "MOA@essai.invalid"), "invalide")
		rule(lambda c: c["organisations"][0]["contacts"][1].__setitem__("adresse", c["organisations"][0]["contacts"][0]["adresse"]), "déjà utilisée")
		rule(lambda c: c["organisations"][1].__setitem__("nom", c["organisations"][0]["nom"]), "en double")
		rule(lambda c: c["organisations"][0].__setitem__("societe", "Autre"), "société inconnue")
		rule(lambda c: c["organisations"][0].__setitem__("role", "client"), "rôle inconnu")
		rule(lambda c: c["organisations"][0]["contacts"][0].__setitem__("prenom", "Marie"), "prénom")
		rule(lambda c: c["organisations"][0].__setitem__("contacts", []), "au moins un contact")
		rule(lambda c: c.__setitem__("domaine", "exemple.com"), "domaine")
		rule(lambda c: c.__setitem__("organisations", [o for o in c["organisations"] if o["role"] != "sous-traitant"]), "sous-traitant")
		rule(lambda c: c["organisations"][0]["contacts"].pop(), "deux contacts")
		errors = parties.validate_catalog(shipped(), {"sales_manager": {}})
		self.assertTrue(any("persona" in e for e in errors))

	def test_read_personas_parser(self):
		tmp = tempfile.mkdtemp()
		try:
			path = os.path.join(tmp, "p.yaml")
			with open(path, "w", encoding="utf-8") as fh:
				fh.write('# c\npersonas:\n  a_b:\n    enabled: true\n    login_user_id: "x@y.ca"  # note\n  c:\n    login_user_id: "z@y.ca"\n')
			self.assertEqual(parties.read_personas(path), {"a_b": {"enabled": "true", "login_user_id": "x@y.ca"}, "c": {"login_user_id": "z@y.ca"}})
		finally:
			shutil.rmtree(tmp)


class TestC11Ensure(PreflightCase):
	def site(self, **kw):
		tables = {"Email Account": []}
		tables.update(kw.pop("tables", {}))
		existing = {("Customer Group", g) for g in ("Commercial", "Government")} | {("Supplier Group", g) for g in ("Local", "Services", "Hardware")} | {("Territory", "Canada")}
		return FakeFrappe(tables=tables, existing=existing, **kw)

	def test_creates_then_is_idempotent(self):
		frappe = self.site()
		first = parties.ensure_parties(frappe, CONF)
		self.assertEqual(len(first["organisations"]), 6)
		self.assertEqual(len(first["contacts"]), 8)
		self.assertTrue(all(o["cree"] for o in first["organisations"]))
		snapshot = {k: len(v) for k, v in frappe.tables.items()}
		frappe.writes.clear()
		second = parties.ensure_parties(frappe, CONF)
		self.assertFalse(any(o["cree"] for o in second["organisations"]))
		self.assertFalse(any(c["cree"] or c["lien_ajoute"] for c in second["contacts"]))
		self.assertEqual({k: len(v) for k, v in frappe.tables.items()}, snapshot)
		self.assertEqual(frappe.writes, [])
		# chaque contact est lié à sa partie fictive et porte l'adresse du catalogue
		links = {(l["parent"], l["link_name"]) for l in frappe.tables["Dynamic Link"]}
		self.assertEqual(len(links), 8)
		self.assertTrue(all(name.startswith("ESSAI — ") for _c, name in links))

	def test_company_filter(self):
		frappe = self.site()
		out = parties.ensure_parties(frappe, CONF, company="Arkonex Conseil")
		self.assertEqual({o["societe"] for o in out["organisations"]}, {"Arkonex Conseil"})
		with self.assertRaises(ValueError):
			parties.ensure_parties(self.site(), CONF, company="Inconnue")

	def test_refuses_an_address_already_held_by_a_contact_of_a_real_third_party(self):
		frappe = self.site(tables={
			"Customer": [{"name": "Vrai Client", "customer_name": "Vrai Client"}],
			"Contact Email": [{"parent": "C-REEL", "parenttype": "Contact", "email_id": "moa-a-1@essai.invalid"}],
			"Dynamic Link": [{"parent": "C-REEL", "parenttype": "Contact", "link_doctype": "Customer", "link_name": "Vrai Client"}],
		})
		with self.assertRaises(mail_qa.GuardError) as ctx:
			parties.ensure_parties(frappe, CONF)
		self.assertIn("tiers non fictif", str(ctx.exception))
		self.assertEqual(frappe.writes, [], "aucune écriture avant la vérification complète")

	def test_refuses_to_link_to_a_party_without_the_prefix(self):
		frappe = self.site(tables={"Customer": [{"name": "Vrai Client", "customer_name": "Vrai Client"}]})
		with self.assertRaises(mail_qa.GuardError) as ctx:
			parties._ensure_contact(frappe, {"cle": "c", "prenom": "ESSAI — X", "nom": "Y", "adresse": "x@essai.invalid"}, "Customer", "Vrai Client", "ESSAI — ")
		self.assertIn("préfixe", str(ctx.exception))
		self.assertEqual(frappe.writes, [])
		# partie fictive créée par l'outil (préfixe + marqueur) : accepté
		frappe.tables["Customer"].append({"name": "ESSAI — Z", "customer_name": "ESSAI — Z", "customer_details": parties.MARKER})
		out = parties._ensure_contact(frappe, {"cle": "c", "prenom": "ESSAI — X", "nom": "Y", "adresse": "x@essai.invalid"}, "Customer", "ESSAI — Z", "ESSAI — ")
		self.assertTrue(out["cree"])

	def test_missing_group_or_territory_is_refused_before_writing(self):
		frappe = FakeFrappe(tables={"Email Account": []}, existing={("Territory", "Canada")})
		with self.assertRaises(mail_qa.GuardError):
			parties.ensure_parties(frappe, CONF)
		self.assertEqual(frappe.writes, [])

	def test_foreign_site_is_refused(self):
		frappe = self.site(site="desk.arkonex.ca")
		with self.assertRaises(mail_qa.GuardError):
			parties.ensure_parties(frappe, CONF)
		self.assertEqual(frappe.writes, [])

	def test_contact_lookup_is_exact(self):
		frappe = self.site(tables={"Contact Email": [{"parent": "C", "parenttype": "Contact", "email_id": "a@essai.invalid"}]})
		self.assertEqual(parties.contacts_for_address(frappe, "a@essai.invalid")[0]["contact"], "C")
		self.assertEqual(parties.contacts_for_address(frappe, "b@essai.invalid"), [])


# --- C7 / C8 : effets et droits -----------------------------------------------------


class TestC7Effects(unittest.TestCase):
	def test_window_reports_only_what_the_action_created(self):
		frappe = FakeFrappe(tables={
			"Communication": [{"name": "COM-0", "creation": "2026-10-25 11:00:00", "reference_doctype": "Avant", "reference_name": "A-0", "subject": "ancien"}],
			"Email Queue": [{"name": "Q-0", "creation": "2026-10-25 11:00:00", "status": "Sent"}],
		})
		frappe.tables["Communication"].append(
			{"name": "COM-1", "creation": "2026-10-25 12:00:01", "reference_doctype": "Doc", "reference_name": "D-1", "subject": "nouveau", "timeline_links": [Link(link_doctype="Personne", link_name="P-1")]})
		frappe.tables["Email Queue"].append({"name": "Q-1", "creation": "2026-10-25 12:00:02", "status": "Not Sent"})
		frappe.tables["ToDo"] = [{"name": "T-1", "creation": "2026-10-25 12:00:03", "status": "Open"}]
		frappe.tables["Mon Type"] = [{"name": "M-1", "creation": "2026-10-25 12:00:04"}, {"name": "M-0", "creation": "2026-10-25 10:00:00"}]
		frappe.existing = set()
		frappe.doctypes = {"Communication", "Email Queue", "File", "ToDo", "Notification Log", "Mon Type"}
		with mail_qa.EffectsWindow(frappe, None, doctypes=("Mon Type",)) as window:
			pass
		report = window.report
		self.assertEqual([r["name"] for r in report["communications"]], ["COM-1"])
		self.assertEqual([r["name"] for r in report["email_queue"]], ["Q-1"])
		self.assertEqual([r["name"] for r in report["todo"]], ["T-1"])
		self.assertEqual(report["autres"], {"Mon Type": ["M-1"]})
		self.assertIn(["Doc", "D-1"], report["documents_references"])
		self.assertIn(["Personne", "P-1"], report["documents_references"])
		self.assertNotIn(["Avant", "A-0"], report["documents_references"])

	def test_new_mailpit_messages_are_the_ids_that_appeared(self):
		frappe = FakeFrappe()
		frappe.doctypes = {"Communication", "Email Queue", "File", "ToDo", "Notification Log"}
		listed = [{"ID": "a"}, {"ID": "b"}]
		originals = (mail_qa.mailpit_list, mail_qa.mailpit_request)
		mail_qa.mailpit_list = lambda conf, query=None, limit=2000: list(listed)
		mail_qa.mailpit_request = lambda conf, method, path, payload=None, timeout=20.0: {"ID": path.rsplit("/", 1)[1], "MessageID": "m-" + path[-1], "Subject": "s", "From": {"Address": "x@essai.invalid"}}
		try:
			window = mail_qa.EffectsWindow(frappe, CONF).start()
			listed.append({"ID": "c"})
			report = window.stop()
		finally:
			mail_qa.mailpit_list, mail_qa.mailpit_request = originals
		self.assertEqual([m["id"] for m in report["mailpit_nouveaux"]], ["c"])


class TestC8Rights(PreflightCase):
	def test_visibility_maps_permission_error_to_refusal_and_checks_file_access(self):
		frappe = FakeFrappe(tables={"File": [{"name": "F-1", "file_name": "p.pdf", "attached_to_doctype": "Communication", "attached_to_name": "COM-1"}]})
		frappe.perms[("File", "read", "lecteur@essai.invalid")] = True

		def timeline(doctype, name, start, limit):
			if frappe.session.user == "interdit@essai.invalid":
				raise frappe.PermissionError("non")
			return [{"name": "COM-1"}]

		rows = mail_qa.timeline_visibility(frappe, "X", "Y", ["lecteur@essai.invalid", "interdit@essai.invalid", "autre@essai.invalid"], CONF, timeline_fn=timeline)
		self.assertEqual([r["chronologie"] for r in rows], ["visible", "refusée", "visible"])
		self.assertEqual(rows[0]["pieces_jointes"]["COM-1"], [{"fichier": "F-1", "nom": "p.pdf", "lisible": True}])
		self.assertFalse(rows[2]["pieces_jointes"]["COM-1"][0]["lisible"])
		self.assertEqual(rows[1]["communications"], [])
		self.assertEqual(frappe.session.user, "Administrator")

	def test_visibility_restores_the_user_even_on_error(self):
		frappe = FakeFrappe()

		def boom(*_a):
			raise RuntimeError("panne")

		rows = mail_qa.timeline_visibility(frappe, "X", "Y", ["a@essai.invalid"], CONF, timeline_fn=boom)
		self.assertEqual(rows[0]["chronologie"], "erreur")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_can_send_checks_the_right_then_tries_for_real_and_rolls_back(self):
		frappe = FakeFrappe(tables={"Email Account": []})
		frappe.perms[("X", "email", "ok@essai.invalid")] = True
		seen = []

		def make(**kw):
			seen.append((frappe.session.user, kw["recipients"], kw["subject"]))
			frappe.tables.setdefault("Email Queue", []).append({"name": "Q"})
			frappe.db.after_commit._functions.append(lambda: None)
			if frappe.session.user == "non@essai.invalid":
				raise frappe.PermissionError("pas de droit courriel")
			frappe.tables.setdefault("Communication", []).append({"name": "C"})

		frappe.db.counts = {}
		ok = mail_qa.can_send(frappe, CONF, "X", "Y", "ok@essai.invalid", "lot7", make_fn=make)
		self.assertTrue(ok["droit_courriel"])
		self.assertEqual(ok["tentative"], "autorisée")
		self.assertEqual(seen[0], ("ok@essai.invalid", ["destinataire+lot7@essai.invalid"], "[ESSAI SYNTHÉTIQUE] droit d'envoi"))
		self.assertIn("savepoint:mail_qa_can_send", frappe.events)
		self.assertIn("rollback:mail_qa_can_send", frappe.events)
		self.assertEqual(frappe.db.after_commit._functions, [], "les rappels ajoutés par la tentative sont retirés")
		self.assertEqual(frappe.session.user, "Administrator")
		# la doublure ne défait rien : le résidu est donc détecté, ce qui prouve que la mesure fonctionne
		self.assertTrue(ok["residu"])
		refused = mail_qa.can_send(frappe, CONF, "X", "Y", "non@essai.invalid", make_fn=make)
		self.assertFalse(refused["droit_courriel"])
		self.assertEqual(refused["tentative"], "refusée")

	def test_can_send_reports_no_residue_when_the_rollback_works(self):
		frappe = FakeFrappe(tables={"Email Account": []})
		base = {"Email Queue": 3, "Communication": 5}
		frappe.db.counts = dict(base)

		def make(**_kw):
			frappe.db.counts = {"Email Queue": 4, "Communication": 6}

		def rollback(save_point=None):
			frappe.events.append("rollback:%s" % save_point)
			frappe.db.counts = dict(base)

		frappe.db.rollback = rollback
		row = mail_qa.can_send(frappe, CONF, "X", "Y", "u@essai.invalid", make_fn=make)
		self.assertFalse(row["residu"])
		self.assertEqual(row["compteurs"], {"avant": [3, 5], "apres": [3, 5]})


# --- Rétention et capture -----------------------------------------------------------


class FakeMailpit:
	def __init__(self, messages):
		self.messages = messages  # id -> détail
		self.headers = {}  # id -> en-têtes bruts
		self.deleted = []
		self.requests = []

	def install(self):
		self._orig = (mail_qa.mailpit_list, mail_qa.mailpit_request)
		mail_qa.mailpit_list = self.lst
		mail_qa.mailpit_request = self.req

	def restore(self):
		mail_qa.mailpit_list, mail_qa.mailpit_request = self._orig

	def lst(self, conf, query=None, limit=2000):
		return [{"ID": i, "Created": d.get("Created", "2026-10-25T12:00:00Z")} for i, d in self.messages.items()]

	def req(self, conf, method, path, payload=None, timeout=20.0):
		self.requests.append((method, path, payload))
		if method == "DELETE":
			self.deleted.extend(payload["IDs"])
			return "ok"
		if path.endswith("/headers"):
			return self.headers.get(path.split("/")[-2], {})
		return dict(self.messages[path.rsplit("/", 1)[1]], ID=path.rsplit("/", 1)[1])


def msg(sender="x@essai.invalid", to=(), cc=(), bcc=(), subject="s", attachments=(), created="2026-10-25T12:00:00Z"):
	a = lambda items: [{"Address": i} for i in items]
	return {"MessageID": "m@x", "Subject": subject, "From": {"Address": sender}, "To": a(to), "Cc": a(cc), "Bcc": a(bcc),
		"ReplyTo": [], "Attachments": [{"FileName": n, "ContentType": "application/pdf", "Size": 10} for n in attachments], "Created": created, "Size": 1}


class TestRetentionAndCapture(PreflightCase):
	def setUp(self):
		super().setUp()
		self.mp = FakeMailpit({
			"A1": msg(to=["client+lot7@essai.invalid"]),
			"A2": msg(sender="f+lot7@essai.invalid", to=["qa@arkonex.ca"]),
			"A3": msg(to=["qa@arkonex.ca"], bcc=["copie+lot7@essai.invalid"]),
			"B1": msg(to=["client+lot8@essai.invalid"]),
			"B2": msg(to=["client+lot77@essai.invalid"]),
			"N1": msg(to=["client@essai.invalid"]),
			"N2": msg(to=["pro@arkonex.ca"], subject="lot7"),
		})
		self.mp.install()

	def tearDown(self):
		self.mp.restore()
		super().tearDown()

	def test_purge_deletes_only_the_messages_of_that_essai(self):
		out = mail_qa.purge_essai(CONF, "lot7")
		self.assertEqual(sorted(out["messages"]), ["A1", "A2", "A3"])
		self.assertEqual(sorted(self.mp.deleted), ["A1", "A2", "A3"])
		delete = [r for r in self.mp.requests if r[0] == "DELETE"]
		self.assertEqual(len(delete), 1)
		self.assertEqual(delete[0][1], "/api/v1/messages")
		self.assertEqual(set(delete[0][2]), {"IDs"})

	def test_purge_dry_run_deletes_nothing(self):
		out = mail_qa.purge_essai(CONF, "lot7", dry_run=True)
		self.assertEqual(len(out["messages"]), 3)
		self.assertEqual(self.mp.deleted, [])

	def test_purge_with_nothing_matching_sends_no_delete(self):
		out = mail_qa.purge_essai(CONF, "absent")
		self.assertEqual(out["supprimes"], 0)
		self.assertEqual([r for r in self.mp.requests if r[0] == "DELETE"], [])

	def test_captured_filters_and_shape(self):
		items = mail_qa.captured(CONF, essai="lot7")
		self.assertEqual(sorted(i["id"] for i in items), ["A1", "A2", "A3"])
		items = mail_qa.captured(CONF, to="copie+lot7@essai.invalid")
		self.assertEqual([i["id"] for i in items], ["A3"])
		self.assertEqual(items[0]["bcc"], ["copie+lot7@essai.invalid"])
		items = mail_qa.captured(CONF, subject="LOT7")
		self.assertEqual([i["id"] for i in items], ["N2"])
		self.assertEqual(mail_qa.captured(CONF, since="2026-10-26T00:00:00Z"), [])
		self.assertEqual(len(mail_qa.captured(CONF, since="2026-10-25T00:00:00+00:00", limit=2)), 2)

	def test_captured_returns_attachments_and_message_id(self):
		self.mp.messages["N1"] = msg(to=["client@essai.invalid"], attachments=["devis.pdf"])
		item = [i for i in mail_qa.captured(CONF, to="client@essai.invalid")][0]
		self.assertEqual(item["pieces_jointes"], [{"nom": "devis.pdf", "type": "application/pdf", "taille": 10}])
		self.assertEqual(item["message_id"], "m@x")
		for key in ("a", "cc", "bcc", "de", "sujet", "id"):
			self.assertIn(key, item)


# --- Ligne de commande --------------------------------------------------------------


class TestCommandLine(unittest.TestCase):
	def run_main(self, argv):
		out, err = io.StringIO(), io.StringIO()
		old = sys.stderr
		sys.stderr = err
		try:
			with redirect_stdout(out):
				code = mail_qa.main(argv)
		finally:
			sys.stderr = old
		return code, out.getvalue(), err.getvalue()

	def test_address_command(self):
		self.assertEqual(self.run_main(["address", "client-a", "--essai", "lot7"])[1].strip(), "client-a+lot7@essai.invalid")

	def test_unknown_variant_is_refused_by_the_parser(self):
		with self.assertRaises(SystemExit):
			mail_qa.main(["inbound", "--site", "l2repro.local", "--via", "inject", "--to", "b@essai.invalid", "--from", "c@essai.invalid", "--variant", "inventee"])

	def test_dry_run_inbound_shows_a_marked_message_without_touching_anything(self):
		code, out, _err = self.run_main(["inbound", "--site", "l2repro.local", "--via", "inject", "--to", "b@essai.invalid",
			"--from", "c@essai.invalid", "--variant", "sans-sujet", "--dry-run", "--essai", "lot7"])
		self.assertEqual(code, 0)
		self.assertIn("X-Arkonex-Essai: synthetique", out)
		self.assertIn("X-Arkonex-Essai-Id: lot7", out)
		self.assertIn("c@essai.invalid", out)

	def test_refusals_are_reported_not_raised(self):
		code, _out, err = self.run_main(["inbound", "--site", "l2repro.local", "--via", "inject", "--to", "direction@arkonex.ca",
			"--from", "c@essai.invalid", "--dry-run"])
		self.assertEqual(code, 1)
		self.assertIn("REFUS", err)
		code, _out, err = self.run_main(["inbound", "--site", "l2repro.local", "--via", "inject", "--to", "b@essai.invalid",
			"--from", "c@arkonex.ca", "--dry-run"])
		self.assertEqual(code, 1)
		self.assertIn("@essai.invalid", err)

	def test_report_dir_requires_an_essai(self):
		code, _o, err = self.run_main(["flush", "--site", "l2repro.local", "--report-dir", tempfile.gettempdir()])
		self.assertEqual(code, 1)
		self.assertIn("--essai", err)

	def test_reply_requires_via(self):
		original = mail_qa.fetch_original
		mail_qa.fetch_original = lambda conf, mid: ORIGINAL
		try:
			code, _o, err = self.run_main(["reply", "--site", "l2repro.local", "--mailpit-id", "x"])
		finally:
			mail_qa.fetch_original = original
		self.assertEqual(code, 1)
		self.assertIn("--via", err)

	def test_parties_validate_and_show(self):
		code, out, _e = self.run_main(["parties", "validate"])
		self.assertEqual(code, 0)
		self.assertIn("PARTIES_CATALOGUE=PASS", out)
		code, out, _e = self.run_main(["parties", "show", "--company", "Arkonex"])
		self.assertEqual(code, 0)
		self.assertIn("ESSAI — Arkonex — Maître de l'ouvrage A", out)
		self.assertNotIn("Arkonex Conseil — Maître", out)


# --- Généralité : aucun nom de DocType métier ---------------------------------------

BUSINESS = ["Lead", "Opportunity", "Quotation", "Sales Invoice", "Purchase Order", "Project", "Contract", "Issue",
	"HD Ticket", "Sales Order", "Purchase Invoice", "Task", "Timesheet", "Employee", "Item", "Customer", "Supplier", "Contact"]
PARTIES_ONLY = {"Customer", "Supplier", "Contact"}


def literal_text(path):
	"""Chaînes (hors docstrings) et commentaires d'un source Python."""
	with open(path, encoding="utf-8") as fh:
		source = fh.read()
	tree = ast.parse(source)
	doc_lines = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
			first = node.body[0] if node.body else None
			if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
				doc_lines.add(first.lineno)
	out = []
	for tok in tokenize.generate_tokens(io.StringIO(source).readline):
		if tok.type == tokenize.COMMENT or (tok.type == tokenize.STRING and tok.start[0] not in doc_lines):
			out.append((tok.start[0], tok.string))
	return out


def doctype_hits(path, names):
	pattern = re.compile(r"(?<![A-Za-z])(%s)(?![A-Za-z])" % "|".join(re.escape(n) for n in names))
	return [(line, m.group(1)) for line, text in literal_text(path) for m in pattern.finditer(text)]


class TestGenerality(unittest.TestCase):
	def test_core_names_no_business_doctype(self):
		self.assertEqual(doctype_hits(os.path.join(ROOT, "mail_qa.py"), BUSINESS), [])

	def test_parties_module_names_only_the_party_doctypes(self):
		hits = doctype_hits(os.path.join(ROOT, "mail_qa_parties.py"), [n for n in BUSINESS if n not in PARTIES_ONLY])
		self.assertEqual(hits, [])
		self.assertTrue(doctype_hits(os.path.join(ROOT, "mail_qa_parties.py"), PARTIES_ONLY), "le module des parties doit nommer les parties")

	def test_the_scan_really_detects_a_name(self):
		tmp = tempfile.mkdtemp()
		try:
			path = os.path.join(tmp, "x.py")
			with open(path, "w", encoding="utf-8") as fh:
				fh.write('"""Doc Lead."""\nX = "Sales Invoice"  # Opportunity\n')
			self.assertEqual([h[1] for h in doctype_hits(path, BUSINESS)], ["Sales Invoice", "Opportunity"])
		finally:
			shutil.rmtree(tmp)

	def test_catalog_is_data_not_code(self):
		with open(os.path.join(ROOT, "parties.json"), encoding="utf-8") as fh:
			json.load(fh)
		self.assertEqual(doctype_hits(os.path.join(ROOT, "mail_qa.py"), ["ESSAI —"]), [])


if __name__ == "__main__":
	unittest.main()
