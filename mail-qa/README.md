# mail-qa — essais de courriel sur DEV sans envoi possible (OPEN-202, mode M1)

- **Lot** : `OPEN-202-DEVOPS-MAIL-QA-001`, [Issue #351](https://github.com/Tweezer1/arkonex-ops-docs/issues/351).
- **Contrat qui fait foi** : `arkonex-ops-docs/open_202/devops_mail_qa_m1_contrat.md` (v0.2.0, ratifié le 2026-10-25).

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

## Utilisation par les agents (lot autorisé)

ERPNext sur DEV : l'Email Account « QA Arkonex » sort vers `127.0.0.1:1025`, sans authentification. Il relève `qa@arkonex.ca` en IMAP sur `mail.arkonex.ca:993`. Le mot de passe est saisi par le propriétaire.

Les essais automatiques des lots et les bancs injectent un courriel brut dans la réception native :

```bash
/home/frappe/frappe-bench/env/bin/python -I /home/frappe/frappe-bench/arkonex-devops-tools/mail-qa/mail_qa.py inject --site <site> --account <Email Account> --mailpit-id <id>
```

DEV seulement, dépôt dans la boîte QA, puis relève :

```bash
/home/frappe/frappe-bench/env/bin/python -I /home/frappe/frappe-bench/arkonex-devops-tools/mail-qa/mail_qa.py reply --site deverp.arkonex.ca --mailpit-id <id> --body "Texte" --attach chemin.pdf
```

```bash
/home/frappe/frappe-bench/env/bin/python -I /home/frappe/frappe-bench/arkonex-devops-tools/mail-qa/mail_qa.py pull --site deverp.arkonex.ca
```

`reply --dry-run` affiche le message sans rien déposer. Lancer ces commandes depuis `/home/frappe/frappe-bench/sites`.

**Messages synthétiques** : chacun porte quatre marques, et l'outil refuse d'en produire un sans elles :
- l'en-tête `X-Arkonex-Essai: synthetique` ;
- le sujet préfixé par `[ESSAI SYNTHÉTIQUE]` ;
- en première ligne : « Message d'essai fabriqué — aucune valeur d'acceptation » ;
- un expéditeur en `@essai.invalid`.

Un tel message ne vaut jamais acceptation ni approbation externe.

**Gardes de l'outil** :
- `reply` et `pull` n'agissent que sur la boîte `qa@arkonex.ca` de `mail.arkonex.ca`, et seulement sur `deverp.arkonex.ca`. Le TLS IMAP vérifie le certificat.
- `reply`, `inject` et `pull` refusent un site dont la messagerie n'est pas conforme.
- **Règle de #347** : aucun message synthétique ne doit être rattaché à une Purchase Order, un Project ou un Contract, sinon il vaudrait pièce d'accord pour le contrôle de T1 d'OPEN-200. `reply` et `inject` refusent de répondre à un tel message. `inject` annule si la Communication créée y serait rattachée, et `pull` le signale (code 1).

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

Ces essais n'utilisent aucun réseau extérieur. Ils couvrent :
- les quatre marques ;
- les règles des sites et de Mailpit ;
- les fichiers P1 et P2 ;
- la persistance ;
- le mode plan d'`install.sh`.

Des mutations ont été vérifiées en rouge.
