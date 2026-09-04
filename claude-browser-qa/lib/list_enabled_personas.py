#!/usr/bin/env python3
"""Print the names of every persona with enabled: true in personas.yaml, one per line,
sorted. Used by bootstrap-browser-qa.sh and verify-browser-qa.sh so no shell script
parses YAML or hardcodes a persona list."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simple_yaml import load_personas  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personas", required=True)
    args = parser.parse_args(argv)

    personas = load_personas(args.personas)
    for name in sorted(personas):
        # Strict boolean only (U2 review finding F4) -- "enabled: yes" (a string) must
        # not be silently treated as active.
        if personas[name].get("enabled") is True:
            print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
