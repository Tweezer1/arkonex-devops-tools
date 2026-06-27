# Open83 validator fixtures

Fixtures for `validate_resume_package.py`.

Expected results:

- `valid` → `decision=GO`, return code `0`.
- `missing_file` → `decision=NO_GO`, return code `10`, `missing_required_files=1`.
- `secret_detected` → `decision=NO_GO`, return code `10`, `secret_scan_failures=1`. The secret-like value is intentionally fake test data.
- `hash_conflict` → `decision=STOP_CONFLICT`, return code `20`.

Generated outputs should stay out of Git except the intentional fixture lock:

- `open83/tests/hash_conflict/resume_package.lock.json`

Do not use these fixtures as operational OPEN evidence.
