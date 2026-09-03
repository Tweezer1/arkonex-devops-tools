#!/usr/bin/env python3
"""Generate a .mcp.json candidate from personas.yaml (OPEN-125, Issue #87).

Zero external dependency (stdlib only + lib/simple_yaml.py). Pure function of its
inputs: never reads a credential, never touches the network, never writes outside the
single output path it is given.

A malformed or incomplete persona entry (any *enabled: true* persona missing a required
field) makes generation fail ENTIRELY -- no partial/half-written .mcp.json is ever
produced. This is deliberate (OPEN-125 contract, section "MCP GENERATION"): a broken
persona must never be presented as a working one.

Usage:
    generate-mcp-config.py --personas personas.yaml \
        --storage-state-dir /path/to/storage-states \
        --output /path/to/.mcp.json.candidate

Exit codes: 0 = written. 1 = validation error (nothing written). 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from simple_yaml import load_personas  # noqa: E402

MCP_PACKAGE_PINNED_VERSION = "0.0.80"  # OPEN-125 D5/D9 contract: never "@latest"

# Shared browser cache, deliberately NOT $HOME (browserqa-refresh has no home directory
# -- `useradd --system --no-create-home` -- and the MCP server / the refresher must use
# the exact SAME installed browser, proven by real launch: plain `chromium.launch()`
# without a channel needs browsers.json's "chromium" revision, installed via
# `npx playwright install chromium`, never the "chrome-for-testing" channel which is a
# DIFFERENT, unrelated download (canary-prep correction 2, runtime discovery).
DEFAULT_PLAYWRIGHT_BROWSERS_PATH = "/opt/arkonex-browser-qa/browsers"

REQUIRED_FIELDS = (
    "login_user_id",
    "credentials_file",
    "storage_state_file",
    "mcp_server_name",
)

COMMON_ARGS = (
    "-y",
    f"@playwright/mcp@{MCP_PACKAGE_PINNED_VERSION}",
    "--headless",
    "--no-sandbox",
    "--browser=chromium",
    "--isolated",
)


# Fields that must be unique across every *enabled* persona (U2 review finding F1/F2,
# 2026 — a collision here silently merges two personas' identity: two mcp_server_name
# entries would collapse into one dict key, or two distinct personas would share the
# very storageState file that is supposed to keep them isolated).
UNIQUE_ACROSS_PERSONAS_FIELDS = (
    "mcp_server_name",
    "storage_state_file",
    "credentials_file",
    "login_user_id",
)


def _is_unsafe_relative_path(value: str) -> bool:
    # credentials_file/storage_state_file must stay simple relative file names, anchored
    # under a base directory supplied at generation/refresh time (U2 review finding F3).
    if os.path.isabs(value):
        return True
    parts = value.replace("\\", "/").split("/")
    return any(part in ("..", "") for part in parts[:-1]) or ".." in parts


def _validate_persona(name: str, entry: dict) -> list[str]:
    errors = []
    if not isinstance(entry, dict):
        return [f"persona '{name}': not a mapping"]
    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"persona '{name}': field '{field}' is missing or empty")
    for field in ("credentials_file", "storage_state_file"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip() and _is_unsafe_relative_path(value):
            errors.append(
                f"persona '{name}': field '{field}' must be a simple relative file "
                f"name (no '..', no leading '/'), got: {value!r}"
            )
    return errors


def build_mcp_config(
    personas: dict, storage_state_dir: str, browsers_path: str = DEFAULT_PLAYWRIGHT_BROWSERS_PATH
) -> dict:
    errors: list[str] = []
    enabled_names: list[str] = []

    for name, entry in personas.items():
        # Strict boolean only (U2 review finding F4) -- "enabled: yes" (a string) must
        # not be silently treated as active.
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        enabled_names.append(name)
        errors.extend(_validate_persona(name, entry))

    if errors:
        raise ValueError(
            "generation aborted, 0 file written -- "
            + "; ".join(sorted(errors))
        )

    if not enabled_names:
        raise ValueError("generation aborted, 0 file written -- no persona with enabled: true")

    # Cross-persona uniqueness (U2 review finding F1/F2). Checked only among personas
    # that already individually validated above, so error messages here always refer to
    # well-formed entries.
    for field in UNIQUE_ACROSS_PERSONAS_FIELDS:
        seen: dict[str, str] = {}
        for name in enabled_names:
            value = personas[name][field]
            if value in seen:
                errors.append(
                    f"personas '{seen[value]}' and '{name}' both declare "
                    f"{field}={value!r} -- must be unique across enabled personas"
                )
            else:
                seen[value] = name

    if errors:
        raise ValueError(
            "generation aborted, 0 file written -- "
            + "; ".join(sorted(errors))
        )

    servers = {}
    for name in sorted(enabled_names):
        entry = personas[name]
        state_path = os.path.join(storage_state_dir, entry["storage_state_file"])
        servers[entry["mcp_server_name"]] = {
            "command": "npx",
            "args": [
                *COMMON_ARGS,
                f"--storage-state={state_path}",
            ],
            "env": {"PLAYWRIGHT_BROWSERS_PATH": browsers_path},
        }

    return {"mcpServers": servers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personas", required=True, help="path to personas.yaml")
    parser.add_argument("--storage-state-dir", required=True, help="base directory resolved storage-state paths are anchored to")
    parser.add_argument("--output", required=True, help="path to write the generated .mcp.json candidate")
    parser.add_argument(
        "--browsers-path",
        default=DEFAULT_PLAYWRIGHT_BROWSERS_PATH,
        help="shared PLAYWRIGHT_BROWSERS_PATH set on every generated server entry",
    )
    args = parser.parse_args(argv)

    try:
        personas = load_personas(args.personas)
        config = build_mcp_config(personas, args.storage_state_dir, args.browsers_path)
    except (ValueError, OSError) as exc:
        print(f"generate-mcp-config: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"

    # Assertions the contract explicitly requires never to appear in generated output.
    assert "@latest" not in rendered, "internal error: @latest leaked into generated config"
    assert "--secrets" not in rendered, "internal error: --secrets leaked into generated config"

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    print(f"generate-mcp-config: wrote {args.output} ({len(config['mcpServers'])} persona server(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
