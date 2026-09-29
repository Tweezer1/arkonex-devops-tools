---
name: reprendre-lot
description: Reprendre un lot OPEN-* d'Arkonex depuis son Issue GitHub (arkonex-ops-docs) — produit la fiche de reprise (RB-83 §4.10.5) et la présente au propriétaire avant toute action (GOUVERNANCE-GITHUB-ISSUES.md §12, point 6). À utiliser au début d'une session qui reprend un lot, ou dès qu'un numéro de lot à reprendre est donné.
argument-hint: <numéro d'Issue, par exemple 181>
allowed-tools: Bash(/usr/bin/python3 {{BENCH_ROOT}}/.claude/hooks/reprendre_lot.py:*)
---

# Reprendre un lot depuis son Issue

Source versionnée : `arkonex-devops-tools/claude-code-hooks/skills/reprendre-lot/` (OPEN-170, Issue #195). Ne pas modifier la copie déployée à la main.

1. Produis la fiche, en lecture seule :

   ```bash
   /usr/bin/python3 {{BENCH_ROOT}}/.claude/hooks/reprendre_lot.py $ARGUMENTS
   ```

2. Présente la fiche au propriétaire **avant toute action**, en français, sans la réécrire ni la résumer au point d'en perdre les liens :
   - décisions en vigueur ;
   - éléments reçus d'autres lots ;
   - contradictions et alertes ;
   - étapes restantes.
3. Si `a_reconcilier: true` : dis-le en une ligne. Propose de mettre à jour le tableau des décisions de l'Issue (même geste, GOUVERNANCE-GITHUB-ISSUES.md §12.2) avant tout travail qui en dépend.
4. Si des éléments reçus sont à intégrer : présente-les. Demande s'il faut les intégrer, les écarter ou les laisser en attente.
5. Le gate GitHub Issue de `CLAUDE.md` reste obligatoire, et la fiche n'a aucune autorité propre. En cas de doute ou de désaccord, l'Issue fait foi.
6. Attends la réponse du propriétaire avant d'agir.

La fiche est enregistrée pour cette session : après une compaction, le contrôle de démarrage la réinjecte automatiquement, avec un avertissement si l'Issue a changé depuis.
