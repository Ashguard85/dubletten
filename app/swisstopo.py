"""Offline validation against a user-installed swisstopo building-address snapshot.

No network calls. Only public reference CSV/ZIP is imported. No person lookup.
Official CSV: STN_LABEL, ADR_NUMBER, ZIP_LABEL, ADR_STATUS, ADR_OFFICIAL.
Legacy separate ZIP_ZIP4 + ZIP_NAME columns are also supported.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

from engine import norm, norm_street, norm_house_number, now, plain

CSV_COLUMNS = {
    'street': ('STN_LABEL','STN_TEXT','STR_LABEL','STREET','STRASSE'),
    'number': ('ADR_NUMBER','HOUSE_NUMBER','HAUSNUMMER'),
    'postcode': ('ZIP_ZIP4','PLZ','POSTCODE','ZIP'),
    'city': ('ZIP_NAME','ZIP_PLACE','ORT','CITY'),
    'status': ('ADR_STATUS',),
    'official': ('ADR_OFFICIAL',),
    'egaid': ('ADR_EGAID',),
}
RESULT_LABELS = {
    'amtlich_aktuell': 'Amtlich, in Betrieb',
    'nicht_amtlich': 'Im Verzeichnis, nicht als amtlich markiert',
    'status_unklar': 'Im Register, Amtlichkeits- oder Betriebsstatus unklar',
    'geplant': 'Adresse projektiert',
    'aufgehoben': 'Adresse aufgehoben',
    'ort_abweichend': 'PLZ/Strasse/Hausnummer gefunden, Ortschaft abweichend',
    'hausnummer_unbekannt': 'Strasse vorhanden, Hausnummer nicht gefunden',
    'plz_abweichend': 'Adresse bei anderer PLZ gefunden',
    'nicht_gefunden': 'Kein Treffer im lokalen Register',
    'unvollstaendig': 'Adresse zu unvollständig für Prüfung',
    'ausland': 'Nicht-Schweizer Adresse',
}


def ensure_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS swisstopo_snapshots (
      snapshot_id INTEGER PRIMARY KEY, import_at TEXT NOT NULL, source_name TEXT NOT NULL,
      reference_date TEXT NOT NULL, source_sha256 TEXT NOT NULL,
      address_count INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS swisstopo_addresses (
      snapshot_id INTEGER NOT NULL REFERENCES swisstopo_snapshots(snapshot_id) ON DELETE CASCADE,
      postal TEXT NOT NULL, street TEXT NOT NULL, house TEXT NOT NULL,
      city TEXT NOT NULL, egaid TEXT NOT NULL,
      adr_status TEXT NOT NULL, official TEXT NOT NULL,
      PRIMARY KEY (snapshot_id, postal, street, house, city, egaid)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_ch_addr_exact
       ON swisstopo_addresses(snapshot_id,postal,street,house);
    CREATE INDEX IF NOT EXISTS idx_ch_addr_city
       ON swisstopo_addresses(snapshot_id,city,street,house);
    CREATE TABLE IF NOT EXISTS swisstopo_results(
      project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
      record_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
      snapshot_id INTEGER NOT NULL REFERENCES swisstopo_snapshots(snapshot_id),
      result TEXT NOT NULL, suggestion TEXT NOT NULL, matched_egaid TEXT NOT NULL,
      processed_at TEXT NOT NULL, PRIMARY KEY (project_id,record_pk)
    );
    CREATE INDEX IF NOT EXISTS idx_ch_results ON swisstopo_results(project_id,result);
    ''')
    db.commit()


def active_snapshot(db):
    row = db.execute('SELECT * FROM swisstopo_snapshots WHERE active=1 ORDER BY snapshot_id DESC LIMIT 1').fetchone()
    return dict(row) if row else None


def list_snapshots(db):
    return [dict(row) for row in db.execute('SELECT * FROM swisstopo_snapshots ORDER BY snapshot_id DESC')]


def _extract(row, name, default=''):
    for candidate in CSV_COLUMNS[name]:
        if candidate in row:
            return plain(row[candidate])
    return default


def _get_postcode(value):
    txt=plain(value)
    m=re.search(r'\b([0-9]{4})\b', txt)
    return m.group(1) if m else ''


class UnsupportedSwisstopoCSV(ValueError):
    """The CSV exists, but is not a building-address directory."""


def _valid_columns(cols):
    cols = set(cols)
    has_street = any(c in cols for c in CSV_COLUMNS['street'])
    has_number = any(c in cols for c in CSV_COLUMNS['number'])
    has_location = 'ZIP_LABEL' in cols or (
        any(c in cols for c in CSV_COLUMNS['postcode'])
        and any(c in cols for c in CSV_COLUMNS['city']))
    return has_street and has_number and has_location


def _postcode_cities(row):
    """Yield all 4-digit postcode/locality combinations from the official ZIP_LABEL.

    A ZIP_LABEL can contain '6003 Luzern, 6004 Luzern'. COM_NAME is the
    *political municipality* and must not be used as the postal locality.
    """
    labelled = plain(row.get('ZIP_LABEL', ''))
    if labelled:
        pairs = []
        for entry in labelled.split(',')[:32]:
            entry = entry.strip()
            match = re.fullmatch(r'(\d{4})\s+(.+)', entry)
            if match:
                postal, city = match.groups()
                city = norm(city)
                if city and (postal, city) not in pairs:
                    pairs.append((postal, city))
        if pairs:
            return pairs
    # Backwards-compatible support for a denormalized/hand-made CSV.
    postal = _get_postcode(_extract(row, 'postcode'))
    city = _extract(row, 'city')
    if city and re.match(r'^\d{4}\s+', city):
        city = re.sub(r'^\d{4}\s+', '', city)
    return [(postal, norm(city))] if postal and city else []


def _iter_csv(f, filename):
    # Streaming read: 400-MB CSVs are not loaded into RAM at once.
    with io.TextIOWrapper(f, encoding='utf-8-sig', errors='replace', newline='') as stream:
        head = stream.readline()
        if not head:
            return
        delimiter = ';' if head.count(';') > head.count(',') else ','
        import itertools
        reader = csv.DictReader(itertools.chain([head], stream), delimiter=delimiter)
        if not reader.fieldnames:
            raise UnsupportedSwisstopoCSV(f'{filename}: CSV-Kopfzeile nicht erkannt.')
        reader.fieldnames = [plain(x).upper() for x in reader.fieldnames]
        if not _valid_columns(reader.fieldnames):
            raise UnsupportedSwisstopoCSV(
                f'{filename}: kein Gebäudeadress-CSV. Erwartet STN_LABEL, ADR_NUMBER '
                'und ZIP_LABEL (alternativ ZIP_ZIP4 + ZIP_NAME).')
        for raw in reader:
            if raw:
                yield raw


def _members(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [x for x in archive.infolist()
                       if not x.is_dir() and x.filename.lower().endswith('.csv')
                       and not x.filename.startswith('__MACOSX/')]
            if not members:
                raise ValueError('ZIP enthält keine CSV-Datei.')
            if sum(x.file_size for x in members) > 3 * 1024**3:
                raise ValueError('ZIP enthält insgesamt zu viel unkomprimierte CSV-Daten (>3 GB).')
            recognized = 0
            for member in members:
                if member.file_size > 3 * 1024**3:
                    raise ValueError('Ein CSV-Eintrag im ZIP ist zu gross.')
                with archive.open(member) as data:
                    try:
                        # Skip unrelated CSV attachments; never extract ZIP to disk.
                        for row in _iter_csv(data, member.filename):
                            recognized += 1
                            yield row
                    except UnsupportedSwisstopoCSV:
                        continue
            if recognized == 0:
                raise ValueError('ZIP enthält keine verwertbare swisstopo-Gebäudeadress-CSV mit STN_LABEL, ADR_NUMBER und ZIP_LABEL.')
    elif path.suffix.lower() == '.csv':
        with path.open('rb') as data:
            yield from _iter_csv(data, path.name)
    else:
        raise ValueError('Datei ist keine gültige ZIP-Datei oder lokale CSV.')


def inspect_source(path):
    """Fast schema/header check without reading the complete (possibly huge) dataset."""
    path = Path(path)
    sources = []
    def check(f, name, size):
        with io.TextIOWrapper(f, encoding='utf-8-sig', errors='replace', newline='') as stream:
            head = stream.readline()
        delimiter = ';' if head.count(';') > head.count(',') else ','
        cols = [plain(c).upper() for c in next(csv.reader([head], delimiter=delimiter), [])]
        if _valid_columns(cols):
            sources.append({'file':name, 'size':size, 'columns':cols,
                            'format':'ZIP_LABEL' if 'ZIP_LABEL' in cols else 'ZIP_ZIP4 + ZIP_NAME'})
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for member in z.infolist():
                if member.is_dir() or not member.filename.lower().endswith('.csv'):
                    continue
                with z.open(member) as file:
                    check(file, member.filename, member.file_size)
    elif path.suffix.lower()=='.csv':
        with path.open('rb') as file:
            check(file, path.name, path.stat().st_size)
    if not sources:
        raise ValueError('Keine Gebäudeadress-CSV gefunden. Benötigt STN_LABEL, ADR_NUMBER und ZIP_LABEL (oder ZIP_ZIP4/ZIP_NAME).')
    return sources


def _official_status(value):
    t=plain(value).casefold()
    return {'1':'real','real':'real','in betrieb':'real',
            '0':'planned','planned':'planned','projektiert':'planned',
            '2':'outdated','outdated':'outdated','aufgehoben':'outdated'}.get(t,t)


def _official(value):
    t=plain(value).casefold()
    if t in ('1','true','yes','ja'): return '1'
    if t in ('0','false','no','nein'): return '0'
    return ''


def import_snapshot(db, file_path, reference_date, progress=None):
    """Import a complete public reference snapshot, atomically activate on success.

    Date is explicitly supplied, never made up from import time. Source SHA256 is
    stored as integrity identifier. A snapshot's rows are never overwritten.
    """
    ensure_schema(db)
    progress=progress or (lambda _:None)
    path=Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('Lokale swisstopo-Datei nicht gefunden.')
    if path.stat().st_size > 1_500_000_000:
        raise ValueError('Referenzdatei zu gross (>1.5GB).')
    try:
        date.fromisoformat(reference_date)
    except (TypeError, ValueError) as exc:
        raise ValueError('Datenstand muss YYYY-MM-DD sein.') from exc
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):
            digest.update(block)
    total=0
    try:
        with db:
            sid=db.execute('''INSERT INTO swisstopo_snapshots(import_at,source_name,reference_date,source_sha256,address_count)
                              VALUES(?,?,?,?,0)''', (now(),path.name,reference_date,digest.hexdigest())).lastrowid
            batch=[]
            for row in _members(path):
                street=_extract(row,'street')
                number=norm_house_number(_extract(row,'number'))
                locations=_postcode_cities(row)
                egaid=_extract(row,'egaid')
                if not street or not locations:
                    continue
                aliases=[norm_street(x) for x in street.split(' / ') if norm_street(x)]
                for postal, city in locations:
                    for street_name in dict.fromkeys(aliases):
                        batch.append((sid, postal, street_name, number, city, egaid,
                                      _official_status(_extract(row,'status')),_official(_extract(row,'official'))))
                total+=1
                if total>6_000_000:
                    raise ValueError('Zu viele Datensätze: max. 6 Millionen.')
                if len(batch)>=5000:
                    db.executemany('''INSERT OR IGNORE INTO swisstopo_addresses
                        (snapshot_id,postal,street,house,city,egaid,adr_status,official)
                        VALUES(?,?,?,?,?,?,?,?)''',batch)
                    batch.clear()
                    if total%100000<5000:
                        progress(f'{total:,} amtliche Datensätze eingelesen')
            if batch:
                db.executemany('''INSERT OR IGNORE INTO swisstopo_addresses
                    (snapshot_id,postal,street,house,city,egaid,adr_status,official)
                    VALUES(?,?,?,?,?,?,?,?)''',batch)
            if total==0:
                raise ValueError('Keine verwertbaren Gebäudeadressen in der Datei.')
            db.execute('UPDATE swisstopo_snapshots SET active=0 WHERE active=1')
            count=db.execute('SELECT count(*) FROM swisstopo_addresses WHERE snapshot_id=?',(sid,)).fetchone()[0]
            db.execute('UPDATE swisstopo_snapshots SET active=1,address_count=? WHERE snapshot_id=?',(count,sid))
    except Exception:
        raise
    return sid, count


def _candidate(row):
    # Ranking favours current, official entries, but only returns them as facts
    # if the row itself explicitly carries their statuses.
    return (row['adr_status']=='real', row['official']=='1',bool(row['egaid']))


def validate_record(db, snapshot_id, rec):
    if plain(json.loads(rec['original_json']).get('country')).casefold() not in ('', 'ch','che','schweiz','suisse','svizzera','switzerland'):
        return ('ausland','', '')
    postal=rec['postal']; street=rec['street_name']; house=rec['house_number']; city=rec['city']
    if not postal or not street or not house:
        return ('unvollstaendig','', '')
    same_city=db.execute('''SELECT * FROM swisstopo_addresses
                     WHERE snapshot_id=? AND postal=? AND street=? AND house=? AND city=? LIMIT 30''',
                    (snapshot_id,postal,street,house,city)).fetchall() if city else []
    rows=[]
    if not same_city:
        rows=db.execute('''SELECT * FROM swisstopo_addresses
                     WHERE snapshot_id=? AND postal=? AND street=? AND house=? LIMIT 30''',
                    (snapshot_id,postal,street,house)).fetchall()
        if not city:
            same_city=rows
    if same_city:
        best=sorted(same_city,key=_candidate,reverse=True)[0]
        if best['adr_status']=='planned': state='geplant'
        elif best['adr_status']=='outdated': state='aufgehoben'
        elif best['adr_status']=='real' and best['official']=='1':
            state='amtlich_aktuell'
        elif best['official']=='0':
            state='nicht_amtlich'
        else:
            state='status_unklar'
        return (state, f"{postal} {best['city']} · {street} {house}",best['egaid'])
    if rows:
        best=sorted(rows,key=_candidate,reverse=True)[0]
        return ('ort_abweichend',f"{postal} {best['city']} · {street} {house}",best['egaid'])
    rows=db.execute('''SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND street=?
                      LIMIT 10''',(snapshot_id,postal,street)).fetchall()
    if rows:
        return ('hausnummer_unbekannt',f"Strasse vorhanden, mögliche Hausnummern: {', '.join(dict.fromkeys(r['house'] for r in rows))}",'')
    if city:
        rows=db.execute('''SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND city=? AND street=? AND house=? LIMIT 10''',
                        (snapshot_id,city,street,house)).fetchall()
        if rows:
            best=rows[0]
            return ('plz_abweichend',f"{best['postal']} {best['city']} · {street} {house}",best['egaid'])
    return ('nicht_gefunden','','')


def validate_project(db,pid,snapshot_id=None,progress=None):
    """Recheck entire project, persist snapshot ID with every result."""
    ensure_schema(db)
    snap=active_snapshot(db) if snapshot_id is None else db.execute(
        'SELECT * FROM swisstopo_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
    if not snap:
        raise ValueError('Kein lokaler swisstopo-Datenstand vorhanden.')
    sid=snap['snapshot_id']
    progress=progress or (lambda _:None)
    done=0
    with db:
        db.execute('DELETE FROM swisstopo_results WHERE project_id=?',(pid,))
        batch=[]
        for row in db.execute('SELECT * FROM records WHERE project_id=? ORDER BY pk',(pid,)):
            status, hint, egaid=validate_record(db,sid,row)
            batch.append((pid,row['pk'],sid,status,hint,egaid,now()))
            done+=1
            if len(batch)>=1000:
                db.executemany('''INSERT INTO swisstopo_results(project_id,record_pk,snapshot_id,result,
                   suggestion,matched_egaid,processed_at) VALUES(?,?,?,?,?,?,?)''',batch)
                batch.clear()
                if done%10000<1000:
                    progress(f'{done:,} CRM-Adressen lokal geprüft')
        if batch:
            db.executemany('''INSERT INTO swisstopo_results(project_id,record_pk,snapshot_id,result,
                suggestion,matched_egaid,processed_at) VALUES(?,?,?,?,?,?,?)''',batch)
    return done


def validation_counts(db,pid):
    return [dict(r) for r in db.execute('''SELECT rec.side, res.result, count(*) n
        FROM swisstopo_results res JOIN records rec ON rec.pk=res.record_pk
        WHERE res.project_id=? GROUP BY rec.side,res.result ORDER BY rec.side,res.result''',(pid,))]


def export_validation(db,pid):
    import pandas as pd
    query='''SELECT r.side crm, r.source_id crm_id,r.street_name strasse,r.house_number hausnummer,
         r.postal plz,r.city ort,v.result validierungsstatus,v.suggestion korrekturvorschlag,
         v.matched_egaid swisstopo_egaid, s.reference_date datenstand,
         s.source_sha256 quell_sha256
         FROM swisstopo_results v JOIN records r ON r.pk=v.record_pk
         JOIN swisstopo_snapshots s ON s.snapshot_id=v.snapshot_id
         WHERE v.project_id=? ORDER BY r.side,r.source_id'''
    return pd.read_sql_query(query,db,params=(pid,)).to_csv(index=False,sep=';').encode('utf-8-sig')
