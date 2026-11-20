#!/usr/bin/env bash
# Consistent SQLite backup plus versioned trained Splink model JSON files.
# Run on Docker HOST: bash scripts/backup.sh crm-dubletten
set -euo pipefail
PROJECT="${1:-crm-dubletten}"
COUNT="${BACKUP_KEEP:-30}"
CID="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
  --filter 'label=com.docker.compose.service=app' --format '{{.ID}}' | head -n1)"
[[ -n "$CID" ]] || { echo "Kein app-Container für Projekt $PROJECT gefunden." >&2; exit 1; }
docker exec -i "$CID" python - <<'PY'
import datetime, pathlib, sqlite3, tarfile, tempfile
source=pathlib.Path('/app/data/crm.sqlite')
model_dir=pathlib.Path('/app/data/models')
backup=pathlib.Path('/app/backups')
backup.mkdir(parents=True,exist_ok=True)
name='crm-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.tar.gz'
with tempfile.TemporaryDirectory() as directory:
    target=pathlib.Path(directory)/'crm.sqlite'
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dest:
        src.backup(dest)
    file=backup/name
    with tarfile.open(file,'w:gz') as archive:
        archive.add(target,arcname='crm.sqlite')
        if model_dir.is_dir():
            for model in sorted(model_dir.glob('*.json')):
                if model.is_file() and not model.is_symlink():
                    archive.add(model,arcname='models/'+model.name)
    file.chmod(0o600)
    print('Lokales Backup:',file)
PY
DIR="/home/buebi/docker/crm-dubletten/backups"
if [[ "$COUNT" =~ ^[0-9]+$ ]] && (( COUNT >= 1 )) && [[ -d "$DIR" ]]; then
  find "$DIR" -maxdepth 1 -type f -name 'crm-*.tar.gz' -printf '%T@ %p\n' \
    | sort -nr | tail -n +$((COUNT + 1)) | cut -d' ' -f2- \
    | while IFS= read -r old; do [[ -n "$old" ]] && rm -- "$old"; done
fi
