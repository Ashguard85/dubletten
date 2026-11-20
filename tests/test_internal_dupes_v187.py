"""True single-file dedupe: no self-pairs, no re-import, auditable groups and exports."""
import csv
import io
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from engine import connect,run_matching,set_review,import_project
from internal_dupes import (create_internal_project,is_internal,group_overview,
    export_internal_pairs,export_internal_groups,export_internal_inventory)
from review_data import pair_page

MAP={'id':'ID','company':'Firma','first_name':'','last_name':'',
     'street':'Strasse','house_number':'Hausnummer',
     'postcode':'PLZ','city':'Ort','country':'','email':'E-Mail','phone':'','uid':''}


def rows_fixture():
    return pd.DataFrame([
       {'ID':'001','Firma':'Müller Elektro AG','Strasse':'Bahnhofstrasse 12','Hausnummer':'','PLZ':'8001','Ort':'Zürich','E-Mail':'kontakt@mueller.test','Freitext':'=2+2'},
       {'ID':'002','Firma':'Mueller Elektro AG','Strasse':'Bahnhofstr. 12','Hausnummer':'','PLZ':'8001','Ort':'Zuerich','E-Mail':'kontakt@mueller.test','Freitext':'Original'},
       {'ID':'003','Firma':'Müller Elektro AG','Strasse':'Bahnhofstrasse 12','Hausnummer':'','PLZ':'8001','Ort':'Zürich','E-Mail':'','Freitext':'Original'},
       {'ID':'004','Firma':'Hodel Gartenbau GmbH','Strasse':'Matthof 24','Hausnummer':'','PLZ':'6014','Ort':'Luzern','E-Mail':'info@hodel.test','Freitext':'Kein Duplikat'},
       {'ID':'005','Firma':'Hodel Gartenbau','Strasse':'Matthof 24','Hausnummer':'','PLZ':'6014','Ort':'Luzern','E-Mail':'','Freitext':'Prüfen'},
       {'ID':'006','Firma':'Hodel Gartenbau GmbH','Strasse':'Matthof 59','Hausnummer':'','PLZ':'6014','Ort':'Luzern','E-Mail':'','Freitext':'Falsche Hausnummer'},
       {'ID':'007','Firma':'Stadtbibliothek','Strasse':'Rathausplatz 1','Hausnummer':'','PLZ':'6004','Ort':'Luzern','E-Mail':'info@bibliothek.test','Freitext':'Unabhängig'},
       {'ID':'008','Firma':'Alpen Treuhand AG','Strasse':'Bergstrasse 14','Hausnummer':'','PLZ':'9000','Ort':'St. Gallen','E-Mail':'office@treuhand.test','Freitext':'Unabhängig'}
    ])


@pytest.fixture
def setup(tmp_path):
    db=connect(tmp_path/'intern.sqlite')
    pid=create_internal_project(db,'Intern',rows_fixture(),MAP)
    yield db,pid
    db.close()


def test_one_import_eight_originals_and_unique_unordered_pairs(setup):
    db,pid=setup
    assert is_internal(db,pid)
    assert db.execute("SELECT count(*) FROM records WHERE project_id=? AND side='A'",(pid,)).fetchone()[0]==8
    assert db.execute("SELECT count(*) FROM records WHERE project_id=? AND side='B'",(pid,)).fetchone()[0]==0
    upper,count=run_matching(db,pid,minimum=55)
    assert upper>=3 and count>=3
    pairs=db.execute('''SELECT a.source_id id1,b.source_id id2,a.pk pk1,b.pk pk2
        FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
        WHERE p.project_id=?''',(pid,)).fetchall()
    assert len({(r['pk1'],r['pk2']) for r in pairs})==count
    assert all(r['pk1']<r['pk2'] and r['id1']!=r['id2'] for r in pairs)
    assert ('001','002') in {(r['id1'],r['id2']) for r in pairs}
    assert ('001','003') in {(r['id1'],r['id2']) for r in pairs}
    assert db.execute('SELECT b_count FROM projects WHERE project_id=?',(pid,)).fetchone()[0]==0


def test_many_to_one_approval_and_group_exports(setup):
    db,pid=setup
    run_matching(db,pid,minimum=50)
    pk={r['source_id']:r['pk'] for r in db.execute('SELECT pk,source_id FROM records WHERE project_id=?',(pid,))}
    # A single original can be confirmed against two others, unlike CRM A↔B migration.
    set_review(db,pid,pk['001'],pk['002'],'bestaetigt')
    set_review(db,pid,pk['001'],pk['003'],'bestaetigt')
    groups=group_overview(db,pid)
    assert len(groups)==1
    assert groups[0]['Anzahl']==3
    assert set(groups[0]['Original_IDs'].split(' · '))=={'001','002','003'}
    data=export_internal_pairs(db,pid)
    pair_rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig')),delimiter=';'))
    assert len([r for r in pair_rows if r['Pruefentscheid']=='bestaetigt'])==2
    g=list(csv.DictReader(io.StringIO(export_internal_groups(db,pid).decode('utf-8-sig')),delimiter=';'))
    assert {r['Original_ID'] for r in g}=={'001','002','003'}
    assert all(r['Manuelle_Links']=='2' for r in g)
    export=list(csv.DictReader(io.StringIO(export_internal_inventory(db,pid).decode('utf-8-sig')),delimiter=';'))
    assert len(export)==8
    assert export[0]['ID']=='001' and export[0]['Freitext']=="'=2+2"
    assert [r['Dubletten_Gruppe'] for r in export[:3]] == ['G00001']*3
    assert export[-1]['ID']=='008' and export[-1]['Dubletten_Gruppe']==''
    assert db.execute('SELECT count(*) FROM records WHERE project_id=?',(pid,)).fetchone()[0]==8


def test_groups_require_explicit_approval(setup):
    db,pid=setup
    run_matching(db,pid,minimum=50)
    assert group_overview(db,pid)==[]
    p=db.execute('SELECT a_pk,b_pk FROM pairs WHERE project_id=? LIMIT 1',(pid,)).fetchone()
    set_review(db,pid,p['a_pk'],p['b_pk'],'unklar')
    assert group_overview(db,pid)==[]
    set_review(db,pid,p['a_pk'],p['b_pk'],'abgelehnt')
    assert group_overview(db,pid)==[]
    set_review(db,pid,p['a_pk'],p['b_pk'],'bestaetigt')
    assert len(group_overview(db,pid))==1


def test_no_silent_loss_of_review_decisions(setup):
    db,pid=setup
    run_matching(db,pid,minimum=45)
    p=db.execute('SELECT a_pk,b_pk FROM pairs WHERE project_id=? LIMIT 1',(pid,)).fetchone()
    set_review(db,pid,p['a_pk'],p['b_pk'],'unklar')
    with pytest.raises(ValueError,match='Prüfentscheide'):
        run_matching(db,pid,minimum=75)
    assert db.execute('SELECT state FROM pairs WHERE project_id=? AND a_pk=? AND b_pk=?',(pid,p['a_pk'],p['b_pk'])).fetchone()[0]=='unklar'


def test_duplicate_source_ids_rejected(tmp_path):
    db=connect(tmp_path/'intern.sqlite')
    frame=rows_fixture().copy()
    frame.loc[1,'ID']='001'
    with pytest.raises(ValueError,match='doppelte IDs'):
        create_internal_project(db,'Schlechte ID',frame,MAP)
    assert db.execute('SELECT count(*) FROM projects').fetchone()[0]==0
    db.close()


def test_old_crm_dual_mode_still_rejects_many_to_one(tmp_path):
    db=connect(tmp_path/'dual.sqlite')
    frame=rows_fixture().iloc[:3].copy()
    pid=import_project(db,'A to B',frame,MAP,frame.copy(),MAP,mode='legacy')
    run_matching(db,pid,minimum=50)
    # Existing CRM A↔B workflow continues to enforce one-to-one acceptance.
    p=db.execute('SELECT a_pk,b_pk FROM pairs WHERE project_id=? ORDER BY a_pk,b_pk LIMIT 1',(pid,)).fetchone()
    set_review(db,pid,p['a_pk'],p['b_pk'],'bestaetigt')
    other=db.execute('SELECT a_pk,b_pk FROM pairs WHERE project_id=? AND (a_pk=? OR b_pk=?) AND NOT (a_pk=? AND b_pk=?) LIMIT 1',(pid,p['a_pk'],p['b_pk'],p['a_pk'],p['b_pk'])).fetchone()
    assert other is not None
    with pytest.raises(ValueError,match='Konflikt'):
        set_review(db,pid,other['a_pk'],other['b_pk'],'bestaetigt')
    db.close()


def test_internal_focus_and_ui_navigation_sources():
    focus=(ROOT/'app/focus_ui.py').read_text()
    ui=(ROOT/'app/ui.py').read_text()
    assert "'Interne Dublettenprüfung'" in focus
    assert 'MODES[3]' in focus
    assert 'render_internal_create' in ui and 'render_internal_project' in ui
    assert '➕ Interne Dublettenprüfung' in ui


def test_explicit_rejected_pair_inside_confirmed_group_is_flagged(setup):
    db,pid=setup
    run_matching(db,pid,minimum=50)
    pk={r['source_id']:r['pk'] for r in db.execute('SELECT pk,source_id FROM records WHERE project_id=?',(pid,))}
    set_review(db,pid,pk['001'],pk['002'],'bestaetigt')
    set_review(db,pid,pk['001'],pk['003'],'bestaetigt')
    set_review(db,pid,pk['002'],pk['003'],'abgelehnt')
    group=group_overview(db,pid)[0]
    assert group['Widerspruechliche_Entscheide']==1
    assert 'Konflikt' in group['Hinweis']
    rows=list(csv.DictReader(io.StringIO(export_internal_groups(db,pid).decode('utf-8-sig')),delimiter=';'))
    assert len(rows)==3 and all(r['Widerspruchspaare']=='1' for r in rows)
    inventory=list(csv.DictReader(io.StringIO(export_internal_inventory(db,pid).decode('utf-8-sig')),delimiter=';'))
    assert inventory[0]['Gruppen_Widersprueche']=='1'
