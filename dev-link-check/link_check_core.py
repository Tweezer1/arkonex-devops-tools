#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Logique pure du contrôle des liens cassés (dev-link-check, OPEN-164, Issue #173).

Aucun import de `frappe` ici : ce module est testable sans bench, avec un simple
`unittest`. La partie qui interroge réellement un site Frappe vit dans
`link_check.py`, exécuté dans `bench console`.

Vocabulaire :
- une « entrée » est un dict décrivant un lien cassé trouvé :
  {"field": "<DocType>.<fieldname>", "target": "<DocType cible>",
   "doctype": "<doctype du document de tête>", "name": "<nom du document de tête>",
   "value": "<valeur trouvée dans le champ>"} ;
- une « référence » (`baseline.json`) est une liste connue et acceptée de tels liens,
  avec une raison par champ — ce contrôle est un contrôle de DRIFT (rien de nouveau
  par rapport à la référence), pas une exigence de zéro lien cassé.
"""

import json


def normalize(value):
    """Normalise une valeur de champ Link pour la comparaison.

    MariaDB compare les chaînes sans distinction de casse et ignore les espaces de
    fin ; normaliser ainsi évite de signaler comme « cassé » un lien qui ne l'est
    pas réellement pour la base.
    """
    return str(value).rstrip().casefold()


def entry_key(entry):
    """Construit la clé stable d'une entrée (identité du lien, valeur normalisée)."""
    return "|".join([
        entry["field"],
        entry["target"],
        entry["doctype"],
        entry["name"],
        normalize(entry["value"]),
    ])


def dedupe(entries):
    """Supprime les doublons de clé, en conservant l'ordre d'apparition."""
    seen = {}
    for entry in entries:
        key = entry_key(entry)
        if key not in seen:
            seen[key] = entry
    return list(seen.values())


def baseline_keys(baseline):
    """Reconstruit l'ensemble des clés d'entrée décrites par une référence."""
    keys = set()
    if not baseline:
        return keys
    fields = baseline.get("fields") or {}
    for field_key, data in fields.items():
        field, target = field_key.split("->", 1)
        for link in data.get("links") or []:
            doctype, name, value = link
            keys.add(entry_key({
                "field": field,
                "target": target,
                "doctype": doctype,
                "name": name,
                "value": value,
            }))
    return keys


def baseline_from_entries(entries, site, generated_at, lot, reasons):
    """Construit une référence fraîche à partir des entrées trouvées.

    `reasons` associe une clé `"<field>-><target>"` à un texte de justification ;
    une clé absente reçoit la raison par défaut `"À JUSTIFIER"` (elle doit être
    complétée par un humain avant d'être acceptée dans un lot).
    """
    reasons = reasons or {}
    groups = {}
    for entry in dedupe(entries):
        field_key = "%s->%s" % (entry["field"], entry["target"])
        groups.setdefault(field_key, set())
        groups[field_key].add((entry["doctype"], entry["name"], entry["value"]))

    fields = {}
    for field_key in sorted(groups):
        links = sorted(groups[field_key])
        fields[field_key] = {
            "reason": reasons.get(field_key, "À JUSTIFIER"),
            "links": [list(link) for link in links],
        }

    return {
        "schema": "dev-link-check/1",
        "site": site,
        "generated_at": generated_at,
        "lot": lot,
        "fields": fields,
    }


def compare(entries, baseline, fields_total, fields_checked, ignored):
    """Compare les entrées trouvées à la référence.

    Rapport au format RB-81 §11.3 : `status` PASS/FAIL, `summary`, `anomalies`
    (entrées hors référence), `warnings` (champs ignorés), `resolved` (entrées de
    référence qui ne sont plus trouvées — une amélioration, sans effet sur le
    statut), `counters`. FAIL si et seulement si au moins une entrée nouvelle
    (hors référence) est trouvée.
    """
    entries = dedupe(entries)
    known_keys = baseline_keys(baseline)

    found_keys = set()
    anomalies = []
    for entry in entries:
        key = entry_key(entry)
        found_keys.add(key)
        if key not in known_keys:
            anomalies.append(entry)

    resolved = sorted(known_keys - found_keys)

    broken_total = len(entries)
    broken_new = len(anomalies)
    broken_known = broken_total - broken_new
    status = "FAIL" if broken_new > 0 else "PASS"

    summary = (
        "%d lien(s) cassé(s) trouvé(s) (%d connu(s) en référence, %d nouveau(x)) -> %s"
        % (broken_total, broken_known, broken_new, status)
    )
    if resolved:
        summary += " ; %d entrée(s) de référence résolue(s)" % len(resolved)

    return {
        "status": status,
        "summary": summary,
        "anomalies": anomalies,
        "warnings": list(ignored),
        "resolved": resolved,
        "counters": {
            "fields_total": fields_total,
            "fields_checked": fields_checked,
            "fields_ignored": len(ignored),
            "broken_total": broken_total,
            "broken_known": broken_known,
            "broken_new": broken_new,
            "baseline_resolved": len(resolved),
        },
    }


def load_json(path):
    """Charge un fichier JSON UTF-8."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(obj, path):
    """Écrit un fichier JSON UTF-8, lisible et stable (tri des clés)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
