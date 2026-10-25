#!/usr/bin/env python3
"""mail_qa.py — capacité d'essai de courriel d'Arkonex (OPEN-202, Issue #351, mode M1 + tranche T2).

Contrat : docs/open_202/devops_mail_qa_m1_contrat.md (v0.3.0, §13).

Une capacité GÉNÉRIQUE : aucun nom de DocType métier n'est écrit ici. Elle s'appuie sur les
mécanismes natifs de Frappe (Email Queue, Communication, réception native, fil de réponse,
`append_to`, permissions du document). Les parties fictives (C11) vivent dans
`mail_qa_parties.py` et `parties.json` : c'est le seul endroit qui nomme des DocTypes.

Sous-commandes (même sémantique en API Python : `sys.path.insert(0, ".../mail-qa"); import mail_qa`) :
  verify         lecture seule. P1, P2 et chaque site de chaque banc. Code 0 = PASS, 1 = FAIL.
  site-check     lecture seule, un site (lancé par `verify` avec le Python du banc).
  address        C1 : adresse de partie fictive `nom[+essai]@essai.invalid`.
  inbox ensure   C2 : Email Account d'entrée simulé (sans réseau), `append_to` facultatif.
  flush          C3 : vide la file « Not Sent » vers Mailpit et rend le statut.
  captured       C3 : messages capturés par Mailpit (destinataires, pièces, Message-ID, origine).
  reply          C4 : réponse synthétique à un message capturé, `--via inject|imap`.
  inject         alias de `reply --via inject`.
  inbound        C5 : message entrant indépendant, `--via inject|imap`.
  pull           relève d'un Email Account (QA, DEV seulement).
  window         C7 : relève les effets d'une action (commande mail-qa imbriquée).
  visibility     C8 : ce que chaque utilisateur voit dans la chronologie d'un document.
  can-send       C8 : droit d'envoi d'un utilisateur, éprouvé puis annulé.
  parties        C11 : catalogue de parties fictives (show, validate, ensure).
  purge          rétention : supprime de Mailpit les seuls messages d'un identifiant d'essai.

Toute commande qui ÉCRIT exige un `verify` PASS (préflight imposé, cache de 5 minutes).
Rien n'est jamais envoyé par SMTP vers l'extérieur. Aucun mot de passe n'est affiché.
Limite connue (contrat §3) : les services d'envoi par HTTPS ne sont pas bloqués par le
réseau ; `verify` les tient par contrôle.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import errno
import re
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":
	# Lancé comme script : les modules frères importent `mail_qa` et doivent retrouver CETTE copie
	# (même état : verrou de préflight, essais), pas une seconde copie rechargée.
	sys.modules.setdefault("mail_qa", sys.modules["__main__"])

# --- Marques des messages synthétiques (contrat §6) -------------------------------
MARK_HEADER = "X-Arkonex-Essai"
MARK_HEADER_VALUE = "synthetique"
MARK_ID_HEADER = "X-Arkonex-Essai-Id"
MARK_SUBJECT = "[ESSAI SYNTHÉTIQUE]"
MARK_BODY_FIRST_LINE = "Message d'essai fabriqué — aucune valeur d'acceptation"
MARK_DOMAIN = "essai.invalid"

# Variantes déclarées (C6). Le sujet vide et le corps vide ne retirent QUE leur propre marque ;
# l'en-tête et l'expéditeur @essai.invalid restent toujours exigés.
VARIANTS = ("sans-sujet", "sans-corps", "sans-piece", "expediteur-inconnu", "piece-lourde", "reponse-inconnue")
HEAVY_DEFAULT_BYTES = 5 * 1024 * 1024
ESSAI_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PARTY_LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

PREFLIGHT_TTL = 300  # secondes : durée de vie du résultat de `verify` mis en cache

SMTP_PORTS = (25, 465, 587, 2525)
LOCAL_SMTP_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUIRED_ACCOUNT_FIELDS = ("service", "email_id", "enable_outgoing", "enable_incoming", "smtp_server", "smtp_port")
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
	"""Un message synthétique ne porte pas toutes ses marques (ou sa variante est inconnue)."""


class PreflightError(Exception):
	"""`verify` n'est pas PASS : aucune écriture n'est permise."""


class GuardError(Exception):
	"""Une garde de l'outil refuse l'opération."""


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


def _sibling(name: str):
	"""Importe un module frère (le Python des bancs est lancé en mode isolé : -I)."""
	if HERE not in sys.path:
		sys.path.insert(0, HERE)
	return importlib.import_module(name)


# --- C1 : parties externes fictives -------------------------------------------------


def new_essai_id() -> str:
	"""Identifiant d'essai : sépare les lots qui tournent en même temps."""
	return "e" + os.urandom(4).hex()


def check_essai_id(essai: str | None) -> str | None:
	if essai is None or essai == "":
		return None
	essai = str(essai).strip().lower()
	if not ESSAI_ID_RE.match(essai):
		raise ValueError("identifiant d'essai invalide (a-z, 0-9 et tirets, 31 caractères au plus) : %r" % essai)
	return essai


def is_party_address(address: str | None) -> bool:
	"""Vrai seulement pour une adresse nue `<local>@essai.invalid` (aucune autre adresse n'est une partie)."""
	addr = (address or "").strip().lower()
	if addr.count("@") != 1 or any(c in addr for c in ' <>,;:"()[]\\\t\r\n'):
		return False
	local, domain = addr.split("@")
	return bool(local) and domain == MARK_DOMAIN


def require_party_address(address: str | None, role: str = "expéditeur d'un message synthétique") -> str:
	if not is_party_address(address):
		raise ValueError("%s : une adresse en @%s est exigée (reçu : %r)" % (role, MARK_DOMAIN, address))
	return address.strip()


def party_address(name: str, essai: str | None = None) -> str:
	"""`nom@essai.invalid`, ou `nom+essai@essai.invalid` pour séparer les lots parallèles."""
	local = (name or "").strip().lower()
	if "+" in local or not PARTY_LOCAL_RE.match(local):
		raise ValueError("nom de partie invalide (a-z, 0-9, point, tiret, souligné ; sans « + ») : %r" % name)
	essai = check_essai_id(essai)
	return "%s%s@%s" % (local, "+" + essai if essai else "", MARK_DOMAIN)


def address_essai_tag(address: str | None) -> str | None:
	"""Identifiant d'essai porté par une adresse `nom+essai@domaine`, sinon None."""
	local = (address or "").strip().lower().split("@", 1)[0]
	return local.split("+", 1)[1] if "+" in local else None


# --- Messages synthétiques ----------------------------------------------------------


def check_variants(variants) -> tuple:
	"""Refuse toute variante inconnue ou incompatible (C6)."""
	variants = tuple(dict.fromkeys(variants or ()))
	unknown = [v for v in variants if v not in VARIANTS]
	if unknown:
		raise MarkError("variante inconnue : %s (permises : %s)" % (", ".join(unknown), ", ".join(VARIANTS)))
	if "sans-piece" in variants and "piece-lourde" in variants:
		raise MarkError("variantes incompatibles : sans-piece et piece-lourde")
	return variants


def check_marks(msg: EmailMessage, variants=()) -> None:
	"""Lève MarkError si une marque exigée manque.

	Toujours exigés : l'en-tête et l'expéditeur @essai.invalid (seul expéditeur, aucune autre
	adresse d'expédition). Exigés sauf variante qui les retire : le préfixe du sujet (sans-sujet
	exige alors un sujet VIDE) et la première ligne du corps (sans-corps exige un corps VIDE).
	"""
	variants = check_variants(variants)
	missing = []
	if (msg.get(MARK_HEADER) or "").strip() != MARK_HEADER_VALUE:
		missing.append("en-tête " + MARK_HEADER)
	subject = str(msg.get("Subject") or "")
	if "sans-sujet" in variants:
		if subject.strip():
			missing.append("sujet vide (variante sans-sujet)")
	elif not subject.startswith(MARK_SUBJECT):
		missing.append("préfixe du sujet")
	from_values = msg.get_all("From") or []
	senders = [a for _n, a in getaddresses([str(v) for v in from_values])]
	if len(from_values) != 1 or len(senders) != 1 or not is_party_address(senders[0]):
		missing.append("expéditeur @" + MARK_DOMAIN)
	for header in ("Sender", "Reply-To", "Return-Path"):
		for _name, addr in getaddresses([str(v) for v in (msg.get_all(header) or [])]):
			if addr and not is_party_address(addr):
				missing.append("%s hors @%s" % (header, MARK_DOMAIN))
	body = msg.get_body(preferencelist=("plain",))
	text = body.get_content() if body else ""
	if "sans-corps" in variants:
		if text.strip():
			missing.append("corps vide (variante sans-corps)")
	elif text.lstrip().splitlines()[:1] != [MARK_BODY_FIRST_LINE]:
		missing.append("première ligne du corps")
	attachments = list(msg.iter_attachments())
	if "sans-piece" in variants and attachments:
		missing.append("aucune pièce jointe (variante sans-piece)")
	if "piece-lourde" in variants and not attachments:
		missing.append("pièce jointe volumineuse (variante piece-lourde)")
	if missing:
		raise MarkError("marques manquantes : " + ", ".join(missing))


def build_message(
	*, from_addr: str, to_addr: str, subject: str, body: str = "", in_reply_to: str | None = None,
	attachments=(), variants=(), essai: str | None = None, heavy_bytes: int = HEAVY_DEFAULT_BYTES,
) -> EmailMessage:
	"""Fabrique un message synthétique (réponse ou message indépendant). Les marques sont imposées."""
	variants = check_variants(variants)
	essai = check_essai_id(essai)
	require_party_address(from_addr)
	if "sans-piece" in variants and attachments:
		raise ValueError("la variante sans-piece est incompatible avec des pièces jointes")
	if "reponse-inconnue" in variants:
		in_reply_to = make_msgid(idstring="inconnu", domain=MARK_DOMAIN).strip("<>")
	msg = EmailMessage(policy=policy.SMTP)
	msg["From"] = "Partie externe d'essai <%s>" % from_addr
	msg["To"] = to_addr
	msg["Subject"] = "" if "sans-sujet" in variants else "%s %s" % (MARK_SUBJECT, subject)
	msg["Date"] = formatdate(localtime=True)
	msg["Message-ID"] = make_msgid(domain=MARK_DOMAIN)
	if in_reply_to:
		msg["In-Reply-To"] = "<%s>" % in_reply_to.strip("<>")
		msg["References"] = "<%s>" % in_reply_to.strip("<>")
	msg[MARK_HEADER] = MARK_HEADER_VALUE
	if essai:
		msg[MARK_ID_HEADER] = essai
	if "sans-corps" in variants:
		msg.set_content("")
	else:
		msg.set_content(MARK_BODY_FIRST_LINE + "\n\n" + (body or "Message d'essai.") + "\n")
	for path in attachments:
		with open(path, "rb") as fh:
			data = fh.read()
		msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=os.path.basename(path))
	if "piece-lourde" in variants:
		if heavy_bytes < 1:
			raise ValueError("taille de la pièce volumineuse invalide")
		msg.add_attachment(b"\0" * heavy_bytes, maintype="application", subtype="octet-stream", filename="piece-lourde.bin")
	check_marks(msg, variants)
	return msg


def resolve_sender(variants, from_addr: str | None, default: str | None, essai: str | None) -> str:
	"""Expéditeur d'un message fabriqué : toujours une partie @essai.invalid (C1, C6)."""
	if "expediteur-inconnu" in variants:
		if from_addr:
			raise ValueError("la variante expediteur-inconnu génère elle-même l'expéditeur : retirer --from")
		return party_address("inconnu-" + os.urandom(3).hex(), essai)
	chosen = from_addr or default
	if not chosen:
		raise ValueError("aucun expéditeur @%s disponible : préciser --from" % MARK_DOMAIN)
	return require_party_address(chosen)


def build_reply(
	original: dict, body: str = "", from_addr: str | None = None, attachments=(), variants=(),
	essai: str | None = None, heavy_bytes: int = HEAVY_DEFAULT_BYTES,
) -> EmailMessage:
	"""Construit la réponse synthétique à un message Mailpit (/api/v1/message/<id>).

	`original` porte au minimum MessageID, Subject, From{Address} et To[{Address}].
	L'expéditeur de la réponse est le premier destinataire d'origine (À puis Cc) en @essai.invalid,
	ou `from_addr`, qui doit lui-même être en @essai.invalid.
	"""
	variants = check_variants(variants)
	message_id = (original.get("MessageID") or "").strip().strip("<>")
	if not message_id:
		raise ValueError("le message d'origine n'a pas de Message-ID")
	to_addr = ((original.get("From") or {}).get("Address") or "").strip()
	if not to_addr:
		raise ValueError("le message d'origine n'a pas d'expéditeur")
	default = None
	for rcpt in list(original.get("To") or []) + list(original.get("Cc") or []):
		address = (rcpt.get("Address") or "").strip()
		if is_party_address(address):
			default = address
			break
	if not from_addr and "expediteur-inconnu" not in variants and not default:
		raise ValueError("aucun destinataire d'origine en @%s : préciser --from" % MARK_DOMAIN)
	sender = resolve_sender(variants, from_addr, default, essai)
	subject = (original.get("Subject") or "").strip()
	if not subject.lower().startswith("re:"):
		subject = "Re: " + subject
	return build_message(
		from_addr=sender, to_addr=to_addr, subject=subject, body=body or "Réponse d'essai.",
		in_reply_to=message_id, attachments=attachments, variants=variants, essai=essai, heavy_bytes=heavy_bytes,
	)


def build_inbound(
	*, to_addr: str, from_addr: str | None = None, subject: str = "", body: str = "", attachments=(),
	variants=(), essai: str | None = None, heavy_bytes: int = HEAVY_DEFAULT_BYTES,
) -> EmailMessage:
	"""Message entrant indépendant (C5) : il ne répond à rien (sauf la variante reponse-inconnue)."""
	variants = check_variants(variants)
	sender = resolve_sender(variants, from_addr, None, essai)
	return build_message(
		from_addr=sender, to_addr=to_addr, subject=subject or "Message d'essai indépendant",
		body=body or "Message entrant d'essai.", attachments=attachments, variants=variants, essai=essai,
		heavy_bytes=heavy_bytes,
	)


# --- Réseau (P1) -------------------------------------------------------------------


OPEN, REFUSED, TIMEOUT, NO_ROUTE, UNRESOLVED = "ouverte", "refusée", "délai dépassé", "sans route", "non résolu"


def classify_connect_error(exc: OSError) -> str:
	"""Distingue un refus actif (RST : la règle P1, ou un port fermé) d'une absence de réponse."""
	if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
		return REFUSED
	if isinstance(exc, (TimeoutError, socket.timeout)):
		return TIMEOUT
	if getattr(exc, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
		return NO_ROUTE
	return TIMEOUT


def tcp_open(host: str, port: int, timeout: float = 4.0) -> list[tuple[str, int, str]]:
	"""Tente une ouverture TCP vers chaque adresse de `host`, sans rien envoyer ni lire.

	Retourne [(adresse, famille, état)]. La connexion est refermée aussitôt : aucun
	dialogue SMTP n'a lieu, donc aucun message ne peut partir.
	"""
	results = []
	try:
		infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except socket.gaierror:
		return [(host, 0, UNRESOLVED)]
	seen = set()
	for family, socktype, proto, _canon, addr in infos:
		if addr[0] in seen:
			continue
		seen.add(addr[0])
		sock = socket.socket(family, socktype, proto)
		sock.settimeout(timeout)
		try:
			sock.connect(addr)
			results.append((addr[0], family, OPEN))
		except OSError as exc:
			results.append((addr[0], family, classify_connect_error(exc)))
		finally:
			sock.close()
	return results


def evaluate_smtp_probe(host: str, port: int, addr: str, family: int, state: str) -> tuple[str, str]:
	"""Fonction pure : seul un refus actif prouve P1 en IPv4.

	P1 rejette localement par RST, avant tout filtrage distant : avec P1 chargé, chaque
	sonde IPv4 est « refusée » immédiatement. Un délai dépassé n'est pas probant (filtrage
	distant, AWS) : FAIL. En IPv6, une absence de route rend la sortie impossible mais
	n'exerce pas P1 : INFO.
	"""
	text = "sortie SMTP %s:%s (%s) %s" % (host, port, addr, state)
	if state == OPEN:
		return ("FAIL", text + " — SORTIE POSSIBLE")
	if state == REFUSED:
		return ("PASS", text)
	if family == socket.AF_INET6 and state == NO_ROUTE:
		return ("INFO", text + " — aucune route IPv6 : sortie impossible, P1 non exercé en IPv6")
	return ("FAIL", text + " — non probant (P1 rejette par RST : un refus immédiat est attendu)")


def check_network(conf: dict) -> list[tuple[str, str]]:
	"""P1 : ports SMTP fermés vers l'extérieur ; IMAP 993 et Mailpit local joignables."""
	out = []
	for host in (conf["MAIL_HOST"], conf["WITNESS_HOST"]):
		for port in SMTP_PORTS:
			for addr, family, state in tcp_open(host, port):
				out.append(evaluate_smtp_probe(host, port, addr, family, state))
	imap = tcp_open(conf["MAIL_HOST"], 993)
	out.append(("PASS" if any(st == OPEN for _a, _f, st in imap) else "FAIL", "IMAP %s:993 joignable" % conf["MAIL_HOST"]))
	local = tcp_open(conf["MAILPIT_SMTP_HOST"], int(conf["MAILPIT_SMTP_PORT"]))
	out.append(("PASS" if any(st == OPEN for _a, _f, st in local) else "FAIL", "Mailpit SMTP local joignable"))
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


def exec_argv(exec_start: str) -> list[str]:
	"""Extrait argv d'une valeur ExecStart de `systemctl show` (« argv[]=… ; »)."""
	if "argv[]=" in exec_start:
		exec_start = exec_start.split("argv[]=", 1)[1].split(" ;", 1)[0]
	try:
		return shlex.split(exec_start)
	except ValueError:
		return exec_start.split()


def option_values(argv: list[str], *names: str) -> list[str]:
	"""Valeurs de toutes les occurrences d'une option (formes « --x v », « --x=v », « -s v »)."""
	values = []
	for i, arg in enumerate(argv):
		for name in names:
			if arg == name and i + 1 < len(argv):
				values.append(argv[i + 1])
			elif arg.startswith(name + "="):
				values.append(arg.split("=", 1)[1])
	return values


def evaluate_mailpit_unit(show: dict, webui: dict | None, cmdlines: list[str]) -> list[tuple[str, str]]:
	"""Fonction pure : juge l'unité, l'API et les processus Mailpit."""
	out = []
	argv = exec_argv(show.get("ExecStart", ""))
	env = show.get("Environment", "")
	env_files = show.get("EnvironmentFiles", "")
	out.append(("PASS" if show.get("UnitFileState") == "enabled" else "FAIL", "mailpit-qa activé au démarrage"))
	out.append(("PASS" if show.get("ActiveState") == "active" else "FAIL", "mailpit-qa actif"))
	bad = sorted({flag for flag in FORBIDDEN_MAILPIT_FLAGS for arg in argv if arg == flag or arg.startswith(flag + "=")})
	out.append(("PASS" if not bad else "FAIL", "unité sans relais ni transfert ni webhook" + (" : " + ", ".join(bad) if bad else "")))
	smtp = option_values(argv, "--smtp", "-s")
	listen = option_values(argv, "--listen", "-l")
	pop3 = option_values(argv, "--pop3", "--pop3-auth-file")
	local_ok = smtp == ["127.0.0.1:1025"] and listen == ["127.0.0.1:8025"] and not pop3
	out.append(("PASS" if local_ok else "FAIL", "Mailpit n'écoute qu'en local (smtp=%s, listen=%s)" % (smtp, listen)))
	out.append(("PASS" if "MP_" not in env and not env_files.strip() else "FAIL", "aucune variable MP_ ni fichier d'environnement dans l'unité"))
	deny = show.get("IPAddressDeny", "")
	out.append(("PASS" if "0.0.0.0/0" in deny and "::/0" in deny else "FAIL", "unité confinée au réseau local (IPAddressDeny=%s)" % (deny or "?")))
	if webui is None:
		out.append(("FAIL", "API Mailpit injoignable"))
	else:
		relay = (webui.get("MessageRelay") or {}).get("Enabled")
		out.append(("PASS" if relay is False else "FAIL", "API Mailpit : relais désactivé (Enabled=%s)" % relay))
	strays = [c for c in cmdlines if any(flag in c for flag in FORBIDDEN_MAILPIT_FLAGS)]
	out.append(("PASS" if not strays else "FAIL", "aucun processus mailpit avec relais, transfert ou webhook"))
	return out


def _systemctl_show(unit: str) -> dict:
	text = _systemctl(["show", unit, "-p", "ExecStart", "-p", "Environment", "-p", "EnvironmentFiles", "-p", "ActiveState", "-p", "UnitFileState", "-p", "IPAddressDeny"])
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


def gather_site(frappe) -> dict:
	"""Lit la configuration de courriel du site connecté (lecture seule)."""
	meta = frappe.get_meta("Email Account")
	missing = [f for f in REQUIRED_ACCOUNT_FIELDS if not meta.has_field(f)]
	if missing:
		raise RuntimeError("champs Email Account introuvables (version de Frappe ?) : " + ", ".join(missing))
	fields = ["name", *REQUIRED_ACCOUNT_FIELDS, "email_server"]
	fields += [f for f in ("login_id", "login_id_is_different") if meta.has_field(f)]
	accounts = frappe.get_all("Email Account", fields=fields)
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


def site_check(bench: str, site: str) -> dict:
	"""Lecture seule d'un site ; s'exécute avec le Python du banc."""
	import frappe

	frappe.init(site=site, sites_path=os.path.join(bench, "sites"))
	frappe.connect()
	try:
		return gather_site(frappe)
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


def collect_verify(conf: dict) -> list[tuple[str, str]]:
	"""Exécute tous les contrôles de `verify` et rend [(statut, texte)]."""
	results = []
	for section in (check_network, lambda c: check_persistence(), check_mailpit, check_sites):
		try:
			results.extend(section(conf))
		except Exception as exc:  # un contrôle qui casse ne vaut jamais PASS
			results.append(("FAIL", "contrôle interrompu : %s" % exc))
	return results


def cmd_verify(args) -> int:
	conf = load_conf(args.conf)
	results = collect_verify(conf)
	for status, text in results:
		print("%-6s %s" % (status, text))
	print("INFO   " + HTTPS_LIMIT)
	failed = any(status == "FAIL" for status, _ in results)
	print("VERIFY_MAIL_QA=" + ("FAIL" if failed else "PASS"))
	return 1 if failed else 0


# --- Préflight imposé (contrat §13.4) ----------------------------------------------

# Réservés aux essais unitaires : remplacent `collect_verify` et le dossier du cache. Ils ne sont
# jamais lus dans l'environnement ni en ligne de commande.
_VERIFY_RUNNER = None
PREFLIGHT_CACHE_PATH = None


def _cache_path() -> str:
	if PREFLIGHT_CACHE_PATH:
		return PREFLIGHT_CACHE_PATH
	return os.path.join(os.path.expanduser("~"), ".cache", "arkonex-mail-qa", "verify.json")


def _read_cache(path: str, now: float, ttl: int):
	"""Rend l'entrée du cache si elle est un PASS récent, d'un dossier et d'un fichier à nous seuls."""
	try:
		st, dst = os.stat(path), os.stat(os.path.dirname(path))
		if st.st_uid != os.getuid() or dst.st_uid != os.getuid() or (st.st_mode | dst.st_mode) & 0o022:
			return None
		with open(path, encoding="utf-8") as fh:
			data = json.load(fh)
	except (OSError, ValueError):
		return None
	at = data.get("at") if isinstance(data, dict) else None
	if data.get("result") != "PASS" or not isinstance(at, (int, float)) or not 0 <= now - at <= ttl:
		return None
	return data


def _write_cache(path: str, now: float) -> None:
	directory = os.path.dirname(path)
	os.makedirs(directory, mode=0o700, exist_ok=True)
	tmp = path + ".%d.tmp" % os.getpid()
	fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as fh:
		json.dump({"result": "PASS", "at": now}, fh)
	os.replace(tmp, path)


def require_preflight(conf: dict, now: float | None = None) -> str:
	"""Refuse (PreflightError) si `verify` n'est pas PASS. Rend « cache » ou « verify ».

	Le résultat PASS est mis en cache 5 minutes au plus ; un FAIL n'est jamais mis en cache.
	"""
	now = time.time() if now is None else now
	path = _cache_path()
	if _read_cache(path, now, PREFLIGHT_TTL):
		return "cache"
	results = (_VERIFY_RUNNER or collect_verify)(conf)
	failed = [text for status, text in results if status == "FAIL"]
	if failed:
		raise PreflightError("préflight refusé : verify n'est pas PASS (%s)" % " ; ".join(failed[:5]))
	try:
		_write_cache(path, now)
	except OSError:
		pass  # pas de cache : le prochain appel relancera verify, jamais d'écriture sans PASS
	return "verify"


def guard_allowed_site(site: str, conf: dict) -> None:
	"""L'outil n'agit que sur le site DEV ou un site de banc `*.local`."""
	if site != conf["DEV_SITE"] and not (site or "").endswith(".local"):
		raise GuardError("site %r refusé : seuls %s et les sites *.local sont permis" % (site, conf["DEV_SITE"]))


def write_guard(frappe, conf: dict) -> str:
	"""Garde commune de toute commande qui écrit : site permis, verify PASS, site conforme."""
	site = frappe.local.site
	guard_allowed_site(site, conf)
	source = require_preflight(conf)
	guard_site_compliant(frappe, conf, site)
	return source


# --- Mailpit (capture) -------------------------------------------------------------


def mailpit_request(conf: dict, method: str, path: str, payload=None, timeout: float = 20.0):
	data = json.dumps(payload).encode("utf-8") if payload is not None else None
	req = urllib.request.Request(conf["MAILPIT_API"] + path, data=data, method=method)
	if data is not None:
		req.add_header("Content-Type", "application/json")
	with urllib.request.urlopen(req, timeout=timeout) as resp:
		body = resp.read().decode("utf-8", "replace")
	try:
		return json.loads(body)
	except ValueError:
		return body


def mailpit_list(conf: dict, query: str | None = None, limit: int = 2000) -> list[dict]:
	"""Résumés des messages (les plus récents d'abord), recherche Mailpit facultative."""
	out, start = [], 0
	while len(out) < limit:
		page = min(200, limit - len(out))
		if query:
			path = "/api/v1/search?" + urllib.parse.urlencode({"query": query, "start": start, "limit": page})
		else:
			path = "/api/v1/messages?" + urllib.parse.urlencode({"start": start, "limit": page})
		data = mailpit_request(conf, "GET", path)
		messages = (data or {}).get("messages") or []
		out.extend(messages)
		start += len(messages)
		total = data.get("messages_count", data.get("total"))
		if not messages or (total is not None and start >= total) or len(messages) < page:
			break
	return out


def mailpit_ids(conf: dict) -> set[str]:
	return {m["ID"] for m in mailpit_list(conf)}


def _addrs(items) -> list[str]:
	return [(i.get("Address") or "").strip().lower() for i in (items or []) if i.get("Address")]


def summarize_message(detail: dict, created: str | None = None) -> dict:
	"""Forme stable d'un message Mailpit : destinataires, copies, pièces, Message-ID, marques d'essai."""
	sender = (detail.get("From") or {}).get("Address") or ""
	to, cc, bcc, reply_to = _addrs(detail.get("To")), _addrs(detail.get("Cc")), _addrs(detail.get("Bcc")), _addrs(detail.get("ReplyTo"))
	tags = sorted({t for a in [sender.lower(), *to, *cc, *bcc, *reply_to] for t in [address_essai_tag(a)] if t})
	return {
		"id": detail.get("ID"),
		"message_id": (detail.get("MessageID") or "").strip("<>"),
		"sujet": detail.get("Subject") or "",
		"de": sender.lower(),
		"a": to, "cc": cc, "bcc": bcc, "repondre_a": reply_to,
		"date": created or detail.get("Date"),
		"taille": detail.get("Size"),
		"pieces_jointes": [
			{"nom": a.get("FileName"), "type": a.get("ContentType"), "taille": a.get("Size")}
			for a in (detail.get("Attachments") or [])
		],
		"essais": tags,
	}


def parse_instant(value) -> datetime | None:
	"""Instant (datetime, epoch, ISO 8601). Sans fuseau : UTC, comme l'horloge de la machine."""
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		dt = value
	elif isinstance(value, (int, float)):
		dt = datetime.fromtimestamp(value, timezone.utc)
	else:
		dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def captured(
	conf: dict, *, to: str | None = None, subject: str | None = None, essai: str | None = None,
	since=None, limit: int = 50, frappe=None,
) -> list[dict]:
	"""C3 : messages capturés par Mailpit, filtrés par destinataire, sujet, identifiant d'essai, date.

	Avec `frappe`, chaque message reçoit son origine (courriel en file, Communication, document).
	Lecture seule : aucun préflight.
	"""
	essai = check_essai_id(essai)
	since_dt = parse_instant(since)
	parts = []
	if to:
		parts.append("addressed:" + to)
	if subject:
		parts.append('subject:"%s"' % subject.replace('"', ""))
	out = []
	for summary in mailpit_list(conf, " ".join(parts) or None):
		created = parse_instant(summary.get("Created"))
		if since_dt and created and created < since_dt:
			continue
		detail = mailpit_request(conf, "GET", "/api/v1/message/" + summary["ID"])
		item = summarize_message(detail, summary.get("Created"))
		if to and to.strip().lower() not in item["a"] + item["cc"] + item["bcc"]:
			continue
		if subject and subject.lower() not in item["sujet"].lower():
			continue
		if essai and essai not in item["essais"]:
			continue
		if frappe is not None:
			item["origine"] = origin_of(frappe, item["message_id"])
		out.append(item)
		if len(out) >= limit:
			break
	return out


def purge_essai(conf: dict, essai: str, dry_run: bool = False) -> dict:
	"""Rétention : supprime de Mailpit les SEULS messages dont une adresse porte `+<essai>`.

	Désignation exacte (comparaison de l'étiquette, jamais de sous-chaîne) ; suppression par
	identifiants. Les messages des autres essais et ceux sans étiquette ne sont jamais touchés.
	"""
	essai = check_essai_id(essai)
	if not essai:
		raise ValueError("un identifiant d'essai est exigé")
	if not dry_run:
		require_preflight(conf)
	ids = []
	for summary in mailpit_list(conf, "addressed:+%s@%s" % (essai, MARK_DOMAIN)):
		detail = mailpit_request(conf, "GET", "/api/v1/message/" + summary["ID"])
		if essai in summarize_message(detail)["essais"]:
			ids.append(summary["ID"])
	if ids and not dry_run:
		mailpit_request(conf, "DELETE", "/api/v1/messages", {"IDs": ids})
	return {"essai": essai, "messages": ids, "supprimes": 0 if dry_run else len(ids), "simulation": dry_run}


# --- Origine d'un message (C3) -----------------------------------------------------


def _by_message_id(frappe, doctype: str, mid: str):
	return frappe.db.get_value(doctype, {"message_id": mid}, "name") or frappe.db.get_value(
		doctype, {"message_id": "<%s>" % mid}, "name"
	)


def origin_of(frappe, message_id: str) -> dict:
	"""Courriel en file, Communication et document d'où vient un Message-ID (lecture seule)."""
	mid = (message_id or "").strip().strip("<>")
	out = {"email_queue": None, "communication": None, "reference_doctype": None, "reference_name": None, "liens": []}
	if not mid:
		return out
	queue = _by_message_id(frappe, "Email Queue", mid)
	comm = _by_message_id(frappe, "Communication", mid)
	if queue:
		row = frappe.db.get_value("Email Queue", queue, ["communication", "reference_doctype", "reference_name"], as_dict=True)
		out.update(email_queue=queue, reference_doctype=row.reference_doctype or None, reference_name=row.reference_name or None)
		comm = comm or row.communication
	if comm:
		doc = frappe.get_doc("Communication", comm)
		out["communication"] = comm
		if doc.reference_doctype:
			out["reference_doctype"], out["reference_name"] = doc.reference_doctype, doc.reference_name
		out["liens"] = [[l.link_doctype, l.link_name] for l in doc.get("timeline_links") or []]
	return out


# --- Frappe : connexion et gardes --------------------------------------------------


def fetch_original(conf: dict, mailpit_id: str) -> dict:
	data = _http_json("%s/api/v1/message/%s" % (conf["MAILPIT_API"], mailpit_id))
	if not data:
		raise SystemExit("message Mailpit %s introuvable" % mailpit_id)
	return data


def _frappe_connect(bench: str, site: str, conf: dict | None = None):
	guard_allowed_site(site, conf or load_conf())
	import frappe

	frappe.init(site=site, sites_path=os.path.join(bench, "sites"))
	frappe.connect()
	return frappe


def imap_ssl_context() -> ssl.SSLContext:
	"""Contexte TLS qui vérifie le certificat et le nom du serveur (revue U2, M1)."""
	return ssl.create_default_context()


def guard_qa_account(acc: dict, conf: dict, site: str) -> None:
	"""Refuse tout Email Account autre que la boîte QA sur le serveur prévu, hors DEV (revue U2, M3)."""
	login = ((acc.get("login_id") or "") if acc.get("login_id_is_different") else "") or (acc.get("email_id") or "")
	if site != conf["DEV_SITE"]:
		raise SystemExit("la boîte QA est réservée à %s : refus sur %s" % (conf["DEV_SITE"], site))
	if login.strip().lower() != conf["QA_ADDRESS"] or (acc.get("email_server") or "").strip().lower() != conf["MAIL_HOST"]:
		raise SystemExit("l'Email Account « %s » ne vise pas %s sur %s : refus" % (acc.get("name"), conf["QA_ADDRESS"], conf["MAIL_HOST"]))


def guard_site_compliant(frappe, conf: dict, site: str) -> None:
	"""Refuse d'agir sur un site dont la messagerie n'est pas conforme (envois hors Mailpit)."""
	data = gather_site(frappe)
	failures = [t for st, t in evaluate_site(site, conf, data["site_conf"], data["accounts"], data["hooks"], data["signals"]) if st == "FAIL"]
	if failures:
		raise SystemExit("site non conforme, refus : " + " ; ".join(failures))


def agreement_signals(frappe, addresses) -> list[dict]:
	"""C9 / #347 : signale chaque expéditeur synthétique qui est l'adresse d'un contact enregistré.

	Une Communication ne vaut pièce d'accord que si son expéditeur est un contact enregistré
	du client du contrat : un tel message synthétique peut donc compter comme pièce d'accord.
	Le signalement ne bloque rien (garde levée, décision #347, échange 23).
	"""
	parties = _sibling("mail_qa_parties")
	out = []
	for address in dict.fromkeys(a.strip().lower() for a in addresses if a):
		contacts = parties.contacts_for_address(frappe, address)
		if contacts:
			out.append({"adresse": address, "contacts": contacts, "piece_d_accord_possible": True})
	return out


# --- C2 : boîtes de réception fictives ---------------------------------------------


def ensure_inbox(frappe, conf: dict, address: str, append_to: str | None = None, name: str | None = None) -> dict:
	"""Crée ou réutilise un Email Account d'entrée SIMULÉ : jamais de relève réseau, jamais d'envoi."""
	write_guard(frappe, conf)
	address = require_party_address(address, "adresse de la boîte simulée").lower()
	if append_to:
		from frappe.email.doctype.email_account.email_account import get_append_to

		valid = sorted(d[0] for d in get_append_to())
		if append_to not in valid:
			raise GuardError("append_to %r invalide (permis : %s)" % (append_to, ", ".join(valid)))
	existing = frappe.db.get_value("Email Account", {"email_id": address}, "name")
	try:
		if existing:
			doc = frappe.get_doc("Email Account", existing)
			if doc.enable_incoming or doc.enable_outgoing or doc.use_imap:
				raise GuardError("l'Email Account « %s » existe mais n'est pas une boîte simulée (réception, envoi ou IMAP actif)" % existing)
			created = False
			if append_to and doc.append_to != append_to:
				doc.append_to = append_to
				doc.save()
		else:
			doc = frappe.get_doc({
				"doctype": "Email Account",
				"email_account_name": name or ("Boîte d'essai " + address.split("@", 1)[0]),
				"email_id": address,
				"enable_incoming": 0, "enable_outgoing": 0, "use_imap": 0, "default_outgoing": 0, "default_incoming": 0,
				"append_to": append_to or None,
			})
			doc.insert()
			created = True
		data = gather_site(frappe)
		failures = [t for st, t in evaluate_site(frappe.local.site, conf, data["site_conf"], data["accounts"], data["hooks"], data["signals"]) if st == "FAIL"]
		if failures:
			raise GuardError("le site ne serait plus conforme : " + " ; ".join(failures))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	return {"compte": doc.name, "adresse": address, "append_to": doc.append_to, "cree": created}


# --- C3 : vidage de la file --------------------------------------------------------


def flush_queue(frappe, conf: dict, names=None, since=None, force: bool = False) -> list[dict]:
	"""Envoie à la demande les courriels « Not Sent » (le planificateur reste coupé) : vers Mailpit.

	Sans `force`, la sourdine (`mute_emails`) est respectée et le courriel reste « Not Sent ».
	"""
	write_guard(frappe, conf)
	filters = {"status": "Not Sent"}
	if names:
		filters["name"] = ["in", list(names)]
	if since:
		filters["creation"] = [">=", since]
	out = []
	for name in frappe.get_all("Email Queue", filters=filters, pluck="name", order_by="creation asc"):
		doc = frappe.get_doc("Email Queue", name)
		error = None
		try:
			doc.send(force_send=force)
		except Exception as exc:  # une erreur d'envoi est un résultat, pas un arrêt (erreur et reprise)
			error = "%s: %s" % (type(exc).__name__, exc)
		frappe.db.commit()
		doc.reload()
		out.append({
			"file": name, "statut": doc.status, "essais": doc.get("retry"), "erreur": error or doc.get("error"),
			"expediteur": doc.sender, "destinataires": [r.recipient for r in doc.get("recipients") or []],
			"message_id": (doc.get("message_id") or "").strip("<>"),
			"reference": [doc.reference_doctype, doc.reference_name] if doc.reference_doctype else None,
			"sourdine": bool(frappe.are_emails_muted()) and not force,
		})
	return out


# --- C4 / C5 : dépôt d'un message fabriqué -----------------------------------------


def inject_message(frappe, conf: dict, msg: EmailMessage, account: str, variants=(), essai: str | None = None) -> dict:
	"""Injecte le message dans la réception native d'un Email Account, sans réseau."""
	preflight = write_guard(frappe, conf)
	check_marks(msg, variants)
	from frappe.email.receive import InboundMail

	acc = frappe.get_doc("Email Account", account)
	try:
		communication = InboundMail(msg.as_bytes(), acc).process()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	return {
		"via": "inject", "compte": account, "message_id": (msg["Message-ID"] or "").strip("<>"), "preflight": preflight,
		"communication": communication.name,
		"reference": [communication.reference_doctype, communication.reference_name] if communication.reference_doctype else None,
		"liens": [[l.link_doctype, l.link_name] for l in communication.get("timeline_links") or []],
		"signalements": agreement_signals(frappe, [parseaddr(msg["From"])[1]]),
	}


def imap_append(frappe, conf: dict, msg: EmailMessage, account: str, variants=(), essai: str | None = None) -> dict:
	"""IMAP APPEND dans l'INBOX de la boîte QA (DEV seulement). Le mot de passe n'est jamais affiché."""
	preflight = write_guard(frappe, conf)
	from frappe.utils.password import get_decrypted_password
	import imaplib

	acc = frappe.get_doc("Email Account", account)
	guard_qa_account(acc.as_dict(), conf, frappe.local.site)
	check_marks(msg, variants)
	login = (acc.login_id if acc.get("login_id_is_different") else "") or acc.email_id
	password = get_decrypted_password("Email Account", acc.name, "password", raise_exception=True)
	imap = imaplib.IMAP4_SSL(conf["MAIL_HOST"], int(acc.incoming_port or 993), ssl_context=imap_ssl_context())
	try:
		imap.login(login, password)
		status, data = imap.append("INBOX", "", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
	finally:
		password = None
		try:
			imap.logout()
		except Exception:
			pass
	return {
		"via": "imap", "compte": account, "message_id": (msg["Message-ID"] or "").strip("<>"), "preflight": preflight,
		"append": status, "detail": (data or [b""])[0].decode("utf-8", "replace"),
		"signalements": agreement_signals(frappe, [parseaddr(msg["From"])[1]]),
	}


def deliver(frappe, conf: dict, msg: EmailMessage, via: str, account: str, variants=(), essai: str | None = None) -> dict:
	"""Dépose un message déjà fabriqué : `inject` (réception native) ou `imap` (boîte QA, DEV)."""
	if via == "inject":
		return inject_message(frappe, conf, msg, account, variants, essai)
	if via == "imap":
		return imap_append(frappe, conf, msg, account, variants, essai)
	raise ValueError("--via doit valoir inject ou imap (reçu : %r)" % via)


def check_inbound_target(conf: dict, to_addr: str) -> str:
	"""Un message entrant indépendant ne s'adresse qu'à une boîte simulée ou à la boîte QA."""
	if not (is_party_address(to_addr) or (to_addr or "").strip().lower() == conf["QA_ADDRESS"]):
		raise ValueError("destinataire d'un message entrant : une boîte @%s ou %s (reçu : %r)" % (MARK_DOMAIN, conf["QA_ADDRESS"], to_addr))
	return to_addr.strip()


def pull_account(frappe, conf: dict, account: str) -> dict:
	"""Relève la boîte QA (DEV seulement) et signale les messages synthétiques d'un contact enregistré."""
	write_guard(frappe, conf)
	from frappe.email.doctype.email_account.email_account import pull_from_email_account

	guard_qa_account(frappe.get_doc("Email Account", account).as_dict(), conf, frappe.local.site)
	started = frappe.utils.now_datetime()
	pull_from_email_account(account)
	frappe.db.commit()
	names = frappe.get_all(
		"Communication", filters={"creation": [">=", started], "sender": ["like", "%@" + MARK_DOMAIN]}, pluck="name"
	)
	senders = [frappe.db.get_value("Communication", n, "sender") for n in names]
	return {"compte": account, "communications": names, "signalements": agreement_signals(frappe, senders)}


# --- C7 : fenêtre d'effets ---------------------------------------------------------

_EFFECT_TABLES = (
	("email_queue", "Email Queue", ("status", "sender", "reference_doctype", "reference_name", "communication", "message_id", "error", "retry")),
	("communications", "Communication", ("communication_type", "sent_or_received", "subject", "sender", "recipients", "cc", "bcc", "reference_doctype", "reference_name", "in_reply_to", "message_id", "delivery_status")),
	("fichiers", "File", ("file_name", "attached_to_doctype", "attached_to_name", "file_size", "is_private")),
	("todo", "ToDo", ("reference_type", "reference_name", "allocated_to", "status", "description")),
	("notifications", "Notification Log", ("for_user", "type", "document_type", "document_name", "subject")),
)


def _created_since(frappe, doctype: str, wanted, since) -> list[dict]:
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	fields = ["name", "creation"] + [f for f in wanted if meta.has_field(f)]
	rows = frappe.get_all(doctype, filters={"creation": [">=", since]}, fields=fields, order_by="creation asc")
	return [{k: (str(v) if k == "creation" else v) for k, v in dict(r).items()} for r in rows]


def collect_effects(frappe, since, doctypes=(), conf: dict | None = None, before_ids=None) -> dict:
	"""Ce qui a été créé depuis `since` : file, Communications, fichiers, ToDo, notifications, documents."""
	report = {"depuis": str(since)}
	for key, doctype, wanted in _EFFECT_TABLES:
		report[key] = _created_since(frappe, doctype, wanted, since)
	if report["email_queue"] and frappe.db.exists("DocType", "Email Queue Recipient"):
		names = [r["name"] for r in report["email_queue"]]
		recipients = frappe.get_all("Email Queue Recipient", filters={"parent": ["in", names]}, fields=["parent", "recipient", "status"])
		for row in report["email_queue"]:
			row["destinataires"] = [{"adresse": r.recipient, "statut": r.status} for r in recipients if r.parent == row["name"]]
	documents = []
	for row in report["communications"]:
		if row.get("reference_doctype"):
			documents.append([row["reference_doctype"], row["reference_name"]])
		for link in frappe.get_doc("Communication", row["name"]).get("timeline_links") or []:
			documents.append([link.link_doctype, link.link_name])
	report["documents_references"] = [list(d) for d in dict.fromkeys(tuple(d) for d in documents)]
	report["autres"] = {dt: [r["name"] for r in _created_since(frappe, dt, (), since)] for dt in doctypes}
	if conf is not None and before_ids is not None:
		try:
			report["mailpit_nouveaux"] = [
				summarize_message(mailpit_request(conf, "GET", "/api/v1/message/" + i))
				for i in sorted(mailpit_ids(conf) - set(before_ids))
			]
		except OSError as exc:
			report["mailpit_nouveaux"] = "Mailpit injoignable : %s" % exc
	return report


class EffectsWindow:
	"""Fenêtre avant/après autour d'une action (C7).

	    with mail_qa.EffectsWindow(frappe, conf, doctypes=("Mon DocType",)) as window:
	        ...action...
	    window.report

	Lecture seule. MariaDB lit un instantané : l'action doit tourner dans la même connexion, ou
	avoir été validée avant la fermeture de la fenêtre.
	"""

	def __init__(self, frappe, conf: dict | None = None, doctypes=()):
		self.frappe, self.conf, self.doctypes = frappe, conf, tuple(doctypes)
		self.started = None
		self.before_ids = None
		self.report = None

	def __enter__(self):
		self.start()
		return self

	def start(self):
		self.started = self.frappe.utils.now_datetime()
		if self.conf is not None:
			try:
				self.before_ids = mailpit_ids(self.conf)
			except OSError:
				self.before_ids = None
		return self

	def stop(self) -> dict:
		self.report = collect_effects(self.frappe, self.started, self.doctypes, self.conf, self.before_ids)
		return self.report

	def __exit__(self, exc_type, exc, tb):
		self.stop()
		return False


# --- C8 : droits ------------------------------------------------------------------


def timeline_visibility(frappe, doctype: str, name: str, users, conf: dict | None = None, timeline_fn=None) -> list[dict]:
	"""Pour chaque utilisateur : la chronologie du document, et l'accès aux pièces jointes (File).

	La chronologie est éprouvée telle que l'écran la sert (`get_communications` sous l'utilisateur) :
	un PermissionError vaut refus. Isolation des sociétés = conséquence des permissions du document.
	"""
	if timeline_fn is None:
		from frappe.desk.form.load import get_communications as timeline_fn

	guard_allowed_site(frappe.local.site, conf or load_conf())
	out = []
	original = frappe.session.user
	try:
		for user in users:
			row = {"utilisateur": user, "chronologie": None, "communications": [], "pieces_jointes": {}}
			frappe.set_user(user)
			try:
				comms = timeline_fn(doctype, name, 0, 1000)
				row["chronologie"] = "visible"
				row["communications"] = [c["name"] for c in comms]
			except frappe.PermissionError:
				row["chronologie"] = "refusée"
			except Exception as exc:
				row["chronologie"] = "erreur"
				row["erreur"] = "%s: %s" % (type(exc).__name__, exc)
			finally:
				frappe.set_user(original)
			for comm in row["communications"]:
				files = frappe.get_all(
					"File", filters={"attached_to_doctype": "Communication", "attached_to_name": comm},
					fields=["name", "file_name"],
				)
				row["pieces_jointes"][comm] = [
					{"fichier": f.name, "nom": f.file_name, "lisible": bool(frappe.has_permission("File", "read", doc=f.name, user=user))}
					for f in files
				]
			out.append(row)
	finally:
		frappe.set_user(original)
	return out


def _count(frappe, doctype: str) -> int:
	return frappe.db.count(doctype)


def can_send(frappe, conf: dict, doctype: str, name: str, user: str, essai: str | None = None, make_fn=None) -> dict:
	"""Droit de l'expéditeur : droit « courriel » du document ET tentative réelle d'envoi, annulée.

	La tentative (`communication.email.make` sous l'utilisateur) tourne dans un point de sauvegarde
	suivi d'un retour arrière ; les compteurs de la file et des Communications le prouvent.
	"""
	write_guard(frappe, conf)
	if make_fn is None:
		from frappe.core.doctype.communication.email import make as make_fn

	recipient = party_address("destinataire", essai)
	row = {
		"utilisateur": user, "droit_courriel": bool(frappe.has_permission(doctype, "email", doc=name, user=user)),
		"tentative": None,
	}
	before = (_count(frappe, "Email Queue"), _count(frappe, "Communication"))
	after_commit = getattr(getattr(frappe.db, "after_commit", None), "_functions", None)
	before_commit = getattr(getattr(frappe.db, "before_commit", None), "_functions", None)
	marks = (len(after_commit) if after_commit is not None else 0, len(before_commit) if before_commit is not None else 0)
	point = "mail_qa_can_send"
	original = frappe.session.user
	frappe.db.savepoint(point)
	try:
		frappe.set_user(user)
		try:
			make_fn(
				doctype=doctype, name=name, content=MARK_BODY_FIRST_LINE, subject="%s droit d'envoi" % MARK_SUBJECT,
				sent_or_received="Sent", recipients=[recipient], send_email=True,
			)
			row["tentative"] = "autorisée"
		except frappe.PermissionError:
			row["tentative"] = "refusée"
		except Exception as exc:
			row["tentative"] = "erreur"
			row["erreur"] = "%s: %s" % (type(exc).__name__, exc)
	finally:
		frappe.set_user(original)
		frappe.db.rollback(save_point=point)
		# Le retour arrière d'un point de sauvegarde ne vide pas les rappels « après validation » :
		# on retire ceux que la tentative a ajoutés, sans toucher à ceux de l'appelant.
		if after_commit is not None:
			while len(after_commit) > marks[0]:
				after_commit.pop()
		if before_commit is not None:
			while len(before_commit) > marks[1]:
				before_commit.pop()
	after = (_count(frappe, "Email Queue"), _count(frappe, "Communication"))
	row["residu"] = after != before
	row["compteurs"] = {"avant": list(before), "apres": list(after)}
	return row


# --- C9 : rapport d'essai ----------------------------------------------------------

REPORT_ESTABLISHES = [
	"Le mécanisme natif de courriel de Frappe a traité, sur ce site et à cette date, les messages et les documents listés (envoi, capture, réception, rattachement, effets, droits).",
	"Chaque message fabriqué portait l'en-tête X-Arkonex-Essai et un expéditeur @essai.invalid (contrôlé avant tout dépôt).",
	"Le préflight (verify PASS) était valide au moment de chaque écriture listée.",
]
REPORT_NOT_ESTABLISHED = [
	"Aucun échange externe réel : mode M1, tout reste sur la machine (M2 est différé).",
	"Aucun accord ni approbation d'un tiers : un message synthétique ne vaut jamais acceptation.",
	"Aucune propriété des services d'envoi par HTTPS (Frappe Mail, API tierces, webhooks, SMS, Slack) : tenus par contrôle, non par confinement.",
	"Aucune garantie sur un banc situé hors de cette machine.",
	"Aucun comportement du serveur de courriel réel, sauf via `--via imap` (DEV, boîte QA) quand il est listé.",
]


def report_path(directory: str, essai: str) -> str:
	return os.path.join(directory, "rapport-%s.json" % essai)


def check_entry(name: str, status: str, detail: str = "") -> dict:
	return {"nom": name, "statut": status, "detail": detail}


def report_update(directory: str, essai: str, site: str | None = None, **sections) -> str:
	"""C9 : ajoute des sections au rapport JSON d'un essai (créé au premier appel, fusionné ensuite)."""
	essai = check_essai_id(essai)
	if not essai:
		raise ValueError("un rapport exige un identifiant d'essai (--essai)")
	path = report_path(directory, essai)
	data = None
	try:
		with open(path, encoding="utf-8") as fh:
			data = json.load(fh)
	except (OSError, ValueError):
		data = None
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	if not isinstance(data, dict):
		data = {
			"essai": essai, "site": site, "cree": now, "mode": "M1 : capture locale, aucun échange externe réel",
			"etablit": list(REPORT_ESTABLISHES), "n_etablit_pas": list(REPORT_NOT_ESTABLISHED),
			"limite_https": HTTPS_LIMIT,
		}
	for key, value in sections.items():
		if value in (None, [], {}):
			continue
		if isinstance(value, list):
			data.setdefault(key, []).extend(value)
		else:
			data.setdefault(key, []).append(value)
	signals = [s for entry in data.get("messages", []) for s in entry.get("signalements", [])]
	if signals:
		data["signalements_347"] = signals
	data["mis_a_jour"] = now
	os.makedirs(directory, exist_ok=True)
	tmp = path + ".%d.tmp" % os.getpid()
	with open(tmp, "w", encoding="utf-8") as fh:
		json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
	os.replace(tmp, path)
	return path


# --- Ligne de commande -------------------------------------------------------------


def _out(data) -> None:
	print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _signal_lines(items) -> None:
	for s in items or []:
		links = "; ".join("%s %s" % (l["doctype"], l["nom"]) for c in s["contacts"] for l in c["liens"]) or "aucun lien"
		print("SIGNAL pièce d'accord possible : l'expéditeur %s est le contact %s (%s)" % (
			s["adresse"], ", ".join(c["contact"] for c in s["contacts"]), links))


def _persona_users(args) -> list[str]:
	users = list(args.user or [])
	if args.persona:
		known = _sibling("mail_qa_parties").read_personas()
		for persona in args.persona:
			if persona not in known:
				raise ValueError("persona inconnue : %s" % persona)
			users.append(known[persona]["login_user_id"])
	if not users:
		raise ValueError("au moins un --user ou --persona est exigé")
	return users


def _with_site(args, conf, work):
	frappe = _frappe_connect(args.bench, args.site, conf)
	try:
		return work(frappe)
	finally:
		frappe.db.rollback()
		frappe.destroy()


def _record(args, frappe, **sections) -> None:
	if getattr(args, "report_dir", None):
		report_update(args.report_dir, args.essai, frappe.local.site, **sections)


def _message_entry(kind, msg, result, variants, essai) -> dict:
	return {
		"genre": kind, "message_id": result.get("message_id"), "sujet": str(msg["Subject"] or ""),
		"de": parseaddr(msg["From"])[1], "a": msg["To"], "variantes": list(variants), "essai": essai,
		"pieces_jointes": [p.get_filename() for p in msg.iter_attachments()], "via": result.get("via"),
		"communication": result.get("communication"), "reference": result.get("reference"),
		"liens": result.get("liens"), "signalements": result.get("signalements") or [],
	}


def cmd_reply(args) -> int:
	conf = load_conf(args.conf)
	essai = check_essai_id(args.essai)
	variants = check_variants(args.variant)
	original = fetch_original(conf, args.mailpit_id)
	msg = build_reply(original, args.body or "", args.from_addr, args.attach or [], variants, essai, args.heavy_bytes)
	if args.dry_run:
		print(msg.as_string())
		return 0
	via = "inject" if args.cmd == "inject" else args.via
	if via is None:
		raise ValueError("reply exige --via inject ou --via imap")
	account = args.account or ("QA Arkonex" if via == "imap" else None)
	if not account:
		raise ValueError("--account est exigé avec --via inject")

	def work(frappe):
		result = deliver(frappe, conf, msg, via, account, variants, essai)
		_record(args, frappe, messages=[_message_entry("réponse", msg, result, variants, essai)],
			controles=[check_entry("marques synthétiques", "PASS", "check_marks avant dépôt"),
				check_entry("préflight", "PASS", "verify PASS (%s)" % result.get("preflight"))])
		_out(result)
		_signal_lines(result.get("signalements"))
		return 0 if result.get("append", "OK") == "OK" else 1

	return _with_site(args, conf, work)


def cmd_inbound(args) -> int:
	conf = load_conf(args.conf)
	essai = check_essai_id(args.essai)
	variants = check_variants(args.variant)
	check_inbound_target(conf, args.to_addr)
	msg = build_inbound(
		to_addr=args.to_addr, from_addr=args.from_addr, subject=args.subject or "", body=args.body or "",
		attachments=args.attach or [], variants=variants, essai=essai, heavy_bytes=args.heavy_bytes,
	)
	if args.dry_run:
		print(msg.as_string())
		return 0
	account = args.account or ("QA Arkonex" if args.via == "imap" else None)
	if not account:
		raise ValueError("--account est exigé avec --via inject")

	def work(frappe):
		result = deliver(frappe, conf, msg, args.via, account, variants, essai)
		_record(args, frappe, messages=[_message_entry("indépendant", msg, result, variants, essai)],
			controles=[check_entry("marques synthétiques", "PASS", "check_marks avant dépôt"),
				check_entry("préflight", "PASS", "verify PASS (%s)" % result.get("preflight"))])
		_out(result)
		_signal_lines(result.get("signalements"))
		return 0 if result.get("append", "OK") == "OK" else 1

	return _with_site(args, conf, work)


def cmd_pull(args) -> int:
	conf = load_conf(args.conf)

	def work(frappe):
		result = pull_account(frappe, conf, args.account)
		_out(result)
		_signal_lines(result["signalements"])
		return 0

	return _with_site(args, conf, work)


def cmd_inbox(args) -> int:
	conf = load_conf(args.conf)
	essai = check_essai_id(args.essai)
	address = args.address or party_address(args.party, essai)

	def work(frappe):
		result = ensure_inbox(frappe, conf, address, args.append_to, args.name)
		_record(args, frappe, boites=[result])
		_out(result)
		return 0

	return _with_site(args, conf, work)


def cmd_flush(args) -> int:
	conf = load_conf(args.conf)

	def work(frappe):
		result = flush_queue(frappe, conf, args.name, args.since, args.force)
		_record(args, frappe, envois=result)
		_out(result)
		return 1 if any(r["statut"] != "Sent" for r in result) else 0

	return _with_site(args, conf, work)


def cmd_captured(args) -> int:
	conf = load_conf(args.conf)
	if args.site:
		items = _with_site(args, conf, lambda frappe: captured(
			conf, to=args.to, subject=args.subject, essai=args.essai, since=args.since, limit=args.limit, frappe=frappe))
	else:
		items = captured(conf, to=args.to, subject=args.subject, essai=args.essai, since=args.since, limit=args.limit)
	if getattr(args, "report_dir", None):
		report_update(args.report_dir, args.essai, args.site, capture=items)
	_out(items)
	return 0


def cmd_window(args) -> int:
	conf = load_conf(args.conf)
	action = [a for a in args.action if a != "--"][:]
	if not action or action[0] not in ("reply", "inject", "inbound", "pull", "flush", "inbox"):
		raise ValueError("window exige, après --, une commande mail-qa (reply, inject, inbound, pull, flush, inbox)")

	def start(frappe):
		window = EffectsWindow(frappe, conf, args.doctype).start()
		return window.started, window.before_ids

	started, before_ids = _with_site(args, conf, start)
	proc = subprocess.run(
		[sys.executable, "-I", os.path.abspath(__file__), *action],
		capture_output=True, text=True, cwd=os.path.join(args.bench, "sites"), timeout=900,
	)

	def stop(frappe):
		return collect_effects(frappe, started, args.doctype, conf, before_ids)

	effects = _with_site(args, conf, stop)
	result = {"action": {"commande": action, "code": proc.returncode, "sortie": proc.stdout[-4000:], "erreur": proc.stderr[-2000:]}, "effets": effects}
	if args.out:
		with open(args.out, "w", encoding="utf-8") as fh:
			json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
	if args.report_dir:
		report_update(args.report_dir, args.essai, args.site, effets=effects)
	_out(result)
	return proc.returncode


def cmd_visibility(args) -> int:
	conf = load_conf(args.conf)
	users = _persona_users(args)

	def work(frappe):
		rows = timeline_visibility(frappe, args.doctype, args.docname, users)
		if args.report_dir:
			report_update(args.report_dir, args.essai, frappe.local.site, droits=[{"genre": "lecteur", "document": [args.doctype, args.docname], "resultat": rows}])
		_out(rows)
		return 0

	return _with_site(args, conf, work)


def cmd_can_send(args) -> int:
	conf = load_conf(args.conf)
	users = _persona_users(args)

	def work(frappe):
		rows = [can_send(frappe, conf, args.doctype, args.docname, u, args.essai) for u in users]
		if args.report_dir:
			report_update(args.report_dir, args.essai, frappe.local.site, droits=[{"genre": "expéditeur", "document": [args.doctype, args.docname], "resultat": rows}])
		_out(rows)
		return 1 if any(r["residu"] for r in rows) else 0

	return _with_site(args, conf, work)


def cmd_purge(args) -> int:
	conf = load_conf(args.conf)
	_out(purge_essai(conf, args.essai, args.dry_run))
	return 0


def cmd_address(args) -> int:
	print(party_address(args.name, args.essai))
	return 0


def cmd_parties(args) -> int:
	conf = load_conf(args.conf)
	return _sibling("mail_qa_parties").run_cli(args, conf, lambda: _frappe_connect(args.bench, args.site, conf))


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--conf", default=None)
	sub = parser.add_subparsers(dest="cmd", required=True)
	bench = "/home/frappe/frappe-bench"

	def site_args(p, required=True):
		p.add_argument("--bench", default=bench)
		p.add_argument("--site", required=required)

	def report_args(p):
		p.add_argument("--essai", default=None, help="identifiant d'essai (C1) ; exigé avec --report-dir")
		p.add_argument("--report-dir", default=None, help="dossier du rapport JSON de l'essai (C9)")

	def message_args(p):
		p.add_argument("--body", default="")
		p.add_argument("--from", dest="from_addr", default=None)
		p.add_argument("--attach", action="append")
		p.add_argument("--variant", action="append", choices=VARIANTS)
		p.add_argument("--heavy-bytes", type=int, default=HEAVY_DEFAULT_BYTES)
		p.add_argument("--account", default=None)
		p.add_argument("--dry-run", action="store_true")
		report_args(p)

	sub.add_parser("verify")
	p = sub.add_parser("site-check")
	p.add_argument("--bench", required=True)
	p.add_argument("--site", required=True)
	for name in ("reply", "inject"):
		p = sub.add_parser(name)
		site_args(p)
		p.add_argument("--mailpit-id", required=True)
		if name == "reply":
			p.add_argument("--via", choices=("inject", "imap"), default=None)
		message_args(p)
	p = sub.add_parser("inbound")
	site_args(p)
	p.add_argument("--via", choices=("inject", "imap"), required=True)
	p.add_argument("--to", dest="to_addr", required=True)
	p.add_argument("--subject", default="")
	message_args(p)
	p = sub.add_parser("pull")
	site_args(p)
	p.add_argument("--account", default="QA Arkonex")
	p = sub.add_parser("inbox")
	isub = p.add_subparsers(dest="inbox_cmd", required=True)
	p = isub.add_parser("ensure")
	site_args(p)
	group = p.add_mutually_exclusive_group(required=True)
	group.add_argument("--address")
	group.add_argument("--party")
	p.add_argument("--append-to", default=None)
	p.add_argument("--name", default=None)
	report_args(p)
	p = sub.add_parser("flush")
	site_args(p)
	p.add_argument("--name", action="append")
	p.add_argument("--since", default=None)
	p.add_argument("--force", action="store_true", help="passe outre la sourdine (mute_emails)")
	report_args(p)
	p = sub.add_parser("captured")
	site_args(p, required=False)
	p.add_argument("--to")
	p.add_argument("--subject")
	p.add_argument("--since")
	p.add_argument("--limit", type=int, default=50)
	report_args(p)
	p = sub.add_parser("window")
	site_args(p)
	p.add_argument("--doctype", action="append", default=[])
	p.add_argument("--out")
	report_args(p)
	p.add_argument("action", nargs=argparse.REMAINDER)
	for name in ("visibility", "can-send"):
		p = sub.add_parser(name)
		site_args(p)
		p.add_argument("--doctype", required=True)
		p.add_argument("--docname", required=True)
		p.add_argument("--user", action="append")
		p.add_argument("--persona", action="append")
		report_args(p)
	p = sub.add_parser("purge")
	p.add_argument("--essai", required=True)
	p.add_argument("--dry-run", action="store_true")
	p = sub.add_parser("address")
	p.add_argument("name")
	p.add_argument("--essai", default=None)
	p = sub.add_parser("parties")
	psub = p.add_subparsers(dest="parties_cmd", required=True)
	for name in ("show", "validate", "ensure"):
		q = psub.add_parser(name)
		q.add_argument("--company", default=None)
		q.add_argument("--catalog", default=None)
		if name == "ensure":
			site_args(q)
		else:
			q.add_argument("--bench", default=bench)
			q.add_argument("--site", default=None)
	return parser


HANDLERS = {
	"reply": cmd_reply, "inject": cmd_reply, "inbound": cmd_inbound, "pull": cmd_pull, "inbox": cmd_inbox,
	"flush": cmd_flush, "captured": cmd_captured, "window": cmd_window, "visibility": cmd_visibility,
	"can-send": cmd_can_send, "purge": cmd_purge, "address": cmd_address, "parties": cmd_parties,
}


def main(argv=None) -> int:
	args = build_parser().parse_args(argv)
	if args.cmd == "verify":
		return cmd_verify(args)
	if args.cmd == "site-check":
		print(json.dumps(site_check(args.bench, args.site), default=str))
		return 0
	if getattr(args, "report_dir", None) and not args.essai:
		print("REFUS : --report-dir exige --essai", file=sys.stderr)
		return 1
	try:
		return HANDLERS[args.cmd](args)
	except (MarkError, PreflightError, GuardError, ValueError) as exc:
		print("REFUS : %s" % exc, file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
