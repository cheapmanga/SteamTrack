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
#
# SECONDE FAMILLE DE PANNES, ajoutee le 28/08/2026 : celles qui ne se voient
# nulle part parce que le service, lui, marche parfaitement. Le jeton GitHub a
# expire le 15/08 ; publish-tunnel-url a echoue toutes les deux minutes pendant
# DIX JOURS sans que personne le remarque, puisque l'adresse deja publiee etait
# encore la bonne. La panne n'est devenue visible que le 25/08, quand
# cloudflared a redemarre et tire une adresse qu'il etait desormais impossible
# de publier : quatorze jours entre la cause et le symptome.
#
# Les sections 1 a 4 ne pouvaient pas la voir -- elles sondent l'adresse
# COURANTE, qui repondait tres bien, jamais celle que le monde exterieur lit.
# La section 5 comble ce trou, et le fait dans l'ordre qui compte : d'abord la
# validite du jeton (la cause, detectable tout de suite), ensuite l'ecart entre
# adresse publiee et adresse vivante (le symptome, dix jours plus tard).
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

# --- Alertes ---------------------------------------------------------------
#
# Une alerte n'a d'interet que si elle SORT de la machine. Les dix jours de
# panne silencieuse d'aout n'ont pas manque d'un test -- le journal disait tout,
# toutes les deux minutes -- ils ont manque de quelqu'un pour le lire. Le canal
# reste neanmoins un detail de configuration : ce script se contente d'appeler
# un programme s'il en trouve un, et fonctionne normalement sans (journal seul).
#
# Contrat de $ALERT_CMD : recoit le message en $1, doit rendre 0 s'il est parti.
ALERT_CMD="${STEAMTRACK_ALERT_CMD:-/etc/steamtrack/alert-command}"

# Une panne qui dure ne doit pas devenir un flux de notifications qu'on finit
# par filtrer, mais elle ne doit pas non plus s'effacer apres un seul envoi
# manque : on repete donc une fois par jour, pas plus.
ALERT_REPEAT_S="${STEAMTRACK_ALERT_REPEAT_S:-86400}"

ALERT_DIR="$STATE_DIR/alertes"

# Chaque alerte garde deux horodatages : sa premiere apparition (qui donne son
# age, la seule facon de distinguer un hoquet d'une panne installee) et son
# dernier envoi (qui espace les rappels).
alerte() {
    local cle="$1" msg="$2" maintenant t0 dernier age=""
    maintenant=$(date +%s)
    mkdir -p "$ALERT_DIR"

    if [[ -f "$ALERT_DIR/$cle" ]]; then
        read -r t0 dernier < "$ALERT_DIR/$cle"
    else
        t0=$maintenant
        dernier=0
    fi

    if (( maintenant - t0 >= 3600 )); then
        age=" -- dure depuis $(( (maintenant - t0) / 3600 )) h"
    fi

    log "ALERTE [$cle] $msg$age"

    # Ce qui sort de la machine est expurge des adresses de tunnel. Le canal
    # d'alerte est un service public tiers : y publier l'adresse de l'API, ce
    # serait donner a un inconnu le point d'entree que toute l'architecture
    # s'emploie a ne pas exposer. Le journal local, lui, garde tout.
    local msg_ext
    msg_ext=$(printf '%s' "$msg$age" \
              | sed -E 's#https://[a-z0-9.-]+\.trycloudflare\.com#<adresse du tunnel>#g')

    if (( maintenant - dernier >= ALERT_REPEAT_S )); then
        if [[ -x "$ALERT_CMD" ]]; then
            if "$ALERT_CMD" "steamtrack: $msg_ext"; then
                dernier=$maintenant
            else
                log "[$cle] l'envoi de l'alerte a echoue -- on reessaiera"
            fi
        fi
    fi

    echo "$t0 $dernier" > "$ALERT_DIR/$cle"
}

# Le retour a la normale se signale aussi : sans ca, on ne sait jamais si le
# silence veut dire "repare" ou "le watchdog ne tourne plus".
alerte_resolue() {
    local cle="$1"
    [[ -f "$ALERT_DIR/$cle" ]] || return 0
    rm -f "$ALERT_DIR/$cle"
    log "[$cle] rentre dans l'ordre"
    if [[ -x "$ALERT_CMD" ]]; then
        "$ALERT_CMD" "steamtrack: $cle -- rentre dans l'ordre" || true
    fi
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
# Anciennement `exit 0` : c'etait suffisant tant que le tunnel etait la
# derniere section, mais la section 5 doit tourner meme API morte -- un jeton
# expire se constate independamment, et c'est justement le genre de panne qu'on
# ne veut plus decouvrir avec quinze jours de retard.
API_MORTE=""
if ! curl -sf --max-time 10 -o /dev/null "$API_LOCAL"; then
    act steamtrack-api "l'API ne repond pas sur 127.0.0.1:8080"
    API_MORTE=1
fi

# --- 4. Tunnel -------------------------------------------------------------

if [[ -z "$API_MORTE" ]] && systemctl is-active --quiet cloudflared-quick; then

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
fi

# --- 5. Publication de l'adresse -------------------------------------------
#
# Tout ce qui precede juge le service tel qu'il tourne ici. Cette section juge
# ce que le monde exterieur, lui, arrive a lire -- et c'est une question
# distincte : entre le 15 et le 25/08/2026 le service etait parfaitement sain
# et parfaitement injoignable en devenir, personne ne pouvait le savoir.

TOKEN_FILE="${STEAMTRACK_GH_TOKEN_FILE:-/etc/steamtrack/github-token}"
GH_REPO="${STEAMTRACK_GH_REPO:-cheapmanga/SteamTrack}"
GH_BRANCH="${STEAMTRACK_GH_BRANCH:-main}"
GH_FILE="${STEAMTRACK_GH_FILE:-tunnel.json}"

# --- 5a. Le jeton est-il encore valide ? ---
#
# C'est LA sonde qui manquait. Elle ne regarde pas si le service marche, elle
# regarde s'il pourra se reparer le jour ou il en aura besoin. Un jeton mort
# est une panne a retardement : inoffensive jusqu'au prochain redemarrage de
# cloudflared, fatale a la seconde d'apres. La detecter le jour meme, c'est
# transformer quatorze jours d'indisponibilite en une reparation de trois
# minutes -- l'ecart entre les deux tient entierement dans cette requete.

if [[ -r "$TOKEN_FILE" ]]; then
    TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
    HDRS=$(mktemp)

    # /user est le point le moins cher qui exige une authentification : il ne
    # lit rien, n'ecrit rien, et suffit a distinguer un jeton vivant d'un jeton
    # revoque ou expire.
    CODE=$(curl -sS -o /dev/null -D "$HDRS" -w '%{http_code}' --max-time 15 \
                -H "Authorization: Bearer $TOKEN" \
                -H "Accept: application/vnd.github+json" \
                https://api.github.com/user 2>/dev/null || echo 000)

    # GitHub annonce lui-meme la date d'expiration des jetons fin-grained, dans
    # un en-tete de reponse. On peut donc prevenir AVANT la panne au lieu de la
    # constater apres : c'est gratuit, il suffisait de lire.
    EXP=$(grep -i '^github-authentication-token-expiration:' "$HDRS" \
          | cut -d: -f2- | tr -d '\r' | sed 's/^ *//' || true)
    rm -f "$HDRS"

    case "$CODE" in
        200)
            alerte_resolue jeton-github
            if [[ -n "$EXP" ]]; then
                FIN=$(date -d "$EXP" +%s 2>/dev/null || echo 0)
                if (( FIN > 0 )); then
                    RESTE=$(( (FIN - $(date +%s)) / 86400 ))
                    if (( RESTE <= 14 )); then
                        alerte jeton-github-expire-bientot \
                            "le jeton GitHub expire dans $RESTE jour(s) ($EXP). Le renouveler MAINTENANT : une fois expire, l'adresse du tunnel ne peut plus etre publiee et le service devient injoignable au premier redemarrage de cloudflared."
                    else
                        alerte_resolue jeton-github-expire-bientot
                    fi
                fi
            fi
            ;;
        401|403)
            alerte jeton-github \
                "le jeton GitHub est refuse (HTTP $CODE). L'adresse du tunnel ne peut plus etre publiee. Le service repond encore sur son adresse actuelle, mais deviendra injoignable de l'exterieur des le prochain redemarrage de cloudflared. Remede : renouveler $TOKEN_FILE puis 'systemctl start publish-tunnel-url.service'."
            ;;
        *)
            # Un 500 chez GitHub ou un reseau qui hoquette ne dit rien sur le
            # jeton : se taire vaut mieux qu'alerter a tort.
            log "jeton GitHub : verification impossible (HTTP $CODE), on repassera"
            ;;
    esac
fi

# --- 5b. L'adresse publiee est-elle l'adresse vivante ? ---

# Lue par l'API Contents et non par raw.githubusercontent : le CDN de raw sert
# le fichier avec max-age=300 et rien ne perce ce cache (teste le 19/07/2026).
# On comparerait donc regulierement a une valeur vieille de cinq minutes, et on
# alerterait pour un ecart qui n'existe deja plus.
adresse_publiee() {
    curl -sf --max-time 15 \
         -H "Accept: application/vnd.github.raw" \
         "https://api.github.com/repos/$GH_REPO/contents/$GH_FILE?ref=$GH_BRANCH" \
         2>/dev/null \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("url",""))
except Exception: print("")'
}

VIVANTE=$(/opt/steamtrack/deploy/tunnel-url.sh 2>/dev/null || true)
PUBLIEE=$(adresse_publiee || true)

if [[ -z "$VIVANTE" || -z "$PUBLIEE" ]]; then
    # Tunnel arrete, ou GitHub illisible : les sections precedentes ont deja
    # traite le premier cas, et le second n'est pas notre panne.
    :
elif [[ "$PUBLIEE" == "$VIVANTE" ]]; then
    alerte_resolue adresse-publiee
else
    # Le timer publie tout seul toutes les deux minutes, et le watchdog ne
    # passe que toutes les cinq : un ecart visible ici a donc deja survecu a
    # deux tentatives. On en tente malgre tout une derniere, sous nos yeux --
    # si elle passe, la panne etait un simple croisement de calendriers ; si
    # elle echoue, on a la preuve et le code d'erreur dans le meme journal.
    log "adresse publiee ($PUBLIEE) != adresse vivante ($VIVANTE) -- tentative de publication"
    systemctl start publish-tunnel-url.service || true
    sleep 10

    if [[ "$(adresse_publiee || true)" == "$VIVANTE" ]]; then
        log "adresse republiee"
        alerte_resolue adresse-publiee
    else
        alerte adresse-publiee \
            "la passerelle publie $PUBLIEE alors que le tunnel vit sur $VIVANTE : le service est injoignable de l'exterieur. La publication echoue -- voir 'journalctl -u publish-tunnel-url -n 20'."
    fi
fi
