"""Two independently auditable migration checks for CRM A -> CRM B.

No CRM API calls, no mutation of source records. The B identifier (if known)
is saved in CRM A. A missing B reference is different from a missing B address.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter

import pandas as pd

from engine import now, norm, norm_name, norm_phone, norm_street, norm_uid, plain

FIELD_LABELS = {
    'company': 'Firma', 'first_name': 'Vorname', 'last_name': 'Nachname',
    'street_name': 'Strassenname', 'house_number': 'Hausnummer',
    'postcode': 'PLZ', 'city': 'Ort', 'country': 'Land',
    'email': 'E-Mail', 'phone': 'Telefon', 'uid': 'UID',
}


def ensure_schema(db):
    """Non-destructive migration of version 1.6 SQLite files."""
    cols = {r['name'] for r in db.execute('PRAGMA table_info(projects)')}
    if 'mode' not in cols:
        db.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'legacy'")
    if 'ref_column' not in cols:
        db.execute("ALTER TABLE projects ADD COLUMN ref_column TEXT NOT NULL DEFAULT ''")
    rcols = {r['name'] for r in db.execute('PRAGMA table_info(records)')}
    if 'b_reference' not in rcols:
        db.execute("ALTER TABLE records ADD COLUMN b_reference TEXT NOT NULL DEFAULT ''")
    if 'in_scope' not in rcols:
        db.execute('ALTER TABLE records ADD COLUMN in_scope INTEGER NOT NULL DEFAULT 1')
    db.executescript('''
    CREATE INDEX IF NOT EXISTS idx_migration_reference ON records(project_id,side,b_reference);
    CREATE TABLE IF NOT EXISTS reference_checks(
        project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        a_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
        b_pk INTEGER REFERENCES records(pk) ON DELETE CASCADE,
        b_ref TEXT NOT NULL,
        result TEXT NOT NULL,
        differences_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id,a_pk)
    );
    CREATE INDEX IF NOT EXISTS idx_reference_checks ON reference_checks(project_id,result);
    ''')
    db.commit()


def is_dual(db, pid):
    row = db.execute('SELECT mode FROM projects WHERE project_id=?', (pid,)).fetchone()
    return bool(row and row['mode'] == 'dual')


def missing_reference(value, markers=()):
    text = plain(value)
    return not text or text.casefold() in {plain(x).casefold() for x in markers if plain(x)}


def _normalized(field, value, record):
    if field == 'company':
        return norm_name(value)
    if field in ('first_name', 'last_name'):
        return norm_name(value)
    if field == 'street_name':
        return record['street_name'] or norm_street(value)
    if field == 'house_number':
        return record['house_number']
    if field == 'postcode':
        return record['postal']
    if field == 'city' or field == 'country':
        return norm(value)
    if field == 'email':
        return plain(value).lower()
    if field == 'phone':
        return norm_phone(value)
    if field == 'uid':
        return norm_uid(value)
    return plain(value)


def _original_for_field(raw, field):
    if field == 'street_name':
        return raw.get('street', '')
    return raw.get(field, '')


def differences_between(a, b):
    """Return field-level comparison including missing values; never silently merge."""
    ra, rb = json.loads(a['original_json']), json.loads(b['original_json'])
    output = []
    for field, label in FIELD_LABELS.items():
        left = plain(_original_for_field(ra, field))
        right = plain(_original_for_field(rb, field))
        na, nb = _normalized(field, left, a), _normalized(field, right, b)
        if not left and not right and not na and not nb:
            continue
        if field == 'house_number':
            # When only a combined address is present, raw house fields are
            # empty, but a parsed number must still be compared.
            left_compare = left or na
            right_compare = right or nb
        else:
            left_compare = left
            right_compare = right
        if not na and not nb:
            continue
        if left_compare == right_compare and left_compare:
            state = 'identisch'
        elif not na or not nb:
            state = 'fehlt'
        elif na == nb:
            state = 'normalisiert_gleich'
        else:
            state = 'abweichend'
        output.append({'feld': field, 'bezeichnung': label,
                       'a_wert': left or (na if field == 'house_number' else ''),
                       'b_wert': right or (nb if field == 'house_number' else ''),
                       'a_normalisiert': na, 'b_normalisiert': nb,
                       'status': state})
    return output


def run_reference_check(db, pid):
    """Workflow 2: inspect A->B references directly. All data remains local."""
    if not is_dual(db, pid):
        raise ValueError('Referenzprüfung nur für Projekte im Modus A → B.')
    brows = {row['source_id']: row for row in db.execute(
        "SELECT * FROM records WHERE project_id=? AND side='B'", (pid,))}
    arows = db.execute(
        "SELECT * FROM records WHERE project_id=? AND side='A' AND b_reference<>'' ORDER BY pk", (pid,)).fetchall()
    counts = Counter(row['b_reference'] for row in arows)
    buffer = []
    with db:
        db.execute('DELETE FROM reference_checks WHERE project_id=?', (pid,))
        for a in arows:
            b = brows.get(a['b_reference'])
            details = differences_between(a, b) if b is not None else []
            if not a['in_scope']:
                result = 'ausgeschlossen'
            elif counts[a['b_reference']] > 1:
                result = 'mehrfachreferenz'
            elif b is None:
                result = 'referenz_fehlt'
            elif any(row['status'] in ('fehlt', 'abweichend') for row in details):
                result = 'abweichend'
            elif any(row['status'] == 'normalisiert_gleich' for row in details):
                result = 'normalisiert_gleich'
            else:
                result = 'identisch'
            buffer.append((pid,a['pk'],b['pk'] if b else None,a['b_reference'],result,
                          json.dumps(details, ensure_ascii=False),now()))
            if len(buffer) >= 1000:
                db.executemany('''INSERT INTO reference_checks
                      (project_id,a_pk,b_pk,b_ref,result,differences_json,updated_at)
                      VALUES(?,?,?,?,?,?,?)''', buffer)
                buffer.clear()
        if buffer:
            db.executemany('''INSERT INTO reference_checks
                  (project_id,a_pk,b_pk,b_ref,result,differences_json,updated_at)
                  VALUES(?,?,?,?,?,?,?)''', buffer)
    return len(arows)


def reference_overview(db, pid):
    rows = db.execute('SELECT result,count(*) n FROM reference_checks WHERE project_id=? GROUP BY result',
                      (pid,)).fetchall()
    return {row['result']: row['n'] for row in rows}


def reference_page(db, pid, result='Alle', limit=100, offset=0):
    where = 'r.project_id=?' + (" AND r.result=?" if result != 'Alle' else '')
    params = [pid] + ([result] if result != 'Alle' else [])
    total = db.execute('SELECT count(*) FROM reference_checks r WHERE '+where, params).fetchone()[0]
    rows = db.execute('''SELECT r.*, a.source_id a_id, b.source_id b_id,
                        a.original_json a_json, b.original_json b_json
                        FROM reference_checks r JOIN records a ON a.pk=r.a_pk
                        LEFT JOIN records b ON b.pk=r.b_pk
                        WHERE '''+where+' ORDER BY r.a_pk LIMIT ? OFFSET ?',
                      [*params,limit,offset]).fetchall()
    return total, rows


def _csv_bytes(rows, fields=None):
    output = io.StringIO()
    if rows or fields:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else fields,
                                delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def export_references(db, pid, only_differences=False):
    query = '''SELECT a.source_id a_id,r.b_ref,COALESCE(b.source_id,'') b_id,
               r.result,r.differences_json
               FROM reference_checks r JOIN records a ON a.pk=r.a_pk
               LEFT JOIN records b ON b.pk=r.b_pk WHERE r.project_id=?'''
    if only_differences:
        query += " AND r.result NOT IN ('identisch','ausgeschlossen')"
    query += ' ORDER BY a.source_id'
    data=[]
    for row in db.execute(query,(pid,)):
        differences=json.loads(row['differences_json'])
        diff = [x for x in differences if x['status'] in ('abweichend','fehlt')]
        data.append({'crm_a_id': row['a_id'], 'b_referenz_in_a': row['b_ref'],
                     'crm_b_id':row['b_id'], 'ergebnis':row['result'],
                     'abweichende_felder': ', '.join(x['bezeichnung'] for x in diff),
                     'abweichungen_json': json.dumps(diff,ensure_ascii=False)})
    return _csv_bytes(data,fields=['crm_a_id','b_referenz_in_a','crm_b_id','ergebnis',
                    'abweichende_felder','abweichungen_json'])


def export_reference_field_differences(db,pid):
    data=[]
    for row in db.execute('''SELECT a.source_id a_id, r.b_ref, r.result,r.differences_json
        FROM reference_checks r JOIN records a ON a.pk=r.a_pk WHERE r.project_id=?
        ORDER BY a.source_id''',(pid,)):
        for d in json.loads(row['differences_json']):
            if d['status'] in ('abweichend','fehlt','normalisiert_gleich'):
                data.append({'crm_a_id':row['a_id'],'b_referenz':row['b_ref'],
                             'referenzstatus':row['result'],'feld':d['bezeichnung'],
                             'wert_a':d['a_wert'],'wert_b':d['b_wert'],'vergleich':d['status']})
    return _csv_bytes(data,fields=['crm_a_id','b_referenz','referenzstatus','feld','wert_a','wert_b','vergleich'])


def workflow1_inventory(db, pid):
    """Every A record gets a status. No match != safe to create in B."""
    if not is_dual(db,pid):
        raise ValueError('Nur für A → B Projekte')
    cur = db.execute('''SELECT a.source_id a_id,a.b_reference,a.in_scope,
        count(p.b_pk) candidates,
        sum(CASE WHEN p.state='bestaetigt' THEN 1 ELSE 0 END) confirmed,
        max(CASE WHEN p.state='bestaetigt' THEN b.source_id ELSE NULL END) matched_b
        FROM records a
        LEFT JOIN pairs p ON p.project_id=a.project_id AND p.a_pk=a.pk
        LEFT JOIN records b ON b.pk=p.b_pk
        WHERE a.project_id=? AND a.side='A'
        GROUP BY a.pk ORDER BY a.source_id''',(pid,))
    out=[]
    for row in cur:
        if not row['in_scope']:
            status='ausgeschlossen'
        elif row['b_reference']:
            status='referenz_vorhanden'
        elif row['confirmed']:
            status='zuordnung_bestaetigt'
        elif row['candidates']:
            status='treffer_offen'
        else:
            status='kein_kandidat_nicht_freigegeben'
        out.append({'crm_a_id':row['a_id'],'b_referenz_in_a':row['b_reference'],
                    'status':status,'gefundene_kandidaten':row['candidates'],
                    'b_id_bestaetigt':row['matched_b'] or ''})
    return out


def export_workflow1_inventory(db,pid):
    return _csv_bytes(workflow1_inventory(db,pid))


def reference_conflict(db, pid, a_pk, b_pk):
    """Prevent linking B to another A when A already references it directly."""
    row = db.execute('SELECT source_id FROM records WHERE pk=? AND project_id=? AND side=?',
                     (b_pk,pid,'B')).fetchone()
    if not row:
        return True
    return db.execute('''SELECT 1 FROM records WHERE project_id=? AND side='A'
        AND pk<>? AND b_reference=? LIMIT 1''',
        (pid,a_pk,row['source_id'])).fetchone() is not None
