"""Regression tests for the official Swiss building address CSV download format.
No internet, no personal data, zip is streamed without extraction.
"""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from swisstopo import import_snapshot, inspect_source, validate_project, active_snapshot
from test_migration_v17 import dbfixture

COLUMNS='ADR_EGAID;STR_ESID;BDG_EGID;ADR_EDID;STN_LABEL;ADR_NUMBER;BDG_CATEGORY;BDG_NAME;ZIP_LABEL;COM_FOSNR;COM_NAME;COM_CANTON;ADR_STATUS;ADR_OFFICIAL;ADR_MODIFIED;ADR_EASTING;ADR_NORTHING\n'
def row(egaid,street,house,zip_label,status='1',official='1'):
    return f'{egaid};1;2;3;{street};{house};2;;{zip_label};1061;Luzern;LU;{status};{official};20261119;2600000;1200000\n'


def test_real_official_schema_imports_zip_label_and_multi_postcode(tmp_path):
    db,pid=dbfixture(tmp_path)
    csv=tmp_path/'amtliches-gebaeudeadressverzeichnis_ch_2056.csv'
    csv.write_text(COLUMNS+
      row('EGAID-1','Bahnhofstrasse','12','6003 Luzern, 6004 Luzern')+
      row('EGAID-2','Hauptstrasse','14','6048 Horw')+
      row('EGAID-3','Seestrasse','12a','6006 Luzern','2')+'\n', encoding='utf-8')
    z=tmp_path/'amtliches-gebaeudeadressverzeichnis_ch_2056.zip'
    with zipfile.ZipFile(z,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('beilage.csv','Foo;Bar\n1;2\n')
        archive.write(csv,arcname='amtliches-gebaeudeadressverzeichnis_ch_2056.csv')
    assert inspect_source(z)[0]['format']=='ZIP_LABEL'
    sid,count=import_snapshot(db,z,'2026-11-20')
    assert sid==1
    assert count==4  # Two postal towns for the first address
    assert active_snapshot(db)['source_name']==z.name
    assert validate_project(db,pid)==8
    r={x['source_id']:x['result'] for x in db.execute('''SELECT r.source_id, s.result FROM swisstopo_results s JOIN records r ON r.pk=s.record_pk WHERE r.side='A' ''')}
    assert r['A01']=='amtlich_aktuell'
    assert r['A02']=='amtlich_aktuell'
    assert r['A03']=='aufgehoben'
    assert db.execute("SELECT city FROM swisstopo_addresses WHERE postal='6004'").fetchone()['city']=='luzern'


def test_bad_format_does_not_replace_snapshot(tmp_path):
    db,pid=dbfixture(tmp_path)
    good=tmp_path/'good.csv'
    good.write_text(COLUMNS+row('EG-1','Hauptstrasse','14','6048 Horw'))
    import_snapshot(db,good,'2026-11-20')
    bad=tmp_path/'bad.csv'
    bad.write_text('SOMETHING;OTHER\n1;2\n')
    with pytest.raises(ValueError, match='STN_LABEL'):
        import_snapshot(db,bad,'2026-11-21')
    assert active_snapshot(db)['reference_date']=='2026-11-20'
    assert db.execute('SELECT count(*) n FROM swisstopo_snapshots').fetchone()['n']==1


def test_large_csv_is_lazy(tmp_path):
    db,pid=dbfixture(tmp_path)
    f=tmp_path/'big.csv'
    with f.open('w') as w:
        w.write(COLUMNS)
        for i in range(120_000):
            w.write(row(i,'Testweg',str(i%4000),f'60{i%10}3 Luzern'))
    sid,count=import_snapshot(db,f,'2026-11-20')
    assert count == 120_000
