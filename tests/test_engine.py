import io
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from engine import (connect, import_project, run_matching, page_pairs, set_review,
                    export_pairs, export_unmatched, score_pair, to_record, norm_phone, norm_name)


def maps():
    return {'id':'CRM-ID','company':'Firma','street':'Strasse','postcode':'PLZ',
            'city':'Ort','email':'E-Mail','phone':'Telefon','uid':'UID'}


def samples():
    a=pd.DataFrame([
      ('00001','Müller Informatik AG','Bahnhofstrasse 12','6003','Luzern','info@mueller.ch','041 123 45 67','CHE-111.222.333'),
      ('00002','H. Meier Elektro GmbH','Hauptstr. 14','6048','Horw','','041 456 78 90',''),
      ('00003','Keller Müller AG','Hauptstrasse 12','6048','Horw','','','CHE-999.888.777'),
      ('00004','Lehmann AG','Dorfstrasse 7','3000','Bern','','',''),
    ],columns=['CRM-ID','Firma','Strasse','PLZ','Ort','E-Mail','Telefon','UID'])
    b=pd.DataFrame([
      ('B-01','Mueller Informatik','Bahnhofstr. 12','6003','Luzern','info@mueller.ch','+41 41 123 45 67','CHE111222333'),
      ('B-02','Heinrich Meier Elektro GmbH','Hauptstrasse 14','6048','Horw','','0041 41 456 78 90',''),
      ('B-03','Keller Müller AG','Hauptstrasse 12','6048','Horw','','','CHE-888.777.666'),
      ('B-04','Huber Gastronomie','Seeweg 3','6003','Luzern','','',''),
    ],columns=a.columns)
    return a,b


def test_normalization():
    assert norm_name('Müller Informatik AG')=='mueller informatik'
    assert norm_phone('041 123 45 67')==norm_phone('+41 41 123 45 67')


def test_matching_and_exports(tmp_path):
    db=connect(tmp_path/'test.db')
    a,b=samples()
    pid=import_project(db,'Test',a,maps(),b,maps())
    pairs,kept=run_matching(db,pid,minimum=40)
    assert pairs>=3
    assert kept>=2
    count, rows=page_pairs(db,pid,state='Alle',min_score=40,limit=200)
    by_id={(p['a_id'],p['b_id']):p for p in rows}
    assert ('00001','B-01') in by_id
    assert ('00002','B-02') in by_id
    mueller=by_id[('00001','B-01')]
    assert mueller['score']>=90
    keller=by_id.get(('00003','B-03'))
    assert keller is None or keller['score']<mueller['score']
    set_review(db,pid,mueller['a_pk'],mueller['b_pk'],'bestaetigt')
    assert '00001' in export_pairs(db,pid,True).decode('utf-8-sig')
    assert '00001' not in export_unmatched(db,pid,'A').decode('utf-8-sig')
    assert '00004' in export_unmatched(db,pid,'A').decode('utf-8-sig')
    db.close()


def test_conflict_prevents_two_approved_matches(tmp_path):
    db=connect(tmp_path/'test.db')
    a,b=samples()
    b.loc[1,'Firma']='Müller Informatik AG'
    b.loc[1,'PLZ']='6003'
    b.loc[1,'Strasse']='Bahnhofstrasse 12'
    pid=import_project(db,'Test',a,maps(),b,maps())
    run_matching(db,pid,minimum=30)
    _, rows=page_pairs(db,pid,state='Alle',min_score=30,limit=200)
    matches=[r for r in rows if r['a_id']=='00001']
    assert len(matches)>=2
    set_review(db,pid,matches[0]['a_pk'],matches[0]['b_pk'],'bestaetigt')
    with pytest.raises(ValueError,match='Konflikt'):
        set_review(db,pid,matches[1]['a_pk'],matches[1]['b_pk'],'bestaetigt')


def test_duplicated_ids_rejected(tmp_path):
    db=connect(tmp_path/'test.db')
    a,b=samples()
    a.loc[1,'CRM-ID']='00001'
    with pytest.raises(ValueError, match='doppelte'):
        import_project(db,'Test',a,maps(),b,maps())


def test_street_and_uid_warning():
    from engine import norm_street, to_record
    assert norm_street('Bahnhofstr. 12') == norm_street('Bahnhofstrasse 12')
    raw = {'company':'Berg AG','first_name':'', 'last_name':'', 'street':'Seestr. 9',
           'postcode':'CH-6048','city':'Horw','country':'CH','email':'',
           'phone':'','uid':'CHE-111.222.333','id':'1'}
    rb = {**raw,'uid':'CHE-999.888.777','postcode':'6048'}
    na,nb=to_record(raw),to_record(rb)
    assert na['postal']==nb['postal']=='6048'
    score,reason=score_pair(na,nb)
    assert 'UID verschieden' in reason
    assert score < 80


def test_different_contacts_do_not_have_identical_name():
    from engine import to_record
    raw={'company':'Test AG','first_name':'Peter', 'last_name':'Muster', 'street':'Seestr. 9',
          'postcode':'6048','city':'Horw','country':'CH','email':'',
          'phone':'','uid':'','id':'1'}
    a=to_record(raw)
    b=to_record({**raw,'first_name':'Ursula','last_name':'Huber'})
    assert a['name'] != b['name']
