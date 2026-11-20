"""Standalone swiss address service: offline, reviewable and non-destructive."""
import csv
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from engine import connect
from swisstopo import import_snapshot, active_snapshot
from address_service import (ensure_schema, import_job, page_rows, job_counts,
    save_review, export_job, list_jobs, delete_job, read_single_upload,
    guess_column, validate_address)


def fixture(tmp_path):
    db=connect(tmp_path/'crm.sqlite')
    sw=tmp_path/'swisstopo.csv'
    sw.write_text('ADR_EGAID;STN_LABEL;ADR_NUMBER;ZIP_LABEL;ADR_STATUS;ADR_OFFICIAL\n'
        'E1;Bahnhofstrasse;12;6003 Luzern;1;1\n'
        'E2;Hauptstrasse;14;6048 Horw;1;1\n'
        'E3;Hauptstrasse;16;6048 Horw;1;1\n'
        'E4;Seestrasse;12a;6006 Luzern;2;1\n',encoding='utf-8')
    sid,count=import_snapshot(db,sw,'2026-11-20')
    ensure_schema(db)
    assert count==4
    return db,sid


def frame():
    return pd.DataFrame([
       {'Nr':'0001','Adresse':'Bahnhofstr. 12','Postleitzahl':'6003','Ortschaft':'Luzern','Firma':'A GmbH'},
       {'Nr':'0002','Adresse':'Hauptstrasse 14','Postleitzahl':'9999','Ortschaft':'Horw','Firma':'B AG'},
       {'Nr':'0003','Adresse':'Hauptstrasse 99','Postleitzahl':'6048','Ortschaft':'Horw','Firma':'C'},
       {'Nr':'0004','Adresse':'Hauptstrsse 14','Postleitzahl':'6048','Ortschaft':'Horw','Firma':'D'},
       {'Nr':'0005','Adresse':'Seestrasse 12a','Postleitzahl':'6006','Ortschaft':'Luzern','Firma':'E'},
       {'Nr':'0006','Adresse':'Unbekanntweg 4','Postleitzahl':'8000','Ortschaft':'Zürich','Firma':'F'},
       {'Nr':'0007','Adresse':'Bahnhofstrasse 12','Postleitzahl':'6003','Ortschaft':'Luzern','Firma':'=2+2'},
       {'Nr':'0008','Adresse':'Bahnhofstrasse 12','Postleitzahl':'6003','Ortschaft':'Luzern','Firma':'X','Land':'DE'},
    ]).fillna('').astype(str)


def mapping():
    return {'id':'Nr','street':'Adresse','house_number':'','postcode':'Postleitzahl',
            'city':'Ortschaft','country':'Land'}


def test_import_standalone_does_not_make_crm_project(tmp_path):
    db,sid=fixture(tmp_path)
    jid=import_job(db,frame(),'Einzeltest','kunden.csv',mapping())
    assert jid==1
    assert db.execute('SELECT COUNT(*) FROM projects').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM address_job_rows').fetchone()[0]==8
    assert list_jobs(db)[0]['snapshot_id']==sid
    assert sum(x['n'] for x in job_counts(db,jid))==8
    _,rows=page_rows(db,jid,limit=30)
    by_id={r['source_id']:r for r in rows}
    assert by_id['0001']['result']=='amtlich_aktuell'
    assert by_id['0002']['result']=='plz_abweichend'
    assert by_id['0003']['result']=='hausnummer_unbekannt'
    assert by_id['0004']['result']=='strasse_aehnlich'
    assert by_id['0005']['result']=='aufgehoben'
    assert by_id['0006']['result']=='nicht_gefunden'
    assert by_id['0008']['result']=='ausland'
    assert json.loads(by_id['0001']['proposals_json'])[0]['strasse']=='Bahnhofstrasse'
    assert json.loads(by_id['0002']['proposals_json'])[0]['plz']=='6048'
    assert not json.loads(by_id['0003']['proposals_json'])  # never guess a house number


def test_explicit_review_and_originals_protected(tmp_path):
    db,sid=fixture(tmp_path)
    jid=import_job(db,frame(),'Kunden','input.csv',mapping())
    _,rows=page_rows(db,jid,limit=30)
    wrong=next(r for r in rows if r['source_id']=='0002')
    save_review(db,jid,wrong['row_pk'],'korrigiert',
                {'strasse':'Hauptstrasse','hausnummer':'14','plz':'6048','ort':'Horw'})
    good=next(r for r in rows if r['source_id']=='0001')
    save_review(db,jid,good['row_pk'],'keine_aenderung')
    out=list(csv.DictReader(io.StringIO(export_job(db,jid).decode('utf-8-sig')),delimiter=';'))
    assert len(out)==8
    corrected=next(r for r in out if r['Nr']=='0002')
    assert corrected['Postleitzahl']=='9999'   # untouched input
    assert corrected['Korrigiert_PLZ']=='6048'
    assert corrected['Pruefentscheid']=='korrigiert'
    assert corrected['Korrekturquelle']=='swisstopo_vorschlag_bestaetigt'
    assert list(csv.DictReader(io.StringIO(export_job(db,jid,True).decode('utf-8-sig')),delimiter=';'))[0]['Nr']=='0002'
    formula=next(r for r in out if r['Nr']=='0007')
    assert formula['Firma']=="'=2+2"  # CSV Excel formula protection
    orig=json.loads(db.execute('SELECT original_json FROM address_job_rows WHERE row_pk=?',(wrong['row_pk'],)).fetchone()[0])
    assert orig['Postleitzahl']=='9999'


def test_manual_correction_is_not_claimed_official(tmp_path):
    db,sid=fixture(tmp_path)
    jid=import_job(db,frame(),'Test','x.csv',mapping())
    _,rows=page_rows(db,jid,limit=30)
    row=next(r for r in rows if r['source_id']=='0006')
    save_review(db,jid,row['row_pk'],'korrigiert',
           {'strasse':'Unbekanntweg','hausnummer':'4','plz':'8000','ort':'Zürich'})
    r=db.execute('SELECT correction_basis FROM address_job_rows WHERE row_pk=?',(row['row_pk'],)).fetchone()[0]
    assert r=='manuell_nicht_amtlich_verifiziert'
    save_review(db,jid,row['row_pk'],'offen')
    assert db.execute('SELECT correction_json FROM address_job_rows WHERE row_pk=?',(row['row_pk'],)).fetchone()[0]=='{}'
    with pytest.raises(ValueError):
        save_review(db,jid,row['row_pk'],'korrigiert',{'strasse':'X','hausnummer':'','plz':'1234','ort':'Luzern'})


def test_wrong_country_and_filter_and_pinned_snapshot(tmp_path):
    db,sid=fixture(tmp_path)
    jid=import_job(db,frame(),'Test','x.csv',mapping())
    total,rows=page_rows(db,jid,'plz_abweichend')
    assert total==1 and rows[0]['source_id']=='0002'
    count,_=page_rows(db,jid,needle='0001')
    assert count==1
    count,_=page_rows(db,jid,needle="%' OR 1=1 --")
    assert count==0
    other=tmp_path/'other.csv'
    other.write_text('STN_LABEL;ADR_NUMBER;ZIP_LABEL\nHauptstrasse;100;6048 Horw\n')
    import_snapshot(db,other,'2026-11-21')
    assert list_jobs(db)[0]['snapshot_id']==sid  # old job is frozen to old data
    delete_job(db,jid)
    assert not list_jobs(db)
    assert db.execute('SELECT count(*) FROM swisstopo_snapshots').fetchone()[0]==2


def test_source_upload_mapping_and_guards():
    class File:
        name='test.csv';size=80
        def getvalue(self): return b'id;strasse;plz\n0001;Bahnhofstrasse 12;6003\n'
    f=File()
    df=read_single_upload(f)
    assert df.iloc[0]['id']=='0001'
    assert guess_column(df.columns,'street')=='strasse'
    assert guess_column(df.columns,'postcode')=='plz'
    f.size=61*1024**2
    with pytest.raises(ValueError,match='60 MB'):
        read_single_upload(f)


def test_missing_snapshot_and_bad_mapping(tmp_path):
    db=connect(tmp_path/'empty.sqlite')
    ensure_schema(db)
    with pytest.raises(ValueError,match='swisstopo'):
        import_job(db,pd.DataFrame([{'strasse':'A 4','plz':'1234'}]),'A','a.csv',{'street':'strasse','postcode':'plz'})
