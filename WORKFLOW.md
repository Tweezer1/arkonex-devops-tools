# Workflow Git / VS Code — arkonex-devops-tools

## 1. Objectif

Ce dépôt contient des outils transversaux Arkonex pour DevOps, gouvernance IA, validation de paquets de reprise, QA et automatisation contrôlée.

Règles principales :

- `main` doit rester stable.
- Toute modification se fait sur une branche dédiée.
- Aucun commit ne doit être fait sans test applicable.
- Aucun secret ne doit être committé.
- Aucun push forcé ne doit être utilisé sans décision explicite.

## 2. Branches

`main` représente l’état stable du dépôt.

Ne pas modifier directement `main` pour un nouveau lot de travail.

Formats recommandés :

```text
docs/<open-id>-<sujet-court>
fix/<open-id>-<sujet-court>
test/<open-id>-<sujet-court>
work/<open-id>-<sujet-court>
```

Exemples :

```text
docs/open-83-git-f-branch-workflow
fix/open-83-validator-schema-error
test/open-83-validator-fixtures
work/open-83-package-manifest
```

## 3. Démarrer un lot

Toujours partir de `main` propre.

```powershell
cd "E:\OneDrive\02_Projects\GitHub\arkonex-devops-tools"

git switch main
git status
git pull --ff-only
git switch -c docs/open-83-git-f-branch-workflow
```

Si `git status` n’est pas propre, arrêter et diagnostiquer avant de continuer.

## 4. Utilisation VS Code

Ouvrir la racine du dépôt :

```text
E:\OneDrive\02_Projects\GitHub\arkonex-devops-tools
```

Ne pas ouvrir seulement `open83`, `tools`, `schemas` ou un sous-dossier isolé.

Pourquoi :

- VS Code détecte correctement Git.
- `.gitignore` est appliqué depuis la racine.
- Les chemins relatifs des scripts restent cohérents.
- Les tests peuvent être lancés depuis la racine.

## 5. Cycle normal de modification

```text
1. Créer une branche depuis main.
2. Modifier les fichiers dans VS Code.
3. Sauvegarder.
4. Vérifier git status.
5. Exécuter les tests applicables.
6. Ajouter seulement les fichiers ciblés.
7. Committer.
8. Pousser la branche.
9. Valider humainement.
10. Merger vers main seulement après validation.
```

## 6. Tests Open83

Avant un commit qui touche Open83, exécuter au minimum :

```powershell
.\.venv\Scripts\python.exe open83\tools\validate_resume_package.py `
  --package open83\templates\resume_package.example.json `
  --schema open83\schemas\resume_package.schema.v1.0.0.json `
  --root-dir open83 `
  --out-dir open83\packages\smoke-test
```

Critère GO :

```text
decision=GO
errors=0
warnings=0
```

Si le test retourne `NO_GO`, ne pas commit.

## 7. Ajouter les fichiers

Éviter `git add .` par défaut.

Préférer :

```powershell
git status --short
git add WORKFLOW.md
```

## 8. Messages de commit

Format recommandé :

```text
type(scope): action courte
```

Exemples :

```text
docs(workflow): add branch and vscode workflow
fix(open83): correct resume package schema example
test(open83): add validator fixtures
chore(open83): update requirements
```

## 9. Push d’une branche

```powershell
git push -u origin docs/open-83-git-f-branch-workflow
```

Ne pas pousser directement `main` sauf après validation.

## 10. Merge vers main

Méthode locale contrôlée :

```powershell
git switch main
git pull --ff-only
git merge --ff-only docs/open-83-git-f-branch-workflow
git push origin main
```

Si `git merge --ff-only` bloque, arrêter. Ne pas forcer.

## 11. Rollback

Avant commit :

```powershell
git restore WORKFLOW.md
```

Après push de branche refusée :

```powershell
git push origin --delete docs/open-83-git-f-branch-workflow
git branch -D docs/open-83-git-f-branch-workflow
```

Après merge dans `main`, préférer :

```powershell
git revert <commit_hash>
git push origin main
```

Éviter :

```powershell
git reset --hard
git push --force
```

## 12. Règles anti-erreur

Stopper si :

- `git status` montre des fichiers inattendus.
- `.venv/` apparaît comme fichier à commit.
- `open83/packages/smoke-test*` apparaît comme fichier à commit.
- Un secret, token, clé ou `.env` apparaît.
- Un test applicable retourne `NO_GO`.
- Git demande un merge automatique non compris.
- Git demande un push forcé.

## 13. Règle IA

L’IA peut proposer, auditer, structurer et générer des brouillons.

L’humain valide :

- les commits,
- les merges,
- les pushes vers `main`,
- les changements de contrat,
- les scripts exécutables,
- les actions touchant un serveur ou un environnement de production.
