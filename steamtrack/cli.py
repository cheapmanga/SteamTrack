"""Ligne de commande : ajouter, retirer et inspecter les jeux suivis.

    steamtrack add 730                 par appid
    steamtrack add "Elden Ring"        par nom, avec choix si ambigu
    steamtrack list
    steamtrack remove 730              demande confirmation
    steamtrack show 730
    steamtrack key add "bot discord" --quota 1000
    steamtrack key add "moi" --admin    cle illimitee et administrateur
    steamtrack key list
    steamtrack key show st_xxx          consommation de l'heure et quota restant
    steamtrack key revoke st_xxx
"""

import argparse
import json
import pathlib
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


def cmd_import(conn, args):
    from . import steamdb_import

    appid, _ = pick(args.game)
    if appid is None:
        return 1
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        print(f"app {appid} n'est pas suivie. Ajoutez-la d'abord : steamtrack add {appid}")
        return 1

    path = pathlib.Path(args.html)
    if not path.exists():
        print(f"fichier introuvable : {path}")
        return 1

    print(f"import de {path.name} pour l'app {appid}...")
    try:
        imported, skipped = steamdb_import.import_history(conn, appid, path)
    except SystemExit as exc:
        print(f"  {exc}")
        return 1

    print(f"  {imported} evenement(s) importe(s), {skipped} deja connu(s)")
    if imported:
        oldest = conn.execute(
            "SELECT MIN(occurred_at) t FROM changes WHERE appid = ? AND source = 'import'",
            (appid,)).fetchone()["t"]
        print(f"  l'historique remonte desormais au {oldest[:10]}")
    return 0


def cmd_link(conn, args):
    """Declare une app apparentee que la decouverte automatique ne trouve pas."""
    appid, _ = pick(args.game)
    if appid is None:
        return 1
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        print(f"app {appid} n'est pas suivie")
        return 1

    conn.execute(
        """INSERT INTO related_links (appid, related_appid, kind, label, added_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(appid, related_appid) DO UPDATE SET
               kind = excluded.kind, label = excluded.label""",
        (appid, args.related, args.kind, args.label,
         datetime.now(timezone.utc).isoformat()),
    )
    print(f"  {args.related} liee a {appid} [{args.kind}]"
          + (f' "{args.label}"' if args.label else ""))
    return 0


def cmd_unlink(conn, args):
    appid, _ = pick(args.game)
    if appid is None:
        return 1
    n = conn.execute("DELETE FROM related_links WHERE appid = ? AND related_appid = ?",
                     (appid, args.related)).rowcount
    print(f"  {n} lien supprime")
    return 0


def cmd_reclean(conn, args):
    from . import news

    appid = None
    if args.game:
        appid, _ = pick(args.game)
        if appid is None:
            return 1
    fixed = news.reclean(conn, appid)
    print(f"{fixed} annonce(s) renettoyee(s)")
    return 0


def _resolve_key(conn, needle):
    """Retrouve une cle depuis sa valeur exacte ou son libelle.

    Accepter le libelle evite d'avoir a recopier un jeton de 32 caracteres pour
    revoquer une cle qu'on vient de nommer.
    """
    row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (needle,)).fetchone()
    if row:
        return row
    rows = conn.execute("SELECT * FROM api_keys WHERE label = ?", (needle,)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        print(f"{len(rows)} cles portent le libelle {needle!r} : designez-la par sa valeur")
        for r in rows:
            print(f"  {r['key']}")
        return None
    print(f"aucune cle ne correspond a {needle!r}")
    return None


def cmd_key(conn, args):
    from . import auth

    if args.key_action == "add":
        if not args.label:
            print("un libelle est requis : steamtrack key add \"mon bot\"")
            return 1
        key = "st_" + secrets.token_urlsafe(24)
        # --admin sans --quota = la cle du proprietaire : quota_per_hour reste
        # NULL, ce que auth.consume interprete comme illimite.
        conn.execute(
            """INSERT INTO api_keys (key, label, quota_per_hour, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, args.label, args.quota, 1 if args.admin else 0,
             datetime.now(timezone.utc).isoformat()),
        )
        print(f"cle creee : {key}")
        print(f"  libelle : {args.label}")
        if args.quota is None:
            print("  quota   : illimite (aucun comptage, aucun 429)")
        else:
            print(f"  quota   : {args.quota} requetes/heure")
        if args.admin:
            print("  droits  : administrateur — peut ajouter et supprimer des jeux")
        else:
            print("  droits  : lecture seule")
        print()
        print("  Utilisation : en-tete  X-API-Key: " + key)
        print("  Cette cle ne sera plus reaffichee en entier ailleurs : notez-la.")
        return 0

    if args.key_action == "list":
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        if not rows:
            print("aucune cle")
            return 0
        print(f"  {'CLE':38}  {'LIBELLE':24}  {'QUOTA':>10}  {'DROITS':6}  ETAT")
        print("  " + "-" * 92)
        anon = 0
        for r in rows:
            # Les seaux anonymes (un par IP) ne sont pas des cles : on les
            # resume en une ligne au lieu d'inonder le tableau.
            if r["key"].startswith(auth.ANON_KEY + ":"):
                anon += 1
                continue
            quota = "illimite" if r["quota_per_hour"] is None else f"{r['quota_per_hour']}/h"
            rights = "admin" if r["is_admin"] else "lecture"
            state = f"revoquee ({r['revoked_at'][:10]})" if r["revoked_at"] else "active"
            # Une cle illimitee n'est jamais comptee : afficher 0 laisserait
            # croire qu'elle ne sert pas.
            suffix = ("  [non compte]" if r["quota_per_hour"] is None
                      else f"  [{auth.usage(conn, r['key'])} cette heure]")
            if r["key"] == auth.ANON_KEY:
                state = "politique anonyme"
                suffix = ""
            print(f"  {r['key']:38}  {r['label'][:24]:24}  {quota:>10}  "
                  f"{rights:6}  {state}{suffix}")
        print()
        print(f"  anonyme : {auth.ANON_QUOTA} requetes/heure et par IP, "
              f"{anon} IP active(s) sur la fenetre en cours")
        return 0

    if args.key_action == "show":
        row = _resolve_key(conn, args.label)
        if row is None:
            return 1
        hits = auth.usage(conn, row["key"])
        print(f"cle     : {row['key']}")
        print(f"libelle : {row['label']}")
        print(f"droits  : {'administrateur' if row['is_admin'] else 'lecture seule'}")
        print(f"etat    : {'revoquee le ' + row['revoked_at'][:19] if row['revoked_at'] else 'active'}")
        print(f"creee   : {row['created_at'][:19]}")
        print()
        print(f"fenetre : {auth.current_hour()}:00 UTC")
        print(f"utilise : {hits} requete(s) dans l'heure en cours")
        if row["quota_per_hour"] is None:
            print("restant : illimite — cette cle ne recevra jamais de 429")
        else:
            remaining = max(0, row["quota_per_hour"] - hits)
            print(f"quota   : {row['quota_per_hour']} requetes/heure")
            print(f"restant : {remaining}")
            if remaining == 0:
                print("  -> le quota est epuise : les appels renvoient 429 jusqu'a la remise a zero")
        print(f"remise a zero : {auth.next_hour_reset()[:19]} UTC "
              f"(dans {auth.seconds_until_reset() // 60} min)")
        if row["revoked_at"]:
            print()
            print("Attention : cette cle est revoquee, ses appels renvoient 401.")
        return 0

    if args.key_action == "revoke":
        row = _resolve_key(conn, args.label)
        if row is None:
            return 1
        if row["key"] == auth.ANON_KEY:
            print("la ligne anonyme n'est pas une cle : ajustez auth.ANON_QUOTA")
            return 1
        if row["revoked_at"]:
            print(f"cle deja revoquee le {row['revoked_at'][:19]}")
            return 0
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE key = ?",
                     (datetime.now(timezone.utc).isoformat(), row["key"]))
        print(f"cle revoquee : {row['key']} ({row['label']})")
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

    p = sub.add_parser("link", help="declarer une app apparentee (alpha fermee, playtest...)")
    p.add_argument("game", help="appid ou nom du jeu")
    p.add_argument("related", type=int, help="appid de l'app apparentee")
    p.add_argument("--kind", default="related",
                   help="demo, playtest, alpha, beta, dlc, soundtrack...")
    p.add_argument("--label", help="nom a afficher (une app a jeton n'en publie aucun)")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="retirer une app apparentee declaree")
    p.add_argument("game")
    p.add_argument("related", type=int)
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("import", help="importer un historique SteamDB (page History en HTML)")
    p.add_argument("game", help="appid ou nom du jeu")
    p.add_argument("html", help="page History de SteamDB sauvegardee")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("reclean", help="repasser le nettoyage BBCode sur les annonces")
    p.add_argument("game", nargs="?")
    p.set_defaults(func=cmd_reclean)

    p = sub.add_parser("key", help="gerer les cles d'API")
    p.add_argument("key_action", choices=["add", "list", "show", "revoke"])
    # Un seul positionnel pour les quatre actions : c'est un libelle pour `add`,
    # et une cle (ou le libelle qui la designe) pour `show` et `revoke`.
    p.add_argument("label", nargs="?", default="", metavar="LABEL|CLE",
                   help="add : libelle de la cle ; show/revoke : la cle ou son libelle")
    p.add_argument("--quota", type=int, default=None,
                   help="requetes par heure ; omis = illimite")
    p.add_argument("--admin", action="store_true",
                   help="autorise l'ajout et la suppression de jeux via l'API")
    p.set_defaults(func=cmd_key)

    args = ap.parse_args()
    # init() et non connect() : la CLI est un point d'entree, c'est ici que les
    # migrations doivent tourner (l'API ne migre plus par requete).
    conn = db.init(args.db)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
