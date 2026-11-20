"""Streamlit UI for local CRM cross-database duplicate review."""
import json
import hmac
from pathlib import Path
import os
from datetime import datetime

import streamlit as st

from engine import (FIELDS, LABELS, best_guess, connect, delete_project,
                    export_pairs, export_unmatched, import_project,
                    list_projects, page_pairs, project_stats, read_upload,
                    run_matching, save_llm, set_review, comparison_diagnostics)
from splink_matcher import run_splink_matching, available_models, train_model_from_reviews
from llm import MODEL, assess, models
from match_config import validate_config, BLOCK_LIMITS, model_signature
from review_ui import render_review

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
st.caption('GUI v1.6 · CRM A ↔ CRM B · RapidFuzz / Splink 5 · Hausnummern · Training · Ollama · ID-Export')
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



def splink_controls(key_prefix, default=None, disabled=False):
    """UI controls feed a strict whitelist; arbitrary SQL cannot be submitted."""
    c=validate_config(default)
    with st.expander('⚙️ Splink-5-Parameter und Blocking-Regeln',expanded=not disabled):
        split=st.checkbox('Strassenname und Hausnummer separat vergleichen',
                          value=c['split_house_number'],key=key_prefix+'_split',disabled=disabled)
        ca,cb=st.columns(2)
        with ca:
            strict=st.slider('Name: starke Ähnlichkeit',0.80,0.99,float(c['name_thresholds'][0]),0.01,
                             key=key_prefix+'_strict',disabled=disabled)
            short=st.slider('Name: weitere Ähnlichkeit',0.50,0.95,float(c['name_thresholds'][1]),0.01,
                             key=key_prefix+'_loose',disabled=disabled)
        with cb:
            dist1=st.number_input('Strasse: geringe Levenshtein-Distanz',1,7,int(c['street_distances'][0]),
                                  key=key_prefix+'_dist1',disabled=disabled)
            dist2=st.number_input('Strasse: weitere Levenshtein-Distanz',2,15,int(c['street_distances'][1]),
                                  key=key_prefix+'_dist2',disabled=disabled)
        st.caption('Blocking definiert, welche Paare überhaupt bewertet werden. Zu strenge Regeln können echte Dubletten ausschliessen.')
        blocks=st.multiselect('Blocking-Felder (mindestens 2)',list(BLOCK_LIMITS),default=c['blocking_fields'],
                              key=key_prefix+'_blocks',disabled=disabled)
        ca,cb=st.columns(2)
        with ca:
            u_pairs=st.select_slider('Zufallspaare für u-Schätzung',
                       options=[10000,50000,100000,300000,500000,1000000,2000000],
                       value=c['u_max_pairs'],key=key_prefix+'_u',disabled=disabled)
        with cb:
            em_pairs=st.select_slider('Max. EM-Trainingspaare je Regel',
                       options=[10000,50000,100000,250000,500000,1000000],
                       value=c['em_max_pairs'],key=key_prefix+'_em',disabled=disabled)
    return {**c,'split_house_number':split,'name_thresholds':[strict,short],
               'street_distances':[dist1,dist2],'blocking_fields':blocks,
               'u_max_pairs':u_pairs,'em_max_pairs':em_pairs}


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
        selected_pretrained_model = None
        cfg = None
        if engine == 'splink5':
            stored = available_models(db)
            model_labels = {'Neu trainieren (Standard / angepasste Parameter)': None}
            for m in stored:
                model_labels[f"Modell #{m['model_id']} · {m['title']} · {m['training_type']}"] = m
            selected_label = st.selectbox('Modelltraining',list(model_labels),
                help='Gespeicherte Modelle stammen ausschliesslich aus lokalen Projekten und liegen im persistenten Docker-Volume.')
            selected_pretrained_model = model_labels[selected_label]
            if selected_pretrained_model:
                cfg = validate_config(json.loads(selected_pretrained_model['config_json']))
                st.info('Gespeichertes Modell: Vergleichsregeln sind festgelegt. Es wird kein neues EM-Training durchgeführt.')
                st.json({k:v for k,v in cfg.items() if k!='min_probability'},expanded=False)
            else:
                cfg = splink_controls('import')
        with st.form('mapping_form'):
            title = st.text_input('Projektname',value=f'CRM-Abgleich {datetime.now():%Y-%m-%d}')
            ca,cb = st.columns(2)
            with ca: mapping_a = map_form(frame_a,'A')
            with cb: mapping_b = map_form(frame_b,'B')
            if engine == 'splink5':
                min_probability = st.slider('Minimale Splink-Matchwahrscheinlichkeit (nur Kandidatenfilter)',
                                            0.01, 0.99, 0.05, 0.01,
                                            help='0.05 ist ein bewusst niedriger Prüf-Schwellwert. Es ist keine automatische Freigabe.')
                st.caption('Splink 5 vergleicht nur CRM A mit CRM B. Wahrscheinlichkeiten hängen vom trainierten Modell ab und sind nicht extern kalibriert.')
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
                            cfg = validate_config(cfg)
                            cfg['min_probability'] = min_probability
                            paircount, kept = run_splink_matching(db,project_id,
                                minimum_probability=min_probability, config=cfg,
                                model_id=selected_pretrained_model['model_id'] if selected_pretrained_model else None,
                                progress=lambda message: box.caption(message))
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
review, training_tab, exports, manage = st.tabs(['✅ Prüfen', '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])

with review:
    render_review(db, pid, is_splink, selected_model, bool(installed))


with training_tab:
    if not is_splink:
        st.info('Training ist nur für Splink-Projekte verfügbar. RapidFuzz hat keine statistischen Modellparameter.')
    else:
        st.markdown('### Modell mit geprüften Beispielen nachtrainieren')
        st.write('Bestätigte Paare werden als **positive Trainingsbeispiele** verwendet. Abgelehnte Paare bleiben ausschliesslich zur Evaluation zurückgehalten. Die bestehende Trefferliste und alle Prüfentscheide werden nicht verändert.')
        reviewed=db.execute("""SELECT state,count(*) AS n FROM pairs WHERE project_id=? AND state IN ('bestaetigt','abgelehnt') GROUP BY state""",(pid,)).fetchall()
        counts={row['state']:row['n'] for row in reviewed}
        positive=counts.get('bestaetigt',0)
        rejected=counts.get('abgelehnt',0)
        c1,c2=st.columns(2)
        c1.metric('Manuell bestätigte Paare',positive)
        c2.metric('Manuell abgelehnte Paare',rejected)
        st.caption('Mindestens 20 bestätigte Paare erforderlich. 20 % werden nicht trainiert und dienen als Prüfstichprobe. Diese ist nicht repräsentativ für unbekannte Paare.')
        cfg_current = validate_config(model_info.get('config'))
        train_cfg=splink_controls('retrain_'+str(pid),default=cfg_current)
        train_cfg['min_probability']=st.slider('Schwelle für den Prüfstichproben-Test',0.01,0.99,
                     float(cfg_current.get('min_probability',0.05)),0.01,key='validate_threshold_'+str(pid))
        if st.button('🧠 Neues Splink-Modell aus bestätigten Paaren trainieren',
                     disabled=positive<20,type='primary'):
            try:
                with st.spinner('Splink trainiert ausschliesslich lokal …'):
                    status=st.empty()
                    model_id,metrics=train_model_from_reviews(db,pid,train_cfg,
                           progress=lambda message: status.caption(message))
                st.success(f'Modell #{model_id} gespeichert. Es steht beim nächsten Import zur Auswahl.')
                st.json(metrics,expanded=True)
            except Exception as exc:
                st.error(f'Training fehlgeschlagen: {exc}')
        models_here=[m for m in available_models(db) if m['project_id']==pid]
        if models_here:
            st.markdown('### Gespeicherte Modellversionen')
            for m in models_here:
                with st.expander(f"Modell #{m['model_id']} · {m['created_at']} · {m['training_type']}"):
                    st.json(json.loads(m['statistics_json']),expanded=False)
                    path=Path(m['model_path'])
                    if path.is_file():
                        st.download_button('Modell-JSON zur lokalen Archivierung herunterladen',
                           data=path.read_bytes(),file_name=f'crm_splink_modell_{m["model_id"]}.json',
                           mime='application/json',key=f'model_export_{m["model_id"]}')
                    st.caption('Das Modell ist lokal unter /app/data/models gespeichert und kann bei neuen Abgleichen wiederverwendet werden.')

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
