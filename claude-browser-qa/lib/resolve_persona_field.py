#!/usr/bin/env python3
"""Resolve a single field for a single persona from personas.yaml.

Used by refresh-persona.sh and verify-browser-qa.sh so that no shell script parses YAML
itself. Prints the raw scalar value to stdout (never a secret -- personas.yaml never
contains one, enforced by tests/test_no_secrets_in_artifacts.py). Exits 1 with a
deterministic message on stderr if the persona or field is unknown.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simple_yaml import load_personas  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personas", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--field", required=True)
    args = parser.parse_args(argv)

    personas = load_personas(args.personas)
    entry = personas.get(args.persona)
    if entry is None:
        print(f"resolve_persona_field: unknown persona '{args.persona}'", file=sys.stderr)
        return 1

    if args.field not in entry:
        print(
            f"resolve_persona_field: persona '{args.persona}' has no field '{args.field}'",
            file=sys.stderr,
        )
        return 1

    print(entry[args.field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
