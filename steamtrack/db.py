"""Acces a la base. Une seule connexion par processus, SQLite en WAL.

Le collecteur ecrit en continu pendant que l'API lit : WAL permet aux deux de
cohabiter sans se bloquer, ce que le mode journal par defaut ne fait pas.
"""

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
    """Ouvre la base et applique le schema (idempotent)."""
    path = Path(path or os.environ.get("STEAMTRACK_DB", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    migrate(conn)
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
    dedupe_news(conn)


def dedupe_news(conn):
    """Garantit l'unicite des annonces au niveau de la base.

    La contrainte UNIQUE (appid, change_number, source) ne couvre pas les news :
    leur change_number est NULL, et en SQL NULL est distinct de NULL, donc la
    contrainte ne se declenche jamais. La deduplication ne tenait que sur une
    verification applicative, non atomique -- deux processus qui initialisent le
    meme jeu en meme temps (collecteur et CLI) inseraient chacun leur copie.

    On nettoie l'existant, puis un index unique partiel empeche la reapparition.
    """
    conn.execute(
        """DELETE FROM changes WHERE id IN (
               SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY appid, json_extract(payload, '$.gid')
                       ORDER BY id
                   ) AS rang
                   FROM changes WHERE source = 'news'
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
