#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Contrôle des règles du mode automatique (OPEN-194, Issue #307). Lecture seule.

1. Règles d'écriture de automode.json : chaque liste commence par "$defaults" ; la
   production (desk.arkonex.ca) n'apparaît que comme destination interdite ; refus fixe
   WebFetch vers arkonex.ca présent.
2. Configuration effective (sortie de `claude auto-mode config`) : chaque règle de
   automode.json est active et aucune règle par défaut (`claude auto-mode defaults`)
   n'est perdue.
3. Réglages utilisateur (~/.claude/settings.json) : les refus fixes WebFetch sont présents.

Usage :
  automode_verifier.py EFFECTIF.json DEFAUTS.json [--reference automode.json]
                       [--reglages ~/.claude/settings.json]
Sortie : PASS, ou FAIL suivi des écarts ; code de retour 0 ou 1.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SECTIONS = ("environment", "allow", "soft_deny", "hard_deny")
PRODUCTION = "desk.arkonex.ca"
REFUS_FIXES = ("WebFetch(domain:arkonex.ca)", "WebFetch(domain:*.arkonex.ca)")


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
            if PRODUCTION in regle:
                ecarts.append("{} : cite la production : {}…".format(section, regle[:60]))
    for regle in am.get("environment") or []:
        if PRODUCTION in regle and "forbidden" not in regle:
            ecarts.append("environment : production citée sans être déclarée interdite")
    if not any(PRODUCTION in regle for regle in am.get("hard_deny") or []):
        ecarts.append("hard_deny : l'interdit absolu de la production est absent")
    refus = (ref.get("permissions") or {}).get("deny") or []
    for entree in REFUS_FIXES:
        if entree not in refus:
            ecarts.append("permissions.deny : {} absent".format(entree))
    return ecarts


def verifier_effectif(ref, effectif, defauts):
    """Chaque règle de référence est active ; aucune règle par défaut n'est perdue."""
    ecarts = []
    am = ref.get("autoMode") or {}
    for section in SECTIONS:
        actives = effectif.get(section) or []
        for regle in am.get(section) or []:
            if regle != "$defaults" and regle not in actives:
                ecarts.append("{} : règle inactive : {}…".format(section, regle[:60]))
        for regle in defauts.get(section) or []:
            if regle not in actives:
                ecarts.append("{} : règle par défaut perdue : {}…".format(section, regle[:60]))
    return ecarts


def verifier_reglages(reglages):
    refus = ((reglages or {}).get("permissions") or {}).get("deny") or []
    return ["réglages utilisateur : {} absent de permissions.deny".format(e)
            for e in REFUS_FIXES if e not in refus]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("effectif")
    p.add_argument("defauts")
    p.add_argument("--reference", default=os.path.join(HERE, "automode.json"))
    p.add_argument("--reglages", default=os.path.expanduser("~/.claude/settings.json"))
    a = p.parse_args(argv)
    with open(a.reference, encoding="utf-8") as fh:
        ref = json.load(fh)
    with open(a.effectif, encoding="utf-8") as fh:
        effectif = json.load(fh)
    with open(a.defauts, encoding="utf-8") as fh:
        defauts = json.load(fh)
    with open(a.reglages, encoding="utf-8") as fh:
        reglages = json.load(fh)
    ecarts = verifier_reference(ref) + verifier_effectif(ref, effectif, defauts) \
        + verifier_reglages(reglages)
    if ecarts:
        print("FAIL")
        for e in ecarts:
            print("  - " + e)
        return 1
    print("PASS : {} règles Arkonex actives, règles par défaut intactes, refus fixes WebFetch "
          "présents".format(sum(1 for s in SECTIONS for r in ref["autoMode"][s] if r != "$defaults")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
