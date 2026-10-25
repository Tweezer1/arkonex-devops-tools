#!/usr/bin/env python3
"""Essai d'intégration de la capacité d'essai des courriels (OPEN-202, T2, preuve de généralité §13.5).

NON lancé par `unittest discover` (dossier sans __init__, aucun fichier test_*). Il écrit réellement
dans le site visé : à lancer avec le Python du banc visé, depuis <banc>/sites :

    <banc>/env/bin/python -I mail-qa/tests/integration/run_integration.py --bench <banc> --site <site> [--report-dir DIR]

Seuls `deverp.arkonex.ca` et les sites `*.local` sont permis. Parcours joué, sur trois DocTypes de
modules différents (CRM : Lead ; Ventes : Quotation ; création par `append_to` : Lead et Issue) :
parties, boîtes simulées, envoi et capture, réponse rattachée, message indépendant, pièces,
variantes, fenêtre d'effets, droits du lecteur et de l'expéditeur, isolation par permission de
document, rapport, rétention. Puis nettoyage SANS `force`, dans l'ordre des dépendances, avec les
comptages avant/après. Code de sortie 0 si tous les contrôles passent ET si les comptages sont
identiques.

Ne supprime que ce que CE passage a créé (création >= début), jamais une donnée préexistante.
Prérequis : verify PASS, Mailpit actif, au moins une Item, une Company, les groupes du catalogue.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # mail-qa/

import mail_qa  # noqa: E402
import mail_qa_parties as parties  # noqa: E402

TRACKED = ["Customer", "Supplier", "Contact", "Lead", "Opportunity", "Quotation", "Issue", "Email Account", "Email Queue",
	"Communication", "File", "ToDo", "Notification Log", "User", "User Permission", "Comment", "Version", "Dynamic Link",
	"Contact Email", "Deleted Document"]
RESULTS = []


def check(name, ok, detail=""):
	RESULTS.append((name, bool(ok), detail))
	print("%-5s %s %s" % ("PASS" if ok else "FAIL", name, ("— " + str(detail)) if detail and not ok else ""))
	return ok


def counts(frappe):
	return {dt: frappe.db.count(dt) for dt in TRACKED}


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--bench", required=True)
	ap.add_argument("--site", required=True)
	ap.add_argument("--report-dir", default=None)
	ap.add_argument("--clean-trace", action="store_true", help="efface aussi la trace native d'effacement créée pendant le passage (défaut : seulement sur un site *.local)")
	args = ap.parse_args()
	conf = mail_qa.load_conf()
	mail_qa.guard_allowed_site(args.site, conf)
	import frappe

	frappe.init(site=args.site, sites_path=os.path.join(args.bench, "sites"))
	frappe.connect()
	frappe.set_user("Administrator")
	essai = mail_qa.new_essai_id()
	from frappe.utils.background_jobs import get_queues

	queued = sum(q.count for q in get_queues())
	print("travaux en file : %d (au-delà de 500, Frappe refuse tout nouvel envoi en file : sur un banc sans worker, vider ses files avant)" % queued)
	report_dir = args.report_dir or tempfile.mkdtemp(prefix="rapport-mail-qa-")
	made = {"Email Account": [], "User": [], "User Permission": []}
	deleted = []
	before = counts(frappe)
	mailpit_before = mail_qa.mailpit_ids(conf)
	started = frappe.utils.now_datetime()
	print("essai %s, site %s, début %s" % (essai, args.site, started))
	try:
		scenario(frappe, conf, essai, report_dir, made, started)
	except Exception as exc:  # un arrêt n'empêche jamais le nettoyage
		check("scénario sans exception", False, "%s: %s" % (type(exc).__name__, exc))
	finally:
		frappe.db.rollback()
		cleanup(frappe, conf, essai, made, started, deleted)
		after = counts(frappe)
		mailpit_after = mail_qa.mailpit_ids(conf)
		print("\n%-20s %7s %7s" % ("DocType", "avant", "après"))
		diff = False
		for dt in TRACKED:
			flag = "" if before[dt] == after[dt] else "  <== ÉCART"
			diff = diff or bool(flag)
			print("%-20s %7s %7s%s" % (dt, before[dt], after[dt], flag))
		print("Mailpit              %7s %7s%s" % (len(mailpit_before), len(mailpit_after), "" if mailpit_before == mailpit_after else "  <== ÉCART"))
		if not TRACE[0]:
			diff = any(before[dt] != after[dt] for dt in TRACKED if dt not in TRACE_TABLES)
			print("INFO  trace native d'effacement non nettoyée sur ce site (Comment, Version, Deleted Document) : hors comparaison")
		check("comptages identiques avant/après", not diff and mailpit_before == mailpit_after,
			"un autre lot écrit peut-être en même temps ; relire les écarts")
		frappe.db.rollback()
		frappe.destroy()
	failed = [r for r in RESULTS if not r[1]]
	print("\nINTEGRATION_MAIL_QA=%s (%d contrôles, %d échecs) ; rapport : %s" % ("FAIL" if failed else "PASS", len(RESULTS), len(failed), report_dir))
	return 1 if failed else 0


def scenario(frappe, conf, essai, report_dir, made, started):
	from frappe.core.doctype.communication.email import make

	Q = mail_qa
	site = frappe.local.site
	# --- préparation : compte sortant, utilisateurs, documents
	sortie = "ESSAI sortie " + essai
	frappe.get_doc({"doctype": "Email Account", "email_account_name": sortie, "email_id": Q.party_address("sortie", essai),
		"enable_outgoing": 1, "enable_incoming": 0, "smtp_server": "127.0.0.1", "smtp_port": "1025", "no_smtp_authentication": 1,
		"default_outgoing": 1, "use_tls": 0, "use_ssl_for_outgoing": 0, "always_use_account_email_id_as_sender": 1}).insert()
	made["Email Account"].append(sortie)
	sender = Q.party_address("sortie", essai)
	leads = {}
	for tag in ("a", "b"):
		user = "user%s-%s@essai.invalid" % (tag, essai)
		frappe.get_doc({"doctype": "User", "email": user, "first_name": "ESSAI " + tag, "send_welcome_email": 0, "enabled": 1,
			"user_type": "System User", "roles": [{"role": "Sales User"}]}).insert()
		made["User"].append(user)
		lead = frappe.get_doc({"doctype": "Lead", "first_name": "ESSAI %s %s" % (essai, tag.upper()), "email_id": Q.party_address("lead-" + tag, essai)}).insert()
		leads[tag] = lead.name
		frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Lead", "for_value": lead.name, "apply_to_all_doctypes": 1}).insert()
		made["User Permission"].append((user, lead.name))
	item = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=1)
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name", limit=1)[0]
	frappe.db.commit()

	# --- C11 / C2
	check("catalogue valide", not parties.validate_catalog(parties.load_catalog()))
	first = parties.ensure_parties(frappe, conf)
	second = parties.ensure_parties(frappe, conf)
	check("parties ensure idempotent", not any(o["cree"] for o in second["organisations"]) and not any(c["cree"] or c["lien_ajoute"] for c in second["contacts"]))
	customer = [o["nom"] for o in first["organisations"] if o["doctype"] == "Customer"][0]
	box_lead = Q.ensure_inbox(frappe, conf, Q.party_address("boite-lead", essai), "Lead")
	box_issue = Q.ensure_inbox(frappe, conf, Q.party_address("boite-issue", essai), "Issue")
	made["Email Account"] += [box_lead["compte"], box_issue["compte"]]
	check("boîtes simulées créées puis réutilisées", box_lead["cree"] and not Q.ensure_inbox(frappe, conf, Q.party_address("boite-lead", essai), "Lead")["cree"])
	check("verify reste PASS avec ces comptes", not [t for s, t in Q.collect_verify(conf) if s == "FAIL"])

	# --- C3 : envoi depuis deux DocTypes, capture
	moa = "moa-a-1@essai.invalid"
	docs = [("Lead", leads["a"])]
	if item:
		quotation = frappe.get_doc({"doctype": "Quotation", "quotation_to": "Customer", "party_name": customer, "company": company,
			"transaction_date": frappe.utils.today(), "items": [{"item_code": item[0], "qty": 1, "rate": 100}]}).insert()
		docs.append(("Quotation", quotation.name))
	else:
		print("SKIP  aucune Item : la Quotation n'est pas jouée")
	frappe.db.commit()
	window = Q.EffectsWindow(frappe, conf, doctypes=("Lead",)).start()
	for dt, name in docs:
		make(doctype=dt, name=name, content="<p>Demande d'essai</p>", subject="Demande %s %s" % (dt, essai), recipients=[moa],
			cc=["moa-a-2@essai.invalid", Q.party_address("observateur", essai)], bcc=["fournisseur-f-1@essai.invalid"], send_email=True,
			sender=sender, attachments=[{"fname": "devis.txt", "fcontent": b"Devis d'essai"}])
	frappe.db.commit()
	sent = Q.flush_queue(frappe, conf, since=started)
	check("flush : tout est envoyé", sent and all(r["statut"] == "Sent" for r in sent), sent)
	effects = window.stop()
	check("fenêtre d'effets : file, Communications, fichiers, Mailpit",
		effects["email_queue"] and effects["communications"] and effects["fichiers"] and effects["mailpit_nouveaux"])
	check("fenêtre d'effets : documents référencés", all([dt, name] in effects["documents_references"] for dt, name in docs))
	Q.report_update(report_dir, essai, site, effets=effects, envois=sent)
	caught = {}
	for dt, name in docs:
		items = Q.captured(conf, essai=essai, subject="Demande %s" % dt, frappe=frappe)
		caught[dt] = items
		check("capture %s : À, Cc, Bcc, pièce, origine" % dt,
			items and any(i["bcc"] for i in items) and all(i["pieces_jointes"] for i in items)
			and all(i["origine"]["reference_name"] == name for i in items), items[:1])
	Q.report_update(report_dir, essai, site, capture=[i for v in caught.values() for i in v])

	# --- C4 / C5 / C6
	account = box_lead["compte"]
	signals = []
	for dt, name in docs:
		original = Q.fetch_original(conf, caught[dt][0]["id"])
		msg = Q.build_reply(original, "Voici notre prix.", moa, [], (), essai)
		res = Q.deliver(frappe, conf, msg, "inject", account, (), essai)
		check("réponse rattachée à %s %s par le fil" % (dt, name), res["reference"] == [dt, name], res["reference"])
		signals += res["signalements"]
		Q.report_update(report_dir, essai, site, messages=[{"genre": "réponse", "reference": res["reference"], "signalements": res["signalements"]}])
	check("signalement #347 : l'expéditeur est un contact enregistré", any(s["adresse"] == moa and s["piece_d_accord_possible"] for s in signals))
	inbound = Q.build_inbound(to_addr=Q.party_address("boite-lead", essai), from_addr=moa, subject="Nouvelle demande " + essai, conf=conf)
	res = Q.deliver(frappe, conf, inbound, "inject", account, (), essai)
	check("message indépendant : un Lead créé par append_to", res["reference"] and res["reference"][0] == "Lead" and res["reference"][1] not in leads.values(), res["reference"])
	issue = Q.build_inbound(to_addr=Q.party_address("boite-issue", essai), from_addr="sous-traitant-s-1@essai.invalid", subject="Panne " + essai, conf=conf)
	res = Q.deliver(frappe, conf, issue, "inject", box_issue["compte"], (), essai)
	check("message indépendant : une Issue créée par append_to", res["reference"] and res["reference"][0] == "Issue", res["reference"])
	for variant in Q.VARIANTS:
		variant_msg = Q.build_inbound(to_addr=Q.party_address("boite-lead", essai), from_addr=None if variant == "expediteur-inconnu" else "moa-c-1@essai.invalid",
			subject="Variante " + variant, variants=(variant,), essai=essai, heavy_bytes=1024 * 1024, conf=conf)
		res = Q.deliver(frappe, conf, variant_msg, "inject", account, (variant,), essai)
		check("variante %s déposée avec ses marques" % variant, bool(res["communication"]))
	original = Q.fetch_original(conf, caught["Lead"][0]["id"])
	ghost = Q.deliver(frappe, conf, Q.build_reply(original, "?", moa, [], ("reponse-inconnue",), essai), "inject", account, ("reponse-inconnue",), essai)
	check("réponse à un message inconnu : pas de rattachement au document d'origine", ghost["reference"] != ["Lead", leads["a"]], ghost["reference"])

	# --- C7 en ligne de commande (même sémantique que l'API)
	proc = subprocess.run([sys.executable, "-I", mail_qa.__file__, "window", "--bench", BENCH[0], "--site", site, "--doctype", "Lead",
		"--", "inbound", "--bench", BENCH[0], "--site", site, "--via", "inject", "--account", account, "--to", Q.party_address("boite-lead", essai),
		"--from", moa, "--subject", "CLI " + essai, "--essai", essai],
		capture_output=True, text=True, cwd=os.path.join(BENCH[0], "sites"), timeout=900)
	cli = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
	check("window en ligne de commande", proc.returncode == 0 and cli.get("effets", {}).get("communications"), proc.stderr[-300:])

	# --- C8 : droits du lecteur, de l'expéditeur, isolation par permission de document
	users = ["user%s-%s@essai.invalid" % (t, essai) for t in ("a", "b")] + ["Administrator"]
	rows = Q.timeline_visibility(frappe, "Lead", leads["a"], users, conf)
	states = {r["utilisateur"]: r["chronologie"] for r in rows}
	check("lecteur : A voit, B est refusé, Administrator voit", states[users[0]] == "visible" and states[users[1]] == "refusée" and states["Administrator"] == "visible", states)
	Q.report_update(report_dir, essai, site, droits=[{"genre": "lecteur", "document": ["Lead", leads["a"]], "resultat": rows}])
	frappe.db.commit()  # fin de l'instantané de lecture avant les tentatives d'écriture annulées
	rights = [Q.can_send(frappe, conf, "Lead", leads["a"], u, essai) for u in users]
	check("expéditeur : A autorisé, B refusé", rights[0]["tentative"] == "autorisée" and rights[1]["tentative"] == "refusée", [(r["tentative"], r.get("erreur")) for r in rights])
	check("tentatives d'envoi annulées sans résidu", not any(r["residu"] for r in rights))
	Q.report_update(report_dir, essai, site, droits=[{"genre": "expéditeur", "document": ["Lead", leads["a"]], "resultat": rights}])
	Q.report_update(report_dir, essai, site, controles=[Q.check_entry(n, "PASS" if ok else "FAIL", str(d)[:200]) for n, ok, d in RESULTS])
	frappe.db.commit()


def cleanup(frappe, conf, essai, made, started, deleted):
	"""Retour arrière sans `force`, dans l'ordre des dépendances ; ne touche que ce que ce passage a créé."""
	def rm(dt, name):
		try:
			frappe.delete_doc(dt, name)  # jamais force=True
			frappe.db.commit()
			deleted.append((dt, name))
		except Exception as exc:
			frappe.db.rollback()
			check("suppression %s %s" % (dt, name), False, "%s: %s" % (type(exc).__name__, str(exc)[:150]))

	def new(dt, extra=None):
		filters = {"creation": [">=", started]}
		filters.update(extra or {})
		return frappe.get_all(dt, filters=filters, pluck="name")

	try:
		mail_qa.purge_essai(conf, essai)
	except Exception as exc:
		check("rétention Mailpit", False, exc)
	for user, lead in made["User Permission"]:
		for n in frappe.get_all("User Permission", filters={"user": user}, pluck="name"):
			rm("User Permission", n)
	for dt in ("Email Queue", "Communication", "Quotation", "Issue", "Lead", "Contact"):
		for n in new(dt):
			rm(dt, n)
	for dt, field in (("Customer", "customer_name"), ("Supplier", "supplier_name")):
		for n in new(dt, {field: ["like", "ESSAI%"]}):
			rm(dt, n)
	for user in made["User"]:
		if frappe.db.exists("User", user):
			rm("User", user)
	for name in made["Email Account"]:
		if frappe.db.exists("Email Account", name):
			rm("Email Account", name)
	# Trace d'effacement propre à Frappe (Comment « Deleted », Deleted Document, Version). Elle ne désigne pas le
	# document effacé (Comment sans nom) : le nettoyage se fait par fenêtre de temps, donc seulement sur un banc
	# `*.local` (ou avec --clean-trace), jamais par défaut sur un site partagé.
	# Commentaires qui désignent un document effacé par son nom (pièces jointes, partages) : précis, toujours effacés.
	for dt, name in deleted:
		for n in frappe.get_all("Comment", filters={"reference_doctype": dt, "reference_name": name}, pluck="name"):
			try:
				frappe.delete_doc("Comment", n)
			except Exception as exc:
				frappe.db.rollback()
				check("commentaire %s" % n, False, exc)
	frappe.db.commit()
	if TRACE[0]:
		for _ in range(6):
			removed = 0
			for dt, filters in (("Comment", {"comment_type": "Deleted"}), ("Version", {}), ("Deleted Document", {})):
				filters = dict(filters, creation=[">=", started])
				for n in frappe.get_all(dt, filters=filters, pluck="name"):
					try:
						frappe.delete_doc(dt, n)
						removed += 1
					except Exception as exc:
						frappe.db.rollback()
						check("trace %s %s" % (dt, n), False, exc)
				frappe.db.commit()
			if not removed:
				break


TRACE = [False]
BENCH = [None]
TRACE_TABLES = ("Comment", "Version", "Deleted Document")


if __name__ == "__main__":
	BENCH[0] = sys.argv[sys.argv.index("--bench") + 1] if "--bench" in sys.argv else None
	TRACE[0] = "--clean-trace" in sys.argv or (sys.argv[sys.argv.index("--site") + 1].endswith(".local") if "--site" in sys.argv else False)
	sys.exit(main())
