#!/usr/bin/env python3
"""Fake stdio MCP server for the lazy-proxy tests (OPEN-209). Appends one line to the file
named by FAKE_MCP_MARKER when it starts and when it stops; echoes tool calls."""
import json, os, sys

marker = os.environ["FAKE_MCP_MARKER"]
with open(marker, "a") as fh:
    fh.write("start\n")
for raw in sys.stdin:
    msg = json.loads(raw)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize" and os.environ.get("FAKE_MCP_MUTE_INIT"):
        continue  # simulates a server that never completes its handshake
    if method == "initialize":
        res = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        res = {"content": [{"type": "text", "text": "echo:" + json.dumps(msg["params"]["arguments"])}]}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
    sys.stdout.flush()
with open(marker, "a") as fh:
    fh.write("stop\n")
