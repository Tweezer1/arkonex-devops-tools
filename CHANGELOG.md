# CHANGELOG

## OPEN-170 — Reprise d'un lot et suivi des décisions

- `claude-code-hooks/reprendre_lot.py` et skill `/reprendre-lot` : fiche de reprise d'un lot, dérivée de son Issue (RB-83 §4.10.5), présentée avant toute action. Le script est en lecture seule sur GitHub ; il note le lot déclaré pour la session.
- `session_start.py` : le suivi des décisions signale les résumés d'Issue en retard sur une décision `[DÉCIDÉ-MÉTIER]` et les éléments « Reçu … — à intégrer » (lots actifs depuis 7 jours). Une nouvelle entrée `SessionStart` filtrée sur `compact` réinjecte la fiche après une compaction, avec un avertissement si l'Issue a changé depuis.
- `install.py` déploie aussi `reprendre_lot.py` et le skill, avec sauvegarde et retour arrière.
- Tests `test_reprise.py`, et workflow `claude-code-hooks-tests.yml` : la suite de tests tourne sur Linux à chaque PR.
- Issue [#195](https://github.com/Tweezer1/arkonex-ops-docs/issues/195).

## OPEN-164 — Contrôle des liens cassés sur DEV

- Ajout de `dev-link-check/` : contrôle en lecture seule, exécuté via `bench console`, qui trouve les champs `Link` pointant vers un document absent sur un site Frappe et compare le constat à une référence versionnée (`baseline.json`, générée sur DEV par l'outil le 2026-09-27 : 41 liens connus, chacun justifié) — PASS tant qu'aucun lien cassé nouveau n'apparaît. Issue [#173](https://github.com/Tweezer1/arkonex-ops-docs/issues/173).

## OPEN-161 — Garde-fous du harnais Claude Code

- Ajout de `claude-code-hooks/` : garde-fous `PreToolUse` (commandes Bash, écritures de fichiers) et contrôle de démarrage `SessionStart`, avec installation et retour arrière (`install.py`), tests et rejeu de l'historique réel. Remplace les anciens scripts non versionnés `block-forbidden-*.sh`, qui n'avaient jamais fonctionné (chemin inexistant). Issue [#168](https://github.com/Tweezer1/arkonex-ops-docs/issues/168).

## v0.1.0 — Initialisation

- Création de la structure `arkonex-devops-tools`.
- Ajout du dossier `open83/`.
- Préparation des dossiers `docs`, `schemas`, `tools`, `templates`, `tests`, `packages`.
