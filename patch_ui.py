from pathlib import Path
p=Path('app/ui.py');s=p.read_text()
s=s.replace("from review_ui import render_review", """from review_ui import render_review
from migration import (is_dual, run_reference_check, reference_overview, reference_page,
                       export_references, export_reference_field_differences,
                       export_workflow1_inventory, workflow1_inventory)
from swisstopo import (ensure_schema as ensure_sw_schema, active_snapshot, import_snapshot,
                      list_snapshots, validate_project, validation_counts, export_validation,
                      RESULT_LABELS)
import pandas as pd
import io
""")
s=s.replace("db = connect(DB_PATH)\n", "db = connect(DB_PATH)\nensure_sw_schema(db)\n")
s=s.replace("GUI v1.6 ·", "GUI v1.7 ·")
s=s.replace("    if available:\n        choices = ['+ Neuer Abgleich'] + list(available)\n        selected = st.selectbox('Projekt', choices, index=0 if st.session_state.get('new_project') else 1)\n        pid = available.get(selected)\n    else:\n        pid = None", """    choices = ['+ Neuer Abgleich', '🏔️ swisstopo-Datenstand'] + list(available)
    selected = st.selectbox('Projekt / Datenstand',choices,
                            index=0 if st.session_state.get('new_project') or not available else 2)
    pid = available.get(selected)
    show_swisstopo = selected == '🏔️ swisstopo-Datenstand'""")
pos=s.index("if pid is None:\n")
new_page='''if show_swisstopo:
    st.subheader('🏔️ swisstopo – amtlicher Gebäudeadress-Datenstand')
    st.info('Diese Funktion importiert ausschliesslich einen öffentlichen swisstopo-Datensatz. Keine CRM-Daten werden an swisstopo oder einen Cloud-Dienst gesendet.')
    current=active_snapshot(db)
    if current:
        st.success(f"Aktiv: Datenstand {current['reference_date']} · {current['address_count']:,} lokale Indexeinträge · SHA256 {current['source_sha256'][:16]}…")
    else:
        st.warning('Noch kein swisstopo-Datenstand geladen. Für einen neuen A → B Workflow bitte zuerst einen Stand importieren.')
    st.markdown('**1. Amtliches Verzeichnis der Gebäudeadressen auf dem eigenen PC herunterladen**')
    st.write('Offizielle Downloadseite: https://www.swisstopo.admin.ch/de/amtliches-verzeichnis-der-gebaeudeadressen')
    st.write('Format CSV (ggf. in ZIP); den unveränderten Datenstand auf dem Docker-Host ablegen, z. B. unter `/home/buebi/docker/crm-dubletten/data/swisstopo/`.')
    st.write('**2. Datei einmalig lokal einlesen**')
    base=Path('/app/data/swisstopo')
    base.mkdir(parents=True,exist_ok=True)
    local_files=sorted([x.name for x in base.iterdir() if x.is_file() and x.suffix.lower() in ('.csv','.zip')])
    st.caption('Grossen Datensatz am besten per SCP/SFTP ins Host-Verzeichnis kopieren. Kleine Testdaten können auch über den Browser hochgeladen werden.')
    uploaded=st.file_uploader('Alternativ: CSV/ZIP in den lokalen Server hochladen (max. 60 MB)',type=['zip','csv'],key='sw_upload')
    if uploaded is not None:
        if len(uploaded.getvalue())>60*1024**2:
            st.error('Zu gross für Browserimport. Bitte den Datensatz direkt in das Host-Verzeichnis kopieren.')
        elif st.button('Hochgeladene öffentliche Referenzdatei lokal speichern'):
            name=Path(uploaded.name).name
            (base/name).write_bytes(uploaded.getvalue())
            st.success('Datei wurde lokal gespeichert. Unten auswählen.')
            st.rerun()
    if local_files:
        chosen=st.selectbox('Vorhandene lokale Datei',local_files)
        stand=st.date_input('Datenstand laut swisstopo-Download',help='Bitte das Publikationsdatum der tatsächlich heruntergeladenen Referenzdatei wählen; nicht das Importdatum.').isoformat()
        if st.button('📥 swisstopo-Datenstand lokal importieren',type='primary'):
            try:
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

'''
s=s[:pos]+new_page+s[pos:]
# New import mode selection in upload panel
s=s.replace("        st.subheader('2 · Felder zuordnen')", """        st.subheader('2 · Prüfläufe und Felder auswählen')
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
        st.subheader('3 · Felder zuordnen')""")
s=s.replace("                    project_id = import_project(db,title,frame_a,mapping_a,frame_b,mapping_b,engine=engine)","""                    if mode=='dual' and not active_snapshot(db):
                        raise ValueError('Für diesen Workflow fehlt der lokale swisstopo-Datenstand.')
                    project_id = import_project(db,title,frame_a,mapping_a,frame_b,mapping_b,engine=engine,
                       mode=mode,a_ref_column=ref_col,a_exclude_column=exclude_col,
                       exclude_values=[x.strip() for x in exclude_list.split(';') if x.strip()],
                       empty_reference_markers=[x.strip() for x in empty_list.split(';') if x.strip()])""")
s=s.replace("                        if engine == 'splink5':\n", """                        if mode=='dual':
                            box.caption('Amtliche Gebäudeadressen lokal prüfen …')
                            validate_project(db,project_id,progress=lambda message:box.caption(message))
                            box.caption('Vorhandene B-Referenzen direkt prüfen …')
                            run_reference_check(db,project_id)
                        if engine == 'splink5':
""",1)
# new tab creation
s=s.replace("review, training_tab, exports, manage = st.tabs(['✅ Prüfen', '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])", """dual = is_dual(db,pid)
if dual:
    review, reference_tab, swisstopo_tab, training_tab, exports, manage = st.tabs([
        '🔎 1 · Fehlende Referenzen', '🔗 2 · Referenzprüfung', '🏔️ swisstopo-Vorprüfung',
        '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])
else:
    review, training_tab, exports, manage = st.tabs(['✅ Prüfen', '🧠 Splink-Training', '📥 Exporte', '⚙️ Verwaltung'])""")
s=s.replace("with review:\n    render_review", """with review:
    if dual:
        st.info('Workflow 1: Nur System-A-Adressen OHNE System-B-Referenz (und ohne Ausschluss) werden mit sämtlichen B-Adressen verglichen. Bestätigungen erzeugen ausschliesslich eine Zuordnungsliste, keine Änderungen in CRM B.')
    render_review""")
mark='\n\nwith training_tab:\n'
insert='''

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
'''
assert mark in s
s=s.replace(mark,insert+mark,1)
s=s.replace("with exports:\n    st.markdown('### Export für die CRM-Migration')", """with exports:
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
    st.markdown('### Export für die CRM-Migration')""")
p.write_text(s)
