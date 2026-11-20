"""Classic CRM review views (all use the same persisted pair states)."""
from __future__ import annotations
import json
import streamlit as st
import pandas as pd
from review_data import pair_page,RISKS
from engine import set_review,save_llm,comparison_diagnostics
from ai_control import is_enabled as ai_is_enabled
from llm import assess

@st.fragment
def render_review(db,pid,is_splink=False,selected_model='',installed=False,ai_enabled=False):
 st.subheader('CRM-Kandidaten prüfen')
 a,b,c,d=st.columns(4)
 with a:state=st.selectbox('Entscheid',['offen','unklar','Alle','bestaetigt','abgelehnt'],key=f'rstate_{pid}')
 with b:score=st.slider('Score ≥',0,100,55,key=f'rscore_{pid}')
 with c:risk=st.selectbox('Risiko',RISKS,key=f'rrisk_{pid}')
 with d:search=st.text_input('Suche',key=f'rsearch_{pid}')
 view=st.segmented_control('Ansicht',['Data Grid','Split-View','Review-Cockpit'],default='Split-View',key=f'rview_{pid}')
 total,pages,_,rows=pair_page(db,pid,state,score,search,risk,1,25)
 st.caption(f'{total:,} Treffer · Status im gemeinsamen Prüfdatensatz gespeichert')
 if not rows:st.info('Keine Treffer im Filter.');return
 if view=='Review-Cockpit':
  stats=db.execute('SELECT state,count(*) n FROM pairs WHERE project_id=? GROUP BY state',(pid,)).fetchall()
  st.dataframe(pd.DataFrame([dict(x) for x in stats]),hide_index=True,use_container_width=True,height=170)
 if view=='Data Grid':
  st.dataframe(pd.DataFrame([{'A':r['a_id'],'B':r['b_id'],'Score':r['score'],'Entscheid':r['state']} for r in rows]),hide_index=True,use_container_width=True,height=250)
 idx=st.selectbox('Paar auswählen',range(len(rows)),format_func=lambda i: f"{rows[i]['a_id']} ↔ {rows[i]['b_id']} · {rows[i]['score']}/100 · {rows[i]['state']}",key=f'ridx_{pid}')
 row=rows[idx]
 left=json.loads(row['a_json']);right=json.loads(row['b_json'])
 show=['company','first_name','last_name','street','house_number','postcode','city','uid','email','phone']
 table=[{'Feld':k,'CRM A':left.get(k,''),'CRM B':right.get(k,''),'Gleich':left.get(k,'').casefold()==right.get(k,'').casefold()} for k in show if left.get(k) or right.get(k)]
 st.dataframe(pd.DataFrame(table),hide_index=True,use_container_width=True,height=270)
 ca,cb,cc,cd=st.columns(4)
 action=('bestaetigt' if ca.button('✓ Bestätigen',key=f'raccept_{pid}') else
         'abgelehnt' if cb.button('✕ Ablehnen',key=f'rreject_{pid}') else
         'unklar' if cc.button('? Unklar',key=f'runclear_{pid}') else
         'offen' if cd.button('↩ Zurücksetzen',key=f'rreset_{pid}') else None)
 if action:
  try:set_review(db,pid,row['a_pk'],row['b_pk'],action);st.rerun(scope='fragment')
  except ValueError as e:st.error(str(e))
 with st.expander('Weitere Vergleichsinformationen',expanded=False):
  st.caption(row['reason'])
  if row['llm_decision']:st.caption(f"Früherer KI-Vorschlag: {row['llm_decision']} – {row['llm_reason']}")
  if installed and ai_enabled and ai_is_enabled(db):
   if st.button('🤖 Dieses Paar lokal analysieren',key=f'rllm_{pid}'):
    try:
     if not ai_is_enabled(db):raise RuntimeError('KI deaktiviert.')
     decision,reason=assess(left,right,selected_model)
     save_llm(db,pid,row['a_pk'],row['b_pk'],decision,reason,selected_model)
     st.rerun(scope='fragment')
    except Exception as e:st.error(str(e))
 if installed and ai_enabled and ai_is_enabled(db):
  with st.expander('KI-Stapelprüfung (nur auf Knopfdruck)',expanded=False):
   n=st.slider('Maximal offene Fälle',1,25,5,key=f'rbatchsize_{pid}')
   if st.button('Stapelprüfung starten',key=f'rbatch_{pid}'):
    jobs=db.execute('''SELECT p.a_pk,p.b_pk,a.original_json a_json,b.original_json b_json
      FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
      WHERE p.project_id=? AND p.state='offen' LIMIT ?''',(pid,n)).fetchall()
    completed=0
    for j in jobs:
     if not ai_is_enabled(db):st.warning('KI deaktiviert – Stapelprüfung beendet.');break
     try:
      decision,reason=assess(json.loads(j['a_json']),json.loads(j['b_json']),selected_model)
      save_llm(db,pid,j['a_pk'],j['b_pk'],decision,reason,selected_model);completed+=1
     except Exception as e:st.error(str(e));break
    st.success(f'{completed} KI-Vorschläge gespeichert (keine automatische Freigabe).')
