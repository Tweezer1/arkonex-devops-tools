#!/usr/bin/env bash
# install.sh — installation du dispositif d'essai de courriel, mode M1 (OPEN-202, Issue #351).
#
# Contrat : docs/open_202/devops_mail_qa_m1_contrat.md (v0.2.0), étapes 2 et 3.
#
# Usage (par le propriétaire, en root) :
#   sudo bash install.sh firewall [--apply]     P1 : règle nftables persistante
#   sudo bash install.sh mailpit  [--apply]     P2 : Mailpit sans relais, service activé
#   sudo bash install.sh mailpit-stop [--apply] retour arrière normal de P2 (P1 reste)
#
# Sans --apply : n'écrit rien, affiche ce qui serait fait (mode plan), code 0.
# Le retrait de P1 n'est PAS proposé ici : il exige une décision humaine distincte
# (contrat §10) ; la commande manuelle figure dans le README.
#
# Propagation explicite des codes de retour, jamais `set -e` (CLAUDE.md, § Construction
# des assets).

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=mail-qa.conf
. "$DIR/mail-qa.conf"

NFT_SRC="$DIR/nftables/arkonex-mailqa.nft"
NFT_DIR="${MAILQA_NFT_DIR:-/etc/nftables.d}"
NFT_CONF="${MAILQA_NFT_CONF:-/etc/nftables.conf}"
NFT_INCLUDE='include "/etc/nftables.d/*.nft"'
UNIT_SRC="$DIR/systemd/mailpit-qa.service"
UNIT_DEST="/etc/systemd/system/mailpit-qa.service"
BIN_DIR="/opt/arkonex-mail-qa/bin"
DATA_DIR="/var/lib/arkonex-mail-qa"

CMD="${1:-}"
APPLY=0
[ "${2:-}" = "--apply" ] && APPLY=1

log() { echo "mail-qa install: $*"; }
die() { echo "mail-qa install: STOP — $*" >&2; exit 1; }

run() {
	# Exécute en mode --apply, affiche seulement en mode plan.
	if [ "$APPLY" -eq 1 ]; then
		"$@"
		local rc=$?
		[ "$rc" -eq 0 ] || die "échec (rc=$rc) : $*"
	else
		echo "  [plan] $*"
	fi
}

need_root() {
	if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
		die "--apply exige root (sudo)"
	fi
}

firewall() {
	need_root
	log "P1 — règle $NFT_SRC → $NFT_DIR/arkonex-mailqa.nft, incluse par $NFT_CONF"
	if [ "$APPLY" -eq 1 ]; then
		nft list tables || die "nft list tables impossible"
	fi
	run install -d -m 0755 "$NFT_DIR"
	run install -m 0644 "$NFT_SRC" "$NFT_DIR/arkonex-mailqa.nft"
	if grep -qxF "$NFT_INCLUDE" "$NFT_CONF"; then
		log "include déjà présent dans $NFT_CONF — aucune modification"
	else
		run cp -p "$NFT_CONF" "$NFT_CONF.bak.$(date -u +%Y%m%dT%H%M%SZ)"
		if [ "$APPLY" -eq 1 ]; then
			printf '\n# OPEN-202 (Issue #351) — protections locales\n%s\n' "$NFT_INCLUDE" >> "$NFT_CONF" || die "écriture de $NFT_CONF"
		else
			echo "  [plan] ajouter en fin de $NFT_CONF : $NFT_INCLUDE"
		fi
	fi
	run nft -c -f "$NFT_CONF"
	# Chargement sans « flush ruleset » : on remplace seulement notre table.
	if [ "$APPLY" -eq 1 ]; then
		if nft list table inet arkonex_mailqa >/dev/null 2>&1; then
			nft delete table inet arkonex_mailqa || die "retrait de l'ancienne table"
		fi
	else
		echo "  [plan] retirer la table inet arkonex_mailqa si elle existe déjà"
	fi
	run nft -f "$NFT_DIR/arkonex-mailqa.nft"
	run systemctl enable nftables.service
	if [ "$APPLY" -eq 1 ]; then
		nft list table inet arkonex_mailqa || die "table absente après chargement"
		log "P1 appliqué. Contrôle : python3 $DIR/mail_qa.py verify"
	fi
}

mailpit() {
	need_root
	log "P2 — Mailpit $MAILPIT_VERSION (sha256 $MAILPIT_SHA256)"
	if ! id mailqa >/dev/null 2>&1; then
		run useradd --system --no-create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin mailqa
	fi
	run install -d -m 0755 "$BIN_DIR"
	run install -d -m 0750 -o mailqa -g mailqa "$DATA_DIR"
	if [ "$APPLY" -eq 1 ]; then
		local tmp
		tmp="$(mktemp -d)" || die "mktemp"
		curl -fsSL -o "$tmp/mailpit.tgz" "$MAILPIT_URL" || die "téléchargement"
		echo "$MAILPIT_SHA256  $tmp/mailpit.tgz" | sha256sum -c - || die "empreinte sha256 différente"
		tar -xzf "$tmp/mailpit.tgz" -C "$tmp" mailpit || die "extraction"
		install -m 0755 -o root -g root "$tmp/mailpit" "$BIN_DIR/mailpit" || die "installation du binaire"
		rm -rf "$tmp"
		"$BIN_DIR/mailpit" version | head -1
	else
		echo "  [plan] télécharger $MAILPIT_URL, vérifier sha256, installer $BIN_DIR/mailpit"
	fi
	run install -m 0644 "$UNIT_SRC" "$UNIT_DEST"
	run systemctl daemon-reload
	run systemctl enable mailpit-qa.service
	run systemctl restart mailpit-qa.service
	[ "$APPLY" -eq 1 ] && log "P2 appliqué. Contrôle : python3 $DIR/mail_qa.py verify"
	return 0
}

mailpit_stop() {
	need_root
	log "retour arrière normal de P2 : Mailpit arrêté et désactivé ; P1 conservé"
	run systemctl disable --now mailpit-qa.service
}

case "$CMD" in
	firewall) firewall ;;
	mailpit) mailpit ;;
	mailpit-stop) mailpit_stop ;;
	*)
		echo "Usage : $0 {firewall|mailpit|mailpit-stop} [--apply]" >&2
		exit 2
		;;
esac
exit 0
