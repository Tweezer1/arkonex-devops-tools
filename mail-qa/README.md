# mail-qa — essais de courriel sur DEV sans envoi possible (OPEN-202, mode M1)

- **Lot** : `OPEN-202-DEVOPS-MAIL-QA-001`, [Issue #351](https://github.com/Tweezer1/arkonex-ops-docs/issues/351).
- **Contrat qui fait foi** : `arkonex-ops-docs/open_202/devops_mail_qa_m1_contrat.md` (v0.3.0 : M1 ratifié le 2026-10-25, tranche T2 au §13).

En cas d'écart, le contrat prévaut sur ce README.

## Ce que fait le dispositif

| Protection | Fichier | Effet |
|---|---|---|
| P1 | `nftables/arkonex-mailqa.nft` | Aucune connexion TCP sortante vers 25, 465, 587 et 2525, en IPv4 et IPv6, pour tous les utilisateurs de la machine (DEV et bancs). Persistant : inclus par `/etc/nftables.conf`, service `nftables` activé au démarrage. |
| P2 | `systemd/mailpit-qa.service` | Mailpit `v1.31.4` (empreinte figée dans `mail-qa.conf`), utilisateur `mailqa`. SMTP sur `127.0.0.1:1025` et interface et API sur `127.0.0.1:8025`. **Aucun relais, aucun transfert automatique, aucun webhook** : ces options sont absentes de la commande, les variables `MP_*` correspondantes sont retirées, et le service ne peut joindre aucune adresse hors de la machine (`IPAddressDeny=any`). Le mode « chaos » sert à simuler une erreur d'envoi. |
| P3 à P6 | (serveur de courriel, compte `qa@arkonex.ca` seulement, fait par le propriétaire) | Le compte QA ne peut ni envoyer, ni programmer de règle de tri, ni se connecter ailleurs que depuis DEV. |

**Mode M1** : ERPNext envoie, Mailpit capture. `mail_qa.py reply` fabrique une réponse **synthétique** et la dépose par IMAP `APPEND` dans la boîte QA. La relève native d'ERPNext la rattache au document d'origine par le fil.

**M2** (vrai échange SMTP) est différé : ni construit, ni testé, ni opérationnel.

**Limite connue** : les services d'envoi par HTTPS ne sont pas bloqués par le réseau. Sont visés Frappe Mail, les API tierces, les webhooks, les SMS et Slack. `verify` les tient par contrôle et la rappelle à chaque exécution.

## Contrôle (lecture seule, tout utilisateur)

```bash
python3 mail-qa/mail_qa.py verify
```

`verify` contrôle :
- P1, par des ouvertures TCP refusées, sans aucun dialogue SMTP ;
- la persistance de P1 ;
- l'unité, les processus et l'API de Mailpit ;
- chaque site de chaque banc de la machine (`/home/frappe/*/sites`) :
  - sortie seulement vers Mailpit ;
  - pas de service Frappe Mail ;
  - pas de crochet `override_email_send` ;
  - boîte entrante QA seulement, et seulement sur `deverp.arkonex.ca`.

Il se termine par `VERIFY_MAIL_QA=PASS` ou `FAIL` (code 0 ou 1).

**Ce que `verify` prouve, et ce qu'il ne prouve pas** :
- **IPv4** : P1 rejette localement par RST. Avec P1 chargé, chaque sonde vers 25, 465, 587 et 2525 est refusée immédiatement, y compris sur 25 et 2525, que le filtrage distant ou AWS laisse autrement sans réponse. Un délai dépassé est donc « non probant » et compte comme FAIL. C'est la preuve, sans droits root, que la règle est chargée dans le noyau.
- **IPv6** : DEV n'a aucune route IPv6 (relu le 2026-10-25). La sortie y est impossible, mais P1 n'y est pas exercé. Il ne l'est que par construction (table `inet`), et c'est signalé en INFO.
- **Bancs** : seuls les bancs de cette machine qui ont `sites/apps.txt` sous `/home/frappe/` sont vus. Un banc ailleurs reste hors garantie.
- **Ports** : seuls 25, 465, 587 et 2525 sont bloqués. Un serveur SMTP sur un autre port reste dans la « limite connue », tenue par contrôle.

## Installation (propriétaire, en root)

Sans `--apply`, rien n'est écrit et le plan est affiché.

```bash
sudo bash mail-qa/install.sh firewall --apply
```

```bash
sudo bash mail-qa/install.sh mailpit --apply
```

## Capacité d'essai des courriels (tranche T2, contrat §13)

L'outil ne connaît **aucun module ni aucun type de document** : il s'appuie sur les mécanismes natifs de Frappe (file d'envoi, Communication, réception native, fil de réponse, `append_to`, permissions du document). Un lot, présent ou futur, l'utilise sans adaptation. Seul le catalogue des parties fictives (`parties.json`, `mail_qa_parties.py`) nomme des clients, fournisseurs et contacts.

Chaque commande existe en ligne de commande et en API Python, avec la même sémantique :

```python
import sys
sys.path.insert(0, "/home/frappe/frappe-bench/arkonex-devops-tools/mail-qa")
import mail_qa
```

Les commandes ci-dessous se lancent depuis `<banc>/sites`, avec le Python du banc visé :

```bash
M="/home/frappe/frappe-bench/env/bin/python -I /home/frappe/frappe-bench/arkonex-devops-tools/mail-qa/mail_qa.py"
B="--bench /home/frappe/frappe-bench --site deverp.arkonex.ca"
```

Pour un banc : son propre Python et `--bench`/`--site` du banc. Seuls `deverp.arkonex.ca` et les sites `*.local` sont permis.

### Règles qui valent pour tous les parcours

- **Préflight imposé.** Toute commande qui écrit (boîte simulée, parties, vidage de la file, dépôt, relève, suppression dans Mailpit) refuse d'agir si `verify` n'est pas `PASS`. Le `PASS` est mis en cache 5 minutes au plus (`~/.cache/arkonex-mail-qa/verify.json`, dossier et fichier à vous seul). Un `FAIL` n'est jamais mis en cache. Le cache n'est pas une frontière de sécurité contre le même utilisateur : qui peut écrire dans son dossier personnel peut y poser un `PASS` ; il protège contre l'erreur, non contre le contournement délibéré. Un cache corrompu est ignoré et `verify` est relancé.
- **Marques et expéditeur.** Tout message fabriqué porte l'en-tête `X-Arkonex-Essai: synthetique` et un expéditeur `<nom>@essai.invalid`, toujours. Le préfixe `[ESSAI SYNTHÉTIQUE]` du sujet et la première ligne du corps sont exigés sauf quand la variante retire justement le sujet ou le corps. Un message fabriqué n'a jamais d'autre expéditeur, ni de `Sender` ou `Reply-To` hors `@essai.invalid`.
- **Identifiant d'essai.** `--essai ID` (ou `mail_qa.new_essai_id()`) étiquette les adresses (`client-a+ID@essai.invalid`) et sépare les lots qui tournent en même temps. Le catalogue, lui, garde des adresses nues et stables.
- **Accord réel et accord simulé : deux choses distinctes.**
  - Un message synthétique **ne prouve jamais un accord réel** ni l'acceptation ou l'approbation d'un vrai tiers. C'est le sens de la marque « aucune valeur d'acceptation ».
  - Il **peut** servir, sur DEV et les bancs, à **exercer le contrôle d'un accord simulé** : un client fictif « ESSAI — … » et son contact fictif du catalogue. Ce scénario est ratifié (#347, échange 23 ; contrat `open_202/` §13.4). Un agent ne doit pas le bloquer au motif qu'un message synthétique « ne vaut pas accord » : il prouve le mécanisme, pas un accord.
  - Le rapport signale chaque message synthétique dont l'expéditeur ou un destinataire est un contact enregistré : « pièce d'accord possible » (voir « Rapport »).
- **Limite connue.** Les services d'envoi par HTTPS ne sont pas bloqués par le réseau ; `verify` les tient par contrôle. Chaque rapport la rappelle.

### Parcours : préparer

```bash
$M parties validate                      # catalogue : préfixe, domaine, adresses uniques, personas
$M parties ensure $B [--company Arkonex] # crée ou complète clients, fournisseurs, contacts (idempotent)
$M inbox ensure $B --party boite-crm --essai ID --append-to <DocType>   # boîte d'entrée simulée
$M address client-a --essai ID           # adresse d'une partie : client-a+ID@essai.invalid
```

- `parties ensure` refuse de lier un contact fictif à un client ou fournisseur dont le nom ne commence pas par `ESSAI — `, et refuse si une adresse du catalogue appartient déjà à un contact lié à un tiers non fictif. Les groupes et le territoire du catalogue doivent exister sur le site.
- Les parties créées par l'outil portent un marqueur dans leur champ « informations » (`customer_details`, `supplier_details`). Une partie existante qui porte un nom du catalogue sans ce marqueur n'est jamais complétée, et un contact lié à une partie sans marqueur fait refuser l'opération. `ensure` est tout ou rien : une erreur annule toutes les écritures.
- Les intervenants internes du catalogue renvoient aux personas de Browser QA par leur nom (`parties show`) ; aucun secret n'est recopié.
- Une boîte simulée n'a ni relève réseau ni envoi (`enable_incoming=0`, `enable_outgoing=0`, `use_imap=0`) ; `verify` reste `PASS`. `--append-to` fait créer un document à la réception d'un message qui ne répond à rien.
- Un banc n'a pas de compte sortant d'office : en créer un vers `127.0.0.1:1025`, sans authentification (`verify` l'accepte).

### Parcours : envoyer et capturer

L'envoi passe par ERPNext (l'action de l'essai). L'outil vide la file à la demande, puis rend ce que Mailpit a capturé :

```bash
$M flush $B --all                        # vide la file « Not Sent » vers Mailpit ; rend le statut de chaque courriel
$M flush $B --name <file> | --since <date>   # ou seulement ce qui est à vous
$M captured $B --essai ID                # destinataires, copie, copie cachée, pièces, Message-ID, document et Communication d'origine
$M captured --to client-a@essai.invalid --subject "Demande" --since 2026-10-25T12:00:00Z
```

- `flush` sans `--name`, `--since` ni `--all` est refusé : la file peut contenir les courriels d'un autre lot.
- La sourdine (`mute_emails`) est respectée ; `flush --force` passe outre (le pare-feu et Mailpit tiennent la barrière).
- Un courriel part en un exemplaire par destinataire : la copie cachée n'apparaît que sur son propre exemplaire.
- `captured` sans `--site` lit Mailpit seul ; avec `--site`, il ajoute l'origine.
- `--to` cherche parmi À, Copie et Copie cachée. Un message étiqueté (une de ses adresses porte `+ID`) est retrouvé par `--essai`.

### Parcours : répondre

```bash
$M reply $B --via inject --account "<boîte>" --mailpit-id <id> --from client-a@essai.invalid --body "Texte" --attach pièce.pdf
```

- `--via inject` : réception native, sans réseau (essais automatiques, bancs, DEV). Le message se rattache au document d'origine par le fil (`In-Reply-To`, qui doit nommer le site : une réponse à un courriel d'un autre site ne se rattache à rien).
- `--via imap` : IMAP `APPEND` dans la boîte QA, **DEV seulement** (pièces jointes seulement sous `IMAP_ATTACH_DIR` de `mail-qa.conf`, 1 Mio au plus chacune) ; il est suivi de `pull` (relève). Gardes M1 inchangées : boîte `qa@arkonex.ca` de `mail.arkonex.ca`, TLS vérifié, site conforme.
- `inject` reste un alias de `reply --via inject`. `--dry-run` affiche le message sans rien déposer.

### Parcours : recevoir un message indépendant

```bash
$M inbound $B --via inject --account "<boîte>" --to <boîte>@essai.invalid --from client-a@essai.invalid --subject "Demande" --attach pièce.pdf
```

Le message ne répond à rien ; avec `append_to`, la boîte crée le document. Destinataire permis : une boîte `@essai.invalid` ou `qa@arkonex.ca`.

### Parcours : messages incomplets ou anormaux

`--variant` (répétable) : `sans-sujet`, `sans-corps`, `sans-piece`, `expediteur-inconnu` (l'outil génère l'expéditeur), `piece-lourde` (`--heavy-bytes`, 5 Mio par défaut), `reponse-inconnue` (le fil mène à un message qui n'existe pas). Une variante inconnue est refusée. Une variante déclarée doit être réellement appliquée : `sans-corps` exige un corps vide, `sans-sujet` un sujet vide, etc.

### Parcours : effets déclenchés

```bash
$M window $B --doctype "<DocType>" -- reply $B --via inject --account "<boîte>" --mailpit-id <id> --from ...
```

```python
with mail_qa.EffectsWindow(frappe, conf, doctypes=("<DocType>",)) as window:
    ...  # l'action
window.report
```

Relève ce qui a été créé depuis le début : courriels en file, Communications, fichiers, ToDo, notifications, documents référencés par les nouvelles Communications, messages Mailpit nouveaux, et les DocTypes passés par l'appelant. Lecture seule. En API, l'action doit tourner dans la même connexion ou avoir été validée avant la fermeture de la fenêtre.

### Parcours : droits

```bash
$M visibility $B --doctype <DocType> --docname <nom> --user a@... --persona sales_manager
$M can-send   $B --doctype <DocType> --docname <nom> --user a@...
```

- `visibility` : ce que chacun voit dans la chronologie du document, comme l'écran la sert (`get_communications` sous l'utilisateur ; `PermissionError` = refus), et s'il peut lire les pièces jointes des Communications (permission `File`). L'isolation entre sociétés n'a pas de règle propre : elle découle des permissions du document.
- `can-send` : droit « courriel » du document **et** tentative réelle d'envoi sous l'utilisateur, dans un point de sauvegarde annulé ; les compteurs de la file et des Communications avant/après prouvent qu'il ne reste rien (`residu`).

### Rapport

`--essai ID --report-dir DIR` ajoute chaque résultat au rapport `DIR/rapport-ID.json` : messages, Communications, liens, contrôles, droits, effets, **ce que l'essai établit** et **ce qu'il n'établit pas**, rappel de la limite HTTPS. Pour chaque message synthétique dont l'expéditeur, ou un destinataire (À, Cc), est l'adresse d'un contact enregistré, il porte `signalements_347` avec `piece_d_accord_possible` et les liens du contact : un tel message peut compter comme pièce d'accord (décision #347, échange 23 ; aucun refus par type de document).

Frappe crée à la réception un contact pour tout expéditeur inconnu : un message suivant du même expéditeur est alors signalé. Les liens du contact disent s'il est rattaché à une partie.

### Rétention

```bash
$M purge --essai ID [--dry-run]
```

Supprime de Mailpit, par identifiants, les seuls messages dont l'ensemble des identifiants d'essai vaut exactement `{ID}`. Ces identifiants sont lus dans les étiquettes `+ID` de toutes les adresses et dans l'en-tête `X-Arkonex-Essai-Id` (posé sur tout message fabriqué quand un essai est donné). Un message qui porte aussi l'identifiant d'un autre essai n'est jamais supprimé, ni par l'un ni par l'autre ; `captured --essai` suit la même règle. Le parcours couvre tous les messages de Mailpit. Pour qu'un envoi d'ERPNext soit retrouvé, adresser au moins un destinataire étiqueté (par exemple en copie). Un envoi sans étiquette se repère par la fenêtre d'effets (`mailpit_nouveaux`).

### Boîte QA, DEV seulement

```bash
$M reply $B --via imap --mailpit-id <id> --body "Texte"
$M pull  $B
```

`reply --via imap` et `pull` n'agissent que sur la boîte `qa@arkonex.ca` de `mail.arkonex.ca` et seulement sur `deverp.arkonex.ca`. Ne pas les lancer depuis un banc.

## Retour arrière (contrat §10)

- **Normal**, vers un état sécurisé :

  ```bash
  sudo bash mail-qa/install.sh mailpit-stop --apply
  ```

  Puis, sur DEV : `mute_emails=1`, Email Account QA désactivé et mot de passe effacé. **P1 reste.**
- **Décision humaine distincte** pour retirer P1 (`nft delete table inet arkonex_mailqa`, retrait du fichier et de la ligne `include`) ou pour rendre à QA ses droits d'avant.

## Essais

```bash
python3 -m unittest discover -s mail-qa/tests -v
```

Ces essais n'utilisent ni réseau extérieur ni Frappe (le site est remplacé par une doublure). Ils couvrent :
- les marques, les variantes et les adresses de parties (C1, C5, C6) ;
- le préflight imposé, son cache et la garde de site ;
- les règles des sites et de Mailpit, les fichiers P1 et P2, la persistance, le mode plan d'`install.sh` ;
- le signalement des contacts, le rapport, le catalogue, les gardes de `parties ensure`, la fenêtre d'effets, les droits, la rétention ;
- l'absence de tout nom de DocType métier dans `mail_qa.py` (seuls client, fournisseur et contact apparaissent, dans `mail_qa_parties.py`).

Des mutations ont été vérifiées en rouge. L'essai d'intégration se fait sur un banc libre de tout autre lot, jamais sur DEV en écriture.
