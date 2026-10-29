#!/usr/bin/env python3
"""Lazy stdio proxy for one Playwright MCP persona server (OPEN-209, Issue #369).

Why: every Claude Code session on the bench starts every server listed in .mcp.json at
launch. With one server per persona, each session held ~2 GB (npm + sh + node per
persona, ~93 MB each, measured 2026-10-29) even when it never opened a browser.

What it does: answers `initialize`, `tools/list` and `ping` from a cache captured from
the real pinned server, and starts the real server (the command after `--`) only when
the session first needs it (any other request, typically `tools/call`). From then on it
forwards every message unchanged in both directions. When the session ends (stdin EOF)
it closes the real server's stdin, which is how Playwright MCP closes its browser, then
terminates it after a bounded wait.

Fail-safe: if the cache is missing, unreadable, built for other arguments, or the client
asks for another protocol version, the proxy starts the real server immediately and is a
plain pass-through (the behaviour before OPEN-209). It never answers a tool call itself.

Usage:
    lazy-mcp-proxy.py --cache <file> -- <real command> [args...]
    lazy-mcp-proxy.py --build-cache <file> -- <real command> [args...]
    lazy-mcp-proxy.py --check-cache <file> -- <real command> [args...]   (exit 0 = usable)

Stdlib only. Never reads a credential; the storage-state path stays an argument of the
real command, passed through untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading

CACHE_FORMAT = 2
# Versions Claude Code may request (2.1.308 requests the first). One cache entry each:
# the server's initialize answer depends on the version asked.
DEFAULT_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
INIT_ID = "open-209-lazy-init"
LIST_ID = "open-209-lazy-list"
CHILD_STOP_TIMEOUT_S = 10
INIT_TIMEOUT_S = float(os.environ.get("LAZY_MCP_INIT_TIMEOUT", "120"))


def args_key(command: list[str]) -> str:
    """Cache key: the real command with per-persona --storage-state removed.

    Every persona server differs only by its storage-state file, which does not change
    the tool list; anything else (package version, flags) invalidates the cache.
    """
    kept = [a for a in command if not a.startswith("--storage-state=")]
    return hashlib.sha256("\0".join(kept).encode()).hexdigest()


def load_cache(path: str, command: list[str]) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict) or cache.get("format") != CACHE_FORMAT:
        return None
    if cache.get("args_key") != args_key(command):
        return None
    entries = cache.get("by_protocol")
    if not isinstance(entries, dict) or not entries:
        return None
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("initialize"), dict) \
                or not isinstance(entry.get("tools"), dict):
            return None
    return entries


class Proxy:
    def __init__(self, command: list[str], entries: dict | None):
        self.command = command
        self.entries = entries
        self.cache: dict | None = None  # entry for the protocol version the client asked
        self.out_lock = threading.Lock()
        self.child_lock = threading.Lock()
        self.child: subprocess.Popen | None = None
        self.client_init: dict | None = None
        self.init_done = threading.Event()

    # -- output -----------------------------------------------------------------
    def send_client(self, msg: dict) -> None:
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        with self.out_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def send_child(self, msg: dict) -> None:
        assert self.child is not None and self.child.stdin is not None
        self.child.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        self.child.stdin.flush()

    # -- child ------------------------------------------------------------------
    def start_child(self, handshake: bool) -> None:
        """Start the real server. With handshake, replay the client's initialize."""
        with self.child_lock:
            if self.child is not None and self.child.poll() is None:
                return
            self.init_done.clear()
            # Own process group: npx runs npm -> sh -> node, and a forced stop must reach
            # the whole chain, not npm alone.
            self.child = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                start_new_session=True,
            )
            threading.Thread(target=self.pump_child, args=(self.child, handshake), daemon=True).start()
            if handshake:
                self.send_child({"jsonrpc": "2.0", "id": INIT_ID, "method": "initialize",
                                 "params": self.client_init["params"]})
                if not self.init_done.wait(INIT_TIMEOUT_S) or self.child.poll() is not None:
                    self.stop_child()  # never keep a server that missed its handshake
                    self.child = None
                    raise RuntimeError("real server did not answer initialize")
                self.send_child({"jsonrpc": "2.0", "method": "notifications/initialized"})
            else:
                self.init_done.set()

    def pump_child(self, child: subprocess.Popen, handshake: bool) -> None:
        assert child.stdout is not None
        for raw in child.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if handshake and isinstance(msg, dict) and msg.get("id") == INIT_ID:
                self.init_done.set()
                continue
            self.send_client(msg)
        self.init_done.set()

    def stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            child.stdin.close()
        except OSError:
            pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                try:
                    os.killpg(child.pid, sig)
                except OSError:
                    pass
            try:
                child.wait(CHILD_STOP_TIMEOUT_S)
                return
            except subprocess.TimeoutExpired:
                continue

    # -- client -----------------------------------------------------------------
    def error(self, req_id, text: str) -> None:
        self.send_client({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": text}})

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        live = self.child is not None and self.child.poll() is None

        if method == "initialize" and not live:
            self.client_init = msg
            wanted = (msg.get("params") or {}).get("protocolVersion")
            self.cache = (self.entries or {}).get(wanted)
            if self.cache is None:
                print(f"lazy-mcp-proxy: no cache for protocol {wanted!r}, pass-through", file=sys.stderr)
                self.start_child(handshake=False)
                self.send_child(msg)
                return
            self.send_client({"jsonrpc": "2.0", "id": msg["id"], "result": self.cache["initialize"]})
            return

        if self.cache is not None and not live:
            if method == "notifications/initialized" or (method or "").startswith("notifications/"):
                return  # nothing to tell a server that does not run yet
            if method == "tools/list":
                self.send_client({"jsonrpc": "2.0", "id": msg["id"], "result": self.cache["tools"]})
                return
            if method == "ping":
                self.send_client({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                return
            if method is None:
                return  # a response for a server that does not run: drop it
            try:
                self.start_child(handshake=True)
            except (OSError, RuntimeError) as exc:
                if "id" in msg:
                    self.error(msg["id"], f"lazy-mcp-proxy: could not start the real server: {exc}")
                return

        if self.child is None or self.child.poll() is not None:
            if "id" in msg and method is not None:
                self.error(msg["id"], "lazy-mcp-proxy: the real server has stopped")
            return
        self.send_child(msg)

    def run(self) -> int:
        if self.entries is None:
            print("lazy-mcp-proxy: no usable cache, pass-through", file=sys.stderr)
            self.start_child(handshake=False)
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    try:
                        self.handle(msg)
                    except (BrokenPipeError, OSError) as exc:
                        if "id" in msg and msg.get("method") is not None:
                            self.error(msg["id"], f"lazy-mcp-proxy: {exc}")
        finally:
            self.stop_child()
        return 0


def query_server(command: list[str], protocol_version: str) -> dict | None:
    """Run the real server once; return its initialize and tools/list answers."""
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    answers: dict = {}

    def ask(req: dict) -> None:
        child.stdin.write((json.dumps(req) + "\n").encode())
        child.stdin.flush()
        for raw in child.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == req["id"]:
                answers[req["id"]] = msg
                return

    try:
        ask({"jsonrpc": "2.0", "id": INIT_ID, "method": "initialize",
             "params": {"protocolVersion": protocol_version, "capabilities": {},
                        "clientInfo": {"name": "lazy-mcp-proxy-cache", "version": "1"}}})
        child.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        child.stdin.flush()
        ask({"jsonrpc": "2.0", "id": LIST_ID, "method": "tools/list", "params": {}})
    finally:
        child.stdin.close()
        try:
            child.wait(CHILD_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()

    init = answers.get(INIT_ID, {}).get("result")
    tools = answers.get(LIST_ID, {}).get("result")
    if not isinstance(init, dict) or not isinstance(tools, dict) or not tools.get("tools"):
        return None
    if "nextCursor" in tools:
        return None  # paginated tool list: not supported, stay eager
    return {"initialize": init, "tools": tools}


def build_cache(path: str, command: list[str], versions: list[str]) -> int:
    entries = {}
    for version in versions:
        entry = query_server(command, version)
        if entry is None:
            print(f"lazy-mcp-proxy: cache NOT written (no usable answer for protocol {version})", file=sys.stderr)
            return 1
        entries[version] = entry
    cache = {"format": CACHE_FORMAT, "args_key": args_key(command), "by_protocol": entries}
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    summary = ", ".join(f"{v}: {len(e['tools']['tools'])} tools" for v, e in entries.items())
    print(f"lazy-mcp-proxy: cache written to {path} ({summary})")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: lazy-mcp-proxy.py (--cache F | --check-cache F | --build-cache F [--protocol-version V]...) -- CMD ...", file=sys.stderr)
        return 2
    sep = argv.index("--")
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--cache")
    group.add_argument("--build-cache")
    group.add_argument("--check-cache")
    parser.add_argument("--protocol-version", action="append",
                        help="protocol version to cache (repeatable); default: %s" % ", ".join(DEFAULT_PROTOCOL_VERSIONS))
    opts = parser.parse_args(argv[:sep])
    command = argv[sep + 1:]
    if not command:
        print("lazy-mcp-proxy: missing real command after --", file=sys.stderr)
        return 2
    if opts.check_cache:
        entries = load_cache(opts.check_cache, command)
        if entries is None:
            print(f"lazy-mcp-proxy: cache {opts.check_cache} missing or built for other arguments")
            return 1
        print(f"lazy-mcp-proxy: cache usable (protocols: {', '.join(sorted(entries))})")
        return 0
    if opts.build_cache:
        return build_cache(opts.build_cache, command, opts.protocol_version or list(DEFAULT_PROTOCOL_VERSIONS))
    return Proxy(command, load_cache(opts.cache, command)).run()


if __name__ == "__main__":
    raise SystemExit(main())
