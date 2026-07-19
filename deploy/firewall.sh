#!/usr/bin/env bash
# Pare-feu de la VM steamtrack. Idempotent : relancable sans effet de bord,
# ufw remplace une regle identique au lieu de l'empiler.
#
# Politique : tout refuser en entree, SSH et 8080 depuis le LAN uniquement.
# L'acces public passe par le tunnel cloudflared, une connexion SORTANTE : il
# n'a besoin d'AUCUN port entrant. Ouvrir 8080 au monde annulerait le benefice
# du tunnel et exposerait l'API sans le filtrage de Cloudflare devant.
#
# Usage :  sudo ./firewall.sh  [CIDR_LAN]
# Defaut :  192.168.1.0/24

set -euo pipefail

LAN="${1:-192.168.1.0/24}"

# Reseaux d'administration supplementaires, en plus du LAN.
#
# Piege vecu : un poste sous WSL2 sort avec une adresse traduite (ici 10.5.5.x)
# qui n'est PAS celle que montre `ip route get` sur ce poste. Autoriser le LAN
# seul avait donc coupe l'acces SSH de l'administrateur, sans recours puisque
# la commande de reparation demande justement un shell.
#
# Avant d'appliquer ce script depuis une machine distante, VERIFIER l'adresse
# reellement vue par le serveur :
#     journalctl -u ssh -n 5     (cote serveur)
# ou, si un service HTTP tourne deja, lire l'IP cliente dans ses journaux.
ADMIN_NETS="${ADMIN_NETS:-10.5.5.0/24}"
API_PORT=8080

if [[ $EUID -ne 0 ]]; then
    echo "Ce script doit tourner en root (sudo ./firewall.sh)." >&2
    exit 1
fi

if ! command -v ufw >/dev/null 2>&1; then
    echo "ufw n'est pas installe :  apt install ufw" >&2
    exit 1
fi

# Garde-fou : un CIDR mal saisi poserait des regles inutiles et laisserait la
# session SSH sans regle correspondante au moment de l'activation.
if [[ ! "$LAN" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
    echo "CIDR invalide : '$LAN' (attendu par ex. 192.168.1.0/24)" >&2
    exit 1
fi

echo "== Pare-feu steamtrack : LAN autorise = $LAN"

# ---------------------------------------------------------------------------
# 1. SSH D'ABORD, AVANT toute activation.
#
# ufw enable applique la politique par defaut deny immediatement. Si la regle
# SSH n'existe pas encore a cet instant, la session SSH en cours tombe et la VM
# devient injoignable autrement que par la console Proxmox. L'ordre de ce
# script est donc : regles -> defauts -> enable, jamais l'inverse.
# ---------------------------------------------------------------------------
echo "-- SSH depuis $LAN"
ufw allow from "$LAN" to any port 22 proto tcp comment 'SSH LAN'

# ---------------------------------------------------------------------------
# 2. API : LAN uniquement.
# ---------------------------------------------------------------------------
echo "-- API $API_PORT depuis $LAN"
ufw allow from "$LAN" to any port "$API_PORT" proto tcp comment 'steamtrack API LAN'

# Filet de securite : si une ancienne regle ouvrait 8080 a tout le monde, elle
# resterait active et rendrait les deux regles ci-dessus decoratives. ufw ne
# remplace pas une regle "any" par une regle "from LAN" : ce sont deux entrees
# distinctes, et la plus permissive gagne. On la retire donc explicitement.
# "|| true" : delete echoue si la regle n'existe pas, ce qui est le cas normal.
echo "-- Retrait d'une eventuelle ouverture publique de $API_PORT"
ufw delete allow "$API_PORT"/tcp 2>/dev/null || true
ufw delete allow "$API_PORT" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Politiques par defaut.
#
# outgoing allow est indispensable : le collecteur joint Steam et cloudflared
# etablit le tunnel. Les couper isolerait completement le service.
# ---------------------------------------------------------------------------
echo "-- Politiques par defaut : deny in / allow out"
ufw default deny incoming
ufw default allow outgoing

# ---------------------------------------------------------------------------
# 4. Activation en dernier, une fois les regles en place.
# ---------------------------------------------------------------------------
if ufw status | grep -q '^Status: active'; then
    echo "-- ufw deja actif, regles rechargees"
    ufw reload
else
    echo "-- Activation de ufw"
    # --force : evite la question interactive "may disrupt existing ssh
    # connections", qui bloquerait le script en execution non interactive.
    # La regle SSH est deja posee a ce stade.
    ufw --force enable
fi

echo
ufw status verbose

cat <<'EOF'

Verifications :
  - Depuis un autre poste du LAN :  curl -sS http://192.168.1.55:8080/health
  - Depuis l'exterieur, le port 8080 doit etre INJOIGNABLE. Seule l'URL du
    tunnel Cloudflare doit repondre.
  - La session SSH courante doit toujours etre vivante. Avant de la fermer,
    en ouvrir une seconde pour confirmer que SSH repond encore.
EOF
