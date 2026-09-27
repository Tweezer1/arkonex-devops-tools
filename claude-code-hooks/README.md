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
| `R-HOST-CODE` | code exécuté (`python -c`, heredoc donné à `python`/`node`, `bench console`/`execute`) contenant une URL ou un nom d'hôte seul entre guillemets vers un autre serveur `arkonex.ca` | validation humaine |
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

## Installer, vérifier, revenir en arrière

```bash
/usr/bin/python3 claude-code-hooks/install.py
```

Sans `--apply`, le script n'écrit rien et affiche le plan. Avec `--apply` : sauvegarde
horodatée dans `.claude/hooks-backup/`, copie des trois scripts (mode 0755), retrait des
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
- `replay_corpus.py` : rejoue les commandes réellement exécutées par les sessions (lecture
  des transcriptions locales, rien n'est exécuté) ; le détail va dans un fichier hors du
  dépôt (`--out`), les commandes pouvant contenir des données de travail.

Chaque règle a été vérifiée par mutation : la casser volontairement fait échouer les tests
(20 ruptures volontaires, toutes détectées).

## Ajouter ou modifier une règle

1. Ouvrir un lot (Issue `OPEN-*`) : une règle change ce que tous les agents peuvent faire.
2. Ajouter dans `test_guards.py` le cas qui doit déclencher **et** le cas voisin qui ne
   doit pas déclencher.
3. Modifier `guard_bash.py` (ou `guard_paths.py`), lancer les tests, puis
   `replay_corpus.py` : aucun nouveau blocage à tort sur l'historique.
4. PR, fusion humaine, puis `install.py --apply`.

Coût mesuré : environ 60 ms par commande (démarrage de Python compris).
