"""Swiss-address focus card: original source, evidence-coloured comparison and actions.

Uses the existing address_service.save_review workflow; no database change and
no automatic approval. Colour is always accompanied by explicit status text.
"""
from __future__ import annotations

import json

import streamlit as st

from focus_swiss_data import (
    comparison_rows, display_values, house_suggestions,
    original_address_lines, original_fields, safe,
)


def _mapping(db, job_id):
    try:
        row = db.execute('SELECT mapping_json FROM address_jobs WHERE job_id=?',
                         (job_id,)).fetchone()
        return json.loads(row['mapping_json']) if row and row['mapping_json'] else {}
    except (KeyError, ValueError, TypeError):
        return {}


def _safe_result(result):
    """Use the local swisstopo labels if available, falling back to result code."""
    try:
        from swisstopo import RESULT_LABELS
        return RESULT_LABELS.get(result, result)
    except ImportError:
        return result


def _comparison_html(source, rows, hint, result, suggestions):
    line_one, line_two = original_address_lines(source)
    original = f'''<div class="fsv-source">
      <div class="fsv-eyebrow">ORIGINALADRESSE · AUS DEN UNVERÄNDERTEN IMPORTFELDERN</div>
      <div class="fsv-original-line">{safe(line_one)}</div>
      <div class="fsv-original-place">{safe(line_two)}</div>
    </div>'''
    head = '''<div class="fsv-label-row"><span>Feld</span><span>Vorher</span><span class="fsv-arrow"> </span>
              <span>Vorschlag / Nachher</span><span>Bewertung</span></div>'''
    rendered = []
    for item in rows:
        tone = item['tone'] if item['tone'] in ('green', 'amber', 'red', 'neutral') else 'neutral'
        rendered.append(f'''<div class="fsv-compare-row">
        <span class="fsv-field">{safe(item['label'])}</span>
        <span class="fsv-value">{safe(item['before'])}</span><span class="fsv-arrow">→</span>
        <span class="fsv-target fsv-{tone}">{safe(item['after'])}</span>
        <span class="fsv-status fsv-status-{tone}">{safe(item['verdict'])}</span>
        </div>''')
    pills = ''
    if suggestions:
        pills = ('<div class="fsv-alternatives"><strong>Mögliche Hausnummern im Register – keine automatische Korrektur:</strong>'
                 + ''.join(f'<span class="fsv-number">{safe(x)}</span>' for x in suggestions)
                 + '</div>')
    elif hint and result in ('eingabe_konflikt', 'hausnummer_unbekannt', 'nicht_gefunden'):
        # Do not duplicate a long, unstructured info section.
        pills = f'<div class="fsv-hint">{safe(hint[:260])}</div>'
    return f'''<div class="fsv-card">{original}
       <div class="fsv-comparison">{head}{''.join(rendered)}</div>{pills}</div>'''


def render_swiss_case(db, row):
    """Returns (decision, correction|None). Persists nothing until caller saves."""
    from address_service import display_street_without_number
    try:
        source = original_fields(row, _mapping(db, row['job_id']))
        addr = source['normalized']
        proposals = json.loads(row['proposals_json'] or '[]')
        if not isinstance(proposals, list):
            proposals = []
    except (ValueError, TypeError, KeyError) as exc:
        st.error(f'Adressdaten konnten nicht gelesen werden: {exc}')
        return None, None

    st.caption(f"**ID {row['source_id']}** · {_safe_result(row['result'])} · Entscheid: {row['decision']}")
    if proposals:
        options = [-1] + list(range(len(proposals)))
        chosen = st.selectbox('Amtlichen Vorschlag auswählen (optional)', options,
             index=1,
             format_func=lambda i: ('Manuell eingeben' if i == -1 else
                f"{proposals[i].get('strasse','')} {proposals[i].get('hausnummer','')}, "
                f"{proposals[i].get('plz','')} {proposals[i].get('ort','')}"
                f" · EGAID {proposals[i].get('egaid','')}").strip(),
             key=f'focus_swiss_proposal_{row["row_pk"]}')
        selected = proposals[chosen] if chosen >= 0 else {}
    else:
        chosen, selected = -1, {}

    try:
        saved = json.loads(row['correction_json'] or '{}') if row['decision'] == 'korrigiert' else {}
    except (ValueError, TypeError):
        saved = {}
    # Saved manual decisions win over proposals; none is silently overwritten.
    defaults = saved or selected
    base = display_values(addr, display_street_without_number(addr))
    after = {k: str(defaults.get(k, base[k])) for k in ('strasse','hausnummer','plz','ort')}
    official = (not saved and bool(selected.get('amtlich')) and selected.get('status') == 'real')
    compared = comparison_rows(base, after, row['result'], official=official,
                               manually_edited=bool(saved) or (chosen < 0 and bool(selected)))
    suggestions = house_suggestions(row['hint'] if 'hint' in row.keys() else '')
    st.markdown(_comparison_html(source, compared, row['hint'] or '', row['result'], suggestions),
                unsafe_allow_html=True)
    st.caption('Grün = amtlich belegt · Orange = Vorschlag / nicht gefunden · Rot = Eingabekonflikt · Grau = nicht einzeln bestätigt. '
               'Die Farben sind keine Freigabe.')

    with st.form(f'focus_swiss_form_{row["row_pk"]}', border=False):
        left, mid, postal_col, right = st.columns([3.1, 1.35, 1.35, 2.2], gap='small')
        key = f'focus_swiss_edit_{row["row_pk"]}_{chosen}'
        with left:
            street = st.text_input('Strasse (ohne Hausnummer)', value=after['strasse'], key=key+'_str')
        with mid:
            house = st.text_input('Hausnummer', value=after['hausnummer'], key=key+'_house')
        with postal_col:
            postal = st.text_input('PLZ', value=after['plz'], key=key+'_zip')
        with right:
            city = st.text_input('Ort', value=after['ort'], key=key+'_city')
        a, b, c, d = st.columns(4, gap='small')
        corrected = a.form_submit_button('✓ Korrektur bestätigen', type='primary', use_container_width=True)
        unchanged = b.form_submit_button('Ohne Änderung', use_container_width=True)
        skipped = c.form_submit_button('→ Überspringen', use_container_width=True)
        reset = d.form_submit_button('↩ Wieder öffnen', use_container_width=True)
        decision = ('korrigiert' if corrected else 'keine_aenderung' if unchanged
                    else 'uebersprungen' if skipped else 'offen' if reset else None)
    with st.expander('Vollständiger Originaldatensatz / technische Details', expanded=False):
        st.json(source['original'], expanded=False)
        st.caption('Quelle: Originalspalten des hochgeladenen Datensatzes; keine nachträgliche Normalisierung.')
        if selected:
            st.json(selected, expanded=False)
    correction = {'strasse': street, 'hausnummer': house, 'plz': postal, 'ort': city} if corrected else None
    return decision, correction
