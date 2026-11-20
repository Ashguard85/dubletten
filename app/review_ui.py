"""Three Streamlit review modes sharing one project and one set of decisions.

The review page never performs automatic CRM merges. All widgets are backed by
review_data's server-side pagination or the existing engine API.
"""
import hashlib
import json

import pandas as pd
import streamlit as st

from engine import (FIELDS, LABELS, comparison_diagnostics, save_llm, set_review)
from llm import assess
from review_data import (RISKS, STATES, STATE_LABELS, cockpit_counts,
                         conflict_flags, display_name, grid_rows, pair_page)

VIEW_NAMES = ('Data Grid', 'Split-View', 'Review-Cockpit')


def _id(pair):
    return int(pair['a_pk']), int(pair['b_pk'])


def _key(pid, name):
    return f'review_{name}_{pid}'


def _set_selection(pid, a_pk, b_pk):
    st.session_state[_key(pid, 'selected')] = (int(a_pk), int(b_pk))


def _jump_to(pid, view, state='Alle', risk='Alle'):
    # Called via on_click BEFORE Streamlit instantiates widgets on rerun.
    st.session_state[_key(pid, 'view')] = view
    st.session_state[_key(pid, 'status')] = state
    st.session_state[_key(pid, 'risk')] = risk
    st.session_state[_key(pid, 'page')] = 1


def _title(pair, is_splink):
    a = display_name(pair['a_json'], pair['a_id'])
    b = display_name(pair['b_json'], pair['b_id'])
    unit = 'Splink' if is_splink else 'RapidFuzz'
    return f"{pair['score']}/100 ({unit}) · {a} ↔ {b}"


def _active_pair(pid, pairs):
    if not pairs:
        return None
    chosen = st.session_state.get(_key(pid, 'selected'))
    for row in pairs:
        if _id(row) == chosen:
            return row
    _set_selection(pid, *_id(pairs[0]))
    return pairs[0]


def _detail(db, pid, pair, is_splink, selected_model, installed, multi_keys, compact=False):
    if pair is None:
        st.info('Kein Kandidat ausgewählt.')
        return
    raw_a = json.loads(pair['a_json'])
    raw_b = json.loads(pair['b_json'])
    st.markdown('#### Adressvergleich')
    # Render CRM-supplied names as plain text: never parse as Markdown/remote images.
    st.text(f"CRM A · ID {pair['a_id']}   ↔   CRM B · ID {pair['b_id']}")
    st.text(_title(pair, is_splink))
    st.caption('Prüfstatus: ' + STATE_LABELS.get(pair['state'], pair['state']))
    if _id(pair) in multi_keys:
        st.warning('Mehrere potenzielle Zuordnungen zu mindestens einer CRM-ID – bitte konkurrierende Kandidaten kontrollieren.')
    if pair['a_house'] and pair['b_house'] and pair['a_house'] != pair['b_house']:
        st.warning('Hausnummern weichen voneinander ab. Dies allein beweist keine Verschiedenheit.')
        st.text(f"Hausnummer A: {pair['a_house']} · Hausnummer B: {pair['b_house']}")

    differences = []
    for field in FIELDS:
        if field == 'id':
            continue
        left = str(raw_a.get(field, '') or '')
        right = str(raw_b.get(field, '') or '')
        if not (left or right):
            continue
        if left.strip().casefold() == right.strip().casefold() and left:
            state = '✓ identisch'
        elif not left or not right:
            state = '— fehlt'
        else:
            state = '≠ abweichend'
        differences.append({'Feld': LABELS.get(field, field), 'CRM A': left or '—',
                            'CRM B': right or '—', 'Vergleich': state})
    if compact:
        # Fit the detail table into the right panel without a fourth wide column.
        compact_rows = [{**{k:v for k,v in row.items() if k != 'Vergleich'},
                         'Feld': ('✓ ' if row['Vergleich'].startswith('✓') else '≠ ') + row['Feld']}
                        for row in differences]
        st.dataframe(pd.DataFrame(compact_rows), hide_index=True, use_container_width=True,
                     height=min(470, max(140, 40 + 35*len(compact_rows))))
    else:
        st.dataframe(pd.DataFrame(differences), hide_index=True, use_container_width=True,
                     height=min(470, max(140, 40 + 35*len(differences))))
    if is_splink and pair['match_probability'] is not None:
        st.caption(f"**Splink-Modellwert:** {pair['match_probability']:.5f} · nicht unabhängig kalibriert")
    else:
        st.caption('RapidFuzz-Wert ist eine Heuristik, keine Wahrscheinlichkeit.')
    st.caption('Matching-Hinweise: ' + str(pair['reason']))
    if pair['llm_decision']:
        st.info('Lokale KI – Vorschlag (keine automatische Freigabe)')
        st.text(f"{pair['llm_model']}: {pair['llm_decision']} · {pair['llm_reason']}")

    # Diagnostics remain available in all modes without long expander stacks.
    diag_key = _key(pid, 'diagnose_' + str(pair['a_pk']) + '_' + str(pair['b_pk']))
    if st.checkbox('Feldweise Diagnose anzeigen', key=diag_key):
        a_norm = db.execute('SELECT * FROM records WHERE pk=?', (pair['a_pk'],)).fetchone()
        b_norm = db.execute('SELECT * FROM records WHERE pk=?', (pair['b_pk'],)).fetchone()
        st.dataframe(comparison_diagnostics(a_norm, b_norm),
                     hide_index=True, use_container_width=True)
        if a_norm['address_conflict'] or b_norm['address_conflict']:
            st.warning('Abweichende Hausnummer zwischen CRM-Feld und Adresszeile erkannt.')
        st.caption('Normalisierte Feldwerte; keine kausale Erklärung des Splink-Scores.')

    st.markdown('**Manuelle Entscheidung**')
    choices = (
        ('bestaetigt', '✅ Bestätigen'), ('abgelehnt', '❌ Ablehnen'),
        ('unklar', '❓ Unklar'), ('offen', '↩️ Zurücksetzen'),
    )
    if compact:
        slots = [*st.columns(2), *st.columns(2)]
    else:
        slots = st.columns(4)
    for col, (new_state, caption) in zip(slots, choices):
        with col:
            if st.button(caption, key=f'action_{pid}_{pair["a_pk"]}_{pair["b_pk"]}_{new_state}',
                         use_container_width=True, disabled=pair['state'] == new_state):
                try:
                    set_review(db, pid, pair['a_pk'], pair['b_pk'], new_state)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if installed:
        if st.button('🤖 Dieses Paar lokal mit Ollama prüfen',
                     key=f'ai_{pid}_{pair["a_pk"]}_{pair["b_pk"]}',
                     use_container_width=True):
            with st.spinner('Ollama prüft den Kandidaten lokal …'):
                try:
                    decision, reason = assess(raw_a, raw_b, selected_model)
                    save_llm(db, pid, pair['a_pk'], pair['b_pk'], decision, reason, selected_model)
                    st.rerun()
                except Exception as exc:
                    st.error(f'KI-Prüfung nicht möglich: {exc}')


def _batch_review(db, pid, is_splink, selected_model, installed):
    if not installed:
        st.caption('Ollama ist nicht verfügbar; manuelle Prüfung funktioniert weiterhin.')
        return
    with st.expander('🤖 Optionale KI-Stapelprüfung von Grenzfällen'):
        st.caption('Nur offene Kandidaten ohne KI-Vorschlag; keine automatische Freigabe.')
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            low = st.number_input('Score von', 0, 100, 40 if is_splink else 65, 1, key=_key(pid,'ai_low'))
        with col_b:
            high = st.number_input('Score bis', 0, 100, 95 if is_splink else 87, 1, key=_key(pid,'ai_high'))
        with col_c:
            limit = st.number_input('Max. Paare', 1, 25, 10, 1, key=_key(pid,'ai_limit'))
        if st.button('Lokale KI-Vorprüfung starten',key=_key(pid,'ai_start')):
            if low > high:
                st.error('Score von muss kleiner/gleich Score bis sein.')
                return
            jobs = db.execute('''SELECT p.a_pk,p.b_pk,a.original_json aj,b.original_json bj
                FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
                WHERE p.project_id=? AND p.state='offen' AND p.score BETWEEN ? AND ?
                    AND p.llm_decision='' ORDER BY p.score DESC LIMIT ?''',
                (pid, low, high, limit)).fetchall()
            if not jobs:
                st.info('Keine passenden offenen Fälle gefunden.')
                return
            bar = st.progress(0.)
            successes = 0
            for job in jobs:
                try:
                    decision, reason = assess(json.loads(job['aj']), json.loads(job['bj']), selected_model)
                    save_llm(db, pid, job['a_pk'], job['b_pk'], decision, reason, selected_model)
                    successes += 1
                    bar.progress(successes / len(jobs))
                except Exception as exc:
                    st.error(f'Nach {successes} erfolgreichen Auswertungen abgebrochen: {exc}')
                    return
            st.success(f'{successes} lokale KI-Vorschläge gespeichert. Bitte manuell prüfen.')


def _split_view(db, pid, rows, is_splink, selected_model, installed, multi):
    left, right = st.columns([1, 1.55], gap='medium')
    with left:
        st.markdown('#### Trefferliste')
        with st.container(height=630, border=True):
            for i, pair in enumerate(rows):
                active = _id(pair) == st.session_state.get(_key(pid,'selected'))
                badge = '⚠️ ' if _id(pair) in multi else ''
                label = f"{badge}{pair['score']} · {display_name(pair['a_json'], pair['a_id'])} ↔ {display_name(pair['b_json'], pair['b_id'])}\n{STATE_LABELS.get(pair['state'], pair['state'])}"
                st.button(label, key=f'split_{pid}_{pair["a_pk"]}_{pair["b_pk"]}',
                          type='primary' if active else 'secondary', use_container_width=True,
                          on_click=_set_selection, args=(pid,*_id(pair)))
    with right:
        selected = _active_pair(pid, rows)
        _detail(db, pid, selected, is_splink, selected_model, installed, multi, compact=True)


def _grid_view(db, pid, rows, is_splink, selected_model, installed, multi, view_scope):
    st.caption('Zeile auswählen, um den Detailvergleich rechts zu öffnen. Die Tabelle kann lokal sortiert werden.')
    left, right = st.columns([1.15, 1], gap='medium')
    with left:
        cycle = st.session_state.get(_key(pid, 'view_cycle'), 0)
        key = _key(pid, 'grid_' + view_scope + '_' + str(cycle))
        df = pd.DataFrame(grid_rows(rows, multi))
        current = st.session_state.get(_key(pid, 'selected'))
        current_idx = next((i for i,r in enumerate(rows) if _id(r) == current), 0)
        event = st.dataframe(df, hide_index=True, use_container_width=True,
                             height=560, selection_mode='single-row', on_select='rerun',
                             selection_default={'selection':{'rows':[current_idx]}}, key=key)
        if event.selection.rows:
            pos = event.selection.rows[0]
            if 0 <= pos < len(rows):
                _set_selection(pid, *_id(rows[pos]))
    with right:
        selected = _active_pair(pid, rows)
        _detail(db, pid, selected, is_splink, selected_model, installed, multi, compact=True)


def _cockpit(db, pid, rows, is_splink, selected_model, installed, multi, view_scope):
    counts = cockpit_counts(db,pid)
    processed = counts['bestaetigt'] + counts['abgelehnt'] + counts['unklar']
    total = counts['gesamt']
    st.markdown('#### Projektübersicht')
    m1,m2,m3,m4 = st.columns(4)
    m1.metric('Kandidaten insgesamt',f'{total:,}')
    m2.metric('Offen', f"{counts['offen']:,}")
    m3.metric('Bestätigt',f"{counts['bestaetigt']:,}")
    m4.metric('Mit KI-Vorschlag', f"{counts['ki_geprueft']:,}")
    st.progress(processed/total if total else 0.,
                text=f'{processed:,} von {total:,} Kandidaten wurden manuell eingeordnet')
    st.caption('Kategorien können sich überschneiden; KI-Vorschläge gelten nicht als manuelle Freigabe.')
    st.markdown('#### Arbeitskategorien')
    boxes = st.columns(3)
    categories = [
        ('Offene Kandidaten',counts['offen'],'offen','Alle'),
        ('Unklare Kandidaten',counts['unklar'],'unklar','Alle'),
        ('Mehrfachkandidaten',counts['mehrfachkandidaten'],'Alle','Mehrfachkandidaten'),
        ('Abweichende Hausnummer',counts['hausnummer_abweichung'],'Alle','Hausnummer-Abweichung'),
        ('Mit KI-Vorschlag',counts['ki_geprueft'],'Alle','KI-geprüft'),
        ('Abgelehnte Paare',counts['abgelehnt'],'abgelehnt','Alle'),
    ]
    for n,(label,number,state,risk) in enumerate(categories):
        with boxes[n%3]:
            with st.container(border=True):
                st.metric(label, f'{number:,}')
                st.button('In Data Grid prüfen', key=f'queue_{pid}_{n}',
                          use_container_width=True,disabled=(number==0),
                          on_click=_jump_to,args=(pid,'Data Grid',state,risk))
    st.divider()
    st.markdown('#### Aktueller Treffer aus der gewählten Arbeitsliste')
    st.caption('Die gemeinsamen Filter gelten auch hier. Du kannst im Cockpit direkt einen Treffer prüfen.')
    if rows:
        selected = _active_pair(pid, rows)
        labels = [f"{i+1}. {_title(r,is_splink)}" for i,r in enumerate(rows)]
        index = next((i for i,r in enumerate(rows) if _id(r) == _id(selected)),0)
        pos = st.selectbox('Treffer',range(len(rows)),index=index,
                           format_func=lambda i:labels[i],key=_key(pid, 'cockpit_'+view_scope))
        if _id(rows[pos]) != _id(selected):
            _set_selection(pid, *_id(rows[pos]))
        _detail(db, pid, rows[pos], is_splink, selected_model, installed, multi)
    else:
        st.info('Keine Treffer in der gewählten Arbeitsliste.')


def render_review(db, pid, is_splink, selected_model, installed):
    st.markdown('### Treffer prüfen')
    view = st.segmented_control('Ansicht', VIEW_NAMES, default='Data Grid',
                                key=_key(pid,'view'), selection_mode='single')
    if view not in VIEW_NAMES:
        view = 'Data Grid'
    last_view_key = _key(pid,'last_view')
    if st.session_state.get(last_view_key) != view:
        st.session_state[last_view_key] = view
        st.session_state[_key(pid,'view_cycle')] = st.session_state.get(_key(pid,'view_cycle'),0) + 1
    f_state, f_score, f_search, f_risk = st.columns([1,1,1.5,1.2])
    with f_state:
        state = st.selectbox('Status', STATES, format_func=lambda s: STATE_LABELS[s],
                             key=_key(pid,'status'))
    with f_score:
        minimum = st.slider('Mindestwert (0–100)', 0,100, 5 if is_splink else 60,
                             step=5,key=_key(pid,'score'))
    with f_search:
        search = st.text_input('Firmenname, CRM-ID oder Ort suchen', key=_key(pid,'search'))
    with f_risk:
        risk = st.selectbox('Arbeitskategorie', RISKS, key=_key(pid,'risk'))
    st.caption('Splink: modellbasierter Score, nicht unabhängig validiert.' if is_splink
               else 'RapidFuzz: heuristischer Ähnlichkeitswert, keine Wahrscheinlichkeit.')
    _batch_review(db,pid,is_splink,selected_model,installed)

    size_col, page_col = st.columns([1,1])
    with size_col:
        page_size = st.selectbox('Treffer je Seite', [20,25,50,100], index=1,
                                  key=_key(pid,'size'))
    # Query once for the current page; only repeat after a page-number change.
    page_key = _key(pid,'page')
    requested = st.session_state.get(page_key, 1)
    total, pages, resolved, rows = pair_page(db, pid, state, minimum, search, risk,
                                             page=requested, page_size=page_size)
    if requested != resolved:
        st.session_state[page_key] = resolved
    with page_col:
        page = st.number_input('Seite',min_value=1,max_value=pages,value=1,step=1,
                               key=page_key)
    if page != resolved:
        total, pages, resolved, rows = pair_page(db, pid, state, minimum, search, risk,
                                                page=page, page_size=page_size)
    page = resolved
    st.caption(f'{total:,} Kandidaten · Seite {page} / {pages} · Alle Ansichten verwenden dieselben Filter und Prüfentscheide.')
    if not rows and view != 'Review-Cockpit':
        st.info('Keine Kandidaten für diese Filter. Status, Mindestwert oder Arbeitskategorie anpassen.')
        return
    multi = conflict_flags(db,pid,rows)
    scope = hashlib.sha256(f'{pid}:{state}:{minimum}:{search}:{risk}:{page}:{page_size}'.encode()).hexdigest()[:16]
    if view == 'Split-View':
        _split_view(db,pid,rows,is_splink,selected_model,installed,multi)
    elif view == 'Review-Cockpit':
        _cockpit(db,pid,rows,is_splink,selected_model,installed,multi,scope)
    else:
        _grid_view(db,pid,rows,is_splink,selected_model,installed,multi,scope)
