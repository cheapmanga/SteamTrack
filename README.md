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
| `GET /v1/apps/{appid}/players` | frequentation relevee, avec pic et moyenne |
| `GET /v1/apps/{appid}/prices` | historique des prix |
| `GET /v1/apps/{appid}/depots` | depots et branches |
| `GET /v1/apps/{appid}/info` | fiche store |
| `GET /v1/apps/{appid}/sections` | sections detaillees de la fiche store |
| `GET /v1/apps/{appid}/related` | DLC, demos et applications liees |
| `GET /v1/apps/{appid}/patches` | suite des builds publiees |
| `GET /v1/search?q=` | recherche parmi les jeux suivis |
| `POST /v1/apps?appid=` | suivre un jeu — **cle admin** |
| `DELETE /v1/apps/{appid}` | retirer un jeu et son historique — **cle admin** |

Authentification par l'en-tete `X-API-Key`. Sans cle, un quota anonyme reduit
(600 requetes/heure et par adresse IP) permet d'essayer l'API. Les reponses portent
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

## Interface web

Servie a la racine par le meme processus que l'API :

| Page | Contenu |
|---|---|
| `/` | jeux suivis, vignettes, volume et date du dernier changement |
| `/app.html?appid=730` | 12 onglets : Store info, Charts, Patches, Metadata, Packages, Depots, Branches, Configuration, Cloud saves, Screenshots, Related apps, Update history |
| `/about.html` | ce qu'est le service, la limite d'historique, exemples d'API |
| `/changes.html` | flux recent, tous jeux confondus |

Apparence calquee sur SteamDB : fond sombre, typographie dense, panneaux a
liseres, differences colorees. Les assets se previsualisent au survol et se
telechargent au clic.

L'API vit sous `/v1`, `/health`, `/api` et `/docs` ; le reste est servi par
l'interface. Le montage statique est enregistre en DERNIER dans `api.py` :
monte sur `/`, il capturerait sinon toutes les routes declarees apres lui.

## Deploiement

Voir `deploy/steamtrack.service` pour une unite systemd. La base vit dans
`data/steamtrack.db` (SQLite, mode WAL : le collecteur ecrit pendant que l'API
lit).

Deux services, deux roles :

| Unite | Role |
|---|---|
| `steamtrack.service` | collecteur PICS, **seul gros ecrivain** de la base |
| `steamtrack-api.service` | uvicorn, 3 workers, lit la base et sert `web/` |

L'API tourne en **3 workers** sur 2 vCPU : les endpoints sont synchrones et
passent l'essentiel de leur temps bloques sur SQLite, donc 2 workers occupent
les 2 coeurs et le 3e absorbe les attentes disque, sans excedent pour 2 Go de
RAM. C'est sans risque : WAL admet plusieurs lecteurs simultanes, chaque requete
ouvre sa propre connexion, et les compteurs de quota vivent dans la table
`api_usage` -- donc partages entre workers, et non par processus.

## Mise en ligne publique

Ordre a respecter. Les etapes marquees **[HUMAIN]** ne peuvent pas etre
scriptees : elles demandent un navigateur, un compte, ou une decision.

### 1. Verifier les quotas AVANT d'ouvrir

C'est le point le plus important, et le seul qui ne se rattrape pas apres coup.

```bash
grep ANON_QUOTA steamtrack/auth.py     # doit etre un entier, jamais None
```

`ANON_QUOTA` (dans `steamtrack/auth.py`) plafonne les visiteurs sans cle.
`None` signifierait illimite : ouvert au public, cela laisse n'importe qui
saturer la VM. Valeur actuelle : `600` requetes/heure et par adresse IP, soit
environ 75 fiches de jeu par heure et par visiteur.

Se creer une cle illimitee pour garder un acces sans limite, avant l'ouverture :

```bash
steamtrack key add perso --admin        # --quota omis = illimite
# `perso` est positionnel (pas --label). Sans --admin, la cle est en lecture
# seule : POST et DELETE /v1/apps repondraient 403.
```

### 2. Sauvegardes en place avant le trafic

Une base sans sauvegarde ne doit pas etre exposee.

```bash
sudo install -m 755 deploy/backup.sh /opt/steamtrack/deploy/backup.sh
sudo -u steamtrack /opt/steamtrack/deploy/backup.sh   # premier passage manuel
```

Puis en cron quotidien (rotation 7 jours, deja geree par le script) :

```
15 4 * * *  steamtrack  /opt/steamtrack/deploy/backup.sh >> /var/log/steamtrack-backup.log 2>&1
```

Le script utilise `.backup` de sqlite3, **pas** un `cp` : copier a chaud un
fichier en WAL donne une base au mieux en retard, au pire corrompue. Chaque
sauvegarde est relue (`PRAGMA integrity_check`) avant d'etre publiee sous son
nom definitif.

### 3. API en plusieurs workers

```bash
sudo cp deploy/steamtrack-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart steamtrack-api
```

`--proxy-headers --forwarded-allow-ips 127.0.0.1` est **indispensable** derriere
le tunnel : sans ces options uvicorn ignore `X-Forwarded-For`, toutes les
requetes semblent venir de 127.0.0.1, et tous les visiteurs anonymes partagent
un seul seau de quota -- le premier gros consommateur bloque alors tout le
monde. Ne jamais mettre `*` : cela permettrait a un client d'usurper son IP, et
donc son quota, avec un en-tete forge.

### 4. Pare-feu

A lancer **avant** d'ouvrir le tunnel.

```bash
sudo ./deploy/firewall.sh              # defaut : le LAN declare dans le script
sudo ./deploy/firewall.sh 10.0.0.0/24  # autre LAN
```

Idempotent, relancable. Il pose la regle SSH **avant** `ufw enable` : l'ordre
inverse couperait la session SSH en cours et rendrait la VM injoignable hors
console Proxmox. Garder malgre tout une seconde session SSH ouverte pendant
l'operation.

Le port 8080 reste limite au LAN. Le tunnel est une connexion **sortante** :
il n'a besoin d'aucun port entrant, et rien n'est a ouvrir sur la box.

### 5. Tunnel Cloudflare **[HUMAIN]**

Procedure detaillee en tete de `deploy/cloudflared.service`. Resume :

```bash
cloudflared tunnel login                 # [HUMAIN] navigateur + compte Cloudflare
cloudflared tunnel create steamtrack
cloudflared tunnel route dns steamtrack steamtrack.example.com   # [HUMAIN] domaine
```

`tunnel login` ouvre une URL a valider dans un navigateur et suppose un compte
Cloudflare possedant deja un domaine : aucun agent ne peut le faire a votre
place. Le fichier `<UUID>.json` produit est un secret (`chmod 600`).

Puis installer l'unite :

```bash
sudo cp deploy/cloudflared.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared
```

### 6. Verifications avant d'annoncer l'URL

| Verification | Attendu |
|---|---|
| `curl https://steamtrack.example.com/health` | `{"status":"ok",...}` |
| `curl http://<ip-lan-de-la-vm>:8080/health` depuis le LAN | repond |
| 8080 depuis l'exterieur | **injoignable** |
| `curl -sD- -o/dev/null https://.../v1/apps \| grep -i ratelimit` | `x-ratelimit-limit: 600` |
| Deux requetes publiques d'IP differentes | compteurs **independants** |
| `sudo ufw status verbose` | `deny (incoming)`, 22 et 8080 limites au LAN |
| `systemctl status steamtrack steamtrack-api cloudflared` | les trois `active` |
| Sauvegarde du jour presente dans `/opt/steamtrack/backups` | oui |

Si les compteurs de deux IP differentes bougent ensemble, `--proxy-headers`
n'est pas actif : reprendre l'etape 3 avant d'ouvrir au public.

Mot de passe root de la VM change, et cle SSH plutot que mot de passe, avant
toute exposition.

## Tunnel public

Deux unites, selon le besoin :

| Unite | Adresse | Prerequis |
|---|---|---|
| `cloudflared-quick.service` | aleatoire en trycloudflare.com, **change a chaque redemarrage** | aucun |
| `cloudflared.service` | stable, sur votre domaine | `cloudflared tunnel login` : navigateur + compte Cloudflare |

Relever l'adresse courante du quick tunnel :

```bash
/opt/steamtrack/deploy/tunnel-url.sh
```

Le tunnel est une connexion sortante : rien a ouvrir sur la box, le pare-feu
reste ferme en entree.

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
- [x] Interface web
- [x] Frequentation, prix, depots, branches, fiche store
- [x] Rafraichissement automatique des pages
