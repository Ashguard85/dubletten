"""Local CSV/XLSX data import, bounded CRM matching and review storage.

No CRM API writes. Source values stored both as user columns and normalized fields.
"""
from __future__ import annotations
import io, json, os, re, sqlite3, unicodedata
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from rapidfuzz import fuzz
BLOCKS=[("uid",2500),("email",1500),("phone",500),("zip_name",450),("zip_street",250),("city_name",200),("name_head",100)]

FIELDS=('id','company','first_name','last_name','street','house_number','postcode','city','country','email','phone','uid')
LABELS=dict(zip(FIELDS,('CRM-ID','Firma / Organisation','Vorname','Nachname','Strasse / Adresse','Hausnummer (separat)','PLZ','Ort','Land','E-Mail','Telefon','UID / MWST')))
ALIASES={
 'id':('ID','CRM_ID','Kundennummer','AccountID'),
 'company':('Firma','Firmenname','Company','Organisation','Name1'),
 'first_name':('Vorname','FirstName','GivenName'),
 'last_name':('Nachname','LastName','Surname'),
 'street':('Strasse','Straße','Street','Adresse','Address','Adresszeile1'),
 'house_number':('Hausnummer','Hausnr','House_Number','Housenumber'),
 'postcode':('PLZ','Postleitzahl','Postcode','Zip'),
 'city':('Ort','Stadt','City','Town'),
 'country':('Land','Country'),
 'email':('Email','E-Mail','Mail'),
 'phone':('Telefon','Phone','Tel','Mobil'),
 'uid':('UID','MWST','VAT','VatNumber'),
}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def plain(v):
 if v is None:return ''
 try:
  if pd.isna(v):return ''
 except (ValueError,TypeError):pass
 return str(v).strip()
def norm(v):
 s=plain(v).lower().replace('ä','ae').replace('ö','oe').replace('ü','ue').replace('ß','ss')
 s=unicodedata.normalize('NFKD',s)
 return re.sub(r'\s+',' ',re.sub('[^a-z0-9]+',' ',''.join(c for c in s if not unicodedata.combining(c)))).strip()
def norm_name(v):
 s=norm(v)
 return re.sub(r'\b(ag|gmbh|sa|sarl|ltd|inc|llc|kg)\b$','',s).strip()
def norm_street(v):
 s=norm(v)
 s=re.sub(r'\b([a-z]+)str\b',r'\1strasse',s)
 return re.sub(r'\b(str|street|stras|strass)\b','strasse',s)
def norm_house_number(v):
 s=plain(v).lower().replace('–','-').replace('—','-')
 s=re.sub(r'\s*([/-])\s*',r'\1',s)
 s=re.sub(r'(?<=\d)\s+(?=[a-z]$)','',s)
 return s if re.fullmatch(r'\d{1,5}[a-z]?(?:[/-]\d{1,5}[a-z]?)?',s) else ''
def split_street_house(street, explicit_number=''):
 raw=plain(street).replace('–','-').replace('—','-')
 explicit=norm_house_number(explicit_number)
 if not raw:return '',explicit,bool(plain(explicit_number) and not explicit)
 m=re.fullmatch(r'(.+?)[,\s]+(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)',raw)
 r=re.fullmatch(r'(\d{1,5}\s?[a-zA-Z]?)[,\s]+(.+)',raw)
 pre,num='',''
 if m:
  pre,num=m.group(1),norm_house_number(m.group(2))
  if norm(pre) in ('route','highway','rue','strasse','street'):pre,num='',''
 elif r: pre,num=r.group(2),norm_house_number(r.group(1))
 if explicit:return norm_street(pre if num else raw),explicit,bool(num and num!=explicit)
 return (norm_street(pre),num,False) if num else (norm_street(raw),'',False)
def norm_phone(v):
 s=re.sub(r'\D','','' if v is None else str(v))
 if s.startswith('0041'):s='41'+s[4:]
 elif len(s)==10 and s.startswith('0'):s='41'+s[1:]
 elif len(s)==9:s='41'+s
 return s if len(s)>=8 else ''
def norm_uid(v):return re.sub('[^A-Z0-9]','',plain(v).upper())
def best_guess(columns,field):
 indexed={norm(c).replace(' ',''):c for c in columns}
 return next((indexed[norm(alias).replace(' ','')] for alias in ALIASES[field] if norm(alias).replace(' ','') in indexed),'')
def read_upload(upload):
 raw=upload.getvalue()
 if len(raw)>60*1024**2:raise ValueError('Einzeldatei zu gross (>60 MB).')
 if upload.name.lower().endswith('.xlsx'):
  frame=pd.read_excel(io.BytesIO(raw),dtype=str,engine='openpyxl',keep_default_na=False)
 elif upload.name.lower().endswith('.csv'):
  for enc in ('utf-8-sig','cp1252','latin-1'):
   try:
    frame=pd.read_csv(io.BytesIO(raw),sep=None,engine='python',dtype=str,keep_default_na=False,encoding=enc,on_bad_lines='error');break
   except (UnicodeError,pd.errors.ParserError):continue
  else:raise ValueError('CSV nicht lesbar.')
 else:raise ValueError('Bitte CSV oder XLSX laden.')
 if frame.empty or len(frame)>150000:raise ValueError('Datei leer oder mehr als 150’000 Zeilen.')
 frame.columns=[plain(c) for c in frame.columns]
 if not all(frame.columns) or len(set(frame.columns))!=len(frame.columns):raise ValueError('Spaltennamen müssen eindeutig sein.')
 return frame.fillna('').astype(str)
def to_record(raw):
 sn,house,conflict=split_street_house(raw['street'],raw.get('house_number',''))
 name=' '.join(x for x in (norm_name(raw['company']),norm_name(' '.join([raw['first_name'],raw['last_name']]))) if x)
 postal=re.sub(r'\W','',plain(raw['postcode']).upper())
 if postal.startswith('CH') and len(postal)==6:postal=postal[2:]
 city=norm(raw['city']);street=norm_street(raw['street']);email=plain(raw['email']).lower();phone=norm_phone(raw['phone']);uid=norm_uid(raw['uid'])
 return dict(name=name,street=street,street_name=sn,house_number=house,address_conflict=int(conflict),postal=postal,city=city,email=email,phone=phone,uid=uid,zip_name=(postal+'|'+name[:6]) if postal and len(name)>=4 else '',zip_street=(postal+'|'+sn[:7]) if postal and len(sn)>=5 else '',city_name=(city[:8]+'|'+name[:8]) if city and len(name)>=5 else '',name_head=name[:10] if len(name)>=7 else '',street_number=house)

def connect(path):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 db=sqlite3.connect(path,timeout=60);db.row_factory=sqlite3.Row
 db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=60000')
 db.executescript('''
 CREATE TABLE IF NOT EXISTS projects(project_id INTEGER PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,
 a_count INTEGER NOT NULL,b_count INTEGER NOT NULL,candidate_count INTEGER DEFAULT 0,engine TEXT DEFAULT 'rapidfuzz',
 match_model_info TEXT DEFAULT '',mode TEXT NOT NULL DEFAULT 'legacy',ref_column TEXT NOT NULL DEFAULT '');
 CREATE TABLE IF NOT EXISTS records(pk INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
 side TEXT NOT NULL CHECK(side IN('A','B')),source_id TEXT NOT NULL,original_json TEXT NOT NULL,source_json TEXT NOT NULL,
 name TEXT NOT NULL,street TEXT NOT NULL,postal TEXT NOT NULL,city TEXT NOT NULL,email TEXT NOT NULL,phone TEXT NOT NULL,
 uid TEXT NOT NULL,zip_name TEXT NOT NULL,zip_street TEXT NOT NULL,city_name TEXT NOT NULL,name_head TEXT NOT NULL,
 street_number TEXT NOT NULL,street_name TEXT NOT NULL DEFAULT '',house_number TEXT NOT NULL DEFAULT '',
 address_conflict INTEGER NOT NULL DEFAULT 0,b_reference TEXT NOT NULL DEFAULT '',in_scope INTEGER NOT NULL DEFAULT 1,
 UNIQUE(project_id,side,source_id));
 CREATE TABLE IF NOT EXISTS pairs(project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
 a_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,b_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
 score INTEGER NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'offen',llm_decision TEXT NOT NULL DEFAULT '',
 llm_reason TEXT NOT NULL DEFAULT '',llm_model TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,match_probability REAL,
 PRIMARY KEY(project_id,a_pk,b_pk));
 CREATE TABLE IF NOT EXISTS trained_models(model_id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
 created_at TEXT NOT NULL,config_json TEXT NOT NULL,model_path TEXT NOT NULL,statistics_json TEXT NOT NULL DEFAULT '{}',
 training_type TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_record_side ON records(project_id,side);
 CREATE INDEX IF NOT EXISTS idx_pair_score ON pairs(project_id,score);
 CREATE INDEX IF NOT EXISTS idx_pair_state ON pairs(project_id,state);
 ''')
 # Compatible migration from older installed schema.
 for table, cols in {'projects':{'mode':"TEXT NOT NULL DEFAULT 'legacy'",'ref_column':"TEXT NOT NULL DEFAULT ''",'engine':"TEXT NOT NULL DEFAULT 'rapidfuzz'",'match_model_info':"TEXT NOT NULL DEFAULT ''"},'records':{'street_name':"TEXT NOT NULL DEFAULT ''",'house_number':"TEXT NOT NULL DEFAULT ''",'address_conflict':'INTEGER NOT NULL DEFAULT 0','b_reference':"TEXT NOT NULL DEFAULT ''",'in_scope':'INTEGER NOT NULL DEFAULT 1'},'pairs':{'match_probability':'REAL'}}.items():
  known={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
  for key,ddl in cols.items():
   if key not in known:db.execute(f'ALTER TABLE {table} ADD COLUMN {key} {ddl}')
 from migration import ensure_schema as migrate
 migrate(db);db.commit();return db

def import_project(db,title,fa,ma,fb,mb,engine='rapidfuzz',mode='dual',a_ref_column='',a_exclude_column='',exclude_values=(),empty_reference_markers=()):
 if engine not in ('rapidfuzz','splink5'):raise ValueError('Unbekannte Engine.')
 if mode not in ('dual','legacy','internal'):raise ValueError('Unbekannter Modus.')
 if mode=='dual' and a_ref_column not in fa.columns:raise ValueError('B-Referenzspalte aus A fehlt.')
 for frame,m,side in (((fa,ma,'A'),) if mode=='internal' else ((fa,ma,'A'),(fb,mb,'B'))):
  if not m.get('id') or m['id'] not in frame.columns:raise ValueError(f'CRM {side}: ID-Spalte erforderlich.')
  ids=frame[m['id']].map(plain)
  if ids.eq('').any() or ids.duplicated().any():raise ValueError(f'CRM {side}: Leere oder doppelte IDs.')
  if not any(m.get(k) for k in ('company','last_name','email','uid')):raise ValueError(f'CRM {side}: Name, Firma, E-Mail oder UID zuordnen.')
 exclusions={plain(x).casefold() for x in exclude_values};missing={plain(x).casefold() for x in empty_reference_markers}
 cols='project_id side source_id original_json source_json name street postal city email phone uid zip_name zip_street city_name name_head street_number street_name house_number address_conflict b_reference in_scope'.split()
 with db:
  pid=db.execute('INSERT INTO projects(title,created_at,a_count,b_count,engine,mode,ref_column) VALUES(?,?,?,?,?,?,?)',
    (plain(title)[:140] or 'CRM-Prüfung',now(),len(fa),0 if mode=='internal' else len(fb),engine,mode,a_ref_column)).lastrowid
  for side,frame,m in ((('A',fa,ma),) if mode=='internal' else (('A',fa,ma),('B',fb,mb))):
   batch=[]
   for row in frame.itertuples(index=False,name=None):
    original=dict(zip(frame.columns,[plain(x) for x in row]))
    raw={field:original.get(m.get(field), '') for field in FIELDS}
    record=to_record(raw)
    bref=original.get(a_ref_column,'') if side=='A' and mode=='dual' else ''
    if bref.casefold() in missing:bref=''
    inscope=not (side=='A' and a_exclude_column and original.get(a_exclude_column,'').casefold() in exclusions)
    batch.append((pid,side,raw['id'],json.dumps(raw,ensure_ascii=False),json.dumps(original,ensure_ascii=False),
     *(record[k] for k in ('name','street','postal','city','email','phone','uid','zip_name','zip_street','city_name','name_head','street_number','street_name','house_number','address_conflict')),bref,int(inscope)))
    if len(batch)==1000:
     db.executemany(f"INSERT INTO records({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",batch);batch=[]
   if batch:db.executemany(f"INSERT INTO records({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",batch)
 return pid

def score_pair(a,b):
 weighted=[];explain=[]
 for k,w in (('name',.48),('street',.26),('postal',.16),('city',.10)):
  if a[k] and b[k]:
   value=int(a[k]==b[k])*100 if k=='postal' else fuzz.WRatio(a[k],b[k]);weighted.append((w,value/100))
   if value>=90:explain.append(k+' ähnlich')
 if not weighted:return 0,'Unzureichende Daten'
 score=100*sum(w*v for w,v in weighted)/sum(w for w,_ in weighted)
 if a['uid'] and b['uid']:
  if a['uid']==b['uid']:score=max(score,96);explain.append('UID identisch')
  else:score-=45;explain.append('UID-KONFLIKT')
 if a['email'] and a['email']==b['email']:score=max(score,88);explain.append('E-Mail identisch')
 if a['phone'] and a['phone']==b['phone']:score+=7
 if a['street_number'] and b['street_number'] and a['street_number']!=b['street_number']:
  score-=17;explain.append('Hausnummer unterschiedlich')
 return max(0,min(100,round(score))),', '.join(explain)

def run_matching(db,pid,minimum=60,max_candidates=400000,progress=None):
 from internal_dupes import is_internal, run_internal_matching
 if is_internal(db,pid):return run_internal_matching(db,pid,minimum,max_candidates,progress)
 progress=progress or (lambda x:None)
 mode=db.execute('SELECT mode FROM projects WHERE project_id=?',(pid,)).fetchone()['mode']
 with db:
  # This does not collect all possible A x B pairs; seven indexed blocking keys.
  db.execute('DELETE FROM pairs WHERE project_id=?',(pid,))
  db.execute('CREATE TEMP TABLE IF NOT EXISTS candidates(a_pk INTEGER,b_pk INTEGER,PRIMARY KEY(a_pk,b_pk)) WITHOUT ROWID')
  db.execute('DELETE FROM candidates')
  for field,fanout in (('uid',2500),('email',1500),('phone',500),('zip_name',450),('zip_street',250),('city_name',200),('name_head',100)):
   sides={}
   for side in ('A','B'):
    wh=" AND b_reference='' AND in_scope=1" if side=='A' and mode=='dual' else ''
    m={}
    for rec in db.execute(f"SELECT pk,{field} FROM records WHERE project_id=? AND side=? AND {field}!=''"+wh,(pid,side)):
     m.setdefault(rec[field],[]).append(rec['pk'])
    sides[side]=m
   buf=[]
   for key,aa in sides['A'].items():
    bb=sides['B'].get(key,[])
    if not bb or len(aa)*len(bb)>fanout:continue
    buf.extend((a,b) for a in aa for b in bb)
    if len(buf)>3000:
     db.executemany('INSERT OR IGNORE INTO candidates VALUES(?,?)',buf);buf=[]
   if buf:db.executemany('INSERT OR IGNORE INTO candidates VALUES(?,?)',buf)
   qty=db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
   if qty>max_candidates:raise ValueError('Zu viele Kandidaten. Blocking eingrenzen.')
   progress(f'Blocking {field}: {qty:,} Kandidaten')
  qty=db.execute('SELECT count(*) FROM candidates').fetchone()[0]
  cursor=db.execute('''SELECT a.*,b.pk AS target_pk,b.name b_name,b.street b_street,b.postal b_postal,
       b.city b_city,b.email b_email,b.phone b_phone,b.uid b_uid,b.street_number b_street_number
       FROM candidates c JOIN records a ON a.pk=c.a_pk JOIN records b ON b.pk=c.b_pk''')
  buf=[];kept=0
  for r in cursor:
   a=dict(r);b={k:r['b_'+k] for k in ('name','street','postal','city','email','phone','uid','street_number')}
   score,reason=score_pair(a,b)
   if score>=minimum:buf.append((pid,r['pk'],r['target_pk'],score,reason,now()))
   if len(buf)>=1000:
    db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at) VALUES(?,?,?,?,?,?)',buf);kept+=len(buf);buf=[]
  if buf:db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at) VALUES(?,?,?,?,?,?)',buf);kept+=len(buf)
  db.execute('UPDATE projects SET candidate_count=? WHERE project_id=?',(kept,pid))
  db.execute('DROP TABLE candidates')
 return qty,kept

def list_projects(db):return db.execute('SELECT * FROM projects ORDER BY project_id DESC').fetchall()
def project_stats(db,pid):
 r=db.execute("""SELECT p.*,(SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='bestaetigt') accepted,
 (SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='offen') pending,
 (SELECT count(*) FROM pairs WHERE project_id=p.project_id AND state='abgelehnt') rejected FROM projects p WHERE project_id=?""",(pid,)).fetchone()
 return dict(r) if r else {}
def set_review(db,pid,a_pk,b_pk,state):
 if state not in ('offen','bestaetigt','abgelehnt','unklar'):raise ValueError('Ungültiger Prüfstatus.')
 with db:
  if state=='bestaetigt':
   from internal_dupes import is_internal
   if is_internal(db,pid):
    db.execute('UPDATE pairs SET state=?,updated_at=? WHERE project_id=? AND a_pk=? AND b_pk=?',(state,now(),pid,a_pk,b_pk))
    return
   from migration import is_dual,reference_conflict
   if is_dual(db,pid) and reference_conflict(db,pid,a_pk,b_pk):raise ValueError('B-ID ist bereits bei anderer A-Adresse referenziert.')
   conflict=db.execute("SELECT 1 FROM pairs WHERE project_id=? AND state='bestaetigt' AND (a_pk=? OR b_pk=?) AND NOT(a_pk=? AND b_pk=?)",(pid,a_pk,b_pk,a_pk,b_pk)).fetchone()
   if conflict:raise ValueError('Konflikt: CRM-ID bereits einem anderen Datensatz zugeordnet.')
  db.execute('UPDATE pairs SET state=?,updated_at=? WHERE project_id=? AND a_pk=? AND b_pk=?',(state,now(),pid,a_pk,b_pk))
def save_llm(db,pid,a_pk,b_pk,decision,reason,model):
 with db:db.execute('UPDATE pairs SET llm_decision=?,llm_reason=?,llm_model=?,updated_at=? WHERE project_id=? AND a_pk=? AND b_pk=?',(decision,plain(reason)[:1800],model,now(),pid,a_pk,b_pk))
def page_pairs(db,pid,state='Alle',offset=0,limit=40,min_score=0):
 wh=" AND p.state=?" if state!='Alle' else '';params=[pid,min_score]+([state] if wh else [])
 fr='FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk WHERE p.project_id=? AND p.score>=? '+wh
 total=db.execute('SELECT count(*) '+fr,params).fetchone()[0]
 rows=db.execute('SELECT p.*,a.source_id a_id,b.source_id b_id,a.original_json a_json,b.original_json b_json '+fr+' ORDER BY p.score DESC,p.a_pk,p.b_pk LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
 return total,rows
def _safe(value):
 s=plain(value)
 return "'"+s if s.lstrip()[:1] in ('=','+','-','@') else s
def export_pairs(db,pid,accepted_only=False):
 sql='''SELECT a.source_id CRM_A_ID,b.source_id CRM_B_ID,p.score score_0_100,
 p.match_probability Splink_Wahrscheinlichkeit,p.state Pruefentscheid,p.reason Gruende,p.llm_decision KI_Vorschlag
 FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk WHERE p.project_id=?'''
 if accepted_only:sql+=" AND p.state='bestaetigt'"
 d=pd.read_sql_query(sql,db,params=(pid,));return d.map(_safe).to_csv(index=False,sep=';').encode('utf-8-sig')
def export_unmatched(db,pid,side):
 if side not in ('A','B'):raise ValueError('Ungültige Quelle.')
 key='a_pk' if side=='A' else 'b_pk'
 rows=db.execute(f'''SELECT source_id,source_json FROM records r WHERE project_id=? AND side=?
 AND NOT EXISTS(SELECT 1 FROM pairs p WHERE p.project_id=? AND p.state='bestaetigt' AND p.{key}=r.pk)''',(pid,side,pid)).fetchall()
 out=[{'ID':r['source_id'],**json.loads(r['source_json'])} for r in rows]
 return pd.DataFrame(out).map(_safe).to_csv(index=False,sep=';').encode('utf-8-sig')
def delete_project(db,pid):
 with db:db.execute('DELETE FROM projects WHERE project_id=?',(pid,))
def comparison_diagnostics(a,b):
 return [{'Feld':k,'A':a[k],'B':b[k],'gleich':a[k]==b[k]} for k in ('name','street_name','house_number','postal','city','uid','email','phone')]
