"""Read-only focus queues and audited manual decisions, on the existing SQLite DB.

Does not modify CRM A/B data, existing matching decisions, or swisstopo snapshots.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

REFERENCE_RESULTS = (
    'Alle', 'identisch', 'normalisiert_gleich', 'abweichend',
    'referenz_fehlt', 'mehrfachreferenz', 'ausgeschlossen',
)
REFERENCE_DECISIONS = ('offen', 'geprueft', 'korrektur_noetig', 'unklar', 'uebersprungen')


def init_reference_reviews(db):
    db.execute('''CREATE TABLE IF NOT EXISTS focus_reference_decisions(
        project_id INTEGER NOT NULL,
        a_pk INTEGER NOT NULL,
        decision TEXT NOT NULL DEFAULT 'offen',
        note TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT NOT NULL,
        PRIMARY KEY(project_id, a_pk),
        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
        FOREIGN KEY(a_pk) REFERENCES records(pk) ON DELETE CASCADE
    )''')
    db.execute('''CREATE INDEX IF NOT EXISTS idx_focus_reference_decisions
                     ON focus_reference_decisions(project_id, decision)''')
    db.commit()


def _ref_where(pid, result='Alle', decision='offen', search=''):
    if result not in REFERENCE_RESULTS:
        raise ValueError('Unbekannter Referenzstatus.')
    if decision not in (*REFERENCE_DECISIONS, 'Alle'):
        raise ValueError('Unbekannter Prüfentscheid.')
    wh = ['r.project_id=?']
    params = [int(pid)]
    if result != 'Alle':
        wh.append('r.result=?')
        params.append(result)
    if decision != 'Alle':
        wh.append("COALESCE(d.decision,'offen')=?")
        params.append(decision)
    if search.strip():
        # Parameterized LIKE and bounded text; never interpret user search as SQL.
        needle = search.strip()[:120].casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        wh.append("(lower(a.source_id) LIKE ? ESCAPE '\\' OR lower(r.b_ref) LIKE ? ESCAPE '\\')")
        params.extend(['%' + needle + '%'] * 2)
    return ' AND '.join(wh), params


def reference_focus_page(db, pid, result='Alle', decision='offen', search='', offset=0):
    """One reviewed reference and the count, both filtered at SQL level."""
    init_reference_reviews(db)
    where, params = _ref_where(pid, result, decision, search)
    join = '''FROM reference_checks r
        JOIN records a ON a.pk=r.a_pk
        LEFT JOIN records b ON b.pk=r.b_pk
        LEFT JOIN focus_reference_decisions d
             ON d.project_id=r.project_id AND d.a_pk=r.a_pk
        WHERE ''' + where
    count = db.execute('SELECT count(*) ' + join, params).fetchone()[0]
    if count == 0 or offset >= count:
        return count, None
    row = db.execute('''SELECT r.*, a.source_id AS a_id, b.source_id AS b_id,
                a.original_json AS a_json, b.original_json AS b_json,
                COALESCE(d.decision, 'offen') AS review_decision, d.note AS review_note
                ''' + join + ' ORDER BY r.a_pk LIMIT 1 OFFSET ?',
                [*params, max(0, int(offset))]).fetchone()
    return count, row


def save_reference_review(db, pid, a_pk, decision, note=''):
    if decision not in REFERENCE_DECISIONS:
        raise ValueError('Nicht zulässiger Prüfentscheid.')
    if len(note) > 1000:
        raise ValueError('Notiz zu lang (maximal 1000 Zeichen).')
    found = db.execute('SELECT 1 FROM reference_checks WHERE project_id=? AND a_pk=?',
                       (pid, a_pk)).fetchone()
    if not found:
        raise ValueError('Referenzfall gehört nicht zu diesem Projekt.')
    init_reference_reviews(db)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db:
        db.execute('''INSERT INTO focus_reference_decisions
            (project_id, a_pk, decision, note, reviewed_at) VALUES (?,?,?,?,?)
            ON CONFLICT(project_id, a_pk) DO UPDATE SET
            decision=excluded.decision, note=excluded.note, reviewed_at=excluded.reviewed_at''',
            (pid, a_pk, decision, note.strip(), stamp))


def export_reference_reviews(db, pid):
    init_reference_reviews(db)
    data = io.StringIO(newline='')
    w = csv.writer(data, delimiter=';', lineterminator='\n')
    w.writerow(['A_ID', 'B_Referenz', 'B_ID', 'Referenzstatus',
                'Pruefentscheid', 'Notiz', 'Pruefzeitpunkt'])
    cursor = db.execute('''SELECT a.source_id, r.b_ref, COALESCE(b.source_id,''),
        r.result, COALESCE(d.decision,'offen'), COALESCE(d.note,''), COALESCE(d.reviewed_at,'')
        FROM reference_checks r JOIN records a ON a.pk=r.a_pk
        LEFT JOIN records b ON b.pk=r.b_pk
        LEFT JOIN focus_reference_decisions d ON d.project_id=r.project_id AND d.a_pk=r.a_pk
        WHERE r.project_id=? ORDER BY r.a_pk''', (pid,))
    for row in cursor:
        w.writerow([_csv_safe(value) for value in row])
    return data.getvalue().encode('utf-8-sig')


def _csv_safe(value):
    s = str(value if value is not None else '')
    if s.lstrip().startswith(('=', '-', '+', '@')) or s.startswith(('\t', '\r', '\n')):
        return "'" + s
    return s


def next_position(current, count_after, still_visible):
    """Next case after a decision; filtered-out cases shift into current slot.

    Returning count_after means the queue is finished, not an implicit wrap.
    """
    if count_after <= 0:
        return 0
    if still_visible:
        return min(current + 1, count_after)
    return min(current, count_after)
