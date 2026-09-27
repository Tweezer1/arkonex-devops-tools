# dev-link-check — contrôle des liens cassés sur DEV

Outil du lot `OPEN-164-GOV-DEV-BROKEN-LINK-CHECK-001`
([Issue #173](https://github.com/Tweezer1/arkonex-ops-docs/issues/173)).

## Objet

Contrôle **en lecture seule** d'un site Frappe : trouve tous les champs `Link` (natifs
ou Custom Field) dont la valeur pointe vers un document absent de la cible, et compare
ce constat à une **référence versionnée** (`baseline.json`). Ce n'est pas un contrôle
« zéro lien cassé » : c'est un contrôle de **drift** — PASS tant qu'aucun lien cassé
**nouveau** n'apparaît par rapport à la référence connue et acceptée ; FAIL sinon. Il ne
supprime ni ne modifie jamais rien.

## Garantie de lecture seule

Le scan (`link_check.py`) n'appelle que des lectures : `frappe.get_all`,
`frappe.get_meta`, `frappe.db.exists`, `frappe.db.get_single_value`. Aucun `insert`,
`save`, `delete`, `set_value`, `db_set`, `sql` mutatif ni `commit`. Le script termine
explicitement par `frappe.db.rollback()` (ceinture et bretelles : `bench console`
annule aussi ses écritures à la fermeture de session).

Cette garantie se **prouve**, elle ne se déclare pas : relever avant et après les
exécutions les compteurs des journaux qui survivent à une annulation (`Error Log`,
`Deleted Document`, `Version`, `Activity Log`, `Access Log`, `Comment`, avec leur
dernière date de modification) et de quelques DocTypes métier ; ils doivent être
strictement identiques. Preuve du 2026-09-27 sur DEV : 28 compteurs sur 28 identiques
après 6 exécutions (#173).

## Lancer le contrôle

Depuis la racine du dépôt (`arkonex-devops-tools/`) :

```bash
bash dev-link-check/run.sh deverp.arkonex.ca
```

Options : `run.sh <site> [baseline] [report]` — référence et rapport ont des valeurs
par défaut (`dev-link-check/baseline.json`, aucun rapport écrit si non précisé).

## Lire le résultat

```text
LINK_CHECK_STATUS=PASS|FAIL
LINK_CHECK_SUMMARY=<phrase résumant les compteurs>
LINK_CHECK_COUNTERS={"fields_total": ..., "broken_new": ..., ...}
LINK_CHECK_NEW <champ> -> <cible> | <doctype> <nom> | <valeur>   (une ligne par anomalie)
LINK_CHECK_RESOLVED <clé>                                        (une ligne par entrée réparée)
```

- **PASS** : aucun lien cassé hors référence. Des lignes `LINK_CHECK_RESOLVED` peuvent
  apparaître : une entrée de la référence n'est plus trouvée (le lien a été réparé
  entre-temps) — c'est une amélioration, elle n'affecte jamais le statut.
- **FAIL** : au moins une ligne `LINK_CHECK_NEW` — un lien cassé nouveau, absent de la
  référence. Code de sortie 1.
- Code de sortie 2 : erreur (`LINK_CHECK_STATUS=ERROR` avec `LINK_CHECK_ERROR=...`) ou
  statut introuvable dans la sortie de `bench console` — les 40 dernières lignes de
  sortie sont alors affichées pour diagnostic.

## Règle de la référence (`baseline.json`)

La référence n'est **jamais** modifiée en dehors d'un lot explicite :

- une entrée n'en est retirée **que lorsqu'elle est effectivement réparée** (constatée
  `LINK_CHECK_RESOLVED`, puis la référence régénérée et revue) ;
- une entrée n'y est **ajoutée** qu'au sein d'un lot qui l'examine et lui donne sa
  raison (`reason`) — jamais silencieusement pour « faire passer » un contrôle.

Référence initiale : générée sur DEV le 2026-09-27 par cet outil (41 liens distincts sur
10 champs, chacun justifié ; les 3 factures annulées de l'essai OPEN-130 y sont gardées
comme preuve, décision du propriétaire, #173).

## Régénérer une référence fraîche

```bash
LINK_CHECK_WRITE_BASELINE=/chemin/vers/nouvelle-baseline.json \
LINK_CHECK_LOT=OPEN-xxx \
bash dev-link-check/run.sh deverp.arkonex.ca
```

Écrit une référence construite depuis les entrées trouvées lors de ce scan ; les
raisons déjà présentes dans la référence courante sont conservées, toute entrée
nouvelle reçoit la raison par défaut `"À JUSTIFIER"` — à compléter par un humain avant
de l'accepter dans un lot (ne jamais commiter une référence avec des `"À JUSTIFIER"`
non traités).

## Limites

- Les champs `Dynamic Link` (cible déterminée par un autre champ) ne sont **pas**
  couverts — seuls les champs `Link` à cible fixe (`options`) le sont.
- Les DocTypes virtuels (parent ou cible) et les DocTypes `Single` en cible sont
  ignorés (`warnings` du rapport) : ce ne sont pas des documents au sens usuel.
- À lancer quand **aucun essai** (test, canary, scénario Browser QA) ne tourne en
  parallèle sur le même site : un état transitoire (document en cours de création,
  transaction non encore validée) peut produire un `FAIL` qui ne reflète rien de
  durable.

## Où ce contrôle est exigé

À la fermeture de tout lot qui a écrit sur DEV — voir
`GOUVERNANCE-GITHUB-ISSUES.md` §7 et `OPEN-164`
([Issue #173](https://github.com/Tweezer1/arkonex-ops-docs/issues/173)).
