#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Contrôle des règles du mode automatique (OPEN-194, Issue #307). Lecture seule.

1. Règles d'écriture de automode.json : chaque liste commence par "$defaults" ; aucun
   hôte arkonex.ca autre que deverp dans une autorisation ou un blocage ; un hôte de
   production n'apparaît dans le contexte que déclaré interdit ; interdit absolu de la
   production présent ; refus fixes WebFetch présents.
2. Configuration effective (`claude auto-mode config`) : exactement la référence, "$defaults"
   remplacé par les règles par défaut (`claude auto-mode defaults`), sans perte ni ajout.
3. Réglages utilisateur (~/.claude/settings.json) : refus fixes WebFetch présents.
4. Barrières déployées : le garde-fou Bash installé refuse openssl vers la production et
   demande validation pour un envoi sur version-16 ; chaque serveur navigateur de
   .mcp.json bloque la production.

Usage :
  automode_verifier.py EFFECTIF.json DEFAUTS.json [--reference automode.json]
      [--reglages ~/.claude/settings.json] [--mcp-json .mcp.json] [--gardes .claude/hooks]
Sortie : PASS, ou FAIL suivi des écarts ; code de retour 0 ou 1.
"""

import argparse
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = "/home/frappe/frappe-bench"
SECTIONS = ("environment", "allow", "soft_deny", "hard_deny")
DEV_HOST = "deverp.arkonex.ca"
PRODUCTION = "desk.arkonex.ca"
REFUS_FIXES = ("WebFetch(domain:arkonex.ca)", "WebFetch(domain:*.arkonex.ca)")
# Hôte arkonex.ca, sauf domaine d'une adresse e-mail (précédé de @).
HOTE = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)*arkonex\.ca)(?![a-z0-9-])", re.I)


def hotes_hors_dev(texte):
    return sorted({h.lower() for h in HOTE.findall(texte or "")} - {DEV_HOST})


def verifier_reference(ref):
    """Règles d'écriture de automode.json."""
    ecarts = []
    am = ref.get("autoMode") or {}
    for section in SECTIONS:
        liste = am.get(section)
        if not liste:
            ecarts.append("{} : section absente".format(section))
        elif liste[0] != "$defaults":
            ecarts.append("{} : ne commence pas par \"$defaults\" (remplacerait les règles "
                          "par défaut)".format(section))
    for section in ("allow", "soft_deny"):
        for regle in am.get(section) or []:
            hotes = hotes_hors_dev(regle)
            if hotes:
                ecarts.append("{} : cite un hôte hors DEV ({}) : {}…".format(
                    section, ", ".join(hotes), regle[:50]))
    for regle in am.get("environment") or []:
        bas = regle.lower()
        if hotes_hors_dev(regle) and not ("forbidden" in bas and "never a trusted" in bas):
            ecarts.append("environment : hôte hors DEV cité sans être déclaré interdit : {}…"
                          .format(regle[:50]))
    if not any(PRODUCTION in regle.lower() for regle in am.get("hard_deny") or []):
        ecarts.append("hard_deny : l'interdit absolu de la production est absent")
    refus = (ref.get("permissions") or {}).get("deny") or []
    for entree in REFUS_FIXES:
        if entree not in refus:
            ecarts.append("permissions.deny : {} absent".format(entree))
    return ecarts


def verifier_effectif(ref, effectif, defauts):
    """La configuration effective est exactement la référence développée."""
    ecarts = []
    am = ref.get("autoMode") or {}
    for section in SECTIONS:
        if not defauts.get(section):
            ecarts.append("{} : règles par défaut absentes ou vides (sortie de "
                          "`auto-mode defaults` invalide)".format(section))
            continue
        attendu = []
        for regle in am.get(section) or []:
            attendu.extend(defauts[section] if regle == "$defaults" else [regle])
        actives = effectif.get(section) or []
        if actives == attendu:
            continue
        for regle in attendu:
            if regle not in actives:
                ecarts.append("{} : règle absente de la configuration effective : {}…"
                              .format(section, regle[:60]))
        for regle in actives:
            if regle not in attendu:
                ecarts.append("{} : règle en plus de la référence : {}…".format(section, regle[:60]))
        if sorted(actives) == sorted(attendu):
            ecarts.append("{} : ordre différent de la référence".format(section))
    return ecarts


def verifier_reglages(reglages):
    refus = ((reglages or {}).get("permissions") or {}).get("deny") or []
    return ["réglages utilisateur : {} absent de permissions.deny".format(e)
            for e in REFUS_FIXES if e not in refus]


def verifier_mcp(mcp):
    ecarts = []
    serveurs = (mcp or {}).get("mcpServers") or {}
    navigateurs = {n: s for n, s in serveurs.items()
                   if any("@playwright/mcp" in str(a) for a in s.get("args") or [])}
    if not navigateurs:
        ecarts.append(".mcp.json : aucun serveur navigateur trouvé")
    for nom, s in sorted(navigateurs.items()):
        bloques = [a for a in s.get("args") or [] if str(a).startswith("--blocked-origins=")]
        if not any(PRODUCTION in a for a in bloques):
            ecarts.append(".mcp.json : {} ne bloque pas la production".format(nom))
    return ecarts


def verifier_gardes(dossier):
    chemin = os.path.join(dossier, "guard_bash.py")
    if not os.path.isfile(chemin):
        return ["garde-fou déployé introuvable : {}".format(chemin)]
    spec = importlib.util.spec_from_file_location("guard_bash_deploye", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ecarts = []
    for commande, attendu in (("openssl s_client -connect {}:443".format(PRODUCTION), "deny"),
                              ("git push origin version-16", "ask")):
        decision = (module.evaluate(commande, "/tmp") or (None,))[0]
        if decision != attendu:
            ecarts.append("garde-fou déployé : « {} » → {} (attendu : {})".format(
                commande, decision, attendu))
    return ecarts


def _charger(chemin):
    with open(chemin, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("effectif")
    p.add_argument("defauts")
    p.add_argument("--reference", default=os.path.join(HERE, "automode.json"))
    p.add_argument("--reglages", default=os.path.expanduser("~/.claude/settings.json"))
    p.add_argument("--mcp-json", default=os.path.join(BENCH, ".mcp.json"))
    p.add_argument("--gardes", default=os.path.join(BENCH, ".claude", "hooks"))
    a = p.parse_args(argv)
    ref = _charger(a.reference)
    ecarts = (verifier_reference(ref)
              + verifier_effectif(ref, _charger(a.effectif), _charger(a.defauts))
              + verifier_reglages(_charger(a.reglages))
              + verifier_mcp(_charger(a.mcp_json))
              + verifier_gardes(a.gardes))
    if ecarts:
        print("FAIL")
        for e in ecarts:
            print("  - " + e)
        return 1
    n = sum(1 for s in SECTIONS for r in ref["autoMode"][s] if r != "$defaults")
    print("PASS : {} règles Arkonex actives, exactement la référence ; règles par défaut "
          "intactes ; refus fixes WebFetch ; garde-fou et navigateurs déployés".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
