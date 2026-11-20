"""Deterministic v1.7 checks; never use real CRM or online services."""
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from engine import connect, import_project, run_matching, set_review
from migration import (run_reference_check, reference_overview, reference_page,
                       export_references, export_reference_field_differences,
                       export_workflow1_inventory, reference_conflict)
from swisstopo import (import_snapshot, active_snapshot, validate_project,
                       validation_counts, export_validation)


def dbfixture(tmp_path):
    d=connect(tmp_path/'crm.sqlite')
    a=pd.DataFrame([
        {'id':'A01','bref':'B001','company':'Müller Informatik AG','street':'Bahnhofstrasse 12','postcode':'6003','city':'Luzern','skip':'Ja'},
        {'id':'A02','bref':'','company':'Heinrich Meier Elektro GmbH','street':'Hauptstrasse 14','postcode':'6048','city':'Horw','skip':'Ja'},
        {'id':'A03','bref':'B404','company':'Unbekannt AG','street':'Seestrasse 12a','postcode':'6006','city':'Luzern','skip':'Ja'},
        {'id':'A04','bref':'','company':'Nicht Migration','street':'Am Hang 3','postcode':'8000','city':'Zürich','skip':'Nein'},
        {'id':'A05','bref':'B001','company':'Andere Firma','street':'Hauptstrasse 99','postcode':'6048','city':'Horw','skip':'Ja'},
        {'id':'A06','bref':'','company':'Ohne Kandidaten','street':'Unbekanntweg 99','postcode':'7000','city':'Chur','skip':'Ja'},
    ])
    b=pd.DataFrame([
        {'id':'B001','company':'Mueller Informatik AG','street':'Bahnhofstr. 12','postcode':'6003','city':'Luzern'},
        {'id':'B002','company':'Heinrich Meier Elektro','street':'Hauptstrasse 14','postcode':'6048','city':'Horw'},
    ])
    mapping={'id':'id','company':'company','street':'street','postcode':'postcode','city':'city'}
    pid=import_project(d,'Test',a,mapping,b,mapping,engine='rapidfuzz',mode='dual',
                       a_ref_column='bref',a_exclude_column='skip',exclude_values=['Nein'])
    return d,pid


def test_import_compatibility_v16_and_dual(tmp_path):
    db,pid=dbfixture(tmp_path)
    assert db.execute('SELECT mode FROM projects WHERE project_id=?',(pid,)).fetchone()['mode']=='dual'
    assert db.execute("SELECT count(*) FROM records WHERE project_id=? AND b_reference='B001'",(pid,)).fetchone()[0]==2
    assert db.execute("SELECT in_scope FROM records WHERE source_id='A04' AND project_id=?",(pid,)).fetchone()[0]==0
    # Opening a pre-v1.7 database twice preserves records and is idempotent.
    assert connect(tmp_path/'crm.sqlite').execute('SELECT count(*) FROM records').fetchone()[0]==8


def test_reference_workflow(tmp_path):
    db,pid=dbfixture(tmp_path)
    assert run_reference_check(db,pid)==3
    results={x['a_id']:x['result'] for x in db.execute('''SELECT a.source_id a_id,r.result FROM reference_checks r
                 JOIN records a ON a.pk=r.a_pk WHERE r.project_id=?''',(pid,))}
    assert results=={'A01':'mehrfachreferenz','A03':'referenz_fehlt','A05':'mehrfachreferenz'}
    csvtext=export_references(db,pid).decode('utf-8-sig')
    assert 'A03' in csvtext and 'referenz_fehlt' in csvtext
    assert 'A01' in export_reference_field_differences(db,pid).decode('utf-8-sig')


def test_workflow_one_only_unref_and_no_automatic_migration(tmp_path):
    db,pid=dbfixture(tmp_path)
    pairs, saved=run_matching(db,pid,minimum=45)
    assert pairs>=1
    ids={x['id'] for x in db.execute('''SELECT a.source_id id FROM pairs p JOIN records a ON a.pk=p.a_pk
                                 WHERE p.project_id=?''',(pid,))}
    assert ids=={'A02'}
    inv={r['crm_a_id']:r for r in __import__('migration').workflow1_inventory(db,pid)}
    assert inv['A04']['status']=='ausgeschlossen'
    assert inv['A01']['status']=='referenz_vorhanden'
    assert inv['A06']['status']=='kein_kandidat_nicht_freigegeben'
    assert 'kein_kandidat_nicht_freigegeben' in export_workflow1_inventory(db,pid).decode('utf-8-sig')


def test_confirmation_blocking_if_b_is_already_referenced(tmp_path):
    db,pid=dbfixture(tmp_path)
    a_pk=db.execute("SELECT pk FROM records WHERE source_id='A02' AND project_id=?",(pid,)).fetchone()[0]
    b_pk=db.execute("SELECT pk FROM records WHERE source_id='B001' AND project_id=?",(pid,)).fetchone()[0]
    assert reference_conflict(db,pid,a_pk,b_pk)
    with db:
        db.execute('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at) VALUES(?,?,?,?,?,?)',
                   (pid,a_pk,b_pk,80,'Test','2026-11-20'))
    with pytest.raises(ValueError,match='bereits als Referenz'):
        set_review(db,pid,a_pk,b_pk,'bestaetigt')


def test_swisstopo_statuses_provenance_and_non_destructive(tmp_path):
    db,pid=dbfixture(tmp_path)
    source=tmp_path/'official-sample.csv'
    source.write_text('STN_LABEL;ADR_NUMBER;ZIP_ZIP4;ZIP_NAME;ADR_STATUS;ADR_OFFICIAL;ADR_EGAID\n'
          'Bahnhofstrasse;12;6003;Luzern;1;1;EG-1\n'
          'Hauptstrasse;14;6048;Horw;1;1;EG-2\n'
          'Seestrasse;12a;6006;Luzern;2;1;EG-3\n',encoding='utf-8')
    sid,n=import_snapshot(db,source,'2026-11-18')
    assert (sid,n)==(1,3)
    assert active_snapshot(db)['reference_date']=='2026-11-18'
    assert validate_project(db,pid)==8
    res={r['id']:r['result'] for r in db.execute('''SELECT r.source_id id,v.result
         FROM swisstopo_results v JOIN records r ON r.pk=v.record_pk WHERE r.side='A' ''')}
    assert res['A01']=='amtlich_aktuell'
    assert res['A03']=='aufgehoben'
    assert res['A06']=='nicht_gefunden'
    assert 'EG-1' in export_validation(db,pid).decode('utf-8-sig')
    # Another snapshot is activated, but prior results stay linked to old version.
    sid2,n2=import_snapshot(db,source,'2026-11-19')
    assert active_snapshot(db)['snapshot_id']==sid2
    assert {r['snapshot_id'] for r in db.execute('SELECT snapshot_id FROM swisstopo_results')}=={sid}


def test_snapshot_failed_import_cannot_replace_old(tmp_path):
    db,pid=dbfixture(tmp_path)
    good=tmp_path/'a.csv';good.write_text('STN_LABEL;ADR_NUMBER;ZIP_ZIP4;ZIP_NAME\nHauptstrasse;1;6048;Horw\n')
    import_snapshot(db,good,'2026-11-18')
    bad=tmp_path/'invalid.csv';bad.write_text('foo;bar\nx;y\n')
    with pytest.raises(ValueError):
        import_snapshot(db,bad,'2026-11-19')
    assert active_snapshot(db)['reference_date']=='2026-11-18'
    assert db.execute('SELECT count(*) FROM swisstopo_snapshots').fetchone()[0]==1


def test_reference_normalisation_without_lying_about_exact_match(tmp_path):
    db,pid=dbfixture(tmp_path)
    db.execute("UPDATE records SET b_reference='B001' WHERE source_id='A01'")
    from migration import differences_between
    a=db.execute("SELECT * FROM records WHERE source_id='A01'").fetchone()
    b=db.execute("SELECT * FROM records WHERE source_id='B001'").fetchone()
    diff=differences_between(a,b)
    street=next(x for x in diff if x['feld']=='street_name')
    assert street['status']=='normalisiert_gleich'
    assert any(x['status'] in ('normalisiert_gleich','abweichend') for x in diff)


def test_swisstopo_zip_and_secondary_street_names(tmp_path):
    import zipfile
    db,pid=dbfixture(tmp_path)
    archive=tmp_path/'swisstopo.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('CH.csv',
          'STN_LABEL,ADR_NUMBER,ZIP_ZIP4,ZIP_NAME,ADR_STATUS,ADR_OFFICIAL,ADR_EGAID\n'
          'Bahnhofstrasse / Via Stazione,12,6003,Luzern,real,true,ABC\n')
    sid,n=import_snapshot(db,archive,'2026-11-18')
    assert n==2
    assert db.execute('SELECT count(*) FROM swisstopo_addresses WHERE snapshot_id=?',(sid,)).fetchone()[0]==2
    n=validate_project(db,pid)
    assert n==8
    result=db.execute('''SELECT s.result FROM swisstopo_results s JOIN records r ON r.pk=s.record_pk
                  WHERE r.project_id=? AND r.source_id='A01' AND r.side='A' ''',(pid,)).fetchone()[0]
    assert result=='amtlich_aktuell'
