#!/usr/bin/env bash
# Manuelles, SQLite-konsistentes Backup via Python sqlite3.backup.
set -euo pipefail
PROJECT="${1:-crm-dubletten}"
COUNT="${BACKUP_KEEP:-30}"
CID="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" --filter 'label=com.docker.compose.service=app' --format '{{.ID}}' | head -n1)"
[[ -n "$CID" ]] || { echo "Kein app-Container fuer Projekt $PROJECT gefunden." >&2; exit 1; }
docker exec "$CID" python -c '
import datetime,pathlib,sqlite3
src=sqlite3.connect("/app/data/crm.sqlite")
p=pathlib.Path("/app/backups")
p.mkdir(parents=True,exist_ok=True)
dest=p/("crm-"+datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".sqlite")
with sqlite3.connect(dest) as dst: src.backup(dst)
print("SQLite-Backup:",dest)
'
# Loeschen nur in eindeutig bestimmtem lokalen Backupordner; ein anderes
# Zielverzeichnis als /home/buebi/... muss bei Bedarf manuell angepasst werden.
DIR="/home/buebi/docker/crm-dubletten/backups"
if [[ "$COUNT" =~ ^[0-9]+$ ]] && (( COUNT >= 1 )) && [[ -d "$DIR" ]]; then
  find "$DIR" -maxdepth 1 -type f -name 'crm-*.sqlite' -printf '%T@ %p\n' \
    | sort -nr | tail -n +$((COUNT + 1)) | cut -d' ' -f2- \
    | while IFS= read -r old; do [[ -n "$old" ]] && rm -- "$old"; done
fi
