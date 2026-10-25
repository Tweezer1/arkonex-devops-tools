#!/usr/bin/env python3
"""mail_qa.py — outil du dispositif d'essai de courriel, mode M1 (OPEN-202, Issue #351).

Contrat : docs/open_202/devops_mail_qa_m1_contrat.md (v0.2.0).

Sous-commandes :
  verify        lecture seule. Contrôle P1 (sortie SMTP bloquée, persistance), P2 (Mailpit
                sans relais ni transfert), et chaque site de chaque banc de la machine.
                Code de sortie : 0 = PASS, 1 = FAIL, 2 = erreur.
  site-check    lecture seule, un site (lancé par `verify` avec le Python du banc).
  reply         construit une réponse SYNTHÉTIQUE marquée à un message capturé par Mailpit
                et la dépose par IMAP APPEND dans la boîte QA (DEV seulement).
  inject        même réponse, injectée dans la réception native d'un site, sans réseau
                (essais automatiques, bancs).
  pull          lance la relève d'un Email Account (le planificateur reste coupé).

Rien n'est jamais envoyé par SMTP vers l'extérieur. Aucun mot de passe n'est affiché.
Limite connue (contrat §3) : les services d'envoi par HTTPS ne sont pas bloqués par le
réseau ; `verify` les tient par contrôle.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import subprocess
import sys
import urllib.request
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Marques des messages synthétiques (contrat §6) -------------------------------
MARK_HEADER = "X-Arkonex-Essai"
MARK_HEADER_VALUE = "synthetique"
MARK_SUBJECT = "[ESSAI SYNTHÉTIQUE]"
MARK_BODY_FIRST_LINE = "Message d'essai fabriqué — aucune valeur d'acceptation"
MARK_DOMAIN = "essai.invalid"

SMTP_PORTS = (25, 465, 587, 2525)
LOCAL_SMTP_HOSTS = {"127.0.0.1", "localhost", "::1"}
FORBIDDEN_MAILPIT_FLAGS = (
	"--smtp-relay-config",
	"--smtp-relay-all",
	"--smtp-relay-matching",
	"--smtp-forward-config",
	"--webhook-url",
)
NFT_CONF = "/etc/nftables.conf"
NFT_RULE_DEST = "/etc/nftables.d/arkonex-mailqa.nft"
NFT_INCLUDE_LINE = 'include "/etc/nftables.d/*.nft"'
HTTPS_LIMIT = (
	"LIMITE CONNUE : les services d'envoi par HTTPS (Frappe Mail, API tierces, webhooks, "
	"SMS, Slack) ne sont pas bloqués par le réseau ; ils sont tenus par ce contrôle."
)


class MarkError(Exception):
	"""Un message synthétique ne porte pas toutes ses marques."""


def load_conf(path: str | None = None) -> dict:
	"""Lit mail-qa.conf (lignes CLE="valeur")."""
	conf = {}
	with open(path or os.path.join(HERE, "mail-qa.conf"), encoding="utf-8") as fh:
		for line in fh:
			line = line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			conf[key.strip()] = value.strip().strip('"')
	return conf


# --- Messages synthétiques ----------------------------------------------------------


def check_marks(msg: EmailMessage) -> None:
	"""Lève MarkError si une des quatre marques manque."""
	missing = []
	if (msg.get(MARK_HEADER) or "").strip() != MARK_HEADER_VALUE:
		missing.append("en-tête " + MARK_HEADER)
	if not (msg.get("Subject") or "").startswith(MARK_SUBJECT):
		missing.append("préfixe du sujet")
	sender = parseaddr(msg.get("From") or "")[1].lower()
	if not sender.endswith("@" + MARK_DOMAIN):
		missing.append("expéditeur @" + MARK_DOMAIN)
	body = msg.get_body(preferencelist=("plain",))
	first = (body.get_content() if body else "").lstrip().splitlines()[:1]
	if first != [MARK_BODY_FIRST_LINE]:
		missing.append("première ligne du corps")
	if missing:
		raise MarkError("marques manquantes : " + ", ".join(missing))


def build_reply(original: dict, body: str = "", from_addr: str | None = None, attachments=()) -> EmailMessage:
	"""Construit la réponse synthétique à un message Mailpit (/api/v1/message/<id>).

	`original` porte au minimum MessageID, Subject, From{Address} et To[{Address}].
	L'expéditeur de la réponse est le premier destinataire d'origine en @essai.invalid,
	ou `from_addr`, qui doit lui-même être en @essai.invalid.
	"""
	message_id = (original.get("MessageID") or "").strip().strip("<>")
	if not message_id:
		raise ValueError("le message d'origine n'a pas de Message-ID")
	to_addr = ((original.get("From") or {}).get("Address") or "").strip()
	if not to_addr:
		raise ValueError("le message d'origine n'a pas d'expéditeur")

	if not from_addr:
		for rcpt in original.get("To") or []:
			address = (rcpt.get("Address") or "").strip()
			if address.lower().endswith("@" + MARK_DOMAIN):
				from_addr = address
				break
	if not from_addr:
		raise ValueError("aucun destinataire d'origine en @%s : préciser --from" % MARK_DOMAIN)
	if not from_addr.lower().endswith("@" + MARK_DOMAIN):
		raise ValueError("l'expéditeur d'une réponse synthétique doit être en @%s" % MARK_DOMAIN)

	subject = (original.get("Subject") or "").strip()
	if not subject.lower().startswith("re:"):
		subject = "Re: " + subject

	msg = EmailMessage(policy=policy.SMTP)
	msg["From"] = "Partie externe d'essai <%s>" % from_addr
	msg["To"] = to_addr
	msg["Subject"] = "%s %s" % (MARK_SUBJECT, subject)
	msg["Date"] = formatdate(localtime=True)
	msg["Message-ID"] = make_msgid(domain=MARK_DOMAIN)
	msg["In-Reply-To"] = "<%s>" % message_id
	msg["References"] = "<%s>" % message_id
	msg[MARK_HEADER] = MARK_HEADER_VALUE
	msg.set_content(MARK_BODY_FIRST_LINE + "\n\n" + (body or "Réponse d'essai.") + "\n")
	for path in attachments:
		with open(path, "rb") as fh:
			data = fh.read()
		msg.add_attachment(
			data, maintype="application", subtype="octet-stream", filename=os.path.basename(path)
		)
	check_marks(msg)
	return msg


# --- Réseau (P1) -------------------------------------------------------------------


def tcp_open(host: str, port: int, timeout: float = 4.0) -> list[tuple[str, bool]]:
	"""Tente une ouverture TCP vers chaque adresse de `host`, sans rien envoyer ni lire.

	Retourne [(adresse, ouvert)]. La connexion est refermée aussitôt : aucun dialogue
	SMTP n'a lieu, donc aucun message ne peut partir.
	"""
	results = []
	try:
		infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except socket.gaierror:
		return [(host + " (non résolu)", False)]
	seen = set()
	for family, socktype, proto, _canon, addr in infos:
		if addr[0] in seen:
			continue
		seen.add(addr[0])
		sock = socket.socket(family, socktype, proto)
		sock.settimeout(timeout)
		try:
			sock.connect(addr)
			results.append((addr[0], True))
		except OSError:
			results.append((addr[0], False))
		finally:
			sock.close()
	return results


def check_network(conf: dict) -> list[tuple[str, str]]:
	"""P1 : ports SMTP fermés vers l'extérieur ; IMAP 993 et Mailpit local joignables."""
	out = []
	for host in (conf["MAIL_HOST"], conf["WITNESS_HOST"]):
		for port in SMTP_PORTS:
			for addr, is_open in tcp_open(host, port):
				out.append(
					("FAIL" if is_open else "PASS", "sortie SMTP %s:%s (%s) %s" % (host, port, addr, "OUVERTE" if is_open else "refusée"))
				)
	imap = tcp_open(conf["MAIL_HOST"], 993)
	out.append(("PASS" if any(o for _a, o in imap) else "FAIL", "IMAP %s:993 joignable" % conf["MAIL_HOST"]))
	local = tcp_open(conf["MAILPIT_SMTP_HOST"], int(conf["MAILPIT_SMTP_PORT"]))
	out.append(("PASS" if any(o for _a, o in local) else "FAIL", "Mailpit SMTP local joignable"))
	return out


def check_persistence(nft_conf: str = NFT_CONF, rule_dest: str = NFT_RULE_DEST, rule_src: str | None = None, systemctl=None) -> list[tuple[str, str]]:
	"""P1 persistant : nftables activé au démarrage, règle incluse et identique au dépôt."""
	out = []
	systemctl = systemctl or _systemctl
	state = systemctl(["is-enabled", "nftables.service"]).strip()
	out.append(("PASS" if state == "enabled" else "FAIL", "nftables activé au démarrage (%s)" % (state or "?")))
	try:
		with open(nft_conf, encoding="utf-8") as fh:
			lines = [line.strip() for line in fh]
		ok = NFT_INCLUDE_LINE in lines and lines.index(NFT_INCLUDE_LINE) > (
			lines.index("flush ruleset") if "flush ruleset" in lines else -1
		)
		out.append(("PASS" if ok else "FAIL", "%s inclut la règle après « flush ruleset »" % nft_conf))
	except OSError as exc:
		out.append(("FAIL", "%s illisible (%s)" % (nft_conf, exc)))
	src = rule_src or os.path.join(HERE, "nftables", "arkonex-mailqa.nft")
	try:
		with open(src, "rb") as a, open(rule_dest, "rb") as b:
			same = a.read() == b.read()
		out.append(("PASS" if same else "FAIL", "%s identique au dépôt" % rule_dest))
	except OSError as exc:
		out.append(("FAIL", "%s absent ou illisible (%s)" % (rule_dest, exc)))
	return out


# --- Mailpit (P2) ------------------------------------------------------------------


def _systemctl(args: list[str]) -> str:
	try:
		return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=20).stdout
	except (OSError, subprocess.SubprocessError):
		return ""


def evaluate_mailpit_unit(show: dict, webui: dict | None, cmdlines: list[str]) -> list[tuple[str, str]]:
	"""Fonction pure : juge l'unité, l'API et les processus Mailpit."""
	out = []
	exec_start = show.get("ExecStart", "")
	env = show.get("Environment", "")
	env_files = show.get("EnvironmentFiles", "")
	out.append(("PASS" if show.get("UnitFileState") == "enabled" else "FAIL", "mailpit-qa activé au démarrage"))
	out.append(("PASS" if show.get("ActiveState") == "active" else "FAIL", "mailpit-qa actif"))
	bad = [flag for flag in FORBIDDEN_MAILPIT_FLAGS if flag in exec_start]
	out.append(("PASS" if not bad else "FAIL", "unité sans relais ni transfert ni webhook" + (" : " + ", ".join(bad) if bad else "")))
	out.append(("PASS" if "--smtp 127.0.0.1:1025" in exec_start and "--listen 127.0.0.1:8025" in exec_start else "FAIL", "Mailpit n'écoute qu'en local"))
	out.append(("PASS" if "MP_" not in env and not env_files.strip() else "FAIL", "aucune variable MP_ ni fichier d'environnement"))
	if webui is None:
		out.append(("FAIL", "API Mailpit injoignable"))
	else:
		relay = (webui.get("MessageRelay") or {}).get("Enabled")
		out.append(("PASS" if relay is False else "FAIL", "API Mailpit : relais désactivé (Enabled=%s)" % relay))
	strays = [c for c in cmdlines if any(flag in c for flag in FORBIDDEN_MAILPIT_FLAGS)]
	out.append(("PASS" if not strays else "FAIL", "aucun processus mailpit avec relais, transfert ou webhook"))
	return out


def _systemctl_show(unit: str) -> dict:
	text = _systemctl(["show", unit, "-p", "ExecStart", "-p", "Environment", "-p", "EnvironmentFiles", "-p", "ActiveState", "-p", "UnitFileState"])
	show = {}
	for line in text.splitlines():
		if "=" in line:
			key, value = line.split("=", 1)
			show[key] = value
	return show


def _mailpit_cmdlines() -> list[str]:
	cmdlines = []
	for path in glob.glob("/proc/[0-9]*/cmdline"):
		try:
			with open(path, "rb") as fh:
				raw = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
		except OSError:
			continue
		first = raw.split(" ", 1)[0]
		if os.path.basename(first) == "mailpit":
			cmdlines.append(raw)
	return cmdlines


def _http_json(url: str, timeout: float = 5.0):
	try:
		with urllib.request.urlopen(url, timeout=timeout) as resp:
			return json.loads(resp.read().decode("utf-8"))
	except (OSError, ValueError):
		return None


def check_mailpit(conf: dict) -> list[tuple[str, str]]:
	return evaluate_mailpit_unit(
		_systemctl_show("mailpit-qa.service"), _http_json(conf["MAILPIT_API"] + "/api/v1/webui"), _mailpit_cmdlines()
	)


# --- Sites (P8 et bancs, contrat §5) -----------------------------------------------


def evaluate_site(site: str, conf: dict, site_conf: dict, accounts: list[dict], hooks: list, signals: dict) -> list[tuple[str, str]]:
	"""Fonction pure : juge la configuration de courriel d'un site."""
	out = []
	label = "site %s :" % site
	qa = conf["QA_ADDRESS"].lower()
	mail_server = (site_conf.get("mail_server") or "").strip()
	if mail_server and not (mail_server in LOCAL_SMTP_HOSTS and str(site_conf.get("mail_port") or "") == conf["MAILPIT_SMTP_PORT"]):
		out.append(("FAIL", "%s mail_server=%s (seul Mailpit local est permis)" % (label, mail_server)))
	for acc in accounts:
		name = acc.get("name")
		if (acc.get("service") or "") == "Frappe Mail":
			out.append(("FAIL", "%s Email Account « %s » de service Frappe Mail (envoi HTTPS)" % (label, name)))
		if acc.get("enable_outgoing"):
			server = (acc.get("smtp_server") or "").strip()
			port = str(acc.get("smtp_port") or "")
			if server not in LOCAL_SMTP_HOSTS or port != conf["MAILPIT_SMTP_PORT"]:
				out.append(("FAIL", "%s Email Account « %s » sort vers %s:%s, pas vers Mailpit" % (label, name, server or "?", port or "?")))
		if acc.get("enable_incoming"):
			login = ((acc.get("login_id") or "") if acc.get("login_id_is_different") else "") or (acc.get("email_id") or "")
			if login.strip().lower() != qa:
				out.append(("FAIL", "%s Email Account entrant « %s » vers %s : seule la boîte QA est permise" % (label, name, login or "?")))
			elif site != conf["DEV_SITE"]:
				out.append(("FAIL", "%s Email Account entrant vers %s : boîte QA réservée à %s" % (label, qa, conf["DEV_SITE"])))
	if hooks:
		out.append(("FAIL", "%s crochet override_email_send présent : %s" % (label, ", ".join(map(str, hooks)))))
	for key, value in sorted(signals.items()):
		if value:
			out.append(("SIGNAL", "%s %s actif(s) : %s (hors courriel, à examiner)" % (label, key, value)))
	if not any(status == "FAIL" for status, _ in out):
		out.append(("PASS", "%s messagerie conforme (mute_emails=%s)" % (label, site_conf.get("mute_emails"))))
	return out


def site_check(bench: str, site: str) -> dict:
	"""Lecture seule d'un site ; s'exécute avec le Python du banc."""
	import frappe

	frappe.init(site=site, sites_path=os.path.join(bench, "sites"))
	frappe.connect()
	try:
		fields = ["name", "service", "email_id", "login_id", "login_id_is_different", "enable_outgoing", "enable_incoming", "smtp_server", "smtp_port", "email_server"]
		meta = frappe.get_meta("Email Account")
		accounts = frappe.get_all("Email Account", fields=[f for f in fields if f == "name" or meta.has_field(f)])
		signals = {
			"Webhook": frappe.db.count("Webhook", {"enabled": 1}) if frappe.db.exists("DocType", "Webhook") else 0,
			"Slack Webhook URL": frappe.db.count("Slack Webhook URL") if frappe.db.exists("DocType", "Slack Webhook URL") else 0,
			"SMS Settings": 1 if frappe.db.get_single_value("SMS Settings", "sms_gateway_url") else 0,
		}
		return {
			"site_conf": {k: frappe.conf.get(k) for k in ("mail_server", "mail_port", "mute_emails")},
			"accounts": accounts,
			"hooks": list(frappe.get_hooks("override_email_send") or []),
			"signals": signals,
		}
	finally:
		frappe.db.rollback()
		frappe.destroy()


def iter_sites(bench_glob: str):
	"""Rend (banc, site) pour chaque site de chaque banc de la machine."""
	for apps_txt in sorted(glob.glob(bench_glob)):
		sites_dir = os.path.dirname(apps_txt)
		bench = os.path.dirname(sites_dir)
		for conf_path in sorted(glob.glob(os.path.join(sites_dir, "*", "site_config.json"))):
			yield bench, os.path.basename(os.path.dirname(conf_path))


def check_sites(conf: dict) -> list[tuple[str, str]]:
	out = []
	for bench, site in iter_sites(conf["BENCH_GLOB"]):
		python = os.path.join(bench, "env", "bin", "python")
		if not os.path.exists(python):
			out.append(("FAIL", "banc %s : Python introuvable, site %s non contrôlé" % (bench, site)))
			continue
		proc = subprocess.run(
			[python, "-I", os.path.abspath(__file__), "site-check", "--bench", bench, "--site", site],
			capture_output=True, text=True, timeout=300, cwd=os.path.join(bench, "sites"),
		)
		line = proc.stdout.strip().splitlines()[-1:] if proc.stdout.strip() else []
		try:
			data = json.loads(line[0]) if line else None
		except ValueError:
			data = None
		if proc.returncode != 0 or data is None:
			out.append(("FAIL", "banc %s, site %s : contrôle impossible (code %s)" % (bench, site, proc.returncode)))
			continue
		out.extend(evaluate_site(site, conf, data["site_conf"], data["accounts"], data["hooks"], data["signals"]))
	return out


def cmd_verify(args) -> int:
	conf = load_conf(args.conf)
	results = []
	for section in (check_network, lambda c: check_persistence(), check_mailpit, check_sites):
		try:
			results.extend(section(conf))
		except Exception as exc:  # un contrôle qui casse ne vaut jamais PASS
			results.append(("FAIL", "contrôle interrompu : %s" % exc))
	for status, text in results:
		print("%-6s %s" % (status, text))
	print("INFO   " + HTTPS_LIMIT)
	failed = any(status == "FAIL" for status, _ in results)
	print("VERIFY_MAIL_QA=" + ("FAIL" if failed else "PASS"))
	return 1 if failed else 0


# --- Réponse synthétique : APPEND, injection, relève -------------------------------


def fetch_original(conf: dict, mailpit_id: str) -> dict:
	data = _http_json("%s/api/v1/message/%s" % (conf["MAILPIT_API"], mailpit_id))
	if not data:
		raise SystemExit("message Mailpit %s introuvable" % mailpit_id)
	return data


def _frappe_connect(bench: str, site: str):
	import frappe

	frappe.init(site=site, sites_path=os.path.join(bench, "sites"))
	frappe.connect()
	return frappe


def cmd_reply(args) -> int:
	conf = load_conf(args.conf)
	msg = build_reply(fetch_original(conf, args.mailpit_id), args.body or "", args.from_addr, args.attach or [])
	if args.dry_run:
		print(msg.as_string())
		return 0
	if args.site != conf["DEV_SITE"]:
		raise SystemExit("reply (APPEND dans la boîte QA) est réservé à %s ; utiliser inject sur un banc" % conf["DEV_SITE"])
	frappe = _frappe_connect(args.bench, args.site)
	try:
		from frappe.utils.password import get_decrypted_password

		acc = frappe.get_doc("Email Account", args.account)
		login = (acc.login_id if acc.get("login_id_is_different") else "") or acc.email_id
		if (login or "").lower() != conf["QA_ADDRESS"] or (acc.email_server or "").lower() != conf["MAIL_HOST"]:
			raise SystemExit("l'Email Account « %s » ne vise pas %s sur %s : refus" % (args.account, conf["QA_ADDRESS"], conf["MAIL_HOST"]))
		password = get_decrypted_password("Email Account", acc.name, "password", raise_exception=True)
		import imaplib
		import time

		check_marks(msg)
		imap = imaplib.IMAP4_SSL(conf["MAIL_HOST"], int(acc.incoming_port or 993))
		try:
			imap.login(login, password)
			status, data = imap.append("INBOX", "", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
		finally:
			try:
				imap.logout()
			except Exception:
				pass
		del password
		print("APPEND %s %s Message-ID=%s" % (status, (data or [b""])[0].decode("utf-8", "replace"), msg["Message-ID"]))
		return 0 if status == "OK" else 1
	finally:
		frappe.db.rollback()
		frappe.destroy()


def cmd_inject(args) -> int:
	conf = load_conf(args.conf)
	msg = build_reply(fetch_original(conf, args.mailpit_id), args.body or "", args.from_addr, args.attach or [])
	frappe = _frappe_connect(args.bench, args.site)
	try:
		from frappe.email.receive import InboundMail

		check_marks(msg)
		acc = frappe.get_doc("Email Account", args.account)
		communication = InboundMail(msg.as_bytes(), acc).process()
		frappe.db.commit()
		print("INJECT OK Communication=%s reference=%s/%s" % (communication.name, communication.reference_doctype, communication.reference_name))
		return 0
	finally:
		frappe.destroy()


def cmd_pull(args) -> int:
	frappe = _frappe_connect(args.bench, args.site)
	try:
		from frappe.email.doctype.email_account.email_account import pull_from_email_account

		pull_from_email_account(args.account)
		frappe.db.commit()
		print("PULL OK %s" % args.account)
		return 0
	finally:
		frappe.destroy()


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--conf", default=None)
	sub = parser.add_subparsers(dest="cmd", required=True)
	sub.add_parser("verify")
	p = sub.add_parser("site-check")
	p.add_argument("--bench", required=True)
	p.add_argument("--site", required=True)
	for name in ("reply", "inject"):
		p = sub.add_parser(name)
		p.add_argument("--bench", default="/home/frappe/frappe-bench")
		p.add_argument("--site", required=True)
		p.add_argument("--account", default="QA Arkonex")
		p.add_argument("--mailpit-id", required=True)
		p.add_argument("--body", default="")
		p.add_argument("--from", dest="from_addr", default=None)
		p.add_argument("--attach", action="append")
		if name == "reply":
			p.add_argument("--dry-run", action="store_true")
	p = sub.add_parser("pull")
	p.add_argument("--bench", default="/home/frappe/frappe-bench")
	p.add_argument("--site", required=True)
	p.add_argument("--account", default="QA Arkonex")
	args = parser.parse_args(argv)
	if args.cmd == "verify":
		return cmd_verify(args)
	if args.cmd == "site-check":
		print(json.dumps(site_check(args.bench, args.site), default=str))
		return 0
	return {"reply": cmd_reply, "inject": cmd_inject, "pull": cmd_pull}[args.cmd](args)


if __name__ == "__main__":
	sys.exit(main())
