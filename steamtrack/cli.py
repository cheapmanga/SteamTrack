"""Ligne de commande : ajouter, retirer et inspecter les jeux suivis.

    steamtrack add 730                 par appid
    steamtrack add "Elden Ring"        par nom, avec choix si ambigu
    steamtrack list
    steamtrack remove 730              demande confirmation
    steamtrack show 730
    steamtrack key add "bot discord" --quota 1000
"""

import argparse
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import db

SEARCH_URL = ("https://store.steampowered.com/api/storesearch/"
              "?term={term}&l=english&cc=US")
USER_AGENT = "steamtrack/1.0"


# --- helpers -------------------------------------------------------------

def resolve(term):
    """Trouve des apps par nom. Renvoie [(appid, nom), ...]."""
    url = SEARCH_URL.format(term=urllib.parse.quote(term))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"recherche impossible : {exc}", file=sys.stderr)
        return []
    return [(item["id"], item.get("name", "?")) for item in data.get("items", [])]


def pick(term):
    """Resout un argument en appid : numerique tel quel, sinon recherche."""
    if re.fullmatch(r"\d+", term):
        return int(term), None

    matches = resolve(term)
    if not matches:
        print(f"aucun jeu trouve pour {term!r}")
        return None, None
    if len(matches) == 1:
        return matches[0]

    print(f"{len(matches)} resultats pour {term!r} :")
    for i, (appid, name) in enumerate(matches[:10], 1):
        print(f"  {i:2d}. {name}  (appid {appid})")
    try:
        choice = input("numero (entree = annuler) : ").strip()
    except EOFError:
        return None, None
    if not choice.isdigit() or not 1 <= int(choice) <= len(matches[:10]):
        print("annule")
        return None, None
    return matches[int(choice) - 1]


def human(iso):
    if not iso:
        return "-"
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = int(delta.total_seconds())
    for size, label in ((86400, "j"), (3600, "h"), (60, "min")):
        if secs >= size:
            return f"il y a {secs // size} {label}"
    return "a l'instant"


# --- commandes -----------------------------------------------------------

def cmd_add(conn, args):
    appid, name = pick(args.game)
    if appid is None:
        return 1

    if not db.add_app(conn, appid, name or ""):
        print(f"app {appid} deja suivie")
        return 0

    print(f"app {appid} ajoutee" + (f" ({name})" if name else ""))
    print("  recuperation de l'etat courant et des annonces...")

    # Le bootstrap demande Steam : on l'importe ici pour que `list` et `remove`
    # restent utilisables sans dependance au client.
    from .collector import Collector

    collector = Collector(conn)
    try:
        collector.connect()
        result = collector.bootstrap(appid)
    except Exception as exc:                              # noqa: BLE001
        print(f"  etat courant indisponible ({exc})")
        print("  le jeu est suivi : le collecteur completera au prochain demarrage.")
        return 0

    if not result:
        print("  appinfo introuvable : appid valide ?")
        return 0

    print(f"  nom      : {result['name']}")
    print(f"  annonces : {result['news']} enregistrees")
    if result["missing_token"]:
        print("  note : cet app ne publie pas ses depots (jeu non sorti ?),")
        print("         les builds seront detectees sans leur buildid.")
    print()
    print("  L'historique demarre aujourd'hui : Steam ne conserve pas les")
    print("  changements passes. Les prochains seront captes en temps reel.")
    return 0


def cmd_remove(conn, args):
    appid, _ = pick(args.game)
    if appid is None:
        return 1

    row = conn.execute("SELECT name FROM apps WHERE appid = ?", (appid,)).fetchone()
    if not row:
        print(f"app {appid} n'est pas suivie")
        return 1

    count = conn.execute("SELECT COUNT(*) AS n FROM changes WHERE appid = ?",
                         (appid,)).fetchone()["n"]
    if not args.yes:
        label = row["name"] or appid
        print(f"Supprimer {label} et ses {count} evenements ? Cette action est definitive.")
        try:
            if input("tapez 'oui' pour confirmer : ").strip().lower() not in ("oui", "o", "yes", "y"):
                print("annule")
                return 0
        except EOFError:
            print("annule")
            return 0

    removed = db.remove_app(conn, appid)
    print(f"{removed['name'] or appid} supprime ({removed['changes']} evenements)")
    return 0


def cmd_list(conn, args):
    apps = db.tracked_apps(conn)
    if not apps:
        print("aucun jeu suivi. Ajoutez-en un : steamtrack add 730")
        return 0

    print(f"{'APPID':>8}  {'NOM':32}  {'EVENTS':>6}  {'DERNIER':>14}  ETAT")
    print("-" * 82)
    for app in apps:
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MAX(occurred_at) AS last FROM changes WHERE appid = ?",
            (app["appid"],),
        ).fetchone()
        flag = "token manquant" if app["missing_token"] else ""
        name = (app["name"] or "?")[:32]
        print(f"{app['appid']:>8}  {name:32}  {stats['n']:>6}  "
              f"{human(stats['last']):>14}  {flag}")
    return 0


def cmd_show(conn, args):
    appid, _ = pick(args.game)
    if appid is None:
        return 1
    events = db.changes_for(conn, appid, limit=args.limit, kind=args.kind)
    if not events:
        print("aucun evenement enregistre")
        return 0
    for e in events:
        print(f"\n[{e['kind']}] {e['occurred_at'][:19]}  #{e['change_number'] or '-'}")
        print(f"  {e['title']}")
        if e["source"] == "news":
            body = (e["changes"] or {}).get("body", "")
            print("  " + body[:200].replace("\n", " "))
        else:
            _print_tree(e["changes"], 2)
    return 0


def _print_tree(nodes, indent):
    for node in nodes or []:
        bullet = {"added": "+", "removed": "-", "modified": "~"}.get(node.get("op"), " ")
        line = "".join(s["v"] for s in node.get("seg", []))
        print(" " * indent + f"{bullet} {line}")
        _print_tree(node.get("children"), indent + 2)


def cmd_key(conn, args):
    if args.key_action == "add":
        key = "st_" + secrets.token_urlsafe(24)
        conn.execute(
            """INSERT INTO api_keys (key, label, quota_per_hour, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, args.label, args.quota, 1 if args.admin else 0,
             datetime.now(timezone.utc).isoformat()),
        )
        print(f"cle creee : {key}")
        print(f"  quota : {args.quota if args.quota else 'illimite'} requetes/heure")
        if args.admin:
            print("  administrateur : peut ajouter et supprimer des jeux")
        return 0

    if args.key_action == "list":
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        if not rows:
            print("aucune cle")
            return 0
        for r in rows:
            quota = r["quota_per_hour"] or "illimite"
            state = "revoquee" if r["revoked_at"] else "active"
            print(f"  {r['key']}  {r['label']:20}  {str(quota):>9}/h  {state}")
        return 0

    if args.key_action == "revoke":
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE key = ?",
                     (datetime.now(timezone.utc).isoformat(), args.label))
        print("cle revoquee")
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(prog="steamtrack", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="suivre un jeu (appid ou nom)")
    p.add_argument("game")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="ne plus suivre un jeu et effacer son historique")
    p.add_argument("game")
    p.add_argument("-y", "--yes", action="store_true", help="sans confirmation")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="lister les jeux suivis")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="derniers evenements d'un jeu")
    p.add_argument("game")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--kind")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("key", help="gerer les cles d'API")
    p.add_argument("key_action", choices=["add", "list", "revoke"])
    p.add_argument("label", nargs="?", default="")
    p.add_argument("--quota", type=int, default=None,
                   help="requetes par heure ; omis = illimite")
    p.add_argument("--admin", action="store_true",
                   help="autorise l'ajout et la suppression de jeux via l'API")
    p.set_defaults(func=cmd_key)

    args = ap.parse_args()
    conn = db.connect(args.db)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
