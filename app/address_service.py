"""Independent, offline Swiss building-address quality service.

The single input file is never merged with a CRM project. Source values are immutable.
Every suggested correction is advisory until a user explicitly approves it.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from engine import best_guess, norm, norm_house_number, norm_street, now, plain, split_street_house
from swisstopo import active_snapshot, validate_record, ensure_schema as ensure_swisstopo_schema

INPUT_FIELDS = ('id', 'street', 'house_number', 'postcode', 'city', 'country')
INPUT_LABELS = {
    'id': 'Eigene ID (optional)', 'street': 'Strasse / Adresse (Pflicht)',
    'house_number': 'Hausnummer (optional)', 'postcode': 'PLZ (Pflicht)',
    'city': 'Ort (optional)', 'country': 'Land (optional)',
}
FIELD_ALIASES = {
    'id': ('id', 'crm id', 'crm_id', 'adress_id', 'kundennummer', 'nr'),
    'street': ('strasse', 'str', 'straße', 'adresse', 'street', 'address', 'address1', 'adresszeile1'),
    'house_number': ('hausnummer', 'hausnr', 'house_number', 'housenumber'),
    'postcode': ('plz', 'postleitzahl', 'postcode', 'zip', 'zipcode'),
    'city': ('ort', 'stadt', 'city', 'locality'),
    'country': ('land', 'country', 'landcode'),
}
REVIEW_STATES = ('offen', 'korrigiert', 'keine_aenderung', 'uebersprungen')


def guess_column(columns, field):
    cols = {norm(c).replace(' ', ''): c for c in columns}
    for alias in FIELD_ALIASES[field]:
        value = cols.get(norm(alias).replace(' ', ''))
        if value:
            return value
    return ''


def ensure_schema(db):
    ensure_swisstopo_schema(db)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS address_jobs(
       job_id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
       file_name TEXT NOT NULL, snapshot_id INTEGER NOT NULL
         REFERENCES swisstopo_snapshots(snapshot_id),
       column_names_json TEXT NOT NULL, mapping_json TEXT NOT NULL,
       row_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS address_job_rows(
       row_pk INTEGER PRIMARY KEY,
       job_id INTEGER NOT NULL REFERENCES address_jobs(job_id) ON DELETE CASCADE,
       row_no INTEGER NOT NULL, source_id TEXT NOT NULL, original_json TEXT NOT NULL,
       normalized_json TEXT NOT NULL, result TEXT NOT NULL, hint TEXT NOT NULL,
       proposals_json TEXT NOT NULL DEFAULT '[]', matched_egaid TEXT NOT NULL DEFAULT '',
       decision TEXT NOT NULL DEFAULT 'offen', correction_json TEXT NOT NULL DEFAULT '{}',
       reviewed_at TEXT NOT NULL DEFAULT '', correction_basis TEXT NOT NULL DEFAULT '',
       UNIQUE(job_id,row_no));
    CREATE INDEX IF NOT EXISTS idx_address_job_result ON address_job_rows(job_id,result);
    CREATE INDEX IF NOT EXISTS idx_address_job_decision ON address_job_rows(job_id,decision);
    ''')
    cols = {r['name'] for r in db.execute('PRAGMA table_info(address_job_rows)')}
    if 'correction_basis' not in cols:
        db.execute("ALTER TABLE address_job_rows ADD COLUMN correction_basis TEXT NOT NULL DEFAULT ''")
    db.commit()


def read_single_upload(upload):
    """The uploaded *personal* file is usually small; public swisstopo ZIP is separate."""
    size = getattr(upload, 'size', None)
    if size is not None and size > 60 * 1024**2:
        raise ValueError('Einzeldatei über 60 MB: Bitte eine kleinere CSV/XLSX mit maximal 150’000 Zeilen verwenden.')
    raw = upload.getvalue()
    if len(raw) > 60 * 1024**2:
        raise ValueError('Einzeldatei über 60 MB.')
    file_name = plain(upload.name)
    if file_name.lower().endswith('.xlsx'):
        data = pd.read_excel(io.BytesIO(raw), dtype=str, engine='openpyxl', keep_default_na=False)
    elif file_name.lower().endswith('.csv'):
        last = None
        for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
            try:
                data = pd.read_csv(io.BytesIO(raw), sep=None, engine='python', dtype=str,
                                   keep_default_na=False, encoding=encoding, on_bad_lines='error')
                break
            except (UnicodeError, pd.errors.ParserError) as exc:
                last = exc
        else:
            raise ValueError(f'CSV nicht lesbar: {last}')
    else:
        raise ValueError('Bitte eine CSV- oder XLSX-Datei verwenden.')
    if data.empty:
        raise ValueError('Datei ist leer.')
    if len(data) > 150_000:
        raise ValueError('Maximal 150’000 Datensätze pro Einzelprüfung.')
    data.columns = [plain(x) for x in data.columns]
    if len(set(data.columns)) != len(data.columns) or not all(data.columns):
        raise ValueError('Die Spaltenüberschriften müssen eindeutig und nicht leer sein.')
    return data.fillna('').astype(str)


def _postcode(value):
    s = plain(value).upper().replace(' ', '')
    return s[2:] if s.startswith('CH') and len(s) == 6 else s


def _normalized_address(original, mapping):
    def val(key):
        return plain(original.get(mapping[key], '')) if mapping.get(key) else ''
    street_raw, house_raw, conflict = split_street_house(val('street'), val('house_number'))
    return {
        'street': street_raw, 'house': house_raw, 'postal': _postcode(val('postcode')),
        'city': norm(val('city')), 'country': val('country'),
        'street_source': val('street'), 'house_source': val('house_number'),
        'postal_source': val('postcode'), 'city_source': val('city'),
        'split_conflict': bool(conflict),
    }


def _canonical(row):
    return {
        'strasse': row['street_label'] or row['street'],
        'hausnummer': row['number_label'] or row['house'],
        'plz': row['postal'],
        'ort': row['city_label'] or row['city'],
        'egaid': row['egaid'],
        'status': row['adr_status'],
        'amtlich': row['official'] == '1',
    }


def _rank(row):
    return (row['adr_status'] == 'real', row['official'] == '1', bool(row['egaid']))


def _proposals(db, sid, addr, code):
    """A bounded local search; suggestions are *not* evidence of residency/identity.

    For wrong house numbers, do not infer a replacement just because the number
    is close. Fuzzy street corrections are scoped to postcode+house number.
    """
    postal, street, house, city = addr['postal'], addr['street'], addr['house'], addr['city']
    result = []
    candidates = []
    if code in ('amtlich_aktuell', 'nicht_amtlich', 'status_unklar', 'geplant', 'aufgehoben'):
        candidates = db.execute('''SELECT * FROM swisstopo_addresses
                 WHERE snapshot_id=? AND postal=? AND street=? AND house=?
                   AND (?='' OR city=?) LIMIT 10''', (sid,postal,street,house,city,city)).fetchall()
    elif code == 'ort_abweichend':
        candidates = db.execute('''SELECT * FROM swisstopo_addresses
                 WHERE snapshot_id=? AND postal=? AND street=? AND house=? LIMIT 30''',
                 (sid,postal,street,house)).fetchall()
    elif code == 'plz_abweichend':
        candidates = db.execute('''SELECT * FROM swisstopo_addresses
                 WHERE snapshot_id=? AND city=? AND street=? AND house=? LIMIT 30''',
                 (sid,city,street,house)).fetchall()
    elif code == 'nicht_gefunden' and postal and house and street:
        # Index-friendly bound: search only streets in this PLZ with matching house.
        if city:
            candidates = db.execute('''SELECT * FROM swisstopo_addresses
                      WHERE snapshot_id=? AND postal=? AND house=? AND city=? LIMIT 3000''',
                      (sid,postal,house,city)).fetchall()
        else:
            candidates = db.execute('''SELECT * FROM swisstopo_addresses
                      WHERE snapshot_id=? AND postal=? AND house=? LIMIT 3000''',
                      (sid,postal,house)).fetchall()
        # Do not present weak street guesses as verified matches.
        candidates = [r for r in candidates if fuzz.ratio(street,r['street']) >= 88]
    # For hausnummer_unbekannt: only an issue flag, no suggested replacement.
    candidates = sorted(candidates, key=lambda r: (_rank(r), fuzz.ratio(street,r['street'])), reverse=True)
    seen=set()
    for row in candidates:
        key=(row['postal'],row['street'],row['house'],row['city'])
        if key in seen:
            continue
        seen.add(key)
        proposal=_canonical(row)
        proposal['strassen_aehnlichkeit']=round(fuzz.ratio(street,row['street']))
        if code=='nicht_gefunden':
            proposal['hinweis']='Ähnliche Strasse mit gleicher Hausnummer/PLZ; Identität manuell prüfen'
        elif code in ('ort_abweichend','plz_abweichend'):
            proposal['hinweis']='Amtlicher Adressvorschlag; Standort und PLZ prüfen'
        else:
            proposal['hinweis']='Im swisstopo-Datenstand gefunden'
        result.append(proposal)
        if len(result)>=3:
            break
    return result


def validate_address(db, sid, addr):
    if addr['split_conflict']:
        return ('eingabe_konflikt', 'Widerspruch zwischen Strasse und separater Hausnummer.', '', [])
    rec = {'original_json':json.dumps({'country':addr['country']}),
           'postal':addr['postal'],'street_name':addr['street'],
           'house_number':addr['house'],'city':addr['city']}
    code, hint, egaid = validate_record(db,sid,rec)
    proposals = _proposals(db,sid,addr,code)
    if code == 'nicht_gefunden' and proposals:
        code='strasse_aehnlich'
        hint='Ähnliche Strassenadresse im amtlichen Register gefunden – bitte manuell prüfen.'
    return code,hint,egaid,proposals


def import_job(db, frame, title, file_name, mapping, snapshot_id=None, progress=None):
    ensure_schema(db)
    snapshot = active_snapshot(db) if snapshot_id is None else db.execute(
        'SELECT * FROM swisstopo_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
    if not snapshot:
        raise ValueError('Zuerst einen lokalen swisstopo-Datenstand importieren.')
    sid=snapshot['snapshot_id']
    if not plain(title):
        raise ValueError('Bitte eine Bezeichnung eingeben.')
    if not mapping.get('street') or not mapping.get('postcode'):
        raise ValueError('Strasse und PLZ müssen zugeordnet sein.')
    cols=list(frame.columns)
    if any(col not in cols for col in mapping.values() if col):
        raise ValueError('Mindestens eine zugeordnete Spalte fehlt in der Datei.')
    if len(frame)>150000:
        raise ValueError('Maximal 150’000 Datensätze.')
    progress = progress or (lambda _: None)
    with db:
        job_id=db.execute('''INSERT INTO address_jobs(title,created_at,file_name,snapshot_id,
            column_names_json,mapping_json,row_count) VALUES(?,?,?,?,?,?,?)''',
            (plain(title),now(),Path(file_name).name,sid,json.dumps(cols,ensure_ascii=False),
             json.dumps(mapping,ensure_ascii=False),len(frame))).lastrowid
        batch=[]
        for row_no, values in enumerate(frame.itertuples(index=False,name=None),1):
            raw={name:plain(value) for name,value in zip(cols,values)}
            addr=_normalized_address(raw,mapping)
            code,hint,egaid,proposals=validate_address(db,sid,addr)
            source_id=raw.get(mapping.get('id',''), '') or str(row_no)
            batch.append((job_id,row_no,source_id,json.dumps(raw,ensure_ascii=False),
                          json.dumps(addr,ensure_ascii=False),code,hint,
                          json.dumps(proposals,ensure_ascii=False),egaid))
            if len(batch)>=500:
                db.executemany('''INSERT INTO address_job_rows(job_id,row_no,source_id,original_json,
                  normalized_json,result,hint,proposals_json,matched_egaid)
                  VALUES(?,?,?,?,?,?,?,?,?)''',batch)
                batch=[]
            if row_no % 5000 == 0:
                progress(f'{row_no:,} / {len(frame):,} Adressen lokal geprüft')
        if batch:
            db.executemany('''INSERT INTO address_job_rows(job_id,row_no,source_id,original_json,
              normalized_json,result,hint,proposals_json,matched_egaid)
              VALUES(?,?,?,?,?,?,?,?,?)''',batch)
    return job_id


def list_jobs(db):
    return [dict(r) for r in db.execute('''SELECT j.*,s.reference_date snapshot_date
             FROM address_jobs j JOIN swisstopo_snapshots s ON s.snapshot_id=j.snapshot_id
             ORDER BY j.job_id DESC''')]


def job_counts(db,job_id):
    rows=db.execute('''SELECT result,decision,COUNT(*) n FROM address_job_rows
                     WHERE job_id=? GROUP BY result,decision''',(job_id,)).fetchall()
    return [dict(x) for x in rows]


def _review_query(job_id, status='Alle', decision='Alle', needle=''):
    # Parameter-bound filtering shared by pagination and navigation.
    wh=['job_id=?']
    args=[job_id]
    if status!='Alle':
        wh.append('result=?');args.append(status)
    if decision!='Alle':
        wh.append('decision=?');args.append(decision)
    if needle:
        wh.append("(source_id LIKE ? ESCAPE '\\' OR original_json LIKE ? ESCAPE '\\')")
        pattern='%' + needle.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        args.extend([pattern,pattern])
    return ' AND '.join(wh),args


def page_rows(db,job_id,status='Alle',decision='Alle',needle='',limit=50,offset=0):
    where,args=_review_query(job_id,status,decision,needle)
    count=db.execute('SELECT COUNT(*) FROM address_job_rows WHERE '+where,args).fetchone()[0]
    results=db.execute('SELECT * FROM address_job_rows WHERE '+where+' ORDER BY row_no LIMIT ? OFFSET ?',
                       [*args,max(1,min(100,limit)),max(0,offset)]).fetchall()
    return count,[dict(r) for r in results]


def navigate_review(db,job_id,row_no,status='Alle',decision='Alle',needle='',direction='next',wrap=False,page_size=30):
    # Return next filtered row and its page, including across page boundaries.
    if direction not in ('next','previous'):
        raise ValueError('Ungültige Navigationsrichtung.')
    where,args=_review_query(job_id,status,decision,needle)
    op,order=('>','ASC') if direction=='next' else ('<','DESC')
    candidate=db.execute(f'''SELECT row_pk,row_no FROM address_job_rows
            WHERE {where} AND row_no {op} ? ORDER BY row_no {order} LIMIT 1''',
            [*args,row_no]).fetchone()
    if candidate is None and wrap:
        candidate=db.execute(f'''SELECT row_pk,row_no FROM address_job_rows
                 WHERE {where} ORDER BY row_no {order} LIMIT 1''',args).fetchone()
    if candidate is None:
        return None
    previous=db.execute(f'''SELECT COUNT(*) FROM address_job_rows
            WHERE {where} AND row_no < ?''', [*args,candidate['row_no']]).fetchone()[0]
    return (candidate['row_pk'], previous//page_size+1)


def display_street_without_number(addr):
    # Separate suffix house number without converting original casing.
    raw=plain(addr.get('street_source',''))
    house=plain(addr.get('house','')).replace(' ','').lower()
    if not house:
        return raw
    display=raw.replace('–','-').replace('—','-')
    m=re.fullmatch(r'(.+?)[,\s]+(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)',display)
    if m and norm_house_number(m.group(2))==norm_house_number(house):
        if norm(m.group(1)) not in ('route','highway','strasse','street','rue'):
            # Use original-cased substring, not the normalized search key.
            return raw[:len(m.group(1))]
    leading=re.fullmatch(r'(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)[,\s]+(.+)',display)
    if leading and norm_house_number(leading.group(1))==norm_house_number(house):
        return raw[len(leading.group(1)):].lstrip(' ,')
    return raw


def save_review(db,job_id,row_pk,decision,corrected=None):
    if decision not in REVIEW_STATES:
        raise ValueError('Ungültiger Prüfentscheid.')
    row=db.execute('SELECT * FROM address_job_rows WHERE job_id=? AND row_pk=?',(job_id,row_pk)).fetchone()
    if row is None:
        raise ValueError('Adresse nicht in diesem Prüfauftrag gefunden.')
    if decision=='korrigiert':
        if not isinstance(corrected,dict):
            raise ValueError('Für Korrektur sind die Adressfelder erforderlich.')
        fields=('strasse','hausnummer','plz','ort')
        cleaned={key:plain(corrected.get(key,'')) for key in fields}
        if not cleaned['strasse'] or not cleaned['plz'] or not cleaned['hausnummer']:
            raise ValueError('Strasse, Hausnummer und PLZ sind für eine Korrektur erforderlich.')
        if not re.fullmatch(r'\d{4}',cleaned['plz']):
            raise ValueError('Schweizer PLZ muss vierstellig sein.')
    else:
        cleaned={}
    basis=''
    if decision=='korrigiert':
        proposals=json.loads(row['proposals_json'])
        for p in proposals:
            if (norm_street(cleaned['strasse']) == norm_street(p['strasse'])
                    and norm_house_number(cleaned['hausnummer']) == norm_house_number(p['hausnummer'])
                    and cleaned['plz'] == p['plz'] and norm(cleaned['ort']) == norm(p['ort'])
                    and p.get('amtlich') and p.get('status')=='real'):
                basis='swisstopo_vorschlag_bestaetigt'
                break
        if not basis:
            basis='manuell_nicht_amtlich_verifiziert'
    with db:
        db.execute('''UPDATE address_job_rows SET decision=?,correction_json=?,reviewed_at=?,correction_basis=?
                   WHERE job_id=? AND row_pk=?''',(decision,json.dumps(cleaned,ensure_ascii=False),now(),basis,job_id,row_pk))



MASS_APPROVED = 'massenfreigegeben'


def _strict_official_rows(db, job_id):
    """Yield unambiguous *exact* Swiss addresses from the pinned reference snapshot.

    SQL preselects only current official records matching all four normalized
    fields. Python additionally requires the original street/locality labels
    to match the original input (case/whitespace/diacritics-insensitive only).
    This is deliberately stricter than fuzzy matching and norm_street().
    """
    job = db.execute('SELECT job_id FROM address_jobs WHERE job_id=?', (job_id,)).fetchone()
    if job is None:
        raise ValueError('Prüfauftrag nicht gefunden.')
    sql = """
        SELECT r.row_pk, r.row_no, r.source_id, r.normalized_json,
               COUNT(*) AS matches, MIN(s.egaid) AS egaid,
               MIN(s.street_label) AS official_street,
               MIN(s.city_label) AS official_city,
               MIN(s.number_label) AS official_house,
               MIN(s.adr_status) AS official_status,
               MIN(s.official) AS official_flag
        FROM address_job_rows r
        JOIN address_jobs j ON j.job_id = r.job_id
        JOIN swisstopo_addresses s ON s.snapshot_id=j.snapshot_id
           AND s.postal=json_extract(r.normalized_json,'$.postal')
           AND s.street=json_extract(r.normalized_json,'$.street')
           AND s.house=json_extract(r.normalized_json,'$.house')
           AND s.city=json_extract(r.normalized_json,'$.city')
        WHERE r.job_id=? AND r.decision='offen'
          AND r.result='amtlich_aktuell'
          AND json_extract(r.normalized_json,'$.city') != ''
          AND json_extract(r.normalized_json,'$.split_conflict')=0
        GROUP BY r.row_pk
        HAVING COUNT(*)=1 AND MIN(s.adr_status)='real' AND MIN(s.official)='1'
        ORDER BY r.row_no
    """
    for rec in db.execute(sql, (job_id,)):
        addr = json.loads(rec['normalized_json'])
        if not all((addr.get('street_source'), addr.get('house'),
                    addr.get('postal_source'), addr.get('city_source'))):
            continue
        if not all((rec['official_street'], rec['official_house'], rec['official_city'], rec['egaid'])):
            # Snapshots imported before original labels were preserved are not eligible.
            continue
        if plain(addr.get('country')).casefold() not in ('', 'ch', 'che', 'schweiz',
                                                        'suisse', 'svizzera', 'switzerland'):
            continue
        if norm(display_street_without_number(addr)) != norm(rec['official_street']):
            continue
        if norm_house_number(addr['house']) != norm_house_number(rec['official_house']):
            continue
        if not norm_house_number(addr['house']):
            continue
        if not re.fullmatch(r'\d{4}', addr['postal']):
            continue
        if norm(addr['city_source']) != norm(rec['official_city']):
            continue
        yield rec


def bulk_approval_preview(db, job_id, sample_size=8):
    """Preview eligibility, never write; never include fuzzy or ambiguous pairs."""
    count = 0
    digest = hashlib.sha256()
    examples = []
    for rec in _strict_official_rows(db, job_id):
        digest.update(f"{rec['row_pk']}:{rec['egaid']}\n".encode('ascii'))
        count += 1
        if len(examples) < sample_size:
            addr = json.loads(rec['normalized_json'])
            examples.append({'ID': rec['source_id'], 'Zeile': rec['row_no'],
                             'Strasse': display_street_without_number(addr),
                             'Hausnummer': addr['house'], 'PLZ': addr['postal'],
                             'Ort': addr['city_source'], 'EGAID': rec['egaid']})
    return {'count': count, 'examples': examples, 'digest': digest.hexdigest()}


def bulk_approve_exact(db, job_id, expected_count, expected_digest):
    """Explicit, idempotent mass-approval. Recheck all criteria at commit time.

    A changed preview (e.g. concurrent reviewer) cancels the whole operation.
    Only the *decision* changes; immutable source fields remain untouched.
    """
    if type(expected_count) is not int or expected_count <= 0:
        raise ValueError('Es ist keine freigabefähige Adresse ausgewählt.')
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError('Fehlender Prüfnachweis der Vorschau.')
    with db:
        db.execute('BEGIN IMMEDIATE')  # Lock candidate set for the full validation + update.
        rows = list(_strict_official_rows(db, job_id))
        digest = hashlib.sha256()
        for rec in rows:
            digest.update(f"{rec['row_pk']}:{rec['egaid']}\n".encode('ascii'))
        if len(rows) != expected_count or digest.hexdigest() != expected_digest:
            raise ValueError('Die Freigabeliste hat sich verändert. Bitte Vorschau neu laden.')
        stamp = now()
        changed_before = db.total_changes
        db.executemany("""UPDATE address_job_rows
            SET decision=?, correction_json='{}', correction_basis=?, reviewed_at=?
            WHERE job_id=? AND row_pk=? AND decision='offen' AND result='amtlich_aktuell' """,
            [(MASS_APPROVED, 'swisstopo_exakt_massengeprueft', stamp, job_id, r['row_pk'])
             for r in rows])
        changed = db.total_changes - changed_before
        if changed != expected_count:
            raise ValueError('Nicht alle Datensätze konnten freigegeben werden; keine Änderungen übernommen.')
    return changed

def delete_job(db,job_id):
    with db:
        db.execute('DELETE FROM address_jobs WHERE job_id=?',(job_id,))


def _safe_csv(val):
    """CSV formula injection avoidance (preserves raw source in SQLite)."""
    text=plain(val)
    if text and (text.lstrip()[:1] in ('=','+','-','@') or text[0] in '\r\n\t'):
        return "'"+text
    return text


def export_job(db,job_id,only_changed=False,split_house=False,
               street_column='Ziel_Strasse',house_column='Ziel_Hausnummer',only_bulk=False):
    # CSV with immutable source and optional dedicated street/house target fields.

    # For unconfirmed rows these fields are only an unverified source parsing.
    # They are not a migration approval.
    if only_changed and only_bulk:
        raise ValueError('Nur einen Exportfilter gleichzeitig verwenden.')
    job=db.execute('SELECT * FROM address_jobs WHERE job_id=?',(job_id,)).fetchone()
    if not job:
        raise ValueError('Prüfauftrag nicht gefunden.')
    cols=json.loads(job['column_names_json'])
    extra=['Pruefung_ID','Swisstopo_Status','Hinweis','Amtliche_EGAID','Pruefentscheid',
           'Korrigiert_Strasse','Korrigiert_Hausnummer','Korrigiert_PLZ','Korrigiert_Ort',
           'Korrekturquelle','Swisstopo_Datenstand','Pruefdatum']
    if split_house:
        street_column=plain(street_column)
        house_column=plain(house_column)
        if (not street_column or not house_column or street_column==house_column
                or any(n in [*cols,*extra,'Zieladresse_Basis'] for n in (street_column,house_column))):
            raise ValueError('Die Zielspalten müssen unterschiedliche neue Spaltennamen haben.')
        extra.extend([street_column,house_column,'Zieladresse_Basis'])
    ref=db.execute('SELECT reference_date FROM swisstopo_snapshots WHERE snapshot_id=?',(job['snapshot_id'],)).fetchone()[0]
    output=io.StringIO(newline='')
    writer=csv.writer(output,delimiter=';',lineterminator='\n')
    writer.writerow([*cols,*extra])
    cursor=db.execute('''SELECT * FROM address_job_rows WHERE job_id=?
           '''+(' AND decision=?' if only_changed or only_bulk else '')+''' ORDER BY row_no''',
           (job_id,'korrigiert' if only_changed else MASS_APPROVED) if (only_changed or only_bulk) else (job_id,))
    for row in cursor:
        orig=json.loads(row['original_json'])
        corrected=json.loads(row['correction_json']) if row['decision']=='korrigiert' else {}
        values=[orig.get(key,'') for key in cols] + [row['source_id'],row['result'],row['hint'],
                row['matched_egaid'],row['decision'],
                corrected.get('strasse',''),corrected.get('hausnummer',''),
                corrected.get('plz',''),corrected.get('ort',''),row['correction_basis'],ref,row['reviewed_at']]
        if split_house:
            addr=json.loads(row['normalized_json'])
            if corrected:
                added=(corrected['strasse'],corrected['hausnummer'],'bestaetigte_korrektur')
            elif row['decision']==MASS_APPROVED:
                added=(display_street_without_number(addr),addr.get('house',''),
                       'swisstopo_exakt_massengeprueft')
            else:
                added=(display_street_without_number(addr),addr.get('house',''),
                       'aus_original_getrennt_ungeprueft' if addr.get('house') else 'hausnummer_nicht_erkannt')
            values.extend(added)
        writer.writerow([_safe_csv(x) for x in values])
    return output.getvalue().encode('utf-8-sig')
