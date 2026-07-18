"""API HTTP de steamtrack.

    uvicorn steamtrack.api:app --host 0.0.0.0 --port 8080

Documentation interactive sur /docs, schema OpenAPI sur /openapi.json.

L'authentification passe par l'en-tete X-API-Key. Sans cle, un quota anonyme
reduit permet d'essayer l'API ; avec une cle, le quota est celui de la cle, et
les cles sans quota sont illimitees.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, diff, probes

app = FastAPI(
    title="steamtrack",
    version="1.0",
    description=(
        "Historique des changements Steam pour une liste de jeux suivis : "
        "builds, depots, branches, metadonnees du store et patch notes.\n\n"
        "**Limite :** l'historique d'un jeu demarre le jour de son ajout au "
        "service. Steam ne conserve pas les changelists passes."
    ),
)

# API destinee a etre appelee depuis n'importe ou, y compris un navigateur.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def header_image(conn, appid):
    """URL de la banniere du jeu.

    On la tire du snapshot plutot que de deviner l'URL du CDN : les jeux non
    encore sortis n'ont pas d'image a l'emplacement standard, mais leur appinfo
    en publie une.
    """
    snapshot = db.get_snapshot(conn, appid)
    if snapshot:
        images = (snapshot.get("common") or {}).get("header_image") or {}
        value = images.get("english") or next(iter(images.values()), None)
        if value:
            url, _ = diff.asset_url(appid, value)
            if url:
                return url
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


def get_conn():
    # SQLite et threads : une connexion par requete, la base est en WAL donc
    # les lectures concurrentes ne se bloquent pas.
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def caller(request: Request,
           conn: sqlite3.Connection = Depends(get_conn),
           x_api_key: str | None = Header(default=None)):
    """Authentifie et applique le quota. Pose les en-tetes de limite."""
    auth.ensure_anon_row(conn)
    try:
        who = auth.authenticate(conn, x_api_key)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except PermissionError as exc:
        # Les en-tetes de quota doivent etre presents SURTOUT sur le refus :
        # c'est la reponse qu'un client automatise doit savoir interpreter.
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={
                "Retry-After": "3600",
                "X-RateLimit-Limit": str(exc.args[1]) if len(exc.args) > 1 else "",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": auth.next_hour_reset(),
            },
        ) from exc
    request.state.who = who
    return who


def admin(who=Depends(caller)):
    if not who["admin"]:
        raise HTTPException(status_code=403,
                            detail="cette operation demande une cle administrateur")
    return who


@app.middleware("http")
async def rate_headers(request: Request, call_next):
    response = await call_next(request)
    who = getattr(request.state, "who", None)
    if who and who.get("limit") is not None:
        response.headers["X-RateLimit-Limit"] = str(who["limit"])
        response.headers["X-RateLimit-Remaining"] = str(who["remaining"])
        response.headers["X-RateLimit-Reset"] = auth.next_hour_reset()
    return response


# --- lecture -------------------------------------------------------------

@app.get("/api", tags=["service"])
def index(conn=Depends(get_conn)):
    """Point d'entree de l'API : ce que le service expose.

    La racine sert l'interface web ; cette description vit donc sur /api.
    """
    apps = conn.execute("SELECT COUNT(*) n FROM apps").fetchone()["n"]
    changes = conn.execute("SELECT COUNT(*) n FROM changes").fetchone()["n"]
    return {
        "service": "steamtrack",
        "version": app.version,
        "description": "Historique des changements Steam pour les jeux suivis.",
        "tracked_apps": apps,
        "changes": changes,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "health": "/health",
            "apps": "/v1/apps",
            "app": "/v1/apps/{appid}",
            "changes": "/v1/apps/{appid}/changes",
            "builds": "/v1/apps/{appid}/builds",
            "feed": "/v1/changes",
        },
        "auth": "en-tete X-API-Key ; sans cle, quota anonyme reduit",
    }


@app.get("/health", tags=["service"])
def health(conn=Depends(get_conn)):
    apps = conn.execute("SELECT COUNT(*) n FROM apps").fetchone()["n"]
    changes = conn.execute("SELECT COUNT(*) n FROM changes").fetchone()["n"]
    cursor = db.get_state(conn, "change_number")
    last = conn.execute("SELECT MAX(occurred_at) t FROM changes").fetchone()["t"]
    return {
        "status": "ok",
        "tracked_apps": apps,
        "changes": changes,
        "collector_cursor": int(cursor) if cursor else None,
        "last_change_at": last,
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/apps", tags=["apps"])
def list_apps(conn=Depends(get_conn), who=Depends(caller)):
    """Liste les jeux suivis, avec le volume et la date du dernier changement."""
    rows = conn.execute(
        """SELECT a.appid, a.name, a.added_at, a.last_change, a.missing_token,
                  COUNT(c.id) AS changes,
                  MAX(c.occurred_at) AS last_change_at
           FROM apps a LEFT JOIN changes c ON c.appid = a.appid
           GROUP BY a.appid ORDER BY a.appid"""
    ).fetchall()
    return {"apps": [
        {
            "appid": r["appid"],
            "name": r["name"],
            "added_at": r["added_at"],
            "changes": r["changes"],
            "last_change_at": r["last_change_at"],
            "last_change_number": r["last_change"],
            "header_image": header_image(conn, r["appid"]),
            # Signale au consommateur que les builds de cet app arriveront sans
            # buildid : ce n'est pas une anomalie de notre cote.
            "depots_public": not bool(r["missing_token"]),
        }
        for r in rows
    ]}


@app.get("/v1/apps/{appid}", tags=["apps"])
def get_app(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    row = conn.execute("SELECT * FROM apps WHERE appid = ?", (appid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="app non suivie")
    stats = conn.execute(
        """SELECT COUNT(*) n, MAX(occurred_at) last, MIN(occurred_at) first,
                  SUM(kind = 'build') builds
           FROM changes WHERE appid = ?""", (appid,)
    ).fetchone()
    build = conn.execute(
        """SELECT buildid, occurred_at FROM changes
           WHERE appid = ? AND buildid IS NOT NULL
           ORDER BY occurred_at DESC LIMIT 1""", (appid,)
    ).fetchone()
    return {
        "appid": row["appid"],
        "name": row["name"],
        "added_at": row["added_at"],
        "tracking_since": row["added_at"],
        # Profondeur reelle de l'historique : un import SteamDB peut le faire
        # remonter bien avant la mise sous suivi.
        "history_from": stats["first"],
        "changes": stats["n"],
        "builds": stats["builds"] or 0,
        "last_change_at": stats["last"],
        "latest_buildid": build["buildid"] if build else None,
        "latest_build_at": build["occurred_at"] if build else None,
        "depots_public": not bool(row["missing_token"]),
        "header_image": header_image(conn, appid),
    }


@app.get("/v1/apps/{appid}/changes", tags=["changes"])
def app_changes(
    appid: int,
    kind: str | None = Query(None, description="build, depot, branch, store, assets, news, meta"),
    since: str | None = Query(None, description="ISO 8601 : ne renvoyer que les changements posterieurs"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn=Depends(get_conn), who=Depends(caller),
):
    """Historique d'un jeu. Un changement mixte est renvoye sous chacune de ses categories."""
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    events = db.changes_for(conn, appid, limit=limit, offset=offset, kind=kind, since=since)
    total = conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE appid = ?", (appid,)
    ).fetchone()["n"]
    # total accompagne toujours count : sinon un client recevant exactement
    # `limit` entrees ne peut pas distinguer "c'est tout" de "il en reste".
    return {"appid": appid, "count": len(events), "total": total,
            "offset": offset, "changes": events}


@app.get("/v1/apps/{appid}/builds", tags=["changes"])
def app_builds(appid: int, limit: int = Query(50, ge=1, le=500),
               conn=Depends(get_conn), who=Depends(caller)):
    """Raccourci : uniquement les builds, l'usage le plus frequent."""
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    return {"appid": appid,
            "builds": db.changes_for(conn, appid, limit=limit, kind="build")}


@app.get("/v1/changes", tags=["changes"])
def all_changes(
    kind: str | None = None,
    since: str | None = Query(None, description="ISO 8601"),
    limit: int = Query(50, ge=1, le=500),
    conn=Depends(get_conn), who=Depends(caller),
):
    """Flux global, tous jeux suivis confondus. `since` permet le suivi incremental."""
    events = db.recent_changes(conn, limit=limit, since=since, kind=kind)
    return {"count": len(events), "changes": events}


@app.get("/v1/apps/{appid}/players", tags=["stats"])
def app_players(appid: int, limit: int = Query(500, ge=1, le=5000),
                conn=Depends(get_conn), who=Depends(caller)):
    """Frequentation relevee au fil du temps.

    L'historique commence a la mise sous suivi : le nombre de joueurs passe
    n'est publie nulle part, il n'existe que si on l'a mesure.
    """
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    return {
        "appid": appid,
        "stats": probes.player_stats(conn, appid),
        "series": probes.player_series(conn, appid, limit),
    }


@app.get("/v1/apps/{appid}/prices", tags=["stats"])
def app_prices(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    """Historique des prix : une entree par changement constate."""
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    return {"appid": appid, "prices": probes.price_history(conn, appid)}


@app.get("/v1/apps/{appid}/depots", tags=["apps"])
def app_depots(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    """Depots et branches, lus dans le dernier appinfo connu.

    Vide pour les apps a jeton : Steam ne publie pas leur section depots.
    """
    snapshot = db.get_snapshot(conn, appid)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="app non suivie ou pas encore initialisee")

    depots = snapshot.get("depots") or {}
    branches = depots.get("branches") or {} if isinstance(depots, dict) else {}

    listing = []
    for key, value in (depots.items() if isinstance(depots, dict) else []):
        if not key.isdigit() or not isinstance(value, dict):
            continue
        manifests = value.get("manifests") or {}
        public = manifests.get("public") or {}
        listing.append({
            "depot": int(key),
            "name": value.get("name"),
            "config": value.get("config") or {},
            "manifest": public.get("gid") if isinstance(public, dict) else public,
            "size": int(public["size"]) if isinstance(public, dict) and public.get("size") else None,
            "download": int(public["download"]) if isinstance(public, dict) and public.get("download") else None,
            "depotfromapp": value.get("depotfromapp"),
            "shared": bool(value.get("sharedinstall")),
        })

    return {
        "appid": appid,
        "depots_public": not bool(snapshot.get("_missing_token")),
        "depots": sorted(listing, key=lambda d: d["depot"]),
        "branches": [
            {
                "name": name,
                "buildid": info.get("buildid") if isinstance(info, dict) else None,
                "description": info.get("description") if isinstance(info, dict) else None,
                "updated": info.get("timeupdated") if isinstance(info, dict) else None,
                "protected": bool(info.get("pwdrequired")) if isinstance(info, dict) else False,
            }
            for name, info in branches.items()
        ],
    }


@app.get("/v1/apps/{appid}/info", tags=["apps"])
def app_info(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    """Fiche store : genres, editeurs, date de sortie, note."""
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    return {"appid": appid, "details": probes.details(conn, appid)}


@app.get("/v1/apps/{appid}/sections", tags=["apps"])
def app_sections(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    """Sections brutes du dernier appinfo connu.

    C'est la matiere de la plupart des onglets de SteamDB : `common` porte les
    metadonnees, `config` la configuration de lancement, `ufs` la sauvegarde
    dans le nuage, `extended` les DLC et le repertoire d'installation. Aucune
    collecte supplementaire n'est necessaire : tout arrive dans le meme
    appinfo que celui deja diffe a chaque changement.
    """
    snapshot = db.get_snapshot(conn, appid)
    if snapshot is None:
        raise HTTPException(status_code=404,
                            detail="app non suivie ou pas encore initialisee")

    sections = {k: v for k, v in snapshot.items()
                if not k.startswith("_") and k != "appid"}
    return {
        "appid": appid,
        "change_number": snapshot.get("_change_number"),
        "sections": sections,
        "available": sorted(sections),
    }


@app.get("/v1/apps/{appid}/related", tags=["apps"])
def app_related(appid: int, conn=Depends(get_conn), who=Depends(caller)):
    """DLC et jeu parent, avec le nom de ceux que l'on suit deja."""
    snapshot = db.get_snapshot(conn, appid) or {}
    extended = snapshot.get("extended") or {}
    common = snapshot.get("common") or {}

    raw = extended.get("listofdlc") or ""
    dlc_ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]

    details = probes.details(conn, appid) or {}
    for extra in details.get("dlc") or []:
        if int(extra) not in dlc_ids:
            dlc_ids.append(int(extra))

    known = {r["appid"]: r["name"] for r in conn.execute("SELECT appid, name FROM apps")}
    return {
        "appid": appid,
        "parent": common.get("parent") or (details.get("fullgame") or {}).get("appid"),
        "dlc": [{"appid": d, "name": known.get(d), "tracked": d in known} for d in dlc_ids],
    }


@app.get("/v1/apps/{appid}/patches", tags=["changes"])
def app_patches(appid: int, limit: int = Query(100, ge=1, le=500),
                conn=Depends(get_conn), who=Depends(caller)):
    """Suite des builds publiees, du plus recent au plus ancien."""
    if not conn.execute("SELECT 1 FROM apps WHERE appid = ?", (appid,)).fetchone():
        raise HTTPException(status_code=404, detail="app non suivie")
    rows = conn.execute(
        """SELECT change_number, buildid, occurred_at, title FROM changes
           WHERE appid = ? AND (buildid IS NOT NULL OR kind = 'build')
           ORDER BY occurred_at DESC LIMIT ?""",
        (appid, limit),
    ).fetchall()
    return {"appid": appid, "patches": [dict(r) for r in rows]}


@app.get("/v1/search", tags=["apps"])
def search(q: str = Query(..., min_length=2, description="nom ou appid"),
           conn=Depends(get_conn), who=Depends(caller)):
    """Recherche parmi les jeux suivis."""
    like = f"%{q.lower()}%"
    rows = conn.execute(
        """SELECT appid, name FROM apps
           WHERE LOWER(name) LIKE ? OR CAST(appid AS TEXT) LIKE ?
           ORDER BY name LIMIT 25""",
        (like, like),
    ).fetchall()
    return {"query": q, "results": [dict(r) for r in rows]}


# --- ecriture (cle administrateur) ---------------------------------------

@app.post("/v1/apps", status_code=202, tags=["apps"])
def add_app(appid: int = Query(..., description="AppID Steam"),
            conn=Depends(get_conn), who=Depends(admin)):
    """Met un jeu sous suivi. L'etat initial est recupere par le collecteur.

    L'API ne parle jamais a Steam : le client Steam s'appuie sur gevent, dont
    le monkey patching casserait la boucle asyncio du serveur. Elle enregistre
    donc l'intention, et le collecteur -- seul processus autorise a joindre
    Steam -- recupere l'etat initial a son passage suivant, en quelques
    secondes.

    L'historique demarre a cet instant : Steam ne conserve pas le passe.
    """
    if not db.add_app(conn, appid):
        raise HTTPException(status_code=409, detail="app deja suivie")
    return {
        "appid": appid,
        "tracked": True,
        "bootstrapped": False,
        "detail": "etat initial en cours de recuperation par le collecteur",
        "history_starts_now": True,
    }


@app.delete("/v1/apps/{appid}", tags=["apps"])
def delete_app(appid: int, conn=Depends(get_conn), who=Depends(admin)):
    """Retire un jeu et tout son historique. Irreversible."""
    removed = db.remove_app(conn, appid)
    if not removed:
        raise HTTPException(status_code=404, detail="app non suivie")
    return {"appid": appid, "removed": True,
            "name": removed["name"], "changes_deleted": removed["changes"]}


class RevalidatingStatics(StaticFiles):
    """Fichiers statiques toujours revalides aupres du serveur.

    Starlette envoie ETag et Last-Modified, mais aucun Cache-Control. Sans lui,
    le navigateur applique une heuristique de fraicheur et peut resservir un
    fichier sans rien demander : une page mise a jour se retrouvait alors avec
    l'ancien script, et appelait une fonction qui n'existait pas encore.

    no-cache n'interdit pas le cache, il impose de le revalider : la reponse
    reste un 304 vide tant que le fichier n'a pas bouge.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return response


# L'interface web est servie a la racine. Ce montage doit rester en DERNIER :
# monte sur "/", il capture toute route enregistree apres lui, ce qui rendrait
# l'API inaccessible.
WEB = Path(__file__).resolve().parent.parent / "web"
if WEB.is_dir():
    app.mount("/", RevalidatingStatics(directory=WEB, html=True), name="web")
