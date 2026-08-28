#!/usr/bin/env bash
# Declenche le workflow du tracker Fading Echo depuis la VM.
#
# POURQUOI ce script existe : le workflow declare `cron: */10`, soit 144 passages
# par jour. GitHub en execute entre 15 et 32 (releve du 23 au 27/08/2026), avec
# des trous mesures de 9 h 24 et 11 h 06. Ce n'est pas une panne, c'est la regle :
# GitHub Actions ne garantit pas la ponctualite des crons et sacrifie en priorite
# ceux des depots peu actifs, aux minutes rondes que tout le monde demande.
# Aucun reglage du cron n'y change grand-chose -- decaler les minutes aide un
# peu, promettre 10 minutes ne sert a rien.
#
# La VM, elle, a une horloge fiable et tourne 24/7 : autant s'en servir. Un
# `workflow_dispatch` obtient exactement le meme run, sans passer par le
# planificateur de GitHub. Le cron du workflow est conserve : il devient le
# filet de secours pour les moments ou la VM est eteinte, ce qui inverse
# proprement les roles.
set -euo pipefail

REPO="${FE_TRACKER_REPO:-cheapmanga/FadingUtilities}"
WORKFLOW="${FE_TRACKER_WORKFLOW:-fe-tracker.yml}"
REF="${FE_TRACKER_REF:-main}"
TOKEN_FILE="${STEAMTRACK_GH_TOKEN_FILE:-/etc/steamtrack/github-token}"

# En dessous de ce delai depuis le dernier run, on ne declenche pas : le cron de
# GitHub vient peut-etre de passer, et deux runs rapproches ne produiraient que
# deux snapshots identiques. Le but est de combler ses trous, pas de doubler son
# travail.
MIN_AGE_S="${FE_TRACKER_MIN_AGE_S:-720}"

log() { echo "$(date -Is)  $*"; }

[[ -r "$TOKEN_FILE" ]] || { log "jeton introuvable : $TOKEN_FILE"; exit 1; }
TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")

API="https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW"

# Age du dernier run, en secondes. Une lecture impossible (reseau, quota) ne
# doit pas empecher le declenchement : mieux vaut un run de trop qu'un trou.
AGE=$(curl -sf --max-time 20 \
        -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "$API/runs?per_page=1" 2>/dev/null \
      | python3 -c '
import datetime, json, sys
try:
    runs = json.load(sys.stdin).get("workflow_runs") or []
    t = datetime.datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
    print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()))
except Exception:
    print(-1)' || echo -1)

if [[ "$AGE" =~ ^[0-9]+$ ]] && (( AGE < MIN_AGE_S )); then
    exit 0                      # GitHub vient de passer : rien a faire
fi

CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
            -X POST \
            -H "Authorization: Bearer $TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "$API/dispatches" -d "{\"ref\":\"$REF\"}" || echo 000)

# 204 est la seule reponse de succes de cet endpoint : il ne renvoie pas de corps.
if [[ "$CODE" == "204" ]]; then
    if [[ "$AGE" == "-1" ]]; then
        log "tracker declenche (age du dernier run inconnu)"
    else
        log "tracker declenche (dernier run il y a $((AGE / 60)) min)"
    fi
else
    log "echec du declenchement (HTTP $CODE)"
    exit 1
fi
