"""Streamlit UI for local CRM cross-database duplicate review."""
import json
import hmac
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from engine import (FIELDS, LABELS, best_guess, connect, delete_project,
                    export_pairs, export_unmatched, import_project,
                    list_projects, page_pairs, project_stats, read_upload,
                    run_matching, save_llm, set_review)
from splink_matcher import run_splink_matching
from llm import MODEL, assess, models

st.set_page_config(page_title=os.environ.get('APP_TITLE', 'CRM Dubletten – lokal'), page_icon='🔎',
                   layout='wide', initial_sidebar_state='expanded')
st.markdown('''<style>
.block-container {padding-top:1.7rem; max-width:1500px}
[data-testid="stMetric"] {border:1px solid #dde2e6;border-radius:10px;padding:10px}
</style>''', unsafe_allow_html=True)
PASSWORD = os.environ.get('APP_PASSWORD', '')
if not PASSWORD:
    st.error('APP_PASSWORD fehlt. Bitte in Portainer eine starke Umgebungsvariable setzen.')
    st.stop()
if not st.session_state.get('authenticated', False):
    st.title('🔐 CRM-Dublettenprüfung – Anmeldung')
    with st.form('login'):
        entered = st.text_input('Passwort', type='password')
        submitted = st.form_submit_button('Anmelden')
    if submitted:
        if hmac.compare_digest(entered, PASSWORD):
            st.session_state['authenticated'] = True
            st.rerun()
        else:
            st.error('Passwort ungültig.')
    st.stop()
DB_PATH = os.environ.get('DB_PATH','/app/data/crm.sqlite')
db = connect(DB_PATH)

st.title('🔎 CRM-Dublettenprüfung')
st.caption('Zwei CRM-Datenbestände lokal vergleichen • RapidFuzz oder Splink 5 • manuelle Prüfung • ID-Export')
st.info('🔒 CRM-Daten bleiben auf diesem Server. Die optionale KI verwendet nur das konfigurierte lokale Host-Ollama. Keine Cloud-API in der Anwendung.')

with st.sidebar:
    if st.button('🔒 Abmelden'):
        st.session_state['authenticated'] = False
        st.rerun()
    st.header('Arbeitsbereich')
    projects = list_projects(db)
    available = {f"#{p['project_id']} · {p['title']}":p['project_id'] for p in projects}
    if available:
        choices = ['+ Neuer Abgleich'] + list(available)
        selected = st.selectbox('Projekt', choices, index=0 if st.session_state.get('new_project') else 1)
        pid = available.get(selected)
    else:
        pid = None
    st.divider()
    installed = models()
    st.write('**Lokale KI**')
    if installed:
        st.success('Ollama erreichbar')
        selected_model = st.selectbox('Installiertes Modell', installed,
                         index=installed.index(MODEL) if MODEL in installed else 0)
    else:
        st.caption('Kein Modell erreichbar. Dublettenprüfung funktioniert ohne KI.')
        selected_model = MODEL
    st.divider()
    st.caption('Heuristische Scores sind KEINE Wahrscheinlichkeiten. Keine Treffer werden automatisch freigegeben.')


def map_form(frame, side):
    opts = ['(nicht vorhanden)'] + list(frame.columns)
    st.markdown(f'**CRM {side}: {len(frame):,} Datensätze, {len(frame.columns)} Spalten**')
    mapping = {}
    for field in FIELDS:
        guess = best_guess(frame.columns, field)
        mapping[field] = st.selectbox(LABELS[field],opts,
                 index=opts.index(guess) if guess in opts else 0,
                 key=f'map_{side}_{field}', help='Keine Zuordnung: Feld wird beim Matching nicht verwendet')
        if mapping[field] == '(nicht vorhanden)':
            mapping[field] = ''
    return mapping


if pid is None:
    st.subheader('1 · Zwei CRM-Dateien importieren')
    st.write('CSV (Komma oder Semikolon) und XLSX werden unterstützt. Jede Datei muss eine **eindeutige, nicht leere CRM-ID** besitzen. IDs mit führenden Nullen bleiben Text.')
    col_a,col_b = st.columns(2)
    with col_a:
        file_a = st.file_uploader('Export aus CRM A',type=['csv','xlsx'],key='upload_A')
    with col_b:
        file_b = st.file_uploader('Export aus CRM B',type=['csv','xlsx'],key='upload_B')
    if file_a and file_b:
        try:
            frame_a = read_upload(file_a)
            frame_b = read_upload(file_b)
        except Exception as exc:
            st.error(f'Import nicht möglich: {exc}')
            st.stop()
        st.subheader('2 · Felder zuordnen')
        st.caption('Felder dürfen in CRM A und CRM B unterschiedlich heissen. Firma ODER Vor-/Nachname reicht für Namensvergleich.')
        # Outside the form so users can see the correct threshold controls
        # immediately after selecting a matching engine.
        engine_label = st.radio('Matching-Verfahren',
             ['Splink 5 (statistisches Record Linkage)', 'RapidFuzz (bisherige Regeln)'],
             horizontal=True, help='Splink 5 trainiert statistische Parameter ab ausreichend grossen Datenmengen. Kleine Testmengen nutzen offen gekennzeichnete Startparameter.')
        engine = 'splink5' if engine_label.startswith('Splink') else 'rapidfuzz'
        with st.form('mapping_form'):
            title = st.text_input('Projektname',value=f'CRM-Abgleich {datetime.now():%Y-%m-%d}')
            ca,cb = st.columns(2)
            with ca: mapping_a = map_form(frame_a,'A')
            with cb: mapping_b = map_form(frame_b,'B')
            if engine == 'splink5':
                min_probability = st.slider('Minimale Splink-Matchwahrscheinlichkeit (nur Kandidatenfilter)',
                                            0.01, 0.99, 0.05, 0.01,
                                            help='0.05 ist ein bewusst niedriger Prüf-Schwellwert. Es ist keine automatische Freigabe.')
                st.caption('Splink 5 vergleicht nur CRM A mit CRM B. Die angezeigte Wahrscheinlichkeit ist modellabhängig und nicht unabhängig kalibriert.')
            else:
                min_score = st.slider('Mindestwert für Kandidaten (RapidFuzz, heuristisch)',
                                      45,95,60,5)
            submitted = st.form_submit_button('Importieren und Abgleich starten',type='primary')
        if submitted:
            try:
                with st.spinner('Importiere und vergleiche die Daten lokal …'):
                    project_id = import_project(db,title,frame_a,mapping_a,frame_b,mapping_b,engine=engine)
                    box = st.empty()
                    try:
                        if engine == 'splink5':
                            paircount, kept = run_splink_matching(db,project_id,
                                minimum_probability=min_probability, progress=lambda message: box.caption(message))
                        else:
                            paircount, kept = run_matching(db,project_id,minimum=min_score,
                                                            progress=lambda message: box.caption(message))
                    except Exception:
                        delete_project(db,project_id)
                        raise
                    box.empty()
                st.success(f'{paircount:,} Vergleichspaare geprüft, {kept:,} Kandidaten zur Sichtung gespeichert.')
                st.session_state['new_project'] = False
                st.rerun()
            except Exception as exc:
                st.error(f'Abgleich fehlgeschlagen: {exc}')
    else:
        st.caption('Beide Dateien auswählen, anschliessend die Spalten zuordnen.')
    st.stop()

stats = project_stats(db,pid)
st.subheader(stats['title'])
is_splink = stats['engine'] == 'splink5'
st.caption(f"Erstellt am {stats['created_at'][:10]} · Projekt #{pid} · Engine: {'Splink 5' if is_splink else 'RapidFuzz'}")
if is_splink:
    try:
        model_info = json.loads(stats.get('match_model_info') or '{}')
    except (ValueError, TypeError):
        model_info = {}
    if model_info.get('mode') == 'u_sampling_em_training':
        st.info('Splink 5: Modellparameter via Zufallsstichprobe (u) und EM (m) geschätzt. Werte sind **keine unabhängig validierten Genauigkeitsangaben**.')
    elif model_info.get('mode') == 'feste_priors_kleine_stichprobe':
        st.warning('Splink 5: Zu kleine Vergleichsmenge für EM-Training. Verwendet feste Beispielparameter; Wahrscheinlichkeitswerte **nicht kalibriert**. Nur zum Funktionstest.')
    else:
        st.caption('Splink 5: ' + str(model_info.get('mode', 'Modellstatus nicht vorhanden')))
a,b,c,d = st.columns(4)
a.metric('Datensätze CRM A',f"{stats['a_count']:,}")
b.metric('Datensätze CRM B',f"{stats['b_count']:,}")
c.metric('Trefferkandidaten',f"{stats['candidate_count']:,}")
d.metric('Bestätigte Zuordnungen',stats['accepted'])
review, exports, manage = st.tabs(['✅ Prüfen', '📥 Exporte', '⚙️ Verwaltung'])

with review:
    st.markdown('### Treffer vergleichen')
    filter_c,score_c = st.columns(2)
    with filter_c:
        state = st.selectbox('Prüfstatus',['offen','unklar','bestaetigt','abgelehnt','Alle'],
                             format_func=lambda s: {'offen':'Offen','unklar':'Unklar',
                                 'bestaetigt':'Bestätigt','abgelehnt':'Abgelehnt'}.get(s,s))
    with score_c:
        minimum = st.slider('Mindest-Score',0,100,5 if is_splink else 60,5)
    count,_ = page_pairs(db,pid,state=state,min_score=minimum,limit=1)
    st.caption(f'{count:,} Einträge mit diesem Filter · ' + ('Score = 100 × modellbasierte Splink-Matchwahrscheinlichkeit (keine validierte Trefferquote)' if is_splink else 'Score ist eine RapidFuzz-Heuristik, keine Trefferwahrscheinlichkeit'))
    if installed:
        with st.expander('🤖 Optional: Grenzfälle stapelweise durch lokale KI vorprüfen'):
            st.caption('Nur offene Kandidaten im gewählten Score-Bereich und ohne KI-Vorschlag. Keine automatische Freigabe!')
            k_lo,k_hi = st.columns(2)
            with k_lo:
                llm_min = st.number_input('KI-Score von', 0, 100, 40 if is_splink else 65, 1)
            with k_hi:
                llm_max = st.number_input('KI-Score bis', 0, 100, 95 if is_splink else 87, 1)
            batch_size = st.slider('Maximale Anzahl',1,25,10,1)
            if st.button('Lokale KI-Vorprüfung starten',key=f'batch_llm_{pid}'):
                if llm_min > llm_max:
                    st.error('KI-Score von darf nicht grösser als KI-Score bis sein.')
                    st.stop()
                jobs = db.execute('''SELECT p.a_pk,p.b_pk,a.original_json aj,b.original_json bj
                   FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
                   WHERE p.project_id=? AND p.state='offen' AND p.score BETWEEN ? AND ?
                     AND p.llm_decision='' ORDER BY p.score DESC LIMIT ?''',(pid,llm_min,llm_max,batch_size)).fetchall()
                if not jobs:
                    st.info('Keine passenden offenen Grenzfälle ohne KI-Vorschlag vorhanden.')
                else:
                    bar = st.progress(0.0)
                    for i,job in enumerate(jobs):
                        try:
                            decision,why = assess(json.loads(job['aj']),json.loads(job['bj']),selected_model)
                            save_llm(db,pid,job['a_pk'],job['b_pk'],decision,why,selected_model)
                            bar.progress((i+1)/len(jobs))
                        except Exception as exc:
                            st.error(f'Nach {i} erfolgreichen Auswertungen abgebrochen: {exc}')
                            break
                    else:
                        st.success(f'{len(jobs)} Vorschläge gespeichert. Bitte manuell prüfen.')
    if count:
        pages = (count+19)//20
        page = st.number_input('Seite',min_value=1,max_value=pages,value=1,step=1)
        _, items = page_pairs(db,pid,state=state,min_score=minimum,offset=(page-1)*20,limit=20)
        for pair in items:
            ra=json.loads(pair['a_json']); rb=json.loads(pair['b_json'])
            title=f"{pair['score']:3d}/100 {'(Splink)' if is_splink else '(RapidFuzz)'} · {ra.get('company') or (ra.get('first_name','')+' '+ra.get('last_name','')).strip()} ↔ {rb.get('company') or (rb.get('first_name','')+' '+rb.get('last_name','')).strip()} · {pair['state']}"
            with st.expander(title):
                l,r=st.columns(2)
                for col,raw,source in [(l,ra,'A'),(r,rb,'B')]:
                    with col:
                        st.markdown(f'**CRM {source} · ID `{pair[source.lower()+"_id"]}`**')
                        st.table([{'Feld':LABELS[f], 'Wert':raw.get(f,'')} for f in FIELDS if f != 'id' and raw.get(f)])
                if is_splink and pair['match_probability'] is not None:
                    st.caption(f"**Splink-Modellwert:** {pair['match_probability']:.5f} · nur zur Priorisierung, keine Garantie")
                st.caption(f"**Erkannte Merkmale:** {pair['reason']}")
                if pair['llm_decision']:
                    st.info(f"Lokale KI: **{pair['llm_decision']}** · {pair['llm_reason']} ({pair['llm_model']})")
                k1,k2,k3,k4 = st.columns(4)
                opts = [('bestaetigt','✅ Bestätigen'),('abgelehnt','❌ Ablehnen'),('unklar','❓ Unklar'),('offen','↩️ Zurücksetzen')]
                for col,(new_state,lab) in zip((k1,k2,k3,k4),opts):
                    with col:
                        if st.button(lab,key=f'{pid}_{pair["a_pk"]}_{pair["b_pk"]}_{new_state}',use_container_width=True):
                            try:
                                set_review(db,pid,pair['a_pk'],pair['b_pk'],new_state)
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
                if installed and st.button('🤖 Nur dieses Paar lokal mit Ollama prüfen',
                                           key=f'llm_{pid}_{pair["a_pk"]}_{pair["b_pk"]}'):
                    with st.spinner('Lokales LLM bewertet den Grenzfall …'):
                        try:
                            dec,why=assess(ra,rb,selected_model)
                            save_llm(db,pid,pair['a_pk'],pair['b_pk'],dec,why,selected_model)
                            st.rerun()
                        except Exception as exc:
                            st.error(f'KI-Prüfung nicht möglich: {exc}')
    else:
        st.write('Keine Treffer für diese Filterauswahl.')

with exports:
    st.markdown('### Export für die CRM-Migration')
    st.warning('Nur **manuell bestätigte** Paare gehören in die Freigabeliste. Jeder Originaldatensatz kann höchstens einmal bestätigt werden. Ein hoher Splink-Score ist kein Identitätsbeweis.')
    st.download_button('⬇️ Bestätigte ID-Zuordnungen (.csv)',export_pairs(db,pid,True),
                       file_name=f'crm_{pid}_freigegebene_zuordnungen.csv',mime='text/csv',type='primary')
    st.download_button('⬇️ Alle Kandidaten inkl. Prüfstatus (.csv)',export_pairs(db,pid,False),
                       file_name=f'crm_{pid}_alle_kandidaten.csv',mime='text/csv')
    lc,rc=st.columns(2)
    with lc:
        st.download_button('⬇️ Nicht zugeordnete CRM-A-Datensätze (.csv)',export_unmatched(db,pid,'A'),
                           file_name=f'crm_{pid}_ohne_freigabe_A.csv',mime='text/csv')
    with rc:
        st.download_button('⬇️ Nicht zugeordnete CRM-B-Datensätze (.csv)',export_unmatched(db,pid,'B'),
                           file_name=f'crm_{pid}_ohne_freigabe_B.csv',mime='text/csv')
    st.caption('„Nicht zugeordnet“ bedeutet: keine bestätigte Verknüpfung. Es kann trotzdem ein offener Trefferkandidat existieren.')

with manage:
    st.markdown('### Projektverwaltung')
    st.write('Die Daten liegen im lokalen Host-Verzeichnis `/home/buebi/docker/crm-dubletten/data`. Es erfolgen keine automatischen Cloud-Backups.')
    if st.checkbox('Ich möchte dieses Projekt mit allen Datensätzen und Prüfentscheiden löschen.'):
        if st.button('Projekt endgültig löschen',type='primary'):
            delete_project(db,pid)
            st.session_state['new_project']=True
            st.rerun()
