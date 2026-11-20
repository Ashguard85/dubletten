"""Allowlisted Splink parameters. No user-defined SQL in configuration."""
import hashlib,json
BLOCK_LIMITS={'uid':2500,'email':1500,'phone':500,'zip_name':450,'zip_street':250,'city_name':200,'name_head':100,'street_name':150}
DEFAULTS={'split_house_number':True,'name_thresholds':[.92,.84],'street_distances':[2,5],
 'u_max_pairs':300000,'em_max_pairs':250000,'min_probability':.05,'blocking_fields':list(BLOCK_LIMITS)}
def validate_config(config=None):
 c={k:v[:] if isinstance(v,list) else v for k,v in DEFAULTS.items()}
 if config:
  if set(config)-set(c):raise ValueError('Unbekannte Splink-Einstellung.')
  c.update(config)
 if not isinstance(c['split_house_number'],bool):raise ValueError('split_house_number muss bool sein.')
 t=c['name_thresholds'];d=c['street_distances'];f=c['blocking_fields']
 if not isinstance(t,list) or len(t)!=2 or not (.5<=t[1]<t[0]<=.99):raise ValueError('Namensschwellen ungültig.')
 if not isinstance(d,list) or len(d)!=2 or not (1<=d[0]<d[1]<=15):raise ValueError('Strassendistanzen ungültig.')
 if type(c['u_max_pairs']) is not int or not 10000<=c['u_max_pairs']<=2000000:raise ValueError('u-Stichprobe ungültig.')
 if type(c['em_max_pairs']) is not int or not 10000<=c['em_max_pairs']<=1000000:raise ValueError('EM-Grenze ungültig.')
 if not .001<=c['min_probability']<=.99:raise ValueError('Wahrscheinlichkeit ungültig.')
 if not isinstance(f,list) or len(f)<2 or len(f)!=len(set(f)) or any(x not in BLOCK_LIMITS for x in f):raise ValueError('Mindestens zwei bekannte Blocking-Felder erforderlich.')
 return c
def model_signature(config):
 c=validate_config(config)
 return hashlib.sha256(json.dumps({k:c[k] for k in ('split_house_number','name_thresholds','street_distances','blocking_fields')},sort_keys=True).encode()).hexdigest()[:16]
