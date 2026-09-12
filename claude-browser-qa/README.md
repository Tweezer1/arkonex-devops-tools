# claude-browser-qa

Infrastructure Browser QA / Playwright MCP déterministe et reproductible pour
`deverp.arkonex.ca` — source maître versionnée de ce dépôt (`arkonex-devops-tools`),
gouvernée par `OPEN-125-DEVOPS-BROWSER-QA-REPRODUCIBLE-001`
([Issue #87](https://github.com/Tweezer1/arkonex-ops-docs/issues/87), `arkonex-ops-docs`).

Cohérent avec le principe déjà énoncé dans le README racine de ce dépôt : *« Le serveur
Frappe peut exécuter une copie contrôlée, mais ne doit pas être la source maître. »* La
configuration active sur le bench (`/home/frappe/frappe-bench/.mcp.json`) est un
**artefact généré et activé atomiquement**, jamais un symlink cross-repo, jamais édité à
la main.

## Statut

**Activation historique prouvée le 4 septembre 2026 ; régression constatée et corrigée
le 12 septembre 2026 (`OPEN-125`, [Issue #87](https://github.com/Tweezer1/arkonex-ops-docs/issues/87)).**
L'état courant et vivant du lot reste l'Issue #87 ; ce README documente le mécanisme
effectivement livré.

**Cause racine `[PROUVÉ-CODE]`** : la cadence D7 d'origine (`OnBootSec` + `OnUnitActiveSec`,
aucun `OnCalendar`) calcule sa prochaine échéance à partir du timestamp d'activation **en
mémoire** de l'unité `.service` déclenchée (`ActiveEnterTimestamp`) — une valeur qui ne
persiste jamais sur disque. Ce timestamp est effacé chaque fois que l'objet runtime de
cette unité est recréé (concrètement : un `daemon-reload` consécutif à un redéploiement
des fichiers unités) — reproduit exactement le 4 septembre : un premier déclenchement a eu
lieu à 17:36 UTC, un redéploiement + `daemon-reload` a suivi peu après (20:21 UTC), et plus
aucune échéance n'a jamais été recalculée ensuite. `Persistent=true` n'a jamais aidé ici :
il ne rattrape que `OnCalendar=`, jamais les spécifications monotones — confirmation d'une
correction déjà actée dans l'Issue avant diagnostic complet.

**Correctif livré et prouvé en runtime le 12 septembre** :
- `OnCalendar=*-*-* 00/6:00:00` remplace `OnUnitActiveSec` (recalculé depuis l'horloge
  murale à chaque vérification, insensible à tout `daemon-reload` futur) ; `Persistent=true`
  effectue désormais un vrai rattrapage ; `OnBootSec=5min` conservé.
- `systemd-install` effectue désormais `enable` **+ `restart` inconditionnel** (pas
  seulement `enable --now`, qui ne recalcule rien sur une unité déjà active — second bug
  réel découvert pendant le tout premier canary de ce correctif : après installation du
  timer corrigé, `enable --now` a laissé `NextElapseUSecRealtime` vide jusqu'à un `restart`
  explicite).
- `prepare-persona.sh` (nouveau) : point d'entrée contrôlé pour une session Claude Code,
  voir « Préparation et récupération à la demande » ci-dessous.
- `provision-sudoers.sh` + `bootstrap-browser-qa.sh sudoers-install` (nouveau, exclu de
  `all`) : règle sudo minimale generée depuis `personas.yaml`, validée `visudo -c` avant
  toute installation.
- Deux bugs supplémentaires trouvés et corrigés par une revue avant merge, tous deux
  vérifiés par reproduction (pas seulement par lecture) : `prepare-persona.sh` pouvait
  rapporter un échec (`SERVICE_UNAVAILABLE`) avec un code de sortie 0 (`if ! CMD; then
  rc=$?` capture le statut de la condition négatée, jamais celui de `CMD`) et testait
  l'autorisation sudo via `sudo -n true` (ne vérifie rien sur la commande scopée réelle,
  sans rapport avec elle) au lieu de `sudo -n -l <commande exacte>` ; `provision-sudoers.sh`
  refusait **toute** divergence, y compris l'ajout légitime d'un persona — corrigé par un
  parcours de mise à jour contrôlée (voir « `personas.yaml` — extensibilité N-personas »).
- **Preuves runtime réelles, distinguées précisément** (ne jamais mélanger un mécanisme
  prouvé et une hypothèse) :
  - `[PROUVÉ]` le mécanisme de renouvellement revalide réellement l'identité attendue —
    observé une première fois via un `systemctl restart` du timer **exécuté par un
    humain** (aucun secret dans cette commande, mais une intervention humaine réelle,
    pas une récupération autonome), puis une seconde fois via
    `sudo -n systemctl start browser-qa-refresh@estimate_user.service` exécuté
    **directement par Claude, sans stub**, via la règle sudo scopée réellement installée —
    preuve que cette autorisation fonctionne vraiment pour `frappe`, sans mot de passe.
  - `[PROUVÉ]` `prepare-persona.sh` exécuté réellement (pas de stub) pour les deux
    personas : chemin « déjà valide, aucun renouvellement demandé » confirmé
    (`PASS (ALREADY_VALID)`).
  - `[PROUVÉ]` deux déclenchements **réellement planifiés** successifs du timer lui-même,
    sans intervention humaine ni Claude au moment du déclenchement
    (`tests/canary-timer-recurrence.sh`, cadence accélérée temporairement puis config
    finale 6h rétablie et sa prochaine échéance revérifiée).
  - `[PROUVÉ]` le cycle complet de `prepare-persona.sh` — détection, appel sudo,
    renouvellement, reconfirmation — exécuté de bout en bout par le script lui-même,
    **sans intervention humaine pendant la récupération** (2026-09-12 17:33:43-53 UTC,
    10s) : storageState `estimate_user` déplacé au préalable par un humain
    (`sudo -u browserqa-refresh mv ...`, seule action humaine, hors récupération —
    équivalent fonctionnel d'une expiration pour `validate-storage-state.mjs`, mais pas
    une expiration survenue naturellement dans le temps) ; le script a détecté
    `observed='Guest'`, déclenché le renouvellement réel, et reconfirmé
    `PASS (RENEWED)`.
  - `[PROUVÉ]` navigateur MCP réel après récupération, même session : premier usage de
    `playwright-estimate_user` (contexte neuf) → `/desk` réel, snapshot confirmant le
    menu utilisateur "E2E Tests" ; `playwright-estimate_manager` → snapshot confirmant
    "E2E Estimate Manager", menu Desk plus large cohérent avec son rôle — deux identités
    distinctes correctement rendues.
  - `[À VALIDER]` nouvelle session Claude Code **distincte** (processus séparé) avec
    navigateur authentifié sous les deux personas — pas encore fait dans cette session-ci
    par construction (une session ne peut pas prouver qu'une AUTRE session fonctionne).
  - 44/44 tests (`tests/run-phase-a-tests.sh`), dont 4 couvrant précisément les deux bugs
    de revue ci-dessus (T42-T44).

**Réserve documentée** (acceptée explicitement, voir clôture #87) : le rollback DNS
(`dns_rollback`) et le rollback config (`.mcp.json`, restauration depuis un backup réel)
sont prouvés par mécanisme réel + tests réels (`tests/T36` pour DNS sur une copie de
`/etc/hosts`, backups horodatés réels pour la config), mais **pas** par un cycle live sur
le fichier de production — jugé disproportionné (risque de casser la résolution DNS ou les
MCP actifs pour une valeur de preuve marginale). À reconsidérer si un incident réel touche
un jour cette infrastructure.

Toute évolution future (N-ème persona, montée de version MCP/navigateur, changement de
cadence timer) reste un changement contrôlé séparé, jamais une improvisation pendant une
session Browser QA — voir « Ce que ce répertoire ne fait jamais » ci-dessous.

## Architecture

```
personas.yaml (manifeste versionné, 0 secret)
        │
        ├─→ generate-mcp-config.py ──→ .mcp.json (généré, activé atomiquement)
        │
        └─→ browser-qa-refresh@<persona>.timer (systemd, templaté)
                    │
                    └─→ refresh-persona.sh <persona>
                              │  (exécuté par l'utilisateur système dédié
                              │   browserqa-refresh — JAMAIS frappe, JAMAIS Claude)
                              ├─→ lit credentials (SEUL composant autorisé)
                              ├─→ auth/playwright-login.mjs   (login réel, storageState candidat)
                              ├─→ auth/validate-storage-state.mjs (preuve session réelle)
                              └─→ atomic rename → storageState actif (lu par MCP au démarrage)
```

## Frontière de sécurité (non négociable)

```
credentials   /etc/arkonex/browser-qa/credentials/<persona>.env
                → lecture : browserqa-refresh UNIQUEMENT
                → frappe : DENIED
                → Claude : jamais, sous aucune forme (pas d'argv, pas d'env, pas de log)

storageState  /var/lib/arkonex-browser-qa/storage-states/<persona>.json
                → écriture : browserqa-refresh UNIQUEMENT (via rename atomique)
                → lecture  : frappe (chargé nativement par --storage-state du serveur MCP)
                → Claude : jamais lu/affiché/cat/jq/Python-loadé, jamais addCookies manuel

MCP (--isolated --storage-state=...) : AUCUN accès credentials, AUCUN --secrets

navigateur      /opt/arkonex-browser-qa/browsers (PLAYWRIGHT_BROWSERS_PATH partagé)
                → installé une seule fois (bootstrap-browser-qa.sh browser-install --apply)
                → lu par browserqa-refresh (refresh) ET frappe (MCP) — jamais deux
                  installations séparées, jamais un cache $HOME implicite (browserqa-refresh
                  n'a pas de $HOME)
```

## Déploiement — copie contrôlée obligatoire pour toute opération runtime

```
Git (ce checkout, source maître)
        │
        └─→ deploy-controlled-copy.sh --target /opt/arkonex-browser-qa/claude-browser-qa --apply
                    (rsync -a --delete : mirroir déterministe, 0 fichier fantôme ;
                     puis `npm ci` dans auth/, jamais `npm install` improvisé)
                    │
                    └─→ TOUTES les opérations suivantes (provision-identity, systemd-install,
                        credentials-configure, generate-config, refresh, verify) s'exécutent
                        depuis CETTE copie déployée (/opt/...), jamais depuis ce checkout.
```
`deploy-controlled-copy.sh` est la **seule** commande qui lit ce checkout comme source.
Une fois le déploiement fait, toute commande ultérieure s'invoque via
`/opt/arkonex-browser-qa/claude-browser-qa/<script>` — jamais via le chemin de ce
checkout — pour qu'un commit local non redéployé ne puisse jamais créer de divergence
silencieuse entre "ce qui a été testé/commité" et "ce qui tourne réellement". Chaque
script résout ses propres chemins relatifs à son propre répertoire (`$SCRIPT_DIR`), donc
ce sont exactement les mêmes fichiers, exécutés depuis l'emplacement voulu — aucune
seconde implémentation. Preuve automatisée : `tests/T25` déploie dans un répertoire
temporaire, modifie *uniquement* la copie déployée, et prouve que les opérations
lancées depuis cette copie ne retombent jamais silencieusement sur ce checkout.

## `personas.yaml` — extensibilité N-personas

Source unique de vérité. Aucun script de ce répertoire ne code en dur un nom de persona.
Ajouter une persona = ajouter une entrée + provisionner ses credentials hors Git + laisser
le refresher générer son storageState + rejouer `bootstrap-browser-qa.sh generate-config`
et `systemd-install` — **zéro modification de script**. Voir `personas.yaml` pour le
schéma exact des champs.

Le manifeste déclare `expected_role` à titre **documentaire uniquement** — Browser QA
*vérifie* les droits, il ne les *définit* jamais. Aucune création/modification de rôle ou
permission ERPNext ne provient de ce répertoire.

### Ajouter un nouveau persona (rôles/droits différents, pour un autre lot)

Division du travail **fixe**, quel que soit le rôle ERPNext visé — un futur lot ne doit
jamais rester bloqué faute d'information technique sur ce point (voir aussi `CLAUDE.md`
racine, section Browser QA) :

| Étape | Qui | Commande / action |
|---|---|---|
| 1. Ajouter l'entrée dans `personas.yaml` | Claude | édition directe (0 secret, versionné) |
| 2. Créer le compte Frappe/ERPNext + assigner ses rôles | Claude, **sous le contrat/gate du lot qui en a besoin**, pas sous OPEN-125 | ORM Frappe (`bench console` ou équivalent autorisé par ce lot) |
| 3. Régénérer `.mcp.json` | Claude | `bash bootstrap-browser-qa.sh generate-config --apply` |
| 4. **Provisionner le mot de passe** | **Humain, terminal réel, jamais Claude Code** | `sudo -u browserqa-refresh bash credentials-configure.sh <persona>` |
| 5. Installer l'instance timer + la règle sudo | **Humain** (root, aucun secret) | `sudo bash bootstrap-browser-qa.sh systemd-install --apply` puis `sudoers-install --apply` |
| 6. Premier login réel + confirmation d'identité | Claude | `bash prepare-persona.sh <persona>` |

**Étape 4 : pourquoi c'est un mur technique, pas une convention.** `credentials-configure.sh`
refuse de s'exécuter si l'entrée n'est pas un vrai TTY interactif (pas de pipe, pas
d'automatisation), et le répertoire cible
(`/etc/arkonex/browser-qa/credentials/`, mode `0700`, propriétaire `browserqa-refresh`)
est illisible/inscriptible pour `frappe` — c'est le système de fichiers qui l'impose,
Claude ne pourrait pas contourner ça même en essayant. Le mot de passe n'est jamais
visible, transmis ou proposé par Claude, à aucune étape.

**Étape 5 : pourquoi c'est un humain, alors qu'il n'y a pas de secret.** Simple manque de
privilège root interactif côté Claude (documenté sous OPEN-125) — `systemd-install` et
`sudoers-install` ne lisent ni n'écrivent jamais de credential ; Claude peut préparer les
deux commandes exactes à copier-coller, mais ne peut pas les exécuter lui-même.

Aucune modification de script n'est jamais nécessaire pour ces 6 étapes — preuve
structurelle : `tests/T10` (« PERSONA_EXTENSION_TEST ») génère une config `.mcp.json`
valide pour un 3e persona fictif sans toucher un seul fichier commun.

## Composants

| Fichier | Rôle | Exécuté en Phase A ? |
|---|---|---|
| `personas.yaml` | manifeste N-personas, 0 secret | lu par les tests |
| `lib/simple_yaml.py` | parseur YAML restreint (0 dépendance externe) | testé |
| `lib/resolve_persona_field.py` | résolution d'un champ pour un script shell | testé |
| `lib/list_enabled_personas.py` | énumération des personas actives | testé |
| `generate-mcp-config.py` | génère un `.mcp.json` candidat depuis le manifeste | testé (répertoire temporaire) |
| `mcp.json.template` | exemple de référence, non consommé directement | — |
| `provision-system-identity.sh` | provisioning idempotent utilisateur/groupe (`browserqa-refresh`/`browserqa-storage`) — PASS/no-op si conforme, **STOP si divergence réelle** | testé (contre de vrais comptes système existants, chemins PASS et STOP prouvés) |
| `deploy-controlled-copy.sh` | mirroir déterministe (`rsync -a --delete`) + `npm ci` vers la copie contrôlée | testé (anti-fantôme, idempotence, version pinnée réellement installée) |
| `credentials-configure.sh` | **seul** outil de provisioning des credentials — prompt TTY masqué, jamais d'argv/log/écho du mot de passe, vérifie sans relire le secret | testé (mode test, valeur fictive uniquement) |
| `bootstrap-browser-qa.sh` | orchestrateur idempotent (`dns`/`provision-identity`/`deploy`/`generate-config`/`browser-install`/`systemd-install`/`sudoers-install`/`all`), **plan par défaut, `--apply` explicite requis** ; `systemd-install` exige un `restart` inconditionnel, pas seulement `enable --now` | installé et prouvé en runtime (2026-09-12) |
| `verify-browser-qa.sh` | vérificateur read-only, ne corrige jamais | non exécuté contre l'instance réelle en Phase A |
| `prepare-persona.sh` | point d'entrée contrôlé pour une session Claude Code — vérifie, demande un renouvellement borné seulement si nécessaire, revérifie | installé et prouvé en runtime (2026-09-12) |
| `provision-sudoers.sh` | génère + valide (`visudo -c`) la règle sudo minimale par persona activée, STOP si divergence | installé et prouvé en runtime (2026-09-12) |
| `tests/canary-timer-recurrence.sh` | outil manuel root, temporaire, jamais dans `all` — preuve de récurrence par cadence accélérée puis restauration automatique | exécuté avec succès (2026-09-12) |
| `auth/package.json` / `auth/package-lock.json` | dépendance Node reproductible (`npm ci`), `playwright` pinné exactement à la version déjà contractée avec `@playwright/mcp` | lockfile généré et vérifié |
| `auth/refresh-persona.sh` | orchestrateur générique de refresh (un seul fichier, `%i` = persona), fixe mode 0640 + groupe partagé sur le storageState **avant** le rename atomique | non exécuté (requiert credentials réels) |
| `auth/playwright-login.mjs` | login Frappe réel + capture storageState | non exécuté |
| `auth/validate-storage-state.mjs` | preuve de session authentifiée sans jamais afficher le cookie | non exécuté (requiert un storageState réel) |
| `auth/browser-qa-refresh@.service` | unité systemd template (oneshot), `SupplementaryGroups=browserqa-storage`, `StartLimitIntervalSec=600`/`StartLimitBurst=3` | installée et prouvée en runtime (2026-09-12) |
| `auth/browser-qa-refresh@.timer` | unité systemd template (cadence D7, `OnCalendar=` depuis le 2026-09-12) | installée et prouvée en runtime (2026-09-12) |

## Version MCP

```
@playwright/mcp@0.0.80 — pin exact, jamais @latest.
Pin lui-même playwright/playwright-core en exact (1.63.0-alpha-2026-08-31 au moment de ce
design) — pinner @playwright/mcp suffit à fixer toute la chaîne de dépendance.
```
Toute montée de version = changement contrôlé et documenté séparément (jamais une
découverte/installation opportuniste pendant une session Browser QA).

## Installation du navigateur — prouvée par canary runtime réel

```
BROWSER_INSTALL_DESIGN=RATIFIED
BROWSER_INSTALL_EXACT_COMMAND=PROVEN (canary runtime réel, activation Phase B — 2026)
```

**Découverte du canary** : `npx @playwright/mcp@0.0.80 install-browser chrome-for-testing`
(utilisé au tout premier canary) télécharge un **canal distinct** («
`chrome-for-testing`», confirmé dans le registre `chromiumAliases` de `playwright-core` —
`{ browserName: "chromium", channel: "chrome-for-testing" }`) — **différent** de ce que
`chromium.launch()` sans canal (utilisé par `auth/playwright-login.mjs` **et** implicitement
par `--browser=chromium` du serveur MCP) résout réellement, à savoir l'entrée `"chromium"`
de `playwright-core/browsers.json` (révision distincte). Confirmé par échec réel en
Phase B : `browserType.launch: Executable doesn't exist at
.../chromium_headless_shell-1243/...`.

**Commande canonique prouvée** (téléchargement réel exécuté, `LAUNCH_OK` confirmé
ensuite) :
```bash
cd <copie contrôlée>/auth
PLAYWRIGHT_BROWSERS_PATH=/opt/arkonex-browser-qa/browsers npx playwright install chromium
```
`bootstrap-browser-qa.sh browser-install --apply` exécute exactement cette commande.
**Jamais** `install-browser chrome-for-testing` — canal non utilisé par ce design.

**Chemin partagé obligatoire** : `browserqa-refresh` n'a pas de répertoire `$HOME`
(`--no-create-home`) — `PLAYWRIGHT_BROWSERS_PATH=/opt/arkonex-browser-qa/browsers` est
donc fixé explicitement (`Environment=` dans `browser-qa-refresh@.service`, `env` dans
chaque entrée générée de `.mcp.json`) plutôt que de dépendre d'un cache
`$HOME/.cache/ms-playwright` implicite — un seul téléchargement sert à la fois le
refresher (`browserqa-refresh`) et le serveur MCP (`frappe`), jamais deux installations
séparées et potentiellement divergentes.

## Cadence du timer (D7, corrigée le 2026-09-12 — #87)

```
OnBootSec=5min
OnCalendar=*-*-* 00/6:00:00
Persistent=true
RandomizedDelaySec=5min
```
`OnUnitActiveSec=6h` (cadence originale) a été **remplacé** par `OnCalendar=`, pas
seulement complété. Raison `[PROUVÉ-CODE]` : `OnUnitActiveSec` se calcule depuis
`ActiveEnterTimestamp` de l'unité `.service` déclenchée, une valeur en mémoire jamais
persistée, effacée par tout `daemon-reload` qui recrée l'objet runtime de cette unité —
exactement ce qui s'est produit le 4 septembre (redéploiement peu après le premier
déclenchement, plus aucune échéance recalculée ensuite). `OnCalendar=` est réévalué à
chaque vérification depuis l'horloge murale : aucune dépendance à une mémoire volatile,
donc insensible à un futur `daemon-reload`. `Persistent=true` effectue maintenant un vrai
rattrapage (fichier de stamp sur disque, propre à `OnCalendar=` — voir
[systemd.timer](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.timer.xml)).
`OnBootSec=5min` reste pour une reprise rapide après un vrai redémarrage — inoffensif à
côté d'`OnCalendar=`.

**Second bug réel, découvert pendant le tout premier canary de ce correctif** :
`systemctl enable --now` sur un timer **déjà actif** ne recalcule rien (`--now` dégénère
en `start` sur une unité déjà démarrée). `systemd-install` fait donc désormais
`enable` (idempotent) **+ `restart` inconditionnel** — correct aussi bien à la première
installation qu'à chaque redéploiement futur.

Preuve de récurrence (`tests/canary-timer-recurrence.sh`, exécution manuelle root,
2026-09-12) : cadence temporairement accélérée à 1/minute via un drop-in systemd
sur l'unité template, deux déclenchements **pilotés par le timer** observés
successivement pour les deux personas (`estimate_user`, `estimate_manager`), cadence
finale 6h rétablie automatiquement et sa prochaine échéance revérifiée non vide.

## Préparation et récupération à la demande — livré et prouvé le 2026-09-12 (#87)

Le mécanisme (règle sudo scopée, script, comportements de concurrence/échec) est livré et
son cycle complet — détection d'un état invalide, appel sudo, renouvellement,
reconfirmation, navigateur MCP réel après coup — a été vérifié de bout en bout par
exécution réelle (voir « Statut » ci-dessus). Ce qui reste `[À VALIDER]` : une nouvelle
session Claude Code distincte sous les deux personas — voir
[Issue #87](https://github.com/Tweezer1/arkonex-ops-docs/issues/87) pour l'état courant.

`prepare-persona.sh <persona>` est l'unique point d'entrée qu'une session Claude Code
peut appeler avant un test Browser QA :

```bash
bash prepare-persona.sh estimate_user
```

1. Vérifie l'identité du storageState de la persona (lecture seule,
   `auth/validate-storage-state.mjs`) ;
2. Si et seulement si elle n'est pas déjà valide, demande **exactement une** tentative
   de renouvellement bornée : `sudo -n systemctl start browser-qa-refresh@<persona>.service`
   (bloquant jusqu'à complétion ou `TimeoutStartSec=120` de l'unité — aucune attente
   réimplémentée ici) ;
3. Revérifie l'identité et rapporte `PREPARE_<persona>=PASS|EXPIRED|FAIL|SERVICE_UNAVAILABLE`
   sur stdout, sans jamais afficher de secret.

Une session déjà valide n'est **jamais** renouvelée (`PASS (ALREADY_VALID)`). Le
storageState renouvelé n'actualise pas automatiquement un contexte MCP déjà ouvert — après
un `PASS`, la session appelante doit recréer/rouvrir le serveur MCP `playwright-<persona>`
concerné puis confirmer réellement l'identité dans le navigateur (voir
[Playwright MCP](https://github.com/microsoft/playwright-mcp#user-profile)). Sur
`EXPIRED`/`FAIL` : `STOP`, ne jamais retenter automatiquement, ne jamais rejouer une
opération métier interrompue par l'expiration — cette décision reste à la session
appelante, après vérification indépendante.

**Autorisation** : `provision-sudoers.sh` (+ `bootstrap-browser-qa.sh sudoers-install`,
exclu de `all` comme `browser-install`) génère depuis `personas.yaml` exactement une règle
`NOPASSWD` par persona activée — `frappe ALL=(root) NOPASSWD: /usr/bin/systemctl start
browser-qa-refresh@<persona>.service`, rien de plus (pas de shell, pas de wildcard, pas
d'autre verbe `systemctl`). Toujours validée par `visudo -c` avant toute installation ;
jamais écrasée silencieusement si le fichier installé diverge de ce que `personas.yaml`
génère maintenant.

**Concurrence et échecs** : deux demandes simultanées pour la même persona sont fusionnées
par systemd lui-même (le `start` d'une unité déjà en cours de démarrage rejoint le job en
cours, jamais un second login) ; `StartLimitIntervalSec=600`/`StartLimitBurst=3` sur
l'unité `.service` limite tout abus au-delà. `refresh-persona.sh` ne supprime jamais un
storageState valide en cas d'échec du renouvellement (candidat temporaire, rename atomique
uniquement sur succès) — un `EXPIRED`/`FAIL` de `prepare-persona.sh` laisse donc l'état
précédent intact, jamais pire qu'avant l'appel.

`verify-browser-qa.sh` reste strictement read-only et distinct de ce mécanisme.

## Ce que ce répertoire ne fait jamais

- Ne lit, n'affiche, ne journalise jamais une valeur de credential, cookie ou SID.
- Ne permet à aucun agent IA d'exécuter un login réel (`auth/playwright-login.mjs` est
  invoqué exclusivement par `refresh-persona.sh`, lui-même exclusivement par systemd ou
  un opérateur humain à un vrai terminal — jamais par Claude, jamais dans une session
  Claude Code). Claude peut *demander* un renouvellement via `prepare-persona.sh` (règle
  sudo scopée à une seule commande, livrée et prouvée le 2026-09-12) ; le login lui-même
  reste exécuté exclusivement par le service dédié `browserqa-refresh`.
- Ne permet jamais à `credentials-configure.sh` de s'exécuter hors d'un vrai TTY
  interactif — aucun mot de passe n'est jamais provisionné par Claude, y compris pour un
  nouveau persona (voir « Ajouter un nouveau persona » ci-dessus).
- Ne fait jamais dépendre `.mcp.json` actif d'un symlink vers ce checkout.
- Ne référence jamais un nom de persona en dur dans un script commun.
- Ne corrige jamais silencieusement un état invalide détecté par `verify-browser-qa.sh`.
