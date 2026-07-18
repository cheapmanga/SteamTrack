#!/usr/bin/env bash
# Sauvegarde de la base steamtrack. Rotation sur 7 jours.
#
# POURQUOI PAS UN cp : la base est en WAL et le collecteur ecrit en continu.
# Copier le fichier .db a chaud capture un instantane a un moment ou une
# transaction peut etre a moitie ecrite, et laisse de cote -wal et -shm : la
# copie obtenue est au mieux en retard, au pire corrompue et illisible.
#
# ".backup" de sqlite3 utilise l'API de sauvegarde en ligne : il lit la base
# page par page en prenant les verrous qu'il faut, et produit un fichier unique
# et coherent -- WAL replaye inclus -- sans arreter le collecteur.
#
# Usage :  ./backup.sh
# Cron   :  15 4 * * *  steamtrack  /opt/steamtrack/deploy/backup.sh >> /var/log/steamtrack-backup.log 2>&1

set -euo pipefail

DB="${STEAMTRACK_DB:-/opt/steamtrack/data/steamtrack.db}"
BACKUP_DIR="${STEAMTRACK_BACKUP_DIR:-/opt/steamtrack/backups}"
KEEP_DAYS=7

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/steamtrack-$STAMP.db"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 est absent :  apt install sqlite3" >&2
    exit 1
fi

if [[ ! -f "$DB" ]]; then
    echo "Base introuvable : $DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

echo "[$(date -Is)] sauvegarde de $DB -> $DEST"

# .backup peut echouer si un ecrivain garde la base occupee. .timeout laisse
# 60 s pour attendre son tour au lieu d'abandonner sur "database is locked".
# On ecrit dans un .part : un echec en cours de route ne doit pas laisser un
# fichier tronque qui passerait pour une sauvegarde valide.
sqlite3 "$DB" ".timeout 60000" ".backup '$DEST.part'"

# Verification avant publication : une sauvegarde jamais relue n'est pas une
# sauvegarde. integrity_check lit toutes les pages et signale toute corruption.
RESULT="$(sqlite3 "$DEST.part" 'PRAGMA integrity_check;')"
if [[ "$RESULT" != "ok" ]]; then
    echo "ECHEC : integrite de la sauvegarde compromise : $RESULT" >&2
    rm -f "$DEST.part"
    exit 1
fi

# On compresse AVANT de publier, puis on renomme.
#
# L'ordre inverse (mv puis gzip) laissait une fenetre ou un .db nu existait
# sous son nom definitif : si gzip echouait, `set -e` arretait le script et ce
# fichier non compresse restait indefiniment -- la rotation ci-dessous ne cible
# que *.db.gz et *.db.part, elle ne l'aurait jamais efface.
gzip -9 "$DEST.part"
mv "$DEST.part.gz" "$DEST.gz"
DEST="$DEST.gz"
echo "[$(date -Is)] ok : $DEST ($(du -h "$DEST" | cut -f1))"

# ---------------------------------------------------------------------------
# Rotation. -mtime +7 supprime ce qui a plus de 7 jours pleins.
# Le motif ne cible que nos propres fichiers : rien d'autre du repertoire n'est
# touche, meme si on l'a pointe sur un dossier partage.
# ---------------------------------------------------------------------------
echo "-- rotation : suppression des sauvegardes de plus de $KEEP_DAYS jours"
find "$BACKUP_DIR" -maxdepth 1 -name 'steamtrack-*.db.gz' -type f \
     -mtime "+$KEEP_DAYS" -print -delete

# Nettoyage des .part orphelins laisses par une execution interrompue.
find "$BACKUP_DIR" -maxdepth 1 -name 'steamtrack-*.db.part' -type f \
     -mtime +1 -print -delete

COUNT="$(find "$BACKUP_DIR" -maxdepth 1 -name 'steamtrack-*.db.gz' -type f | wc -l)"
echo "[$(date -Is)] $COUNT sauvegarde(s) conservee(s) dans $BACKUP_DIR"

# Restauration :
#   systemctl stop steamtrack steamtrack-api
#   gunzip -c steamtrack-AAAAMMJJ-HHMMSS.db.gz > /opt/steamtrack/data/steamtrack.db
#   rm -f /opt/steamtrack/data/steamtrack.db-wal /opt/steamtrack/data/steamtrack.db-shm
#   chown steamtrack:steamtrack /opt/steamtrack/data/steamtrack.db
#   systemctl start steamtrack steamtrack-api
# Les -wal/-shm residuels appartiennent a l'ANCIENNE base : les laisser en place
# a cote d'un fichier restaure fait diverger SQLite ou refuser d'ouvrir.
