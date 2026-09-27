# arkonex-devops-tools

Outils transversaux Arkonex pour DevOps, gouvernance IA, validation de paquets de reprise, QA et automatisation contrôlée.

## Portée initiale

- `open83/` : outils liés au 83-kb-runbook — Contrat de contexte, ownership runtime et reprise IA.
- `claude-browser-qa/` : infrastructure Browser QA reproductible (OPEN-125).
- `claude-code-hooks/` : garde-fous du harnais Claude Code de l'instance DEV (OPEN-161).
- `dev-link-check/` : contrôle en lecture seule des liens vers un document absent sur DEV (OPEN-164).

## Règles

- `main` doit rester stable.
- Chaque changement se fait sur une branche de lot.
- Aucun secret ne doit être committé.
- Les scripts sont testés localement avant commit.
- Le serveur Frappe peut exécuter une copie contrôlée, mais ne doit pas être la source maître.

## Statut

Brouillon initial.
