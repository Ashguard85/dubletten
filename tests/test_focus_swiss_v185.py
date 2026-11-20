"""Address focus: honest evidence colours, unchanged source data, safe patching."""
import importlib
import json
from pathlib import Path
import sys
import types

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
sys.path.insert(0,str(ROOT/'scripts'))
from focus_swiss_data import (original_fields, original_address_lines,
    comparison_rows, house_suggestions, display_values, safe)
from apply_focus_patch import patch_file
from apply_swiss_focus_patch import patch_files


def case(**kwargs):
    raw=kwargs.get('raw', {'ID':'00019','Strasse':'Steinenvorstadt',
                     'Hausnummer':'59','PLZ':'4051','Ort':'Basel'})
    norm=kwargs.get('norm', {'street_source':'Steinenvorstadt',
                     'house_source':'59', 'house':'59',
                     'postal_source':'4051','city_source':'Basel'})
    return {'original_json':json.dumps(raw,ensure_ascii=False),
            'normalized_json':json.dumps(norm,ensure_ascii=False)}


def mapping():
    return {'street':'Strasse','house_number':'Hausnummer','postcode':'PLZ','city':'Ort'}


def test_exact_original_fields_from_upload_and_assembly():
    c=case()
    original=original_fields(c,mapping())
    assert original['strasse']=='Steinenvorstadt'
    assert original['hausnummer']=='59'
    assert original_address_lines(original)==('Steinenvorstadt 59','4051 Basel')
    assert original['original']['ID']=='00019'
    assert 'Steinenvorstadt' in c['original_json']
    # For a combined input column, do not duplicate house number.
    c=case(raw={'Strasse':'Bahnhofstrasse 12a','PLZ':'6003','Ort':'Luzern'},
           norm={'street_source':'Bahnhofstrasse 12a', 'house_source':'',
                 'house':'12a','postal_source':'6003','city_source':'Luzern'})
    original=original_fields(c,{'street':'Strasse','house_number':'','postcode':'PLZ','city':'Ort'})
    assert original_address_lines(original)==('Bahnhofstrasse 12a','6003 Luzern')


def test_hausnummer_unknown_is_amber_not_false_error():
    before={'strasse':'Steinenvorstadt','hausnummer':'59','plz':'4051','ort':'Basel'}
    rows=comparison_rows(before,before,'hausnummer_unbekannt')
    assert [r['tone'] for r in rows]==['green','amber','neutral','neutral']
    assert rows[1]['verdict']=='Nummer nicht gefunden'
    assert house_suggestions('Strasse vorhanden, mögliche Hausnummern: 1, 10, 11, 12, 1a') == [
        '1','10','11','12','1a']


def test_official_and_manual_proposals_have_different_claims():
    before={'strasse':'Bahnhofstr.','hausnummer':'12a','plz':'6003','ort':'Luzern'}
    after={'strasse':'Bahnhofstrasse','hausnummer':'12a','plz':'6003','ort':'Luzern'}
    verified=comparison_rows(before,after,'strasse_aehnlich',official=True)
    assert verified[0]['tone']=='amber'
    assert verified[0]['verdict']=='Amtlicher Vorschlag'
    assert all(r['tone']=='green' for r in verified[1:])
    manual=comparison_rows(before,after,'strasse_aehnlich',manually_edited=True)
    assert manual[0]['verdict']=='Manuell geändert'
    assert all(r['tone']=='neutral' for r in manual[1:])
    no_official=comparison_rows(before,after,'nicht_gefunden')
    assert no_official[0]['tone']=='amber'
    assert no_official[1]['tone']=='neutral'


def test_explicit_conflict_red_and_html_escaped():
    before={'strasse':'<script>alert(1)</script>','hausnummer':'12','plz':'6003','ort':'Luzern'}
    rows=comparison_rows(before,before,'eingabe_konflikt')
    assert rows[1]['tone']=='red'
    assert safe(before['strasse'])=='&lt;script&gt;alert(1)&lt;/script&gt;'
    # Use fake Streamlit to load the presentation helper without a real browser.
    monkey=types.ModuleType('streamlit')
    old=sys.modules.get('streamlit')
    sys.modules['streamlit']=monkey
    try:
        sys.modules.pop('focus_swiss_view',None)
        view=importlib.import_module('focus_swiss_view')
        html=view._comparison_html({'strasse':before['strasse'],'hausnummer':'12',
                           'plz':'6003','ort':'Luzern'},rows,'','eingabe_konflikt',[])
        assert '<script>' not in html
        assert 'fsv-status-red' in html
        assert '&lt;script&gt;' in html
    finally:
        sys.modules.pop('focus_swiss_view',None)
        if old is None:sys.modules.pop('streamlit',None)
        else:sys.modules['streamlit']=old


def test_no_imaginary_suggestions():
    assert house_suggestions('Strasse vorhanden, Hausnummer nicht gefunden')==[]
    assert house_suggestions('mögliche Hausnummern: 1, <svg/onload=foo>, 17a')==['1','17a']
    assert len(house_suggestions('mögliche Hausnummern: '+','.join(map(str,range(100)))))==16


def test_swiss_patch_is_additive_and_idempotent(tmp_path):
    main=tmp_path/'ui.py'
    addr=tmp_path/'address_service_ui.py'
    main.write_text('''import streamlit as st
from ai_control import is_enabled
st.title('🔎 CRM-Dublettenprüfung')
st.caption('GUI v1.8.3')
with st.sidebar:
    choices = ['+ Neuer Abgleich', '🏔️ swisstopo-Datenstand'] + ['#1 · CRM']
    selected = st.selectbox('Projekt', choices, index=2)
    show_swisstopo = selected == '🏔️ swisstopo-Datenstand'
if show_swisstopo:
    st.subheader('swisstopo')
if selected == 'Schweizer Adressprüfung':
    render_address_service(db)
''',encoding='utf8')
    addr.write_text('''import streamlit as st

def render(db):
    with review_tab:
        render_review_fragment(db, jobs)
    with export_tab:
        st.write('Export')
''',encoding='utf8')
    assert patch_file(main)
    status=patch_files(main,addr)
    assert status==(True,True)
    original_main, original_addr=main.read_text(),addr.read_text()
    assert patch_files(main,addr)==(False,False)
    assert main.read_text()==original_main
    assert addr.read_text()==original_addr
    assert 'from ai_control import is_enabled' in original_main
    assert "'_focus_route_swiss'" in original_main
    assert "'address_service_selected_job'" in original_addr
    compile(original_main,str(main),'exec')
    compile(original_addr,str(addr),'exec')


def test_unsafe_or_partial_patch_does_not_overwrite_file(tmp_path):
    main=tmp_path/'ui.py';addr=tmp_path/'address_service_ui.py'
    main.write_text("import streamlit as st\n",encoding='utf8')
    addr.write_text('with review_tab:\n    pass\n',encoding='utf8')
    with pytest.raises(RuntimeError,match='Zuerst'):
        patch_files(main,addr)
    assert main.read_text()=='import streamlit as st\n'
    assert addr.read_text()=='with review_tab:\n    pass\n'


def test_render_swiss_focus_card_with_fake_streamlit(monkeypatch):
    """Smoke-run the render path without needing an actual Streamlit server."""
    import sqlite3
    class Box:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def form_submit_button(self, label, **kwargs): return fake.form_submit_button(label, **kwargs)
    class FakeStreamlit(types.ModuleType):
        def __init__(self):
            super().__init__('streamlit')
            self.markdown_calls=[]
            self.buttons=[]
        def caption(self, *a, **k): return None
        def selectbox(self, label, choices, **kwargs): return choices[kwargs.get('index',0)]
        def markdown(self, txt, **kwargs):
            self.markdown_calls.append(txt)
        def form(self, *a, **k): return Box()
        def expander(self, *a, **k): return Box()
        def columns(self, choices, **kwargs):
            return [Box() for _ in range(choices if isinstance(choices,int) else len(choices))]
        def text_input(self, label, value='', **kwargs): return value
        def form_submit_button(self, label, **kwargs):
            self.buttons.append(label)
            return label.startswith('✓ Korrektur')
        def json(self,*a,**k): return None
        def error(self,*a,**k): raise AssertionError('unexpected Streamlit error')
    fake=FakeStreamlit()
    monkeypatch.setitem(sys.modules,'streamlit',fake)
    module=types.ModuleType('address_service')
    module.display_street_without_number=lambda a: a['street_source']
    monkeypatch.setitem(sys.modules,'address_service',module)
    sys.modules.pop('focus_swiss_view',None)
    from focus_swiss_view import render_swiss_case
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE address_jobs(job_id INTEGER,mapping_json TEXT)')
    conn.execute('INSERT INTO address_jobs VALUES (?,?)',(1,json.dumps(mapping())))
    payload=case()
    row={**payload,'row_pk':7,'job_id':1,'source_id':'00019','result':'hausnummer_unbekannt',
         'hint':'Strasse vorhanden, mögliche Hausnummern: 10, 12, 1a',
         'proposals_json':'[]','correction_json':'{}','decision':'offen'}
    try:
        action, correction = render_swiss_case(conn,row)
        assert action=='korrigiert'
        assert correction=={'strasse':'Steinenvorstadt','hausnummer':'59','plz':'4051','ort':'Basel'}
        html='\n'.join(fake.markdown_calls)
        assert 'ORIGINALADRESSE' in html
        assert 'Steinenvorstadt 59' in html
        assert 'fsv-status-amber' in html
        assert 'fsv-number' in html
        assert len(fake.buttons)==4
    finally:
        sys.modules.pop('focus_swiss_view',None)
        conn.close()
