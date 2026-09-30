#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Fiche de reprise d'un lot suivi dans GitHub — OPEN-170 (Issue #195), RB-83 §4.10.5.

Source versionnée : arkonex-devops-tools/claude-code-hooks/ (voir install.py).
Copie déployée    : /home/frappe/frappe-bench/.claude/hooks/, appelée par le skill
                    /reprendre-lot <numéro d'Issue>.

Lecture seule sur GitHub : une requête GraphQL (gh), durée bornée, jamais bloquante. Seule
écriture : un petit fichier propre à la session (lot déclaré et fiche), que le contrôle de
démarrage relit après une compaction (session_start.py --compact). La fiche est une preuve
de lecture datée, sans autorité propre : l'Issue fait foi (docs/GOUVERNANCE-GITHUB-ISSUES.md
§12).

Usage : reprendre_lot.py <numéro d'Issue>
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys

OWNER = "Tweezer1"
DOCS_REPO = "arkonex-ops-docs"
MARKER = "DÉCIDÉ-MÉTIER"
STATE_DIR = os.environ.get("ARKONEX_REPRISE_DIR",
                           os.path.expanduser("~/.cache/arkonex-claude/reprise"))
TIMEOUT = float(os.environ.get("ARKONEX_REPRISE_TIMEOUT", "20"))
RECENT = 15          # décisions en vigueur affichées (les plus récentes)
MAX_GATES = 15       # étapes restantes affichées

QUERY = """# reprise-open170
query($n: Int!) {
  repository(owner: "%s", name: "%s") {
    issue(number: $n) {
      number title url state createdAt updatedAt lastEditedAt body
      labels(first: 30) { nodes { name } }
      comments(last: 100) { nodes { createdAt url body } }
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 50) {
        nodes { ... on CrossReferencedEvent { createdAt source { __typename
          ... on Issue { number title }
          ... on PullRequest { number title repository { name } } } } }
      }
    }
    registre: object(expression: "main:BESOINS-TRANSVERSES.md") { ... on Blob { text } }
  }
}
""" % (OWNER, DOCS_REPO)

FRESH_QUERY = """# fraicheur-open170
query($n: Int!) {
  repository(owner: "%s", name: "%s") { issue(number: $n) { updatedAt } }
}
""" % (OWNER, DOCS_REPO)

LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
URL = re.compile(r"\((https?://[^)\s]+)\)")
HEADING = re.compile(r"^###\s+(?:\d+(?:\.\d+)*\.?\s+)?(.+?)\s*$")


def gh_binary():
    return (os.environ.get("ARKONEX_GH") or shutil.which("gh")
            or os.path.expanduser("~/.local/bin/gh"))


def gh_graphql(query, variables, timeout):
    """Lecture seule. Lève TimeoutError ou RuntimeError ; jamais d'écriture."""
    cmd = [gh_binary(), "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        cmd += ["-F", "{}={}".format(key, value)]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise TimeoutError
    if r.returncode != 0:
        raise RuntimeError("gh a échoué, code {}".format(r.returncode))
    data = json.loads(r.stdout)
    if data.get("errors"):
        raise RuntimeError("GraphQL : " + "; ".join(
            str(e.get("message", "?")) for e in data["errors"])[:160])
    return data["data"]


def plain(text, limit=None):
    """Texte lisible : liens réduits à leur libellé, espaces normalisés."""
    text = re.sub(r"\s+", " ", LINK.sub(r"\1", text or "")).strip()
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def first_line(body):
    return next((l.strip() for l in (body or "").splitlines() if l.strip()), "")


def sections(body):
    """Sections « ### Titre » du corps (modèle open-lot, numérotation tolérée)."""
    found, name, buf = {}, None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = HEADING.match(line)
        if m:
            if name is not None and name not in found:
                found[name] = "\n".join(buf).strip()
            name, buf = m.group(1), []
        elif name is not None:
            if line.startswith("## "):
                found.setdefault(name, "\n".join(buf).strip())
                name, buf = None, []
            else:
                buf.append(line)
    if name is not None and name not in found:
        found[name] = "\n".join(buf).strip()
    return found


def decision_rows(text):
    """Lignes du tableau des décisions (§12.2) : date, décision, portée, lien, état."""
    rows = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if len(cells) < 5 or cells[0].lower() == "date" or set(cells[0]) <= set("-: "):
            continue
        link = URL.search(cells[-2])
        rows.append({"date": cells[0], "decision": cells[1], "portee": cells[2],
                     "url": link.group(1) if link else "", "etat": cells[-1]})
    return rows


def kind(etat):
    e = etat.lower()
    if e.startswith("à intégrer") or e.startswith("a integrer"):
        return "recu"
    if "en partie" in e:              # « en partie remplacée » : reste en vigueur pour le reste
        return "vigueur"
    if "remplacée" in e or "remplacee" in e:
        return "remplacee"
    if e.startswith("intégré") or e.startswith("écarté"):
        return "traite"
    return "vigueur"


def order(row):
    """Ordre chronologique : date, puis numéro du commentaire source (croissant dans le
    temps sur GitHub) pour départager les décisions d'un même jour."""
    m = re.search(r"issuecomment-(\d+)", row["url"])
    return (row["date"], int(m.group(1)) if m else 0)


def registry_entries(text):
    """Fiches « ### BT-nnn — … » du registre : {'BT-003': (titre, qualification)}."""
    entries, current = {}, None
    for line in (text or "").split("\n"):
        m = re.match(r"^###\s+(BT-\d+)\s+—\s+(.+)$", line.strip())
        if m:
            current = m.group(1)
            entries[current] = [m.group(2).strip(), ""]
        elif current and line.strip().startswith("- **Qualification**"):
            entries[current][1] = plain(line.split(":", 1)[-1], 140)
    return {k: tuple(v) for k, v in entries.items()}


def build_fiche(issue, registre, now):
    """Fiche de reprise (texte) et méta-données, à partir de la réponse GraphQL."""
    body = issue.get("body") or ""
    secs = sections(body)
    labels = [n["name"] for n in (issue.get("labels") or {}).get("nodes", [])]
    m = re.search(r"OPEN-\d+[A-Z0-9-]*", issue.get("title") or "")
    lot = m.group(0).rstrip("-") if m else "OPEN-?"
    body_time = issue.get("lastEditedAt") or issue.get("createdAt") or ""
    comments = (issue.get("comments") or {}).get("nodes", [])
    decisions = [c for c in comments if MARKER in first_line(c.get("body"))]
    late = [c for c in decisions if c.get("createdAt", "") > body_time]
    last_decision = max((c["createdAt"] for c in decisions), default="aucune")
    mentions = [n for n in (issue.get("timelineItems") or {}).get("nodes", [])
                if n.get("createdAt", "") > body_time and n.get("source")]
    table = decision_rows(secs.get("Decision / gate evidence", ""))
    in_force = [r for r in table if kind(r["etat"]) == "vigueur"]
    replaced = [r for r in table if kind(r["etat"]) == "remplacee"]
    received = [r for r in table if kind(r["etat"]) == "recu"]
    gates = [l.strip()[6:] for l in secs.get("Remaining gates", "").split("\n")
             if l.strip().startswith("- [ ]")]
    besoins = sorted(l for l in labels if l.startswith("besoin:"))
    reg = registry_entries(registre)
    status = [l for l in labels if l.startswith(("status:", "risk:", "review:"))]

    out = ["---",
           "lot: " + lot,
           "issue: {}".format(issue.get("number")),
           "lue_le: " + now,
           "corps_edite_le: " + (body_time or "inconnu"),
           "derniere_decision_le: " + last_decision,
           "recus_non_integres: {}".format(len(received)),
           "a_reconcilier: " + ("true" if late else "false"),
           "---",
           "# Fiche de reprise — #{} — {}".format(issue.get("number"), lot),
           "",
           "{} ({}) — {}".format(plain(issue.get("title"), 160),
                                 (issue.get("state") or "").lower(), issue.get("url", "")),
           "Étiquettes : {}{}".format(", ".join(status) or "aucune",
                                      " ; besoins : " + ", ".join(besoins) if besoins else ""),
           "",
           "## Objectif",
           plain(secs.get("Objectif / objet", ""), 700) or "(section absente)",
           ""]
    if table:
        out.append("## Décisions en vigueur ({} ; {} remplacée(s))".format(len(in_force),
                                                                          len(replaced)))
        recent = sorted(in_force, key=order)[-RECENT:]
        if len(in_force) > len(recent):
            out.append("Les {} plus récentes ; les autres sont dans le tableau de "
                       "l'Issue.".format(len(recent)))
        for r in recent:
            etat = plain(r["etat"], 90)
            out.append("- {} · {} · {} — {}{}".format(
                r["date"], plain(r["portee"], 30), plain(r["decision"], 170), r["url"],
                "" if etat.lower() == "en vigueur" else " ({})".format(etat)))
    else:
        out.append("## Décisions")
        out.append("Tableau des décisions absent du corps (format §12.2 non appliqué). "
                   "Décisions repérées par le marqueur [{}] : {}.".format(MARKER, len(decisions)))
        for c in decisions[-RECENT:]:
            out.append("- {} · {} — {}".format(c["createdAt"][:10],
                                              plain(first_line(c["body"]).lstrip("# "), 150),
                                              c.get("url", "")))
    out.append("")
    out.append("## Éléments reçus d'autres lots, à intégrer ({})".format(len(received)))
    out += ["- {} · {} — {}".format(r["date"], plain(r["decision"], 190), r["url"])
            for r in received] or ["- aucun"]
    out.append("")
    out.append("## Décisions consignées après la dernière mise à jour du résumé "
               "({})".format(len(late)))
    out += ["- {} · {} — {}".format(c["createdAt"][:16].replace("T", " "),
                                   plain(first_line(c["body"]).lstrip("# "), 150), c.get("url", ""))
            for c in late] or ["- aucune"]
    out.append("")
    out.append("## Mentions reçues d'autres dossiers depuis la dernière mise à jour du "
               "résumé ({})".format(len(mentions)))
    for n in mentions:
        src = n["source"]
        repo = (src.get("repository") or {}).get("name", DOCS_REPO)
        prefix = "" if repo == DOCS_REPO else repo
        out.append("- {} · {}#{} {}".format(n["createdAt"][:10], prefix, src.get("number"),
                                           plain(src.get("title"), 90)))
    if not mentions:
        out.append("- aucune")
    out.append("")
    out.append("## Besoins transverses")
    if besoins:
        for b in besoins:
            key = b.split(":", 1)[1].upper()
            title, qual = reg.get(key, ("fiche introuvable dans BESOINS-TRANSVERSES.md", ""))
            out.append("- {} — {}{}".format(key, plain(title, 110),
                                           " — " + qual if qual else ""))
    else:
        out.append("- aucun (aucune étiquette besoin:*)")
    out.append("")
    out.append("## Étapes restantes ({})".format(len(gates)))
    out += ["- " + plain(g, 200) for g in gates[:MAX_GATES]] or ["- aucune case ouverte"]
    if len(gates) > MAX_GATES:
        out.append("- … {} autre(s) dans l'Issue".format(len(gates) - MAX_GATES))
    out.append("")
    alerts = []
    if late:
        alerts.append("résumé en retard sur {} décision(s) : mettre le tableau à jour (même "
                      "geste, §12.2) avant tout travail qui en dépend".format(len(late)))
    if not table:
        alerts.append("tableau des décisions absent du corps (§12.2)")
    if re.search(r"(?m)^\*\*Phase\s*:\s*[A-Z_]+\.\*\*", body):
        alerts.append("une phrase de statut figure dans le corps ; le statut est l'étiquette "
                      "status:* (§4)")
    if received:
        alerts.append("{} élément(s) reçu(s) à intégrer ou à écarter".format(len(received)))
    out.append("## Contradictions et alertes")
    out += ["- " + a for a in alerts] or ["- aucune"]
    out.append("")
    out.append("## Sources lues")
    out.append("- Issue #{} (mise à jour {}, corps édité {}), {} commentaire(s) lus ; "
               "BESOINS-TRANSVERSES.md sur main{}.".format(
                   issue.get("number"), issue.get("updatedAt", "?"), body_time or "?",
                   len(comments), "" if registre else " (illisible)"))
    out.append("")
    out.append("## Prochain travail permis")
    out.append("À établir avec le propriétaire à partir des étapes restantes et des décisions "
               "en vigueur. Présenter cette fiche avant toute action et attendre sa réponse "
               "(GOUVERNANCE-GITHUB-ISSUES.md §12, point 6).")
    meta = {"lot": lot, "issue": issue.get("number"), "lue_le": now,
            "issue_updated_at": issue.get("updatedAt", ""), "a_reconcilier": bool(late),
            "recus_non_integres": len(received)}
    return "\n".join(out), meta


def safe_id(session_id):
    return re.sub(r"[^A-Za-z0-9-]", "", session_id or "")[:80]


def state_path(session_id):
    return os.path.join(STATE_DIR, safe_id(session_id) + ".json")


def save_state(session_id, meta, fiche):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path = state_path(session_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(meta, fiche=fiche), fh, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_state(session_id):
    if not safe_id(session_id):
        return None
    try:
        with open(state_path(session_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv):
    arg = (argv[1] if len(argv) > 1 else "").lstrip("#")
    if not arg.isdigit():
        print("Usage : reprendre_lot.py <numéro d'Issue> (exemple : reprendre_lot.py 181)")
        return 0
    number = int(arg)
    try:
        data = gh_graphql(QUERY, {"n": number}, TIMEOUT)
        issue = (data.get("repository") or {}).get("issue")
        if not issue:
            print("Issue #{} introuvable dans {}/{}.".format(number, OWNER, DOCS_REPO))
            return 0
        registre = ((data["repository"].get("registre") or {}).get("text"))
        fiche, meta = build_fiche(issue, registre, utc_now())
    except TimeoutError:
        print("Fiche de reprise indisponible (délai dépassé). Lire l'Issue : "
              "gh issue view {} -R {}/{}".format(number, OWNER, DOCS_REPO))
        return 0
    except Exception as exc:  # jamais bloquant
        print("Fiche de reprise indisponible ({}). Lire l'Issue : gh issue view {} -R {}/{}"
              .format(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                      number, OWNER, DOCS_REPO))
        return 0
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if safe_id(session_id):
        try:
            save_state(session_id, meta, fiche)
            note = ("Lot déclaré pour cette session : la fiche sera réinjectée après une "
                    "compaction.")
        except OSError as exc:
            note = "Lot non enregistré pour la session ({}).".format(type(exc).__name__)
    else:
        note = ("Session inconnue (CLAUDE_CODE_SESSION_ID absent) : pas de réinjection après "
                "une compaction.")
    print(fiche + "\n\n_" + note + "_")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
