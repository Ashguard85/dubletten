"""Streamlit UI for local CRM cross-database duplicate review."""
import json
import hmac
from pathlib import Path
import os
import shutil
import tempfile
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
from migration import (is_dual, run_reference_check, reference_overview, reference_page,
                       export_references, export_reference_field_differences,
                       export_workflow1_inventory, workflow1_inventory)
from address_service_ui import render as render_address_service
from swisstopo import (ensure_schema as ensure_sw_schema, active_snapshot, import_snapshot,
                      list_snapshots, validate_project, validation_counts, export_validation, inspect_source,
                      RESULT_LABELS)
import pandas as pd
import io


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
ensure_sw_schema(db)

st.title('🔎 CRM-Dublettenprüfung')
st.caption('GUI v1.8 · CRM A ↔ CRM B · RapidFuzz / Splink 5 · Hausnummern · Training · Ollama · ID-Export')
st.info('🔒 CRM-Daten bleiben auf diesem Server. Die optionale KI verwendet nur das konfigurierte lokale Host-Ollama. Keine Cloud-API in der Anwendung.')

with st.sidebar:
    if st.button('🔒 Abmelden'):
        st.session_state['authenticated'] = False
        st.rerun()
    st.header('Arbeitsbereich')
    projects = list_projects(db)
    available = {f"#{p['project_id']} · {p['title']}":p['project_id'] for p in projects}
    choices = ['+ Neuer Abgleich', '🏔️ swisstopo-Datenstand', '📬 Schweizer Adressprüfung'] + list(available)
    selected = st.selectbox('Projekt / Datenstand',choices,
                            index=0 if st.session_state.get('new_project') or not available else 3)
    pid = available.get(selected)
    show_swisstopo = selected == '🏔️ swisstopo-Datenstand'
    show_address_service = selected == '📬 Schweizer Adressprüfung'
    st.divider()
    installed = [] if show_address_service else models()
    st.write('**Lokale KI**')
    if show_address_service:
        st.caption('Für die Schweizer Adressprüfung wird kein LLM benötigt.')
        selected_model = MODEL
    elif installed:
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


if show_swisstopo:
    st.subheader('🏔️ swisstopo – amtlicher Gebäudeadress-Datenstand')
    st.info('Diese Funktion importiert ausschliesslich einen öffentlichen swisstopo-Datensatz. Keine CRM-Daten werden an swisstopo oder einen Cloud-Dienst gesendet.')
    current=active_snapshot(db)
    if current:
        st.success(f"Aktiv: Datenstand {current['reference_date']} · {current['address_count']:,} lokale Indexeinträge · SHA256 {current['source_sha256'][:16]}…")
        if current['address_count'] < 100000:
            st.warning('Dieser Datenstand enthält weniger als 100’000 Einträge. Er könnte nur ein Test- oder Teilbestand sein; nicht als schweizweite Validierungsgrundlage interpretieren.')
    else:
        st.warning('Noch kein swisstopo-Datenstand geladen. Für einen neuen A → B Workflow bitte zuerst einen Stand importieren.')
    st.markdown('**1. Amtliches Verzeichnis der Gebäudeadressen auf dem eigenen PC herunterladen**')
    st.write('Offizielle Downloadseite: https://www.swisstopo.admin.ch/de/amtliches-verzeichnis-der-gebaeudeadressen')
    st.write('Format CSV (ggf. in ZIP); den unveränderten Datenstand auf dem Docker-Host ablegen, z. B. unter `/home/buebi/docker/crm-dubletten/data/swisstopo/`.')
    st.write('**2. Datei einmalig lokal einlesen**')
    base=Path('/app/data/swisstopo')
    base.mkdir(parents=True,exist_ok=True)
    local_files=sorted([x.name for x in base.iterdir() if x.is_file() and x.suffix.lower() in ('.csv','.zip')])
    st.caption('Empfohlen: das originale swisstopo-ZIP (z. B. 136 MB) direkt hochladen. Die enthaltene CSV (ca. 400 MB) wird beim Import aus dem ZIP gestreamt – nicht entpacken. Webupload-Limit: 250 MB pro Datei.')
    uploaded=st.file_uploader('CSV/ZIP in den lokalen Server hochladen (max. 250 MB)',type=['zip','csv'],key='sw_upload')
    if uploaded is not None:
        if uploaded.size > 250 * 1024**2:
            st.error('Datei über 250 MB: Bitte direkt in das Host-Verzeichnis kopieren.')
        elif st.button('Hochgeladene öffentliche Referenzdatei lokal speichern'):
            # Stream in chunks to avoid creating a second full in-memory copy.
            name=Path(uploaded.name).name
            tmp=None
            try:
                with tempfile.NamedTemporaryFile(mode='wb', dir=base, prefix='.swisstopo-upload-', suffix=Path(name).suffix.lower(), delete=False) as out:
                    tmp=Path(out.name)
                    uploaded.seek(0)
                    shutil.copyfileobj(uploaded, out, length=1024*1024)
                found=inspect_source(tmp)
                os.replace(tmp,base/name)
            except (OSError, ValueError, __import__('zipfile').BadZipFile) as exc:
                st.error(f'Upload wurde nicht gespeichert: {exc}')
            else:
                st.success(f'ZIP/CSV lokal gespeichert. Swisstopo-Adressformat erkannt: {found[0]["format"]}. Unten importieren.')
                st.rerun()
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
    if local_files:
        chosen=st.selectbox('Vorhandene lokale Datei',local_files)
        stand=st.text_input('Datenstand laut swisstopo-Download (YYYY-MM-DD)', value='',
                 help='Datum aus dem offiziell heruntergeladenen Referenzpaket übernehmen, niemals das Importdatum erfinden.')
        if st.button('📥 swisstopo-Datenstand lokal importieren',type='primary'):
            try:
                found=inspect_source(base/chosen)
                with st.spinner('Referenzdatei wird in die lokale Datenbank eingelesen …'):
                    stage=st.empty()
                    sid,count=import_snapshot(db,base/chosen,stand,lambda m:stage.caption(m))
                st.success(f'Datenstand #{sid}: {count:,} Indexeinträge importiert. Alte Prüfresultate bleiben dem früheren Stand zugeordnet.')
                st.rerun()
            except Exception as exc:
                st.error(f'Stand konnte nicht importiert werden: {exc}')
    else:
        st.caption('Keine CSV/ZIP-Datei unter /app/data/swisstopo/ gefunden.')
    if list_snapshots(db):
        st.markdown('**Verfügbare lokale Datenstände**')
        st.dataframe(pd.DataFrame(list_snapshots(db)),hide_index=True,use_container_width=True)
    st.warning('Das amtliche Verzeichnis bestätigt Gebäudeadressen, nicht ob eine Firma oder Person dort noch ansässig ist. Nicht gefundene Adressen sind Prüffälle, keine automatische Löschfreigabe.')
    st.stop()

if show_address_service:
    render_address_service(db)
    st.stop()

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
        st.subheader('2 · Prüfläufe und Felder auswählen')
        mode_label = st.radio('Art des CRM-Abgleichs',
            ['Zwei Workflows A → B (fehlende Referenzen + Referenzprüfung)',
             'Klassischer Dublettenabgleich wie bisher'],
            help='Die B-ID wird in System A gespeichert. Beide Auswertungen verwenden dieselben hochgeladenen Dateien.')
        mode='dual' if mode_label.startswith('Zwei Workflows') else 'legacy'
        ref_col=''
        exclude_col=''
        exclude_list=''
        empty_list='0;NULL;N/A;-'
        if mode=='dual':
            current=active_snapshot(db)
            if current:
                st.success(f"swisstopo: Stand {current['reference_date']} (wird VOR dem Splink-/RapidFuzz-Vergleich lokal geprüft)")
            else:
                st.error('Für die Vorprüfung zuerst im Menü «swisstopo-Datenstand» einen lokalen Datenstand installieren.')
            cols=['(bitte wählen)']+list(frame_a.columns)
            ref_col=st.selectbox('System A: Spalte mit der gespeicherten System-B-ID (Pflichtfeld)',cols)
            if ref_col=='(bitte wählen)':ref_col=''
            empty_list=st.text_input('Werte, die als fehlende B-Referenz gelten (mit ; trennen)',value=empty_list)
            excl_cols=['(kein Ausschlussfilter)']+list(frame_a.columns)
            exclude_col=st.selectbox('Optional: System-A-Spalte für ausgeschlossene Adressen',excl_cols)
            if exclude_col=='(kein Ausschlussfilter)':exclude_col=''
            if exclude_col:
                exclude_list=st.text_input('Werte, bei denen eine A-Adresse NICHT geprüft wird (mit ; trennen)',value='Nein;0;false')
                st.caption('Diese Ausschlüsse werden protokolliert, aber nicht gelöscht. Nur aktivieren, wenn diese Werte tatsächlich «nicht migrieren» bedeuten.')
        st.subheader('3 · Felder zuordnen')
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
                    if mode=='dual' and not active_snapshot(db):
                        raise ValueError('Für diesen Workflow fehlt der lokale swisstopo-Datenstand.')
                    project_id = import_project(db,title,frame_a,mapping_a,frame_b,mapping_b,engine=engine,
                       mode=mode,a_ref_column=ref_col,a_exclude_column=exclude_col,
                       exclude_values=[x.strip() for x in exclude_list.split(';') if x.strip()],
                       empty_reference_markers=[x.strip() for x in empty_list.split(';') if x.strip()])
                    box = st.empty()
                    try:
                        if mode=='dual':
                            box.caption('Amtliche Gebäudeadressen lokal prüfen …')
                            validate_project(db,project_id,progress=lambda message:box.caption(message))
                            box.caption('Vorhandene B-Referenzen direkt prüfen …')
                            run_reference_check(db,project_id)
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
dual = is_dual(db,pid)
if dual:
    review, reference_tab, swisstopo_tab, training_tab, exports, manage = st.tabs([
        '🔎 1 · Fehlende Referenzen', '🔗 2 · Referenzprüfung', '🏔️ swisstopo-Vorprüfung',
        '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])
else:
    review, training_tab, exports, manage = st.tabs(['✅ Prüfen', '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])

with review:
    if dual:
        st.info('Workflow 1: Nur System-A-Adressen OHNE System-B-Referenz (und ohne Ausschluss) werden mit sämtlichen B-Adressen verglichen. Bestätigungen erzeugen ausschliesslich eine Zuordnungsliste, keine Änderungen in CRM B.')
    render_review(db, pid, is_splink, selected_model, bool(installed))


if dual:
    with reference_tab:
        st.markdown('### Workflow 2 – Referenzen in A gegen B kontrollieren')
        st.caption('Direkter Vergleich anhand der B-ID in System A. Hier wird weder Splink noch Ollama benötigt.')
        summary=reference_overview(db,pid)
        metric_cols=st.columns(4)
        for col,(label,key) in zip(metric_cols,[('Identisch','identisch'),('Normalisiert gleich','normalisiert_gleich'),
             ('Abweichend','abweichend'),('Referenzfehler','referenz_fehlt')]):
            col.metric(label,summary.get(key,0))
        if summary.get('mehrfachreferenz'):
            st.warning(f"{summary['mehrfachreferenz']} A-Adressen haben eine mehrfach verwendete B-Referenz.")
        filter_options=['Alle','identisch','normalisiert_gleich','abweichend','referenz_fehlt','mehrfachreferenz','ausgeschlossen']
        col1,col2=st.columns([2,1])
        with col1:
            result_filter=st.selectbox('Referenzstatus',filter_options,key=f'ref_filter_{pid}')
        with col2:
            page_num=st.number_input('Seite (100 Fälle/Seite)',min_value=1,value=1,key=f'ref_page_{pid}')
        total,refs=reference_page(db,pid,result_filter,limit=100,offset=(page_num-1)*100)
        st.caption(f'{total:,} Datensätze in der Auswahl')
        if refs:
            view=[{'A-ID':r['a_id'],'B-Referenz in A':r['b_ref'],'B-ID gefunden':r['b_id'] or '—',
                   'Status':r['result'],
                   'Unterschiede':', '.join(d['bezeichnung'] for d in json.loads(r['differences_json'])
                     if d['status'] in ('abweichend','fehlt'))} for r in refs]
            st.dataframe(pd.DataFrame(view),hide_index=True,use_container_width=True,height=350)
            options={f"{r['a_id']} → {r['b_ref']} · {r['result']}":r for r in refs}
            selected_ref=st.selectbox('Detailfall auswählen',list(options),key=f'ref_detail_{pid}')
            chosen=options[selected_ref]
            diff=json.loads(chosen['differences_json'])
            if diff:
                st.dataframe(pd.DataFrame([{'Feld':d['bezeichnung'],'CRM A':d['a_wert'] or '—',
                     'CRM B':d['b_wert'] or '—','Status':d['status']} for d in diff]),
                     hide_index=True,use_container_width=True)
            else:
                st.info('Keine B-Adressdaten für den Feldvergleich vorhanden bzw. ausgeschlossen.')
            if chosen['result'] in ('referenz_fehlt','mehrfachreferenz'):
                st.error('Referenzkonflikt: nicht automatisch korrigieren.')
    with swisstopo_tab:
        st.markdown('### Vorprüfung: Schweizer Gebäudeadressen')
        st.caption('Mit einem datierten, lokal gespeicherten swisstopo-Datenstand. Keine Personen-/Wohnsitzprüfung.')
        counts=validation_counts(db,pid)
        if counts:
            st.dataframe(pd.DataFrame([{'CRM':v['side'],'Ergebnis':RESULT_LABELS.get(v['result'],v['result']),
                            'Anzahl':v['n']} for v in counts]),hide_index=True,use_container_width=True)
            col_a,col_b=st.columns(2)
            with col_a:
                sw_side=st.selectbox('CRM-Quelle', ['A','B'],key=f'sw_side_{pid}')
            with col_b:
                sw_filter=st.selectbox('Ergebnisfilter', ['Alle',*RESULT_LABELS],
                                       format_func=lambda x:RESULT_LABELS.get(x,x),key=f'sw_filter_{pid}')
            clause=" AND v.result=?" if sw_filter!='Alle' else ''
            args=[pid,sw_side]+([sw_filter] if sw_filter!='Alle' else [])
            rows=db.execute('''SELECT r.source_id id,r.street_name strasse,r.house_number hausnummer,
                  r.postal plz,r.city ort,v.result,v.suggestion,s.reference_date stand
                  FROM swisstopo_results v JOIN records r ON r.pk=v.record_pk
                  JOIN swisstopo_snapshots s ON s.snapshot_id=v.snapshot_id
                  WHERE v.project_id=? AND r.side=?'''+clause+''' ORDER BY r.source_id LIMIT 100''',args).fetchall()
            if rows:
                st.dataframe(pd.DataFrame([{'ID':r['id'],'Strasse':r['strasse'],
                    'Hausnr.':r['hausnummer'],'PLZ':r['plz'],'Ort':r['ort'],
                    'Ergebnis':RESULT_LABELS.get(r['result'],r['result']),
                    'Amtlicher Hinweis':r['suggestion'],'Stand':r['stand']} for r in rows]),
                    hide_index=True,use_container_width=True)
                st.caption('Max. 100 Datensätze in dieser Vorschau; vollständige Ergebnisse unter Exporte.')
        else:
            st.info('Noch keine swisstopo-Prüfung durchgeführt.')
        if st.button('Mit dem aktuell installierten Datenstand erneut prüfen',key=f'recheck_{pid}'):
            try:
                with st.spinner('Prüfe lokal …'):
                    done=validate_project(db,pid)
                st.success(f'{done:,} Adressen neu geprüft.')
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        st.warning('Nicht gefunden bedeutet nicht: Gebäude existiert nicht. Keine Adressen automatisch ändern oder löschen.')


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
    if dual:
        st.markdown('### Zwei unabhängige Prüfauswertungen')
        st.download_button('⬇️ Workflow 1: bestätigte A→B-Zuordnungen',
            export_pairs(db,pid,True),file_name=f'crm_{pid}_w1_bestaetigte_matches.csv',mime='text/csv')
        st.download_button('⬇️ Workflow 1: vollständiger Status ALLER A-Adressen',
            export_workflow1_inventory(db,pid),file_name=f'crm_{pid}_w1_alle_adressen.csv',mime='text/csv')
        st.download_button('⬇️ Workflow 2: alle vorhandenen B-Referenzen und ihre Ergebnisse',
            export_references(db,pid),file_name=f'crm_{pid}_w2_referenzvergleich.csv',mime='text/csv')
        st.download_button('⬇️ Workflow 2: Feldabweichungen (eine Zeile pro Feld)',
            export_reference_field_differences(db,pid),file_name=f'crm_{pid}_w2_felddifferenzen.csv',mime='text/csv')
        st.download_button('⬇️ swisstopo: Adressprüfung A und B (datierter Stand)',
            export_validation(db,pid),file_name=f'crm_{pid}_swisstopo_pruefung.csv',mime='text/csv')
        st.warning('«Kein Kandidat» ist KEINE Importfreigabe. Diese Version erstellt ausdrücklich keine automatische Import-/Löschliste.')
        st.divider()
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
