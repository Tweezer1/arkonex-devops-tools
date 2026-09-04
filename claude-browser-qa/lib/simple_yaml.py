"""Restricted YAML-subset parser for personas.yaml (OPEN-125, Issue #87).

Deliberately NOT a general YAML parser. Depends only on the Python standard library --
no PyYAML/js-yaml, so the whole claude-browser-qa/ toolchain has zero external runtime
dependency, which matters for "reproductible apres reconstruction de l'instance."

Supports exactly the shape personas.yaml uses:

    top_key:
      child_key:
        field: value
        field2: "quoted value"
        field3: true

Two levels of mapping under a single top-level key, scalar values only (str/bool/int),
comments starting with '#' (full-line or trailing), blank lines ignored. Indentation is
significant and must be consistent 2-space steps. Anything outside this shape raises
ValueError with a line number -- fail loud, never guess.
"""

from __future__ import annotations


def _strip_comment(line: str) -> str:
    in_quote = None
    out = []
    for ch in line:
        if in_quote:
            out.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _coerce_scalar(raw: str):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    if raw in ("true", "True", "TRUE"):
        return True
    if raw in ("false", "False", "FALSE"):
        return False
    if raw == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        return raw


def parse_restricted_yaml(text: str) -> dict:
    """Parse the exact two-level-mapping subset described in the module docstring."""
    lines = text.splitlines()

    root: dict = {}
    top_key = None
    top_map: dict = {}
    current_persona = None
    current_persona_map: dict = {}

    def flush_persona():
        nonlocal current_persona, current_persona_map
        if current_persona is not None:
            top_map[current_persona] = current_persona_map
        current_persona = None
        current_persona_map = {}

    for lineno, raw_line in enumerate(lines, start=1):
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            if not stripped.endswith(":"):
                raise ValueError(f"line {lineno}: expected top-level 'key:' , got: {raw_line!r}")
            if top_key is not None:
                raise ValueError(
                    f"line {lineno}: multiple top-level keys not supported by this "
                    f"restricted parser (found second key after {top_key!r})"
                )
            flush_persona()
            top_key = stripped[:-1].strip()
            continue

        if top_key is None:
            raise ValueError(f"line {lineno}: content before a top-level 'key:' line")

        if indent == 2:
            if not stripped.endswith(":"):
                raise ValueError(f"line {lineno}: expected persona 'name:' at indent 2, got: {raw_line!r}")
            flush_persona()
            current_persona = stripped[:-1].strip()
            continue

        if indent == 4:
            if current_persona is None:
                raise ValueError(f"line {lineno}: field at indent 4 without an open persona block")
            if ":" not in stripped:
                raise ValueError(f"line {lineno}: expected 'field: value' at indent 4, got: {raw_line!r}")
            field, _, value = stripped.partition(":")
            current_persona_map[field.strip()] = _coerce_scalar(value)
            continue

        raise ValueError(f"line {lineno}: unsupported indentation ({indent} spaces) for this restricted parser")

    flush_persona()
    if top_key is not None:
        root[top_key] = top_map
    return root


def load_personas(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = parse_restricted_yaml(fh.read())
    if "personas" not in data:
        raise ValueError(f"{path}: missing top-level 'personas:' key")
    return data["personas"]
