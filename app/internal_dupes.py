"""Single-export duplicate discovery and auditable review/cleanup exports.

All candidate pairs refer to two different records from side A of one project;
records are NEVER duplicated, edited, deleted, or merged.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import io
import json
from math import comb

import pandas as pd

from engine import import_project, score_pair, now, plain, _safe

# Conservative candidate caps, no quadratic full-file cross join.
BLOCKS = (('uid', 2500), ('email', 1500), ('phone', 500),
          ('zip_name', 450), ('zip_street', 250),
          ('city_name', 200), ('name_head', 100))


def is_internal(db, pid):
    row = db.execute('SELECT mode FROM projects WHERE project_id=?', (pid,)).fetchone()
    return bool(row and row['mode'] == 'internal')


def create_internal_project(db, title, frame, mapping, engine='rapidfuzz'):
    """Reuse validated CRM import, but store a single A-side only."""
    return import_project(db, title, frame, mapping, None, None,
                          engine=engine, mode='internal')


def _check(db, pid):
    if not is_internal(db, pid):
        raise ValueError('Das Projekt ist keine interne Dublettenprüfung.')


def run_internal_matching(db, pid, minimum=60, max_candidates=400000, progress=None):
    """Bounded SQL blocking; unordered pairs only (a_pk < b_pk), no self-links.

    Fuzzy scoring is reused from the existing CRM A/B workflow. No unbounded
    all-pairs Cartesian product is performed. A high-frequency blocking bucket
    is skipped rather than expanded beyond its rule-specific limit.
    """
    _check(db, pid)
    progress = progress or (lambda _: None)
    if not 0 <= minimum <= 100:
        raise ValueError('Schwellwert ausserhalb 0–100.')
    if max_candidates < 1:
        raise ValueError('Kandidatenlimit muss positiv sein.')
    with db:
        db.execute('CREATE TEMP TABLE IF NOT EXISTS internal_candidates(a_pk INTEGER NOT NULL, b_pk INTEGER NOT NULL, PRIMARY KEY (a_pk, b_pk)) WITHOUT ROWID')
        db.execute('DELETE FROM internal_candidates')
        try:
            for field, fanout in BLOCKS:
                buckets = defaultdict(list)
                for r in db.execute(f'''SELECT pk,{field} FROM records WHERE project_id=? AND side='A' AND {field}!='' AND in_scope=1''', (pid,)):
                    buckets[r[field]].append(r['pk'])
                buf = []
                for bucket in buckets.values():
                    if len(bucket) < 2 or comb(len(bucket), 2) > fanout:
                        continue
                    for i, a in enumerate(bucket):
                        for b in bucket[i + 1:]:
                            buf.append((min(a, b), max(a, b)))
                            if len(buf) >= 2000:
                                db.executemany('INSERT OR IGNORE INTO internal_candidates VALUES (?,?)', buf)
                                buf.clear()
                if buf:
                    db.executemany('INSERT OR IGNORE INTO internal_candidates VALUES (?,?)', buf)
                count = db.execute('SELECT count(*) FROM internal_candidates').fetchone()[0]
                if count > max_candidates:
                    raise ValueError('Zu viele interne Kandidaten. Blocking-Regeln oder Datenbestand eingrenzen.')
                progress(f'{field}: {count:,} mögliche Paare')
            examined = db.execute('SELECT count(*) FROM internal_candidates').fetchone()[0]
            # First collect/score; replace old results only after scoring succeeds.
            scored = []
            cur = db.execute('''SELECT a.*, b.pk AS target_pk, b.name AS b_name,
                b.street AS b_street, b.postal AS b_postal, b.city AS b_city,
                b.email AS b_email, b.phone AS b_phone, b.uid AS b_uid,
                b.street_number AS b_street_number
                FROM internal_candidates c
                JOIN records a ON a.pk=c.a_pk
                JOIN records b ON b.pk=c.b_pk
                ORDER BY c.a_pk,c.b_pk''')
            for row in cur:
                a = dict(row)
                b = {k: row['b_' + k] for k in ('name','street','postal','city','email','phone','uid','street_number')}
                score, reason = score_pair(a, b)
                if score >= minimum:
                    scored.append((pid, row['pk'], row['target_pk'], score, reason, now()))
            # Never silently erase existing human review decisions when rerun.
            if db.execute("SELECT 1 FROM pairs WHERE project_id=? AND state!='offen' LIMIT 1", (pid,)).fetchone():
                raise ValueError('Für dieses Projekt existieren manuelle Prüfentscheide. Bitte ein neues Projekt anlegen, statt sie durch einen Neulauf zu überschreiben.')
            db.execute('DELETE FROM pairs WHERE project_id=?', (pid,))
            db.executemany('''INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at)
                VALUES(?,?,?,?,?,?)''', scored)
            db.execute('UPDATE projects SET candidate_count=? WHERE project_id=?', (len(scored), pid))
        finally:
            db.execute('DROP TABLE IF EXISTS internal_candidates')
    return examined, len(scored)


def _confirmed_components(db, pid):
    """Connected components of explicitly confirmed pairwise links, not inferred identity."""
    _check(db, pid)
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    edges = list(db.execute('''SELECT a_pk,b_pk FROM pairs
        WHERE project_id=? AND state='bestaetigt' ORDER BY a_pk,b_pk''', (pid,)))
    for r in edges:
        union(r['a_pk'], r['b_pk'])
    groups = defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)
    components = sorted((sorted(nodes) for nodes in groups.values() if len(nodes) >= 2), key=lambda g: g[0])
    mapping = {}
    for i, nodes in enumerate(components, 1):
        for node in nodes:
            mapping[node] = f'G{i:05d}'
    return mapping, len(components), len(edges)


def _group_contradictions(db, pid, membership):
    """Explicitly rejected pairs inside a connected confirmed group are contradictions."""
    conflicts = defaultdict(int)
    if membership:
        for r in db.execute("SELECT a_pk,b_pk FROM pairs WHERE project_id=? AND state='abgelehnt'", (pid,)):
            a, b = membership.get(r['a_pk']), membership.get(r['b_pk'])
            if a and a == b:
                conflicts[a] += 1
    return conflicts


def group_overview(db, pid):
    mapping, count, edges = _confirmed_components(db, pid)
    by_group = defaultdict(list)
    conflicts = _group_contradictions(db, pid, mapping)
    if mapping:
        id_by_pk = {r['pk']: r['source_id'] for r in db.execute(
            "SELECT pk,source_id FROM records WHERE project_id=? AND side='A'", (pid,))}
        for pk, g in mapping.items():
            by_group[g].append(id_by_pk[pk])
    return [dict(Gruppe=g, Anzahl=len(ids), Original_IDs=' · '.join(sorted(ids)),
                 Widerspruechliche_Entscheide=conflicts.get(g, 0),
                 Hinweis=('Konflikt: abgelehntes Paar in bestätigtem Verbund! Manuell klären.'
                          if conflicts.get(g, 0) else
                          'Nur manuell bestätigte Paarverbindungen; keine automatische Zusammenführung'))
            for g, ids in sorted(by_group.items())]


def _csv_bytes(rows, columns):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=columns, delimiter=';', extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _safe(row.get(k, '')) for k in columns})
    return ('\ufeff' + buf.getvalue()).encode('utf-8')


def export_internal_pairs(db, pid, accepted_only=False):
    _check(db, pid)
    where = " AND p.state='bestaetigt'" if accepted_only else ''
    rows = []
    for r in db.execute('''SELECT a.source_id id1,b.source_id id2,p.score,p.match_probability,
        p.state,p.reason,p.llm_decision,p.updated_at FROM pairs p
        JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
        WHERE p.project_id=?'''+where+' ORDER BY p.score DESC,a.source_id,b.source_id', (pid,)):
        rows.append(dict(Datensatz_1_ID=r['id1'],Datensatz_2_ID=r['id2'],Score_0_100=r['score'],
                         Splink_Wahrscheinlichkeit=r['match_probability'] or '',
                         Pruefentscheid=r['state'],Begruendung=r['reason'],
                         KI_Hinweis=r['llm_decision'],Letzte_Bearbeitung=r['updated_at']))
    cols = ('Datensatz_1_ID','Datensatz_2_ID','Score_0_100','Splink_Wahrscheinlichkeit',
            'Pruefentscheid','Begruendung','KI_Hinweis','Letzte_Bearbeitung')
    return _csv_bytes(rows, cols)


def export_internal_inventory(db, pid):
    """Original source fields + review metadata, never a modified/merged CRM record."""
    mapping, _, _ = _confirmed_components(db, pid)
    data = list(db.execute("SELECT pk,source_id,source_json FROM records WHERE project_id=? AND side='A' ORDER BY pk", (pid,)))
    cols = []
    sources = []
    for r in data:
        original = json.loads(r['source_json'])
        sources.append(original)
        for key in original:
            if key not in cols:
                cols.append(key)
    prefix = ['Interne_Original_ID','Dubletten_Gruppe','Bestaetigte_Links','Offene_Kandidaten','Gruppen_Widersprueche']
    # Avoid collisions with user-provided column names.
    assigned = {}
    used = set(cols)
    for key in prefix:
        name = key
        while name in used:
            name = 'Pruefung_' + name
        assigned[key] = name
        used.add(name)
    group_conflicts = _group_contradictions(db, pid, mapping)
    link_counts = defaultdict(int)
    pending_counts = defaultdict(int)
    for r in db.execute('SELECT a_pk,b_pk,state FROM pairs WHERE project_id=?', (pid,)):
        target = link_counts if r['state'] == 'bestaetigt' else pending_counts if r['state'] in ('offen','unklar') else None
        if target is not None:
            target[r['a_pk']] += 1
            target[r['b_pk']] += 1
    rows = []
    for dbrow, source in zip(data, sources):
        record = {assigned['Interne_Original_ID']: dbrow['source_id'],
                  assigned['Dubletten_Gruppe']: mapping.get(dbrow['pk'], ''),
                  assigned['Bestaetigte_Links']: link_counts[dbrow['pk']],
                  assigned['Offene_Kandidaten']: pending_counts[dbrow['pk']],
                  assigned['Gruppen_Widersprueche']: group_conflicts.get(mapping.get(dbrow['pk']), 0)}
        record.update(source)
        rows.append(record)
    return _csv_bytes(rows, list(assigned.values()) + cols)


def export_internal_groups(db, pid):
    _check(db, pid)
    columns = ['Gruppe','Original_ID','Anzahl_in_Gruppe','Manuelle_Links','Widerspruchspaare','Hinweis']
    membership, _, _ = _confirmed_components(db, pid)
    if not membership:
        return _csv_bytes([], columns)
    by_group = defaultdict(list)
    for pk, g in membership.items():
        by_group[g].append(pk)
    ids = {r['pk']: r['source_id'] for r in db.execute(
        "SELECT pk,source_id FROM records WHERE project_id=? AND side='A'", (pid,))}
    conflicts = _group_contradictions(db, pid, membership)
    links = defaultdict(int)
    for r in db.execute("SELECT a_pk,b_pk FROM pairs WHERE project_id=? AND state='bestaetigt'", (pid,)):
        links[membership[r['a_pk']]] += 1
    rows = []
    for g, pks in sorted(by_group.items()):
        for pk in sorted(pks, key=lambda p: ids[p]):
            rows.append(dict(Gruppe=g,Original_ID=ids[pk],Anzahl_in_Gruppe=len(pks),
                             Manuelle_Links=links[g],Widerspruchspaare=conflicts.get(g, 0),
                             Hinweis=('ACHTUNG: widersprüchlich abgelehntes Paar in bestätigter Gruppe' if conflicts.get(g, 0) else
                                      'Verbund bestätigter Paarlinks; nicht alle Paarverbindungen zwingend geprüft')))
    return _csv_bytes(rows, columns)
