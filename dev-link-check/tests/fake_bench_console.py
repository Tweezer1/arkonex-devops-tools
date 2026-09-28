#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Imitation minimale de `bench --site <site> console` pour les essais (OPEN-168).

Utilisé de deux façons :
- importé par les essais, pour sa classe `FakeFrappe` (exécution de link_check.py
  dans le processus de test) ;
- lancé comme script par un faux exécutable `bench` : lit sur l'entrée standard la
  ligne que `run.sh` envoie à la console (`exec(open(...).read(), globals())`) et
  l'exécute avec un faux `frappe` dans l'espace de noms. Le faux `bench` se place au
  préalable dans `sites/`, comme le vrai (`run_frappe_cmd`, cwd=sites).

Le faux `frappe` ne connaît aucun champ Link : le parcours du site ne trouve donc
aucun lien cassé. Chaque appel de lecture est noté dans `calls`, pour vérifier
qu'une référence introuvable arrête le contrôle avant tout parcours.
"""

import sys
import types


class FakeDB:

    def __init__(self, calls):
        self.calls = calls

    def exists(self, doctype, name):
        self.calls.append(("db.exists", doctype))
        return True

    def get_single_value(self, doctype, fieldname):
        self.calls.append(("db.get_single_value", doctype))
        return None

    def rollback(self):
        pass


class FakeFrappe:

    def __init__(self):
        self.calls = []
        self.db = FakeDB(self.calls)
        self.local = types.SimpleNamespace(site="fake.site")

    def get_all(self, doctype, **kwargs):
        self.calls.append(("get_all", doctype))
        return []

    def get_meta(self, doctype):
        self.calls.append(("get_meta", doctype))
        raise LookupError(doctype)


def main():
    code = sys.stdin.read()
    # Comme IPython sans terminal : l'invite précède la première ligne de sortie.
    sys.stdout.write("In [1]: ")
    exec(code, {"frappe": FakeFrappe(), "__name__": "__console__"})


if __name__ == "__main__":
    main()
