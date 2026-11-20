"""CLI import of an already-downloaded swisstopo CSV/ZIP, no network access."""
import argparse
import os
from pathlib import Path

from engine import connect
from swisstopo import import_snapshot


def main():
    parser=argparse.ArgumentParser(description='Lade einen öffentlichen swisstopo-Datenstand offline in CRM Dubletten.')
    parser.add_argument('--file',required=True,help='Pfad im Container zur CSV- oder ZIP-Datei')
    parser.add_argument('--date',required=True,help='Offizieller Datenstand YYYY-MM-DD')
    args=parser.parse_args()
    data=Path('/app/data/swisstopo').resolve()
    target=Path(args.file).resolve()
    if not target.is_relative_to(data):
        parser.error('Die Datei muss unter /app/data/swisstopo/ liegen.')
    db=connect(os.environ.get('DB_PATH','/app/data/crm.sqlite'))
    sid,count=import_snapshot(db,target,args.date,progress=print)
    print(f'OK: swisstopo snapshot_id={sid} rows={count} (öffentliche Daten, keine CRM-Übertragung)')


if __name__=='__main__':
    main()
