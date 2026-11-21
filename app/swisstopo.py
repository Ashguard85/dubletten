"""Streaming, offline importer of the Swiss official building-address directory.

Accepts official ZIP_LABEL (e.g. '6048 Horw') or legacy ZIP_ZIP4/ZIP_NAME.
No outgoing address lookups; reference is immutable per imported snapshot.
"""
from __future__ import annotations
import csv,hashlib,io,json,re,sqlite3,zipfile
from datetime import datetime
from pathlib import Path
from rapidfuzz import fuzz
from engine import plain,norm,norm_street,norm_house_number,now

RESULT_LABELS={
 'amtlich_aktuell':'Aktuelle amtliche Gebäudeadresse',
 'strasse_aehnlich':'Ähnlicher Strassenname',
 'hausnummer_unbekannt':'Strasse vorhanden, Hausnummer nicht gefunden',
 'plz_abweichend':'PLZ abweichend',
 'ort_abweichend':'Ort abweichend',
 'geplant':'Adresse projektiert',
 'aufgehoben':'Adresse aufgehoben',
 'nicht_amtlich':'Kein amtlich bestätigter Eintrag',
 'status_unklar':'Amtlicher Status unklar',
 'nicht_gefunden':'Keine passende Gebäudeadresse gefunden',
 'unvollstaendig':'Adresse unvollständig',
 'ausland':'Adresse ausserhalb der Schweiz',
 'eingabe_konflikt':'Widerspruch in der Eingabe',
}

def ensure_schema(db):
 db.executescript('''
 CREATE TABLE IF NOT EXISTS swisstopo_snapshots(
   snapshot_id INTEGER PRIMARY KEY, reference_date TEXT NOT NULL,source_sha256 TEXT NOT NULL,
   imported_at TEXT NOT NULL,address_count INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 0);
 CREATE TABLE IF NOT EXISTS swisstopo_addresses(
   id INTEGER PRIMARY KEY,snapshot_id INTEGER NOT NULL REFERENCES swisstopo_snapshots(snapshot_id) ON DELETE CASCADE,
   postal TEXT NOT NULL,street TEXT NOT NULL,house TEXT NOT NULL,city TEXT NOT NULL,egaid TEXT NOT NULL,
   street_label TEXT NOT NULL DEFAULT '',city_label TEXT NOT NULL DEFAULT '',number_label TEXT NOT NULL DEFAULT '',
   adr_status TEXT NOT NULL DEFAULT 'real',official TEXT NOT NULL DEFAULT '1');
 CREATE INDEX IF NOT EXISTS ix_swisstopo_address ON swisstopo_addresses(snapshot_id,postal,street,house,city);
 CREATE INDEX IF NOT EXISTS ix_swisstopo_city ON swisstopo_addresses(snapshot_id,city,street,house);
 CREATE INDEX IF NOT EXISTS ix_swisstopo_street ON swisstopo_addresses(snapshot_id,postal,street);
 CREATE TABLE IF NOT EXISTS swisstopo_validation(
   project_id INTEGER NOT NULL,pk INTEGER NOT NULL,result TEXT NOT NULL,hint TEXT NOT NULL,
   egaid TEXT NOT NULL,snapshot_id INTEGER NOT NULL,PRIMARY KEY(project_id,pk));
 ''')
 fields={r['name'] for r in db.execute('PRAGMA table_info(swisstopo_addresses)')}
 for k in ('street_label','city_label','number_label'):
  if k not in fields:db.execute(f"ALTER TABLE swisstopo_addresses ADD COLUMN {k} TEXT NOT NULL DEFAULT ''")
 db.commit()
def active_snapshot(db):return db.execute('SELECT * FROM swisstopo_snapshots WHERE active=1 ORDER BY snapshot_id DESC LIMIT 1').fetchone()
def list_snapshots(db):return [dict(r) for r in db.execute('SELECT * FROM swisstopo_snapshots ORDER BY snapshot_id DESC')]
def _open_rows(path):
 path=Path(path)
 if path.suffix.lower()=='.zip':
  with zipfile.ZipFile(path) as z:
   members=[m for m in z.infolist() if m.filename.lower().endswith('.csv') and not m.is_dir()]
   members.sort(key=lambda m:m.file_size,reverse=True)
   if not members:raise ValueError('Die ZIP enthält keine CSV.')
   for entry in members:
    with z.open(entry) as binary:
     with io.TextIOWrapper(binary,encoding='utf-8-sig',errors='replace',newline='') as txt:
      peek=txt.readline()
      delimiter=';' if peek.count(';')>=peek.count(',') else ','
      from itertools import chain
      reader=csv.DictReader(chain([peek],txt),delimiter=delimiter)
      headers={plain(x) for x in (reader.fieldnames or [])}
      if {'STN_LABEL','ADR_NUMBER'}.issubset(headers) and ('ZIP_LABEL' in headers or 'ZIP_ZIP4' in headers):
       yield entry.filename,reader
 elif path.suffix.lower()=='.csv':
  with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as txt:
   peek=txt.readline();delimiter=';' if peek.count(';')>=peek.count(',') else ','
   from itertools import chain
   reader=csv.DictReader(chain([peek],txt),delimiter=delimiter)
   yield path.name,reader
 else:raise ValueError('Nur CSV oder ZIP erlaubt.')
def inspect_source(path):
 if not Path(path).is_file():raise ValueError('Referenzdatei nicht vorhanden.')
 found=[]
 for name,rows in _open_rows(path):
  cols={plain(x) for x in (rows.fieldnames or [])}
  if {'STN_LABEL','ADR_NUMBER'}.issubset(cols) and ('ZIP_LABEL' in cols or {'ZIP_ZIP4','ZIP_NAME'}.issubset(cols)):
   found.append({'member':name,'format':'ZIP_LABEL' if 'ZIP_LABEL' in cols else 'ZIP_ZIP4 / ZIP_NAME'})
 if not found:raise ValueError('Keine gültigen swisstopo-Adressspalten. Benötigt STN_LABEL, ADR_NUMBER und ZIP_LABEL (oder ZIP_ZIP4 und ZIP_NAME).')
 return found
def _status(value):
 s=plain(value).lower()
 return {'1':'real','0':'geplant','2':'aufgehoben','real':'real','planned':'geplant','retired':'aufgehoben'}.get(s,'status_unklar')
def _postal_places(row):
 label=plain(row.get('ZIP_LABEL'))
 if label:
  matches=[]
  for item in label.split(',')[:32]:
   m=re.match(r'^\s*(\d{4})\s+(.+?)\s*$',item)
   if m:matches.append((m[1],m[2]))
  if matches:return matches
 post=plain(row.get('ZIP_ZIP4') or row.get('ZIP'))
 city=plain(row.get('ZIP_NAME') or row.get('COM_NAME'))
 return [(post,city)] if post and city else []
def import_snapshot(db,path,reference_date,progress=None):
 inspect_source(path)
 progress=progress or (lambda msg:None)
 if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',plain(reference_date)):
  raise ValueError('swisstopo-Bezugsdatum als YYYY-MM-DD angeben.')
 datetime.strptime(reference_date,'%Y-%m-%d')
 sha=hashlib.sha256()
 with open(path,'rb') as binary:
  for chunk in iter(lambda:binary.read(2**20),b''):sha.update(chunk)
 with db:
  sid=db.execute('INSERT INTO swisstopo_snapshots(reference_date,source_sha256,imported_at) VALUES(?,?,?)',
                 (reference_date,sha.hexdigest(),now())).lastrowid
  batch=[];count=0
  for member,rows in _open_rows(path):
   for row in rows:
    street_raw=plain(row.get('STN_LABEL'));number_raw=plain(row.get('ADR_NUMBER'))
    nstreet=norm_street(street_raw);nnumber=norm_house_number(number_raw)
    if not nstreet or not nnumber:continue
    for post,city in _postal_places(row):
     if not re.fullmatch(r'\d{4}',post) or not norm(city):continue
     batch.append((sid,post,nstreet,nnumber,norm(city),plain(row.get('ADR_EGAID')),street_raw,city,number_raw,
                   _status(row.get('ADR_STATUS') or '1'), '0' if plain(row.get('ADR_OFFICIAL') or '1').lower() in ('0','false','no') else '1'))
    if len(batch)>=5000:
     db.executemany('''INSERT INTO swisstopo_addresses(snapshot_id,postal,street,house,city,egaid,street_label,city_label,number_label,adr_status,official)
       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',batch);count+=len(batch);batch=[]
     if count%50000==0:progress(f'{count:,} Referenzadressen importiert')
  if batch:
   db.executemany('''INSERT INTO swisstopo_addresses(snapshot_id,postal,street,house,city,egaid,street_label,city_label,number_label,adr_status,official)
       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',batch);count+=len(batch)
  if count==0:raise ValueError('Keine gültigen amtlichen Gebäudeadressen im Datenstand.')
  db.execute('UPDATE swisstopo_snapshots SET active=0')
  db.execute('UPDATE swisstopo_snapshots SET active=1,address_count=? WHERE snapshot_id=?',(count,sid))
 return sid,count

def validate_record(db,sid,record):
 # CRM records come from SQLite as sqlite3.Row, which has indexing but no
 # .get() method. Standalone callers may pass dicts; normalize both here.
 record = dict(record)
 raw=json.loads(record.get('original_json') or '{}')
 country=plain(raw.get('country'))
 if country and norm(country) not in ('ch','che','schweiz','suisse','svizzera','switzerland'):
  return 'ausland','Nichtschweizer Adresse',''
 post=plain(record.get('postal'));street=plain(record.get('street_name'));house=plain(record.get('house_number'));city=plain(record.get('city'))
 if not post or not street or not house:return 'unvollstaendig','Strasse, Hausnummer oder PLZ fehlt',''
 rows=db.execute('''SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND street=? AND house=? LIMIT 60''',
                 (sid,post,street,house)).fetchall()
 if rows:
  exact=[r for r in rows if not city or r['city']==city]
  if not exact:return 'ort_abweichend','Ort weicht vom gefundenen amtlichen Eintrag ab',''
  best=next((r for r in exact if r['adr_status']=='real' and r['official']=='1'),exact[0])
  if best['adr_status']=='geplant':return 'geplant','Gebäudeadresse ist projektiert',best['egaid']
  if best['adr_status']=='aufgehoben':return 'aufgehoben','Adresse ist aufgehoben',best['egaid']
  if best['adr_status']!='real':return 'status_unklar','Status im Datenstand unklar',best['egaid']
  if best['official']!='1':return 'nicht_amtlich','Nicht als amtlich bestätigt',best['egaid']
  return 'amtlich_aktuell','Aktuelle amtliche Gebäudeadresse',best['egaid']
 if city and db.execute('''SELECT 1 FROM swisstopo_addresses WHERE snapshot_id=? AND city=? AND street=? AND house=? LIMIT 1''',(sid,city,street,house)).fetchone():
  return 'plz_abweichend','PLZ passt nicht zur angegebenen Adresse',''
 candidate=db.execute('SELECT house FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND street=? LIMIT 15',(sid,post,street)).fetchall()
 if candidate:
  nums=', '.join(dict.fromkeys(x['house'] for x in candidate))
  return 'hausnummer_unbekannt',f'Strasse vorhanden, mögliche Hausnummern: {nums}',''
 return 'nicht_gefunden','Kein Gebäudeadress-Treffer',''
def validate_project(db,pid,progress=None):
 sid=active_snapshot(db)
 if not sid:raise ValueError('Kein swisstopo-Datenstand installiert.')
 with db:
  db.execute('DELETE FROM swisstopo_validation WHERE project_id=?',(pid,))
  for row in db.execute("SELECT * FROM records WHERE project_id=? AND side='A'",(pid,)):
   result,hint,egaid=validate_record(db,sid['snapshot_id'],row)
   db.execute('INSERT INTO swisstopo_validation VALUES(?,?,?,?,?,?)',(pid,row['pk'],result,hint,egaid,sid['snapshot_id']))
def validation_counts(db,pid):
 return [dict(r) for r in db.execute('SELECT result,count(*) n FROM swisstopo_validation WHERE project_id=? GROUP BY result',(pid,))]
def export_validation(db,pid):
 import pandas as pd
 return pd.read_sql_query('''SELECT r.source_id A_ID,v.result Status,v.hint Hinweis,v.egaid EGAID,s.reference_date Datenstand
   FROM swisstopo_validation v JOIN records r ON r.pk=v.pk JOIN swisstopo_snapshots s ON s.snapshot_id=v.snapshot_id
   WHERE v.project_id=?''',db,params=(pid,)).to_csv(index=False,sep=';').encode('utf-8-sig')
