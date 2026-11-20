"""Two independent workflows for source CRM A and target CRM B."""
import io,json,csv
from collections import Counter
from engine import norm,norm_name,norm_phone,norm_uid,plain,now

FIELDS={'company':'Firma','first_name':'Vorname','last_name':'Nachname','street_name':'Strasse','house_number':'Hausnummer','postcode':'PLZ','city':'Ort','country':'Land','email':'E-Mail','phone':'Telefon','uid':'UID'}
def ensure_schema(db):
 db.executescript('''CREATE TABLE IF NOT EXISTS reference_checks(
 project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
 a_pk INTEGER NOT NULL REFERENCES records(pk) ON DELETE CASCADE,
 b_pk INTEGER REFERENCES records(pk) ON DELETE CASCADE,
 b_ref TEXT NOT NULL,result TEXT NOT NULL,differences_json TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(project_id,a_pk));
 CREATE INDEX IF NOT EXISTS idx_ref_status ON reference_checks(project_id,result);''')
 for table,cols in {'projects':{'mode':"TEXT NOT NULL DEFAULT 'legacy'",'ref_column':"TEXT NOT NULL DEFAULT ''"},'records':{'b_reference':"TEXT NOT NULL DEFAULT ''",'in_scope':'INTEGER NOT NULL DEFAULT 1'}}.items():
  existing={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
  for name,ddl in cols.items():
   if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
 db.commit()
def is_dual(db,pid):
 row=db.execute('SELECT mode FROM projects WHERE project_id=?',(pid,)).fetchone()
 return bool(row and row['mode']=='dual')
def missing_reference(value,markers=()):return not plain(value) or plain(value).casefold() in {plain(x).casefold() for x in markers}
def reference_conflict(db,pid,a_pk,b_pk):
 target=db.execute('SELECT source_id FROM records WHERE pk=? AND project_id=? AND side=\'B\'',(b_pk,pid)).fetchone()
 if not target:return True
 clash=db.execute("SELECT 1 FROM records WHERE project_id=? AND side='A' AND pk!=? AND b_reference=? AND in_scope=1 LIMIT 1",(pid,a_pk,target['source_id'])).fetchone()
 current=db.execute("SELECT b_reference FROM records WHERE project_id=? AND pk=? AND side='A'",(pid,a_pk)).fetchone()
 return bool(clash or (current and current['b_reference'] and current['b_reference']!=target['source_id']))
def _val(record,field):
 raw=json.loads(record['original_json'])
 if field=='street_name':return raw.get('street',''),record['street_name']
 if field=='house_number':return raw.get('house_number','') or record['house_number'],record['house_number']
 if field=='postcode':return raw.get('postcode',''),record['postal']
 if field=='city':return raw.get('city',''),record['city']
 if field=='country':return raw.get('country',''),norm(raw.get('country',''))
 if field=='company':return raw.get('company',''),norm_name(raw.get('company',''))
 if field=='phone':return raw.get('phone',''),norm_phone(raw.get('phone',''))
 if field=='uid':return raw.get('uid',''),norm_uid(raw.get('uid',''))
 if field=='email':return raw.get('email',''),raw.get('email','').lower()
 return raw.get(field,''),norm_name(raw.get(field,''))
def differences_between(a,b):
 result=[]
 for key,label in FIELDS.items():
  ar,an=_val(a,key);br,bn=_val(b,key)
  if not (ar or br or an or bn):continue
  state='identisch' if ar==br and ar else 'fehlt' if not an or not bn else 'normalisiert_gleich' if an==bn else 'abweichend'
  result.append(dict(feld=key,bezeichnung=label,a_wert=ar,b_wert=br,a_normalisiert=an,b_normalisiert=bn,status=state))
 return result
def run_reference_check(db,pid):
 if not is_dual(db,pid):raise ValueError('Nicht im A→B-Modus.')
 brows={r['source_id']:r for r in db.execute("SELECT * FROM records WHERE project_id=? AND side='B'",(pid,))}
 arows=list(db.execute("SELECT * FROM records WHERE project_id=? AND side='A' AND b_reference!='' ORDER BY pk",(pid,)))
 freq=Counter(r['b_reference'] for r in arows)
 with db:
  db.execute('DELETE FROM reference_checks WHERE project_id=?',(pid,))
  for a in arows:
   b=brows.get(a['b_reference']);diff=differences_between(a,b) if b else []
   result=('ausgeschlossen' if not a['in_scope'] else 'mehrfachreferenz' if freq[a['b_reference']]>1 else
      'referenz_fehlt' if not b else 'abweichend' if any(x['status'] in ('abweichend','fehlt') for x in diff)
      else 'normalisiert_gleich' if any(x['status']=='normalisiert_gleich' for x in diff) else 'identisch')
   db.execute('INSERT INTO reference_checks VALUES(?,?,?,?,?,?,?)',(pid,a['pk'],b['pk'] if b else None,a['b_reference'],result,json.dumps(diff,ensure_ascii=False),now()))
 return len(arows)
def reference_overview(db,pid):
 return [dict(r) for r in db.execute('SELECT result,count(*) anzahl FROM reference_checks WHERE project_id=? GROUP BY result',(pid,))]
def reference_page(db,pid,status='Alle',offset=0,limit=50):
 params=[pid]+([status] if status!='Alle' else [])
 wh=' AND r.result=?' if status!='Alle' else ''
 fr="FROM reference_checks r JOIN records a ON a.pk=r.a_pk LEFT JOIN records b ON b.pk=r.b_pk WHERE r.project_id=?"+wh
 total=db.execute('SELECT count(*) '+fr,params).fetchone()[0]
 rows=db.execute('SELECT r.*,a.source_id a_id,b.source_id b_id '+fr+' ORDER BY r.a_pk LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
 return total,rows
def _safe(val):
 val=plain(val)
 return "'"+val if val.lstrip()[:1] in ('=','+','-','@') else val
def export_references(db,pid):
 buffer=io.StringIO();w=csv.writer(buffer,delimiter=';',lineterminator='\n')
 w.writerow(['CRM_A_ID','B_Referenz_in_A','CRM_B_ID','Referenzstatus','Felddifferenzen'])
 for r in db.execute('''SELECT a.source_id a_id,r.b_ref,b.source_id b_id,r.result,r.differences_json
     FROM reference_checks r JOIN records a ON a.pk=r.a_pk LEFT JOIN records b ON b.pk=r.b_pk WHERE r.project_id=? ORDER BY a.source_id''',(pid,)):
  diffs=json.loads(r['differences_json']);w.writerow([_safe(r['a_id']),_safe(r['b_ref']),_safe(r['b_id']),r['result'],sum(x['status'] in ('abweichend','fehlt') for x in diffs)])
 return buffer.getvalue().encode('utf-8-sig')
def export_reference_field_differences(db,pid):
 buffer=io.StringIO();w=csv.writer(buffer,delimiter=';',lineterminator='\n')
 w.writerow(['CRM_A_ID','B_Referenz','Feld','Wert_A','Wert_B','Status'])
 for r in db.execute('''SELECT a.source_id a_id,c.b_ref,c.differences_json FROM reference_checks c
  JOIN records a ON a.pk=c.a_pk WHERE c.project_id=? ORDER BY c.a_pk''',(pid,)):
  for d in json.loads(r['differences_json']):
   if d['status'] in ('abweichend','fehlt'):w.writerow([_safe(r['a_id']),_safe(r['b_ref']),d['bezeichnung'],_safe(d['a_wert']),_safe(d['b_wert']),d['status']])
 return buffer.getvalue().encode('utf-8-sig')
def workflow1_inventory(db,pid):
 return [dict(r) for r in db.execute("""SELECT a.source_id AS A_ID,a.b_reference AS B_Referenz,
       CASE WHEN a.in_scope=0 THEN 'ausgeschlossen' WHEN a.b_reference!='' THEN 'referenziert'
       WHEN EXISTS(SELECT 1 FROM pairs p WHERE p.project_id=a.project_id AND p.a_pk=a.pk AND p.state='bestaetigt') THEN 'zugeordnet'
       WHEN EXISTS(SELECT 1 FROM pairs p WHERE p.project_id=a.project_id AND p.a_pk=a.pk) THEN 'kandidaten_offen'
       ELSE 'kein_kandidat' END AS Status
       FROM records a WHERE a.project_id=? AND a.side='A' ORDER BY a.source_id""",(pid,))]
def export_workflow1_inventory(db,pid):
 rows=workflow1_inventory(db,pid);buffer=io.StringIO();w=csv.DictWriter(buffer,fieldnames=['A_ID','B_Referenz','Status'],delimiter=';',lineterminator='\n');w.writeheader();w.writerows([{k:_safe(v) for k,v in r.items()} for r in rows]);return buffer.getvalue().encode('utf-8-sig')
