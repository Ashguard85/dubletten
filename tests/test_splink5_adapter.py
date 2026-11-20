"""Contract tests for our Splink 5 adapter.

These tests use a strict stub of the documented Splink API, NOT the real
Splink engine. Run the real integration smoke test in the built Docker image.
"""
import json
import sqlite3
import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from engine import connect, import_project, page_pairs, project_stats, set_review, export_pairs
from splink_matcher import _cap_block_fanout, _load, run_splink_matching


def maps():
    return {'id': 'ID', 'company': 'Firma', 'street': 'Strasse',
            'postcode': 'PLZ', 'city': 'Ort', 'email': 'E-Mail',
            'phone': 'Telefon', 'uid': 'UID'}


def fixtures():
    cols = ['ID', 'Firma', 'Strasse', 'PLZ', 'Ort', 'E-Mail', 'Telefon', 'UID']
    a = pd.DataFrame([('001', 'Müller AG', 'Bahnhofstrasse 12', '6003', 'Luzern',
                       'info@mueller.ch', '', 'CHE-111.222.333'),
                      ('002', 'Meyer GmbH', 'Seeweg 4', '6048', 'Horw', '', '', '')], columns=cols)
    b = pd.DataFrame([('B1', 'Mueller', 'Bahnhofstr. 12', '6003', 'Luzern',
                       'info@mueller.ch', '', 'CHE111222333'),
                      ('B2', 'Meyer GmbH', 'Seeweg 4', '6048', 'Horw', '', '', '')], columns=cols)
    return a,b


def test_db_migration_preserves_old_projects(tmp_path):
    db = connect(tmp_path/'local.sqlite')
    a,b = fixtures()
    old = import_project(db,'Altbestand',a,maps(),b,maps())
    db.close()
    db = sqlite3.connect(tmp_path/'local.sqlite')
    # Simulate an actual pre-upgrade v1.3 schema.
    db.execute('ALTER TABLE projects RENAME TO projects_new')
    db.execute('CREATE TABLE projects(project_id INTEGER PRIMARY KEY, title TEXT NOT NULL, '
               'created_at TEXT NOT NULL, a_count INTEGER NOT NULL, b_count INTEGER NOT NULL, '
               'candidate_count INTEGER NOT NULL DEFAULT 0)')
    db.execute('INSERT INTO projects SELECT project_id,title,created_at,a_count,b_count,candidate_count FROM projects_new')
    db.execute('DROP TABLE projects_new')
    db.commit()
    db.close()
    db = connect(tmp_path/'local.sqlite')
    assert project_stats(db,old)['engine']=='rapidfuzz'
    assert 'match_probability' in {r['name'] for r in db.execute('PRAGMA table_info(pairs)')}


def test_preflight_rejects_large_candidate_fanout():
    import pandas as pd
    frames = {s: pd.DataFrame({'uid': [f'key-{i}' for i in range(30)],
                               'email':[None]*30, 'phone':[None]*30,
                               'zip_name':[None]*30, 'zip_street':[None]*30,
                               'city_name':[None]*30, 'name_head':[None]*30}) for s in ('A','B')}
    upper, blocks = _cap_block_fanout(frames, max_candidates=40, fields=['uid','email','phone','zip_name','zip_street','city_name','name_head'])
    assert upper==30
    assert blocks[0]['pair_upper_bound']==30


def test_missing_values_are_null(tmp_path):
    db=connect(tmp_path/'test.sqlite')
    a,b=fixtures()
    pid=import_project(db,'Test',a,maps(),b,maps())
    frames=_load(db,pid)
    assert frames['A'].loc[0,'phone'] is None
    assert frames['A'].loc[1,'uid'] is None


def test_adapter_persists_probabilities_and_keeps_review_workflow(tmp_path,monkeypatch):
    fake = types.ModuleType('splink')
    fake.__path__ = []
    cl = types.ModuleType('splink.comparison_library')
    class Compare:
        def __init__(self, *args, **kwargs): self.args=args
        def configure(self, **kwargs): return self
    for key in ('JaroWinklerAtThresholds', 'LevenshteinAtThresholds', 'ExactMatch'):
        setattr(cl,key,Compare)
    fake.comparison_library=cl
    fake.block_on=lambda name: ('block',name)
    fake.SettingsCreator=lambda **kwargs: kwargs
    class Api:
        def register(self,frame,dataset_display_name):return (frame,dataset_display_name)
    fake.DuckDBAPI=Api
    class Relation:
        def __init__(self,inputs):
            self.data=[(int(inputs[0][0].iloc[0]['unique_id']),
                        int(inputs[1][0].iloc[0]['unique_id']),
                        'CRM_A','CRM_B',0.987)]
        def project(self,names):
            assert names.startswith('unique_id_l, unique_id_r')
            return self
        def iter_rows(self,batch_size=1024):
            assert batch_size == 2000
            # Strict DuckDB 2.0 contract: relation has no fetchmany method.
            yield from self.data
    class Prediction:
        def __init__(self,inputs):self.inputs=inputs
        def as_duckdbpyrelation(self):return Relation(self.inputs)
    class Inference:
        def __init__(self,inputs):self.inputs=inputs
        def predict(self, threshold_match_probability):
            assert threshold_match_probability==0.05
            return Prediction(self.inputs)
    class Linker:
        def __init__(self,inputs,settings):
            assert settings['link_type']=='link_only'
            assert len(inputs)==2
            self.inference=Inference(inputs)
            self.misc=types.SimpleNamespace(save_model_to_json=lambda: {'_description':'mock-model'})
    fake.Linker=Linker
    monkeypatch.setitem(sys.modules, 'splink', fake)
    monkeypatch.setitem(sys.modules, 'splink.comparison_library', cl)
    monkeypatch.setattr('splink_matcher.package_version', lambda _: '5.0.0')
    db=connect(tmp_path/'db.sqlite')
    a,b=fixtures()
    pid=import_project(db,'Splink-Projekt',a,maps(),b,maps(),engine='splink5')
    n,kept=run_splink_matching(db,pid)
    assert n>=2 and kept==1
    count,pairs=page_pairs(db,pid,state='Alle',min_score=0,limit=10)
    assert count==1
    pair=pairs[0]
    assert pair['match_probability']==0.987
    assert pair['score']==99
    assert 'Splink 5' in pair['reason']
    assert json.loads(project_stats(db,pid)['match_model_info'])['mode']=='feste_priors_kleine_stichprobe'
    set_review(db,pid,pair['a_pk'],pair['b_pk'],'bestaetigt')
    exported=export_pairs(db,pid,True).decode('utf-8-sig')
    assert 'splink_match_probability' in exported
    assert '0.987' in exported
    assert 'splink5' in exported


def test_duckdb2_iter_rows_flushes_multiple_sqlite_batches(tmp_path, monkeypatch):
    """Regression: store >2000 predictions without removed relation.fetchmany()."""
    import splink_matcher as adapter

    n = 2003
    cols = ['ID', 'Firma', 'Strasse', 'PLZ', 'Ort', 'E-Mail', 'Telefon', 'UID']
    values_a = [(f'A-{i:05d}', f'Firma {i}', f'Weg {i}', f'{6000+i}', 'Ort',
                 f'firma{i}@example.invalid', '', f'CHE-{i:09d}') for i in range(n)]
    values_b = [(f'B-{i:05d}', f'Firma {i}', f'Weg {i}', f'{6000+i}', 'Ort',
                 f'firma{i}@example.invalid', '', f'CHE-{i:09d}') for i in range(n)]
    db = connect(tmp_path / 'multi.sqlite')
    pid = import_project(db, 'Splink Mehrfach-Chunks', pd.DataFrame(values_a, columns=cols),
                         maps(), pd.DataFrame(values_b, columns=cols), maps(), engine='splink5')

    class Relation:
        def project(self, columns):
            assert 'match_probability' in columns
            return self

        def iter_rows(self, batch_size=1024):
            assert batch_size == 2000
            for i in range(n):
                yield (i + 1, n + i + 1, 'CRM_A', 'CRM_B', 0.91)

    class Predictions:
        def as_duckdbpyrelation(self):
            return Relation()

    class Inference:
        def predict(self, threshold_match_probability):
            return Predictions()

    class Linker:
        inference = Inference()
        misc = types.SimpleNamespace(save_model_to_json=lambda: {'_description':'mock-model'})

    splink_mock = types.ModuleType('splink')
    monkeypatch.setitem(sys.modules, 'splink', splink_mock)
    monkeypatch.setattr(adapter, 'package_version', lambda name: '5.0.0')
    monkeypatch.setattr(adapter, '_new_linker', lambda *args, **kwargs: Linker())
    monkeypatch.setattr(adapter, '_train', lambda *args, **kwargs: None)
    upper, kept = run_splink_matching(db, pid)
    assert upper >= n
    assert kept == n
    assert db.execute('SELECT COUNT(*) FROM pairs WHERE project_id=?', (pid,)).fetchone()[0] == n
    assert db.execute('SELECT COUNT(DISTINCT a_pk) FROM pairs WHERE project_id=?', (pid,)).fetchone()[0] == n
