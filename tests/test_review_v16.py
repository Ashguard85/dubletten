"""Integration tests for shared review queries using a real SQLite DB, no Streamlit."""
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from engine import connect, import_project, run_matching, set_review, save_llm
from review_data import pair_page, conflict_flags, cockpit_counts, grid_rows, _filters


@pytest.fixture()
def populated(tmp_path):
    db = connect(tmp_path / 'data.db')
    left = pd.DataFrame([
        ('A001','Müller Informatik AG','Bahnhofstrasse 12','6003','Luzern'),
        ('A002','H. Meier Elektro GmbH','Hauptstrasse 14','6048','Horw'),
        ('A003','Keller Müller AG','Hauptstrasse 12','6048','Horw'),
    ],columns=['ID','Firma','Strasse','PLZ','Ort'])
    right = pd.DataFrame([
        ('B001','Mueller Informatik','Bahnhofstrasse 12','6003','Luzern'),
        ('B002','Heinrich Meier Elektro GmbH','Hauptstrasse 14','6048','Horw'),
        ('B003','Keller Müller AG','Hauptstrasse 12','6048','Horw'),
        ('B004','Keller Müller AG','Hauptstrasse 21','6048','Horw'),
    ],columns=['ID','Firma','Strasse','PLZ','Ort'])
    columns = {'id':'ID','company':'Firma','street':'Strasse','postcode':'PLZ','city':'Ort'}
    pid = import_project(db, 'Test', left, columns, right, columns, engine='rapidfuzz')
    run_matching(db, pid, minimum=30)
    yield db, pid
    db.close()


def test_default_page_and_paging(populated):
    db,pid = populated
    count,pages,page,rows = pair_page(db,pid,state='Alle',min_score=0,page_size=20)
    assert count >= 3
    assert page == pages == 1
    assert all('a_json' in r.keys() and 'a_name' in r.keys() for r in rows)
    n,pages,page,short = pair_page(db,pid,state='Alle',min_score=0,page=999,page_size=20)
    assert (n,pages,page)==(count,1,1)
    assert len(short)==len(rows)


def test_filter_search_by_id_and_company(populated):
    db,pid=populated
    n,_,_,rows=pair_page(db,pid,state='Alle',min_score=0,search='A001')
    assert n>=1 and all(r['a_id']=='A001' for r in rows)
    n2,_,_,rows2=pair_page(db,pid,state='Alle',min_score=0,search='Müller')
    assert n2>=n and rows2
    n3,_,_,rows3=pair_page(db,pid,state='Alle',min_score=0,search='MuellER')
    assert n3>=1 and rows3
    n4,_,_,rows4=pair_page(db,pid,state='Alle',min_score=0,search='a%' )
    assert n4==0 and rows4==[]  # %-wildcards must be escaped


def test_review_state_is_shared_across_views(populated):
    db,pid=populated
    _,_,_,rows=pair_page(db,pid,state='Alle',min_score=0)
    first=rows[0]
    set_review(db,pid,first['a_pk'],first['b_pk'],'bestaetigt')
    n,_,_,accepted=pair_page(db,pid,state='bestaetigt',min_score=0)
    assert n==1 and accepted[0]['a_pk']==first['a_pk']
    counts=cockpit_counts(db,pid)
    assert counts['bestaetigt']==1
    assert counts['gesamt'] >= 3
    save_llm(db,pid,first['a_pk'],first['b_pk'],'gleich','Künstlicher Test','gemma3:12b')
    a,_,_,ai=pair_page(db,pid,state='Alle',min_score=0,risk='KI-geprüft')
    assert a==1 and ai[0]['llm_decision']=='gleich'


def test_conflicts_and_address_differences_are_separate(populated):
    db,pid=populated
    _,_,_,rows=pair_page(db,pid,state='Alle',min_score=0)
    multi=conflict_flags(db,pid,rows)
    assert isinstance(multi,set)
    grid=grid_rows(rows,multi)
    assert len(grid)==len(rows)
    assert all('Hinweise' in r and 'Status' in r for r in grid)
    count,_,_,diffs=pair_page(db,pid,state='Alle',min_score=0,risk='Hausnummer-Abweichung')
    assert all(r['a_house'] != r['b_house'] for r in diffs)
    assert all(r['a_house'] and r['b_house'] for r in diffs)
    assert cockpit_counts(db,pid)['hausnummer_abweichung']==count
    flagged,_,_,multi_rows=pair_page(db,pid,state='Alle',min_score=0,risk='Mehrfachkandidaten')
    assert flagged == len(multi_rows)
    assert cockpit_counts(db,pid)['mehrfachkandidaten']==flagged


def test_no_injection_or_invalid_filters(populated):
    db,pid=populated
    with pytest.raises(ValueError,match='Prüfstatus'):
        pair_page(db,pid,state="offen' OR 1=1--")
    with pytest.raises(ValueError,match='Arbeitskategorie'):
        pair_page(db,pid,risk='DROP TABLE pairs')
    with pytest.raises(ValueError,match='Seitengrösse'):
        pair_page(db,pid,page_size=999999)
    n,_,_,_ = pair_page(db,pid,state='Alle',min_score=0,search="' OR 1=1--")
    assert n == 0
    assert db.execute('SELECT COUNT(*) FROM projects').fetchone()[0]==1


def test_project_filter_isolated(tmp_path):
    db=connect(tmp_path/'a.db')
    from engine import import_project, run_matching
    df=pd.DataFrame([('x1','Test AG','Hauptstrasse 1','6000','Luzern')],columns=['ID','Firma','Strasse','PLZ','Ort'])
    fields={'id':'ID','company':'Firma','street':'Strasse','postcode':'PLZ','city':'Ort'}
    p1=import_project(db,'A',df,fields,df,fields)
    p2=import_project(db,'B',df,fields,df,fields)
    run_matching(db,p1,minimum=0)
    assert pair_page(db,p1,state='Alle',min_score=0)[0]==1
    assert pair_page(db,p2,state='Alle',min_score=0)[0]==0
    db.close()
