#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Contrôle de cohérence « ERPNext d'abord » : valeurs codées en dur (OPEN-204).

Lot : OPEN-204-DEVOPS-ERP-COHERENCE-GUARDIANS-001 (Tweezer1/arkonex-ops-docs#359).

Cherche, dans le code d'une application Arkonex (hors essais), des valeurs qu'ERPNext tient
déjà par sa configuration : taux et noms de taxes, comptes d'une société, nom de société,
devise, adresses de courriel. Lecture seule : rien n'est écrit, aucune base n'est lue.

  coherence_check.py DOSSIER_DU_DÉPÔT [--baseline FICHIER] [--json]

Verdict :
  PASS  aucun cas nouveau (chaque cas trouvé est dans la liste de référence, ou porte sur
        sa ligne la marque « arkonex-coherence: <raison> ») ;
  FAIL  au moins un cas nouveau (code de sortie 1) ;
  ERREUR dossier illisible ou liste de référence invalide (code de sortie 2).

Même entrée, même sortie : les cas sont triés, rien ne dépend de l'heure ni de l'ordre du
système de fichiers. Seuls les textes et les nombres du code sont examinés (jamais les
commentaires) : en Python par le module tokenize, en JavaScript par un découpage qui retire
les commentaires.
"""

import argparse
import io
import json
import os
import re
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASELINE = os.path.join(HERE, "coherence_baseline.json")
MARK = re.compile(r"arkonex-coherence:\s*(\S.*)")

# Familles : (identifiant, motif, application, explication donnée à l'agent)
#   application « text » = contenu d'un texte entre guillemets, en entier ;
#   application « number » = nombre écrit dans le code.
FAMILIES = [
    # Taux distinctifs seulement (TVQ 9,975 %, TPS+TVQ 14,975 %) : 0.05, 0.13 ou 0.15 sont
    # trop courants dans le code (facteurs, marges) ; une TPS codée en dur se repère par son
    # nom (famille nom-taxe).
    ("taux-taxe", re.compile(r"^(?:0\.09975|9\.975|0\.14975|14\.975)$"),
     "number",
     "Taux de taxe écrit dans le code : lire les modèles de taxes de la société "
     "(Sales Taxes and Charges Template, Item Tax Template)."),
    ("nom-taxe", re.compile(r"^(?:TPS|TVQ|TVH|GST|QST|HST|PST)(?:\s.*)?$", re.I), "text",
     "Nom de taxe écrit dans le code : les taxes sont celles des modèles de la société."),
    ("compte-societe", re.compile(r"^[^\n]{2,80} - [A-Z][A-Za-z0-9]{0,5}$"), "text",
     "Nom de compte (ou d'entrepôt, de centre de coûts) suffixé par l'abréviation d'une "
     "société : le lire dans la configuration de la société (Company, Account)."),
    ("societe", re.compile(r"^Arkonex$"), "text",
     "Nom de société écrit dans le code : le recevoir du document (champ company) ou des "
     "valeurs par défaut de l'utilisateur."),
    ("devise", re.compile(r"^(?:CAD|USD|EUR)$"), "text",
     "Devise écrite dans le code : la lire sur la société ou le document."),
    ("courriel", re.compile(r"^[A-Za-z0-9._%+-]+@(?!essai\.invalid$|example\.(?:com|org|net)$)"
                            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"), "text",
     "Adresse de courriel écrite dans le code : la lire sur le contact, l'utilisateur ou "
     "le compte de courriel."),
]
FAMILY_IDS = [f[0] for f in FAMILIES]

SKIP_DIRS = {".git", "node_modules", "__pycache__", "tests", "test", "dist", "build",
             "public/dist"}
JS_TOKEN = re.compile(
    r"//[^\n]*|/\*.*?\*/"                                   # commentaires (retirés)
    r"|(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)"  # textes
    r"|(?<![\w.])(\d+\.\d+|\d+)(?![\w.])",                  # nombres
    re.S)


def is_test_file(rel):
    name = os.path.basename(rel)
    parts = rel.split("/")
    return (name.startswith("test_") or name.endswith("_test.py") or ".test." in name
            or ".spec." in name or any(p in ("tests", "test") for p in parts[:-1]))


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and
                             (rel_dir + "/" + d).lstrip("./") not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith((".py", ".js")) and not name.endswith(".min.js"):
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                if not is_test_file(rel):
                    yield rel


def literals_py(source):
    """(ligne, genre, valeur) pour chaque texte et nombre d'un fichier Python."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING:
                m = re.match(r"(?i)^([rbuf]*)(\"\"\"|'''|\"|')(.*)\2$", tok.string, re.S)
                if m and "f" not in m.group(1).lower():
                    out.append((tok.start[0], "text", m.group(3)))
            elif tok.type == tokenize.NUMBER:
                out.append((tok.start[0], "number", tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def literals_js(source):
    out = []
    for m in JS_TOKEN.finditer(source):
        line = source.count("\n", 0, m.start()) + 1
        if m.group(1):
            out.append((line, "text", m.group(1)[1:-1]))
        elif m.group(2):
            out.append((line, "number", m.group(2)))
    return out


def scan(root):
    """Renvoie (cas, illisibles). Un cas : dict(path, line, family, value, marked)."""
    cases, unreadable = [], []
    for rel in iter_files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError):
            unreadable.append(rel)
            continue
        lits = literals_py(source) if rel.endswith(".py") else literals_js(source)
        if lits is None:
            unreadable.append(rel)
            continue
        lines = source.splitlines()
        for line, kind, value in lits:
            for fid, rx, applies, _ in FAMILIES:
                if applies == kind and rx.match(value.strip() if kind == "text" else value):
                    text = lines[line - 1] if line <= len(lines) else ""
                    cases.append({"path": rel, "line": line, "family": fid,
                                  "value": value.strip(), "marked": bool(MARK.search(text))})
    cases.sort(key=lambda c: (c["path"], c["line"], c["family"], c["value"]))
    return cases, sorted(unreadable)


def load_baseline(path, repo):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("repos", {}).get(repo, [])
    allowed = {}
    for e in entries:
        if not str(e.get("raison", "")).strip():
            raise ValueError("entrée sans raison : {}".format(e))
        if e.get("family") not in FAMILY_IDS:
            raise ValueError("famille inconnue : {}".format(e))
        key = (e["path"], e["family"], e["value"])
        allowed[key] = allowed.get(key, 0) + int(e.get("count", 1))
    return allowed


def repo_name(root):
    return os.path.basename(os.path.abspath(root).rstrip("/"))


def evaluate(root, baseline_path=DEFAULT_BASELINE, repo=None):
    repo = repo or repo_name(root)
    cases, unreadable = scan(root)
    allowed = load_baseline(baseline_path, repo)
    used, new = {}, []
    for c in cases:
        if c["marked"]:
            continue
        key = (c["path"], c["family"], c["value"])
        used[key] = used.get(key, 0) + 1
        if used[key] > allowed.get(key, 0):
            new.append(c)
    return {"repo": repo, "verdict": "FAIL" if new else "PASS", "new": new,
            "known": len(cases) - len(new), "unreadable": unreadable}


def explain(fid):
    return next(f[3] for f in FAMILIES if f[0] == fid)


def render(result):
    lines = ["Contrôle de cohérence ERPNext d'abord (OPEN-204) — {} : {}".format(
        result["repo"], result["verdict"])]
    lines.append("Cas connus (liste de référence ou marque) : {} ; cas nouveaux : {}".format(
        result["known"], len(result["new"])))
    for c in result["new"]:
        lines.append("  - {}:{} [{}] « {} » — {}".format(
            c["path"], c["line"], c["family"], c["value"], explain(c["family"])))
    if result["new"]:
        lines.append("Corriger en lisant la configuration d'ERPNext, ou, si la valeur est "
                     "réellement voulue, ajouter sur la ligne le commentaire "
                     "« arkonex-coherence: <raison> » (la raison sera relue en revue).")
    if result["unreadable"]:
        lines.append("Fichiers non analysés (illisibles) : " + ", ".join(result["unreadable"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--repo", help="nom du dépôt dans la liste de référence (défaut : nom du dossier)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="lister tous les cas trouvés (inventaire)")
    opts = ap.parse_args(argv)
    if not os.path.isdir(opts.root):
        print("ERREUR : dossier introuvable : {}".format(opts.root))
        return 2
    if opts.all:
        cases, unreadable = scan(opts.root)
        print(json.dumps({"cases": cases, "unreadable": unreadable}, ensure_ascii=False,
                         indent=1))
        return 0
    try:
        result = evaluate(opts.root, opts.baseline, opts.repo)
    except (OSError, ValueError, KeyError) as exc:
        print("ERREUR : liste de référence invalide ({})".format(exc))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1) if opts.json else render(result))
    return 1 if result["new"] else 0


if __name__ == "__main__":
    sys.exit(main())
