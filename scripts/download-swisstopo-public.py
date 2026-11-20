#!/usr/bin/env python3
"""Optional one-off download of a *public* swisstopo CSV/ZIP asset.

Run on a workstation or the server BEFORE the air-gap / egress lockdown.
Never accepts CRM data and never reads CRM DB. Requires direct official HTTPS
CSV/ZIP asset URL copied from the official swisstopo download page.
"""
import argparse
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

ALLOWED_HOSTS = {'data.geo.admin.ch', 'ogd.swisstopo.admin.ch'}
MAX_BYTES = 1_500_000_000


def download(url, destination):
    out=Path(destination).expanduser().resolve()
    if out.suffix.lower() not in ('.zip','.csv'):
        raise ValueError('Zieldatei muss .zip oder .csv sein.')
    current=url
    with requests.Session() as client:
        client.trust_env=False
        for attempt in range(5):
            parsed=urlparse(current)
            if parsed.scheme!='https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username:
                raise ValueError('Nur offizielle HTTPS-Downloads von data.geo.admin.ch oder ogd.swisstopo.admin.ch sind erlaubt.')
            response=client.get(current,stream=True,timeout=(15,90),allow_redirects=False)
            if response.status_code in (301,302,303,307,308):
                current=urljoin(current,response.headers['Location'])
                response.close()
                continue
            response.raise_for_status()
            out.parent.mkdir(parents=True,exist_ok=True)
            tmp=out.with_name(out.name+'.partial')
            digest=hashlib.sha256()
            total=0
            try:
                with tmp.open('xb') as f:
                    for piece in response.iter_content(chunk_size=4*1024*1024):
                        if not piece:continue
                        total+=len(piece)
                        if total>MAX_BYTES:
                            raise ValueError('Download zu gross (>1,5 GB).')
                        digest.update(piece)
                        f.write(piece)
                os.replace(tmp,out)
            finally:
                response.close()
                tmp.unlink(missing_ok=True)
            return total,digest.hexdigest()
    raise ValueError('Zu viele Weiterleitungen beim Download.')


def main():
    parser=argparse.ArgumentParser(description='Öffentlichen swisstopo-Datensatz sicher herunterladen.')
    parser.add_argument('--url',required=True,help='Direkter CSV-/ZIP-Downloadlink von der offiziellen swisstopo-Seite')
    parser.add_argument('--output',required=True,help='Lokale Zieldatei .csv oder .zip')
    args=parser.parse_args()
    n,sha=download(args.url,args.output)
    print(f'OK: {n:,} öffentliche Bytes, SHA256 {sha}. Keine CRM-Daten übertragen.')


if __name__=='__main__': main()
