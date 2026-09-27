#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Garde-fou PreToolUse (outil Bash) du harnais Claude Code — instance DEV Arkonex.

Source versionnée : arkonex-devops-tools/claude-code-hooks/ (OPEN-161, Issue #168).
Copie déployée    : /home/frappe/frappe-bench/.claude/hooks/ (voir install.py).

Lit l'appel d'outil (JSON) sur l'entrée standard et répond :
  - rien, code 0              : la commande suit le circuit normal de permissions ;
  - permissionDecision "deny" : refus (invariant de CLAUDE.md) ;
  - permissionDecision "ask"  : validation humaine dans l'interface.
Le garde ne répond jamais "allow" : il ne peut qu'ajouter des contrôles.

Les règles visent la commande exécutée, pas le texte : arguments entre guillemets,
corps de heredoc (sauf s'il est donné à un shell), commentaires et noms de branches
ne déclenchent rien. Les commandes imbriquées (bash -c, sh -c, eval, sudo, $(...),
`...`) sont analysées avec les mêmes règles.

Limite assumée : protège contre l'erreur, pas contre un contournement délibéré
(commande masquée dans une variable, un script intermédiaire, un encodage…).
"""

import json
import os
import re
import shlex
import subprocess
import sys

DEV_HOST = "deverp.arkonex.ca"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
SITE_OK = re.compile(r"^(?:deverp\.arkonex\.ca|" + _LABEL + r"(?:\." + _LABEL + r")*\.local)$",
                     re.I)
# Hôte arkonex.ca en position de CONNEXION : début d'argument, éventuellement précédé d'un
# schéma (https://, ssh://…) et d'un utilisateur (frappe@), suivi de : / ? # ou de la fin.
# Un nom d'hôte cité dans un texte ou dans des données envoyées ne déclenche rien.
HOST_AT_START = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^@/\s:]+@)?"
                           r"((?:[A-Za-z0-9-]+\.)*arkonex\.ca)(?=$|[:/?#])", re.I)
# Hôte arkonex.ca dans le code réellement exécuté par un interpréteur (argument de -c/-e,
# heredoc donné à python/node/bench console, arguments de bench execute) : URL (://hôte) ou
# nom d'hôte seul passé en argument d'un appel — SMTP('hôte'), connect(("hôte", 22)).
# Une simple mention du nom dans un texte ne déclenche rien.
CODE_HOST = re.compile(r"://(?:[^@/\s'\"]+@)?((?:[A-Za-z0-9-]+\.)*arkonex\.ca)(?![A-Za-z0-9.-])"
                       r"|\(\s*\(?\s*(['\"])((?:[A-Za-z0-9-]+\.)*arkonex\.ca)\2", re.I)
CODE_OPTS = {"-c", "-e", "-E", "-p", "-r", "--eval", "--print"}

NET_CMDS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet",
            "http", "https", "xh", "ftp", "lftp", "socat", "mosh"}
# Options dont la valeur est une donnée envoyée ou un fichier local, pas une destination.
DATA_OPTS = {
    "curl": {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii",
             "--json", "-F", "--form", "--form-string", "-H", "--header", "-e", "--referer",
             "-A", "--user-agent", "-b", "--cookie", "-c", "--cookie-jar", "-o", "--output",
             "-T", "--upload-file", "-u", "--user", "-w", "--write-out", "--url-query",
             "--proxy-header", "-K", "--config"},
    "wget": {"--post-data", "--body-data", "--post-file", "--body-file", "--header", "-O",
             "--output-document", "-o", "--output-file", "-U", "--user-agent", "--referer",
             "--user", "--password", "-P", "--directory-prefix"},
}
DATA_OPTS["http"] = DATA_OPTS["https"] = DATA_OPTS["xh"] = DATA_OPTS["curl"]
_INTERP = r"(?:python\d*(?:\.\d+)?|pypy\d*|node|nodejs|perl|ruby|php|deno|bun)"
INTERPRETERS = re.compile(r"^" + _INTERP + r"$")
SQL_CMDS = {"mysql", "mariadb", "mysqldump", "mariadb-dump", "mysqladmin", "mariadb-admin",
            "mysqlshow", "mariadb-show"}
BENCH_SQL_SUBCMDS = {"mariadb", "db-console", "mysql", "postgres"}
SQL_EXECUTE_TARGETS = re.compile(r"^frappe\.db\.(?:sql|multisql|sql_ddl)$")
SHELLS = {"bash", "sh", "dash", "zsh", "ksh"}
KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "time"}
# Enveloppes : mot -> options qui prennent une valeur séparée.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"},
    "xargs": {"-I", "-d", "--delimiter", "-E", "-L", "-n", "--max-args", "-P", "--max-procs",
              "-s", "--max-chars", "-a", "--arg-file", "--process-slot-var"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "nohup": set(), "exec": {"-a"}, "command": set(), "builtin": set(),
}
SKIP_WHOLE = {"for", "case", "select", "function", "fi", "done", "esac", "[[", "]]", "[",
              "test", "echo", "printf", "true", "false", ":"}
PROTECTED_BRANCHES = {"main", "master"}
INFO_FLAGS = {"--version", "-V", "--help", "-?"}
SEP_CHARS = set(";&|()\n")

SEVERITY = {"deny": 2, "ask": 1}

MSG = {
    "R-SQL": "Accès direct à la base de données refusé : CLAUDE.md interdit toute requête SQL "
             "directe ; passer par l'ORM Frappe.",
    "R-HOST": "Connexion vers {host} refusée : depuis cette instance DEV, seul "
              "deverp.arkonex.ca est autorisé (CLAUDE.md, invariant n° 1).",
    "R-HOST-CODE": "Du code exécuté mentionne le serveur {host}, autre que deverp.arkonex.ca : "
                   "validation humaine (connexion possible, non vérifiable).",
    "R-SITE": "Site bench « {site} » refusé : seuls deverp.arkonex.ca et les sites de test "
              "en .local sont autorisés.",
    "R-SITE-VAR": "Site bench « {site} » non vérifiable (variable non résolue) : "
                  "validation humaine.",
    "R-MIGRATE": "bench migrate sans --site explicite refusé (CLAUDE.md).",
    "R-UPDATE": "bench update est écarté sur cette instance (OPEN-142) : mettre à jour app par "
                "app (git fetch --tags, puis checkout du tag).",
    "R-BUILD": "bench build global interdit sur cette instance (OPEN-142) : construire app par "
               "app avec --app.",
    "R-SUDO": "Action avec droits d'administrateur (sudo) : validation humaine requise.",
    "R-PUSH-MAIN": "Push vers la branche protégée {branch} : validation humaine requise "
                   "(CLAUDE.md).",
    "R-PUSH-FORCE": "Push forcé : validation humaine requise.",
    "R-DROP-SITE": "bench drop-site : validation humaine requise.",
}


class Verdicts:
    """Accumule les décisions ; seule la plus sévère est rendue."""

    def __init__(self):
        self.items = []

    def add(self, decision, rule, **fmt):
        self.items.append((decision, rule, MSG[rule].format(**fmt)))

    def worst(self):
        if not self.items:
            return None
        return max(self.items, key=lambda item: SEVERITY[item[0]])


# ------------------------------------------------------------------------------ lecture

_HEREDOC = re.compile(r"<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z0-9_.-]+)\3")
_CODE_BEFORE = re.compile(r"(?:^|[;&|(])\s*(?:\S+=\S*\s+)*(?:(?:sudo|env|exec|timeout|nohup)"
                          r"(?:\s+\S+)*?\s+)?(?:(?:\S*/)?" + _INTERP + r"\b|(?:\S*/)?bench\b"
                          r"[^;&|]*\b(?:console|execute)\b)[^;&|]*$")
_SHELL_BEFORE = re.compile(r"(?:^|[;&|(])\s*(?:\S+=\S*\s+)*(?:(?:sudo|env|exec|timeout|nohup)"
                           r"(?:\s+\S+)*?\s+)?(?:\S*/)?(?:bash|sh|dash|zsh|ksh)\b[^;&|]*$")


def _scan(line, stack, pending, buf):
    """Lit une ligne avec la pile de contextes et écrit dans buf le texte à analyser.

    Contextes : ["N", parenthèses, parent] texte normal ou substitution $( ... ) ;
    ["'"] et ['"'] guillemets ; ["H"] corps de heredoc à délimiteur non protégé, où seuls
    $( ... ) et `...` sont exécutés par bash (le reste est du texte, ignoré)."""
    j, n = 0, len(line)
    while j < n:
        ch = line[j]
        top = stack[-1]
        kind = top[0]
        if kind == "N":
            if ch == "#" and (j == 0 or line[j - 1] in " \t;&|()"):
                return
            if ch == "\\" and j + 1 < n:
                buf.append(line[j:j + 2])
                j += 2
                continue
            if ch == "`":
                buf.append(" ; ")
                j += 1
                continue
            if ch in "'\"":
                stack.append([ch])
            elif line.startswith("$((", j):
                buf.append("$((")
                top[1] += 2
                j += 3
                continue
            elif line.startswith("$(", j):
                stack.append(["N", 0, "N"])
                buf.append(" ; ")
                j += 2
                continue
            elif ch == "(":
                top[1] += 1
            elif ch == ")" and top[2] is not None:
                if top[1] == 0:
                    stack.pop()
                    buf.append(' ; "' if top[2] == '"' else " ; ")
                    j += 1
                    continue
                top[1] -= 1
            elif line.startswith("<<", j) and not line.startswith("<<<", j) \
                    and (j == 0 or line[j - 1] != "<"):
                m = _HEREDOC.match(line, j)
                if m:
                    if _SHELL_BEFORE.search(line[:j]):
                        mode = "shell"                 # le corps est un script : commandes
                    elif m.group(2) or m.group(3):
                        mode = "literal"               # délimiteur protégé : texte pur
                    else:
                        mode = "expand"                # bash exécute les $( ) et ` `
                    is_code = mode != "shell" and bool(_CODE_BEFORE.search(line[:j]))
                    pending.append((m.group(4), m.group(1) == "-", mode, is_code))
                    buf.append(" ")
                    j = m.end()
                    continue
        elif kind == "'":
            if ch == "'":
                stack.pop()
        elif kind == "H":
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                end = line.find("`", j + 1)
                end = n if end < 0 else end
                buf.append(" ; " + line[j + 1:end] + " ; ")
                j = end + 1
                continue
            if line.startswith("$(", j) and not line.startswith("$((", j):
                stack.append(["N", 0, "H"])
                buf.append(" ; ")
                j += 2
                continue
            j += 1
            continue                                   # texte du heredoc : ignoré
        else:  # entre guillemets doubles
            if ch == "\\" and j + 1 < n:
                buf.append(line[j:j + 2])
                j += 2
                continue
            if ch == "`":
                end = line.find("`", j + 1)
                end = n if end < 0 else end
                buf.append('" ; ' + line[j + 1:end] + ' ; "')
                j = end + 1
                continue
            if line.startswith("$(", j) and not line.startswith("$((", j):
                stack.append(["N", 0, '"'])
                buf.append('" ; ')
                j += 2
                continue
            if ch == '"':
                stack.pop()
        buf.append(ch)
        j += 1


def preprocess(src, code=None):
    """Prépare le texte de la commande pour le découpage : retire commentaires et texte des
    heredocs, traite les continuations de ligne et sort le contenu des substitutions
    $( ... ) et `...` de leurs guillemets pour qu'il soit analysé comme une commande.

    Corps de heredoc : donné à un shell -> analysé comme des commandes ; délimiteur protégé
    (<<'EOF', <<"EOF", <<\\EOF) -> texte pur, ignoré ; délimiteur non protégé (<<EOF) ->
    seuls ses $( ... ) et `...` sont analysés, comme bash les exécute quel que soit le
    programme qui reçoit le heredoc. Si `code` est une liste, le corps des heredocs donnés
    à un interpréteur (python, node, bench console…) y est ajouté pour la règle R-HOST-CODE."""
    src = src.replace("\\\r\n", " ").replace("\\\n", " ")
    out = []
    stack = [["N", 0, None]]
    pending = []          # heredocs en attente : (délimiteur, retrait des tabs, mode, code)
    for line in src.split("\n"):
        if pending:
            delim, strip_tabs, mode, is_code = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate.strip() == delim:
                pending.pop(0)
                out.append("")
                continue
            if is_code and code is not None:
                code.append(line)
            if mode == "literal":
                out.append("")
                continue
            if mode == "expand":
                buf, local = [], [["H"]]
                _scan(line, local, [], buf)
                if len(local) > 1:                     # substitution non refermée
                    buf.append(" ; ")
                out.append("".join(buf))
                continue
        buf = []
        _scan(line, stack, pending, buf)
        out.append("".join(buf))
    return "\n".join(out)


def tokenize(text):
    """Renvoie (tokens, certain). Guillemets déséquilibrés : repli prudent, incertain."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex), True
    except ValueError:
        rough = re.findall(r"[;&|()\n]+|[<>]+&?\d*|[^\s;&|()<>]+", text)
        return [t.strip("'\"") for t in rough if t.strip("'\"")], False


def is_separator(tok):
    return bool(tok) and all(c in SEP_CHARS for c in tok)


def is_redirect(tok):
    return bool(tok) and all(c in "<>&|0123456789" for c in tok) and any(c in "<>" for c in tok)


def split_commands(tokens):
    """Découpe en commandes simples ; retire les redirections et leur cible."""
    cmds, cur, i = [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if is_separator(t) or (t.endswith("(") and any(c in "<>" for c in t)):  # <( ... )
            if cur:
                cmds.append(cur)
            cur = []
        elif is_redirect(t):
            if cur and cur[-1].isdigit():              # 2>&1 : le « 2 » n'est pas un argument
                cur.pop()
            i += 1                                     # la cible de la redirection
        else:
            cur.append(t)
        i += 1
    if cur:
        cmds.append(cur)
    return cmds


# ------------------------------------------------------------------------------ contexte

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")


class Context:
    def __init__(self, cwd, variables=None):
        self.cwd = cwd
        self.vars = dict(variables or {"HOME": os.path.expanduser("~")})

    def expand(self, s):
        return _VAR.sub(lambda m: self.vars.get(m.group(1) or m.group(2), m.group(0)), s)

    def resolve_dir(self, d):
        d = self.expand(d)
        if "$" in d or d == "-":
            return None
        d = os.path.expanduser(d)
        if not os.path.isabs(d):
            if self.cwd is None:
                return None
            d = os.path.join(self.cwd, d)
        return os.path.normpath(d)


def base(word):
    return os.path.basename(word)


def strip_prefixes(cmd, ctx):
    """Retire affectations, mots-clés et enveloppes (env, timeout, xargs…)."""
    i = 0
    while i < len(cmd):
        t = cmd[i]
        m = _ASSIGN.match(t)
        if m:
            ctx.vars[m.group(1)] = ctx.expand(m.group(2))
            i += 1
            continue
        if t in KEYWORDS:
            i += 1
            continue
        w = base(t)
        if w in WRAPPERS:
            with_value = WRAPPERS[w]
            i += 1
            while i < len(cmd):
                a = cmd[i]
                if a == "--":
                    i += 1
                    break
                if a.startswith("-"):
                    i += 2 if (a in with_value and "=" not in a) else 1
                    continue
                if w == "env" and _ASSIGN.match(a):
                    i += 1
                    continue
                if w == "timeout" and _DURATION.match(a):
                    i += 1
                break
            continue
        break
    return cmd[i:]


# ------------------------------------------------------------------------------ règles

def current_branch(directory):
    if not directory or not os.path.isdir(directory):
        return None
    try:
        r = subprocess.run(["git", "-C", directory, "symbolic-ref", "--short", "-q", "HEAD"],
                           capture_output=True, text=True, timeout=3,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def rule_git(args, ctx, v):
    i, repo_dir = 0, ctx.cwd
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            repo_dir = ctx.resolve_dir(args[i + 1])
            i += 2
            continue
        if a in ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env") \
                and i + 1 < len(args):
            i += 2
            continue
        i += 1
    if i >= len(args) or args[i] != "push":
        return
    rest, positional, force, j = args[i + 1:], [], False, 0
    with_value = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
    while j < len(rest):
        a = rest[j]
        if a in with_value:
            j += 2
            continue
        if a.startswith("-"):
            if a in ("--force", "--force-with-lease", "--force-if-includes", "--mirror") \
                    or a.startswith("--force-with-lease=") \
                    or (re.fullmatch(r"-[A-Za-z]+", a) and "f" in a[1:]):
                force = True
            if a in ("--all", "--branches", "--mirror"):
                v.add("ask", "R-PUSH-MAIN", branch="(toutes les branches)")
            j += 1
            continue
        positional.append(a)
        j += 1
    if force:
        v.add("ask", "R-PUSH-FORCE")
    refspecs = positional[1:]
    targets = [current_branch(repo_dir)] if not refspecs else []
    for r in refspecs:
        if r.startswith("+"):
            v.add("ask", "R-PUSH-FORCE")
            r = r[1:]
        dst = r.split(":", 1)[1] if ":" in r else r
        if dst in ("HEAD", "@"):
            dst = current_branch(repo_dir)
        if dst and dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        targets.append(dst)
    for t in targets:
        if t in PROTECTED_BRANCHES:
            v.add("ask", "R-PUSH-MAIN", branch=t)


def rule_bench(args, ctx, v):
    if "--help" in args:                               # simple affichage de l'aide
        return
    sites, pos, has_app, i = [], [], False, 0
    while i < len(args):
        a = args[i]
        if a == "--site" and i + 1 < len(args):
            sites.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--site="):
            sites.append(a[len("--site="):])
        elif a in ("--app", "--apps") or a.startswith(("--app=", "--apps=")):
            has_app = True
        elif not a.startswith("-"):
            pos.append(a)
        i += 1
    for s in sites:
        expanded = ctx.expand(s)
        if SITE_OK.match(expanded):
            continue
        if "$" in expanded:
            v.add("ask", "R-SITE-VAR", site=s)
        else:
            v.add("deny", "R-SITE", site=expanded)
    sub = pos[0] if pos else None
    if sub == "execute":
        rule_code_host(" ".join(args), v)
    if sub in BENCH_SQL_SUBCMDS:
        v.add("deny", "R-SQL")
    elif sub == "execute" and len(pos) > 1 and SQL_EXECUTE_TARGETS.match(pos[1]):
        v.add("deny", "R-SQL")
    elif sub == "migrate" and not sites:
        v.add("deny", "R-MIGRATE")
    elif sub == "update":
        v.add("deny", "R-UPDATE")
    elif sub == "build" and not has_app:
        v.add("deny", "R-BUILD")
    elif sub == "drop-site":
        v.add("ask", "R-DROP-SITE")


def rule_network(word, args, v):
    """Refuse une connexion vers un hôte arkonex.ca autre que DEV. Seules les destinations
    comptent : les valeurs des options de données (--data, -H, -F…) sont ignorées."""
    skip = DATA_OPTS.get(word, set())
    i = 0
    while i < len(args):
        a = args[i]
        if a in skip:
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            opt, a = a.split("=", 1)
            if opt in skip:
                i += 1
                continue
        m = HOST_AT_START.match(a)
        if m and m.group(1).lower() != DEV_HOST:
            v.add("deny", "R-HOST", host=m.group(1).lower())
        i += 1


def rule_code_host(code, v):
    """Code exécuté par un interpréteur : URL ou hôte passé à un appel, autre que DEV."""
    for m in CODE_HOST.finditer(code or ""):
        host = (m.group(1) or m.group(3)).lower()
        if host != DEV_HOST:
            v.add("ask", "R-HOST-CODE", host=host)
            return


_SUDO_WITH_VALUE = {"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-T", "-U", "--user",
                    "--group", "--prompt", "--close-from", "--chdir", "--host", "--role",
                    "--type", "--command-timeout", "--other-user"}
_SYSTEMCTL_READ = {"status", "show", "is-active", "is-enabled", "is-failed", "list-units",
                   "list-timers", "list-unit-files", "cat"}


def rule_sudo(args, ctx, v, depth):
    i, listmode = 0, False
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in _SUDO_WITH_VALUE:
            i += 2
            continue
        if a.startswith("--"):
            listmode = listmode or a == "--list"
            i += 1
            continue
        flags = a[1:]
        listmode = listmode or "l" in flags
        i += 2 if (len(flags) > 1 and flags[-1] in "ugpCDhrtTU") else 1
    inner = args[i:]
    if listmode:              # sudo -l [commande] : interroge les droits, n'exécute rien
        return
    if not inner:
        v.add("ask", "R-SUDO")
        return
    word = base(inner[0])
    read_only = (
        (word == "true" and len(inner) == 1)
        or (word == "supervisorctl" and len(inner) >= 2 and inner[1] == "status")
        or (word == "systemctl" and len(inner) >= 2 and inner[1] in _SYSTEMCTL_READ)
        or (word == "dmesg" and not any(x.startswith(("-c", "-C", "--clear", "--read-clear"))
                                        for x in inner[1:]))
    )
    if not read_only:
        v.add("ask", "R-SUDO")
    analyze_simple(inner, ctx, v, depth + 1)


def analyze_simple(cmd, ctx, v, depth):
    cmd = strip_prefixes(cmd, ctx)
    if not cmd:
        return
    word, args = base(cmd[0]), cmd[1:]
    if word in SKIP_WHOLE:
        return
    if word in ("cd", "pushd"):
        target = next((a for a in args if not a.startswith("-")), "~")
        ctx.cwd = ctx.resolve_dir(target)
        return
    if word == "sudo":
        rule_sudo(args, ctx, v, depth)
        return
    if word in SQL_CMDS and not (args and all(a in INFO_FLAGS for a in args)):
        v.add("deny", "R-SQL")
    if word in NET_CMDS:
        rule_network(word, args, v)
    if word == "git":
        rule_network(word, args, v)     # URL de dépôt : clone, fetch, push, remote add…
        rule_git(args, ctx, v)
    if INTERPRETERS.match(word):
        for k, a in enumerate(args[:-1]):
            if a in CODE_OPTS:
                rule_code_host(args[k + 1], v)
    if word == "bench":
        rule_bench(args, ctx, v)
    if word in SHELLS:
        for k, a in enumerate(args):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and k + 1 < len(args):
                analyze(args[k + 1], Context(ctx.cwd, ctx.vars), v, depth + 1)
                break
    if word == "eval" and args:
        analyze(" ".join(args), ctx, v, depth + 1)


def analyze(text, ctx, v, depth=0):
    """Analyse un texte de commandes ; renvoie False si le découpage a été incertain."""
    if depth > 6 or not text or not text.strip():
        return True
    code = []
    tokens, certain = tokenize(preprocess(text, code))
    for cmd in split_commands(tokens):
        analyze_simple(cmd, ctx, v, depth)
    rule_code_host("\n".join(code), v)
    return certain


def evaluate(command, cwd=None):
    """Renvoie None (rien à signaler) ou (décision, règle, message)."""
    v = Verdicts()
    certain = analyze(command, Context(cwd), v)
    worst = v.worst()
    if worst and not certain and worst[0] == "deny":
        return ("ask", worst[1], worst[2] + " (découpage incertain de la commande : "
                                            "validation humaine)")
    return worst


def respond(decision, rule, message):
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": decision,
                                      "permissionDecisionReason":
                                          "[OPEN-161 {}] {}".format(rule, message)}},
              sys.stdout, ensure_ascii=False)


def main():
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command") or ""
        verdict = evaluate(command, data.get("cwd") or os.getcwd())
    except Exception as exc:  # jamais silencieux, jamais de blocage total
        verdict = ("ask", "R-ERREUR", "Garde-fou en erreur interne ({}) : validation humaine "
                                      "par précaution.".format(type(exc).__name__))
    if verdict:
        respond(*verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
