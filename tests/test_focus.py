import csv
import io
import importlib
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
sys.path.insert(0,str(ROOT/'scripts'))

from focus_data import (
    init_reference_reviews, reference_focus_page, save_reference_review,
    export_reference_reviews, next_position,
)
from apply_focus_patch import patch_file


@pytest.fixture
def db():
    cx=sqlite3.connect(':memory:')
    cx.row_factory=sqlite3.Row
    cx.execute('PRAGMA foreign_keys=ON')
    cx.executescript('''CREATE TABLE projects(project_id INTEGER PRIMARY KEY);
      CREATE TABLE records(pk INTEGER PRIMARY KEY, source_id TEXT, original_json TEXT);
      CREATE TABLE reference_checks(project_id INTEGER, a_pk INTEGER,
              b_pk INTEGER,b_ref TEXT,result TEXT,differences_json TEXT);
      INSERT INTO projects VALUES(1);
      INSERT INTO records VALUES (1,'A-001','{}');
      INSERT INTO records VALUES (2,'B-001','{}');
      INSERT INTO records VALUES (3,'=A-003','{}');
      INSERT INTO reference_checks VALUES(1,1,2,'B-001','identisch','[]');
      INSERT INTO reference_checks VALUES(1,3,NULL,'B-x','referenz_fehlt','[]');''')
    yield cx
    cx.close()


def test_reference_review_persists_and_filters(db):
    init_reference_reviews(db)
    n,r=reference_focus_page(db,1)
    assert n==2 and r['a_id']=='A-001'
    save_reference_review(db,1,1,'geprueft','Kontrolliert')
    assert reference_focus_page(db,1)[0]==1
    n,r=reference_focus_page(db,1,decision='geprueft')
    assert n==1 and r['review_note']=='Kontrolliert'
    save_reference_review(db,1,3,'korrektur_noetig','B-ID fehlt')
    assert reference_focus_page(db,1,decision='offen')[0]==0
    content=list(csv.DictReader(io.StringIO(export_reference_reviews(db,1).decode('utf-8-sig')),delimiter=';'))
    assert len(content)==2
    assert content[0]['Pruefentscheid']=='geprueft'
    assert content[1]['A_ID']=="'=A-003"  # Spreadsheet formula safe.
    assert content[1]['Pruefentscheid']=='korrektur_noetig'


def test_parameterization_and_guards(db):
    init_reference_reviews(db)
    assert reference_focus_page(db,1,search="%' OR 1=1 --")[0]==0
    assert reference_focus_page(db,1,search='A-001')[0]==1
    assert reference_focus_page(db,1,result='referenz_fehlt')[0]==1
    assert reference_focus_page(db,1,offset=999)[1] is None
    with pytest.raises(ValueError):
        reference_focus_page(db,1,result='bad')
    with pytest.raises(ValueError):
        save_reference_review(db,1,1,'superfreigabe')
    with pytest.raises(ValueError):
        save_reference_review(db,1,1,'geprueft','x'*1001)
    with pytest.raises(ValueError):
        save_reference_review(db,2,1,'geprueft')


def test_next_in_filtered_queue():
    assert next_position(0,9,True)==1
    assert next_position(5,9,False)==5
    assert next_position(8,8,False)==8
    assert next_position(8,9,True)==9  # Finished, don't silently wrap.
    assert next_position(0,0,False)==0


def test_patch_script_is_idempotent_and_preserves_new_logic(tmp_path):
    ui=tmp_path/'ui.py'
    ui.write_text('''import streamlit as st
from ai_control import is_enabled
st.title('🔎 CRM-Dublettenprüfung')
st.caption('GUI v1.8.3')
st.info('Datenschutz')
with st.sidebar:
    choices = ['+ Neuer Abgleich', '🏔️ swisstopo-Datenstand'] + ['#1 · CRM']
    selected = st.selectbox('Projekt', choices, index=2)
    show_swisstopo = selected == '🏔️ swisstopo-Datenstand'
if show_swisstopo:
    st.subheader('swisstopo')
if selected == 'Schweizer Adressprüfung':
    render_address_service(db)
''',encoding='utf8')
    assert patch_file(ui)
    once=ui.read_text()
    assert not patch_file(ui)
    assert once==ui.read_text()
    assert once.count("choices.append('🎯 Fokusmodus')")==1
    assert once.count('from focus_ui import render_focus_workspace')==1
    assert "from ai_control import is_enabled" in once
    compile(once,str(ui),'exec')


def test_focus_layout_helpers_without_streamlit_installation(monkeypatch):
    fake = types.ModuleType('streamlit')
    fake.fragment=lambda fn:fn
    monkeypatch.setitem(sys.modules,'streamlit',fake)
    sys.modules.pop('focus_ui',None)
    module=importlib.import_module('focus_ui')
    assert len(module._plain_rows({'company':'Test AG','street':'Strasse 12','postcode':'1234','city':'Luzern'},
                                  {'company':'Other AG','street':'Strasse 14','postcode':'1234','city':'Luzern'},
                                  '12','14'))==4
    assert len(module._short('X'*200))==75
    assert len(module.MODES)==4
    assert module.MODES[3]=='Interne Dublettenprüfung'


def test_crm_queue_is_server_paginated(monkeypatch):
    fake = types.ModuleType('streamlit')
    fake.fragment=lambda fn:fn
    monkeypatch.setitem(sys.modules,'streamlit',fake)
    stub=types.ModuleType('review_data')
    pages=[]
    def pair_page(db,pid,state,min_score,search,risk,page,page_size):
        pages.append((page,page_size,state,min_score,search,risk))
        rows=[{'a_pk':i} for i in range((page-1)*25,min(25*page,35))]
        return 35,2,page,rows
    stub.pair_page=pair_page
    monkeypatch.setitem(sys.modules,'review_data',stub)
    sys.modules.pop('focus_ui',None)
    focus=importlib.import_module('focus_ui')
    flt=dict(status='offen',score=70,search='Mueller',risk='Alle')
    n,first=focus._crm_queue(None,7,flt,0)
    assert (n,first['a_pk'])==(35,0)
    n,last=focus._crm_queue(None,7,flt,34)
    assert (n,last['a_pk'])==(35,34)
    assert pages[0]==(1,25,'offen',70,'Mueller','Alle')
    assert pages[-1][0]==2


def test_export_does_not_interpret_user_entered_notes(db):
    save_reference_review(db,1,1,'unklar','=IMPORT("http://example.com")')
    records=list(csv.DictReader(io.StringIO(export_reference_reviews(db,1).decode('utf-8-sig')),delimiter=';'))
    assert records[0]['Notiz'].startswith("'=")
