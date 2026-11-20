"""Opt-in persisted local Ollama switch. Fail-closed for missing DB."""
import os,sqlite3
from pathlib import Path
SETTING_KEY='ollama_ai_enabled'
def ensure_schema(db):
 db.execute('CREATE TABLE IF NOT EXISTS app_settings(setting TEXT PRIMARY KEY,value TEXT NOT NULL)');db.commit()
def is_enabled(db):
 try:
  r=db.execute('SELECT value FROM app_settings WHERE setting=?',(SETTING_KEY,)).fetchone()
  return bool(r and r[0]=='1')
 except sqlite3.Error:return False
def set_enabled(db,value):
 ensure_schema(db)
 with db:db.execute('INSERT INTO app_settings VALUES(?,?) ON CONFLICT(setting) DO UPDATE SET value=excluded.value',(SETTING_KEY,'1' if value else '0'))
def enabled_from_path():
 p=Path(os.environ.get('DB_PATH','/app/data/crm.sqlite'))
 if not p.is_file():return False
 try:
  with sqlite3.connect(f'file:{p.resolve()}?mode=ro',uri=True,timeout=1) as cx:return is_enabled(cx)
 except (OSError,sqlite3.Error):return False
