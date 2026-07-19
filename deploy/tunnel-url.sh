#!/usr/bin/env bash
# Affiche l'URL publique du quick tunnel Cloudflare.
#
# cloudflared n'ecrit cette adresse nulle part : elle n'apparait que dans son
# journal, au demarrage. On la relit donc la, en remontant assez loin pour
# retrouver le dernier demarrage du service.
set -euo pipefail

UNIT="${1:-cloudflared-quick}"

if ! systemctl is-active --quiet "$UNIT"; then
    echo "Le service $UNIT ne tourne pas." >&2
    echo "  systemctl start $UNIT" >&2
    exit 1
fi

# --since : on ne veut pas l'URL d'une session precedente, morte depuis.
START=$(systemctl show "$UNIT" -p ActiveEnterTimestamp --value)
URL=$(journalctl -u "$UNIT" --since "${START:--1h}" --no-pager -o cat 2>/dev/null \
      | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)

if [[ -z "$URL" ]]; then
    echo "Adresse introuvable dans le journal." >&2
    echo "Le tunnel vient peut-etre de demarrer : reessayer dans ~20 s." >&2
    exit 1
fi

echo "$URL"
