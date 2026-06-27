# open83

Outillage associé au `83-kb-runbook — Contrat de contexte, ownership runtime et reprise IA`.

## Dossiers

- `docs/` : runbooks, contrats et notes de référence.
- `schemas/` : schémas JSON validables.
- `tools/` : scripts exécutables.
- `templates/` : exemples et gabarits.
- `tests/` : fixtures de validation.
- `packages/` : paquets de reprise locaux non versionnés.

## Artefacts prévus

- `schemas/resume_package.schema.v1.0.0.json`
- `tools/validate_resume_package.py`
- `templates/resume_package.example.json`
- `templates/resume_package.manifest.example.json`
- `templates/resume_package.lock.example.json`

## Règle

Le code exécutable va dans `tools/`.
Les contrats et explications vont dans `docs/`.
Les paquets réels de reprise restent hors Git par défaut.
