#!/usr/bin/env python3
"""Browser QA residue check (OPEN-209, Issue #369). READ-ONLY.

Lists the Playwright browsers still running and ties each one to its Claude Code session
and persona. Never stops, signals or changes any process: closing a browser is the job of
the campaign that opened it (`browser_close`), stopping a session is the owner's decision.

Default (end of a campaign): checks the session that runs this command.
    RESIDUE=PASS  no browser left under this session
    RESIDUE=FAIL  at least one browser still open under this session (listed)
A browser or MCP server whose session is gone (orphan) is reported as ORPHANS=WARN: it is
nobody's campaign any more and normally stops by itself within a minute (observed
2026-10-29); it does not fail the campaign check.

    check-browser-residue.py [--wait SECONDS]   # re-measure until PASS or timeout (default 30)
    check-browser-residue.py --all              # every session on the machine, informational, exit 0

Output names personas and session ids only: never a command line, cookie or path to a
storage-state's content. The session id is read from the single environment variable
CLAUDE_CODE_HOST_SESSION_ID of the session process; no other variable is read.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

STATE_RE = re.compile(r"--storage-state=\S*/([A-Za-z0-9_-]+)\.json")
SESSION_ID_VAR = b"CLAUDE_CODE_HOST_SESSION_ID="


class Proc:
    __slots__ = ("pid", "ppid", "comm", "argv", "start")

    def __init__(self, pid, ppid, comm, argv, start):
        self.pid, self.ppid, self.comm, self.argv, self.start = pid, ppid, comm, argv, start


def read_procs() -> dict[int, Proc]:
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat") as fh:
                stat = fh.read()
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        procs[pid] = Proc(pid, int(fields[1]), comm, argv, int(fields[19]))
    return procs


def is_session_root(p: Proc) -> bool:
    """A Claude Code session process (desktop app CLI or terminal `claude`)."""
    if not p.argv:
        return False
    exe = p.argv[0]
    return "/ccd-cli/" in exe or os.path.basename(exe) == "claude"


def is_browser_main(p: Proc) -> bool:
    # Chromium rewrites the argv of its helpers into one space-joined string.
    return p.comm == "chrome" and "--type=" not in " ".join(p.argv)


def is_mcp_server(p: Proc) -> bool:
    joined = " ".join(p.argv)
    return "@playwright/mcp" in joined or "playwright-mcp" in joined or "lazy-mcp-proxy.py" in joined


def ancestors(pid: int, procs: dict[int, Proc]):
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        yield procs[pid]
        pid = procs[pid].ppid


def persona_of(pid: int, procs: dict[int, Proc]) -> str:
    for p in ancestors(pid, procs):
        for a in p.argv:
            m = STATE_RE.search(a)
            if m:
                return m.group(1)
    return "?"


def session_of(pid: int, procs: dict[int, Proc]) -> Proc | None:
    for p in ancestors(pid, procs):
        if is_session_root(p):
            return p
    return None


def session_label(root: Proc | None) -> str:
    if root is None:
        return "aucune (orpheline)"
    try:
        with open(f"/proc/{root.pid}/environ", "rb") as fh:
            for var in fh.read().split(b"\0"):
                if var.startswith(SESSION_ID_VAR):
                    return f"{var[len(SESSION_ID_VAR):].decode(errors='replace')} (pid {root.pid})"
    except OSError:
        pass
    return f"pid {root.pid}"


def mem_mb(pids) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as fh:
                text = fh.read()
        except OSError:
            continue
        for key in ("Pss:", "Swap:"):
            m = re.search(rf"^{key}\s+(\d+)", text, re.M)
            if m:
                total += int(m.group(1))
    return total // 1024


def subtree(pid: int, procs: dict[int, Proc]) -> list[int]:
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    out, stack = [], [pid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(kids.get(cur, []))
    return out


def age_text(start_ticks: int) -> str:
    try:
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
    except OSError:
        return "?"
    seconds = int(uptime - start_ticks / os.sysconf("SC_CLK_TCK"))
    hours, rem = divmod(seconds, 3600)
    return f"{hours} h {rem // 60:02d} min" if hours else f"{rem // 60} min {rem % 60:02d} s"


def snapshot():
    procs = read_procs()
    browsers = [p for p in procs.values() if is_browser_main(p)]
    servers = [p for p in procs.values() if is_mcp_server(p)]
    return procs, browsers, servers


def own_session(procs: dict[int, Proc]) -> Proc | None:
    return session_of(os.getpid(), procs)


def describe_browser(b: Proc, procs) -> str:
    return (f"persona={persona_of(b.pid, procs)} pid={b.pid} age={age_text(b.start)} "
            f"memoire={mem_mb(subtree(b.pid, procs))} Mo session={session_label(session_of(b.pid, procs))}")


def report_all() -> int:
    procs, browsers, servers = snapshot()
    sessions: dict[int | None, dict] = {}
    for p in servers + browsers:
        root = session_of(p.pid, procs)
        key = root.pid if root else None
        entry = sessions.setdefault(key, {"root": root, "servers": set(), "browsers": []})
        if is_browser_main(p):
            entry["browsers"].append(p)
        else:
            entry["servers"].add(persona_of(p.pid, procs))
    print(f"=== BROWSERS (machine) ===\nBROWSERS_TOTAL={len(browsers)}")
    for key, entry in sorted(sessions.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        root = entry["root"]
        print(f"SESSION {session_label(root)}"
              + (f" age={age_text(root.start)}" if root else "")
              + f" personas_chargees={len(entry['servers'])} navigateurs={len(entry['browsers'])}")
        for b in entry["browsers"]:
            print(f"  BROWSER {describe_browser(b, procs)}")
    return 0


def check_own(wait_s: int) -> int:
    deadline = time.monotonic() + max(wait_s, 0)
    while True:
        procs, browsers, servers = snapshot()
        mine = own_session(procs)
        if mine is None:
            print("RESIDUE=FAIL (cette commande ne tourne pas dans une session Claude Code : utiliser --all)")
            return 2
        own = [b for b in browsers if session_of(b.pid, procs) is mine]
        orphans = [p for p in browsers + servers if session_of(p.pid, procs) is None]
        if not own and not orphans:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    print(f"=== RESIDUE (session {session_label(mine)}) ===")
    for b in own:
        print(f"BROWSER {describe_browser(b, procs)}")
    if orphans:
        names = sorted({persona_of(p.pid, procs) for p in orphans})
        print(f"ORPHANS=WARN ({len(orphans)} processus sans session : {', '.join(names)}) "
              "-- s'arrêtent normalement seuls en moins d'une minute ; à signaler s'ils persistent")
    else:
        print("ORPHANS=PASS")
    if own:
        print(f"RESIDUE=FAIL ({len(own)} navigateur(s) encore ouvert(s) dans cette session : browser_close sur chaque persona listée)")
        return 1
    print("RESIDUE=PASS (aucun navigateur ouvert par cette session)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="report every session (informational, exit 0)")
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for browsers to finish closing")
    args = parser.parse_args(argv)
    if args.all:
        return report_all()
    return check_own(args.wait)


if __name__ == "__main__":
    sys.exit(main())
