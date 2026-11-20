"""Local, explainable cross-CRM matching. No external network calls."""
from __future__ import annotations

import io
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

FIELDS = ('id', 'company', 'first_name', 'last_name', 'street', 'postcode',
          'city', 'country', 'email', 'phone', 'uid')
LABELS = {
    'id': 'Eindeutige CRM-ID', 'company': 'Firma / Organisation',
    'first_name': 'Vorname', 'last_name': 'Nachname', 'street': 'Strasse + Hausnummer',
    'postcode': 'PLZ', 'city': 'Ort', 'country': 'Land', 'email': 'E-Mail',
    'phone': 'Telefon', 'uid': 'UID / MWST-Nummer / Handelsregister-ID',
}
ALIASES = {
    'id': ['id', 'recordid', 'accountid', 'kontaktid', 'kundennummer', 'customerid', 'crm_id', 'nr'],
    'company': ['firma', 'firmenname', 'company', 'companyname', 'organisation', 'organization', 'name1', 'accountname'],
    'first_name': ['vorname', 'firstname', 'first_name', 'givenname'],
    'last_name': ['nachname', 'lastname', 'last_name', 'surname'],
    'street': ['strasse', 'straße', 'street', 'address', 'adresse', 'adresszeile1', 'address1'],
    'postcode': ['plz', 'postcode', 'zip', 'zipcode', 'postleitzahl'],
    'city': ['ort', 'stadt', 'city', 'town'],
    'country': ['land', 'country'],
    'email': ['email', 'e_mail', 'e-mail', 'mail'],
    'phone': ['telefon', 'telephone', 'phone', 'tel', 'mobile', 'mobil'],
    'uid': ['uid', 'mwst', 'vat', 'vatnumber', 'ustid', 'taxid'],
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def plain(s):
    if s is None or pd.isna(s):
        return ''
    return str(s).strip()


def norm(s):
    s = plain(s).lower().replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue').replace('ß', 'ss')
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def norm_name(s):
    s = norm(s)
    # Juristische Endungen fuer Aehnlichkeitsvergleich entfernen; Original bleibt erhalten.
    while re.search(r'(?:^| )(?:ag|gmbh|sa|sarl|s rl|ltd|inc|kg|llc)$', s):
        s = re.sub(r'(?:^| )(?:ag|gmbh|sa|sarl|s rl|ltd|inc|kg|llc)$', '', s).strip()
    return s


def norm_street(s):
    s = norm(s)
    s = re.sub(r'\b([a-z]+)str\b', r'\1strasse', s)
    s = re.sub(r'\b(strasse|str|street|stras|strass)\b', 'strasse', s)
    s = re.sub(r'\b(avenue|ave)\b', 'avenue', s)
    s = re.sub(r'\b(platz|pl)\b', 'platz', s)
    return s


def norm_phone(s):
    digits = re.sub(r'\D', '', plain(s))
    if digits.startswith('0041'):
        digits = '41' + digits[4:]
    elif len(digits) == 10 and digits.startswith('0'):
        digits = '41' + digits[1:]
    elif len(digits) == 9:
        digits = '41' + digits
    return digits if len(digits) >= 8 else ''


def norm_uid(s):
    return re.sub(r'[^A-Z0-9]', '', plain(s).upper())


def best_guess(columns, field):
    indexed = {norm(c).replace(' ', ''): c for c in columns}
    for alias in ALIASES[field]:
        key = norm(alias).replace(' ', '')
        if key in indexed:
            return indexed[key]
    return ''


def read_upload(file):
    content = file.getvalue()
    if len(content) > 60 * 1024 * 1024:
        raise ValueError('Datei zu gross (maximal 60 MB pro Export).')
    if file.name.lower().endswith('.xlsx'):
        frame = pd.read_excel(io.BytesIO(content), dtype=str, engine='openpyxl')
    elif file.name.lower().endswith('.csv'):
        err = None
        for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
            try:
                frame = pd.read_csv(io.BytesIO(content), sep=None, engine='python',
                                    dtype=str, keep_default_na=False, encoding=enc,
                                    on_bad_lines='error')
                break
            except (UnicodeError, pd.errors.ParserError) as exc:
                err = exc
        else:
            raise ValueError(f'CSV nicht lesbar: {err}')
    else:
        raise ValueError('Nur .csv und .xlsx werden unterstuetzt.')
    if frame.empty:
        raise ValueError('Die Datei enthaelt keine Daten.')
    if len(frame) > 150_000:
        raise ValueError('Maximal 150’000 Datensaetze pro Quelle in dieser Version.')
    frame.columns = [str(c).strip() for c in frame.columns]
    if len(set(frame.columns)) != len(frame.columns):
        raise ValueError('Doppelte Spaltennamen im Export. Bitte zuerst umbenennen.')
    frame = frame.fillna('').astype(str)
    return frame


def row_values(row, mapping):
    return {field: plain(row[mapping[field]]) if mapping.get(field) else '' for field in FIELDS}


def validate_mapping(frame, mapping, side):
    if not mapping.get('id'):
        raise ValueError(f'CRM {side}: Eine eindeutige ID-Spalte muss zugeordnet sein.')
    if not any(mapping.get(k) for k in ('company', 'last_name', 'email', 'uid')):
        raise ValueError(f'CRM {side}: Firma, Name, E-Mail oder UID erforderlich.')
    ids = frame[mapping['id']].map(plain)
    if (ids == '').any() or ids.duplicated().any():
        raise ValueError(f'CRM {side}: Leere oder doppelte IDs. Bitte vor dem Import bereinigen.')


def to_record(raw):
    company = norm_name(raw['company'])
    person = norm_name(' '.join(x for x in (raw['first_name'], raw['last_name']) if x))
    # Unterschiedliche Ansprechpartner bei derselben Firma nicht blind vereinen.
    name_n = ' '.join(part for part in (company,person) if part)
    street_n = norm_street(raw['street'])
    postal_n = re.sub(r'\W', '', plain(raw['postcode']).upper())
    if postal_n.startswith('CH') and len(postal_n) == 6 and postal_n[2:].isdigit():
        postal_n = postal_n[2:]
    city_n = norm(raw['city'])
    email_n = plain(raw['email']).lower().strip()
    phone_n = norm_phone(raw['phone'])
    uid_n = norm_uid(raw['uid'])
    return {
        'name': name_n, 'street': street_n, 'postal': postal_n,
        'city': city_n, 'email': email_n, 'phone': phone_n, 'uid': uid_n,
        'zip_name': f'{postal_n}|{name_n[:6]}' if postal_n and len(name_n) >= 4 else '',
        'zip_street': f'{postal_n}|{street_n[:7]}' if postal_n and len(street_n) >= 5 else '',
        'city_name': f'{city_n[:8]}|{name_n[:8]}' if city_n and len(name_n) >= 5 else '',
        'name_head': name_n[:10] if len(name_n) >= 7 else '',
        'street_number': ''.join(re.findall(r'\d+', street_n)),
    }


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=60000')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS projects (
        project_id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
        a_count INTEGER NOT NULL, b_count INTEGER NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0,
        engine TEXT NOT NULL DEFAULT 'rapidfuzz', match_model_info TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS records (
        pk INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        side TEXT NOT NULL CHECK(side IN ('A','B')), source_id TEXT NOT NULL,
        original_json TEXT NOT NULL, source_json TEXT NOT NULL, name TEXT NOT NULL, street TEXT NOT NULL,
        postal TEXT NOT NULL, city TEXT NOT NULL, email TEXT NOT NULL,
        phone TEXT NOT NULL, uid TEXT NOT NULL, zip_name TEXT NOT NULL,
        zip_street TEXT NOT NULL, city_name TEXT NOT NULL, name_head TEXT NOT NULL,
        street_number TEXT NOT NULL,
        UNIQUE(project_id,side,source_id)
    );
    CREATE TABLE IF NOT EXISTS pairs (
        project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        a_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
        b_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
        score INTEGER NOT NULL,
        reason TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'offen',
        llm_decision TEXT NOT NULL DEFAULT '', llm_reason TEXT NOT NULL DEFAULT '',
        llm_model TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        match_probability REAL,
        PRIMARY KEY(project_id,a_pk,b_pk)
    );
    CREATE INDEX IF NOT EXISTS idx_records_project_side ON records(project_id,side);
    CREATE INDEX IF NOT EXISTS idx_pairs_project_score ON pairs(project_id,score DESC);
    CREATE INDEX IF NOT EXISTS idx_pairs_state ON pairs(project_id,state);
    ''')
    # Safe schema migration for existing persistent SQLite databases.
    project_columns = {r['name'] for r in db.execute('PRAGMA table_info(projects)')}
    if 'engine' not in project_columns:
        db.execute("ALTER TABLE projects ADD COLUMN engine TEXT NOT NULL DEFAULT 'rapidfuzz'")
    if 'match_model_info' not in project_columns:
        db.execute("ALTER TABLE projects ADD COLUMN match_model_info TEXT NOT NULL DEFAULT ''")
    pair_columns = {r['name'] for r in db.execute('PRAGMA table_info(pairs)')}
    if 'match_probability' not in pair_columns:
        db.execute('ALTER TABLE pairs ADD COLUMN match_probability REAL')
    for col in ('uid', 'email', 'phone', 'zip_name', 'zip_street', 'city_name', 'name_head'):
        db.execute(f'CREATE INDEX IF NOT EXISTS idx_block_{col} ON records(project_id,side,{col})')
    db.commit()
    return db


def import_project(db, title, frame_a, map_a, frame_b, map_b, engine='rapidfuzz'):
    if engine not in ('rapidfuzz','splink5'):
        raise ValueError('Unbekannte Matching-Engine')
    validate_mapping(frame_a, map_a, 'A')
    validate_mapping(frame_b, map_b, 'B')
    # IDs niemals numerisch umwandeln: 000123 bleibt 000123.
    with db:
        cur = db.execute('INSERT INTO projects(title,created_at,a_count,b_count,engine) VALUES(?,?,?,?,?)',
                         (title[:140].strip() or 'CRM-Abgleich', now(), len(frame_a), len(frame_b), engine))
        pid = cur.lastrowid
        for side, frame, mapping in [('A', frame_a, map_a), ('B', frame_b, map_b)]:
            batch = []
            for vals in frame.itertuples(index=False, name=None):
                original = {str(col): plain(val) for col, val in zip(frame.columns, vals)}
                raw = {field: plain(original.get(mapping[field])) if mapping.get(field) else '' for field in FIELDS}
                n = to_record(raw)
                batch.append((pid,side,raw['id'],json.dumps(raw,ensure_ascii=False),
                              json.dumps(original,ensure_ascii=False), n['name'],n['street'],n['postal'],n['city'],n['email'],n['phone'],
                              n['uid'],n['zip_name'],n['zip_street'],n['city_name'],n['name_head'],
                              n['street_number']))
                if len(batch) >= 1000:
                    db.executemany('''INSERT INTO records(project_id,side,source_id,original_json,
                        source_json,name,street,postal,city,email,phone,uid,zip_name,zip_street,city_name,
                        name_head,street_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
                    batch = []
            if batch:
                db.executemany('''INSERT INTO records(project_id,side,source_id,original_json,
                    source_json,name,street,postal,city,email,phone,uid,zip_name,zip_street,city_name,
                    name_head,street_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
    return pid


def score_pair(a, b):
    """Heuristic review score, NOT calibrated match probability."""
    vals = []
    reasons = []
    if a['name'] and b['name']:
        s = fuzz.WRatio(a['name'], b['name']) / 100
        vals.append((0.48, s))
        if s >= .90: reasons.append('Name sehr aehnlich')
        elif s < .50: reasons.append('Name deutlich unterschiedlich')
    if a['street'] and b['street']:
        s = fuzz.WRatio(a['street'], b['street']) / 100
        vals.append((0.26, s))
        if s >= .90: reasons.append('Strasse aehnlich')
    if a['postal'] and b['postal']:
        same = a['postal'] == b['postal']
        vals.append((0.16, float(same)))
        if same: reasons.append('PLZ identisch')
        else: reasons.append('PLZ verschieden')
    if a['city'] and b['city']:
        vals.append((0.10, fuzz.WRatio(a['city'], b['city']) / 100))
    if not vals:
        return 0, 'Keine vergleichbaren Adress- oder Namensfelder'
    score = (sum(w * s for w,s in vals) / sum(w for w,_ in vals)) * 100
    if a['uid'] and b['uid']:
        if a['uid'] == b['uid']:
            score = max(score, 96)
            reasons.append('UID identisch')
        else:
            score -= 45
            if score < 65 and vals and sum(w*s for w,s in vals)/sum(w for w,_ in vals) >= .85:
                score = 65  # Konflikt mit ansonsten fast identischen Stammdaten zur Pruefung anzeigen.
            reasons.append('WARNUNG: UID verschieden')
    if a['email'] and b['email'] and a['email'] == b['email']:
        score = max(score, 78)
        score += 10
        reasons.append('E-Mail identisch')
    if a['phone'] and b['phone'] and a['phone'] == b['phone']:
        score += 7
        reasons.append('Telefon identisch')
    if a['street_number'] and b['street_number'] and a['street_number'] != b['street_number']:
        score -= 17
        reasons.append('Hausnummer verschieden')
    return max(0, min(100, int(round(score)))), ', '.join(reasons) or 'Aehnliche Datensaetze'


BLOCKS = [
    ('uid', 2500), ('email', 1500), ('phone', 500),
    ('zip_name', 450), ('zip_street', 250), ('city_name', 200), ('name_head', 100)
]


def run_matching(db, pid, minimum=60, max_candidates=400_000, progress=None):
    """Indexed, capped blocking; no 35k x 35k Cartesian product."""
    progress = progress or (lambda msg: None)
    with db:
        db.execute('DELETE FROM pairs WHERE project_id=?', (pid,))
        db.execute('''CREATE TEMP TABLE IF NOT EXISTS work_candidates (
            a_pk INTEGER NOT NULL, b_pk INTEGER NOT NULL, PRIMARY KEY(a_pk,b_pk)) WITHOUT ROWID''')
        db.execute('DELETE FROM work_candidates')
        for field, fanout in BLOCKS:
            progress(f'Kandidaten ermitteln: {field}')
            # In Python nach Block-Key gruppieren: verhindert problematische
            # SQLite-Joinplaene und vermeidet den vollen Kreuzvergleich.
            side_maps = {}
            for side in ('A','B'):
                mapping = {}
                for row in db.execute(f"SELECT pk,{field} FROM records WHERE project_id=? AND side=? AND {field} != ''", (pid,side)):
                    mapping.setdefault(row[field], []).append(row['pk'])
                side_maps[side] = mapping
            a_map,b_map = side_maps['A'],side_maps['B']
            buffer=[]
            for key, a_ids in a_map.items():
                b_ids=b_map.get(key)
                if not b_ids or len(a_ids)*len(b_ids)>fanout:
                    continue
                for a_id in a_ids:
                    for b_id in b_ids:
                        buffer.append((a_id,b_id))
                        if len(buffer)>=4000:
                            db.executemany('INSERT OR IGNORE INTO work_candidates(a_pk,b_pk) VALUES(?,?)',buffer)
                            buffer.clear()
            if buffer:
                db.executemany('INSERT OR IGNORE INTO work_candidates(a_pk,b_pk) VALUES(?,?)',buffer)
            qty = db.execute('SELECT COUNT(*) FROM work_candidates').fetchone()[0]
            if qty > max_candidates:
                raise ValueError(f'{qty:,} Kandidaten: Limit {max_candidates:,} ueberschritten. Engere Blocking-Regeln erforderlich.')
        qty = db.execute('SELECT COUNT(*) FROM work_candidates').fetchone()[0]
        progress(f'{qty:,} Kandidaten bewerten')
        cursor = db.execute('''SELECT w.a_pk,w.b_pk,
            a.name a_name,a.street a_street,a.postal a_postal,a.city a_city,
            a.email a_email,a.phone a_phone,a.uid a_uid,a.street_number a_street_number,
            b.name b_name,b.street b_street,b.postal b_postal,b.city b_city,
            b.email b_email,b.phone b_phone,b.uid b_uid,b.street_number b_street_number
            FROM work_candidates w
            JOIN records a ON a.pk=w.a_pk
            JOIN records b ON b.pk=w.b_pk''')
        buffer = []
        kept = 0
        while True:
            fetched = cursor.fetchmany(2500)
            if not fetched: break
            for row in fetched:
                a = {k:row['a_'+k] for k in ('name','street','postal','city','email','phone','uid','street_number')}
                b = {k:row['b_'+k] for k in a}
                score, reason = score_pair(a,b)
                if score >= minimum:
                    buffer.append((pid,row['a_pk'],row['b_pk'],score,reason,now()))
            if buffer:
                db.executemany('''INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at)
                                  VALUES(?,?,?,?,?,?)''',buffer)
                kept += len(buffer)
                buffer = []
        db.execute('UPDATE projects SET candidate_count=? WHERE project_id=?', (kept,pid))
        db.execute('DROP TABLE work_candidates')
    return qty, kept


def list_projects(db):
    return db.execute('SELECT * FROM projects ORDER BY project_id DESC').fetchall()


def project_stats(db, pid):
    return dict(db.execute('''SELECT p.*,
            (SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='bestaetigt') accepted,
            (SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='offen') pending,
            (SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='abgelehnt') rejected
            FROM projects p WHERE project_id=?''',(pid,)).fetchone())


def page_pairs(db, pid, state='Alle', offset=0, limit=40, min_score=0):
    clause = " AND p.state=?" if state != 'Alle' else ''
    params = [pid,min_score]+([state] if state != 'Alle' else [])
    sql = f'''FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
              WHERE p.project_id=? AND p.score >= ? {clause}'''
    count = db.execute('SELECT COUNT(*) '+sql,params).fetchone()[0]
    items = db.execute('''SELECT p.*,a.source_id a_id,b.source_id b_id,
                        a.original_json a_json,b.original_json b_json '''+sql+
                       ' ORDER BY p.score DESC,p.a_pk,p.b_pk LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    return count,items


def set_review(db,pid,a_pk,b_pk,state):
    if state not in ('bestaetigt','abgelehnt','offen','unklar'):
        raise ValueError('Ungueltiger Pruefstatus')
    with db:
        if state=='bestaetigt':
            clash = db.execute('''SELECT a_pk,b_pk FROM pairs WHERE project_id=? AND state='bestaetigt'
                  AND (a_pk=? OR b_pk=?) AND NOT (a_pk=? AND b_pk=?) LIMIT 1''',
                  (pid,a_pk,b_pk,a_pk,b_pk)).fetchone()
            if clash:
                raise ValueError('Konflikt: Mindestens eine CRM-ID ist bereits in einem anderen Treffer bestaetigt. Zuerst die andere Freigabe aufheben.')
        db.execute('''UPDATE pairs SET state=?,updated_at=?
                      WHERE project_id=? AND a_pk=? AND b_pk=?''',
                   (state,now(),pid,a_pk,b_pk))


def save_llm(db,pid,a_pk,b_pk,decision,reason,model):
    if decision not in ('gleich','verschieden','unklar'):
        decision='unklar'
    with db:
        db.execute('''UPDATE pairs SET llm_decision=?,llm_reason=?,llm_model=?,updated_at=?
                      WHERE project_id=? AND a_pk=? AND b_pk=?''',
                   (decision,reason[:1800],model[:100],now(),pid,a_pk,b_pk))


def export_pairs(db,pid,accepted_only=False):
    sql = '''SELECT a.source_id crm_a_id, b.source_id crm_b_id,
             p.score vergleichswert_0_100, p.match_probability splink_match_probability,
             pr.engine matching_engine, p.state pruefentscheid,
             p.reason gruende, p.llm_decision ki_vorschlag,
             p.llm_reason ki_begruendung, p.llm_model ki_modell,
             p.updated_at bearbeitet_am
             FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk
             JOIN projects pr ON pr.project_id=p.project_id
             WHERE p.project_id=?'''
    if accepted_only:
        sql += " AND p.state='bestaetigt'"
    sql += ' ORDER BY p.score DESC'
    return pd.read_sql_query(sql,db,params=(pid,)).to_csv(index=False,sep=';').encode('utf-8-sig')


def export_unmatched(db,pid,side):
    if side not in ('A','B'):
        raise ValueError('ungueltige Quelle')
    col = 'a_pk' if side=='A' else 'b_pk'
    sql = f'''SELECT r.source_id crm_id,r.source_json originaldaten
              FROM records r WHERE r.project_id=? AND r.side=?
              AND NOT EXISTS(SELECT 1 FROM pairs p WHERE p.project_id=r.project_id
                             AND p.state='bestaetigt' AND p.{col}=r.pk)
              ORDER BY r.source_id'''
    rows = db.execute(sql, (pid,side)).fetchall()
    data = [{'crm_id':r['crm_id'],**json.loads(r['originaldaten'])} for r in rows]
    return pd.DataFrame(data).to_csv(index=False,sep=';').encode('utf-8-sig')


def delete_project(db,pid):
    with db:
        db.execute('DELETE FROM projects WHERE project_id=?',(pid,))
