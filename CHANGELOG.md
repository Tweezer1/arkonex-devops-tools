# CHANGELOG

## OPEN-161 — Garde-fous du harnais Claude Code

- Ajout de `claude-code-hooks/` : garde-fous `PreToolUse` (commandes Bash, écritures de fichiers) et contrôle de démarrage `SessionStart`, avec installation et retour arrière (`install.py`), tests et rejeu de l'historique réel. Remplace les anciens scripts non versionnés `block-forbidden-*.sh`, qui n'avaient jamais fonctionné (chemin inexistant). Issue [#168](https://github.com/Tweezer1/arkonex-ops-docs/issues/168).

## v0.1.0 — Initialisation

- Création de la structure `arkonex-devops-tools`.
- Ajout du dossier `open83/`.
- Préparation des dossiers `docs`, `schemas`, `tools`, `templates`, `tests`, `packages`.
