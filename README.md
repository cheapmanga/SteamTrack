# steamtrack

Suivi des changements Steam pour une liste de jeux choisis, avec API.

Meme principe que SteamDB : un client Steam en **login anonyme** ecoute le flux
PICS, qui annonce en continu quels apps viennent d'etre modifies. Pour les jeux
suivis, l'appinfo est recharge et compare au precedent ; la difference devient un
evenement consultable.

Aucun compte Steam n'est necessaire.

## Ce que le service capte

| Donnee | Disponible |
|---|---|
| Builds, depots, branches (y compris branches cachees) | oui |
| Metadonnees store, tags, langues, assets | oui |
| Annonces et patch notes | oui, ~200 dernieres |
| Changements **anterieurs** a l'ajout du jeu | **non** |

### La limite a connaitre

**Un jeu ajoute commence son historique le jour de son ajout.** Steam ne
conserve pas les changelists passes : PICS ne donne que l'etat courant et la
suite. Rien ne permet de reconstituer automatiquement des annees d'historique.
Seules les annonces sont partiellement rattrapables.

Pour un historique anterieur, il faut importer un export HTML de SteamDB
(page History enregistree depuis le navigateur) -- Cloudflare y bloque tout
acces automatise.

### Apps a jeton

Certains apps, typiquement les jeux **non encore sortis**, ne publient pas leur
section `depots` (`_missing_token`). Leurs builds sont detectees via le
changenumber, mais sans le `buildid`. La CLI le signale a l'ajout.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# suivre un jeu, par appid ou par nom (avec desambiguisation)
python3 -m steamtrack.cli add 730
python3 -m steamtrack.cli add "Elden Ring"

python3 -m steamtrack.cli list
python3 -m steamtrack.cli show 730 --limit 5
python3 -m steamtrack.cli show 730 --kind build

# retirer un jeu ET tout son historique (confirmation demandee)
python3 -m steamtrack.cli remove 730

# cles d'API : quota horaire, ou illimite si --quota est omis
python3 -m steamtrack.cli key add "bot discord" --quota 1000
python3 -m steamtrack.cli key add "moi"
python3 -m steamtrack.cli key list
```

Le collecteur tourne en continu :

```bash
python3 -m steamtrack.collector
```

## API

```bash
uvicorn steamtrack.api:app --host 0.0.0.0 --port 8080
```

Documentation interactive sur `/docs`, schema OpenAPI sur `/openapi.json`.
CORS ouvert : l'API est appelable depuis n'importe quel domaine.

| Route | Description |
|---|---|
| `GET /health` | etat du service et curseur du collecteur |
| `GET /v1/apps` | jeux suivis |
| `GET /v1/apps/{appid}` | detail, derniere build connue |
| `GET /v1/apps/{appid}/changes` | historique (`kind`, `since`, `limit`, `offset`) |
| `GET /v1/apps/{appid}/builds` | raccourci builds |
| `GET /v1/changes` | flux global (`since` pour le suivi incremental) |
| `POST /v1/apps?appid=` | suivre un jeu — **cle admin** |
| `DELETE /v1/apps/{appid}` | retirer un jeu et son historique — **cle admin** |

Authentification par l'en-tete `X-API-Key`. Sans cle, un quota anonyme reduit
(60 requetes/heure, partage) permet d'essayer l'API. Les reponses portent
`X-RateLimit-Limit`, `X-RateLimit-Remaining` et `X-RateLimit-Reset`, y compris
sur un refus 429.

```bash
curl localhost:8080/v1/apps
curl -H "X-API-Key: st_..." "localhost:8080/v1/apps/730/changes?kind=build&limit=10"
curl -X POST -H "X-API-Key: st_admin..." "localhost:8080/v1/apps?appid=440"
```

### Deux processus distincts

L'API **ne joint jamais Steam**. Le client Steam s'appuie sur gevent, dont le
monkey patching casse la boucle asyncio du serveur ; les faire cohabiter fige
le processus. `POST /v1/apps` enregistre donc l'intention et repond
immediatement, puis le collecteur -- seul autorise a parler a Steam -- recupere
l'etat initial a son passage suivant.

## Deploiement

Voir `deploy/steamtrack.service` pour une unite systemd. La base vit dans
`data/steamtrack.db` (SQLite, mode WAL : le collecteur ecrit pendant que l'API
lit).

## Architecture

```
steamtrack/
  schema.sql      tables : apps, snapshots, changes, api_keys, state
  db.py           acces base
  diff.py         comparaison de deux appinfo -> arbre de differences
  news.py         annonces via ISteamNews
  collector.py    daemon : flux PICS -> diff -> base
  cli.py          ajout / suppression / consultation / cles
```

Le format des evenements est un arbre de segments types (`text`, `field`,
`del`, `ins`, `muted`), directement rendu par l'interface et expose tel quel par
l'API. Les assets y portent leur URL, ce qui permet apercu et telechargement.

## Etat

- [x] Collecteur PICS, diff, base
- [x] CLI : ajouter / retirer / lister / consulter
- [x] Cles d'API en base
- [x] API HTTP (cles, quotas, OpenAPI)
- [ ] Interface web
