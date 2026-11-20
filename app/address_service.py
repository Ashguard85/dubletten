"""Standalone Swiss-address quality check, immutable originals and reviewed exports."""
from __future__ import annotations
import csv,hashlib,io,json,re
from pathlib import Path
import pandas as pd
from rapidfuzz import fuzz
from engine import plain,norm,norm_street,norm_house_number,split_street_house,read_upload,now
from swisstopo import active_snapshot,validate_record,ensure_schema as ensure_sw
REVIEW_STATES=('offen','korrigiert','keine_aenderung','uebersprungen','massenfreigegeben')
MASS_APPROVED='massenfreigegeben'
ALIASES={'id':('ID','Kundennummer','Nr'), 'street':('Strasse','Straße','Adresse','Street'),
 'house_number':('Hausnummer','Hausnr','House_Number'), 'postcode':('PLZ','Postleitzahl','ZIP'),
 'city':('Ort','Stadt','City'), 'country':('Land','Country')}

def ensure_schema(db):
 ensure_sw(db)
 db.executescript('''CREATE TABLE IF NOT EXISTS address_jobs(
 job_id INTEGER PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,file_name TEXT NOT NULL,
 snapshot_id INTEGER NOT NULL REFERENCES swisstopo_snapshots(snapshot_id),
 column_names_json TEXT NOT NULL,mapping_json TEXT NOT NULL,row_count INTEGER NOT NULL DEFAULT 0);
 CREATE TABLE IF NOT EXISTS address_job_rows(
 row_pk INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES address_jobs(job_id) ON DELETE CASCADE,
 row_no INTEGER NOT NULL,source_id TEXT NOT NULL,original_json TEXT NOT NULL,normalized_json TEXT NOT NULL,
 result TEXT NOT NULL,hint TEXT NOT NULL,proposals_json TEXT NOT NULL DEFAULT '[]',
 matched_egaid TEXT NOT NULL DEFAULT '',decision TEXT NOT NULL DEFAULT 'offen',
 correction_json TEXT NOT NULL DEFAULT '{}',reviewed_at TEXT NOT NULL DEFAULT '',
 correction_basis TEXT NOT NULL DEFAULT '',UNIQUE(job_id,row_no));
 CREATE INDEX IF NOT EXISTS idx_addr_jobs ON address_job_rows(job_id,result,decision);''')
 db.commit()
def guess_column(cols,field):
 index={norm(c).replace(' ',''):c for c in cols}
 return next((index[norm(alias).replace(' ','')] for alias in ALIASES[field] if norm(alias).replace(' ','') in index),'')
def read_single_upload(upload):return read_upload(upload)
def display_street_without_number(addr):
 raw=plain(addr.get('street_source',''));house=norm_house_number(addr.get('house',''))
 if not raw or not house:return raw
 m=re.fullmatch(r'(.+?)[,\s]+(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)',raw.replace('–','-'))
 if m and norm_house_number(m[2])==house:return raw[:len(m[1])]
 m=re.fullmatch(r'(\d{1,5}[a-zA-Z]?)[,\s]+(.+)',raw)
 if m and norm_house_number(m[1])==house:return m[2]
 return raw
def _normalized(original,mapping):
 def get(k):return plain(original.get(mapping.get(k,''))) if mapping.get(k) else ''
 st,house,conflict=split_street_house(get('street'),get('house_number'))
 postal=get('postcode').upper().replace(' ','')
 if postal.startswith('CH') and len(postal)==6:postal=postal[2:]
 return dict(street=st,house=house,postal=postal,city=norm(get('city')),country=get('country'),
   street_source=get('street'),house_source=get('house_number'),postal_source=get('postcode'),
   city_source=get('city'),split_conflict=bool(conflict))
def _canonical(row):
 return {'strasse':row['street_label'] or row['street'],'hausnummer':row['number_label'] or row['house'],
  'plz':row['postal'],'ort':row['city_label'] or row['city'],'egaid':row['egaid'],
  'status':row['adr_status'],'amtlich':row['official']=='1'}
def _proposals(db,sid,addr,result):
 p,s,h,c=addr['postal'],addr['street'],addr['house'],addr['city']
 rows=[]
 if result in ('amtlich_aktuell','geplant','aufgehoben','nicht_amtlich','status_unklar'):
  rows=db.execute('SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND street=? AND house=? LIMIT 10',(sid,p,s,h)).fetchall()
 elif result=='ort_abweichend':
  rows=db.execute('SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND street=? AND house=? LIMIT 30',(sid,p,s,h)).fetchall()
 elif result=='plz_abweichend' and c:
  rows=db.execute('SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND city=? AND street=? AND house=? LIMIT 30',(sid,c,s,h)).fetchall()
 elif result=='nicht_gefunden' and h and p and s:
  if c:rows=db.execute('SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND house=? AND city=? LIMIT 2500',(sid,p,h,c)).fetchall()
  else:rows=db.execute('SELECT * FROM swisstopo_addresses WHERE snapshot_id=? AND postal=? AND house=? LIMIT 2500',(sid,p,h)).fetchall()
  rows=[r for r in rows if fuzz.ratio(s,r['street'])>=88]
 # Unknown house never receives a guessed replacement.
 output=[];seen=set()
 for r in sorted(rows,key=lambda x:(x['adr_status']=='real',x['official']=='1',fuzz.ratio(s,x['street'])),reverse=True):
  key=(r['postal'],r['street'],r['house'],r['city'])
  if key in seen:continue
  seen.add(key);proposal=_canonical(r)
  proposal['hinweis']='Kandidat aus lokalem swisstopo-Register; fachlich prüfen'
  output.append(proposal)
  if len(output)>=3:break
 return output
def validate_address(db,sid,addr):
 if addr['split_conflict']:return 'eingabe_konflikt','Abweichende Hausnummer in Strassenfeld und separater Spalte','',[]
 fake={'original_json':json.dumps({'country':addr['country']}),'street_name':addr['street'],
       'house_number':addr['house'],'postal':addr['postal'],'city':addr['city']}
 result,hint,egaid=validate_record(db,sid,fake)
 proposals=_proposals(db,sid,addr,result)
 if result=='nicht_gefunden' and proposals:
  result='strasse_aehnlich';hint='Ähnliche Strasse gefunden – Vorschlag manuell überprüfen.'
 return result,hint,egaid,proposals
def import_job(db,frame,title,file_name,mapping,snapshot_id=None,progress=None):
 ensure_schema(db)
 snap=active_snapshot(db) if snapshot_id is None else db.execute('SELECT * FROM swisstopo_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
 if not snap:raise ValueError('Zuerst einen lokalen swisstopo-Stand importieren.')
 if not mapping.get('street') or not mapping.get('postcode'):raise ValueError('Strasse und PLZ zuordnen.')
 if len(frame)>150000:raise ValueError('Maximal 150’000 Zeilen.')
 cols=list(frame.columns)
 if any(x not in cols for x in mapping.values() if x):raise ValueError('Zugeordnete Spalte fehlt.')
 progress=progress or (lambda x:None)
 with db:
  jid=db.execute('INSERT INTO address_jobs(title,created_at,file_name,snapshot_id,column_names_json,mapping_json,row_count) VALUES(?,?,?,?,?,?,?)',
    (plain(title) or 'Adressprüfung',now(),Path(file_name).name,snap['snapshot_id'],json.dumps(cols,ensure_ascii=False),json.dumps(mapping,ensure_ascii=False),len(frame))).lastrowid
  batch=[]
  for i,row in enumerate(frame.itertuples(index=False,name=None),1):
   source=dict(zip(cols,[plain(x) for x in row]));addr=_normalized(source,mapping)
   result,hint,egaid,proposals=validate_address(db,snap['snapshot_id'],addr)
   batch.append((jid,i,source.get(mapping.get('id'), '') or str(i),json.dumps(source,ensure_ascii=False),
      json.dumps(addr,ensure_ascii=False),result,hint,json.dumps(proposals,ensure_ascii=False),egaid))
   if len(batch)==500:
    db.executemany('''INSERT INTO address_job_rows(job_id,row_no,source_id,original_json,normalized_json,result,hint,proposals_json,matched_egaid)
     VALUES(?,?,?,?,?,?,?,?,?)''',batch);batch=[]
   if i%5000==0:progress(f'{i:,} Adressen geprüft')
  if batch:db.executemany('''INSERT INTO address_job_rows(job_id,row_no,source_id,original_json,normalized_json,result,hint,proposals_json,matched_egaid)
     VALUES(?,?,?,?,?,?,?,?,?)''',batch)
 return jid
def list_jobs(db):
 return [dict(r) for r in db.execute('''SELECT j.*,s.reference_date snapshot_date FROM address_jobs j
 JOIN swisstopo_snapshots s ON s.snapshot_id=j.snapshot_id ORDER BY j.job_id DESC''')]
def job_counts(db,jid):
 return [dict(r) for r in db.execute('SELECT result,decision,count(*) n FROM address_job_rows WHERE job_id=? GROUP BY result,decision',(jid,))]
def page_rows(db,jid,status='Alle',decision='Alle',needle='',limit=50,offset=0):
 wh=['job_id=?'];args=[jid]
 if status!='Alle':wh.append('result=?');args.append(status)
 if decision!='Alle':wh.append('decision=?');args.append(decision)
 if needle:
  needle=plain(needle)[:120].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
  wh.append("(source_id LIKE ? ESCAPE '\\' OR original_json LIKE ? ESCAPE '\\')");args.extend(['%'+needle+'%']*2)
 where=' AND '.join(wh);total=db.execute('SELECT count(*) FROM address_job_rows WHERE '+where,args).fetchone()[0]
 rows=db.execute('SELECT * FROM address_job_rows WHERE '+where+' ORDER BY row_no LIMIT ? OFFSET ?',args+[max(1,min(100,int(limit))),max(0,int(offset))]).fetchall()
 return total,[dict(r) for r in rows]
def save_review(db,jid,row_pk,decision,corrected=None):
 if decision not in REVIEW_STATES or decision==MASS_APPROVED:raise ValueError('Prüfentscheid nicht erlaubt.')
 row=db.execute('SELECT * FROM address_job_rows WHERE job_id=? AND row_pk=?',(jid,row_pk)).fetchone()
 if row is None:raise ValueError('Datensatz gehört nicht zu diesem Auftrag.')
 basis='';values={}
 if decision=='korrigiert':
  values={k:plain((corrected or {}).get(k,'')) for k in ('strasse','hausnummer','plz','ort')}
  if not values['strasse'] or not norm_house_number(values['hausnummer']) or not re.fullmatch(r'\d{4}',values['plz']):
   raise ValueError('Strasse, gültige Hausnummer und vierstellige PLZ benötigt.')
  basis='manuell_nicht_amtlich_verifiziert'
  for p in json.loads(row['proposals_json']):
   if (p['status']=='real' and p['amtlich'] and norm(values['strasse'])==norm(p['strasse'])
       and norm_house_number(values['hausnummer'])==norm_house_number(p['hausnummer'])
       and values['plz']==p['plz'] and norm(values['ort'])==norm(p['ort'])):
    basis='swisstopo_vorschlag_bestaetigt';break
 with db:
  db.execute('UPDATE address_job_rows SET decision=?,correction_json=?,correction_basis=?,reviewed_at=? WHERE row_pk=? AND job_id=?',
             (decision,json.dumps(values,ensure_ascii=False),basis,now(),row_pk,jid))
def _strict_rows(db,jid):
 if not db.execute('SELECT 1 FROM address_jobs WHERE job_id=?',(jid,)).fetchone():raise ValueError('Auftrag nicht gefunden.')
 query='''SELECT r.row_pk,r.source_id,r.row_no,r.normalized_json,COUNT(*) ct,MIN(s.egaid) egaid,
 MIN(s.street_label) street_label,MIN(s.city_label) city_label,MIN(s.number_label) number_label
 FROM address_job_rows r JOIN address_jobs j ON j.job_id=r.job_id
 JOIN swisstopo_addresses s ON s.snapshot_id=j.snapshot_id
 AND s.postal=json_extract(r.normalized_json,'$.postal')
 AND s.street=json_extract(r.normalized_json,'$.street')
 AND s.house=json_extract(r.normalized_json,'$.house')
 AND s.city=json_extract(r.normalized_json,'$.city')
 WHERE r.job_id=? AND r.decision='offen' AND r.result='amtlich_aktuell'
 AND json_extract(r.normalized_json,'$.city')!=''
 AND json_extract(r.normalized_json,'$.split_conflict')=0
 GROUP BY r.row_pk HAVING COUNT(*)=1 AND MIN(s.adr_status)='real' AND MIN(s.official)='1'
 ORDER BY r.row_no'''
 for r in db.execute(query,(jid,)):
  a=json.loads(r['normalized_json'])
  if not all((r['egaid'],r['street_label'],r['city_label'],r['number_label'],a['street_source'],a['postal_source'],a['city_source'],a['house'])):continue
  if plain(a.get('country')).casefold() not in ('','ch','che','schweiz','suisse','svizzera','switzerland'):continue
  if norm(display_street_without_number(a))!=norm(r['street_label']):continue
  if norm_house_number(a['house'])!=norm_house_number(r['number_label']):continue
  if not re.fullmatch(r'\d{4}',a['postal']):continue
  if norm(a['city_source'])!=norm(r['city_label']):continue
  yield r
def bulk_approval_preview(db,jid,sample_size=8):
 digest=hashlib.sha256();examples=[];count=0
 for r in _strict_rows(db,jid):
  digest.update(f"{r['row_pk']}:{r['egaid']}\n".encode());count+=1
  if len(examples)<sample_size:
   a=json.loads(r['normalized_json'])
   examples.append({'ID':r['source_id'],'Strasse':display_street_without_number(a),
      'Hausnummer':a['house'],'PLZ':a['postal'],'Ort':a['city_source'],'EGAID':r['egaid']})
 return {'count':count,'examples':examples,'digest':digest.hexdigest()}
def bulk_approve_exact(db,jid,expected_count,expected_digest):
 if type(expected_count) is not int or expected_count<=0:raise ValueError('Keine freigabefähige Adresse.')
 with db:
  db.execute('BEGIN IMMEDIATE')
  rows=list(_strict_rows(db,jid));digest=hashlib.sha256()
  for r in rows:digest.update(f"{r['row_pk']}:{r['egaid']}\n".encode())
  if len(rows)!=expected_count or digest.hexdigest()!=expected_digest:
   raise ValueError('Vorschau hat sich verändert – bitte erneut prüfen.')
  db.executemany('''UPDATE address_job_rows SET decision='massenfreigegeben',
     correction_basis='swisstopo_exakt_massengeprueft',reviewed_at=? WHERE job_id=? AND row_pk=? AND decision='offen' ''',
     [(now(),jid,r['row_pk']) for r in rows])
 return len(rows)
def delete_job(db,jid):
 with db:db.execute('DELETE FROM address_jobs WHERE job_id=?',(jid,))
def _safe(v):
 s=plain(v)
 return "'"+s if s.lstrip()[:1] in ('=','+','-','@') else s
def export_job(db,jid,only_changed=False,split_house=False,street_column='Ziel_Strasse',house_column='Ziel_Hausnummer',only_bulk=False):
 if only_bulk and only_changed:raise ValueError('Nur einen Exportfilter.')
 job=db.execute('SELECT * FROM address_jobs WHERE job_id=?',(jid,)).fetchone()
 if not job:raise ValueError('Unbekannter Auftrag.')
 cols=json.loads(job['column_names_json']);extra=['Pruefung_ID','Swisstopo_Status','Hinweis','Amtliche_EGAID','Pruefentscheid','Korrigiert_Strasse','Korrigiert_Hausnummer','Korrigiert_PLZ','Korrigiert_Ort','Korrekturquelle','Swisstopo_Datenstand','Pruefdatum']
 if split_house:
  if not all((street_column,house_column)) or street_column==house_column or any(n in cols+extra+['Zieladresse_Basis'] for n in (street_column,house_column)):
   raise ValueError('Zielspalten müssen unterschiedliche, neue Namen haben.')
  extra+=[street_column,house_column,'Zieladresse_Basis']
 snapshot=db.execute('SELECT reference_date FROM swisstopo_snapshots WHERE snapshot_id=?',(job['snapshot_id'],)).fetchone()[0]
 buf=io.StringIO();w=csv.writer(buf,delimiter=';',lineterminator='\n');w.writerow(cols+extra)
 state='massenfreigegeben' if only_bulk else 'korrigiert' if only_changed else None
 wh=' AND decision=?' if state else ''
 for r in db.execute('SELECT * FROM address_job_rows WHERE job_id=?'+wh+' ORDER BY row_no',(jid,state) if state else (jid,)):
  raw=json.loads(r['original_json']);fix=json.loads(r['correction_json']) if r['decision']=='korrigiert' else {}
  values=[raw.get(k,'') for k in cols]+[r['source_id'],r['result'],r['hint'],r['matched_egaid'],r['decision'],
          *(fix.get(k,'') for k in ('strasse','hausnummer','plz','ort')),r['correction_basis'],snapshot,r['reviewed_at']]
  if split_house:
   a=json.loads(r['normalized_json'])
   values.extend(([fix['strasse'],fix['hausnummer'],'bestaetigte_korrektur'] if fix else
      [display_street_without_number(a),a['house'],'swisstopo_exakt_massengeprueft' if r['decision']==MASS_APPROVED else
       'aus_original_getrennt_ungeprueft' if a['house'] else 'hausnummer_nicht_erkannt']))
  w.writerow([_safe(v) for v in values])
 return buf.getvalue().encode('utf-8-sig')
