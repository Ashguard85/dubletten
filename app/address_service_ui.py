"""Standalone Swiss-address upload, review and export screen."""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
import streamlit as st
from address_service import (ensure_schema,read_single_upload,guess_column,import_job,list_jobs,
 job_counts,page_rows,save_review,export_job,delete_job,bulk_approval_preview,bulk_approve_exact,
 MASS_APPROVED)
from swisstopo import active_snapshot,RESULT_LABELS

def render(db):
 ensure_schema(db)
 st.subheader('📬 Schweizer Adressprüfung')
 snap=active_snapshot(db)
 if not snap:
  st.warning('Zuerst den öffentlichen swisstopo-Datenstand im Menü importieren.');return
 st.caption(f"Referenzstand {snap['reference_date']} · {snap['address_count']:,} Einträge · alles lokal")
 import_tab,review_tab,export_tab,manage_tab=st.tabs(['1 · Einlesen','2 · Prüfen','3 · Export','4 · Aufträge'])
 jobs=list_jobs(db)
 with import_tab:
  st.caption('Eine CSV/XLSX-Liste · max. 60 MB · 150’000 Zeilen. Die swisstopo-ZIP wird separat geladen.')
  upload=st.file_uploader('Adressliste',type=['csv','xlsx'],key='address_single')
  if upload:
   try:frame=read_single_upload(upload)
   except Exception as e:st.error(str(e));frame=None
   if frame is not None:
    st.dataframe(frame.head(5),hide_index=True,use_container_width=True)
    title=st.text_input('Prüfauftrag',value=Path(upload.name).stem)
    mapping={};choices=['(nicht vorhanden)',*frame.columns]
    cols=st.columns(3)
    labels={'id':'Eigene ID','street':'Strasse / Adresse *','house_number':'Hausnummer separat',
      'postcode':'PLZ *','city':'Ort','country':'Land'}
    for i,(k,label) in enumerate(labels.items()):
     guess=guess_column(frame.columns,k)
     with cols[i%3]:
      pick=st.selectbox(label,choices,index=choices.index(guess) if guess else 0,key=f'amap_{k}')
      mapping[k]=pick if pick!='(nicht vorhanden)' else ''
    if st.button('🔍 Lokal prüfen',type='primary',disabled=not mapping['street'] or not mapping['postcode']):
     try:
      with st.spinner('Adressbestand wird lokal geprüft …'):
       jid=import_job(db,frame,title,upload.name,mapping)
      st.session_state['address_service_selected_job_id']=jid
      st.success(f'{len(frame):,} Adressen geprüft – Reiter 2 oder Fokusmodus öffnen.')
      st.rerun()
     except Exception as exc:st.error(str(exc))
 with review_tab:
  if not jobs:st.info('Zuerst eine Adressliste importieren.')
  else:
   names={j['job_id']:f"#{j['job_id']} · {j['title']} · Stand {j['snapshot_date']}" for j in jobs}
   jid=st.selectbox('Prüfauftrag',list(names),format_func=lambda j:names[j],
       index=next((i for i,j in enumerate(jobs) if j['job_id']==st.session_state.get('address_service_selected_job_id')),0),
       key='address_job_pick')
   stats=job_counts(db,jid)
   st.caption(' · '.join(f"{x['decision']}: {x['n']}" for x in stats[:8]))
   if st.button('🎯 Fokusmodus für die Adressprüfung starten',type='primary',key='address_launch_focus'):
    st.session_state['_focus_route_swiss']=jid
    st.session_state['_focus_active']=True
    st.session_state['_next_nav']='🎯 Fokusmodus'
    st.rerun()
   with st.expander('Exakte amtliche Adressen massenfreigeben',expanded=False):
    key=f'addr_preview_{jid}'
    if st.button('Vorschau berechnen',key=f'addr_pre_button_{jid}'):
     st.session_state[key]=bulk_approval_preview(db,jid)
    pv=st.session_state.get(key)
    if pv:
     st.write(f"**{pv['count']}** eindeutige, aktuell amtliche Vier-Felder-Treffer")
     if pv['examples']:st.dataframe(pd.DataFrame(pv['examples']),hide_index=True,use_container_width=True)
     checked=st.checkbox('Die streng geprüfte Freigabeliste bestätigen',key=f'addr_bulk_confirm_{jid}')
     if st.button('Massenfreigabe ausführen',disabled=not checked or not pv['count'],key=f'addr_bulk_exec_{jid}'):
      try:
       count=bulk_approve_exact(db,jid,pv['count'],pv['digest']);st.session_state.pop(key,None)
       st.success(f'{count} Adressen geprüft und freigegeben.');st.rerun()
      except ValueError as e:st.session_state.pop(key,None);st.error(str(e))
   st.caption('Für die schnelle Einzelkontrolle den Fokusmodus verwenden; vollständige Rohfelder bleiben über den Export erhalten.')
 with export_tab:
  if not jobs:st.info('Noch kein Prüfauftrag.')
  else:
   jid=st.selectbox('Prüfauftrag', [j['job_id'] for j in jobs],format_func=lambda x:next(j['title'] for j in jobs if j['job_id']==x),key='addr_export_job')
   split=st.checkbox('Strasse und Hausnummer in zwei eigene Spalten exportieren',value=True,key='addr_export_split')
   left,right=st.columns(2)
   street=left.text_input('Ziel-Strassenspalte','Ziel_Strasse')
   house=right.text_input('Ziel-Hausnummernspalte','Ziel_Hausnummer')
   kinds=[('Alle geprüften Adressen',None,None),('Nur bestätigte Korrekturen',True,None),('Nur amtlich massenfreigegebene',None,True)]
   for title,changed,bulk in kinds:
    if st.button('CSV vorbereiten: '+title,key=f'exp_prepare_{jid}_{title}'):
     try:st.session_state[f'exp_{jid}_{title}']=export_job(db,jid,only_changed=bool(changed),only_bulk=bool(bulk),split_house=split,street_column=street,house_column=house)
     except ValueError as e:st.error(str(e))
    if f'exp_{jid}_{title}' in st.session_state:
     st.download_button('⬇️ '+title,st.session_state[f'exp_{jid}_{title}'],file_name=f'adresspruefung_{jid}_{title[:5].lower()}.csv',mime='text/csv',key=f'exp_dl_{jid}_{title}')
   st.warning('swisstopo bestätigt nur Gebäudeadressen – nicht, ob Personen oder Firmen dort wohnen.')
 with manage_tab:
  if jobs:
   jid=st.selectbox('Prüfauftrag löschen',[j['job_id'] for j in jobs],key='address_delete_job')
   if st.checkbox('Auftrag samt Ergebnissen dauerhaft löschen',key='addr_del_confirm'):
    if st.button('Endgültig löschen',key='addr_del_final'):
     delete_job(db,jid);st.rerun()
