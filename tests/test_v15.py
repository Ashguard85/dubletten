"""v1.5 regression tests. Splink API fakes are clearly marked; no live Docker claim."""
from pathlib import Path
import json
import sqlite3
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from engine import (connect, import_project, split_street_house, to_record,
                    comparison_diagnostics, run_matching, set_review)
from match_config import validate_config, model_signature
from splink_matcher import (_load, _cap_block_fanout, available_models, _resolve_model,
                            train_model_from_reviews)


def records():
    return pd.DataFrame([
       {'ID':f'{x:06d}', 'Company':f'Firma {x}', 'Street':f'Bahnhofstrasse {x+1}',
        'House':str(x+1), 'Zip':str(6000+x),'City':'Luzern', 'Email':f'a{x}@example.invalid'}
       for x in range(30)
    ])


def mapping():
    return {'id':'ID','company':'Company','street':'Street','house_number':'House',
            'postcode':'Zip','city':'City','email':'Email'}


@pytest.mark.parametrize('raw,expected',[
 ('Bahnhofstrasse 12',('bahnhofstrasse','12')),
 ('Bahnhofstr. 12a',('bahnhofstrasse','12a')),
 ('Bahnhofstrasse 12 A',('bahnhofstrasse','12a')),
 ('12a Bahnhofstrasse',('bahnhofstrasse','12a')),
 ('Seestrasse 12–14',('seestrasse','12-14')),
 ('Rue du 1er Août 12',('rue du 1er aout','12')),
 ('Route 66',('route 66','')),
 ('Hauptstrasse',('hauptstrasse','')),
 ('5th Avenue',('5th avenue','')),
])
def test_address_split(raw,expected):
    street,num,conflict = split_street_house(raw)
    assert (street,num)==expected
    assert not conflict


def test_explicit_house_number_overrides_and_flags_conflict():
    assert split_street_house('Bahnhofstrasse 12a','12b') == ('bahnhofstrasse','12b',True)
    assert split_street_house('Bahnhofstrasse','12a') == ('bahnhofstrasse','12a',False)
    raw={x:'' for x in ('id','company','first_name','last_name','street',
                         'postcode','city','country','email','phone','uid','house_number')}
    raw.update(company='Firma',street='Seeweg 12',house_number='12a')
    r=to_record(raw)
    assert r['house_number']=='12a' and r['address_conflict']==1


def test_new_schema_preserves_existing_records_and_source_ids(tmp_path):
    db=connect(tmp_path/'test.sqlite')
    a=records(); b=records().copy()
    pid=import_project(db,'Alt',a,mapping(),b,mapping())
    pka=db.execute('SELECT pk FROM records WHERE project_id=? LIMIT 1',(pid,)).fetchone()['pk']
    db.close()
    db=connect(tmp_path/'test.sqlite')
    row=db.execute('SELECT * FROM records WHERE pk=?',(pka,)).fetchone()
    assert row['street_name']=='bahnhofstrasse'
    assert row['house_number']=='1'
    assert row['source_id']=='000000'
    assert db.execute('SELECT COUNT(*) FROM trained_models').fetchone()[0]==0


def test_old_sqlite_schema_address_backfill(tmp_path):
    db=connect(tmp_path/'test.sqlite')
    pid=import_project(db,'Alt',records(),mapping(),records(),mapping())
    db.close()
    # In a genuine 1.4 DB the extra address columns are missing.
    db=sqlite3.connect(tmp_path/'test.sqlite')
    db.execute('ALTER TABLE records DROP COLUMN street_name')
    db.execute('ALTER TABLE records DROP COLUMN house_number')
    db.execute('ALTER TABLE records DROP COLUMN address_conflict')
    db.commit();db.close()
    db=connect(tmp_path/'test.sqlite')
    assert db.execute('SELECT count(*) FROM records WHERE house_number<>""').fetchone()[0]==60


def test_blocking_keeps_matching_evidence_even_for_broad_keys(tmp_path):
    db=connect(tmp_path/'test.sqlite')
    frame=records()
    frame['Street']='Hauptstrasse 12'
    pid=import_project(db,'Test',frame,mapping(),frame,mapping(),engine='splink5')
    frames=_load(db,pid)
    # Artificial key with large fanout: disable blocking, but not street comparison.
    frames['A']['street_name']='hauptstrasse'
    frames['B']['street_name']='hauptstrasse'
    _cap_block_fanout(frames,400000,fields=['street_name','email'])
    assert all(x=='hauptstrasse' for x in frames['A']['street_name'])
    assert frames['A']['block_street_name'].isna().all()
    assert frames['A']['block_email'].notna().all()


def test_config_prevents_unsafe_fields_and_unordered_thresholds():
    with pytest.raises(ValueError): validate_config({'blocking_fields':['unknown','uid']})
    with pytest.raises(ValueError): validate_config({'name_thresholds':[0.82,0.92]})
    with pytest.raises(ValueError): validate_config({'u_max_pairs':9999999999})
    with pytest.raises(ValueError): validate_config({'min_probability':2.0})
    default=validate_config()
    shifted=validate_config({'street_distances':[1,7]})
    assert model_signature(default)!=model_signature(shifted)


def test_diagnostics_are_fieldwise_not_claimed_as_splink_evidence(tmp_path):
    db=connect(tmp_path/'db.sqlite')
    a=records().iloc[:1].copy(); b=a.copy();b['House']='2';b['Street']='Bahnhofstrasse 2'
    pid=import_project(db,'Test',a,mapping(),b,mapping())
    aa= db.execute('SELECT * FROM records WHERE project_id=? AND side="A"',(pid,)).fetchone()
    bb= db.execute('SELECT * FROM records WHERE project_id=? AND side="B"',(pid,)).fetchone()
    diag={r['Feld']:r for r in comparison_diagnostics(aa,bb)}
    assert diag['Strassenname']['Befund']=='Identisch'
    assert diag['Hausnummer']['Befund']=='Abweichend'
    assert diag['Hausnummer']['Ähnlichkeit']==0


def test_supervised_training_writes_separate_model_leaves_review_intact(tmp_path,monkeypatch):
    import splink_matcher as sm
    db=connect(tmp_path/'db.sqlite')
    df=records()
    pid=import_project(db,'Test',df,mapping(),df,mapping(),engine='splink5')
    run_matching(db,pid,minimum=20)
    pairs=db.execute('SELECT a_pk,b_pk FROM pairs WHERE project_id=?',(pid,)).fetchall()
    assert len(pairs)>=20
    for row in pairs[:24]:
        set_review(db,pid,row['a_pk'],row['b_pk'],'bestaetigt')
    for row in pairs[24:26]:
        set_review(db,pid,row['a_pk'],row['b_pk'],'abgelehnt')
    class MockRel:
        def project(self, names): return self
        def iter_rows(self,batch_size):
            return iter([])
    class MockLinker:
        def __init__(self):
            self.misc=types.SimpleNamespace(save_model_to_json=lambda: {'link_type':'link_only'})
            self.training=types.SimpleNamespace(
                estimate_u_using_random_sampling=lambda **kw: None,
                estimate_m_from_pairwise_labels=lambda table: None)
            self.table_management=types.SimpleNamespace(register_table=lambda df,name,overwrite: None)
            self.inference=types.SimpleNamespace(predict=lambda **kw:types.SimpleNamespace(as_duckdbpyrelation=lambda:MockRel()))
    monkeypatch.setattr(sm,'_new_linker',lambda *a,**kw:MockLinker())
    model,stats=train_model_from_reviews(db,pid)
    assert model>0 and stats['positive_training']>=16
    assert stats['positive_holdout']>=4
    assert stats['negative_holdout']==2
    path,cfg,row=_resolve_model(db,model)
    assert json.loads(path.read_text())['link_type']=='link_only'
    assert row['training_type']=='manuelle_labels'
    assert len(available_models(db))==1
    assert db.execute('SELECT count(*) FROM pairs WHERE project_id=? AND state="bestaetigt"',(pid,)).fetchone()[0]==24


def test_project_deletion_cleans_own_model_files(tmp_path):
    from engine import delete_project
    import splink_matcher as sm
    db=connect(tmp_path/'test.sqlite')
    df=records().iloc[:1]
    pid=import_project(db,'Delete',df,mapping(),df,mapping(),engine='splink5')
    class Saver:
        misc=types.SimpleNamespace(save_model_to_json=lambda: {'link_type':'link_only'})
    with db:
        model_id=sm._save_model(db,Saver(),pid,validate_config(),'test',{})
    path,_,_=sm._resolve_model(db,model_id)
    assert path.exists()
    delete_project(db,pid)
    assert not path.exists()
    assert db.execute('SELECT count(*) FROM trained_models WHERE project_id=?',(pid,)).fetchone()[0]==0
