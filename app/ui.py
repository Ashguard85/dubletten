"""Complete fresh-install Streamlit CRM address and migration auditing UI v1.8.7.2.

All workflows local, no CRM writes, opt-in local Ollama, Swiss focus reviews.
"""
from __future__ import annotations
import io,json,hmac,os,shutil,tempfile
from pathlib import Path
import pandas as pd
import streamlit as st
from engine import (FIELDS,LABELS,best_guess,connect,read_upload,import_project,
  run_matching,list_projects,project_stats,export_pairs,export_unmatched,delete_project)
from migration import (is_dual,run_reference_check,reference_overview,reference_page,
 export_references,export_reference_field_differences,export_workflow1_inventory)
from swisstopo import (ensure_schema as ensure_sw,active_snapshot,list_snapshots,
 inspect_source,import_snapshot,validate_project,validation_counts,export_validation,RESULT_LABELS)
from address_service_ui import render as render_addresses
from focus_ui import render_focus_workspace
from internal_ui import render_create as render_internal_create, render_project as render_internal_project
from review_ui import render_review
from ai_control import ensure_schema as ensure_ai,is_enabled as ai_is_enabled,set_enabled as set_ai_enabled
from llm import models,MODEL
from match_config import validate_config,BLOCK_LIMITS
from splink_matcher import run_splink_matching,available_models,train_model_from_reviews

st.set_page_config(page_title=os.environ.get('APP_TITLE','CRM Dubletten Lokal'),page_icon='🎯',layout='wide',initial_sidebar_state='expanded')
st.markdown('''<style>.block-container{max-width:1480px;padding-top:1.0rem;padding-bottom:1rem}
[data-testid="stMetric"]{padding:.3rem .7rem;border:1px solid #dce5e9;border-radius:8px}</style>''',unsafe_allow_html=True)

password=os.environ.get('APP_PASSWORD','')
if not password:
 st.error('APP_PASSWORD fehlt. Bitte in der Portainer-Stack-Umgebung als starkes Passwort setzen.');st.stop()
if not st.session_state.get('authenticated',False):
 st.title('🔐 CRM-Dubletten – Anmeldung')
 with st.form('login'):
  entered=st.text_input('Passwort',type='password');login=st.form_submit_button('Anmelden',type='primary')
 if login:
  if hmac.compare_digest(entered,password):st.session_state['authenticated']=True;st.rerun()
  else:st.error('Ungültiges Passwort.')
 st.stop()

db_path=os.environ.get('DB_PATH','/app/data/crm.sqlite')
db=connect(db_path)
ensure_sw(db);ensure_ai(db)

# Apply navigation only before the sidebar widget is instantiated.
pending_nav=st.session_state.pop('_next_nav',None)
if pending_nav is not None:st.session_state['nav_selected']=pending_nav

with st.sidebar:
 if st.button('🔒 Abmelden'):
  st.session_state['authenticated']=False;st.rerun()
 st.markdown('### Arbeitsbereich')
 projects=list_projects(db)
 labels={f"#{j['project_id']} · {j['title']}":j['project_id'] for j in projects}
 choices=['+ Neuer Abgleich','➕ Interne Dublettenprüfung','📬 Schweizer Adressprüfung','🏔️ swisstopo-Datenstand','🎯 Fokusmodus',*labels]
 if st.session_state.get('nav_selected') not in choices:
  st.session_state['nav_selected']='+ Neuer Abgleich'
 selected=st.selectbox('Dienst / Projekt',choices,key='nav_selected')
 st.divider();st.markdown('**Lokale KI (Ollama)**')
 enabled=ai_is_enabled(db)
 def switch_ai():set_ai_enabled(db,bool(st.session_state['ai_switch']))
 if st.session_state.get('ai_switch_last') != enabled:
  st.session_state['ai_switch']=enabled;st.session_state['ai_switch_last']=enabled
 st.toggle('KI-Analyse aktivieren',key='ai_switch',on_change=switch_ai)
 enabled=ai_is_enabled(db);st.session_state['ai_switch_last']=enabled
 installed=[];selected_model=MODEL
 if not enabled:st.caption('Aus: Keine Ollama-Netzwerkanfragen aus dieser App.')
 elif selected.startswith('#'):
  installed=models()
  if installed:selected_model=st.selectbox('Lokales Modell',installed,index=installed.index(MODEL) if MODEL in installed else 0)
  else:st.caption('Kein lokales Modell verfügbar; Abgleich funktioniert ohne KI.')
 else:st.caption('Dieser Dienst benötigt keine KI.')

# Avoid tall dashboard headings during case-by-case review.
if selected!='🎯 Fokusmodus':
 st.title('🔎 CRM-Dubletten Lokal')
 st.caption('GUI v1.8.7.2 · Interne Dubletten · Schweizer Adressprüfung · CRM A→B · RapidFuzz / Splink 5 · optionale KI')

if selected=='🎯 Fokusmodus':
 render_focus_workspace(db_path)
 st.stop()
if selected=='📬 Schweizer Adressprüfung':
 render_addresses(db)
 st.stop()
if selected=='🏔️ swisstopo-Datenstand':
 st.subheader('Amtlichen swisstopo-Datenstand lokal installieren')
 current=active_snapshot(db)
 if current:st.success(f"Aktiver Stand {current['reference_date']} · {current['address_count']:,} Adressindexeinträge")
 else:st.warning('Noch kein amtlicher Datenstand vorhanden.')
 st.caption('Originale öffentliche ZIP/CSV von swisstopo: https://www.swisstopo.admin.ch/de/amtliches-verzeichnis-der-gebaeudeadressen')
 path=Path('/app/data/swisstopo');path.mkdir(parents=True,exist_ok=True)
 st.caption('Empfohlen: unveränderte swisstopo-ZIP direkt auf dem Host ablegen (insbesondere bei Upload-Fehlern).')
 upload=st.file_uploader('Referenzdatei (ZIP oder CSV, maximal 250 MB)',type=['zip','csv'],key='sw_upload')
 if upload:
  if upload.size>250*1024**2:st.error('Datei grösser als 250 MB. Bitte per SFTP auf den Host kopieren.')
  elif st.button('Öffentliche ZIP/CSV lokal speichern'):
   tmp=None
   try:
    name=Path(upload.name).name
    with tempfile.NamedTemporaryFile(mode='wb',dir=path,prefix='.upload-',suffix=Path(name).suffix,delete=False) as f:
     tmp=Path(f.name);upload.seek(0);shutil.copyfileobj(upload,f,length=1024*1024)
    inspect_source(tmp);os.replace(tmp,path/name);st.success(f'{name} gespeichert');st.rerun()
   except Exception as e:st.error(str(e))
   finally:
    if tmp:tmp.unlink(missing_ok=True)
 files=sorted(f.name for f in path.iterdir() if f.is_file() and f.suffix.lower() in ('.csv','.zip'))
 if files:
  file=st.selectbox('Lokale Referenzdatei',files)
  date=st.text_input('Bezugsdatum gemäss Quelle (YYYY-MM-DD)')
  if st.button('📥 Lokal indexieren',type='primary'):
   try:
    note=st.empty()
    with st.spinner('Verzeichnis wird ohne Entpacken gestreamt und indexiert …'):
     sid,n=import_snapshot(db,path/file,date,progress=note.caption)
    st.success(f'Datenstand #{sid}: {n:,} Indexeinträge.');st.rerun()
   except Exception as e:st.error(str(e))
 else:st.info('Noch keine lokale ZIP/CSV im Datenordner.')
 with st.expander('Alle importierten Datenstände'):
  st.dataframe(pd.DataFrame(list_snapshots(db)),hide_index=True,use_container_width=True)
 st.warning('Gebäudeadressprüfung ist kein Beleg für Wohnsitz oder Existenz einer Person/Firma.')
 st.stop()

def mapping_form(frame,side):
 st.markdown(f'**CRM {side} · {len(frame):,} Adressen**')
 out={};opts=['(nicht vorhanden)',*frame.columns]
 # Setup stage may scroll; the later focus review stays intentionally compact.
 for i in range(0,len(FIELDS),4):
  boxes=st.columns(4)
  for j,key in enumerate(FIELDS[i:i+4]):
   guess=best_guess(frame.columns,key)
   with boxes[j]:
    pick=st.selectbox(LABELS[key],opts,index=opts.index(guess) if guess else 0,key=f'map_{side}_{key}')
    out[key]='' if pick=='(nicht vorhanden)' else pick
 return out

def splink_settings(prefix):
 default=validate_config()
 with st.expander('⚙️ Splink-5-Parameter (Expertenmodus)',expanded=False):
  a,b=st.columns(2)
  with a:
   split=st.checkbox('Hausnummer separat vergleichen',value=True,key=prefix+'_split')
   name_hi=st.slider('Name: starke Ähnlichkeit',.80,.99,default['name_thresholds'][0],.01,key=prefix+'_hi')
   name_low=st.slider('Name: weitere Ähnlichkeit',.50,.95,default['name_thresholds'][1],.01,key=prefix+'_low')
   blocks=st.multiselect('Blocking-Felder (mindestens 2)',list(BLOCK_LIMITS),default=default['blocking_fields'],key=prefix+'_blocks')
  with b:
   d1=st.number_input('Strasse: geringe Distanz',1,7,default['street_distances'][0],key=prefix+'_d1')
   d2=st.number_input('Strasse: weitere Distanz',2,15,default['street_distances'][1],key=prefix+'_d2')
   u=st.select_slider('Zufallspaare für u-Schätzung',options=[10000,50000,100000,300000,500000,1000000],value=300000,key=prefix+'_u')
   em=st.select_slider('EM-Paare je Regel',options=[10000,50000,100000,250000,500000,1000000],value=250000,key=prefix+'_em')
 return validate_config(dict(split_house_number=split,name_thresholds=[name_hi,name_low],street_distances=[d1,d2],blocking_fields=blocks,u_max_pairs=u,em_max_pairs=em))

if selected=='➕ Interne Dublettenprüfung':
 render_internal_create(db,mapping_form,splink_settings)
 st.stop()

if selected=='+ Neuer Abgleich':
 st.subheader('Neuen CRM-A→B-Abgleich erstellen')
 a,b=st.columns(2)
 with a:file_a=st.file_uploader('CSV/XLSX aus CRM A',type=['csv','xlsx'],key='input_A')
 with b:file_b=st.file_uploader('CSV/XLSX aus CRM B',type=['csv','xlsx'],key='input_B')
 if file_a and file_b:
  try:fa=read_upload(file_a);fb=read_upload(file_b)
  except Exception as e:st.error(str(e));st.stop()
  title=st.text_input('Projektbezeichnung',value='CRM-Prüfung')
  mode=st.radio('Arbeitsweise',['A→B: fehlende und vorhandene Referenzen','Klassischer Dublettenabgleich'],horizontal=True)
  dual=mode.startswith('A→B');engine=st.radio('Kandidatenabgleich',['RapidFuzz','Splink 5'],horizontal=True)
  if dual:
   cols=['(nicht vorhanden)',*fa.columns]
   ref_guess=next((x for x in fa.columns if 'referenz' in x.casefold() or 'b_id' in x.casefold()),'')
   ref=st.selectbox('B-Referenz aus CRM A',cols,index=cols.index(ref_guess) if ref_guess else 0)
   exclusion=st.selectbox('Optional: Nicht migrieren / Ausschlussspalte',cols)
   exclusion_values=st.text_input('Auszuschliessende Werte (kommagetrennt)',value='Nein' if exclusion!='(nicht vorhanden)' else '')
   empty_markers=st.text_input('Zusätzliche Leerwerte für B-Referenz',value='n/a,keine,null')
  else:ref='';exclusion='(nicht vorhanden)';exclusion_values='';empty_markers=''
  with st.expander('Spalten von CRM A und CRM B zuordnen',expanded=True):
   st.markdown('**IDs unbedingt als Text importieren. `Testfall` und `Soll_B_ID` sind nur Testhinweise.**')
   st.markdown('##### CRM A');ma=mapping_form(fa,'A');st.markdown('##### CRM B');mb=mapping_form(fb,'B')
  settings=splink_settings('new_splink') if engine=='Splink 5' else None
  threshold=st.slider('Mindestwert zum Anzeigen',0,100,60,5)
  saved=available_models(db) if engine=='Splink 5' else []
  chosen=st.selectbox('Bereits gespeichertes Splink-Modell (optional)',[None,*[j['model_id'] for j in saved]],format_func=lambda x: 'Neues Modell' if x is None else f'Modell #{x}') if saved else None
  if st.button('▶ CRM-Abgleich starten',type='primary'):
   pid=None
   try:
    with st.spinner('Import, Blocking und Zuordnung lokal durchführen …'):
     pid=import_project(db,title,fa,ma,fb,mb,engine='splink5' if engine=='Splink 5' else 'rapidfuzz',mode='dual' if dual else 'legacy',
      a_ref_column='' if not dual else ref,a_exclude_column='' if exclusion=='(nicht vorhanden)' else exclusion,
      exclude_values=[x.strip() for x in exclusion_values.split(',')],empty_reference_markers=[x.strip() for x in empty_markers.split(',')])
     if dual:run_reference_check(db,pid)
     if engine=='Splink 5':upper,kept=run_splink_matching(db,pid,config=settings,minimum_probability=max(.001,threshold/100),model_id=chosen)
     else:upper,kept=run_matching(db,pid,minimum=threshold)
    st.success(f'Projekt #{pid} erstellt: {upper:,} Kandidaten untersucht, {kept:,} Treffer gespeichert.')
    st.session_state['_next_nav']=f'#{pid} · {title}';st.rerun()
   except Exception as e:
    st.error(f'Abgleich fehlgeschlagen: {e}')
    if pid:st.warning(f'Projekt #{pid} wurde angelegt; fehlgeschlagenen Versuch im Projekt löschen oder prüfen.')
 else:st.caption('Beide CRM-Dateien wählen, um Spalten und Referenzen zuzuordnen.')
 st.stop()

pid=labels.get(selected)
if not pid:st.stop()
info=project_stats(db,pid)
if info.get('mode')=='internal':
 render_internal_project(db,pid,selected_model,bool(installed),enabled,db_path)
 st.stop()
st.subheader(f"#{pid} · {info.get('title',selected)}")
a,b,c,d=st.columns(4)
a.metric('CRM A',info.get('a_count',0));b.metric('CRM B',info.get('b_count',0));c.metric('Kandidaten',info.get('candidate_count',0));d.metric('Noch offen',info.get('pending',0))
if st.button('🎯 Fokusmodus für diesen Arbeitsbereich starten',type='primary'):
 st.session_state['_next_nav']='🎯 Fokusmodus';st.session_state['_focus_active']=True;st.rerun()

tabs=st.tabs(['Kandidaten','B-Referenzen','Exporte','Splink-Training','swisstopo: CRM A','Verwaltung'])
with tabs[0]:render_review(db_path,pid,info.get('engine')=='splink5',selected_model,bool(installed),ai_enabled=enabled)
with tabs[1]:
 if not is_dual(db,pid):st.info('Nur im Modus A→B verfügbar.')
 else:
  st.dataframe(pd.DataFrame(reference_overview(db,pid)),hide_index=True,use_container_width=True)
  st.caption('Detaillierte Einzelfälle inklusive Prüfnotizen im Fokusmodus (Prüfart: vorhandene Referenzen).')
  if st.button('Referenzprüfung neu berechnen'):
   run_reference_check(db,pid);st.rerun()
with tabs[2]:
 exports=[('Alle Dublettenkandidaten',lambda:export_pairs(db,pid)),
          ('Nur bestätigte A↔B-Zuordnungen',lambda:export_pairs(db,pid,True)),
          ('Nicht bestätigte A-Adressen',lambda:export_unmatched(db,pid,'A')),
          ('Nicht bestätigte B-Adressen',lambda:export_unmatched(db,pid,'B'))]
 if is_dual(db,pid):
  exports += [('B-Referenzbericht',lambda:export_references(db,pid)),
              ('Referenz-Felddifferenzen',lambda:export_reference_field_differences(db,pid)),
              ('Vollständigkeit aller A-Datensätze',lambda:export_workflow1_inventory(db,pid))]
 for i,(text,prepare) in enumerate(exports):
  key=f'export_{pid}_{i}'
  if st.button('CSV vorbereiten · '+text,key='prep_'+key):
   try:st.session_state[key]=prepare()
   except Exception as e:st.error(str(e))
  if key in st.session_state:st.download_button('⬇️ '+text,st.session_state[key],file_name=f'crm_{pid}_{i}.csv',mime='text/csv',key='dl_'+key)
with tabs[3]:
 if info.get('engine')!='splink5':st.info('Splink-Training nur für Splink-Projekte.')
 else:
  st.markdown('**Splink-Modelltraining aus mindestens 20 bestätigten Paaren**')
  cfg=splink_settings(f'train_{pid}')
  if st.button('Lokales Modell trainieren'):
   try:
    with st.spinner('Trainiere Splink lokal …'):
     model_id,stats=train_model_from_reviews(db,pid,config=cfg)
    st.success(f'Modell #{model_id} gespeichert.');st.json(stats)
   except Exception as e:st.error(str(e))
  saved=available_models(db)
  if saved:st.dataframe(pd.DataFrame([{'Modell':x['model_id'],'Projekt':x['project_id'],'Art':x['training_type']} for x in saved]),hide_index=True)
with tabs[4]:
 if not active_snapshot(db):st.warning('Zuerst unter swisstopo-Datenstand die öffentliche ZIP importieren.')
 else:
  if st.button('CRM-A-Adressen mit swisstopo prüfen'):
   try:validate_project(db,pid);st.success('Fertig.');st.rerun()
   except Exception as e:st.error(str(e))
  report=validation_counts(db,pid)
  if report:
   st.dataframe(pd.DataFrame(report),hide_index=True,use_container_width=True)
   st.download_button('CSV: amtlicher Adressstatus',export_validation(db,pid),file_name=f'crm_{pid}_swisstopo.csv')
with tabs[5]:
 st.warning('Löschen entfernt dieses Projekt und seine gespeicherten Entscheide, nicht den öffentlichen swisstopo-Referenzbestand.')
 if st.checkbox('Endgültig löschen',key=f'delete_{pid}'):
  if st.button('Projekt löschen',key=f'confirm_{pid}'):
   delete_project(db,pid);st.session_state['_next_nav']='+ Neuer Abgleich';st.rerun()
