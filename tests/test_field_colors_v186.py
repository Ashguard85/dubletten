"""Exact/near/missing/different evidence never confuses colors with identity proof."""
import json
import sys
from pathlib import Path
import pandas as pd
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from field_colors import classify, pair_rows, reference_rows, html_table, CSS
from engine import connect,import_project,run_matching
from migration import run_reference_check


def test_exact_normalization_similarity_and_conflict():
    assert classify('Strasse','Matthof','Matthof')[0]=='green'
    assert classify('Strasse','Matthof','MATTHOF')[0]=='amber'
    assert classify('Firma / Person','Müller Informatik AG','Mueller Informatik')[0]=='amber'
    assert classify('Strasse','Bahnhofstr.','Bahnhofstrasse')[0]=='amber'
    assert classify('Strasse','Matthof','Hauptstrasse')[0]=='red'
    assert classify('Hausnummer','24','59')[0]=='red'
    assert classify('PLZ','6014','4051')[0]=='red'
    assert classify('Ort','','Basel')[0]=='neutral'
    assert classify('Hausnummer','','')[0]=='neutral'
    assert classify('UID','CHE-123.456.789','CHE-987.654.321')[0]=='red'
    assert classify('PLZ','6014','6014')[0]=='green'


def test_separate_house_number_color_with_combined_input():
    a={'company':'A AG','street':'Matthof 24','house_number':'','postcode':'6014','city':'Luzern'}
    b={'company':'A AG','street':'Matthof 59','house_number':'','postcode':'6014','city':'Luzern'}
    rows=pair_rows(a,b)
    assert len(rows)==5
    assert rows[1]['field']=='Strasse' and rows[1]['tone']=='green'  # same street
    assert rows[2]['field']=='Hausnummer' and rows[2]['tone']=='red'
    assert rows[3]['tone']=='green' and rows[4]['tone']=='green'
    assert a['street']=='Matthof 24'  # input never modified
    assert b['street']=='Matthof 59'


def test_mixed_case_and_no_phone_evidence():
    a={'company':'Hans Müller GmbH','street':'Hauptstrasse','house_number':'12a',
       'postcode':'6014','city':'Luzern','phone':''}
    b={'company':'Hans Mueller','street':'Hauptstrasse 12a','house_number':'',
       'postcode':'6014','city':'LUZERN','phone':'041 123 45 67'}
    rows=pair_rows(a,b,extra=True)
    assert rows[0]['tone']=='amber'
    assert rows[1]['tone']=='green'
    assert rows[2]['tone']=='green'
    assert rows[4]['tone']=='amber'
    assert next(r for r in rows if r['field']=='Telefon')['tone']=='neutral'


def test_reference_uses_stored_deterministic_field_results():
    rows=reference_rows([
      {'status':'identisch','bezeichnung':'Hausnummer','a_wert':'12','b_wert':'12'},
      {'status':'normalisiert_gleich','bezeichnung':'Strasse','a_wert':'Bahnhofstr.','b_wert':'Bahnhofstrasse'},
      {'status':'abweichend','bezeichnung':'PLZ','a_wert':'6014','b_wert':'6003'},
      {'status':'fehlt','bezeichnung':'Ort','a_wert':'','b_wert':'Luzern'},
    ])
    assert [r['tone'] for r in rows]==['green','amber','red','neutral']
    assert [r['label'] for r in rows]==['✓ identisch','≈ normalisiert','≠ abweichend','– fehlt']


def test_html_escapes_original_crm_data_and_keeps_accessible_labels():
    rows=pair_rows({'company':'<script>alert(1)</script>','street':'M&N 12','postcode':'6014'},
                   {'company':'<img src=x onerror=alert(1)>','street':'M&N 12','postcode':'6014'})
    html=html_table(rows)
    assert '<script>' not in html and '<img' not in html
    assert '&lt;script&gt;' in html and '&lt;img' in html
    assert 'M&amp;N' in html
    assert 'aria-label=' in html and '≠ abweichend' in html
    assert 'crm-ev-red' in html and 'crm-ev-green' in html
    assert '.crm-ev-amber' in CSS and '.crm-ev-neutral' in CSS


def test_integrates_with_real_reference_data(tmp_path):
    from engine import import_project
    a=pd.DataFrame([{'ID':'A1','Firma':'Example AG','Strasse':'Matthof 24', 'PLZ':'6014','Ort':'Luzern','B_Referenz':'B1'}])
    b=pd.DataFrame([{'ID':'B1','Firma':'Example AG','Strasse':'Matthof 25', 'PLZ':'6014','Ort':'Luzern'}])
    mapping={'id':'ID','company':'Firma','street':'Strasse','postcode':'PLZ','city':'Ort'}
    db=connect(tmp_path/'crm.sqlite')
    pid=import_project(db,'Test',a,mapping,b,mapping,mode='dual',a_ref_column='B_Referenz')
    assert run_reference_check(db,pid)==1
    original=db.execute('SELECT differences_json FROM reference_checks WHERE project_id=?',(pid,)).fetchone()[0]
    rows=reference_rows(json.loads(original))
    assert next(x for x in rows if x['field']=='Hausnummer')['tone']=='red'
    assert next(x for x in rows if x['field']=='PLZ')['tone']=='green'
    assert db.execute('SELECT COUNT(*) FROM pairs WHERE project_id=?',(pid,)).fetchone()[0]==0
    db.close()
