"""Persistent, fail-closed local AI permission switch.

AI starts disabled for all installations. The UI stores a single permission
in the existing SQLite database, and Ollama requests verify it independently.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

SETTING_KEY = 'ollama_ai_enabled'


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS app_settings (
        setting TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    db.commit()


def is_enabled(db: sqlite3.Connection) -> bool:
    try:
        row = db.execute('SELECT value FROM app_settings WHERE setting=?', (SETTING_KEY,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == '1')


def set_enabled(db: sqlite3.Connection, value: bool) -> None:
    ensure_schema(db)
    db.execute('''INSERT INTO app_settings(setting, value) VALUES (?, ?)
                  ON CONFLICT(setting) DO UPDATE SET value=excluded.value''',
               (SETTING_KEY, '1' if value else '0'))
    db.commit()


def enabled_from_path() -> bool:
    """Re-check permission for each API call; unavailable DB => disabled."""
    path = Path(os.environ.get('DB_PATH', '/app/data/crm.sqlite'))
    if not path.is_file():
        return False
    try:
        # Open read-only: a model request must never create or modify a DB.
        uri = 'file:' + quote(str(path.resolve()), safe='/') + '?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            return is_enabled(db)
    except (OSError, sqlite3.Error):
        return False
