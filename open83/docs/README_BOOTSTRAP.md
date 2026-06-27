# OPEN-83 Bootstrap v1.0.0

Contenu à copier dans la racine du dépôt `arkonex-devops-tools`.

## Fichiers

- `open83/tools/validate_resume_package.py`
- `open83/schemas/resume_package.schema.v1.0.0.json`
- `open83/templates/resume_package.example.json`
- `open83/templates/resume_package.manifest.example.json`
- `open83/templates/resume_package.lock.example.json`
- `open83/templates/validation_report.example.json`
- `open83/templates/evidence/final_essential_output.txt`

## Smoke test Windows PowerShell

Depuis la racine du dépôt local :

```powershell
python open83	oolsalidate_resume_package.py `
  --package open83	emplatesesume_package.example.json `
  --schema open83\schemasesume_package.schema.v1.0.0.json `
  --root-dir open83 `
  --out-dir open83\packages\smoke-test
```

Retour attendu : `decision=GO` et code retour 0.

## Limite

Le paquet exemple est un smoke test. Ne pas l'utiliser comme preuve opérationnelle ERPNext/Frappe.
