"""Cles d'API et quotas.

Trois niveaux :
  - cle absente        -> quota anonyme, genereux mais borne, pour essayer l'API ;
  - cle avec quota     -> limite horaire propre a la cle ;
  - cle sans quota     -> illimite (les tiennes, et les invites de confiance).

La consommation est comptee par cle et par heure dans api_usage : une ligne par
heure et par cle, plutot qu'un journal de requetes qui grossirait sans fin.
"""

from datetime import datetime, timedelta, timezone

# Sans cle, on autorise assez pour tester l'API mais pas pour s'en servir en
# production : c'est ce qui pousse a demander une cle plutot qu'a scraper.
ANON_QUOTA = 60
ANON_KEY = "anon"


def current_hour():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def next_hour_reset():
    now = datetime.now(timezone.utc)
    return (now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)).isoformat()


def lookup(conn, key):
    """Resout une cle. Renvoie None si elle est inconnue ou revoquee."""
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key = ? AND revoked_at IS NULL", (key,)
    ).fetchone()
    return dict(row) if row else None


def consume(conn, key, quota):
    """Incremente la consommation. Renvoie (autorise, restant, limite).

    quota None = illimite : on ne compte meme pas, pour ne pas ecrire a chaque
    requete sur une cle qui n'a de toute façon pas de plafond.
    """
    if quota is None:
        return True, None, None

    hour = current_hour()
    conn.execute(
        """INSERT INTO api_usage (key, hour, hits) VALUES (?, ?, 1)
           ON CONFLICT(key, hour) DO UPDATE SET hits = hits + 1""",
        (key, hour),
    )
    hits = conn.execute(
        "SELECT hits FROM api_usage WHERE key = ? AND hour = ?", (key, hour)
    ).fetchone()["hits"]

    # Purge des fenetres passees : sans cela la table grossit indefiniment.
    if hits % 200 == 0:
        conn.execute("DELETE FROM api_usage WHERE hour < ?", (hour,))

    return hits <= quota, max(0, quota - hits), quota


def authenticate(conn, key):
    """Identifie l'appelant et applique son quota.

    Renvoie un dict decrivant l'appelant, ou leve ValueError si la cle est
    invalide, et PermissionError si le quota est depasse.
    """
    if key:
        record = lookup(conn, key)
        if not record:
            raise ValueError("cle inconnue ou revoquee")
        allowed, remaining, limit = consume(conn, key, record["quota_per_hour"])
        caller = {
            "key": key,
            "label": record["label"],
            "admin": bool(record["is_admin"]),
            "remaining": remaining,
            "limit": limit,
        }
    else:
        allowed, remaining, limit = consume(conn, ANON_KEY, ANON_QUOTA)
        caller = {
            "key": None,
            "label": "anonymous",
            "admin": False,
            "remaining": remaining,
            "limit": limit,
        }

    if not allowed:
        # La limite voyage avec l'erreur : l'API la renvoie dans ses en-tetes.
        raise PermissionError("quota horaire depasse", limit)
    return caller


def ensure_anon_row(conn):
    """La consommation anonyme reference une cle : on la cree une fois.

    api_usage a une contrainte de cle etrangere vers api_keys ; sans cette
    ligne, la premiere requete anonyme echouerait.
    """
    conn.execute(
        """INSERT OR IGNORE INTO api_keys (key, label, quota_per_hour, is_admin, created_at)
           VALUES (?, 'anonymous (partage)', ?, 0, ?)""",
        (ANON_KEY, ANON_QUOTA, datetime.now(timezone.utc).isoformat()),
    )
