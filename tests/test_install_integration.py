"""Fresh-install integration tests without Streamlit, DuckDB or a Docker daemon."""
from __future__ import annotations
import csv,io,json,sys
from pathlib import Path
import pandas as pd
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from engine import connect,import_project,run_matching,set_review,export_pairs,read_upload,split_street_house
from migration import run_reference_check,reference_overview,export_references,export_reference_field_differences,export_workflow1_inventory
from swisstopo import ensure_schema as sw_schema,import_snapshot,inspect_source,validate_record,active_snapshot
from address_service import (ensure_schema,import_job,page_rows,save_review,
    bulk_approval_preview,bulk_approve_exact,export_job,display_street_without_number)
from focus_data import reference_focus_page,save_reference_review
from review_data import pair_page
from ai_control import is_enabled,set_enabled,enabled_from_path
import llm

@pytest.fixture
def database(tmp_path):
 db=connect(tmp_path/'new.sqlite');sw_schema(db);ensure_schema(db)
 yield db
 db.close()

def test_full_crm_import_reference_matching_and_review(database):
 db=database
 fa=pd.read_csv(ROOT/'examples/CRM_A_Testdaten_Fokusmodus.csv',sep=';',dtype=str,keep_default_na=False)
 fb=pd.read_csv(ROOT/'examples/CRM_B_Testdaten_Fokusmodus.csv',sep=';',dtype=str,keep_default_na=False)
 mapping={'id':'ID','company':'Firma','first_name':'Vorname','last_name':'Nachname',
     'street':'Strasse','house_number':'Hausnummer','postcode':'PLZ','city':'Ort',
     'country':'Land','email':'E-Mail','phone':'Telefon','uid':''}
 pid=import_project(db,'End-to-end',fa,mapping,fb,mapping,mode='dual',a_ref_column='B_Referenz',
                    a_exclude_column='Migration',exclude_values=['Nein'])
 assert db.execute('SELECT count(*) FROM records WHERE project_id=?',(pid,)).fetchone()[0]==125
 assert run_reference_check(db,pid)==23
 overview={r['result']:r['anzahl'] for r in reference_overview(db,pid)}
 assert overview['referenz_fehlt']==2
 assert overview['mehrfachreferenz']==2
 count,kept=run_matching(db,pid,minimum=65)
 assert count>0 and kept>0
 total,_,_,rows=pair_page(db,pid,state='offen',min_score=65)
 assert total>0 and rows
 pair=rows[0]
 set_review(db,pid,pair['a_pk'],pair['b_pk'],'unklar')
 assert db.execute('SELECT state FROM pairs WHERE project_id=? AND a_pk=? AND b_pk=?',(pid,pair['a_pk'],pair['b_pk'])).fetchone()[0]=='unklar'
 assert b'CRM_A_ID' in export_pairs(db,pid)
 assert b'B_Referenz_in_A' in export_references(db,pid)
 assert b'Wert_A' in export_reference_field_differences(db,pid)
 assert len(list(csv.DictReader(io.StringIO(export_workflow1_inventory(db,pid).decode('utf-8-sig')),delimiter=';')))==66
 count,r=reference_focus_page(db,pid,decision='offen')
 assert count>0
 save_reference_review(db,pid,r['a_pk'],'geprueft')
 assert reference_focus_page(db,pid,decision='geprueft')[0]==1

def test_official_zip_and_standalone_addresses(tmp_path,database):
 from zipfile import ZipFile
 db=database
 zip_path=tmp_path/'original.zip'
 content=(ROOT/'examples/TEST-SWISSTOPO-KLEIN.csv').read_bytes()
 with ZipFile(zip_path,'w') as z:
  z.writestr('ADR_GBAS.csv',content)
  z.writestr('metadata.csv','meta;a\nexample;1\n')
 assert inspect_source(zip_path)[0]['format']=='ZIP_LABEL'
 sid,count=import_snapshot(db,zip_path,'2026-11-20')
 assert count==3
 assert active_snapshot(db)['snapshot_id']==sid
 frame=pd.DataFrame([
    {'ID':'0001','Adresse':'Matthof 24','PLZ':'6014','Ort':'Luzern','Land':'CH'},
    {'ID':'0002','Adresse':'Steinenvorstadt 59','PLZ':'4051','Ort':'Basel','Land':'CH'},
    {'ID':'0003','Adresse':'Bahnhofstr. 12a','PLZ':'6003','Ort':'Luzern','Land':'CH'},
    {'ID':'0004','Adresse':'Matthof 24','PLZ':'6014','Ort':'Luzern','Land':'DE'},
 ])
 jid=import_job(db,frame,'Swiss test','file.csv',{'id':'ID','street':'Adresse','house_number':'','postcode':'PLZ','city':'Ort','country':'Land'})
 total,rows=page_rows(db,jid,limit=20)
 assert total==4
 assert [r['result'] for r in rows]==['amtlich_aktuell','hausnummer_unbekannt','amtlich_aktuell','ausland']
 assert display_street_without_number(json.loads(rows[0]['normalized_json']))=='Matthof'
 preview=bulk_approval_preview(db,jid)
 assert preview['count']==1
 assert bulk_approve_exact(db,jid,preview['count'],preview['digest'])==1
 assert bulk_approval_preview(db,jid)['count']==0
 save_review(db,jid,rows[1]['row_pk'],'uebersprungen')
 assert page_rows(db,jid,decision='uebersprungen')[0]==1
 save_review(db,jid,rows[2]['row_pk'],'korrigiert',{'strasse':'Bahnhofstrasse','hausnummer':'12a','plz':'6003','ort':'Luzern'})
 whole=list(csv.DictReader(io.StringIO(export_job(db,jid,split_house=True).decode('utf-8-sig')),delimiter=';'))
 assert whole[0]['Adresse']=='Matthof 24'
 assert whole[0]['Ziel_Hausnummer']=='24'
 assert whole[0]['Pruefentscheid']=='massenfreigegeben'
 assert whole[2]['Korrigiert_Strasse']=='Bahnhofstrasse'
 assert len(list(csv.DictReader(io.StringIO(export_job(db,jid,only_bulk=True).decode('utf-8-sig')),delimiter=';')))==1

def test_ai_off_by_default_and_no_network(tmp_path,monkeypatch):
 path=tmp_path/'config.sqlite';db=connect(path)
 monkeypatch.setenv('DB_PATH',str(path))
 assert not is_enabled(db) and not enabled_from_path()
 called=[]
 monkeypatch.setattr(llm.session,'get',lambda *a,**kw:called.append('get'))
 monkeypatch.setattr(llm.session,'post',lambda *a,**kw:called.append('post'))
 assert llm.models()==[]
 with pytest.raises(RuntimeError,match='ausgeschaltet'):llm.assess({}, {})
 assert called==[]
 set_enabled(db,True);assert enabled_from_path()
 set_enabled(db,False);assert not enabled_from_path()
 db.close()

def test_explicit_mapping_and_house_number():
 assert split_street_house('Matthof 24','')[1]=='24'
 assert split_street_house('Matthof 24','25')[2]
 assert split_street_house('Matthof','24')[1]=='24'
