"""Strict swisstopo bulk approval: only exact, unique, official, unchanged addresses."""
import csv
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from engine import connect
from swisstopo import import_snapshot
from address_service import (
    ensure_schema, import_job, page_rows, save_review,
    bulk_approval_preview, bulk_approve_exact, export_job,
)


def setup(tmp_path, extra_register=''):
    db = connect(tmp_path/'bulk.sqlite')
    path = tmp_path/'ch.csv'
    path.write_text(
       'ADR_EGAID;STN_LABEL;ADR_NUMBER;ZIP_LABEL;ADR_STATUS;ADR_OFFICIAL\n'
       'E1;Bahnhofstrasse;12;6003 Luzern;1;1\n'
       'E2;Hauptstrasse;14;6048 Horw;1;1\n'
       'E3;Matthof;24;6014 Luzern;1;1\n' + extra_register,
       encoding='utf-8')
    import_snapshot(db, path, '2026-11-20')
    ensure_schema(db)
    return db


def job(db, records):
    frame = pd.DataFrame(records).fillna('').astype(str)
    return import_job(db,frame,'Qualität','test.csv',
        {'id':'ID','street':'Adresse','house_number':'',
         'postcode':'PLZ','city':'Ort','country':'Land'})


def sample(id, address, postal, city, country='CH'):
    return {'ID':id,'Adresse':address,'PLZ':postal,'Ort':city,'Land':country}


def test_strict_exact_only_and_original_immutable(tmp_path):
    db = setup(tmp_path)
    jid = job(db, [
        sample('E','Bahnhofstrasse 12','6003','Luzern'),
        sample('C','BAHNHOFSTRASSE 12','6003','Luzern'),
        sample('ABBR','Bahnhofstr. 12','6003','Luzern'),
        sample('EMPTY','Bahnhofstrasse 12','6003',''),
        sample('WRONG','Hauptstrasse 14','9999','Horw'),
        sample('OFFICIAL','Matthof 24','6014','Luzern'),
        sample('FOREIGN','Hauptstrasse 14','6048','Horw','DE'),
    ])
    preview = bulk_approval_preview(db,jid)
    assert preview['count'] == 3
    assert {e['ID'] for e in preview['examples']} == {'E','C','OFFICIAL'}
    source_before = dict(db.execute("SELECT source_id,original_json FROM address_job_rows WHERE job_id=?",(jid,)).fetchall())
    assert bulk_approve_exact(db,jid,3,preview['digest'])==3
    after = {r['source_id']:r for r in db.execute('SELECT * FROM address_job_rows WHERE job_id=?',(jid,))}
    assert {k for k,r in after.items() if r['decision']=='massenfreigegeben'} == {'E','C','OFFICIAL'}
    assert after['ABBR']['decision']=='offen'
    assert after['EMPTY']['decision']=='offen'
    assert after['WRONG']['decision']=='offen'
    assert after['FOREIGN']['decision']=='offen'
    assert all(after[k]['original_json'] == v for k,v in source_before.items())
    assert all(after[k]['correction_json']=='{}' for k in ('E','C','OFFICIAL'))
    assert all(after[k]['correction_basis']=='swisstopo_exakt_massengeprueft' for k in ('E','C','OFFICIAL'))
    assert bulk_approval_preview(db,jid)['count']==0
    with pytest.raises(ValueError):
        bulk_approve_exact(db,jid,3,preview['digest'])

    csv_rows=list(csv.DictReader(io.StringIO(export_job(db,jid,split_house=True).decode('utf-8-sig')),delimiter=';'))
    matched=next(x for x in csv_rows if x['ID']=='E')
    assert matched['Adresse']=='Bahnhofstrasse 12'
    assert matched['Ziel_Hausnummer']=='12'
    assert matched['Pruefentscheid']=='massenfreigegeben'
    assert matched['Zieladresse_Basis']=='swisstopo_exakt_massengeprueft'
    only=list(csv.DictReader(io.StringIO(export_job(db,jid,only_bulk=True).decode('utf-8-sig')),delimiter=';'))
    assert len(only)==3
    with pytest.raises(ValueError):
        export_job(db,jid,only_changed=True,only_bulk=True)


def test_register_duplicates_and_nonofficial_block_bulk(tmp_path):
    db=setup(tmp_path,
       'E99;Bahnhofstrasse;12;6003 Luzern;1;1\n'
       'E98;Hauptstrasse;14;6048 Horw;2;1\n')
    jid=job(db,[sample('DUP','Bahnhofstrasse 12','6003','Luzern'),
                sample('MIX','Hauptstrasse 14','6048','Horw'),
                sample('OK','Matthof 24','6014','Luzern')])
    preview=bulk_approval_preview(db,jid)
    assert preview['count']==1 and preview['examples'][0]['ID']=='OK'
    assert bulk_approve_exact(db,jid,1,preview['digest'])==1


def test_existing_decision_and_stale_preview(tmp_path):
    db=setup(tmp_path)
    jid=job(db,[sample('1','Hauptstrasse 14','6048','Horw'),
                sample('2','Matthof 24','6014','Luzern')])
    assert bulk_approval_preview(db,jid)['count']==2
    first=page_rows(db,jid)[1][0]
    save_review(db,jid,first['row_pk'],'keine_aenderung')
    initial = bulk_approval_preview(db,jid)
    # The previously computed snapshot must no longer be usable.
    with pytest.raises(ValueError,match='verändert'):
        bulk_approve_exact(db,jid,2,initial['digest'])
    preview=bulk_approval_preview(db,jid)
    assert preview['count']==1
    assert bulk_approve_exact(db,jid,1,preview['digest'])==1
    assert db.execute('SELECT COUNT(*) FROM address_job_rows WHERE job_id=? AND decision=?',(jid,'keine_aenderung')).fetchone()[0]==1


def test_requires_labels_from_updated_swisstopo_import(tmp_path):
    db=setup(tmp_path)
    jid=job(db,[sample('1','Matthof 24','6014','Luzern')])
    assert bulk_approval_preview(db,jid)['count']==1
    db.execute("UPDATE swisstopo_addresses SET street_label='' WHERE egaid='E3'")
    db.commit()
    assert bulk_approval_preview(db,jid)['count']==0


def test_no_update_for_no_address_matches(tmp_path):
    db=setup(tmp_path)
    jid=job(db,[sample('1','Foo 99','5999','Bar')])
    assert bulk_approval_preview(db,jid)['count']==0
    with pytest.raises(ValueError,match='keine freigabefähige'):
        bulk_approve_exact(db,jid,0,'0'*64)
