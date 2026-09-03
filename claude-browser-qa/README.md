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

**PHASE A uniquement** (2026) — cette source versionnée existe, ses tests non destructifs
passent. **Rien n'est activé sur l'instance DEV** : aucune écriture `/etc/hosts`, aucun
utilisateur système créé, aucun répertoire `/etc/arkonex/`ou `/var/lib/arkonex-browser-qa/`
créé, aucune unité systemd installée, aucun navigateur installé, aucune credential créée,
aucun login E2E réel effectué, `.mcp.json` actif du bench **non touché**. Un GO humain
distinct est requis pour toute activation runtime (voir Issue #87).

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
| `bootstrap-browser-qa.sh` | orchestrateur idempotent (`dns`/`provision-identity`/`deploy`/`generate-config`/`browser-install`/`systemd-install`/`all`), **plan par défaut, `--apply` explicite requis** | fonctions non destructives testées uniquement |
| `verify-browser-qa.sh` | vérificateur read-only, ne corrige jamais | non exécuté contre l'instance réelle en Phase A |
| `auth/package.json` / `auth/package-lock.json` | dépendance Node reproductible (`npm ci`), `playwright` pinné exactement à la version déjà contractée avec `@playwright/mcp` | lockfile généré et vérifié |
| `auth/refresh-persona.sh` | orchestrateur générique de refresh (un seul fichier, `%i` = persona), fixe mode 0640 + groupe partagé sur le storageState **avant** le rename atomique | non exécuté (requiert credentials réels) |
| `auth/playwright-login.mjs` | login Frappe réel + capture storageState | non exécuté |
| `auth/validate-storage-state.mjs` | preuve de session authentifiée sans jamais afficher le cookie | non exécuté (requiert un storageState réel) |
| `auth/browser-qa-refresh@.service` | unité systemd template (oneshot), `SupplementaryGroups=browserqa-storage` | non installée |
| `auth/browser-qa-refresh@.timer` | unité systemd template (cadence D7) | non installée |

## Version MCP

```
@playwright/mcp@0.0.80 — pin exact, jamais @latest.
Pin lui-même playwright/playwright-core en exact (1.63.0-alpha-2026-08-31 au moment de ce
design) — pinner @playwright/mcp suffit à fixer toute la chaîne de dépendance.
```
Toute montée de version = changement contrôlé et documenté séparément (jamais une
découverte/installation opportuniste pendant une session Browser QA).

## Installation du navigateur — statut explicite

```
BROWSER_INSTALL_DESIGN=RATIFIED
BROWSER_INSTALL_EXACT_COMMAND=PENDING_RUNTIME_CANARY
```
`bootstrap-browser-qa.sh browser-install` existe mais retourne délibérément un statut
« non contractualisé » (code de sortie 3) tant que le futur canary R2 n'a pas prouvé la
commande exacte supportée par la version pinnée (séquence de preuve : cache contrôlé →
install → révision relevée → rebootstrap → 0 nouveau téléchargement → seulement alors
documentation canonique).

## Cadence du timer (D7, ratifiée)

```
OnBootSec=5min
OnUnitActiveSec=6h
Persistent=true
RandomizedDelaySec=5min
```
Objectif : maintenir les sessions fraîches sans dépendre d'une mesure réelle de
`session_expiry`. Réévaluation = changement contrôlé séparé après mesure réelle, jamais
un ajustement silencieux.

## Ce que ce répertoire ne fait jamais

- Ne lit, n'affiche, ne journalise jamais une valeur de credential, cookie ou SID.
- Ne permet à aucun agent IA d'exécuter un login réel (`auth/playwright-login.mjs` est
  invoqué exclusivement par `refresh-persona.sh`, lui-même exclusivement par systemd ou
  un opérateur humain à un vrai terminal — jamais par Claude, jamais dans une session
  Claude Code).
- Ne fait jamais dépendre `.mcp.json` actif d'un symlink vers ce checkout.
- Ne référence jamais un nom de persona en dur dans un script commun.
- Ne corrige jamais silencieusement un état invalide détecté par `verify-browser-qa.sh`.
