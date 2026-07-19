# steamtrack

Steam change tracking for a chosen list of games, with an API.

Same principle as SteamDB: a Steam client using **anonymous login** listens to the
PICS stream, which continuously announces which apps have just been modified. For
tracked games, the appinfo is reloaded and compared against the previous one; the
difference becomes a browsable event.

No Steam account required.

## What the service captures

| Data | Available |
|---|---|
| Builds, depots, branches (including hidden branches) | yes |
| Store metadata, tags, languages, assets | yes |
| Announcements and patch notes | yes, last ~200 |
| Changes **predating** the game being added | **no** |

### The limitation to be aware of

**A game starts its history on the day it is added.** Steam does not keep past
changelists: PICS only gives you the current state and everything after it.
Nothing lets you automatically rebuild years of history. Only announcements can
be partially backfilled.

For history predating that, you have to import a SteamDB HTML export (the History
page saved from your browser) -- Cloudflare blocks all automated access there.

### Token-gated apps

Some apps, typically **unreleased** games, do not publish their `depots` section
(`_missing_token`). Their builds are detected through the changenumber, but
without the `buildid`. The CLI warns about this when the game is added.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# track a game, by appid or by name (with disambiguation)
python3 -m steamtrack.cli add 730
python3 -m steamtrack.cli add "Elden Ring"

python3 -m steamtrack.cli list
python3 -m steamtrack.cli show 730 --limit 5
python3 -m steamtrack.cli show 730 --kind build

# remove a game AND its entire history (asks for confirmation)
python3 -m steamtrack.cli remove 730

# API keys: hourly quota, or unlimited if --quota is omitted
python3 -m steamtrack.cli key add "bot discord" --quota 1000
python3 -m steamtrack.cli key add "moi"
python3 -m steamtrack.cli key list
```

The collector runs continuously:

```bash
python3 -m steamtrack.collector
```

## API

```bash
uvicorn steamtrack.api:app --host 0.0.0.0 --port 8080
```

Interactive documentation at `/docs`, OpenAPI schema at `/openapi.json`.
CORS is wide open: the API can be called from any domain.

| Route | Description |
|---|---|
| `GET /health` | service status and collector cursor |
| `GET /v1/apps` | tracked games |
| `GET /v1/apps/{appid}` | details, latest known build |
| `GET /v1/apps/{appid}/changes` | history (`kind`, `since`, `limit`, `offset`) |
| `GET /v1/apps/{appid}/builds` | builds shortcut |
| `GET /v1/changes` | global feed (`since` for incremental polling) |
| `GET /v1/apps/{appid}/players` | recorded player counts, with peak and average |
| `GET /v1/apps/{appid}/prices` | price history |
| `GET /v1/apps/{appid}/depots` | depots and branches |
| `GET /v1/apps/{appid}/info` | store page data |
| `GET /v1/apps/{appid}/sections` | detailed sections of the store page |
| `GET /v1/apps/{appid}/related` | DLC, demos and related applications |
| `GET /v1/apps/{appid}/patches` | sequence of published builds |
| `GET /v1/search?q=` | search among tracked games |
| `POST /v1/apps?appid=` | track a game — **admin key** |
| `DELETE /v1/apps/{appid}` | remove a game and its history — **admin key** |

Authentication uses the `X-API-Key` header. Without a key, a reduced anonymous
quota (600 requests/hour per IP address) lets you try the API out. Responses carry
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`, including on
a 429 rejection.

```bash
curl localhost:8080/v1/apps
curl -H "X-API-Key: st_..." "localhost:8080/v1/apps/730/changes?kind=build&limit=10"
curl -X POST -H "X-API-Key: st_admin..." "localhost:8080/v1/apps?appid=440"
```

### Two separate processes

The API **never talks to Steam**. The Steam client relies on gevent, whose monkey
patching breaks the server's asyncio loop; running both in the same process
freezes it. So `POST /v1/apps` records the intent and answers immediately, then
the collector -- the only thing allowed to talk to Steam -- fetches the initial
state on its next pass.

## Web interface

Served at the root by the same process as the API:

| Page | Contents |
|---|---|
| `/` | tracked games, thumbnails, volume and date of the last change |
| `/app.html?appid=730` | 12 tabs: Store info, Charts, Patches, Metadata, Packages, Depots, Branches, Configuration, Cloud saves, Screenshots, Related apps, Update history |
| `/about.html` | what the service is, the history limitation, API examples |
| `/changes.html` | recent feed, across all games |

The look is modelled on SteamDB: dark background, dense typography, bordered
panels, colored diffs. Assets preview on hover and download on click.

The API lives under `/v1`, `/health`, `/api` and `/docs`; everything else is
served by the interface. The static mount is registered LAST in `api.py`: mounted
on `/`, it would otherwise swallow every route declared after it.

## Deployment

See `deploy/steamtrack.service` for a systemd unit. The database lives in
`data/steamtrack.db` (SQLite, WAL mode: the collector writes while the API reads).

Two services, two roles:

| Unit | Role |
|---|---|
| `steamtrack.service` | PICS collector, **the only heavy writer** to the database |
| `steamtrack-api.service` | uvicorn, 3 workers, reads the database and serves `web/` |

The API runs with **3 workers** on 2 vCPUs: the endpoints are synchronous and
spend most of their time blocked on SQLite, so 2 workers keep both cores busy and
the 3rd absorbs disk waits, without overshooting 2 GB of RAM. This is safe: WAL
allows several concurrent readers, each request opens its own connection, and the
quota counters live in the `api_usage` table -- so they are shared across workers
rather than per-process.

## Going public

Follow this order. Steps marked **[HUMAN]** cannot be scripted: they require a
browser, an account, or a decision.

### 1. Check the quotas BEFORE opening up

This is the most important point, and the only one you cannot fix after the fact.

```bash
grep ANON_QUOTA steamtrack/auth.py     # must be an integer, never None
```

`ANON_QUOTA` (in `steamtrack/auth.py`) caps visitors without a key. `None` would
mean unlimited: once public, that lets anyone saturate the VM. Current value:
`600` requests/hour per IP address, roughly 75 game pages per hour per visitor.

Create yourself an unlimited key to keep unrestricted access, before opening up:

```bash
steamtrack key add perso --admin        # --quota omitted = unlimited
# `perso` is positional (not --label). Without --admin, the key is read-only:
# POST and DELETE /v1/apps would answer 403.
```

### 2. Backups in place before any traffic

A database without backups should not be exposed.

```bash
sudo install -m 755 deploy/backup.sh /opt/steamtrack/deploy/backup.sh
sudo -u steamtrack /opt/steamtrack/deploy/backup.sh   # first manual run
```

Then as a daily cron job (7-day rotation, already handled by the script):

```
15 4 * * *  steamtrack  /opt/steamtrack/deploy/backup.sh >> /var/log/steamtrack-backup.log 2>&1
```

The script uses sqlite3's `.backup`, **not** a `cp`: copying a WAL-mode file
while it is live gives you a database that is stale at best and corrupt at worst.
Every backup is read back (`PRAGMA integrity_check`) before being published under
its final name.

### 3. API with multiple workers

```bash
sudo cp deploy/steamtrack-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart steamtrack-api
```

`--proxy-headers --forwarded-allow-ips 127.0.0.1` is **mandatory** behind the
tunnel: without those options uvicorn ignores `X-Forwarded-For`, every request
looks like it comes from 127.0.0.1, and all anonymous visitors share a single
quota bucket -- the first heavy user then blocks everyone else. Never use `*`:
that would let a client spoof its IP, and therefore its quota, with a forged
header.

### 4. Firewall

Run this **before** opening the tunnel.

```bash
sudo ./deploy/firewall.sh              # default: the LAN declared in the script
sudo ./deploy/firewall.sh 10.0.0.0/24  # another LAN
```

Idempotent, safe to re-run. It adds the SSH rule **before** `ufw enable`: the
reverse order would cut the current SSH session and make the VM unreachable
outside the Proxmox console. Keep a second SSH session open anyway during the
operation.

Port 8080 stays restricted to the LAN. The tunnel is an **outbound** connection:
it needs no inbound port, and nothing has to be opened on the router.

### 5. Cloudflare tunnel **[HUMAN]**

Detailed procedure at the top of `deploy/cloudflared.service`. Summary:

```bash
cloudflared tunnel login                 # [HUMAN] browser + Cloudflare account
cloudflared tunnel create steamtrack
cloudflared tunnel route dns steamtrack steamtrack.example.com   # [HUMAN] domain
```

`tunnel login` opens a URL you have to approve in a browser and assumes a
Cloudflare account that already owns a domain: no agent can do it for you. The
resulting `<UUID>.json` file is a secret (`chmod 600`).

Then install the unit:

```bash
sudo cp deploy/cloudflared.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared
```

### 6. Checks before announcing the URL

| Check | Expected |
|---|---|
| `curl https://steamtrack.example.com/health` | `{"status":"ok",...}` |
| `curl http://<vm-lan-ip>:8080/health` from the LAN | answers |
| 8080 from outside | **unreachable** |
| `curl -sD- -o/dev/null https://.../v1/apps \| grep -i ratelimit` | `x-ratelimit-limit: 600` |
| Two public requests from different IPs | **independent** counters |
| `sudo ufw status verbose` | `deny (incoming)`, 22 and 8080 restricted to the LAN |
| `systemctl status steamtrack steamtrack-api cloudflared` | all three `active` |
| Today's backup present in `/opt/steamtrack/backups` | yes |

If the counters for two different IPs move together, `--proxy-headers` is not
active: redo step 3 before opening to the public.

Change the VM's root password, and use an SSH key rather than a password, before
any exposure.

## Public tunnel

Two units, depending on what you need:

| Unit | Address | Requirements |
|---|---|---|
| `cloudflared-quick.service` | random one on trycloudflare.com, **changes on every restart** | none |
| `cloudflared.service` | stable, on your own domain | `cloudflared tunnel login`: browser + Cloudflare account |

Read the quick tunnel's current address:

```bash
/opt/steamtrack/deploy/tunnel-url.sh
```

The tunnel is an outbound connection: nothing to open on the router, the firewall
stays closed to inbound traffic.

## Architecture

```
steamtrack/
  schema.sql      tables: apps, snapshots, changes, api_keys, state
  db.py           database access
  diff.py         comparison of two appinfo -> tree of differences
  news.py         announcements via ISteamNews
  collector.py    daemon: PICS stream -> diff -> database
  cli.py          add / remove / browse / keys
```

The event format is a tree of typed segments (`text`, `field`, `del`, `ins`,
`muted`), rendered directly by the interface and exposed as-is by the API. Assets
carry their URL, which is what makes preview and download possible.

## Status

- [x] PICS collector, diff, database
- [x] CLI: add / remove / list / browse
- [x] API keys in the database
- [x] HTTP API (keys, quotas, OpenAPI)
- [x] Web interface
- [x] Player counts, prices, depots, branches, store page
- [x] Automatic page refresh
