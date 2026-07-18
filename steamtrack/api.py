"""API HTTP de steamtrack.

    uvicorn steamtrack.api:app --host 0.0.0.0 --port 8080

Documentation interactive sur /docs, schema OpenAPI sur /openapi.json.

L'authentification passe par l'en-tete X-API-Key. Sans cle, un quota anonyme
reduit permet d'essayer l'API ; avec une cle, le quota est celui de la cle, et
les cles sans quota sont illimitees.
"""

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, db

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

@app.get("/", tags=["service"])
def index(conn=Depends(get_conn)):
    """Point d'entree : ce que le service expose, sans avoir a lire la doc.

    Sans cette route, la racine renvoie 404 -- c'est pourtant la premiere URL
    qu'on essaie en decouvrant un service.
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
        """SELECT COUNT(*) n, MAX(occurred_at) last,
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
        "changes": stats["n"],
        "builds": stats["builds"] or 0,
        "last_change_at": stats["last"],
        "latest_buildid": build["buildid"] if build else None,
        "latest_build_at": build["occurred_at"] if build else None,
        "depots_public": not bool(row["missing_token"]),
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
    return {"appid": appid, "count": len(events), "changes": events}


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
