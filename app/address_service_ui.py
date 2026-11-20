"""Independent, opt-in Swiss address-quality workflow in the existing Streamlit app."""
from __future__ import annotations
import json
from math import ceil

import pandas as pd
import streamlit as st

from address_service import (INPUT_FIELDS, INPUT_LABELS, guess_column, ensure_schema,
      read_single_upload, import_job, list_jobs, job_counts, page_rows,
      save_review, delete_job, export_job, navigate_review, display_street_without_number)
from swisstopo import active_snapshot, RESULT_LABELS
from address_review_ui import render_review_fragment

EXTRA_LABELS = {
    'eingabe_konflikt': 'Widerspruch in der Eingabe',
    'strasse_aehnlich': 'Ähnliche Strasse (Vorschlag)',
}

def label(code):
    return EXTRA_LABELS.get(code,RESULT_LABELS.get(code,code))




def render(db):
    ensure_schema(db)
    st.header('🏔️ Schweizer Adressprüfung')
    st.caption('Eigenständiger Dienst · Eine CSV/XLSX einlesen → lokale swisstopo-Prüfung → Korrekturen manuell bestätigen → CSV exportieren')
    st.info('Keine Adressen verlassen den Server. Der öffentliche swisstopo-Datenstand wird lokal durchsucht. Die Prüfung bestätigt ausschliesslich Gebäudeadressen, nicht Personen, Firmen oder Wohnsitze.')
    snapshot=active_snapshot(db)
    if not snapshot:
        st.warning('Noch kein lokaler swisstopo-Datenstand installiert. Bitte zuerst im Bereich «swisstopo-Datenstand» die öffentliche ZIP-Datei importieren.')
        return
    st.success(f"Referenz: swisstopo-Datenstand {snapshot['reference_date']} · {snapshot['address_count']:,} Indexeinträge")
    upload_tab,review_tab,export_tab,manage_tab=st.tabs(['1 · Datei prüfen','2 · Ergebnisse & Korrekturen','3 · Export','4 · Aufträge'])
    jobs=list_jobs(db)
    with upload_tab:
        st.markdown('### Einzelne Adressliste prüfen')
        st.caption('CSV und XLSX bis 60 MB, maximal 150’000 Zeilen. Die 136-MB-swisstopo-ZIP gehört in «swisstopo-Datenstand» und muss nicht erneut hochgeladen werden.')
        file=st.file_uploader('Adressliste auswählen',type=('csv','xlsx'),key='addr_service_upload')
        if file:
            try:
                frame=read_single_upload(file)
            except Exception as exc:
                st.error(f'Datei kann nicht gelesen werden: {exc}')
            else:
                st.success(f'{len(frame):,} Datensätze · {len(frame.columns)} Spalten')
                st.dataframe(frame.head(5),hide_index=True,use_container_width=True)
                name=st.text_input('Bezeichnung der Prüfung',value=file.name.rsplit('.',1)[0],key='addr_service_title')
                st.write('**Spalten zuordnen**')
                options=['(nicht vorhanden)',*frame.columns.tolist()]
                mapping={}
                left,right=st.columns(2)
                for i,key in enumerate(INPUT_FIELDS):
                    guess=guess_column(frame.columns,key)
                    with (left if i%2==0 else right):
                        selected=st.selectbox(INPUT_LABELS[key], options,
                                  index=options.index(guess) if guess in options else 0,
                                  key=f'addr_service_map_{key}')
                        mapping[key] = '' if selected == '(nicht vorhanden)' else selected
                st.caption('Bei kombinierter Strasse/Hausnummer bleibt «Hausnummer» leer. Separate Hausnummern haben Vorrang. Alle Originalspalten bleiben beim Export erhalten.')
                if st.button('🔍 Adressen gegen swisstopo prüfen',type='primary',disabled=not(mapping['street'] and mapping['postcode'])):
                    try:
                        with st.spinner('Prüfung läuft vollständig lokal …'):
                            progress=st.empty()
                            jid=import_job(db,frame,name,file.name,mapping,snapshot['snapshot_id'],progress.caption)
                        st.session_state['address_job_id']=jid
                        st.success(f'{len(frame):,} Adressen geprüft. Ergebnisse unter «2 · Ergebnisse & Korrekturen».')
                        st.rerun()
                    except Exception as exc:
                        st.error(f'Prüfung fehlgeschlagen: {exc}')
    with review_tab:
        render_review_fragment(db, jobs)
    with export_tab:
        if not jobs:
            st.info('Noch kein Prüfauftrag vorhanden.')
        else:
            ejob=st.selectbox('Prüfauftrag exportieren',jobs,
                format_func=lambda j:f"#{j['job_id']} · {j['title']}",key='address_export_job')
            jid=ejob['job_id']
            st.markdown('Die Originalspalten bleiben unverändert. Zusätzlich kannst du die Strasse **ohne Hausnummer** und die Hausnummer **als getrennte Zielspalten** exportieren.')
            split=st.checkbox('Strasse und Hausnummer in eigene Zielspalten ausgeben',value=True,
                     key=f'address_split_export_{jid}')
            street_col,house_col='Ziel_Strasse','Ziel_Hausnummer'
            if split:
                x,y=st.columns(2)
                with x: street_col=st.text_input('Neue Zielspalte Strasse',street_col,key=f'address_street_column_{jid}')
                with y: house_col=st.text_input('Neue Zielspalte Hausnummer',house_col,key=f'address_house_column_{jid}')
                st.caption('Für unbestätigte Fälle ist die Aufteilung nur eine ungeprüfte Extraktion. Die Spalte «Zieladresse_Basis» macht das sichtbar.')
            try:
                output_all=export_job(db,jid,split_house=split,street_column=street_col,house_column=house_col)
                output_corrected=export_job(db,jid,True,split_house=split,street_column=street_col,house_column=house_col)
                output_bulk=export_job(db,jid,split_house=split,street_column=street_col,house_column=house_col,only_bulk=True)
                st.download_button('⬇️ Gesamte geprüfte Liste (CSV)',output_all,
                     file_name=f'adresspruefung_{jid}_alle.csv',mime='text/csv',type='primary')
                st.download_button('⬇️ Nur bestätigte Korrekturen (CSV)',output_corrected,
                     file_name=f'adresspruefung_{jid}_korrigiert.csv',mime='text/csv')
                st.download_button('⬇️ Nur amtlich massenfreigegebene Adressen (CSV)',output_bulk,
                     file_name=f'adresspruefung_{jid}_massenfreigegeben.csv',mime='text/csv')
            except ValueError as exc:
                st.error(str(exc))
            st.warning('«Nicht gefunden» bedeutet nicht, dass eine Adresse unmöglich oder eine Person weggezogen ist. Korrekturspalten erst nach fachlicher Kontrolle in das Zielsystem übernehmen.')
    with manage_tab:
        if jobs:
            djob=st.selectbox('Prüfauftrag verwalten',jobs,
                    format_func=lambda j:f"#{j['job_id']} · {j['title']}",key='address_manage_job')
            st.caption(f"{djob['row_count']:,} Datensätze · swisstopo-Datenstand {djob['snapshot_date']}")
            if st.checkbox('Ich möchte diesen Prüfauftrag und seine Entscheide endgültig löschen.',key='address_delete_confirm'):
                if st.button('Prüfauftrag löschen',type='primary'):
                    delete_job(db,djob['job_id'])
                    st.rerun()
        else:
            st.info('Noch keine Prüfaufträge.')
