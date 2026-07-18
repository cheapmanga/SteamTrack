"""Cles d'API et quotas.

Trois niveaux :
  - cle absente        -> quota anonyme, genereux mais borne, pour essayer l'API ;
  - cle avec quota     -> limite horaire propre a la cle ;
  - cle sans quota     -> illimite (les tiennes, et les invites de confiance).

La consommation est comptee par cle et par heure dans api_usage : une ligne par
heure et par cle, plutot qu'un journal de requetes qui grossirait sans fin.
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Quota anonyme, par heure et PAR ADRESSE IP.
#
# Le dimensionnement vient de l'interface web : elle appelle la meme API sans
# cle, et l'affichage d'une seule fiche de jeu coute environ 8 requetes (app,
# changes pagines, depots, players, info, sections, related, patches).
#
#   600 / 8 = 75 fiches consultees par heure et par visiteur.
#
# Un humain qui navigue vite ouvre 20 a 30 fiches dans l'heure : 600 laisse
# donc un facteur 2 a 3 de marge pour les rechargements, les onglets multiples
# et les allers-retours sur la liste. L'essai a 60/h avait rendu le site
# inutilisable des la septieme fiche — d'ou cette marge volontairement large.
#
# Cote aspiration, 600/h reste un frein reel : recuperer les 8 endpoints de
# quelques milliers de jeux demanderait des jours et autant d'adresses IP
# distinctes. Qui veut plus prend une cle, ce qui rend l'usage identifiable.
ANON_QUOTA = 600

# Prefixe des seaux anonymes dans api_usage. Compter tous les anonymes sur une
# seule ligne partagee reviendrait a laisser un seul visiteur (ou un seul bot)
# consommer le quota de tout le monde : le seau est donc par IP.
ANON_KEY = "anon"

# Derriere un reverse-proxy, request.client.host est l'adresse du proxy et tous
# les visiteurs partageraient un seau. X-Forwarded-For corrige cela, mais il est
# trivial a falsifier quand l'API est jointe en direct : on ne le lit que si le
# deploiement declare explicitement qu'un proxy de confiance est en amont.
#
# A savoir : uvicorn active --proxy-headers par defaut et reecrit deja
# request.client a partir de X-Forwarded-For, mais uniquement quand la connexion
# vient de 127.0.0.1. Un proxy local est donc traite correctement sans ce
# drapeau, et un client distant ne peut pas se falsifier une IP. Ce drapeau ne
# sert que si le proxy est sur une autre machine — dans ce cas il faut aussi
# restreindre l'acces direct au port 8080 par le pare-feu, sinon n'importe qui
# peut se forger autant de seaux que d'IP inventees.
TRUST_PROXY = os.environ.get("STEAMTRACK_TRUST_PROXY", "") in ("1", "true", "yes")


def current_hour():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _next_hour():
    now = datetime.now(timezone.utc)
    return (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


def next_hour_reset():
    return _next_hour().isoformat()


def seconds_until_reset():
    """Secondes restantes avant la remise a zero, pour l'en-tete Retry-After."""
    delta = (_next_hour() - datetime.now(timezone.utc)).total_seconds()
    return max(1, int(delta) + 1)


def anon_bucket(client_ip):
    """Nom du seau anonyme d'une IP.

    L'IP est hachee : la base garde une trace de consommation sans conserver
    d'adresse en clair, et le hachage borne la longueur de la cle.
    """
    if not client_ip:
        return ANON_KEY
    digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:16]
    return f"{ANON_KEY}:{digest}"


def client_ip(request):
    """Adresse de l'appelant, en tenant compte du proxy si on lui fait confiance."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return getattr(request.client, "host", None) if request.client else None


def lookup(conn, key):
    """Resout une cle. Renvoie None si elle est inconnue ou revoquee.

    Les seaux anonymes ('anon' et 'anon:<hash>') vivent dans api_keys pour
    satisfaire la contrainte de cle etrangere d'api_usage, mais ce ne sont PAS
    des cles d'API : leur nom est derivable de l'IP (sha256 non sale), donc les
    accepter permettrait a n'importe qui de calculer le seau d'une victime et
    de vider son quota a sa place. On les refuse a l'entree.
    """
    if not key or key == ANON_KEY or key.startswith(ANON_KEY + ":"):
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key = ? AND revoked_at IS NULL", (key,)
    ).fetchone()
    return dict(row) if row else None


def _ensure_bucket(conn, key):
    """Cree la ligne api_keys d'un seau anonyme.

    api_usage a une contrainte de cle etrangere vers api_keys (schema.sql active
    PRAGMA foreign_keys) : sans cette ligne, la premiere requete d'une nouvelle
    IP echouerait. Ces lignes ne sont pas des cles utilisables : `lookup` refuse
    explicitement tout ce qui commence par le prefixe anonyme.
    """
    conn.execute(
        """INSERT OR IGNORE INTO api_keys (key, label, quota_per_hour, is_admin, created_at)
           VALUES (?, 'anonymous bucket', ?, 0, ?)""",
        (key, ANON_QUOTA, datetime.now(timezone.utc).isoformat()),
    )


def _purge(conn, hour):
    """Efface les fenetres passees et les seaux anonymes devenus inutiles.

    Sans cela api_usage grossirait sans fin, et api_keys accumulerait une ligne
    par IP ayant visite le service depuis la mise en service.
    """
    conn.execute("DELETE FROM api_usage WHERE hour < ?", (hour,))
    conn.execute(
        """DELETE FROM api_keys
           WHERE key LIKE ? AND key NOT IN (SELECT key FROM api_usage)""",
        (ANON_KEY + ":%",),
    )


def consume(conn, key, quota):
    """Incremente la consommation. Renvoie (autorise, restant, limite).

    quota None = illimite : on ne compte meme pas, pour ne pas ecrire a chaque
    requete sur une cle qui n'a de toute façon pas de plafond.
    """
    if quota is None:
        return True, None, None

    hour = current_hour()

    # Compter est la SEULE ecriture du chemin de lecture, et elle ne doit jamais
    # faire tomber le service. Deux precautions :
    #
    # 1. busy_timeout court. La connexion est ouverte avec timeout=30 s, ce qui
    #    convient au collecteur mais pas ici : quand une transaction d'ecriture
    #    etait deja en cours, chaque requete anonyme attendait 30 s puis rendait
    #    un 500. Mesure avant correction : 30 133 ms puis "Internal Server
    #    Error" sur une simple visite du site.
    #
    # 2. Echec ouvert. Si le verrou reste indisponible, on laisse passer la
    #    requete sans la compter plutot que de la refuser : un limiteur de debit
    #    qui rend le site indisponible est pire que le depassement qu'il evite.
    #    La fenetre est d'une seconde et le compteur repart au coup suivant.
    previous = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout = 1000")
    try:
        # La creation du seau et son premier comptage doivent etre atomiques :
        # sans transaction, un autre worker peut passer _purge entre les deux,
        # supprimer le seau qui n'a pas encore de ligne api_usage, et faire
        # echouer l'INSERT suivant sur la cle etrangere (500 sur une requete
        # parfaitement normale).
        conn.execute("BEGIN IMMEDIATE")
        try:
            if key.startswith(ANON_KEY + ":"):
                _ensure_bucket(conn, key)
            conn.execute(
                """INSERT INTO api_usage (key, hour, hits) VALUES (?, ?, 1)
                   ON CONFLICT(key, hour) DO UPDATE SET hits = hits + 1""",
                (key, hour),
            )
            hits = conn.execute(
                "SELECT hits FROM api_usage WHERE key = ? AND hour = ?", (key, hour)
            ).fetchone()["hits"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError:
        # Base verrouillee : on sert la requete sans la comptabiliser.
        return True, None, quota
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(previous)}")

    # Purge periodique. Le declencheur est le compteur de la cle courante : sur
    # un seau anonyme il tombe forcement avant l'epuisement du quota.
    # Elle aussi ecrit : un echec de verrou ne doit pas faire echouer la requete
    # de l'utilisateur, la purge suivante s'en chargera.
    if hits % 200 == 0:
        try:
            _purge(conn, hour)
        except sqlite3.OperationalError:
            pass

    return hits <= quota, max(0, quota - hits), quota


def usage(conn, key):
    """Consommation de la fenetre horaire courante pour une cle."""
    row = conn.execute(
        "SELECT hits FROM api_usage WHERE key = ? AND hour = ?", (key, current_hour())
    ).fetchone()
    return row["hits"] if row else 0


def authenticate(conn, key, ip=None):
    """Identifie l'appelant et applique son quota.

    Renvoie un dict decrivant l'appelant, ou leve ValueError si la cle est
    invalide, et PermissionError si le quota est depasse.
    """
    if key:
        record = lookup(conn, key)
        if not record:
            raise ValueError("unknown or revoked API key")
        allowed, remaining, limit = consume(conn, key, record["quota_per_hour"])
        caller = {
            "key": key,
            "label": record["label"],
            "admin": bool(record["is_admin"]),
            "remaining": remaining,
            "limit": limit,
        }
    else:
        allowed, remaining, limit = consume(conn, anon_bucket(ip), ANON_QUOTA)
        caller = {
            "key": None,
            "label": "anonymous",
            "admin": False,
            "remaining": remaining,
            "limit": limit,
        }

    if not allowed:
        # Message en anglais et en UN SEUL argument : PermissionError derive
        # d'OSError, qui avec deux arguments les reformate en "[Errno x] y".
        # Un second argument ferait lire au visiteur refuse
        # "[Errno quota horaire depasse] 600". La limite voyage a cote, en
        # attribut, pour que l'API puisse la poser dans ses en-tetes.
        exc = PermissionError("hourly quota exceeded")
        exc.limit = limit
        raise exc
    return caller


def ensure_anon_row(conn):
    """Ligne temoin decrivant la politique anonyme dans api_keys.

    Les seaux anonymes vivent dans api_usage sous 'anon:<hash ip>' et ne sont
    pas des cles utilisables ; cette ligne unique sert a documenter le quota en
    vigueur dans `steamtrack key list`, et son quota est reharmonise au
    demarrage pour ne pas afficher la valeur d'une version precedente.

    Appelee une seule fois par `db.init()`, jamais dans le chemin des requetes :
    son ON CONFLICT DO UPDATE ecrit systematiquement, ce qui imposerait une
    transaction d'ecriture a chaque lecture.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO api_keys (key, label, quota_per_hour, is_admin, created_at)
           VALUES (?, 'anonymous (par IP)', ?, 0, ?)
           ON CONFLICT(key) DO UPDATE SET quota_per_hour = excluded.quota_per_hour,
                                          label = excluded.label""",
        (ANON_KEY, ANON_QUOTA, now),
    )
