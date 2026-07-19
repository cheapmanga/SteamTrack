#!/usr/bin/env bash
# Publie l'adresse courante du quick tunnel, pour que la page passerelle la lise.
#
# Le quick tunnel tire une adresse au hasard a chaque demarrage : un lien
# partage meurt au premier reboot. Plutot que d'acheter un domaine, on publie
# l'adresse dans un fichier a l'emplacement stable, et la passerelle l'y lit.
#
# Le fichier est ecrit via l'API GitHub Contents (pas de clone, pas de working
# tree a maintenir sur la VM). Il n'est reecrit QUE si l'adresse a change :
# sinon chaque passage du timer creerait un commit inutile.
set -euo pipefail

REPO="${STEAMTRACK_GH_REPO:-cheapmanga/SteamTrack}"
BRANCH="${STEAMTRACK_GH_BRANCH:-main}"
FILE="${STEAMTRACK_GH_FILE:-tunnel.json}"
TOKEN_FILE="${STEAMTRACK_GH_TOKEN_FILE:-/etc/steamtrack/github-token}"
STATE="/var/lib/steamtrack/published-url"

log() { echo "$(date -Is)  $*"; }

[[ -r "$TOKEN_FILE" ]] || { log "jeton introuvable : $TOKEN_FILE"; exit 1; }
TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")

URL=$(/opt/steamtrack/deploy/tunnel-url.sh 2>/dev/null || true)
if [[ -z "$URL" ]]; then
    log "aucune adresse de tunnel disponible (service arrete ou pas encore pret)"
    exit 0
fi

mkdir -p "$(dirname "$STATE")"
if [[ -f "$STATE" ]] && [[ "$(cat "$STATE")" == "$URL" ]]; then
    exit 0                      # inchangee : rien a publier
fi

log "nouvelle adresse : $URL"

PAYLOAD=$(URL="$URL" python3 -c '
import json, os, datetime
print(json.dumps({
    "url": os.environ["URL"],
    "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "note": "Adresse du quick tunnel Cloudflare. Change a chaque redemarrage du service.",
}, indent=1))')

API="https://api.github.com/repos/$REPO/contents/$FILE"

# L'API Contents exige le SHA du fichier existant pour le remplacer ; son
# absence signifie simplement qu'on le cree.
SHA=$(curl -sS -H "Authorization: Bearer $TOKEN" \
           -H "Accept: application/vnd.github+json" \
           "$API?ref=$BRANCH" 2>/dev/null \
      | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("sha",""))
except Exception: print("")')

BODY=$(PAYLOAD="$PAYLOAD" SHA="$SHA" BRANCH="$BRANCH" python3 -c '
import base64, json, os
body = {
    "message": "gateway: adresse du tunnel steamtrack",
    "content": base64.b64encode(os.environ["PAYLOAD"].encode()).decode(),
    "branch": os.environ["BRANCH"],
}
if os.environ.get("SHA"):
    body["sha"] = os.environ["SHA"]
print(json.dumps(body))')

# Une seconde tentative sur 409 : le SHA lu peut avoir ete invalide entre le
# GET et le PUT (timer et appel manuel qui se croisent, par exemple). Relire le
# SHA et rejouer suffit -- c'est une course, pas une erreur de fond.
publish() {
    curl -sS -o /tmp/steamtrack-publish.out -w '%{http_code}' \
         -X PUT -H "Authorization: Bearer $TOKEN" \
         -H "Accept: application/vnd.github+json" \
         -d "$1" "$API"
}

CODE=$(publish "$BODY")

if [[ "$CODE" == "409" ]]; then
    log "conflit de version, nouvelle tentative"
    sleep 2
    SHA=$(curl -sS -H "Authorization: Bearer $TOKEN" \
               -H "Accept: application/vnd.github+json" \
               "$API?ref=$BRANCH" 2>/dev/null \
          | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("sha",""))
except Exception: print("")')
    BODY=$(PAYLOAD="$PAYLOAD" SHA="$SHA" BRANCH="$BRANCH" python3 -c '
import base64, json, os
body = {
    "message": "gateway: adresse du tunnel steamtrack",
    "content": base64.b64encode(os.environ["PAYLOAD"].encode()).decode(),
    "branch": os.environ["BRANCH"],
}
if os.environ.get("SHA"):
    body["sha"] = os.environ["SHA"]
print(json.dumps(body))')
    CODE=$(publish "$BODY")
fi

if [[ "$CODE" == "200" || "$CODE" == "201" ]]; then
    echo "$URL" > "$STATE"
    log "publiee (HTTP $CODE)"
else
    log "echec de publication (HTTP $CODE) : $(head -c 200 /tmp/steamtrack-publish.out)"
    exit 1
fi
