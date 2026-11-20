"""Read-only queries for the three review views.

The existing SQLite schema is reused without schema changes. The page limit applies
at SQL level; 35k+ CRM records are not loaded into a Streamlit widget all at once.
"""
from __future__ import annotations

import math
import json
from engine import norm_name

STATES = ('offen', 'unklar', 'bestaetigt', 'abgelehnt', 'Alle')
STATE_LABELS = {
    'offen': 'Offen', 'unklar': 'Unklar', 'bestaetigt': 'Bestätigt',
    'abgelehnt': 'Abgelehnt', 'Alle': 'Alle',
}
RISKS = ('Alle', 'Mehrfachkandidaten', 'Hausnummer-Abweichung',
         'KI-geprüft', 'Ohne KI-Vorschlag')


def _filters(pid: int, state='offen', min_score=0, search='', risk='Alle'):
    """Construct SQL from whitelisted identifiers and bound user values only."""
    if state not in STATES:
        raise ValueError('Unbekannter Prüfstatus')
    if risk not in RISKS:
        raise ValueError('Unbekannte Arbeitskategorie')
    parts = ['p.project_id=?', 'p.score>=?']
    args: list = [int(pid), int(min_score)]
    if state != 'Alle':
        parts.append('p.state=?')
        args.append(state)
    if search and search.strip():
        text = search.strip()[:120]
        def escape_like(v):
            return v.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        literal = '%' + escape_like(text.casefold()) + '%'
        canonical_text = norm_name(text) if not any(c in text for c in ('%', '_', '\\')) else text.casefold()
        canonical = '%' + escape_like(canonical_text) + '%'
        parts.append('''(lower(a.source_id) LIKE ? ESCAPE '\\' OR
                          lower(b.source_id) LIKE ? ESCAPE '\\' OR
                          a.name LIKE ? ESCAPE '\\' OR b.name LIKE ? ESCAPE '\\' OR
                          lower(a.city) LIKE ? ESCAPE '\\' OR lower(b.city) LIKE ? ESCAPE '\\')''')
        args.extend((literal, literal, canonical, canonical, literal, literal))
    if risk == 'Mehrfachkandidaten':
        parts.append('''(EXISTS (SELECT 1 FROM pairs q
                             WHERE q.project_id=p.project_id AND q.a_pk=p.a_pk
                               AND q.b_pk<>p.b_pk)
                         OR EXISTS (SELECT 1 FROM pairs q
                             WHERE q.project_id=p.project_id AND q.b_pk=p.b_pk
                               AND q.a_pk<>p.a_pk))''')
    elif risk == 'Hausnummer-Abweichung':
        parts.append("(a.house_number<>'' AND b.house_number<>'' AND a.house_number<>b.house_number)")
    elif risk == 'KI-geprüft':
        parts.append("p.llm_decision<>''")
    elif risk == 'Ohne KI-Vorschlag':
        parts.append("p.llm_decision=''")
    return ' AND '.join(parts), args


def pair_page(db, pid, state='offen', min_score=0, search='', risk='Alle',
              page=1, page_size=25):
    if page_size not in (20, 25, 50, 100):
        raise ValueError('Ungültige Seitengrösse')
    if page < 1:
        raise ValueError('Seite muss positiv sein')
    where, args = _filters(pid, state, min_score, search, risk)
    from_clause = '''FROM pairs p
       JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
       WHERE ''' + where
    total = db.execute('SELECT count(*) ' + from_clause, args).fetchone()[0]
    total_pages = max(1, math.ceil(total/page_size))
    page = min(page, total_pages)
    rows = db.execute('''SELECT p.*, a.source_id a_id, b.source_id b_id,
                         a.original_json a_json, b.original_json b_json,
                         a.name a_name, b.name b_name,
                         a.house_number a_house, b.house_number b_house,
                         a.postal a_postal, b.postal b_postal
                        ''' + from_clause + '''
                        ORDER BY p.score DESC,p.a_pk,p.b_pk LIMIT ? OFFSET ?''',
                      [*args, page_size, (page-1)*page_size]).fetchall()
    return total, total_pages, page, rows


def conflict_flags(db, pid, rows):
    """Flag competing candidate pairs on the current page (not confirmed conflicts)."""
    if not rows:
        return set()
    a_ids = sorted({int(r['a_pk']) for r in rows})
    b_ids = sorted({int(r['b_pk']) for r in rows})
    a_ph = ','.join('?' for _ in a_ids)
    b_ph = ','.join('?' for _ in b_ids)
    a_counts = {int(r['a_pk']): r['n'] for r in db.execute(
        f'SELECT a_pk, COUNT(*) n FROM pairs WHERE project_id=? AND a_pk IN ({a_ph}) GROUP BY a_pk',
        [pid, *a_ids]).fetchall()}
    b_counts = {int(r['b_pk']): r['n'] for r in db.execute(
        f'SELECT b_pk, COUNT(*) n FROM pairs WHERE project_id=? AND b_pk IN ({b_ph}) GROUP BY b_pk',
        [pid, *b_ids]).fetchall()}
    return {(r['a_pk'], r['b_pk']) for r in rows
            if a_counts.get(r['a_pk'], 0) > 1 or b_counts.get(r['b_pk'], 0) > 1}


def cockpit_counts(db, pid):
    """Aggregate work queue counts; categories overlap and must not be summed."""
    counts = {'offen': 0, 'unklar': 0, 'bestaetigt': 0, 'abgelehnt': 0,
              'ki_geprueft': 0, 'hausnummer_abweichung': 0,
              'mehrfachkandidaten': 0, 'gesamt': 0}
    row = db.execute('''SELECT count(*) gesamt,
                     sum(case when p.state='offen' then 1 else 0 end) offen,
                     sum(case when p.state='unklar' then 1 else 0 end) unklar,
                     sum(case when p.state='bestaetigt' then 1 else 0 end) bestaetigt,
                     sum(case when p.state='abgelehnt' then 1 else 0 end) abgelehnt,
                     sum(case when p.llm_decision<>'' then 1 else 0 end) ki_geprueft
                     FROM pairs p WHERE p.project_id=?''', (pid,)).fetchone()
    for field in ('gesamt', 'offen', 'unklar', 'bestaetigt', 'abgelehnt', 'ki_geprueft'):
        counts[field] = row[field] or 0
    counts['hausnummer_abweichung'] = db.execute('''SELECT count(*) FROM pairs p
        JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
        WHERE p.project_id=? AND a.house_number<>'' AND b.house_number<>''
         AND a.house_number<>b.house_number''',(pid,)).fetchone()[0]
    counts['mehrfachkandidaten'] = db.execute('''SELECT count(*) FROM pairs p
        WHERE p.project_id=? AND (EXISTS
        (SELECT 1 FROM pairs q WHERE q.project_id=p.project_id AND q.a_pk=p.a_pk AND q.b_pk<>p.b_pk)
        OR EXISTS
        (SELECT 1 FROM pairs q WHERE q.project_id=p.project_id AND q.b_pk=p.b_pk AND q.a_pk<>p.a_pk))''',
        (pid,)).fetchone()[0]
    return counts


def display_name(raw_json, fallback):
    """Preserve the CRM spelling and capitalization in the UI."""
    try:
        raw = json.loads(raw_json)
    except (ValueError, TypeError):
        return str(fallback)
    company = str(raw.get('company') or '').strip()
    person = ' '.join(str(raw.get(field) or '').strip() for field in ('first_name','last_name')).strip()
    return company or person or str(fallback)


def grid_rows(rows, multi_keys):
    """Presentational rows; stable identifier tuple stays out of display DataFrame."""
    result = []
    for r in rows:
        a = display_name(r['a_json'], r['a_id'])
        b = display_name(r['b_json'], r['b_id'])
        issues = []
        if (r['a_pk'], r['b_pk']) in multi_keys:
            issues.append('Mehrfach')
        if r['a_house'] and r['b_house'] and r['a_house'] != r['b_house']:
            issues.append('Hausnr.')
        result.append({
            'Score': r['score'], 'CRM A': a, 'CRM B': b,
            'ID A': r['a_id'], 'ID B': r['b_id'],
            'Status': STATE_LABELS.get(r['state'], r['state']),
            'KI': r['llm_decision'] or '—', 'Hinweise': ', '.join(issues) or '—',
        })
    return result
