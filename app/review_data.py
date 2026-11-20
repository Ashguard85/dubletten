"""SQL-paginated CRM pair review for focus and classic views."""
RISKS=('Alle','Hausnummer unterschiedlich','UID verschieden','Mehrfachkandidat')
def pair_page(db,pid,state='offen',min_score=0,search='',risk='Alle',page=1,page_size=25):
 if risk not in RISKS:raise ValueError('Unbekannter Risikofilter.')
 where=['p.project_id=?','p.score>=?'];params=[pid,max(0,min(100,int(min_score)))]
 if state!='Alle':where.append('p.state=?');params.append(state)
 if search.strip():
  q='%'+search[:120].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
  where.append("(a.source_id LIKE ? ESCAPE '\\' OR b.source_id LIKE ? ESCAPE '\\' OR a.original_json LIKE ? ESCAPE '\\' OR b.original_json LIKE ? ESCAPE '\\')");params.extend([q]*4)
 if risk=='Hausnummer unterschiedlich':
  where.append("a.house_number!='' AND b.house_number!='' AND a.house_number!=b.house_number")
 elif risk=='UID verschieden':
  where.append("a.uid!='' AND b.uid!='' AND a.uid!=b.uid")
 elif risk=='Mehrfachkandidat':
  internal=db.execute('SELECT mode FROM projects WHERE project_id=?',(pid,)).fetchone()['mode']=='internal'
  if internal:
   where.append('''((SELECT COUNT(*) FROM pairs c WHERE c.project_id=p.project_id AND (c.a_pk=p.a_pk OR c.b_pk=p.a_pk))>1
     OR (SELECT COUNT(*) FROM pairs c WHERE c.project_id=p.project_id AND (c.a_pk=p.b_pk OR c.b_pk=p.b_pk))>1)''')
  else:
   where.append('''(SELECT COUNT(*) FROM pairs c WHERE c.project_id=p.project_id AND c.a_pk=p.a_pk)>1''')
 query='''FROM pairs p JOIN records a ON a.pk=p.a_pk JOIN records b ON b.pk=p.b_pk WHERE '''+' AND '.join(where)
 count=db.execute('SELECT count(*) '+query,params).fetchone()[0]
 size=max(1,min(50,int(page_size)));total_pages=max(1,(count+size-1)//size);page=max(1,min(int(page),total_pages))
 rows=db.execute('''SELECT p.*,a.source_id a_id,b.source_id b_id,a.original_json a_json,b.original_json b_json,
 a.house_number a_house,b.house_number b_house '''+query+' ORDER BY p.score DESC,p.a_pk,p.b_pk LIMIT ? OFFSET ?',params+[size,(page-1)*size]).fetchall()
 return count,total_pages,page,[dict(x) for x in rows]
