# CHANGELOG

## OPEN-164 — Contrôle des liens cassés sur DEV

- Ajout de `dev-link-check/` : contrôle en lecture seule, exécuté via `bench console`, qui trouve les champs `Link` pointant vers un document absent sur un site Frappe et compare le constat à une référence versionnée (`baseline.json`, générée sur DEV par l'outil le 2026-09-27 : 41 liens connus, chacun justifié) — PASS tant qu'aucun lien cassé nouveau n'apparaît. Issue [#173](https://github.com/Tweezer1/arkonex-ops-docs/issues/173).

## OPEN-161 — Garde-fous du harnais Claude Code

- Ajout de `claude-code-hooks/` : garde-fous `PreToolUse` (commandes Bash, écritures de fichiers) et contrôle de démarrage `SessionStart`, avec installation et retour arrière (`install.py`), tests et rejeu de l'historique réel. Remplace les anciens scripts non versionnés `block-forbidden-*.sh`, qui n'avaient jamais fonctionné (chemin inexistant). Issue [#168](https://github.com/Tweezer1/arkonex-ops-docs/issues/168).

## v0.1.0 — Initialisation

- Création de la structure `arkonex-devops-tools`.
- Ajout du dossier `open83/`.
- Préparation des dossiers `docs`, `schemas`, `tools`, `templates`, `tests`, `packages`.
