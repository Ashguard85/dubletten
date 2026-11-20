"""Independent, opt-in Swiss address-quality workflow in the existing Streamlit app."""
from __future__ import annotations
import json
from math import ceil
import re

import pandas as pd
import streamlit as st

from address_service import (INPUT_FIELDS, INPUT_LABELS, guess_column, ensure_schema,
      read_single_upload, import_job, list_jobs, job_counts, page_rows,
      save_review, delete_job, export_job)
from swisstopo import active_snapshot, RESULT_LABELS

EXTRA_LABELS = {
    'eingabe_konflikt': 'Widerspruch in der Eingabe',
    'strasse_aehnlich': 'Ähnliche Strasse (Vorschlag)',
}

def label(code):
    return EXTRA_LABELS.get(code,RESULT_LABELS.get(code,code))


def original_street_without_number(addr):
    raw=addr['street_source']
    house=addr['house']
    if not house or addr['house_source']:
        return raw
    matched=re.fullmatch(r'(.+?)[,\s]+(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)',raw)
    return matched.group(1) if matched and matched.group(2).replace(' ','').lower()==house else raw


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
                decision_filter=st.selectbox('Entscheid',['Alle','offen','korrigiert','keine_aenderung'],
                                      key=f'address_decision_filter_{jid}')
            with cc:
                term=st.text_input('ID / Adresse suchen',key=f'address_search_{jid}')
            total,_=page_rows(db,jid,result_filter,decision_filter,term,1,0)
            max_page=max(1,ceil(total/30))
            page=st.number_input('Seite (30 pro Seite)',min_value=1,max_value=max_page,
                                 value=1,key=f'address_page_{jid}')
            _,rows=page_rows(db,jid,result_filter,decision_filter,term,30,(page-1)*30)
            st.caption(f'{total:,} Ergebnisse im aktuellen Filter')
            if rows:
                grid=[]
                for row in rows:
                    raw=json.loads(row['original_json'])
                    addr=json.loads(row['normalized_json'])
                    grid.append({'Zeile':row['row_no'],'ID':row['source_id'],
                       'Adresse':addr['street_source'],'PLZ':addr['postal_source'],'Ort':addr['city_source'],
                       'Ergebnis':label(row['result']),'Prüfentscheid':row['decision']})
                st.dataframe(pd.DataFrame(grid),hide_index=True,use_container_width=True,height=295)
                options={f"{r['row_no']} · {r['source_id']} · {label(r['result'])}":r for r in rows}
                chosen=st.selectbox('Adresse für Detailprüfung auswählen',list(options),key=f'address_row_select_{jid}')
                row=options[chosen]
                orig=json.loads(row['original_json']); addr=json.loads(row['normalized_json'])
                st.markdown(f"#### ID {row['source_id']} · {label(row['result'])}")
                st.caption(row['hint'] or 'Kein zusätzlicher Hinweis')
                a,b=st.columns(2)
                with a:
                    st.markdown('**Originaldatensatz**')
                    st.dataframe(pd.DataFrame([{'Feld':k,'Wert':v} for k,v in orig.items()]),
                                 hide_index=True,use_container_width=True,height=265)
                with b:
                    st.markdown('**Normalisierte Prüffelder**')
                    fields=[('Strasse',addr['street']),('Hausnummer',addr['house']),
                            ('PLZ',addr['postal']),('Ort',addr['city']),('Land',addr['country'])]
                    st.dataframe(pd.DataFrame(fields,columns=['Feld','Wert']),hide_index=True,
                                 use_container_width=True,height=265)
                proposals=json.loads(row['proposals_json'])
                if proposals:
                    descriptions=['Manuell eingeben']+[f"{p['strasse']} {p['hausnummer']}, {p['plz']} {p['ort']} · EGAID {p['egaid']}" for p in proposals]
                    p_choice=st.selectbox('Amtlichen Vorschlag auswählen (nicht automatisch übernommen)',
                               descriptions,index=1,key=f'address_proposal_{row["row_pk"]}')
                    selected_proposal=proposals[descriptions.index(p_choice)-1] if p_choice!='Manuell eingeben' else {}
                    if selected_proposal:
                        st.caption(selected_proposal.get('hinweis','')+' · Status: '+selected_proposal.get('status','unbekannt'))
                else:
                    selected_proposal={}
                    st.warning('Kein eindeutiger amtlicher Korrekturvorschlag verfügbar. Bei Postfächern und Sonderadressen ist das Verzeichnis nicht abschliessend.')
                if row['result'] in ('geplant','aufgehoben','nicht_amtlich','status_unklar'):
                    st.warning('Dieser Datensatz ist nicht als aktuelle amtliche Gebäudeadresse bestätigt. Eine Übernahme als «korrekt» wäre verfrüht.')
                old=json.loads(row['correction_json']) if row['decision']=='korrigiert' else {}
                defaults=old or selected_proposal
                with st.form(f'address_review_{row["row_pk"]}'):
                    st.markdown('**Vorgeschlagene / manuelle Korrektur (Original bleibt unangetastet)**')
                    l,r=st.columns(2)
                    with l:
                        street=st.text_input('Korrigierte Strasse',value=defaults.get('strasse',original_street_without_number(addr)))
                        house=st.text_input('Korrigierte Hausnummer',value=defaults.get('hausnummer',addr['house']))
                    with r:
                        postal=st.text_input('Korrigierte PLZ',value=defaults.get('plz',addr['postal']))
                        city=st.text_input('Korrigierter Ort',value=defaults.get('ort',addr['city_source']))
                    st.caption('Nur «Korrektur bestätigen» erzeugt einen bestätigten Korrekturdatensatz. «Keine Änderung» und «Offen» verändern keine Adresswerte.')
                    b1,b2,b3=st.columns(3)
                    save=b1.form_submit_button('✅ Korrektur bestätigen',type='primary')
                    skip=b2.form_submit_button('Ohne Änderung abschliessen')
                    reset=b3.form_submit_button('Wieder offen')
                    if save or skip or reset:
                        try:
                            decision='korrigiert' if save else ('keine_aenderung' if skip else 'offen')
                            save_review(db,jid,row['row_pk'],decision,
                                    {'strasse':street,'hausnummer':house,'plz':postal,'ort':city} if save else None)
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
    with export_tab:
        if not jobs:
            st.info('Noch kein Prüfauftrag vorhanden.')
        else:
            ejob=st.selectbox('Prüfauftrag exportieren',jobs,
                format_func=lambda j:f"#{j['job_id']} · {j['title']}",key='address_export_job')
            jid=ejob['job_id']
            st.markdown('Alle ursprünglichen Eingabespalten bleiben unverändert. Nur zusätzlich beschriftete Korrekturspalten enthalten bestätigte Änderungen. Die Spalte `Korrekturquelle` unterscheidet manuelle Änderungen von bestätigten aktuellen swisstopo-Vorschlägen.')
            st.download_button('⬇️ Gesamte geprüfte Liste (CSV)',export_job(db,jid),
                 file_name=f'adresspruefung_{jid}_alle.csv',mime='text/csv',type='primary')
            st.download_button('⬇️ Nur bestätigte Korrekturen (CSV)',export_job(db,jid,True),
                 file_name=f'adresspruefung_{jid}_korrigiert.csv',mime='text/csv')
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
