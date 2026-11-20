"""Thread-local, short-lived SQLite connections for Streamlit fragment reruns.

Never pass sqlite3.Connection from the main script into an @st.fragment:
Streamlit can rerun that fragment in a different script thread.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def fragment_db(db_path):
    """Open a fresh connection on the *calling* fragment thread and close it.

    The main application creates/migrates the database before rendering any
    fragments. Fragment connections therefore only use already existing DBs
    and do not run the schema migration on every widget interaction.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f'Datenbank nicht gefunden: {path}')
    db = sqlite3.connect(str(path), timeout=60)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=60000')
        # Do not toggle journal_mode here. Main engine.connect() enables WAL.
        yield db
    finally:
        # Writes use explicit `with db:` blocks in the application. Do not
        # accidentally commit an incomplete write on fragment disposal.
        db.close()
