#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026-06-27 — OPEN-83-F3 — validate_resume_package.py — v1.0.0 (FR)

Validator non destructif du paquet de reprise machine-readable OPEN-83-F.

Contrat v1.0.0 :
- Lire resume_package.json.
- Valider contre resume_package.schema.v1.0.0.json.
- Vérifier les preuves référencées dans evidence.items[].ref.
- Calculer SHA-256 des preuves physiques.
- Scanner les secrets probables.
- Calculer canonical_key + canonical_hash.
- Vérifier / créer resume_package.lock.json.
- Générer resume_package.manifest.json.
- Générer validation_report.json.
- Retourner GO / NO-GO / STOP_CONFLICT / INTERNAL_ERROR.

Règle centrale : le script ne modifie jamais les preuves source.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import jsonschema
    from jsonschema import Draft202012Validator, FormatChecker
except Exception:  # pragma: no cover - handled at runtime
    jsonschema = None
    Draft202012Validator = None
    FormatChecker = None

VERSION = "1.0.0"
DOCUMENT = "2026-06-27 — OPEN-83-F3 — validate_resume_package.py — v1.0.0"

RC_GO = 0
RC_NO_GO = 10
RC_STOP_CONFLICT = 20
RC_INTERNAL_ERROR = 30

DECISION_GO = "GO"
DECISION_NO_GO = "NO_GO"
DECISION_STOP_CONFLICT = "STOP_CONFLICT"
DECISION_INTERNAL_ERROR = "INTERNAL_ERROR"

VOLATILE_KEYS = {
    "generated_at",
    "validated_at",
    "created_at",
    "locked_at",
    "validator_notes",
    "mtime",
    "absolute_path",
}

SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*([^\s'\"]+)"
)
AUTH_BEARER_RE = re.compile(r"(?i)Authorization\s*:\s*Bearer\s+([A-Za-z0-9._\-+/=]{12,})")
AWS_SECRET_RE = re.compile(r"(?i)AWS_SECRET_ACCESS_KEY\s*[:=]\s*([A-Za-z0-9/+=]{20,})")
PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
COOKIE_RE = re.compile(r"(?i)\b(cookie|sid|sessionid|session_id)\b\s*[:=]\s*([^\s'\"]{12,})")

PLACEHOLDER_RE = re.compile(r"^\{\{[A-Z0-9_./:-]+\}\}$")

WRITE_MODES = {"canary_patch", "controlled_execution", "rollback", "repair", "closure"}


@dataclasses.dataclass
class Check:
    id: str
    severity: str
    status: str
    message: str
    evidence_id: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        return {k: v for k, v in data.items() if v is not None}


@dataclasses.dataclass
class FileResult:
    id: str
    ref: str
    path: str
    required: bool
    exists: bool
    status: str
    size_bytes: int = 0
    sha256: Optional[str] = None
    non_empty: bool = False
    secret_scan_status: str = "not_run"
    secret_findings_count: int = 0
    error: Optional[str] = None

    def to_manifest_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "required": self.required,
            "exists": self.exists,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "non_empty": self.non_empty,
            "secret_scan_status": self.secret_scan_status,
        }
        if self.secret_findings_count:
            data["secret_findings_count"] = self.secret_findings_count
        if self.error:
            data["error"] = self.error
        return data


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Dict[str, Any], no_write: bool) -> None:
    if no_write:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_json(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.match(value.strip()))


def scrub_volatile(obj: Any) -> Any:
    """Return a deep copy without volatile keys used only for audit timestamps/notes."""
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in obj.items():
            if key in VOLATILE_KEYS:
                continue
            cleaned[key] = scrub_volatile(value)
        return cleaned
    if isinstance(obj, list):
        return [scrub_volatile(item) for item in obj]
    return obj


def build_canonical_key(package: Dict[str, Any]) -> str:
    ctx = package.get("work_context", {})
    return "|".join(
        [
            "resume_package",
            str(ctx.get("open_id", "UNKNOWN")),
            str(ctx.get("lot_id", "UNKNOWN")),
            str(package.get("profile", "generic")),
            str(ctx.get("environment", "unknown")),
            str(package.get("document_version", "v0.0.0")),
        ]
    )


def build_canonical_payload(package: Dict[str, Any], file_results: List[FileResult]) -> Dict[str, Any]:
    """Build canonical payload. Includes physical evidence hashes so changed proof files change the package hash."""
    cleaned = scrub_volatile(copy.deepcopy(package))
    evidence_integrity = []
    for result in sorted(file_results, key=lambda x: x.id):
        evidence_integrity.append(
            {
                "id": result.id,
                "ref": result.ref,
                "required": result.required,
                "exists": result.exists,
                "size_bytes": result.size_bytes,
                "sha256": result.sha256,
                "non_empty": result.non_empty,
                "status": result.status,
                "secret_scan_status": result.secret_scan_status,
            }
        )
    cleaned["_computed_evidence_integrity"] = evidence_integrity
    return cleaned


def validate_schema(package: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[bool, List[str]]:
    if jsonschema is None or Draft202012Validator is None:
        return False, ["Python package 'jsonschema' is required but not installed."]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(package), key=lambda e: list(e.path))
    if not errors:
        return True, []
    messages = []
    for error in errors:
        path = "/" + "/".join(str(p) for p in error.path) if error.path else "/"
        messages.append(f"{path}: {error.message}")
    return False, messages


def resolve_inside_root(root_dir: Path, ref: str) -> Tuple[Optional[Path], Optional[str]]:
    if ref.startswith("http://") or ref.startswith("https://"):
        return None, "remote_ref_not_supported"
    if ref.startswith("{{") and ref.endswith("}}"):
        return None, "placeholder_ref"

    raw = Path(ref)
    candidate = raw if raw.is_absolute() else root_dir / raw
    try:
        root_real = root_dir.resolve(strict=False)
        cand_real = candidate.resolve(strict=False)
        if os.path.commonpath([str(root_real), str(cand_real)]) != str(root_real):
            return cand_real, "path_outside_root"
        return cand_real, None
    except Exception as exc:
        return None, f"path_resolution_error:{exc}"


def scan_text_for_secrets(text: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []

    def add(pattern_id: str, match_text: str) -> None:
        findings.append({"pattern": pattern_id, "sample": match_text[:120]})

    for match in SECRET_KEY_VALUE_RE.finditer(text):
        value = match.group(2).strip().strip("'\"")
        if not is_placeholder(value):
            add("key_value_secret", match.group(0))

    for regex, pattern_id in [
        (AUTH_BEARER_RE, "authorization_bearer"),
        (AWS_SECRET_RE, "aws_secret_access_key"),
        (COOKIE_RE, "cookie_or_session"),
    ]:
        for match in regex.finditer(text):
            value = match.group(1 if pattern_id != "cookie_or_session" else 2).strip().strip("'\"")
            if not is_placeholder(value):
                add(pattern_id, match.group(0))

    if PEM_PRIVATE_KEY_RE.search(text):
        add("pem_private_key", "-----BEGIN PRIVATE KEY-----")

    return findings


def scan_bytes_for_secrets(content: bytes) -> List[Dict[str, str]]:
    text = content.decode("utf-8", errors="ignore")
    return scan_text_for_secrets(text)


def collect_evidence_id_references(obj: Any) -> List[str]:
    refs: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "evidence_ids" and isinstance(value, list):
                refs.extend(str(v) for v in value)
            else:
                refs.extend(collect_evidence_id_references(value))
    elif isinstance(obj, list):
        for item in obj:
            refs.extend(collect_evidence_id_references(item))
    return refs


def build_file_results(package: Dict[str, Any], root_dir: Path, secret_scan: bool) -> Tuple[List[FileResult], List[Check]]:
    checks: List[Check] = []
    results: List[FileResult] = []
    evidence = package.get("evidence", {}) or {}
    items = evidence.get("items", []) or []

    final_ref = evidence.get("final_essential_output_ref")
    final_ref_present_in_items = False

    for item in items:
        ev_id = str(item.get("id", "UNKNOWN"))
        ref = str(item.get("ref", ""))
        required = True
        if final_ref and ref == final_ref:
            final_ref_present_in_items = True

        resolved, error = resolve_inside_root(root_dir, ref)
        if error:
            status = "path_outside_root" if error == "path_outside_root" else "missing_required"
            results.append(
                FileResult(
                    id=ev_id,
                    ref=ref,
                    path=ref,
                    required=required,
                    exists=False,
                    status=status,
                    error=error,
                )
            )
            checks.append(Check(f"FILE-{ev_id}", "blocking", "failed", f"Evidence ref invalid: {error}", ev_id))
            continue

        assert resolved is not None
        display_path = str(resolved.relative_to(root_dir.resolve(strict=False))) if resolved.exists() else ref
        if not resolved.exists():
            results.append(
                FileResult(
                    id=ev_id,
                    ref=ref,
                    path=display_path,
                    required=required,
                    exists=False,
                    status="missing_required",
                )
            )
            checks.append(Check(f"FILE-{ev_id}", "blocking", "failed", f"Required evidence file missing: {ref}", ev_id))
            continue

        if not resolved.is_file():
            results.append(
                FileResult(
                    id=ev_id,
                    ref=ref,
                    path=display_path,
                    required=required,
                    exists=True,
                    status="read_error",
                    error="ref_is_not_a_file",
                )
            )
            checks.append(Check(f"FILE-{ev_id}", "blocking", "failed", f"Evidence ref is not a file: {ref}", ev_id))
            continue

        try:
            content = resolved.read_bytes()
            size = len(content)
            file_hash = sha256_bytes(content)
            non_empty = size > 0
            findings = scan_bytes_for_secrets(content) if secret_scan else []
            if not non_empty:
                status = "empty_required"
                severity = "blocking"
                check_status = "failed"
                message = f"Required evidence file is empty: {ref}"
            elif findings:
                status = "secret_suspected"
                severity = "critical"
                check_status = "failed"
                message = f"Secret suspected in evidence file: {ref}"
            else:
                status = "present"
                severity = "info"
                check_status = "passed"
                message = f"Evidence file present: {ref}"
            results.append(
                FileResult(
                    id=ev_id,
                    ref=ref,
                    path=display_path,
                    required=required,
                    exists=True,
                    status=status,
                    size_bytes=size,
                    sha256=file_hash,
                    non_empty=non_empty,
                    secret_scan_status="failed" if findings else ("passed" if secret_scan else "not_run"),
                    secret_findings_count=len(findings),
                )
            )
            checks.append(Check(f"FILE-{ev_id}", severity, check_status, message, ev_id))
        except Exception as exc:
            results.append(
                FileResult(
                    id=ev_id,
                    ref=ref,
                    path=display_path,
                    required=required,
                    exists=True,
                    status="read_error",
                    error=str(exc),
                )
            )
            checks.append(Check(f"FILE-{ev_id}", "blocking", "failed", f"Cannot read evidence file {ref}: {exc}", ev_id))

    if final_ref and not final_ref_present_in_items:
        resolved, error = resolve_inside_root(root_dir, str(final_ref))
        if error or resolved is None or not resolved.exists() or not resolved.is_file():
            checks.append(Check("FILE-FINAL_ESSENTIAL_OUTPUT", "blocking", "failed", "final_essential_output_ref is not present as a valid evidence file."))
            results.append(
                FileResult(
                    id="FINAL_ESSENTIAL_OUTPUT",
                    ref=str(final_ref),
                    path=str(final_ref),
                    required=True,
                    exists=False,
                    status="missing_required",
                    error=error,
                )
            )

    evidence_ids_declared = {str(item.get("id")) for item in items}
    referenced_ids = set(collect_evidence_id_references(package))
    missing_refs = sorted(ref for ref in referenced_ids if ref and ref not in evidence_ids_declared)
    for ref in missing_refs:
        checks.append(Check("EVIDENCE-ID-REF", "blocking", "failed", f"Referenced evidence_id is absent from evidence.items[]: {ref}"))

    return results, checks


def package_secret_scan(package: Dict[str, Any]) -> Tuple[bool, List[str]]:
    text = json.dumps(package, ensure_ascii=False, sort_keys=True)
    findings = scan_text_for_secrets(text)
    return (len(findings) == 0, [f"{f['pattern']}: {f['sample']}" for f in findings])


def profile_specific_checks(package: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    profile = package.get("profile")
    next_mode = ((package.get("next_action") or {}).get("mode") or "")

    if profile == "erpnext_frappe":
        erp = package.get("erpnext_frappe") or {}
        if next_mode in WRITE_MODES:
            if erp.get("meta_verified") is not True:
                checks.append(Check("ERP-META-VERIFIED", "blocking", "failed", "meta_verified must be true before ERPNext/Frappe write modes."))
            if erp.get("fieldnames_verified") is not True:
                checks.append(Check("ERP-FIELDNAMES-VERIFIED", "blocking", "failed", "fieldnames_verified must be true before ERPNext/Frappe write modes."))
        for key in ["rb15_dependency_check", "rb25_invariant_check", "rb82_determinism_check"]:
            status = ((erp.get(key) or {}).get("status") or "unknown")
            if status == "failed":
                checks.append(Check(f"ERP-{key.upper()}", "blocking", "failed", f"{key}.status=failed"))

    if profile == "n8n":
        n8n = package.get("n8n") or {}
        if n8n.get("workflow_active_expected") is True and next_mode in {"controlled_execution", "closure"}:
            checks.append(Check("N8N-ACTIVE-PROD-GUARD", "warning", "warning", "workflow_active_expected=true; ensure explicit human GO before production activation."))

    return checks


def validation_field_checks(package: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    validation = package.get("validation") or {}
    expected_true = ["no_secret_plaintext", "required_refs_present", "resume_ready"]
    for key in expected_true:
        if validation.get(key) is not True:
            checks.append(Check(f"VALIDATION-{key.upper()}", "blocking", "failed", f"validation.{key} must be true for GO."))
    return checks


def summarize_manifest(package: Dict[str, Any], root_dir: Path, canonical_key: str, canonical_hash: str, file_results: List[FileResult], decision: str) -> Dict[str, Any]:
    files_json = [r.to_manifest_json() for r in file_results]
    required_total = sum(1 for r in file_results if r.required)
    required_present = sum(1 for r in file_results if r.required and r.exists and r.status == "present")
    missing_required = sum(1 for r in file_results if r.status == "missing_required")
    empty_required = sum(1 for r in file_results if r.status == "empty_required")
    hash_mismatches = sum(1 for r in file_results if r.status == "hash_mismatch")
    secret_failures = sum(1 for r in file_results if r.status == "secret_suspected")
    read_errors = sum(1 for r in file_results if r.status == "read_error")
    path_errors = sum(1 for r in file_results if r.status == "path_outside_root")

    return {
        "manifest_version": VERSION,
        "package_id": package.get("package_id"),
        "canonical_key": canonical_key,
        "canonical_hash": canonical_hash,
        "generated_at": utc_now_iso(),
        "root_dir": str(root_dir),
        "files": files_json,
        "summary": {
            "files_total": len(file_results),
            "required_files_total": required_total,
            "required_files_present": required_present,
            "missing_required_files": missing_required,
            "empty_required_files": empty_required,
            "hash_mismatches": hash_mismatches,
            "secret_scan_failures": secret_failures,
            "read_errors": read_errors,
            "path_errors": path_errors,
        },
        "manifest_go_no_go": "GO" if decision == DECISION_GO else "NO-GO",
    }


def read_lock(lock_path: Path) -> Optional[Dict[str, Any]]:
    if not lock_path.exists():
        return None
    return load_json(lock_path)


def evaluate_lock(lock: Optional[Dict[str, Any]], canonical_key: str, canonical_hash: str) -> Tuple[str, List[Check]]:
    checks: List[Check] = []
    if lock is None:
        checks.append(Check("LOCK-ABSENT", "info", "passed", "No existing lock; lock can be created if validation is GO."))
        return "create", checks

    lock_key = lock.get("canonical_key")
    lock_hash = lock.get("canonical_hash")
    if lock_key == canonical_key and lock_hash == canonical_hash:
        checks.append(Check("LOCK-MATCH", "info", "passed", "Existing lock matches canonical_key and canonical_hash."))
        return "reuse", checks
    if lock_key == canonical_key and lock_hash != canonical_hash:
        checks.append(Check("LOCK-CONFLICT", "critical", "failed", "Existing lock has same canonical_key but different canonical_hash."))
        return "conflict", checks

    checks.append(Check("LOCK-SCOPE-MISMATCH", "blocking", "failed", "Existing lock canonical_key differs from current package canonical_key."))
    return "scope_mismatch", checks


def build_lock(package: Dict[str, Any], canonical_key: str, canonical_hash: str) -> Dict[str, Any]:
    return {
        "lock_version": VERSION,
        "package_id": package.get("package_id"),
        "canonical_key": canonical_key,
        "canonical_hash": canonical_hash,
        "locked_at": utc_now_iso(),
        "lock_state": "SOFT_LOCKED",
        "rerun_policy": "REUSE_IF_HASH_MATCH_STOP_IF_DIFFERS",
        "supersedes": None,
        "superseded_by": None,
    }


def decide(checks: List[Check], lock_status: str, package: Dict[str, Any]) -> str:
    if lock_status == "conflict":
        return DECISION_STOP_CONFLICT
    if any(c.severity in {"blocking", "critical"} and c.status == "failed" for c in checks):
        return DECISION_NO_GO
    if package.get("validation", {}).get("resume_ready") is not True:
        return DECISION_NO_GO
    return DECISION_GO


def build_validation_report(
    package: Dict[str, Any],
    schema_ok: bool,
    schema_errors: List[str],
    manifest: Dict[str, Any],
    lock_status: str,
    canonical_key: str,
    canonical_hash: str,
    checks: List[Check],
    decision: str,
) -> Dict[str, Any]:
    warnings = [c.to_json() for c in checks if c.severity == "warning"]
    errors = [c.to_json() for c in checks if c.severity in {"blocking", "critical"} and c.status == "failed"]
    return {
        "report_version": VERSION,
        "validator_version": VERSION,
        "package_id": package.get("package_id"),
        "validated_at": utc_now_iso(),
        "schema_validation": {
            "status": "passed" if schema_ok else "failed",
            "errors": schema_errors,
        },
        "manifest_validation": {
            "status": "passed" if manifest.get("manifest_go_no_go") == "GO" else "failed",
            "summary": manifest.get("summary", {}),
        },
        "lock_validation": {
            "status": "passed" if lock_status in {"create", "reuse"} else "failed",
            "decision": lock_status,
        },
        "secret_scan": {
            "status": "passed" if not any("SECRET" in c.id or "secret" in c.message.lower() for c in checks if c.status == "failed") else "failed"
        },
        "canonical": {
            "canonical_key": canonical_key,
            "canonical_hash": canonical_hash,
        },
        "checks": [c.to_json() for c in checks],
        "warnings": warnings,
        "errors": errors,
        "decision": decision,
        "next_action": next_action_message(decision),
    }


def next_action_message(decision: str) -> str:
    if decision == DECISION_GO:
        return "Package is resumable. Continue with next_action from resume_package.json."
    if decision == DECISION_STOP_CONFLICT:
        return "Stop. Same canonical_key has a different canonical_hash. Choose supersede, new package, or corrected package."
    if decision == DECISION_NO_GO:
        return "Fix blocking validation errors, then rerun validator."
    return "Internal error. Inspect validator logs/output."


def print_essential_output(report: Dict[str, Any], manifest: Optional[Dict[str, Any]]) -> None:
    decision = report.get("decision", DECISION_INTERNAL_ERROR)
    canonical = report.get("canonical", {})
    summary = (manifest or {}).get("summary", {})
    print("===== VALIDATOR ESSENTIAL OUTPUT =====")
    print(f"validator={DOCUMENT}")
    print(f"validator_version={VERSION}")
    print(f"package_id={report.get('package_id')}")
    print(f"decision={decision}")
    print(f"canonical_key={canonical.get('canonical_key')}")
    print(f"canonical_hash={canonical.get('canonical_hash')}")
    print(f"required_files_total={summary.get('required_files_total')}")
    print(f"required_files_present={summary.get('required_files_present')}")
    print(f"missing_required_files={summary.get('missing_required_files')}")
    print(f"empty_required_files={summary.get('empty_required_files')}")
    print(f"secret_scan_failures={summary.get('secret_scan_failures')}")
    print(f"errors={len(report.get('errors', []))}")
    print(f"warnings={len(report.get('warnings', []))}")
    print(f"next_action={report.get('next_action')}")


def run(args: argparse.Namespace) -> int:
    package_path = Path(args.package).expanduser()
    schema_path = Path(args.schema).expanduser()
    root_dir = Path(args.root_dir).expanduser().resolve(strict=False)
    out_dir = Path(args.out_dir).expanduser().resolve(strict=False) if args.out_dir else root_dir

    checks: List[Check] = []
    manifest: Dict[str, Any] = {}

    package = load_json(package_path)
    schema = load_json(schema_path)

    if args.profile and args.profile != package.get("profile"):
        checks.append(Check("PROFILE-OVERRIDE", "warning", "warning", f"CLI --profile={args.profile} differs from package profile={package.get('profile')}; no mutation performed."))

    schema_ok, schema_errors = validate_schema(package, schema)
    if schema_ok:
        checks.append(Check("SCHEMA", "info", "passed", "JSON Schema validation passed."))
    else:
        checks.append(Check("SCHEMA", "blocking", "failed", "JSON Schema validation failed."))
        for idx, error in enumerate(schema_errors, start=1):
            checks.append(Check(f"SCHEMA-ERROR-{idx}", "blocking", "failed", error))

    package_secret_ok, package_secret_findings = package_secret_scan(package) if args.secret_scan else (True, [])
    if package_secret_ok:
        checks.append(Check("SECRET-PACKAGE", "info", "passed", "No probable plaintext secret found in resume_package.json."))
    else:
        checks.append(Check("SECRET-PACKAGE", "critical", "failed", "Probable plaintext secret found in resume_package.json."))
        for idx, finding in enumerate(package_secret_findings, start=1):
            checks.append(Check(f"SECRET-PACKAGE-{idx}", "critical", "failed", finding))

    file_results, file_checks = build_file_results(package, root_dir, args.secret_scan)
    checks.extend(file_checks)
    checks.extend(validation_field_checks(package))
    checks.extend(profile_specific_checks(package))

    canonical_key = build_canonical_key(package)
    canonical_payload = build_canonical_payload(package, file_results)
    canonical_hash = sha256_json(canonical_payload)

    lock_path = out_dir / "resume_package.lock.json"
    try:
        lock = read_lock(lock_path)
        lock_status, lock_checks = evaluate_lock(lock, canonical_key, canonical_hash)
    except Exception as exc:
        lock_status = "error"
        lock_checks = [Check("LOCK-READ", "blocking", "failed", f"Cannot read lock: {exc}")]
    checks.extend(lock_checks)

    preliminary_decision = decide(checks, lock_status, package)
    manifest = summarize_manifest(package, root_dir, canonical_key, canonical_hash, file_results, preliminary_decision)
    final_decision = decide(checks, lock_status, package)
    manifest["manifest_go_no_go"] = "GO" if final_decision == DECISION_GO else "NO-GO"

    report = build_validation_report(
        package=package,
        schema_ok=schema_ok,
        schema_errors=schema_errors,
        manifest=manifest,
        lock_status=lock_status,
        canonical_key=canonical_key,
        canonical_hash=canonical_hash,
        checks=checks,
        decision=final_decision,
    )

    if not args.canonical_only:
        write_json(out_dir / "resume_package.manifest.json", manifest, args.no_write)
        write_json(out_dir / "validation_report.json", report, args.no_write)
        if final_decision == DECISION_GO and lock_status == "create":
            write_json(lock_path, build_lock(package, canonical_key, canonical_hash), args.no_write)

    print_essential_output(report, manifest)

    if final_decision == DECISION_GO:
        return RC_GO
    if final_decision == DECISION_STOP_CONFLICT:
        return RC_STOP_CONFLICT
    if final_decision == DECISION_NO_GO:
        return RC_NO_GO
    return RC_INTERNAL_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_resume_package.py",
        description="Validator non destructif du paquet de reprise OPEN-83-F v1.0.0.",
    )
    parser.add_argument("--package", required=True, help="Chemin vers resume_package.json")
    parser.add_argument("--schema", required=True, help="Chemin vers resume_package.schema.v1.0.0.json")
    parser.add_argument("--root-dir", required=True, help="Racine du paquet de reprise")
    parser.add_argument("--out-dir", default=None, help="Dossier de sortie; défaut: root-dir")
    parser.add_argument("--profile", default=None, help="Profil attendu en lecture seulement; ne modifie pas le paquet")
    parser.add_argument("--strict", action="store_true", help="Réservé v1.1.0; v1.0.0 applique déjà les blocages critiques")
    parser.add_argument("--no-write", action="store_true", help="Dry-run : ne génère pas manifest/lock/report")
    parser.add_argument("--allow-missing-optional", action="store_true", help="Réservé v1.1.0; evidence.items[] est requis par défaut")
    parser.add_argument("--no-secret-scan", dest="secret_scan", action="store_false", help="Désactive le scan secrets; déconseillé")
    parser.add_argument("--canonical-only", action="store_true", help="Calcule canonical_key/hash et rapport console sans écrire les artefacts")
    parser.set_defaults(secret_scan=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("VALIDATOR_INTERRUPTED", file=sys.stderr)
        return RC_INTERNAL_ERROR
    except Exception as exc:
        report = {
            "report_version": VERSION,
            "validator_version": VERSION,
            "package_id": None,
            "validated_at": utc_now_iso(),
            "decision": DECISION_INTERNAL_ERROR,
            "errors": [
                {
                    "id": "INTERNAL-ERROR",
                    "severity": "critical",
                    "status": "failed",
                    "message": str(exc),
                }
            ],
            "warnings": [],
            "canonical": {},
            "next_action": "Inspect validator error and rerun after correction.",
        }
        print_essential_output(report, None)
        print(f"INTERNAL_ERROR: {exc}", file=sys.stderr)
        return RC_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
