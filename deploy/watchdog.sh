#!/usr/bin/env bash
# Surveille les trois services et relance ceux qui sont figes.
#
# POURQUOI : le 21/07/2026, une coupure Internet de 50 minutes cote box a mis
# le service hors ligne pendant TROIS JOURS. Le reseau etait revenu depuis
# longtemps, mais aucun des deux services ne sait se relever tout seul :
#
#   - cloudflared perd son quick tunnel pendant la coupure ; l'edge libere
#     l'adresse, et au retour du reseau cloudflared retente la MEME session a
#     l'infini. En mode --url il ne redemande jamais une nouvelle adresse en
#     cours de vie : seul un redemarrage du processus en obtient une.
#   - le collecteur plante, systemd le relance en pleine coupure, son bootstrap
#     WebAPI echoue et la lib steam se coince en SYN-SENT sur un CM injoignable.
#     Pas de timeout de connexion, pas de reprise : fige pour toujours.
#
# Dans les deux cas le processus reste VIVANT : Restart=always ne se declenche
# pas et `systemctl is-active` repond `active`. C'est precisement le trou que ce
# script bouche -- il juge sur des preuves d'activite, pas sur l'etat systemd.
set -euo pipefail

# Le collecteur logge en continu : sur les deux jours precedant la panne, le
# plus grand silence mesure etait de 52 s. 10 minutes laissent donc plus de dix
# fois la marge normale -- assez pour ne jamais confondre "calme" et "fige".
STALL_S="${STEAMTRACK_STALL_S:-600}"

# Delai minimal entre deux interventions sur le MEME service. Un redemarrage qui
# ne resout rien (panne de fond, base corrompue) ne doit pas devenir une boucle
# de redemarrages toutes les 5 minutes : mieux vaut un service arrete et un
# journal lisible qu'un service qui se relance sans fin.
GRACE_S="${STEAMTRACK_GRACE_S:-1800}"

STATE_DIR=/var/lib/steamtrack/watchdog
API_LOCAL=http://127.0.0.1:8080/api

log() { echo "$(date -Is)  $*"; }

# --- Garde-fou anti-boucle -------------------------------------------------

recently_acted() {
    local f="$STATE_DIR/$1"
    [[ -f "$f" ]] || return 1
    (( $(date +%s) - $(cat "$f") < GRACE_S ))
}

mark_acted() {
    mkdir -p "$STATE_DIR"
    date +%s > "$STATE_DIR/$1"
}

# Un redemarrage n'est jamais tente sans passer par ici : c'est le seul endroit
# qui ecrit le journal de ce qu'on a fait, et le seul qui respecte le delai.
act() {
    local unit="$1" raison="$2"
    if recently_acted "$unit"; then
        log "$unit : $raison -- deja relance il y a moins de $((GRACE_S/60)) min, on n'insiste pas"
        return 1
    fi
    log "$unit : $raison -- redemarrage"
    mark_acted "$unit"
    systemctl restart "$unit" || log "$unit : le redemarrage a echoue"
    return 0
}

# --- 1. Internet -----------------------------------------------------------

# Rien de ce qui suit n'a de sens si la VM n'a pas de sortie : les sondes
# echoueraient toutes, et on redemarrerait trois services sains pour rien.
# Pendant une coupure on ne fait donc rien du tout -- c'est au retour du reseau,
# au passage suivant, que le watchdog repare. Deux cibles independantes pour ne
# pas prendre la panne d'un seul hebergeur pour une coupure.
online() {
    curl -sf --max-time 8 -o /dev/null https://cloudflare.com/cdn-cgi/trace && return 0
    curl -sf --max-time 8 -o /dev/null https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/ && return 0
    return 1
}

if ! online; then
    log "pas de sortie Internet : rien a reparer ici, on repassera"
    exit 0
fi

# --- 2. Collecteur : actif mais muet ? -------------------------------------

if systemctl is-active --quiet steamtrack; then
    # Derniere ligne du journal de l'unite, en secondes epoch. On lit l'unite
    # entiere (messages systemd compris) : peu importe QUI a parle, seul
    # compte le fait que quelque chose bouge encore.
    last=$(journalctl -u steamtrack -n1 -o short-unix --no-pager 2>/dev/null \
           | cut -d' ' -f1 | cut -d. -f1)

    if [[ "$last" =~ ^[0-9]+$ ]]; then
        age=$(( $(date +%s) - last ))
        if (( age > STALL_S )); then
            # En secondes sous la minute : avec un seuil abaisse pour un test,
            # "0 min" ne dit rien de ce qui a ete constate.
            if (( age < 120 )); then duree="${age} s"; else duree="$((age/60)) min"; fi
            act steamtrack "aucun log depuis $duree (fige, probablement SYN-SENT sur un CM mort)"
        fi
    else
        log "steamtrack : horodatage du journal illisible, on ne touche a rien"
    fi
fi

# --- 3. API locale ---------------------------------------------------------

# Sondee AVANT le tunnel : si l'API est morte, la sonde du tunnel echouerait
# elle aussi et ferait redemarrer cloudflared pour un probleme qui n'est pas le
# sien. On sort dans ce cas -- le tunnel sera juge au prochain passage, une fois
# l'API rendue a la vie.
if ! curl -sf --max-time 10 -o /dev/null "$API_LOCAL"; then
    act steamtrack-api "l'API ne repond pas sur 127.0.0.1:8080"
    exit 0
fi

# --- 4. Tunnel -------------------------------------------------------------

systemctl is-active --quiet cloudflared-quick || exit 0

# La sonde part de la VM mais fait le tour complet : Internet, edge Cloudflare,
# tunnel, API locale. C'est le seul test qui prouve que l'adresse PUBLIQUE
# marche -- lire le journal de cloudflared ne le dirait pas, puisqu'il boucle
# sur des erreurs en restant "active".
tunnel_ok() {
    local url
    url=$(/opt/steamtrack/deploy/tunnel-url.sh 2>/dev/null) || return 1
    [[ -n "$url" ]] || return 1
    curl -sf --max-time 20 -o /dev/null "$url/api"
}

# Deux essais espaces : un tunnel qui se rattache apres un hoquet reseau met
# quelques secondes, et cela ne justifie pas de changer l'adresse publique --
# chaque redemarrage en tire une nouvelle, que la passerelle mettra jusqu'a
# 5 min a diffuser (cache CDN de raw.githubusercontent).
if ! tunnel_ok; then
    sleep 15
    if ! tunnel_ok; then
        if act cloudflared-quick "l'adresse publique ne repond plus (tunnel perdu)"; then
            # L'adresse a change : la publier tout de suite plutot que
            # d'attendre le passage du timer.
            sleep 20
            systemctl start publish-tunnel-url.service || true
        fi
    fi
fi
