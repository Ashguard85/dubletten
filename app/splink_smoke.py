"""Realer Splink-5-Rauchtest IM gebauten App-Container.

Run: docker exec <crm-app-container> python /opt/app/splink_smoke.py
Only synthetic test data. Writes to an automatically cleaned temporary database.
"""
from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from engine import connect, import_project, page_pairs, project_stats
from splink_matcher import run_splink_matching


def main():
    assert version('splink').startswith('5.'), 'Splink 5 fehlt'
    cols = ['CRM-ID', 'Firma', 'Strasse', 'PLZ', 'Ort', 'E-Mail', 'Telefon', 'UID']
    data_a, data_b = [], []
    for n in range(12):
        a_id = f'A-{n:03d}'
        b_id = f'B-{n:03d}'
        a = (a_id, f'Testfirma {n} AG', f'Bahnhofstrasse {n+1}', f'{6000+n}',
             'Luzern', f'firma{n}@example.invalid', '', f'CHE-{n+1:09d}')
        b = (b_id, f'Testfirma {n}', f'Bahnhofstr. {n+1}', f'{6000+n}',
             'Luzern', f'firma{n}@example.invalid', '', f'CHE-{n+1:09d}')
        data_a.append(a);data_b.append(b)
    mapping = {'id': 'CRM-ID', 'company': 'Firma', 'street': 'Strasse',
               'postcode': 'PLZ', 'city': 'Ort', 'email': 'E-Mail',
               'phone': 'Telefon', 'uid': 'UID'}
    with TemporaryDirectory(prefix='crm-splink5-smoke-') as tmp:
        db = connect(Path(tmp) / 'crm.sqlite')
        try:
            pid = import_project(db, 'Splink-Rauchtest', pd.DataFrame(data_a, columns=cols),
                                 mapping, pd.DataFrame(data_b, columns=cols), mapping,
                                 engine='splink5')
            compared, kept = run_splink_matching(db, pid, minimum_probability=0.01,
                                                 progress=lambda s: print(s, flush=True))
            model = json.loads(project_stats(db,pid)['match_model_info'])
            count, pairs = page_pairs(db,pid,state='Alle', min_score=0,limit=50)
            if count < 8:
                raise RuntimeError(f'Nur {count} Testtreffer gefunden, erwartet mindestens 8.')
            assert model['engine']=='splink5'
            assert any(pair['match_probability'] is not None for pair in pairs)
            print(f'OK: Splink {version("splink")} · Kandidatenobergrenze {compared} · '
                  f'{kept} gespeicherte Paare · Modus {model["mode"]}')
        finally:
            db.close()


if __name__ == '__main__':
    main()
