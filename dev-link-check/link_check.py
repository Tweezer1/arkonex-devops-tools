#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Contrôle en lecture seule des liens cassés sur un site Frappe (OPEN-164, #173).

Ce fichier n'est PAS importé : il est lu et exécuté tel quel dans `bench console`
par `run.sh`, via `exec(open(p).read(), globals())`. Conséquence directe : `__file__`
n'existe pas dans ce contexte, et aucune variable Python ne traverse l'appel — tout
passe par des variables d'environnement, lues une seule fois au début de `main()`.

Variables d'environnement :
  LINK_CHECK_DIR (obligatoire)        : dossier de l'outil (ajouté à sys.path pour
                                        importer link_check_core).
  LINK_CHECK_BASELINE (optionnel)     : chemin de la référence à comparer (défaut
                                        "<LINK_CHECK_DIR>/baseline.json"). Introuvable
                                        (explicite ou par défaut) -> LINK_CHECK_STATUS=
                                        ERROR, sans parcourir le site (OPEN-168). Un
                                        chemin relatif se lit depuis le dossier de la
                                        console (sites/) : run.sh le rend absolu avant.
  LINK_CHECK_REPORT (optionnel)       : chemin où écrire le rapport JSON.
  LINK_CHECK_WRITE_BASELINE (option.) : chemin où écrire une référence fraîche,
                                        construite depuis les entrées trouvées.
  LINK_CHECK_LOT (optionnel)          : identifiant de lot inscrit dans la référence
                                        fraîche (LINK_CHECK_WRITE_BASELINE), sinon "".

Lecture seule stricte : ce script n'exécute jamais insert/save/delete/set_value/
db_set/sql/commit. Il ne fait que lire (frappe.get_all, frappe.get_meta,
frappe.db.exists, frappe.db.get_single_value) et termine par frappe.db.rollback()
(ceinture et bretelles ; bench console annule aussi ses écritures à la fermeture).

Tout est encapsulé dans main(), appelée en fin de fichier, pour ne pas polluer
l'espace de noms global de la console (où ce fichier est exécuté par exec(...,
globals())) avec des noms de variables banals (fields, entries, rows...).
"""

import json
import os
import sys


def main():
    link_check_dir = os.environ.get("LINK_CHECK_DIR")
    if not link_check_dir:
        print("LINK_CHECK_STATUS=ERROR")
        print("LINK_CHECK_ERROR=RuntimeError: variable LINK_CHECK_DIR manquante")
        return

    if link_check_dir not in sys.path:
        sys.path.insert(0, link_check_dir)
    import link_check_core as core

    explicit_baseline = os.environ.get("LINK_CHECK_BASELINE")
    baseline_path = os.path.abspath(
        explicit_baseline or os.path.join(link_check_dir, "baseline.json")
    )
    report_path = os.environ.get("LINK_CHECK_REPORT")
    write_baseline_path = os.environ.get("LINK_CHECK_WRITE_BASELINE")
    lot = os.environ.get("LINK_CHECK_LOT", "")

    # Référence introuvable = erreur, avant tout parcours du site. La lire comme vide
    # rendrait « nouveaux » tous les liens connus (faux FAIL du 28/09, OPEN-168).
    if not os.path.isfile(baseline_path):
        print("LINK_CHECK_STATUS=ERROR")
        print("LINK_CHECK_ERROR=référence introuvable : %s (%s)" % (
            baseline_path,
            "LINK_CHECK_BASELINE" if explicit_baseline else "référence par défaut",
        ))
        return
    baseline = core.load_json(baseline_path)
    print("LINK_CHECK_BASELINE_LOADED=%s (%d entrée(s))" % (
        baseline_path, len(core.baseline_keys(baseline)),
    ))

    # Cache des meta par DocType : chaque champ Link visité relit potentiellement la
    # même meta de parent ou de cible, autant ne la charger qu'une fois.
    meta_cache = {}

    def get_meta_cached(doctype):
        if doctype not in meta_cache:
            try:
                meta_cache[doctype] = frappe.get_meta(doctype)
            except Exception:
                meta_cache[doctype] = None
        return meta_cache[doctype]

    # Tous les champs Link avec une cible (options) renseignée, DocField natifs et
    # Custom Field — logique de découverte déjà exécutée avec succès sur DEV.
    fields = [
        (d.parent, d.fieldname, d.options)
        for d in frappe.get_all(
            "DocField",
            filters={"fieldtype": "Link", "options": ["is", "set"]},
            fields=["parent", "fieldname", "options"],
            limit_page_length=0,
        )
    ]
    fields += [
        (d.dt, d.fieldname, d.options)
        for d in frappe.get_all(
            "Custom Field",
            filters={"fieldtype": "Link", "options": ["is", "set"]},
            fields=["dt", "fieldname", "options"],
            limit_page_length=0,
        )
    ]

    entries = []
    ignored = []

    for parent, fieldname, target in fields:
        meta_parent = get_meta_cached(parent)
        if meta_parent is None:
            ignored.append("%s.%s -> %s : meta introuvable (parent)" % (parent, fieldname, target))
            continue

        meta_target = get_meta_cached(target)
        if meta_target is None:
            ignored.append("%s.%s -> %s : meta introuvable (cible)" % (parent, fieldname, target))
            continue

        if meta_parent.is_virtual:
            ignored.append("%s.%s -> %s : parent virtuel" % (parent, fieldname, target))
            continue

        if meta_target.is_virtual:
            ignored.append("%s.%s -> %s : cible virtuelle" % (parent, fieldname, target))
            continue

        if meta_target.issingle:
            ignored.append("%s.%s -> %s : cible Single" % (parent, fieldname, target))
            continue

        if meta_parent.issingle:
            # Un Single n'a qu'une seule ligne de configuration : le document de
            # tête est le Single lui-même (doctype == name).
            value = frappe.db.get_single_value(parent, fieldname)
            if value and not frappe.db.exists(target, value):
                entries.append({
                    "field": "%s.%s" % (parent, fieldname),
                    "target": target,
                    "doctype": parent,
                    "name": parent,
                    "value": value,
                })
            continue

        select_fields = ["name", fieldname]
        if meta_parent.istable:
            # Table enfant : le document de tête est parent/parenttype, pas la ligne.
            select_fields += ["parent", "parenttype"]

        try:
            rows = frappe.get_all(
                parent,
                filters={fieldname: ["is", "set"]},
                fields=select_fields,
                limit_page_length=0,
            )
        except Exception as ex:
            frappe.db.rollback()
            ignored.append("%s.%s -> %s : %s" % (parent, fieldname, target, type(ex).__name__))
            continue

        distinct_values = sorted(set(
            row.get(fieldname)
            for row in rows
            if row.get(fieldname) not in (None, "")
        ))

        # Vérification par paquets de 500 valeurs distinctes (évite un IN() géant).
        existing_normalized = set()
        for i in range(0, len(distinct_values), 500):
            chunk = distinct_values[i:i + 500]
            found = frappe.get_all(
                target,
                filters={"name": ["in", chunk]},
                pluck="name",
                limit_page_length=0,
            )
            for name in found:
                existing_normalized.add(core.normalize(name))

        for row in rows:
            value = row.get(fieldname)
            if value in (None, ""):
                continue
            if core.normalize(value) in existing_normalized:
                continue
            if meta_parent.istable:
                doctype = row.get("parenttype")
                name = row.get("parent")
            else:
                doctype = parent
                name = row.get("name")
            entries.append({
                "field": "%s.%s" % (parent, fieldname),
                "target": target,
                "doctype": doctype,
                "name": name,
                "value": value,
            })

    entries = core.dedupe(entries)
    fields_total = len(fields)
    fields_checked = fields_total - len(ignored)

    report = core.compare(entries, baseline, fields_total, fields_checked, ignored)

    if report_path:
        core.dump_json(report, report_path)

    if write_baseline_path:
        # Conserve les raisons déjà écrites dans la référence courante ; une clé
        # nouvellement apparue reçoit "À JUSTIFIER" (core.baseline_from_entries).
        reasons = {}
        for field_key, data in (baseline.get("fields") or {}).items():
            reasons[field_key] = data.get("reason", "À JUSTIFIER")

        import datetime

        generated_at = datetime.datetime.now().isoformat()
        site = getattr(frappe.local, "site", "")
        fresh_baseline = core.baseline_from_entries(entries, site, generated_at, lot, reasons)
        core.dump_json(fresh_baseline, write_baseline_path)

    print("LINK_CHECK_STATUS=%s" % report["status"])
    print("LINK_CHECK_SUMMARY=%s" % report["summary"])
    print("LINK_CHECK_COUNTERS=%s" % json.dumps(report["counters"], sort_keys=True))
    for entry in report["anomalies"]:
        print("LINK_CHECK_NEW %s -> %s | %s %s | %s" % (
            entry["field"], entry["target"], entry["doctype"], entry["name"], entry["value"],
        ))
    for key in report["resolved"]:
        print("LINK_CHECK_RESOLVED %s" % key)

    frappe.db.rollback()


try:
    main()
except Exception as _link_check_exc:
    print("LINK_CHECK_STATUS=ERROR")
    print("LINK_CHECK_ERROR=%s: %s" % (type(_link_check_exc).__name__, _link_check_exc))
    try:
        frappe.db.rollback()
    except Exception:
        pass
