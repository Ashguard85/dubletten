"""Unified, compact Streamlit focus review for the existing CRM-Dubletten app.

Runs only after the application's normal password check. No browser JS, remote
requests, background worker, or automatic CRM changes. Existing review views
and their decisions remain available in the original project tabs.
"""
from __future__ import annotations

import json
import hashlib

import pandas as pd
import streamlit as st

from focus_data import (
    REFERENCE_RESULTS, REFERENCE_DECISIONS,
    reference_focus_page, save_reference_review, export_reference_reviews,
    next_position,
)

MODES = ('CRM – fehlende Referenzen', 'CRM – vorhandene Referenzen',
         'Schweizer Adressprüfung')


FOCUS_CSS = '''<style>
/* Minimal on-screen review with no nested 600px tables or marketing blocks. */
.block-container {max-width:1500px; padding-top:.65rem !important; padding-bottom:.4rem !important}
[data-testid="stHeader"] {height:1.2rem}
[data-testid="stVerticalBlock"] {gap:.42rem}
[data-testid="stHorizontalBlock"] {gap:.5rem}
[data-testid="stMetric"] {padding:.2rem .5rem !important}
div[data-testid="stForm"] {border:0; padding:0;}
section[data-testid="stSidebar"] {min-width:220px;}

/* Address focus: one original excerpt + four brief evidence-coloured rows. */
.fsv-card {border:1px solid #dce5e9;border-radius:10px;overflow:hidden;margin:.28rem 0 .3rem}
.fsv-source {background:#f2f6f8;padding:10px 16px;border-bottom:1px solid #dce5e9}
.fsv-eyebrow {font-size:10px;letter-spacing:.7px;font-weight:700;color:#617587}
.fsv-original-line {font-size:18px;font-weight:750;color:#19394e;line-height:1.35;overflow-wrap:anywhere}
.fsv-original-place {font-size:13px;color:#415c70;font-weight:600;line-height:1.35}
.fsv-comparison {padding:4px 12px 8px}
.fsv-label-row,.fsv-compare-row {display:grid;grid-template-columns:16% 25% 3% 30% 26%;align-items:center;column-gap:0;padding:4px 5px}
.fsv-label-row {font-size:10px;color:#738494;font-weight:700;border-bottom:1px solid #e8edf1}
.fsv-compare-row {min-height:36px;border-bottom:1px solid #edf1f3;font-size:12px}
.fsv-compare-row:last-child {border-bottom:0}
.fsv-field {font-weight:700;color:#334d5f}
.fsv-value,.fsv-target {padding:5px 7px;border-radius:5px;overflow-wrap:anywhere}
.fsv-target {font-weight:700}
.fsv-arrow {text-align:center;color:#93a3ae}
.fsv-green {background:#e8f5eb;color:#1c6c3c}
.fsv-amber {background:#fff1d6;color:#80540b}
.fsv-red {background:#fce8e7;color:#9c2d31}
.fsv-neutral {background:#edf2f5;color:#516577}
.fsv-status {font-size:10.5px;font-weight:700;padding-left:8px}
.fsv-status-green {color:#238047}
.fsv-status-amber {color:#956214}
.fsv-status-red {color:#a13439}
.fsv-status-neutral {color:#708391}
.fsv-alternatives {background:#fff9eb;border-top:1px solid #f1dfb8;padding:8px 14px;font-size:10.5px;color:#7a551d}
.fsv-number {display:inline-block;background:#fff1d0;border:1px solid #ebd29f;padding:2px 7px;border-radius:12px;margin:3px 3px 0;font-weight:700}
.fsv-hint {background:#f5f8fa;border-top:1px solid #e5ebed;padding:7px 14px;color:#657787;font-size:11px}
@media (max-width:750px){
.fsv-label-row,.fsv-compare-row {grid-template-columns:18% 24% 3% 31% 24%;font-size:11px}
.fsv-status{font-size:9px;padding-left:4px}
.fsv-original-line{font-size:15px}
}
</style>'''


def _caption(value):
    return str(value or '—')


def _short(text, limit=75):
    text = str(text or '').replace('\n', ' ').strip()
    return text if len(text) <= limit else text[:limit-1] + '…'


def _raw(row, key):
    try:
        return json.loads(row[key]) if row[key] else {}
    except (ValueError, TypeError, KeyError):
        return {}


def _head(raw, fallback=''):
    name = str(raw.get('company') or '').strip()
    if not name:
        name = ' '.join(str(raw.get(k) or '').strip() for k in ('first_name', 'last_name')).strip()
    return name or str(fallback or '—')


def _plain_rows(a, b, a_house='', b_house=''):
    return [
        ('Firma / Person', _head(a), _head(b)),
        ('Strasse', a.get('street',''), b.get('street','')),
        ('Hausnummer', a_house or a.get('house_number',''), b_house or b.get('house_number','')),
        ('PLZ / Ort', f"{a.get('postcode','')} {a.get('city','')}", f"{b.get('postcode','')} {b.get('city','')}"),
    ]


def _table_rows(items, title_a='CRM A', title_b='CRM B'):
    rows = []
    for field, left, right in items:
        left, right = _caption(left), _caption(right)
        rows.append({'Feld': field, title_a: _short(left), title_b: _short(right),
                     'Vergleich': '✓' if left.casefold()==right.casefold() else '≠'})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 height=min(230, 36 + 35*len(rows)))


def _crm_queue(db, pid, filters, position):
    from review_data import pair_page
    size = 25
    total, _, _, rows = pair_page(
        db, pid, state=filters['status'], min_score=filters['score'],
        search=filters['search'], risk=filters['risk'],
        page=position//size+1, page_size=size)
    if position >= total:
        return total, None
    return total, rows[position % size] if len(rows)>position%size else None


def _swiss_queue(db, job_id, filters, position):
    from address_service import page_rows
    total, rows = page_rows(db, job_id, status=filters['result'],
                            decision=filters['status'], needle=filters['search'],
                            offset=position, limit=1)
    return total, rows[0] if rows else None


def _queue(db, mode, identity, filters, position):
    if mode == MODES[0]:
        return _crm_queue(db, identity, filters, position)
    if mode == MODES[1]:
        return reference_focus_page(db, identity,
                                    filters['result'], filters['status'],
                                    filters['search'], position)
    return _swiss_queue(db, identity, filters, position)


def _primary_diff(a, b):
    fields = [('E-Mail','email'), ('UID','uid'), ('Telefon','phone'),
              ('Vorname','first_name'), ('Nachname','last_name'), ('Land','country')]
    return [(label, a.get(field,''), b.get(field,'')) for label,field in fields
            if (a.get(field,'') or b.get(field,'')) and
               str(a.get(field,'')).casefold() != str(b.get(field,'')).casefold()]


def _case_crm(db, pid, row):
    a, b = _raw(row,'a_json'), _raw(row,'b_json')
    a_id, b_id = row['a_id'], row['b_id']
    head, info = st.columns([5, 2])
    head.text(f'{_short(_head(a, a_id), 100)}  ↔  {_short(_head(b, b_id), 100)}')
    info.caption(f"Score {row['score']}/100 · {_caption(row['state'])}")
    st.text(f'CRM A: {a_id}     ·     CRM B: {b_id}')
    _table_rows(_plain_rows(a,b,row['a_house'],row['b_house']))
    with st.expander('Zusätzliche Unterschiede und Vergleichshinweise', expanded=False):
        extra = _primary_diff(a,b)
        if extra:
            _table_rows(extra)
        st.text('Algorithmische Hinweise: ' + _caption(row['reason']))
        if row['llm_decision']:
            st.text('Früherer KI-Vorschlag (nicht verbindlich): '+_caption(row['llm_decision']))
    if row['a_house'] and row['b_house'] and row['a_house'] != row['b_house']:
        st.caption('⚠️ Unterschiedliche Hausnummer – manuell kontrollieren.')


def _case_reference(db, pid, row):
    st.text(f"A {row['a_id']}  →  B {_caption(row['b_ref'])}")
    st.caption(f"Referenzprüfung: {row['result']}  ·  Fachentscheid: {row['review_decision']}")
    try:
        diffs = json.loads(row['differences_json'] or '[]')
    except (ValueError, TypeError):
        diffs = []
    important = [d for d in diffs if d.get('status') in ('abweichend','fehlt')]
    others = [d for d in diffs if d not in important]
    if row['result']=='referenz_fehlt':
        st.warning('Die B-Referenz ist im B-Export nicht vorhanden.')
    elif not important:
        st.caption('Keine inhaltlich abweichenden Felder gefunden.')
    if important:
        st.dataframe(pd.DataFrame([{'Feld':d.get('bezeichnung',d.get('feld','')),
                 'CRM A':_short(d.get('a_wert','')), 'CRM B':_short(d.get('b_wert',''))}
                 for d in important[:7]]), hide_index=True, use_container_width=True,
                 height=min(245, 37+36*min(7,len(important))))
    with st.expander('Alle Felder und Originalwerte', expanded=False):
        if len(important)>7:
            st.dataframe(pd.DataFrame(important[7:]),hide_index=True,use_container_width=True)
        if others:
            st.dataframe(pd.DataFrame([{'Feld':d.get('bezeichnung', ''),
                  'CRM A':d.get('a_wert',''), 'CRM B':d.get('b_wert',''),
                  'Status':d.get('status','')} for d in others]),hide_index=True,use_container_width=True)
        st.caption('Originaldaten')
        st.json({'A':_raw(row,'a_json'),'B':_raw(row,'b_json')})


def _case_swiss(db, row):
    from focus_swiss_view import render_swiss_case
    return render_swiss_case(db, row)


def _scope_key(mode, identity):
    return f'focus::{mode}::{identity}'


def _mode_choices(db):
    projects=[dict(x) for x in db.execute('SELECT project_id,title FROM projects ORDER BY project_id DESC')]
    from migration import is_dual
    dual=[p for p in projects if is_dual(db,p['project_id'])]
    try:
        from address_service import list_jobs
        jobs=list_jobs(db)
    except Exception:
        jobs=[]
    return projects,dual,jobs



@st.fragment
def _render_focus(db):
    projects, dual, jobs = _mode_choices(db)
    available = []
    if projects:
        available.append(MODES[0])
    if dual:
        available.append(MODES[1])
    if jobs:
        available.append(MODES[2])
    if not available:
        st.info('Noch keine Prüfdaten vorhanden. Bitte zuerst ein CRM-Projekt oder eine Schweizer Adressprüfung importieren.')
        return

    if st.session_state.get('focus_mode') not in available:
        st.session_state['focus_mode'] = available[0]
    swiss_route = st.session_state.get('_focus_route_swiss')
    if swiss_route and MODES[2] in available:
        st.session_state['focus_mode'] = MODES[2]
        matching = next((j for j in jobs if j['job_id'] == swiss_route), None)
        if matching:
            st.session_state[f'focus_selection_{available.index(MODES[2])}'] = matching
    x, y = st.columns([2, 3], gap='small')
    with x:
        mode = st.selectbox('Prüfart', available, key='focus_mode')
    candidates = jobs if mode == MODES[2] else dual if mode == MODES[1] else projects
    with y:
        choice = st.selectbox('Prüfauftrag / Projekt', candidates,
             format_func=lambda q: f"#{q.get('job_id',q.get('project_id'))} · {q['title']}",
             key=f'focus_selection_{available.index(mode)}')
    identity = choice.get('job_id', choice.get('project_id'))
    scope = _scope_key(mode, identity)
    token = hashlib.sha1(scope.encode()).hexdigest()[:12]
    applied_key = f'fap_{token}'
    cursor_key = f'fcur_{token}'
    applied = st.session_state.get(applied_key)

    with st.expander('⚙️ Filter wählen / ändern', expanded=applied is None):
        with st.form(f'focus_filters_{token}'):
            ca, cb, cc = st.columns([2, 2, 3])
            if mode == MODES[0]:
                with ca:
                    status = st.selectbox('Prüfstatus', ['offen','unklar','Alle','bestaetigt','abgelehnt'])
                with cb:
                    from review_data import RISKS
                    risk = st.selectbox('Risiko', list(RISKS))
                with cc:
                    search = st.text_input('Name / ID / Ort')
                min_score = st.slider('Min. Match-Score',0,100,50)
                opts = dict(status=status,risk=risk,search=search,score=min_score)
            elif mode == MODES[1]:
                with ca:
                    status = st.selectbox('Prüfentscheid',list(REFERENCE_DECISIONS)+['Alle'])
                with cb:
                    result = st.selectbox('Referenzergebnis',REFERENCE_RESULTS)
                with cc:
                    search = st.text_input('A-ID oder B-Referenz')
                opts = dict(status=status,result=result,search=search)
            else:
                with ca:
                    status = st.selectbox('Prüfentscheid', ['offen','uebersprungen','Alle',
                              'korrigiert','keine_aenderung','massenfreigegeben'])
                with cb:
                    from swisstopo import RESULT_LABELS
                    statuses=['Alle',*sorted(RESULT_LABELS), 'strasse_aehnlich','eingabe_konflikt']
                    result=st.selectbox('swisstopo-Ergebnis',statuses)
                with cc:
                    search=st.text_input('Adresse oder ID')
                opts=dict(status=status,result=result,search=search)
            go=st.form_submit_button('🎯 Prüfmodus starten / Filter übernehmen',type='primary')
        if go:
            st.session_state[applied_key]=opts
            st.session_state[cursor_key]=0
            st.rerun(scope='fragment')

    if not applied:
        st.caption('Zuerst die Filter einstellen, dann startet die Einzelprüfung.')
        return

    if mode == MODES[2]:
        with st.expander('✓ Exakte swisstopo-Massenfreigabe', expanded=False):
            st.caption('Nur vollständige, eindeutige, aktuelle amtliche Treffer. Keine Fuzzy-Treffer. Originaldaten bleiben unverändert.')
            from address_service import bulk_approval_preview, bulk_approve_exact
            pv_key=f'focus_bulk_{token}'
            if st.button('Freigabefähige Adressen zählen',key=f'focus_bulk_pre_{token}'):
                try:
                    st.session_state[pv_key]=bulk_approval_preview(db,identity)
                except Exception as exc:
                    st.error(str(exc))
            preview=st.session_state.get(pv_key)
            if preview:
                st.caption(f"{preview['count']:,} exakt passende Adressen")
                ok=st.checkbox('Freigabe dieser eindeutig geprüften Gebäudeadressen bestätigen',
                               key=f'focus_bulk_approve_{token}')
                if st.button('Massenfreigabe ausführen',disabled=not ok or not preview['count'],
                             type='primary',key=f'focus_bulk_exec_{token}'):
                    try:
                        bulk_approve_exact(db,identity,preview['count'],preview['digest'])
                        st.session_state.pop(pv_key,None)
                        st.session_state[cursor_key]=0
                        st.success('Massenfreigabe gespeichert; alle Fälle bleiben nachvollziehbar.')
                        st.rerun(scope='fragment')
                    except Exception as exc:
                        st.session_state.pop(pv_key,None)
                        st.error(str(exc))

    pos=int(st.session_state.get(cursor_key,0))
    try:
        count, row = _queue(db,mode,identity,applied,pos)
    except Exception as exc:
        st.error(f'Arbeitsliste konnte nicht geladen werden: {exc}')
        return
    # Header and navigation fit on one line and remain at a stable position.
    c1,c2,c3,c4=st.columns([3,1,1,1], gap='small')
    with c1:
        st.caption(f'**{min(pos+1,count) if count else 0} / {count}** · Filter aktiv · Einzelfallprüfung')
    with c2:
        if st.button('← Zurück',disabled=pos<1,use_container_width=True,key=f'fp_{token}'):
            st.session_state[cursor_key]=max(0,pos-1)
            st.rerun(scope='fragment')
    with c3:
        if st.button('Weiter →',disabled=pos>=count,use_container_width=True,key=f'fn_{token}'):
            st.session_state[cursor_key]=pos+1
            st.rerun(scope='fragment')
    with c4:
        if st.button('Zum Start',disabled=pos==0,use_container_width=True,key=f'fs_{token}'):
            st.session_state[cursor_key]=0
            st.rerun(scope='fragment')
    if not count:
        st.success('Keine Fälle im aktuellen Filter. Filter anpassen oder anderen Auftrag wählen.')
        return
    if row is None:
        st.success('Ende der gefilterten Arbeitsliste erreicht. Über „Zurück“ kannst du Fälle erneut prüfen.')
        return

    try:
        if mode==MODES[0]:
            _case_crm(db,identity,row)
            ca,cb,cc,cd,ce=st.columns(5,gap='small')
            did_ok=ca.button('✓ Bestätigen',type='primary',use_container_width=True,key=f'fok_{token}')
            did_no=cb.button('✕ Ablehnen',use_container_width=True,key=f'fno_{token}')
            did_unclear=cc.button('? Unklar',use_container_width=True,key=f'fud_{token}')
            did_skip=cd.button('→ Überspringen',use_container_width=True,key=f'fsk_{token}')
            did_reset=ce.button('↩ Zurücksetzen',use_container_width=True,key=f'frs_{token}')
            action=('bestaetigt' if did_ok else 'abgelehnt' if did_no else 'unklar' if did_unclear
                    else 'skip' if did_skip else 'offen' if did_reset else None)
            if action:
                if action!='skip':
                    from engine import set_review
                    set_review(db,identity,row['a_pk'],row['b_pk'],action)
                new_total,_=_queue(db,mode,identity,applied,pos)
                visible=(action=='skip' or applied['status']=='Alle' or applied['status']==action)
                st.session_state[cursor_key]=next_position(pos,new_total,visible)
                st.rerun(scope='fragment')
        elif mode==MODES[1]:
            _case_reference(db,identity,row)
            with st.expander('Notiz zur fachlichen Prüfung',expanded=False):
                note=st.text_input('Notiz (optional, maximal 1000 Zeichen)',
                   value=row['review_note'] or '', key=f'fnote_{identity}_{row["a_pk"]}',max_chars=1000)
            if not st.session_state.get(f'fnote_{identity}_{row["a_pk"]}'):
                note=row['review_note'] or ''
            ca,cb,cc,cd,ce=st.columns(5,gap='small')
            did_ok=ca.button('✓ Geprüft',type='primary',use_container_width=True,key=f'frefok_{token}')
            did_corr=cb.button('! Korrektur',use_container_width=True,key=f'frefcorr_{token}')
            did_unclear=cc.button('? Unklar',use_container_width=True,key=f'frefunk_{token}')
            did_skip=cd.button('→ Überspringen',use_container_width=True,key=f'frefskip_{token}')
            did_reset=ce.button('↩ Zurück',use_container_width=True,key=f'frefreset_{token}')
            action=('geprueft' if did_ok else 'korrektur_noetig' if did_corr else 'unklar' if did_unclear
                    else 'skip' if did_skip else 'offen' if did_reset else None)
            if action:
                new_decision='uebersprungen' if action=='skip' else action
                save_reference_review(db,identity,row['a_pk'],new_decision,note)
                new_total,_=_queue(db,mode,identity,applied,pos)
                visible=(applied['status']=='Alle' or applied['status']==new_decision)
                st.session_state[cursor_key]=next_position(pos,new_total,visible)
                st.rerun(scope='fragment')
            with st.expander('Prüfprotokoll exportieren',expanded=False):
                exp_key=f'focus_export_{token}'
                if st.button('CSV vorbereiten',key=f'fprep_{token}'):
                    st.session_state[exp_key]=export_reference_reviews(db,identity)
                if exp_key in st.session_state:
                    st.download_button('CSV herunterladen',st.session_state[exp_key],
                        file_name=f'referenzpruefung_{identity}.csv',mime='text/csv')
        else:
            action,correction=_case_swiss(db,row)
            if action is not None:
                from address_service import save_review
                save_review(db,identity,row['row_pk'],action,correction)
                st.session_state.pop(f'focus_bulk_{token}',None)
                new_total,_=_queue(db,mode,identity,applied,pos)
                visible=(applied['status']=='Alle' or applied['status']==action)
                st.session_state[cursor_key]=next_position(pos,new_total,visible)
                st.rerun(scope='fragment')
            with st.expander('Adressliste exportieren',expanded=False):
                from address_service import export_job
                exp_key=f'focus_export_swiss_{token}'
                if st.button('Prüf-CSV vorbereiten',key=f'fswprep_{token}'):
                    st.session_state[exp_key]=export_job(db,identity,split_house=True)
                if exp_key in st.session_state:
                    st.download_button('CSV herunterladen',st.session_state[exp_key],
                                       file_name=f'adresspruefung_{identity}.csv',mime='text/csv')
    except ValueError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f'Bewertung fehlgeschlagen: {exc}')


def render_focus_workspace(db):
    st.markdown(FOCUS_CSS, unsafe_allow_html=True)
    if st.session_state.get('_focus_route_swiss'):
        if st.button('← Zurück zur Schweizer Adressprüfung', key='focus_exit_swiss', type='secondary'):
            st.session_state.pop('_focus_route_swiss', None)
            st.session_state['_focus_active'] = False
            st.session_state['_next_nav'] = '📬 Schweizer Adressprüfung'
            st.rerun()
    st.markdown('**🎯 Fokusprüfung** · ein Fall nach dem anderen')
    _render_focus(db)
