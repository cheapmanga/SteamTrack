"""Sondes periodiques : frequentation et prix.

PICS decrit ce qu'est un jeu, pas comment il se porte. Ces deux mesures
viennent d'autres API Steam, et surtout : elles ne se rattrapent pas. Un releve
manque est perdu pour toujours, personne ne republie le nombre de joueurs
d'hier. C'est pourquoi le collecteur les prend regulierement des le premier
jour, meme si le graphique est vide au debut.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("probes")

PLAYERS_URL = ("https://api.steampowered.com/ISteamUserStats/"
               "GetNumberOfCurrentPlayers/v1/?appid={appid}")
DETAILS_URL = ("https://store.steampowered.com/api/appdetails"
               "?appids={appid}&cc={cc}&l=english")
USER_AGENT = "steamtrack/1.0"

# Devise de reference pour l'historique des prix.
DEFAULT_CC = "fr"


def _get(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.debug("%s : %s", url.split("?")[0], exc)
        return None


def now():
    return datetime.now(timezone.utc).isoformat()


# --- frequentation -------------------------------------------------------

def sample_players(conn, appid):
    """Releve le nombre de joueurs. Renvoie le compte, ou None si indisponible.

    result != 1 signifie que Steam ne publie pas la donnee : jeu non sorti,
    application sans suivi de session. Ce n'est pas une erreur.
    """
    payload = _get(PLAYERS_URL.format(appid=appid))
    if not payload:
        return None
    body = payload.get("response") or {}
    if body.get("result") != 1 or "player_count" not in body:
        return None

    players = int(body["player_count"])
    conn.execute(
        "INSERT OR REPLACE INTO player_counts (appid, measured, players) VALUES (?, ?, ?)",
        (appid, now(), players),
    )
    return players


def prune_players(conn, keep_days=400):
    """Limite l'historique : une mesure toutes les 10 min sur des annees finit
    par peser lourd sur une VM qui n'a que quelques Go."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    conn.execute("DELETE FROM player_counts WHERE measured < ?", (cutoff_iso,))


# --- prix et fiche store -------------------------------------------------

def sample_details(conn, appid, cc=DEFAULT_CC):
    """Rafraichit la fiche store et enregistre le prix s'il a change."""
    payload = _get(DETAILS_URL.format(appid=appid, cc=cc))
    if not payload:
        return None
    entry = payload.get(str(appid)) or {}
    if not entry.get("success"):
        return None
    data = entry.get("data") or {}

    # On ne garde que l'utile : la reponse complete pese plusieurs dizaines de
    # Ko par app, l'essentiel tient en quelques champs.
    slim = {
        "name": data.get("name"),
        "type": data.get("type"),
        "is_free": data.get("is_free"),
        "release_date": (data.get("release_date") or {}).get("date"),
        "coming_soon": (data.get("release_date") or {}).get("coming_soon"),
        "developers": data.get("developers") or [],
        "publishers": data.get("publishers") or [],
        "genres": [g.get("description") for g in data.get("genres") or []],
        "categories": [c.get("description") for c in data.get("categories") or []][:12],
        "metacritic": (data.get("metacritic") or {}).get("score"),
        "platforms": data.get("platforms") or {},
        "required_age": data.get("required_age"),
        "website": data.get("website"),
        "short_description": data.get("short_description"),
        "support": data.get("support_info") or {},
        # Screenshots et packages viennent de la fiche store, pas de PICS :
        # ce sont les deux seuls onglets de SteamDB que l'appinfo ne porte pas.
        "screenshots": [
            {"id": s.get("id"),
             "thumb": s.get("path_thumbnail"),
             "full": s.get("path_full")}
            for s in (data.get("screenshots") or [])
        ][:40],
        "movies": [_movie(m) for m in (data.get("movies") or [])][:20],
        "packages": [
            {
                "title": g.get("title"),
                "subs": [
                    {"packageid": sub.get("packageid"),
                     "text": sub.get("option_text"),
                     "price": sub.get("price_in_cents_with_discount")}
                    for sub in (g.get("subs") or [])
                ],
            }
            for g in (data.get("package_groups") or [])
        ],
        "dlc": data.get("dlc") or [],
        "fullgame": data.get("fullgame") or {},
    }
    conn.execute(
        """INSERT INTO app_details (appid, data, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(appid) DO UPDATE SET data = excluded.data,
                                            updated_at = excluded.updated_at""",
        (appid, json.dumps(slim, ensure_ascii=False), now()),
    )

    price = data.get("price_overview")
    if price:
        record_price(conn, appid, price)
    return slim


# Steam ne publie plus d'URL mp4/webm dans appdetails : les champs actuels
# (hls_h264, dash_h264, dash_av1) sont du streaming adaptatif, qu'un <video>
# ne sait pas lire sans bibliotheque dediee. Les fichiers directs existent
# pourtant toujours a l'ancienne convention, verifiee sur plusieurs jeux --
# on reconstruit donc l'URL a partir de l'identifiant du film.
TRAILER_ROOT = "https://video.akamai.steamstatic.com/store_trailers/"


def _exists(url, timeout=10):
    """Verifie qu'une URL repond, sans telecharger le corps."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:                                      # noqa: BLE001
        return False


def _movie(m):
    """Sources lisibles d'une bande-annonce.

    Steam a deux conventions selon l'anciennete de la video :

      - anciennes : un fichier direct, store_trailers/<id>/movie_max.mp4 ;
      - recentes  : uniquement du streaming segmente (hls_h264, dash_h264),
        sous un tout autre chemin. AUCUN mp4 n'existe pour celles-la.

    On ne peut pas deviner laquelle s'applique : appdetails ne publie plus les
    URL mp4 du tout. On teste donc l'existence du fichier une fois, a la
    collecte, plutot que de laisser le navigateur echouer sur un 404 -- c'est
    ce qui produisait "No video with supported format and MIME type found".
    """
    mid = m.get("id")
    base = f"{TRAILER_ROOT}{mid}/" if mid else None
    hls = m.get("hls_h264")
    hls = hls.get("max") if isinstance(hls, dict) else hls

    mp4 = mp4_480 = None
    if base and _exists(f"{base}movie_max.mp4"):
        mp4 = f"{base}movie_max.mp4"
        mp4_480 = f"{base}movie480.mp4"

    return {
        "id": mid,
        "name": m.get("name"),
        "thumb": m.get("thumbnail"),
        "mp4": mp4,
        "mp4_480": mp4_480,
        # Repli universel : lisible via hls.js, et nativement sur Safari.
        "hls": hls,
    }


def record_price(conn, appid, price):
    """Enregistre le prix uniquement s'il differe du dernier connu."""
    currency = price.get("currency", "?")
    initial = price.get("initial")
    final = price.get("final")
    discount = price.get("discount_percent", 0)

    last = conn.execute(
        """SELECT initial, final, discount FROM prices
           WHERE appid = ? AND currency = ?
           ORDER BY observed DESC LIMIT 1""",
        (appid, currency),
    ).fetchone()

    if last and (last["initial"], last["final"], last["discount"]) == (initial, final, discount):
        return False

    conn.execute(
        """INSERT OR REPLACE INTO prices
               (appid, currency, observed, initial, final, discount)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (appid, currency, now(), initial, final, discount),
    )
    return True


# --- lecture -------------------------------------------------------------

def player_series(conn, appid, limit=500):
    rows = conn.execute(
        """SELECT measured, players FROM player_counts
           WHERE appid = ? ORDER BY measured DESC LIMIT ?""",
        (appid, limit),
    ).fetchall()
    return [{"t": r["measured"], "players": r["players"]} for r in reversed(rows)]


def player_stats(conn, appid):
    row = conn.execute(
        """SELECT COUNT(*) n, MAX(players) peak, MIN(players) low,
                  AVG(players) avg, MAX(measured) last
           FROM player_counts WHERE appid = ?""",
        (appid,),
    ).fetchone()
    current = conn.execute(
        """SELECT players FROM player_counts WHERE appid = ?
           ORDER BY measured DESC LIMIT 1""", (appid,),
    ).fetchone()
    if not row["n"]:
        return None
    return {
        "current": current["players"] if current else None,
        "peak": row["peak"],
        "low": row["low"],
        "average": round(row["avg"]) if row["avg"] else None,
        "samples": row["n"],
        "last_measured": row["last"],
    }


def price_history(conn, appid):
    rows = conn.execute(
        """SELECT currency, observed, initial, final, discount FROM prices
           WHERE appid = ? ORDER BY observed DESC""", (appid,),
    ).fetchall()
    return [dict(r) for r in rows]


def details(conn, appid):
    row = conn.execute("SELECT data, updated_at FROM app_details WHERE appid = ?",
                       (appid,)).fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    data["_updated_at"] = row["updated_at"]
    return data


# --- apps apparentees ----------------------------------------------------

SEARCH_URL = ("https://store.steampowered.com/api/storesearch/"
              "?term={term}&l=english&cc=US")


def related_by_name(name, appid, limit=12):
    """Apps du store dont le nom derive de celui du jeu : demo, edition, suite.

    PICS ne relie pas ces apps entre elles : `listofdlc` ne couvre que les DLC,
    et une demo est une app autonome. SteamDB les retrouve parce qu'il indexe
    le nom de TOUS les apps de Steam ; a defaut, la recherche du store donne
    l'essentiel.

    Limite assumee : les apps non listees publiquement -- alpha fermee,
    playtest sur invitation -- n'apparaissent dans aucune recherche. Elles
    resteraient invisibles sans index complet de PICS.
    """
    if not name:
        return []
    payload = _get(SEARCH_URL.format(term=urllib.parse.quote(name)))
    if not payload:
        return []

    base = name.strip().lower()
    out = []
    for item in payload.get("items", [])[:limit]:
        other = int(item.get("id", 0))
        label = (item.get("name") or "").strip()
        if other == appid or not label:
            continue
        # Le nom doit deriver du jeu, pas seulement lui ressembler :
        # "Fading Echoes" n'a rien a voir avec "Fading Echo".
        low = label.lower()
        if not low.startswith(base):
            continue
        rest = low[len(base):]
        # Le suffixe doit commencer par un separateur : sinon "Fading Echoes"
        # passerait pour une declinaison de "Fading Echo", alors que c'est un
        # autre jeu.
        if rest and rest[0].isalnum():
            continue
        suffix = rest.strip(" -:")
        out.append({
            "appid": other,
            "name": label,
            "kind": _guess_kind(suffix),
        })
    return out


def _guess_kind(suffix):
    for word, kind in (("demo", "demo"), ("playtest", "playtest"),
                       ("beta", "beta"), ("alpha", "alpha"),
                       ("soundtrack", "soundtrack"), ("artbook", "artbook"),
                       ("bundle", "bundle")):
        if word in suffix:
            return kind
    return "related" if suffix else "edition"
