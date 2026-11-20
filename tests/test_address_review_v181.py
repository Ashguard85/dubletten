"""Version 1.8.1: safe review navigation and split export of house number."""
import csv
import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from engine import connect
from swisstopo import import_snapshot
from address_service import (ensure_schema, import_job, page_rows, navigate_review,
                             save_review, job_counts, display_street_without_number,
                             export_job)


def job(tmp_path, size=36):
    db=connect(tmp_path/'v181.sqlite')
    sw=tmp_path/'swiss.csv'
    sw.write_text('ADR_EGAID;STN_LABEL;ADR_NUMBER;ZIP_LABEL;ADR_STATUS;ADR_OFFICIAL\n'
                  'E1;Bahnhofstrasse;12;6003 Luzern;1;1\n'
                  'E2;Hauptstrasse;14;6048 Horw;1;1\n',encoding='utf8')
    import_snapshot(db,sw,'2026-11-20')
    ensure_schema(db)
    data=pd.DataFrame([{'ID':f'ID{i:03}', 'Adresse':'Bahnhofstrasse 12',
                        'PLZ':'6003','Ort':'Luzern'} for i in range(size)])
    ident=import_job(db,data,'Test v181','kunden.csv',
                     {'id':'ID','street':'Adresse','house_number':'',
                      'postcode':'PLZ','city':'Ort','country':''})
    return db,ident


def test_skip_is_distinct_and_preserves_source(tmp_path):
    db,jid=job(tmp_path,3)
    _,rows=page_rows(db,jid)
    first=rows[0]
    original=first['original_json']
    save_review(db,jid,first['row_pk'],'uebersprungen')
    after=db.execute('SELECT * FROM address_job_rows WHERE row_pk=?',(first['row_pk'],)).fetchone()
    assert after['decision']=='uebersprungen'
    assert after['correction_json']=='{}'
    assert after['original_json']==original
    assert page_rows(db,jid,decision='offen')[0]==2
    assert page_rows(db,jid,decision='uebersprungen')[0]==1
    save_review(db,jid,first['row_pk'],'offen')
    assert db.execute('SELECT decision FROM address_job_rows WHERE row_pk=?',(first['row_pk'],)).fetchone()[0]=='offen'


def test_next_goes_forward_across_pages_and_respects_filters(tmp_path):
    db,jid=job(tmp_path,64)
    _,rows=page_rows(db,jid,limit=100)
    chosen=rows[29]
    target=navigate_review(db,jid,chosen['row_no'],page_size=30)
    assert target == (rows[30]['row_pk'],2)
    assert navigate_review(db,jid,rows[30]['row_no'],direction='previous')==(rows[29]['row_pk'],1)
    save_review(db,jid,rows[30]['row_pk'],'uebersprungen')
    target=navigate_review(db,jid,chosen['row_no'],decision='offen')
    assert target[0] == rows[31]['row_pk']
    assert target[1] == 2
    assert navigate_review(db,jid,rows[-1]['row_no'],wrap=False) is None
    assert navigate_review(db,jid,rows[-1]['row_no'],wrap=True)[0] == rows[0]['row_pk']
    assert navigate_review(db,jid,rows[0]['row_no'],direction='previous') is None
    assert navigate_review(db,jid,rows[0]['row_no'],direction='previous',wrap=True)[0]==rows[-1]['row_pk']
    assert navigate_review(db,jid,0,needle="%' OR 1=1 --") is None


def test_export_separate_house_for_confirmed_and_unconfirmed(tmp_path):
    db,jid=job(tmp_path,4)
    _,rows=page_rows(db,jid)
    save_review(db,jid,rows[1]['row_pk'],'korrigiert',
        {'strasse':'Bahnhofstrasse','hausnummer':'12a','plz':'6003','ort':'Luzern'})
    save_review(db,jid,rows[2]['row_pk'],'uebersprungen')
    doc=export_job(db,jid,split_house=True)
    data=list(csv.DictReader(io.StringIO(doc.decode('utf-8-sig')),delimiter=';'))
    assert data[0]['Adresse']=='Bahnhofstrasse 12'
    assert data[0]['Ziel_Strasse']=='Bahnhofstrasse'
    assert data[0]['Ziel_Hausnummer']=='12'
    assert data[0]['Zieladresse_Basis']=='aus_original_getrennt_ungeprueft'
    assert data[1]['Ziel_Hausnummer']=='12a'
    assert data[1]['Zieladresse_Basis']=='bestaetigte_korrektur'
    assert data[2]['Pruefentscheid']=='uebersprungen'
    assert data[2]['Zieladresse_Basis']=='aus_original_getrennt_ungeprueft'
    renamed=list(csv.DictReader(io.StringIO(export_job(db,jid,split_house=True,
                 street_column='Strasse_Neu',house_column='Hausnummer_Neu').decode('utf-8-sig')),delimiter=';'))
    assert renamed[0]['Hausnummer_Neu']=='12'
    assert renamed[0]['Strasse_Neu']=='Bahnhofstrasse'
    for colliding in [('Adresse','Nr_neu'),('Ziel_Strasse','Ziel_Strasse'),('StrasseNeu','Pruefentscheid')]:
        with pytest.raises(ValueError):
            export_job(db,jid,split_house=True,street_column=colliding[0],house_column=colliding[1])
    assert 'Ziel_Hausnummer' not in export_job(db,jid).decode('utf-8-sig').splitlines()[0]


def test_display_street_preserves_official_casing_and_house_suffix():
    assert display_street_without_number({'street_source':'Matthof 24','house':'24','house_source':''})=='Matthof'
    assert display_street_without_number({'street_source':'Hauptstrasse 12a','house':'12a','house_source':''})=='Hauptstrasse'
    assert display_street_without_number({'street_source':'Rue du Lac 12–14','house':'12-14','house_source':''})=='Rue du Lac'
    assert display_street_without_number({'street_source':'Matthof 24','house':'24','house_source':'24'})=='Matthof'
    assert display_street_without_number({'street_source':'14 Luzernerstrasse','house':'14','house_source':''})=='Luzernerstrasse'
    assert display_street_without_number({'street_source':'Seestrasse','house':'','house_source':''})=='Seestrasse'
