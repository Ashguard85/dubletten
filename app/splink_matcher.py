"""Actual Splink 5 / DuckDB offline linkage with optional saved model reuse.

Not a RapidFuzz fallback: unsupported Splink versions and training failures raise.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
from uuid import uuid4
from importlib.metadata import version
import json,math,random
import pandas as pd
from engine import now,score_pair
from match_config import BLOCK_LIMITS,DEFAULTS,validate_config,model_signature
COLUMNS=('unique_id','name','street','postal','city','uid','email','phone',
         'zip_name','zip_street','city_name','name_head','street_name','house_number')

def _load(db,pid):
 result={}
 for side in ('A','B'):
  from migration import is_dual
  wh=" AND b_reference='' AND in_scope=1" if side=='A' and is_dual(db,pid) else ''
  cols=[x for x in COLUMNS if x!='unique_id']
  data=[]
  for row in db.execute('SELECT pk,'+','.join(cols)+' FROM records WHERE project_id=? AND side=?'+wh,(pid,side)):
   d={k:(row[k] if row[k]!='' else None) for k in cols};d['unique_id']=row['pk'];data.append(d)
  result[side]=pd.DataFrame(data,columns=COLUMNS)
 return result

def _blocks(frames,cfg,max_candidates):
 ceiling=0;stats=[]
 for field in cfg['blocking_fields']:
  a,b=frames['A'][field],frames['B'][field]
  ca=Counter(v for v in a if v is not None);cb=Counter(v for v in b if v is not None)
  ignored={k for k in ca.keys()&cb.keys() if ca[k]*cb[k]>BLOCK_LIMITS[field]}
  for side in ('A','B'):
   frames[side]['block_'+field]=frames[side][field].where(~frames[side][field].isin(ignored),other=None)
  n=sum(ca[k]*cb[k] for k in ca.keys()&cb.keys() if k not in ignored);ceiling+=n
  stats.append({'field':field,'pairs':n,'ignored_keys':len(ignored)})
  if ceiling>max_candidates:raise ValueError('Splink-Blocking erzeugt zu viele Kandidaten. Regeln einschränken.')
 return ceiling,stats

def _linker(frames,cfg,baseline=True,prior=.001,model_path=None,dedupe=False):
 import splink.comparison_library as cl
 from splink import DuckDBAPI,Linker,SettingsCreator,block_on
 api=DuckDBAPI()
 sources=[api.register(frames[k],dataset_display_name=f'CRM_{k}') for k in (('A',) if dedupe else ('A','B'))]
 if model_path is not None:return Linker(sources[0] if dedupe else sources,settings=str(model_path))
 def item(comp,m,u):return comp.configure(m_probabilities=m,u_probabilities=u) if baseline else comp
 cmp=[
  item(cl.JaroWinklerAtThresholds('name',cfg['name_thresholds']),[.72,.17,.08,.03],[.0001,.0006,.004,.9953]),
  item(cl.LevenshteinAtThresholds('street_name' if cfg['split_house_number'] else 'street',cfg['street_distances']),[.75,.10,.10,.05],[.0003,.001,.005,.9937]),
  item(cl.ExactMatch('postal'),[.88,.12],[.012,.988]),
  item(cl.JaroWinklerAtThresholds('city',[.95,.85]),[.85,.09,.04,.02],[.03,.01,.04,.92]),
  item(cl.ExactMatch('uid'),[.94,.06],[.0001,.9999]),
  item(cl.ExactMatch('email'),[.88,.12],[.0004,.9996]),
  item(cl.ExactMatch('phone'),[.84,.16],[.001,.999])]
 if cfg['split_house_number']:
  cmp.insert(2,item(cl.ExactMatch('house_number'),[.91,.09],[.02,.98]))
 settings=SettingsCreator(link_type='dedupe_only' if dedupe else 'link_only',unique_id_column_name='unique_id',
  probability_two_random_records_match=prior,comparisons=cmp,
  blocking_rules_to_generate_predictions=[block_on('block_'+x) for x in cfg['blocking_fields']],
  retain_matching_columns=False,retain_intermediate_calculation_columns=False)
 return Linker(sources[0] if dedupe else sources,settings)

def _learn(linker,n_a,n_b,cfg,progress):
 from splink import block_on
 progress('Splink u-Parameter schätzen')
 linker.training.estimate_u_using_random_sampling(max_pairs=min(cfg['u_max_pairs'],n_a*n_b),seed=42,min_count_per_level=15)
 for field in ('postal','name_head'):
  progress(f'Splink EM-Schätzung mit {field}')
  linker.training.estimate_parameters_using_expectation_maximisation(block_on(field),max_pairs=cfg['em_max_pairs'])
def _folder(db):
 base=Path(db.execute('PRAGMA database_list').fetchone()['file']).resolve().parent/'models'
 base.mkdir(exist_ok=True,parents=True,mode=0o700)
 return base
def _save(db,linker,pid,cfg,kind,stats):
 folder=_folder(db);path=folder/f'projekt_{pid}_{uuid4().hex[:12]}.json'
 tmp=folder/f'.{path.name}.tmp'
 try:
  with tmp.open('x',encoding='utf8') as f:json.dump(linker.misc.save_model_to_json(),f,ensure_ascii=False,indent=2)
  tmp.chmod(0o600);tmp.replace(path)
  db.execute('''INSERT INTO trained_models(project_id,created_at,config_json,model_path,statistics_json,training_type)
   VALUES(?,?,?,?,?,?)''',(pid,now(),json.dumps(cfg),str(path),json.dumps(stats),kind))
  return db.execute('SELECT last_insert_rowid()').fetchone()[0]
 except Exception:
  tmp.unlink(missing_ok=True);path.unlink(missing_ok=True);raise
def available_models(db):
 return [dict(r) for r in db.execute('''SELECT m.*,p.title FROM trained_models m
 JOIN projects p ON p.project_id=m.project_id ORDER BY m.model_id DESC''')]
def _resolve(db,model_id):
 row=db.execute('SELECT * FROM trained_models WHERE model_id=?',(int(model_id),)).fetchone()
 if row is None:raise ValueError('Gespeichertes Modell fehlt.')
 p=Path(row['model_path']).resolve()
 if not p.is_relative_to(_folder(db).resolve()) or not p.is_file():raise ValueError('Ungültiger lokaler Modellpfad.')
 return p,validate_config(json.loads(row['config_json']))

def run_splink_matching(db,project_id,minimum_probability=.05,max_candidates=400000,progress=None,config=None,model_id=None):
 from internal_dupes import is_internal
 if is_internal(db,project_id):
  return run_splink_internal(db,project_id,minimum_probability,max_candidates,progress,config,model_id)
 cfg=validate_config(config);progress=progress or (lambda _:None)
 try:
  if not version('splink').startswith('5.'):raise RuntimeError('Splink 5 erforderlich.')
  import splink
 except ImportError as e:raise RuntimeError('Splink 5 fehlt: Docker-Image neu bauen.') from e
 if model_id is not None:
  model_path,saved_cfg=_resolve(db,model_id)
  if model_signature(cfg)!=model_signature(saved_cfg):raise ValueError('Modellparameter unterscheiden sich.')
 else:model_path=None
 frames=_load(db,project_id);na,nb=len(frames['A']),len(frames['B'])
 if not na or not nb:
  with db:db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
  return 0,0
 upper,stats=_blocks(frames,cfg,max_candidates)
 if not upper:return 0,0
 prior=max(1e-7,min(.1,.5/max(na,nb)))
 tiny=na*nb<250000 or min(na,nb)<500
 linker=_linker(frames,cfg,baseline=tiny,prior=prior,model_path=model_path)
 if not tiny and model_path is None:_learn(linker,na,nb,cfg,progress)
 predictions=linker.inference.predict(threshold_match_probability=minimum_probability)
 rel=predictions.as_duckdbpyrelation().project('unique_id_l,unique_id_r,source_dataset_l,source_dataset_r,match_probability')
 a={r['pk']:r for r in db.execute("SELECT * FROM records WHERE project_id=? AND side='A'",(project_id,))}
 b={r['pk']:r for r in db.execute("SELECT * FROM records WHERE project_id=? AND side='B'",(project_id,))}
 kept=0
 with db:
  db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,));batch=[]
  for l,r,ls,rs,prob in rel.iter_rows(batch_size=2000):
   if ls=='CRM_A' and rs=='CRM_B':ap,bp=int(l),int(r)
   elif ls=='CRM_B' and rs=='CRM_A':ap,bp=int(r),int(l)
   else:raise RuntimeError('Unerwartete Splink-Quellkennung.')
   prob=float(prob)
   if not math.isfinite(prob):raise RuntimeError('Ungültige Matchwahrscheinlichkeit.')
   heuristic,reason=score_pair(a[ap],b[bp])
   batch.append((project_id,ap,bp,round(prob*100),f'Splink 5 · RapidFuzz {heuristic}/100 · {reason}',now(),prob))
   if len(batch)==2000:
    db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,match_probability) VALUES(?,?,?,?,?,?,?)',batch);kept+=len(batch);batch=[]
  if batch:
   db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,match_probability) VALUES(?,?,?,?,?,?,?)',batch);kept+=len(batch)
  model_saved=_save(db,linker,project_id,cfg,'feste_priors_kleine_stichprobe' if tiny else 'em_training',{'blocking':stats}) if model_path is None else model_id
  db.execute('UPDATE projects SET candidate_count=?,match_model_info=? WHERE project_id=?',
   (kept,json.dumps({'engine':'splink5','model_id':model_saved,'blocking':stats}),project_id))
 return upper,kept

def train_model_from_reviews(db,project_id,config=None,progress=None):
 cfg=validate_config(config);progress=progress or (lambda _:None)
 labels=list(db.execute("SELECT a_pk,b_pk,state FROM pairs WHERE project_id=? AND state IN ('bestaetigt','abgelehnt')",(project_id,)))
 positive=[(r['a_pk'],r['b_pk']) for r in labels if r['state']=='bestaetigt']
 negative=[(r['a_pk'],r['b_pk']) for r in labels if r['state']=='abgelehnt']
 if len(positive)<20:raise ValueError('Mindestens 20 manuell bestätigte Paare nötig.')
 random.Random(42).shuffle(positive)
 holdout=max(4,round(len(positive)*.2));train=positive[holdout:];reserved=positive[:holdout]
 frames=_load(db,project_id);_blocks(frames,cfg,400000)
 linker=_linker(frames,cfg,baseline=False,prior=max(1e-7,min(.1,.5/max(len(frames['A']),len(frames['B'])))))
 linker.training.estimate_u_using_random_sampling(max_pairs=min(cfg['u_max_pairs'],len(frames['A'])*len(frames['B'])),seed=42,min_count_per_level=15)
 df=pd.DataFrame([{'source_dataset_l':'CRM_A','unique_id_l':a,'source_dataset_r':'CRM_B','unique_id_r':b} for a,b in train])
 linker.table_management.register_table(df,'crm_training_labels',overwrite=True)
 linker.training.estimate_m_from_pairwise_labels('crm_training_labels')
 result={'train_positive':len(train),'holdout_positive':len(reserved),'negative_reviewed':len(negative),
         'caution':'Keine repräsentative Schätzung von Precision/Recall ohne unabhängige Prüfdaten.'}
 with db:mid=_save(db,linker,project_id,cfg,'manuelle_labels',result)
 return mid,result


def run_splink_internal(db,project_id,minimum_probability=.05,max_candidates=400000,progress=None,config=None,model_id=None):
    """Actual Splink-5 dedupe_only, not self-linkage or a RapidFuzz fallback."""
    from internal_dupes import _check
    _check(db,project_id)
    if model_id is not None:
        raise ValueError('Splink-Modelle aus dem A↔B-Modus nicht für interne Prüfungen übernehmen. Neues internes Modell erstellen.')
    cfg=validate_config(config)
    progress=progress or (lambda _:None)
    if not .001 <= minimum_probability <= 1:
        raise ValueError('Ungültiger Wahrscheinlichkeitsschwellwert.')
    if not version('splink').startswith('5.'):
        raise RuntimeError('Splink 5 erforderlich.')
    if db.execute("SELECT 1 FROM pairs WHERE project_id=? AND state!='offen' LIMIT 1",(project_id,)).fetchone():
        raise ValueError('Manuelle Prüfentscheide vorhanden. Bitte ein neues Projekt für einen neuen Lauf anlegen.')
    cols=[x for x in COLUMNS if x!='unique_id']
    data=[]
    for r in db.execute("SELECT pk,"+','.join(cols)+" FROM records WHERE project_id=? AND side='A' AND in_scope=1",(project_id,)):
        d={k:r[k] if r[k] else None for k in cols}
        d['unique_id']=r['pk']
        data.append(d)
    n=len(data)
    if n<2:
        with db:
            db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
            db.execute('UPDATE projects SET candidate_count=0 WHERE project_id=?',(project_id,))
        return 0,0
    df=pd.DataFrame(data,columns=COLUMNS)
    total_upper=0
    block_stats=[]
    for field in cfg['blocking_fields']:
        counts=Counter(v for v in df[field] if v is not None)
        ignored={k for k,c in counts.items() if c*(c-1)//2>BLOCK_LIMITS[field]}
        df['block_'+field]=df[field].where(~df[field].isin(ignored),other=None)
        upper=sum(c*(c-1)//2 for k,c in counts.items() if k not in ignored)
        total_upper+=upper
        block_stats.append({'field':field,'pairs':upper,'ignored_keys':len(ignored)})
        if total_upper>max_candidates:
            raise ValueError('Splink-Blocking erzeugt zu viele interne Kandidaten. Regeln eingrenzen.')
    if not total_upper:
        with db:
            db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
            db.execute('UPDATE projects SET candidate_count=0 WHERE project_id=?',(project_id,))
        return 0,0
    tiny=n*(n-1)//2<250000 or n<500
    progress('Splink 5: interne Kandidaten prüfen')
    linker=_linker({'A':df},cfg,baseline=tiny,prior=max(1e-7,min(.1,.5/n)),dedupe=True)
    if not tiny:
        _learn(linker,n,n,cfg,progress)
    predictions=linker.inference.predict(threshold_match_probability=minimum_probability)
    rel=predictions.as_duckdbpyrelation().project('unique_id_l,unique_id_r,match_probability')
    stored={r['pk']:r for r in db.execute("SELECT * FROM records WHERE project_id=? AND side='A'",(project_id,))}
    kept=0
    with db:
        db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
        batch=[]
        for l,r,prob in rel.iter_rows(batch_size=2000):
            a,b=sorted((int(l),int(r)))
            if a==b:
                raise RuntimeError('Splink hat einen Datensatz mit sich selbst verglichen.')
            prob=float(prob)
            if not math.isfinite(prob):
                raise RuntimeError('Ungültige Matchwahrscheinlichkeit.')
            heuristic,reason=score_pair(stored[a],stored[b])
            batch.append((project_id,a,b,round(prob*100),f'Splink 5 (intern) · RapidFuzz {heuristic}/100 · {reason}',now(),prob))
            if len(batch)>=2000:
                db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,match_probability) VALUES(?,?,?,?,?,?,?)',batch)
                kept+=len(batch);batch=[]
        if batch:
            db.executemany('INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,match_probability) VALUES(?,?,?,?,?,?,?)',batch)
            kept+=len(batch)
        mid=_save(db,linker,project_id,cfg,'interne_dedupe_priors' if tiny else 'interne_dedupe_em',{'blocking':block_stats})
        db.execute('UPDATE projects SET candidate_count=?,match_model_info=? WHERE project_id=?',
                   (kept,json.dumps({'engine':'splink5','mode':'internal','model_id':mid,'blocking':block_stats}),project_id))
    return total_upper,kept
