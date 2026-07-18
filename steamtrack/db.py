"""Acces a la base. Une seule connexion par processus, SQLite en WAL.

Le collecteur ecrit en continu pendant que l'API lit : WAL permet aux deux de
cohabiter sans se bloquer, ce que le mode journal par defaut ne fait pas.
"""

import fcntl
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "steamtrack.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path=None):
    """Ouvre une connexion. N'ECRIT RIEN : ni schema, ni migration.

    C'est le chemin des requetes HTTP : l'API ouvre une connexion par requete.
    Tout DDL pose ici se payait a chaque lecture. Deux defauts constates avant
    d'en sortir le schema et les migrations :

      - `migrate()` appelle dedupe_news, un DELETE sur toute la table changes,
        donc une transaction d'ECRITURE par requete : 19 ms mesures sur 30 000
        news, et surtout un 500 apres 30 s d'attente des que le collecteur
        tenait le verrou d'ecriture. Les 3 workers triplaient la contention.
      - `executescript(SCHEMA)` rejouait tout le DDL par requete, et entrait en
        collision avec la migration d'un autre worker au demarrage
        (`sqlite3.OperationalError: no such column: name` sur /health).

    La creation du schema et les migrations vivent dans `init()`, appele une
    fois par processus au demarrage.
    """
    path = Path(path or os.environ.get("STEAMTRACK_DB", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False : FastAPI execute les endpoints synchrones dans un
    # pool de threads, et libere la connexion depuis un thread different de
    # celui qui l'a ouverte -- ce que sqlite3 refuse par defaut, provoquant des
    # 500 intermittents. Sans danger ici : chaque requete a sa propre connexion,
    # aucune n'est partagee entre threads.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # foreign_keys est un reglage PAR CONNEXION : il etait pose par schema.sql,
    # qu'on ne rejoue plus ici. Sans cette ligne, les contraintes de cle
    # etrangere (api_usage -> api_keys, changes -> apps) ne seraient plus
    # verifiees sur les connexions de l'API. journal_mode=WAL, lui, est un
    # reglage persistant du fichier : il est pose une fois par init().
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path=None):
    """Ouvre la base, cree le schema et applique les migrations.

    A appeler une fois par processus au demarrage : API (evenement startup),
    collecteur et CLI. Renvoie la connexion, utilisable ensuite.

    L'ensemble est serialise par un verrou de fichier : l'API tourne en 3
    workers qui demarrent tous en meme temps, et deux migrations concurrentes se
    marchaient dessus (une requete servie pendant le DROP/RENAME de `changes`
    voyait un schema a mi-chemin -- `no such column: name` sur /health).

    Un verrou SQL ne conviendrait pas : `executescript` emet un COMMIT implicite
    avant de s'executer, il romprait la transaction censee proteger la
    migration. flock est pris sur un fichier voisin, tenu jusqu'a la fin, et
    relache automatiquement si le processus meurt en cours de route.
    """
    from . import auth

    path = Path(path or os.environ.get("STEAMTRACK_DB", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".init-lock")

    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn = connect(path)
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        migrate(conn)
        auth.ensure_anon_row(conn)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return conn


# Colonnes ajoutees apres coup. CREATE TABLE IF NOT EXISTS laisse les bases
# existantes intactes : sans ces ALTER, une base d'avant la modification
# planterait a la premiere requete sur la nouvelle colonne.
MIGRATIONS = [
    ("api_keys", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(conn):
    for table, column, spec in MIGRATIONS:
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    widen_changes_key(conn)
    dedupe_news(conn)


def widen_changes_key(conn):
    """Ajoute occurred_at a la cle d'unicite de `changes`.

    SQLite ne sait pas modifier une contrainte : il faut recreer la table. La
    contrainte d'origine, (appid, change_number, source), ecrasait des
    evenements pourtant distincts -- SteamDB publie plusieurs panneaux sous un
    meme changeid, et l'import en perdait, dont une build.
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='changes'"
    ).fetchone()
    if not sql or "occurred_at)" in sql["sql"].replace(" ", "").replace("\n", ""):
        return
    if "UNIQUE(appid,change_number,source)" not in sql["sql"].replace(" ", "").replace("\n", ""):
        return

    # La table est recreee a la main : executescript() emet un COMMIT implicite,
    # qui romprait la transaction et laisserait la migration a mi-chemin.
    new_table = """
        CREATE TABLE changes_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            appid         INTEGER NOT NULL REFERENCES apps(appid) ON DELETE CASCADE,
            change_number INTEGER,
            kind          TEXT    NOT NULL,
            types         TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            buildid       TEXT,
            occurred_at   TEXT    NOT NULL,
            payload       TEXT    NOT NULL,
            source        TEXT    NOT NULL,
            UNIQUE (appid, change_number, source, occurred_at)
        )"""

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(new_table)
        # `id` est recopie explicitement : l'API l'expose (db.changes_for) et le
        # front s'en sert pour compter les nouveautes. Le laisser a
        # l'AUTOINCREMENT de la nouvelle table renumeroterait tout, et chaque
        # reference externe a un id designerait un autre evenement.
        conn.execute(
            """INSERT OR IGNORE INTO changes_new
                   (id, appid, change_number, kind, types, title, buildid,
                    occurred_at, payload, source)
               SELECT id, appid, change_number, kind, types, title, buildid,
                      occurred_at, payload, source
               FROM changes"""
        )
        conn.execute("DROP TABLE changes")
        conn.execute("ALTER TABLE changes_new RENAME TO changes")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Les index suivaient l'ancienne table : ils ont saute avec le DROP.
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))


def dedupe_news(conn):
    """Garantit l'unicite des annonces au niveau de la base.

    La contrainte UNIQUE (appid, change_number, source) ne couvre pas les news :
    leur change_number est NULL, et en SQL NULL est distinct de NULL, donc la
    contrainte ne se declenche jamais. La deduplication ne tenait que sur une
    verification applicative, non atomique -- deux processus qui initialisent le
    meme jeu en meme temps (collecteur et CLI) inseraient chacun leur copie.

    On nettoie l'existant, puis un index unique partiel empeche la reapparition.

    Les news SANS gid sont exclues du dedoublonnage. Sans ce filtre, toutes
    celles d'un meme app tombent dans une seule partition (json_extract renvoie
    NULL, et PARTITION BY regroupe les NULL entre eux) et toutes sauf une sont
    supprimees definitivement et sans trace. En pratique news.backfill
    renseigne toujours gid, mais un seul item Steam sans gid suffisait a
    declencher la perte. Sans gid il n'existe de toute façon aucun critere
    d'identite fiable : on garde tout.
    """
    conn.execute(
        """DELETE FROM changes WHERE id IN (
               SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY appid, json_extract(payload, '$.gid')
                       ORDER BY id
                   ) AS rang
                   FROM changes
                   WHERE source = 'news'
                     AND json_extract(payload, '$.gid') IS NOT NULL
               ) WHERE rang > 1
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_news_unique
           ON changes (appid, json_extract(payload, '$.gid'))
           WHERE source = 'news'"""
    )


# --- apps ----------------------------------------------------------------

def add_app(conn, appid, name=""):
    """Enregistre un jeu a suivre. Renvoie False s'il l'etait deja."""
    existing = conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO apps (appid, name, added_at) VALUES (?, ?, ?)",
        (appid, name, now()),
    )
    return True


def remove_app(conn, appid):
    """Retire un jeu et tout son historique (cascade). Renvoie ce qui a saute."""
    row = conn.execute("SELECT name FROM apps WHERE appid = ?", (appid,)).fetchone()
    if not row:
        return None
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM changes WHERE appid = ?", (appid,)
    ).fetchone()["n"]
    conn.execute("DELETE FROM apps WHERE appid = ?", (appid,))
    return {"name": row["name"], "changes": count}


def tracked_apps(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM apps ORDER BY appid")]


def tracked_ids(conn):
    return {r["appid"] for r in conn.execute("SELECT appid FROM apps")}


def touch_app(conn, appid, change_number=None, name=None, missing_token=None):
    fields, values = ["last_checked_at = ?"], [now()]
    if change_number is not None:
        fields.append("last_change = ?")
        values.append(change_number)
    if name:
        fields.append("name = ?")
        values.append(name)
    if missing_token is not None:
        fields.append("missing_token = ?")
        values.append(1 if missing_token else 0)
    values.append(appid)
    conn.execute(f"UPDATE apps SET {', '.join(fields)} WHERE appid = ?", values)


# --- snapshots -----------------------------------------------------------

def get_snapshot(conn, appid):
    row = conn.execute("SELECT * FROM snapshots WHERE appid = ?", (appid,)).fetchone()
    return json.loads(row["data"]) if row else None


def put_snapshot(conn, appid, data, change_number=None):
    conn.execute(
        """INSERT INTO snapshots (appid, change_number, data, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(appid) DO UPDATE SET
               change_number = excluded.change_number,
               data = excluded.data,
               updated_at = excluded.updated_at""",
        (appid, change_number, json.dumps(data, sort_keys=True), now()),
    )


# --- changes -------------------------------------------------------------

def add_change(conn, appid, event):
    """Enregistre un evenement. Renvoie False si le doublon existait deja.

    La contrainte UNIQUE fait le tri : un collecteur qui redemarre et rejoue une
    fenetre de changelists ne cree pas de doublons.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO changes
           (appid, change_number, kind, types, title, buildid, occurred_at, payload, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            appid,
            event.get("change_number"),
            event["kind"],
            json.dumps(event.get("types") or [event["kind"]]),
            event["title"],
            event.get("buildid"),
            event.get("occurred_at") or now(),
            json.dumps(event.get("payload") or [], ensure_ascii=False),
            event.get("source", "pics"),
        ),
    )
    return cur.rowcount > 0


def changes_for(conn, appid, limit=100, offset=0, kind=None, since=None):
    sql = "SELECT * FROM changes WHERE appid = ?"
    args = [appid]
    if kind:
        # types contient un tableau JSON : on cherche la categorie dedans, pour
        # qu'un changement mixte reste trouvable sous chacune de ses etiquettes.
        sql += " AND (kind = ? OR types LIKE ?)"
        args += [kind, f'%"{kind}"%']
    if since:
        sql += " AND occurred_at > ?"
        args.append(since)
    sql += " ORDER BY occurred_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [_row_to_event(r) for r in conn.execute(sql, args)]


def recent_changes(conn, limit=100, since=None, kind=None):
    sql = "SELECT * FROM changes WHERE 1 = 1"
    args = []
    if since:
        sql += " AND occurred_at > ?"
        args.append(since)
    if kind:
        sql += " AND (kind = ? OR types LIKE ?)"
        args += [kind, f'%"{kind}"%']
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    args.append(limit)
    return [_row_to_event(r) for r in conn.execute(sql, args)]


def _row_to_event(row):
    return {
        "id": row["id"],
        "appid": row["appid"],
        "change_number": row["change_number"],
        "kind": row["kind"],
        "types": json.loads(row["types"]),
        "title": row["title"],
        "buildid": row["buildid"],
        "occurred_at": row["occurred_at"],
        "source": row["source"],
        "changes": json.loads(row["payload"]),
    }


# --- etat du collecteur --------------------------------------------------

def get_state(conn, name, default=None):
    row = conn.execute("SELECT value FROM state WHERE name = ?", (name,)).fetchone()
    return row["value"] if row else default


def set_state(conn, name, value):
    conn.execute(
        """INSERT INTO state (name, value) VALUES (?, ?)
           ON CONFLICT(name) DO UPDATE SET value = excluded.value""",
        (name, str(value)),
    )
