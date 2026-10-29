#!/usr/bin/env python3
"""Drive lazy-mcp-proxy.py like Claude Code does, against the fake server (OPEN-209).

usage: lazy_proxy_client.py <proxy> <cache> <marker> <protocol> -- <real command...>
Prints one line per step; the marker file tells whether the real server was running."""
import json, os, subprocess, sys

proxy, cache, marker, protocol = sys.argv[1:5]
real = sys.argv[sys.argv.index("--") + 1:]
p = subprocess.Popen([sys.executable, proxy, "--cache", cache, "--", *real],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                     env=dict(os.environ, FAKE_MCP_MARKER=marker))

def started():
    return open(marker).read().count("start") if os.path.exists(marker) else 0

def rpc(i, method, params):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    while True:
        msg = json.loads(p.stdout.readline())
        if msg.get("id") == i:
            return msg

r = rpc(1, "initialize", {"protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
print("initialize", r["result"]["protocolVersion"], "started=%d" % started())
p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); p.stdin.flush()
r = rpc(2, "tools/list", {})
print("tools/list", ",".join(t["name"] for t in r["result"]["tools"]), "started=%d" % started())
r = rpc(3, "tools/call", {"name": "echo", "arguments": {"x": 1}})
print("call1", r["result"]["content"][0]["text"], "started=%d" % started())
r = rpc(4, "tools/call", {"name": "echo", "arguments": {"x": 2}})
print("call2", r["result"]["content"][0]["text"], "started=%d" % started())
p.stdin.close(); p.wait(15)
print("exit", p.returncode, "stopped=%d" % open(marker).read().count("stop"))
