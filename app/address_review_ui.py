"""Stable, fragment-scoped Swiss address review with strict offline bulk approval.

Only the address review panel rerenders after a decision. No browser JS, no
manual scroll manipulation, no automatic CRM modifications.
"""
from __future__ import annotations

import json
from math import ceil

import pandas as pd
import streamlit as st

from address_service import (
    job_counts, page_rows, navigate_review, save_review,
    display_street_without_number, bulk_approval_preview, bulk_approve_exact,
    MASS_APPROVED,
)
from swisstopo import RESULT_LABELS

LABELS = {'eingabe_konflikt': 'Widerspruch in der Eingabe',
          'strasse_aehnlich': 'Ähnliche Strasse (Vorschlag)'}


def _label(result):
    return LABELS.get(result, RESULT_LABELS.get(result, result))


@st.fragment
def render_review_fragment(db, jobs):
    """Rendered inside the review tab; each interaction reruns only this panel."""
    if not jobs:
        st.info('Bitte zuerst eine Adressliste unter «1 · Datei prüfen» hochladen.')
        return

    selected = st.selectbox(
        'Prüfauftrag', jobs,
        format_func=lambda j: f"#{j['job_id']} · {j['title']} ({j['row_count']:,} Adressen, Stand {j['snapshot_date']})",
        index=next((i for i, j in enumerate(jobs)
                    if j['job_id'] == st.session_state.get('address_job_id')), 0),
        key='address_service_selected_job')
    jid = selected['job_id']
    counts = job_counts(db, jid)
    summary = {}
    for item in counts:
        summary[item['result']] = summary.get(item['result'], 0) + item['n']
    reviewed = sum(x['n'] for x in counts if x['decision'] == 'korrigiert')
    mass = sum(x['n'] for x in counts if x['decision'] == MASS_APPROVED)
    open_count = sum(x['n'] for x in counts if x['decision'] == 'offen')
    a, b, c, d = st.columns(4)
    a.metric('Adressen', f"{selected['row_count']:,}")
    b.metric('Korrekturen bestätigt', f'{reviewed:,}')
    c.metric('Amtlich massenfreigegeben', f'{mass:,}')
    d.metric('Noch offen', f'{open_count:,}')

    flash_key = f'addr_review_notice_{jid}'
    if st.session_state.get(flash_key):
        st.success(st.session_state.pop(flash_key))

    # Batch review on demand: avoids an expensive database preview on every click.
    with st.expander('✅ Eindeutige Adressen massenweise freigeben', expanded=False):
        st.caption('Nur offene Adressen: aktuelle amtliche Gebäudeadresse, genau EIN Registertreffer und identische Strasse, Hausnummer, PLZ und Ort. '
                   'Die Originalschreibweise wird nur hinsichtlich Gross-/Kleinschreibung, Akzenten und Leerzeichen normalisiert. '
                   'Fuzzy-Treffer, Strassenabkürzungen, fehlende Orte, Statuskonflikte und Mehrfachtreffer sind ausgeschlossen.')
        preview_key = f'addr_bulk_preview_{jid}'
        if st.button('🔎 Freigabefähige Adressen ermitteln / Vorschau aktualisieren',
                     key=f'addr_bulk_preview_btn_{jid}'):
            try:
                st.session_state[preview_key] = bulk_approval_preview(db, jid)
            except Exception as exc:
                st.error(f'Massenfreigabe konnte nicht geprüft werden: {exc}')
        preview = st.session_state.get(preview_key)
        if preview is None:
            st.info('Zuerst die Vorschau berechnen. Die Massenfreigabe verändert keine Adresswerte.')
        else:
            st.metric('Freigabefähige Adressen', f"{preview['count']:,}")
            if preview['examples']:
                st.dataframe(pd.DataFrame(preview['examples']), use_container_width=True,
                             hide_index=True, height=min(260, 35 * (len(preview['examples']) + 1)))
            key = f'addr_bulk_confirm_{jid}'
            if st.session_state.pop(f'addr_bulk_reset_{jid}', False):
                st.session_state[key] = False
            checked = st.checkbox('Ich habe die Bedingungen geprüft. Nur diese eindeutigen Treffer freigeben.', key=key)
            if st.button(f"{preview['count']:,} exakt geprüfte Adressen freigeben",
                         disabled=not checked or not preview['count'], type='primary', key=f'addr_bulk_run_{jid}'):
                try:
                    number = bulk_approve_exact(db, jid, preview['count'], preview['digest'])
                    st.session_state.pop(preview_key, None)
                    st.session_state[f'addr_bulk_reset_{jid}'] = True
                    st.session_state[flash_key] = f'{number:,} Adressen als amtlich exakt geprüft markiert. Originaldaten unverändert.'
                    st.rerun(scope='fragment')
                except Exception as exc:
                    st.session_state.pop(preview_key, None)
                    st.error(str(exc))

    with st.expander('Filter und Ergebnisübersicht', expanded=False):
        if summary:
            st.dataframe(pd.DataFrame([{'Prüfergebnis': _label(k), 'Anzahl': v}
                                       for k, v in sorted(summary.items())]),
                         hide_index=True, use_container_width=True, height=170)
        ca, cb, cc = st.columns(3)
        with ca:
            result_filter = st.selectbox('Prüfergebnis', ['Alle', *sorted(summary)],
                                         format_func=lambda x: _label(x) if x != 'Alle' else x,
                                         key=f'address_result_filter_{jid}')
        with cb:
            decision_filter = st.selectbox(
                'Entscheid', ['Alle', 'offen', 'korrigiert', MASS_APPROVED,
                              'keine_aenderung', 'uebersprungen'],
                key=f'address_decision_filter_{jid}')
        with cc:
            term = st.text_input('ID / Adresse suchen', key=f'address_search_{jid}')
    # Widgets inside collapsed expanders still exist and preserve their state.
    result_filter = st.session_state.get(f'address_result_filter_{jid}', 'Alle')
    decision_filter = st.session_state.get(f'address_decision_filter_{jid}', 'Alle')
    term = st.session_state.get(f'address_search_{jid}', '')

    page_key = f'address_page_{jid}'
    row_key = f'address_row_select_{jid}'
    pending_key = f'address_pending_{jid}'
    pending = st.session_state.pop(pending_key, None)
    if pending:
        # Must happen before widgets are created.
        st.session_state[page_key], st.session_state[row_key] = pending[1], pending[0]

    total, _ = page_rows(db, jid, result_filter, decision_filter, term, 1, 0)
    max_page = max(1, ceil(total / 30))
    if page_key in st.session_state:
        st.session_state[page_key] = max(1, min(int(st.session_state[page_key]), max_page))
    c1, c2 = st.columns([1, 4])
    with c1:
        page = st.number_input('Seite', min_value=1, max_value=max_page,
                               key=page_key, value=1)
    with c2:
        st.caption(f'{total:,} Datensätze im Filter · Seite {page}/{max_page} · Auswahl wird ohne vollständiges Neuladen aktualisiert')
    _, rows = page_rows(db, jid, result_filter, decision_filter, term, 30, (page - 1) * 30)
    if not rows:
        st.success('Keine Datensätze im aktuellen Filter.')
        return

    by_pk = {row['row_pk']: row for row in rows}
    if st.session_state.get(row_key) not in by_pk:
        st.session_state[row_key] = rows[0]['row_pk']
    selected_pk = st.selectbox('Adresse auswählen', list(by_pk),
        format_func=lambda pk: (f"{by_pk[pk]['row_no']} · {by_pk[pk]['source_id']} · "
                                f"{_label(by_pk[pk]['result'])} · {by_pk[pk]['decision']}"),
        key=row_key)
    row = by_pk[selected_pk]

    na, nb, nc = st.columns([1, 1, 4])
    def jump(direction):
        target = navigate_review(db, jid, row['row_no'], result_filter, decision_filter,
                                 term, direction=direction, wrap=False)
        if target:
            st.session_state[pending_key] = target
            st.rerun(scope='fragment')
        st.info('Kein weiterer Datensatz im aktuellen Filter.')
    if na.button('← Zurück', key=f'addr_prev_{jid}'):
        jump('previous')
    if nb.button('Weiter →', key=f'addr_next_{jid}'):
        jump('next')
    nc.caption(f"Zeile {row['row_no']:,} · ID {row['source_id']} · Status: {row['decision']}")

    addr = json.loads(row['normalized_json'])
    orig = json.loads(row['original_json'])
    proposals = json.loads(row['proposals_json'])
    old = json.loads(row['correction_json']) if row['decision'] == 'korrigiert' else {}

    # Keep the on-screen review card compact and consistent in height.
    with st.container(height=610, border=True):
        st.markdown(f"**{_label(row['result'])}**")
        st.caption(row['hint'] or 'Kein weiterer Hinweis')
        if proposals:
            options = list(range(-1, len(proposals)))
            choice = st.selectbox('Amtlichen Vorschlag wählen', options, index=1,
                format_func=lambda i: ('Manuell eingeben' if i == -1 else
                     f"{proposals[i]['strasse']} {proposals[i]['hausnummer']}, "
                     f"{proposals[i]['plz']} {proposals[i]['ort']} · EGAID {proposals[i]['egaid']}"),
                key=f'addr_official_choice_{row["row_pk"]}')
            proposed = proposals[choice] if choice != -1 else {}
        else:
            proposed = {}
            st.caption('Kein eindeutiger amtlicher Korrekturvorschlag – gegebenenfalls manuell prüfen.')
        if row['result'] in ('geplant', 'aufgehoben', 'nicht_amtlich', 'status_unklar'):
            st.warning('Keine aktuell amtliche Adresse bestätigt.')
        defaults = old or proposed
        with st.form(f'address_review_{row["row_pk"]}'):
            before, after = st.columns(2, gap='large')
            with before:
                st.markdown('**Vorher · unverändert**')
                vals = [('Strasse', addr['street_source']),
                        ('Hausnummer (erkannt)', addr['house_source'] or addr['house']),
                        ('PLZ', addr['postal_source']), ('Ort', addr['city_source'])]
                st.dataframe(pd.DataFrame(vals, columns=['Feld', 'Original']),
                             hide_index=True, use_container_width=True, height=185)
            with after:
                st.markdown('**Nachher · bearbeitbar**')
                prefix = f'addr_edit_{row["row_pk"]}_{proposals.index(proposed) if proposed else -1}'
                street = st.text_input('Strasse ohne Hausnummer',
                      value=defaults.get('strasse', display_street_without_number(addr)), key=prefix+'_street')
                house = st.text_input('Hausnummer separat',
                      value=defaults.get('hausnummer', addr['house']), key=prefix+'_house')
                postal = st.text_input('PLZ', value=defaults.get('plz', addr['postal']), key=prefix+'_postal')
                city = st.text_input('Ort', value=defaults.get('ort', addr['city_source']), key=prefix+'_city')
            ba, bb, bc, bd = st.columns(4)
            save = ba.form_submit_button('✅ Korrektur bestätigen', type='primary')
            unchanged = bb.form_submit_button('Ohne Änderung')
            skip = bc.form_submit_button('⏭ Überspringen')
            reset = bd.form_submit_button('Wieder öffnen')
            if any((save, unchanged, skip, reset)):
                try:
                    decision = ('korrigiert' if save else 'keine_aenderung' if unchanged
                                else 'uebersprungen' if skip else 'offen')
                    save_review(db, jid, row['row_pk'], decision,
                        {'strasse': street, 'hausnummer': house, 'plz': postal, 'ort': city}
                        if save else None)
                    st.session_state.pop(f'addr_bulk_preview_{jid}', None)
                    target = navigate_review(db, jid, row['row_no'], result_filter,
                                             decision_filter, term, direction='next')
                    if target:
                        st.session_state[pending_key] = target
                        st.session_state[flash_key] = 'Entscheid gespeichert · nächster Datensatz.'
                    else:
                        st.session_state[flash_key] = 'Entscheid gespeichert · Ende der Trefferliste.'
                    st.rerun(scope='fragment')
                except Exception as exc:
                    st.error(str(exc))
    with st.expander('Alle Originalspalten anzeigen', expanded=False):
        st.dataframe(pd.DataFrame([{'Feld': k, 'Wert': v} for k, v in orig.items()]),
                     hide_index=True, use_container_width=True)
