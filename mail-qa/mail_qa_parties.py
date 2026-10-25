"""mail_qa_parties.py — C11 : parties fictives cohérentes pour les essais de courriel (OPEN-202, T2).

Seul module de l'outil qui nomme des DocTypes (client, fournisseur, contact et leurs liens) :
`mail_qa.py` reste générique. Le catalogue `parties.json` est une DONNÉE versionnée, comme
`personas.yaml` de Browser QA ; aucun code par DocType métier.

Gardes : l'outil n'agit que sur DEV ou un banc `*.local` ; il refuse de lier un contact fictif à
un client ou fournisseur dont le nom ne commence pas par `ESSAI — ` ; il refuse si une adresse du
catalogue appartient déjà à un contact lié à un tiers non fictif.
"""

from __future__ import annotations

import json
import os
import re
import sys

import mail_qa

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "parties.json")

# rôle du catalogue -> (DocType de la partie, champ du nom affiché, champ du groupe, DocType du groupe)
PARTY_KINDS = {
	"maitre-ouvrage": ("Customer", "customer_name", "customer_group", "Customer Group"),
	"fournisseur": ("Supplier", "supplier_name", "supplier_group", "Supplier Group"),
	"sous-traitant": ("Supplier", "supplier_name", "supplier_group", "Supplier Group"),
}


def load_catalog(path: str | None = None) -> dict:
	with open(path or CATALOG_PATH, encoding="utf-8") as fh:
		return json.load(fh)


def read_personas(path: str | None = None) -> dict:
	"""Personas de Browser QA : {nom: {login_user_id, enabled, ...}}. Aucun secret : le manifeste n'en contient pas."""
	path = path or os.path.join(HERE, load_catalog()["personas_fichier"])
	out, current, in_block = {}, None, False
	with open(path, encoding="utf-8") as fh:
		for line in fh:
			line = line.rstrip("\n")
			if re.match(r"^personas:\s*$", line):
				in_block = True
				continue
			if not in_block:
				continue
			m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
			if m:
				current = m.group(1)
				out[current] = {}
				continue
			m = re.match(r'^    ([A-Za-z0-9_]+):\s*"?([^"#]*?)"?\s*(#.*)?$', line)
			if m and current:
				out[current][m.group(1)] = m.group(2)
	return out


def validate_catalog(catalog: dict, personas: dict | None = None) -> list[str]:
	"""Rend la liste des erreurs du catalogue (vide = valide). Fonction pure."""
	errors = []
	prefix = catalog.get("prefixe") or ""
	if not prefix.strip():
		errors.append("préfixe vide")
	if catalog.get("domaine") != mail_qa.MARK_DOMAIN:
		errors.append("domaine %r : @%s est exigé" % (catalog.get("domaine"), mail_qa.MARK_DOMAIN))
	if not catalog.get("territoire"):
		errors.append("territoire par défaut manquant")
	societes = catalog.get("societes") or []
	if not societes:
		errors.append("aucune société")
	seen_addr, seen_names, seen_keys = {}, set(), set()
	by_company = {s: {"maitre-ouvrage": [], "fournisseur": [], "sous-traitant": []} for s in societes}
	for org in catalog.get("organisations") or []:
		label = "organisation %s" % (org.get("cle") or org.get("nom"))
		if org.get("role") not in PARTY_KINDS:
			errors.append("%s : rôle inconnu %r" % (label, org.get("role")))
		if org.get("societe") not in societes:
			errors.append("%s : société inconnue %r" % (label, org.get("societe")))
		nom = org.get("nom") or ""
		if not nom.startswith(prefix) or not nom[len(prefix):].strip():
			errors.append("%s : le nom doit commencer par %r" % (label, prefix))
		if nom in seen_names:
			errors.append("%s : nom en double" % label)
		seen_names.add(nom)
		if org.get("cle") in seen_keys or not org.get("cle"):
			errors.append("%s : clé manquante ou en double" % label)
		seen_keys.add(org.get("cle"))
		if not org.get("groupe"):
			errors.append("%s : groupe manquant" % label)
		contacts = org.get("contacts") or []
		if not contacts:
			errors.append("%s : au moins un contact est exigé" % label)
		for contact in contacts:
			address = (contact.get("adresse") or "").strip()
			if not mail_qa.is_party_address(address) or address != address.lower() or "+" in address:
				errors.append("%s : adresse %r invalide (nue, minuscule, @%s)" % (label, address, mail_qa.MARK_DOMAIN))
			if address in seen_addr:
				errors.append("%s : adresse %r déjà utilisée par %s" % (label, address, seen_addr[address]))
			seen_addr[address] = org.get("cle")
			if not (contact.get("prenom") or "").startswith(prefix):
				errors.append("%s : le prénom du contact %r doit commencer par %r" % (label, contact.get("cle"), prefix))
			if not contact.get("nom"):
				errors.append("%s : nom de famille manquant (%s)" % (label, contact.get("cle")))
		if org.get("societe") in by_company and org.get("role") in PARTY_KINDS:
			by_company[org["societe"]][org["role"]].append(org)
	for societe, roles in by_company.items():
		if not any(len(o.get("contacts") or []) >= 2 for o in roles["maitre-ouvrage"]):
			errors.append("société %s : un maître de l'ouvrage avec deux contacts au moins est exigé" % societe)
		for role in ("fournisseur", "sous-traitant"):
			if not roles[role]:
				errors.append("société %s : un %s est exigé" % (societe, role))
	if personas is not None:
		for interne in catalog.get("internes") or []:
			if interne.get("persona") not in personas:
				errors.append("interne %s : persona %r absente de personas.yaml" % (interne.get("role"), interne.get("persona")))
	return errors


def resolve_internes(catalog: dict, personas: dict | None = None) -> list[dict]:
	"""Intervenants internes : renvoient aux personas Browser QA par leur nom (identifiant fonctionnel, jamais de secret)."""
	personas = personas if personas is not None else read_personas()
	return [
		{"role": i["role"], "persona": i["persona"], "utilisateur": (personas.get(i["persona"]) or {}).get("login_user_id")}
		for i in catalog.get("internes") or []
	]


# --- Frappe : lecture et garde -------------------------------------------------------


def contacts_for_address(frappe, address: str) -> list[dict]:
	"""Contacts enregistrés dont l'adresse est exactement `address`, avec leurs liens (lecture seule)."""
	parents = frappe.get_all("Contact Email", filters={"email_id": address, "parenttype": "Contact"}, pluck="parent")
	out = []
	for contact in dict.fromkeys(parents):
		links = frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": contact}, fields=["link_doctype", "link_name"])
		out.append({"contact": contact, "liens": [{"doctype": l["link_doctype"], "nom": l["link_name"]} for l in links]})
	return out


def display_name(frappe, doctype: str, name: str) -> str:
	"""Nom affiché d'un document : son champ titre si le DocType en a un, sinon son nom."""
	meta = frappe.get_meta(doctype)
	title = getattr(meta, "title_field", None)
	if title and meta.has_field(title):
		return frappe.db.get_value(doctype, name, title) or name
	return name


def require_fictive(frappe, doctype: str, name: str, prefix: str) -> None:
	"""Refuse de lier un contact fictif à une partie dont le nom ne commence pas par le préfixe."""
	shown = display_name(frappe, doctype, name)
	if not str(shown).startswith(prefix):
		raise mail_qa.GuardError("%s %s : nom %r sans le préfixe %r, lien refusé" % (doctype, name, shown, prefix))


def check_existing_contacts(frappe, address: str, prefix: str) -> list[dict]:
	"""Refuse si l'adresse appartient déjà à un contact lié à un tiers non fictif."""
	found = contacts_for_address(frappe, address)
	for contact in found:
		for link in contact["liens"]:
			shown = display_name(frappe, link["doctype"], link["nom"])
			if not str(shown).startswith(prefix):
				raise mail_qa.GuardError(
					"l'adresse %s appartient au contact %s, lié à %s %s (%r), un tiers non fictif : refus"
					% (address, contact["contact"], link["doctype"], link["nom"], shown)
				)
	return found


def ensure_parties(frappe, conf: dict, catalog: dict | None = None, company: str | None = None) -> dict:
	"""Crée ou complète, sur DEV ou un banc, les clients, fournisseurs et contacts du catalogue (idempotent)."""
	mail_qa.write_guard(frappe, conf)
	catalog = catalog or load_catalog()
	errors = validate_catalog(catalog)
	if errors:
		raise mail_qa.GuardError("catalogue invalide : " + " ; ".join(errors))
	if company and company not in catalog["societes"]:
		raise ValueError("société inconnue : %r (catalogue : %s)" % (company, ", ".join(catalog["societes"])))
	prefix = catalog["prefixe"]
	orgs = [o for o in catalog["organisations"] if company in (None, o["societe"])]
	# 1) toutes les vérifications avant la première écriture
	for org in orgs:
		doctype, _title, _group_field, group_doctype = PARTY_KINDS[org["role"]]
		if not frappe.db.exists(group_doctype, org["groupe"]):
			raise mail_qa.GuardError("%s %r introuvable sur ce site : corriger le catalogue ou le site" % (group_doctype, org["groupe"]))
		if doctype == "Customer" and not frappe.db.exists("Territory", catalog["territoire"]):
			raise mail_qa.GuardError("Territory %r introuvable sur ce site" % catalog["territoire"])
		for contact in org["contacts"]:
			check_existing_contacts(frappe, contact["adresse"], prefix)
	# 2) écriture, tout ou rien
	result = {"organisations": [], "contacts": []}
	try:
		for org in orgs:
			doctype, title_field, group_field, _group_doctype = PARTY_KINDS[org["role"]]
			name = frappe.db.get_value(doctype, {title_field: org["nom"]}, "name")
			created = name is None
			if created:
				values = {"doctype": doctype, title_field: org["nom"], group_field: org["groupe"]}
				if doctype == "Customer":
					values.update(territory=catalog["territoire"], customer_type="Company")
				else:
					values.update(supplier_type="Company")
				doc = frappe.get_doc(values)
				doc.insert()
				name = doc.name
			result["organisations"].append({"societe": org["societe"], "role": org["role"], "doctype": doctype, "nom": name, "cree": created})
			for contact in org["contacts"]:
				result["contacts"].append(_ensure_contact(frappe, contact, doctype, name, prefix))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	return result


def _ensure_contact(frappe, contact: dict, party_doctype: str, party_name: str, prefix: str) -> dict:
	require_fictive(frappe, party_doctype, party_name, prefix)
	found = contacts_for_address(frappe, contact["adresse"])
	if found:
		doc = frappe.get_doc("Contact", found[0]["contact"])
		linked = any(l.link_doctype == party_doctype and l.link_name == party_name for l in doc.get("links") or [])
		if not linked:
			doc.append("links", {"link_doctype": party_doctype, "link_name": party_name})
			doc.save()
		return {"adresse": contact["adresse"], "contact": doc.name, "cree": False, "lien_ajoute": not linked}
	doc = frappe.get_doc({
		"doctype": "Contact", "first_name": contact["prenom"], "last_name": contact["nom"],
		"email_ids": [{"email_id": contact["adresse"], "is_primary": 1}],
		"links": [{"link_doctype": party_doctype, "link_name": party_name}],
	})
	doc.insert()
	return {"adresse": contact["adresse"], "contact": doc.name, "cree": True, "lien_ajoute": True}


# --- Ligne de commande ---------------------------------------------------------------


def run_cli(args, conf: dict, connect) -> int:
	catalog = load_catalog(args.catalog)
	try:
		personas = read_personas(os.path.join(os.path.dirname(args.catalog), catalog["personas_fichier"]) if args.catalog else None)
	except OSError as exc:  # personas.yaml absent de cette copie : la vérification des intervenants internes est sautée
		print("INFO   personas.yaml illisible (%s) : intervenants internes non vérifiés" % exc, file=sys.stderr)
		personas = None
	errors = validate_catalog(catalog, personas)
	if args.parties_cmd == "validate":
		for error in errors:
			print("FAIL   " + error)
		print("PARTIES_CATALOGUE=" + ("FAIL" if errors else "PASS"))
		return 1 if errors else 0
	if errors:
		print("REFUS : catalogue invalide : " + " ; ".join(errors), file=sys.stderr)
		return 1
	if args.parties_cmd == "show":
		orgs = [o for o in catalog["organisations"] if args.company in (None, o["societe"])]
		mail_qa._out({"organisations": orgs, "internes": resolve_internes(catalog, personas) if personas is not None else catalog.get("internes")})
		return 0
	frappe = connect()
	try:
		mail_qa._out(ensure_parties(frappe, conf, catalog, args.company))
		return 0
	finally:
		frappe.db.rollback()
		frappe.destroy()
