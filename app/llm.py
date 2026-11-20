"""Local Ollama only; requests forbidden until user enables them."""
import os,json,requests
from ai_control import enabled_from_path
OLLAMA=os.environ.get('OLLAMA_BASE_URL','http://host.docker.internal:11434').rstrip('/')
MODEL=os.environ.get('OLLAMA_TEXT_MODEL','qwen3:14b')
TIMEOUT=int(os.environ.get('OLLAMA_TIMEOUT_SECONDS','600'))
session=requests.Session();session.trust_env=False

def models():
 if not enabled_from_path():return []
 try:
  r=session.get(OLLAMA+'/api/tags',timeout=5);r.raise_for_status();return [x['name'] for x in r.json().get('models',[]) if x.get('name')]
 except (requests.RequestException,ValueError,KeyError):return []
def assess(a,b,model=MODEL):
 if not enabled_from_path():raise RuntimeError('KI ist ausgeschaltet. Keine Ollama-Anfrage gesendet.')
 if model not in models():raise RuntimeError('Gewähltes lokales Ollama-Modell nicht vorhanden.')
 prompt=('Prüfe zwei CRM-Einträge auf dieselbe Person oder Firma. Antworte nur als JSON mit '
   '"decision" (gleich/verschieden/unklar), "reason". Unterschiedliche Hausnummern, UID und '
   'juristische Personen sind wichtige Gegenargumente. Niemals automatisch freigeben. '
   'CRM A: '+json.dumps(a,ensure_ascii=False)+' CRM B: '+json.dumps(b,ensure_ascii=False))
 payload={'model':model,'messages':[{'role':'user','content':prompt}],'stream':False,'format':'json',
          'options':{'temperature':0}}
 if not enabled_from_path():raise RuntimeError('KI inzwischen deaktiviert.')
 r=session.post(OLLAMA+'/api/chat',json=payload,timeout=TIMEOUT);r.raise_for_status()
 raw=r.json().get('message',{}).get('content','{}')
 try:
  obj=json.loads(raw);decision=obj.get('decision','unklar');reason=str(obj.get('reason',''))
 except (ValueError,AttributeError):return 'unklar','Antwort nicht als gültiges JSON lesbar'
 return decision if decision in ('gleich','verschieden','unklar') else 'unklar',reason[:1800]
