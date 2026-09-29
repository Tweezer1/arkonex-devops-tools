# claude-code-hooks — garde-fous du harnais Claude Code (instance DEV)

Source versionnée des garde-fous que Claude Code exécute avant chaque commande et au
démarrage de chaque session sur l'instance DEV Arkonex (`deverp.arkonex.ca`).
Lot : `OPEN-161-DEVOPS-CLAUDE-CODE-GUARD-HOOKS-001`
([Issue #168](https://github.com/Tweezer1/arkonex-ops-docs/issues/168)).

Le serveur exécute une **copie déployée** (`/home/frappe/frappe-bench/.claude/hooks/`) ; ce
dossier est la source maître. Ne jamais corriger la copie déployée à la main : corriger
ici, tester, fusionner, puis réinstaller.

## Pourquoi ce dossier existe

Jusqu'à OPEN-161, deux scripts de blocage existaient sur la machine seulement, sans
version ni test. `.claude/settings.json` les appelait par `~/.claude/hooks/…`, un dossier
qui n'existe pas : chaque appel échouait (code 127), Claude Code traitait l'échec comme
une erreur non bloquante, et la commande passait. Les transcriptions conservées montrent
22 905 échecs silencieux du 30/07 au 26/09/2026 et aucun blocage réel. L'un des deux
scripts n'était en outre pas exécutable. Rejoués tels quels sur l'historique, ils auraient
aussi bloqué à tort des centaines de commandes légitimes (sites de test `.local`, mots
interdits cités dans un texte ou un nom de branche).

## Ce que font les garde-fous

Principe : **l'agent reste autonome pour le travail normal** ; seuls quelques gestes rares
et sensibles, déjà encadrés par `CLAUDE.md`, sont refusés ou soumis à validation humaine.
Les règles visent la **commande exécutée**, jamais le texte : arguments entre guillemets,
commentaires, noms de branches et données envoyées ne déclenchent rien. Les commandes
imbriquées (`bash -c`, `sh -c`, `eval`, `sudo`, `$(…)`, `` `…` ``) sont analysées avec les
mêmes règles. Corps de heredoc, comme bash les traite :

- donné à un shell (`bash <<EOF`) : lu comme des commandes ;
- délimiteur protégé (`<<'EOF'`, `<<"EOF"`, `<<\EOF`) : texte pur, ignoré ;
- délimiteur non protégé (`<<EOF`) : le texte est ignoré, mais les `$(…)` et `` `…` `` qu'il
  contient sont analysés — bash les **exécute**, quel que soit le programme qui reçoit le
  heredoc (`cat`, `tee`…). Un `` `bench update` `` écrit dans un tel heredoc serait donc
  réellement lancé : utiliser `<<'EOF'` pour écrire du texte.

| Règle | Déclencheur | Réponse |
|---|---|---|
| `R-SQL` | client `mysql`/`mariadb` (sauf `--version`/`--help`), `bench … mariadb`/`db-console`, `bench … execute frappe.db.sql` | refus |
| `R-HOST` | commande réseau (`curl`, `wget`, `ssh`, `scp`, `rsync`…) ou `git` (`clone`, `fetch`, `push`, `remote add`…) dont la **destination** est un hôte `*.arkonex.ca` autre que `deverp.arkonex.ca` ; les données envoyées (`--data`, `-H`, `-e`…) sont ignorées | refus |
| `R-HOST-CODE` | code réellement exécuté par un interpréteur — argument de `-c`/`-e` (python, node, perl…), heredoc donné à un interpréteur ou à `bench console`, arguments de `bench execute` — contenant une URL, ou un nom d'hôte seul passé en argument d'un appel (`SMTP('…')`), vers un autre serveur `arkonex.ca`. Une simple mention du nom, ou une mention ailleurs dans la commande, ne déclenche rien | validation humaine |
| `R-SITE` | `bench --site X` avec X ni `deverp.arkonex.ca` ni `*.local`, sans distinction de casse (dont `--site all`) | refus |
| `R-SITE-VAR` | site donné par une variable non résolue | validation humaine |
| `R-MIGRATE` | `bench migrate` sans `--site` | refus |
| `R-UPDATE` | `bench update` (OPEN-142) | refus |
| `R-BUILD` | `bench build` sans `--app` (OPEN-142) | refus |
| `R-SECRETS` | Write/Edit sur `sites/<site>/site_config.json`, `.env`, `.env.*` | refus |
| `R-SUDO` | `sudo` hors vérifications en lecture (`sudo -l …`, `sudo -n true`, `supervisorctl status`, `dmesg`, et pour le statut des services `systemctl status`, `show`, `is-active`, `is-enabled`, `is-failed`, `list-units`, `list-timers`, `list-unit-files`, `cat`) | validation humaine |
| `R-PUSH-MAIN` | `git push` dont la branche cible est `main`/`master` (y compris `git push` sans destination depuis `main`, `--all`) | validation humaine |
| `R-PUSH-FORCE` | `git push -f`/`--force`/`--force-with-lease`/`+refspec`/`--mirror` | validation humaine |
| `R-DROP-SITE` | `bench … drop-site` | validation humaine |
| `R-ERREUR` | erreur interne d'un garde-fou | validation humaine (jamais silencieux, jamais blocage total) |

`aide` (`--help`) et fusion de PR (`gh pr merge`) ne sont pas contrôlées. Les garde-fous
ne répondent jamais « autoriser » : ils ne peuvent qu'ajouter un contrôle, jamais
contourner le circuit normal de permissions.

**Limite assumée** : ils protègent contre l'**erreur**, pas contre un contournement
délibéré (commande masquée dans une variable, un script intermédiaire, un code Python qui
appelle une base, un encodage…). Ils ne remplacent ni les règles de `CLAUDE.md`, ni les
droits du système (l'utilisateur `frappe` n'a pas de `sudo` général ni d'accès root à la
base).

## Contrôle de démarrage (`session_start.py`)

À chaque démarrage de session, en lecture seule :

1. la copie principale de `docs` (qui porte `CLAUDE.md`) est sur `main`, sans
   modification suivie, et égale à `main` sur GitHub (`git ls-remote`, aucune écriture
   locale) ;
2. chaque garde-fou configuré existe, est exécutable et **refuse réellement** un cas connu,
   appelé exactement comme Claude Code l'appelle.

En cas de problème, un message s'affiche à la personne et l'agent en est informé. Sinon,
une seule ligne de contexte positive est donnée à l'agent.

**Rappel de suivi GitHub** (`OPEN-169`,
[Issue #189](https://github.com/Tweezer1/arkonex-ops-docs/issues/189)), indépendant des
deux vérifications ci-dessus. Une requête GraphQL en lecture (`gh api graphql`) relève :

- les PR ouvertes des dépôts `Tweezer1`, avec leur ancienneté ;
- les Issues `open-lot` ouvertes qu'une PR fusionnée depuis 30 jours référence par
  `Refs`, `Fixes` ou `Closes` (`#N` ou `Tweezer1/arkonex-ops-docs#N`), lorsque cette fusion
  est postérieure à la dernière mise à jour de l'Issue. Les dépôts de code n'ont aucune
  Issue propre : un `#N` y désigne une Issue de `arkonex-ops-docs`. Les simples liens et
  les titres sont des mentions ; ils ne sont pas retenus.

Le résultat va **à l'agent seulement** (contexte), jamais à l'écran, qui reste réservé aux
alertes : l'agent le signale au propriétaire. Durée bornée à 8 s (mesurée : environ 2 s
avec les deux vérifications) ; en cas d'échec (réseau, `gh`, délai, réponse illisible), une
ligne « indisponible », sans jamais bloquer la session. Variables utiles aux tests :
`ARKONEX_GH` (binaire `gh`), `ARKONEX_FOLLOWUP=off`, `ARKONEX_FOLLOWUP_TIMEOUT`.

**Suivi des décisions** (`OPEN-170`,
[Issue #195](https://github.com/Tweezer1/arkonex-ops-docs/issues/195)). Même principe, avec une
requête distincte, bornée à 6 s. Parmi les lots `open-lot` actifs depuis 7 jours, il relève :

- ceux dont le résumé (le corps de l'Issue) est plus ancien qu'un commentaire de décision
  marqué `[DÉCIDÉ-MÉTIER — …]` ;
- les lignes « Reçu de #N … — à intégrer » de leur tableau des décisions
  (`docs/GOUVERNANCE-GITHUB-ISSUES.md` §12).

Le résultat va à l'agent seulement. Variables : `ARKONEX_TRACE=off`, `ARKONEX_TRACE_TIMEOUT`.

**Après une compaction** : une seconde entrée `SessionStart`, filtrée sur `compact`, appelle
`session_start.py --compact`.

- Si la session a déclaré un lot par `/reprendre-lot`, sa fiche est réinjectée. Un
  avertissement s'ajoute si l'Issue a changé depuis (une requête de lecture, bornée).
- Sinon, rien n'est ajouté.

L'entrée générale continue de faire ses vérifications habituelles.

## Reprendre un lot : `/reprendre-lot` (OPEN-170)

`/reprendre-lot 181` lance `reprendre_lot.py 181`. Le skill vit dans `skills/reprendre-lot/SKILL.md`
et est déployé dans `.claude/skills/`. Le script fait une seule requête GraphQL en lecture
(Issue, commentaires, mentions, registre `BESOINS-TRANSVERSES.md`), bornée à 20 s et jamais
bloquante.

Il produit la fiche de reprise de RB-83 §4.10.5 :

- un en-tête lisible par machine (`a_reconcilier`, `recus_non_integres`…) ;
- l'objectif du lot ;
- les décisions en vigueur, tirées du tableau §12.2 de l'Issue ;
- les éléments reçus à intégrer ;
- les décisions consignées après la dernière mise à jour du résumé ;
- les mentions reçues d'autres dossiers ;
- les besoins transverses et les étapes restantes ;
- les contradictions.

L'agent présente la fiche au propriétaire avant toute action.

Seule écriture : un fichier propre à la session, `~/.cache/arkonex-claude/reprise/<session>.json`
(modifiable par `ARKONEX_REPRISE_DIR`). Il est indexé par `CLAUDE_CODE_SESSION_ID` et relu après
une compaction. La fiche n'a aucune autorité propre : l'Issue fait foi.

## Installer, vérifier, revenir en arrière

```bash
/usr/bin/python3 claude-code-hooks/install.py
```

Sans `--apply`, le script n'écrit rien et affiche le plan. Avec `--apply` : sauvegarde
horodatée dans `.claude/hooks-backup/`, copie des quatre scripts (mode 0755) et du skill
`/reprendre-lot` (`.claude/skills/`), retrait des
anciens `block-forbidden-*.sh` (conservés dans la sauvegarde), remplacement de la seule
section `hooks` de `.claude/settings.json` (les autres réglages sont conservés), puis
contrôle après installation.

**Effet immédiat** : Claude Code relit ses réglages en cours de session ; toutes les
sessions ouvertes appliquent les garde-fous dès l'installation.

Retour arrière, restauration à l'identique (empreintes vérifiées) :

```bash
/usr/bin/python3 claude-code-hooks/install.py --rollback /home/frappe/frappe-bench/.claude/hooks-backup/<horodatage> --apply
```

## Tests

```bash
/usr/bin/python3 -m unittest discover -s claude-code-hooks/tests -v
```

- `test_guards.py` : chaque règle a un cas qui déclenche et un cas voisin qui ne déclenche
  pas ; les cas marqués `REJEU` reproduisent des blocages à tort trouvés sur l'historique
  réel et ne doivent jamais revenir.
- `test_session_install.py` : contrôle de démarrage et installation sur un bench et un
  dépôt `docs` fictifs, dont la reproduction de la panne d'origine et un retour arrière
  vérifié octet pour octet.
- `test_followup.py` : rappel de suivi GitHub avec un faux `gh` (aucun appel réseau), dont
  la reconstitution du cas OPEN-124 (fusion deux minutes après la dernière mise à jour du
  dossier), une mention seule non retenue, la pagination, l'échec, le délai dépassé et une
  réponse illisible. Cinq ruptures volontaires du code, toutes détectées.
- `test_reprise.py` (OPEN-170) : fiche de reprise et suivi des décisions, avec un faux `gh`
  qui répond selon la requête. Il couvre :
  - les décisions en vigueur, remplacées et reçues ;
  - une décision consignée après le résumé, et une mention récente et une ancienne ;
  - un corps sans tableau ;
  - l'échec et le délai dépassé, jamais bloquants ;
  - la réinjection après compaction, avec une Issue changée ou inchangée, et sans lot déclaré ;
  - l'installation du skill et de l'entrée `compact`.
- `replay_corpus.py` : rejoue les commandes réellement exécutées par les sessions (lecture
  des transcriptions locales, rien n'est exécuté) ; le détail va dans un fichier hors du
  dépôt (`--out`), les commandes pouvant contenir des données de travail.

Chaque règle a été vérifiée par mutation : la casser volontairement fait échouer les tests
(24 ruptures volontaires, toutes détectées).

Depuis OPEN-170, la suite tourne aussi sur Linux à chaque PR qui touche ce dossier
(`.github/workflows/claude-code-hooks-tests.yml`, `python3` du système, aucun accès réseau).

## Ajouter ou modifier une règle

1. Ouvrir un lot (Issue `OPEN-*`) : une règle change ce que tous les agents peuvent faire.
2. Ajouter dans `test_guards.py` le cas qui doit déclencher **et** le cas voisin qui ne
   doit pas déclencher.
3. Modifier `guard_bash.py` (ou `guard_paths.py`), lancer les tests, puis
   `replay_corpus.py` : aucun nouveau blocage à tort sur l'historique.
4. PR, fusion humaine, puis `install.py --apply`.

Coût mesuré : environ 60 ms par commande (démarrage de Python compris).
