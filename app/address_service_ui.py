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
        if not jobs:
            st.info('Bitte zuerst eine Adressliste unter «1 · Datei prüfen» hochladen.')
        else:
            selected=st.selectbox('Prüfauftrag',jobs,
                format_func=lambda j:f"#{j['job_id']} · {j['title']} ({j['row_count']:,} Adressen, Stand {j['snapshot_date']})",
                index=next((i for i,j in enumerate(jobs) if j['job_id']==st.session_state.get('address_job_id')),0),
                key='address_service_selected_job')
            jid=selected['job_id']
            counts=job_counts(db,jid)
            summary={}
            for item in counts:
                summary[item['result']]=summary.get(item['result'],0)+item['n']
            a,b,c=st.columns(3)
            a.metric('Adressen',selected['row_count'])
            b.metric('Korrekturen bestätigt',sum(x['n'] for x in counts if x['decision']=='korrigiert'))
            c.metric('Noch offen',sum(x['n'] for x in counts if x['decision']=='offen'))
            if summary:
                st.dataframe(pd.DataFrame([{'Prüfergebnis':label(k),'Anzahl':v} for k,v in sorted(summary.items())]),
                    hide_index=True,use_container_width=True)
            ca,cb,cc=st.columns(3)
            with ca:
                statuses=['Alle',*sorted(summary)]
                result_filter=st.selectbox('Prüfergebnis',statuses,format_func=lambda x:label(x) if x!='Alle' else x,
                                       key=f'address_result_filter_{jid}')
            with cb:
                decision_filter=st.selectbox('Entscheid',['Alle','offen','korrigiert','keine_aenderung','uebersprungen'],
                                      key=f'address_decision_filter_{jid}')
            with cc:
                term=st.text_input('ID / Adresse suchen',key=f'address_search_{jid}')
            # Deferred navigation: update session-state keys BEFORE Streamlit
            # creates the number_input / selectbox widgets in this script run.
            page_key=f'address_page_{jid}'
            row_key=f'address_row_select_{jid}'
            pending_key=f'address_pending_{jid}'
            flash_key=f'address_flash_{jid}'
            pending=st.session_state.pop(pending_key,None)
            if pending is not None:
                st.session_state[page_key]=pending[1]
                st.session_state[row_key]=pending[0]
            if flash_key in st.session_state:
                st.info(st.session_state.pop(flash_key))
            total,_=page_rows(db,jid,result_filter,decision_filter,term,1,0)
            max_page=max(1,ceil(total/30))
            if page_key in st.session_state:
                st.session_state[page_key]=max(1,min(int(st.session_state[page_key]),max_page))
            page=st.number_input('Seite (30 pro Seite)',min_value=1,max_value=max_page,
                                 value=1,key=page_key)
            _,rows=page_rows(db,jid,result_filter,decision_filter,term,30,(page-1)*30)
            st.caption(f'{total:,} Ergebnisse im aktuellen Filter')
            if not rows:
                st.success('Keine weiteren Adressen in diesem Filter.')
            else:
                grid=[]
                for candidate in rows:
                    addr=json.loads(candidate['normalized_json'])
                    grid.append({'Zeile':candidate['row_no'],'ID':candidate['source_id'],
                       'Adresse':addr['street_source'],'PLZ':addr['postal_source'],'Ort':addr['city_source'],
                       'Ergebnis':label(candidate['result']),'Prüfentscheid':candidate['decision']})
                st.dataframe(pd.DataFrame(grid),hide_index=True,use_container_width=True,height=235)
                row_lookup={item['row_pk']:item for item in rows}
                if st.session_state.get(row_key) not in row_lookup:
                    st.session_state[row_key]=rows[0]['row_pk']
                selected_pk=st.selectbox('Adresse für Detailprüfung auswählen',list(row_lookup),
                      format_func=lambda pk: f"{row_lookup[pk]['row_no']} · {row_lookup[pk]['source_id']} · {label(row_lookup[pk]['result'])} · {row_lookup[pk]['decision']}",
                      key=row_key)
                row=row_lookup[selected_pk]
                nav_a,nav_b,nav_c=st.columns([1,1,3])
                def jump(direction):
                    target=navigate_review(db,jid,row['row_no'],result_filter,decision_filter,term,direction)
                    if target:
                        st.session_state[pending_key]=target
                        st.rerun()
                    st.info('Kein weiterer Eintrag in dieser Richtung.')
                if nav_a.button('← Vorherige',key=f'addr_prev_{jid}'):
                    jump('previous')
                if nav_b.button('Nächste →',key=f'addr_next_{jid}'):
                    jump('next')
                nav_c.caption(f"Aktuelle Adresse: Zeile {row['row_no']} · ID {row['source_id']}")

                orig=json.loads(row['original_json']);addr=json.loads(row['normalized_json'])
                st.markdown(f"#### {label(row['result'])} · ID {row['source_id']}")
                st.caption(row['hint'] or 'Kein zusätzlicher Hinweis')
                proposals=json.loads(row['proposals_json'])
                if proposals:
                    descriptions=['Manuell eingeben']+[f"{p['strasse']} {p['hausnummer']}, {p['plz']} {p['ort']} · EGAID {p['egaid']}" for p in proposals]
                    p_choice=st.selectbox('Amtlichen Vorschlag auswählen (nicht automatisch übernommen)',
                               descriptions,index=1,key=f'address_proposal_{row["row_pk"]}')
                    proposal_ix=descriptions.index(p_choice)
                    selected_proposal=proposals[proposal_ix-1] if proposal_ix else {}
                    if selected_proposal:
                        st.caption(selected_proposal.get('hinweis','')+' · Status: '+selected_proposal.get('status','unbekannt'))
                else:
                    proposal_ix=0
                    selected_proposal={}
                    st.warning('Kein eindeutiger amtlicher Korrekturvorschlag verfügbar. Bei Postfächern und Sonderadressen ist das Verzeichnis nicht abschliessend.')
                if row['result'] in ('geplant','aufgehoben','nicht_amtlich','status_unklar'):
                    st.warning('Nicht als aktuelle amtliche Gebäudeadresse bestätigt. Vor Übernahme prüfen.')
                old=json.loads(row['correction_json']) if row['decision']=='korrigiert' else {}
                defaults=selected_proposal or old

                st.markdown('### Vorher / Nachher')
                with st.form(f'address_review_{row["row_pk"]}'):
                    before,after=st.columns(2,gap='large')
                    with before:
                        st.markdown('**Vorher · Original aus deiner Datei**')
                        for field,value in (
                            ('Strasse / Adresse',addr['street_source']),
                            ('Hausnummer (erkannt)',addr['house_source'] or addr['house']),
                            ('PLZ',addr['postal_source']),('Ort',addr['city_source'])):
                            st.text_input(field,value=value,disabled=True,
                               key=f'addr_orig_{row["row_pk"]}_{field}')
                    with after:
                        st.markdown('**Nachher · vorgeschlagen / bearbeitbar**')
                        prefix=f'addr_edit_{row["row_pk"]}_{proposal_ix}'
                        street=st.text_input('Strasse (ohne Hausnummer)',
                            value=defaults.get('strasse',display_street_without_number(addr)),key=prefix+'_street')
                        house=st.text_input('Hausnummer (separates Feld)',
                            value=defaults.get('hausnummer',addr['house']),key=prefix+'_house')
                        postal=st.text_input('PLZ',value=defaults.get('plz',addr['postal']),key=prefix+'_postal')
                        city=st.text_input('Ort',value=defaults.get('ort',addr['city_source']),key=prefix+'_city')
                    st.caption('Rechts ist zunächst nur ein Vorschlag. Nur «Korrektur bestätigen» speichert die neuen Werte; der Originaldatensatz bleibt erhalten.')
                    b1,b2,b3,b4=st.columns(4)
                    save=b1.form_submit_button('✅ Korrektur bestätigen',type='primary')
                    unchanged=b2.form_submit_button('Ohne Änderung')
                    skip=b3.form_submit_button('⏭ Überspringen')
                    reset=b4.form_submit_button('Wieder öffnen')
                    if any((save,unchanged,skip,reset)):
                        try:
                            decision=('korrigiert' if save else 'keine_aenderung' if unchanged
                                      else 'uebersprungen' if skip else 'offen')
                            save_review(db,jid,row['row_pk'],decision,
                                  {'strasse':street,'hausnummer':house,'plz':postal,'ort':city} if save else None)
                            target=navigate_review(db,jid,row['row_no'],result_filter,decision_filter,term,
                                                   direction='next',wrap=False)
                            if target:
                                st.session_state[pending_key]=target
                                st.session_state[flash_key]='Entscheid gespeichert · nächster Datensatz geladen.'
                            else:
                                st.session_state[flash_key]='Entscheid gespeichert · keine weitere Adresse nach dieser Zeile im aktuellen Filter.'
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                with st.expander('Alle Originalspalten anzeigen'):
                    st.dataframe(pd.DataFrame([{'Feld':k,'Wert':v} for k,v in orig.items()]),
                             hide_index=True,use_container_width=True)
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
                st.download_button('⬇️ Gesamte geprüfte Liste (CSV)',output_all,
                     file_name=f'adresspruefung_{jid}_alle.csv',mime='text/csv',type='primary')
                st.download_button('⬇️ Nur bestätigte Korrekturen (CSV)',output_corrected,
                     file_name=f'adresspruefung_{jid}_korrigiert.csv',mime='text/csv')
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
