"""Single-file CRM duplicate review views and a compact import workflow."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from engine import FIELDS,LABELS,read_upload,run_matching,project_stats,delete_project
from internal_dupes import (create_internal_project,group_overview,export_internal_groups,
                            export_internal_inventory,export_internal_pairs)
from review_ui import render_review
from swisstopo import active_snapshot,validate_project,validation_counts,export_validation
from splink_matcher import run_splink_matching


def render_create(db,mapping_form,splink_settings):
    st.subheader('Interne Dublettenprüfung – ein CRM-Export')
    st.caption('Prüfung nur innerhalb dieser Datei · keine automatischen Löschungen oder Zusammenführungen')
    upload=st.file_uploader('CSV- oder Excel-Export',type=['csv','xlsx'],key='internal_upload')
    if upload is None:
        st.info('Einen einzelnen CRM-Export laden. Danach Felder zuordnen und Dubletten suchen.')
        return
    try:
        frame=read_upload(upload)
    except Exception as exc:
        st.error(str(exc));return
    a,b,c=st.columns([3,2,2])
    with a:title=st.text_input('Prüfauftrag',value='Interne CRM-Dublettenprüfung',key='internal_title')
    with b:engine=st.radio('Abgleich',['RapidFuzz','Splink 5'],horizontal=True,key='internal_engine')
    with c:threshold=st.slider('Mindest-Score',0,100,65,5,key='internal_score')
    with st.expander('Spalten einmalig zuordnen',expanded=True):
        st.caption('Eine eindeutige Quell-ID ist erforderlich; reine Test-/Soll-Felder bitte nicht fürs Matching verwenden.')
        mapping=mapping_form(frame,'Intern')
    settings=splink_settings('internal_splink') if engine=='Splink 5' else None
    if st.button('▶ Interne Dubletten suchen',type='primary',key='internal_run'):
        pid=None
        try:
            with st.spinner('Datensätze einmal importieren und lokale Dublettenkandidaten bilden …'):
                pid=create_internal_project(db,title,frame,mapping,engine='splink5' if engine=='Splink 5' else 'rapidfuzz')
                if engine=='Splink 5':
                    checked,found=run_splink_matching(db,pid,config=settings,minimum_probability=max(.001,threshold/100))
                else:
                    checked,found=run_matching(db,pid,minimum=threshold)
            st.success(f'{len(frame):,} Originaldatensätze · {checked:,} Paare geprüft · {found:,} Kandidaten gefunden')
            st.session_state['_next_nav']=f'#{pid} · {title[:140]}'
            st.rerun()
        except Exception as exc:
            st.error(f'Interne Prüfung fehlgeschlagen: {exc}')
            if pid:
                st.warning(f'Prüfprojekt #{pid} wurde erstellt. Du kannst es im Projektbereich löschen oder kontrollieren.')


def render_project(db,pid,selected_model,installed,ai_enabled):
    info=project_stats(db,pid)
    st.subheader(f"#{pid} · {info['title']} · Interne Dublettenprüfung")
    a,b,c=st.columns(3)
    a.metric('Originaldatensätze',info['a_count'])
    b.metric('Kandidatenpaare',info['candidate_count'])
    c.metric('Noch offen',info['pending'])
    if st.button('🎯 Fokusmodus starten',type='primary',key=f'internal_focus_{pid}'):
        from focus_ui import MODES
        st.session_state['focus_mode']=MODES[3]
        st.session_state['_focus_route_internal']=pid
        st.session_state['_next_nav']='🎯 Fokusmodus'
        st.rerun()
    tabs=st.tabs(['Kandidaten','Bestätigte Gruppen','Exporte','swisstopo','Verwaltung'])
    with tabs[0]:
        st.caption('Farben zeigen Feldabweichungen. Fachentscheide bestätigen nur das jeweilige Paar.')
        render_review(db,pid,info['engine']=='splink5',selected_model,installed,ai_enabled=ai_enabled)
    with tabs[1]:
        st.caption('Gruppen werden ausschliesslich aus **manuell bestätigten Paaren** gebildet. Ein indirekter Zusammenhang ist kein Nachweis, dass alle Gruppenmitglieder identisch sind.')
        groups=group_overview(db,pid)
        if groups:
            st.dataframe(pd.DataFrame(groups),use_container_width=True,hide_index=True,height=min(360,80+len(groups)*36))
        else:st.info('Noch keine bestätigten Dublettengruppen.')
    with tabs[2]:
        st.caption('CSV mit Original-IDs; Originaldaten bleiben erhalten. Erst nach Klick wird der Export erzeugt.')
        exports=[('Alle Kandidaten und Entscheide',export_internal_pairs,False,'paare'),
                 ('Nur bestätigte Paare',export_internal_pairs,True,'bestaetigt'),
                 ('Gruppen mit Original-IDs',export_internal_groups,None,'gruppen'),
                 ('Vollständiger Originalexport mit Prüfspalten',export_internal_inventory,None,'bereinigung')]
        for i,(title,fn,opt,suffix) in enumerate(exports):
            key=f'iexport_{pid}_{i}'
            if st.button('CSV vorbereiten · '+title,key=f'iprep_{pid}_{i}'):
                try:st.session_state[key]=fn(db,pid,opt) if opt is not None else fn(db,pid)
                except Exception as exc:st.error(str(exc))
            if key in st.session_state:
                st.download_button('⬇️ '+title,st.session_state[key],file_name=f'intern_{pid}_{suffix}.csv',mime='text/csv',key=f'idownload_{pid}_{i}')
    with tabs[3]:
        if not active_snapshot(db):st.info('Zuerst einen amtlichen swisstopo-Datenstand importieren.')
        else:
            if st.button('Originaladressen des Exports prüfen',key=f'isw_{pid}'):
                try:validate_project(db,pid);st.success('Adressprüfung abgeschlossen.')
                except Exception as exc:st.error(str(exc))
            report=validation_counts(db,pid)
            if report:
                st.dataframe(pd.DataFrame(report),use_container_width=True,hide_index=True)
                st.download_button('Amtlichen Adressstatus exportieren',export_validation(db,pid),file_name=f'intern_{pid}_swisstopo.csv')
    with tabs[4]:
        st.warning('Löschen entfernt dieses lokale Prüfprojekt samt Entscheiden. Originaldatei und andere Projekte bleiben unverändert.')
        if st.checkbox('Projekt endgültig löschen',key=f'idel_{pid}'):
            if st.button('Projekt löschen',key=f'idelyes_{pid}'):
                delete_project(db,pid)
                st.session_state['_next_nav']='➕ Interne Dublettenprüfung'
                st.rerun()
