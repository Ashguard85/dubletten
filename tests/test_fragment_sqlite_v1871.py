"""Regression for Streamlit fragment reruns on a different thread.

In v1.8.7 the score slider reran `render_review` with a sqlite3.Connection
created by the parent Streamlit script thread, raising ProgrammingError.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from engine import connect, run_matching, set_review
from fragment_db import fragment_db
from internal_dupes import create_internal_project
from review_data import pair_page

MAP = {
    'id': 'ID', 'company': 'Firma', 'first_name': '', 'last_name': '',
    'street': 'Strasse', 'house_number': '', 'postcode': 'PLZ',
    'city': 'Ort', 'country': '', 'email': '', 'phone': '', 'uid': '',
}


def test_fragment_connection_created_in_worker_thread(tmp_path):
    path = tmp_path / 'local.sqlite'
    parent = connect(path)
    parent.execute('CREATE TABLE IF NOT EXISTS fragment_regression (n INT)')
    parent.execute('INSERT INTO fragment_regression VALUES (7)')
    parent.commit()

    def wrong_connection():
        try:
            return parent.execute('SELECT n FROM fragment_regression').fetchone()[0]
        except sqlite3.ProgrammingError:
            return 'reproduced'

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(wrong_connection).result() == 'reproduced'

        def own_connection():
            with fragment_db(path) as db:
                return db.execute('SELECT n FROM fragment_regression').fetchone()[0]

        assert executor.submit(own_connection).result() == 7
        assert executor.submit(own_connection).result() == 7
    parent.close()


def test_internal_score_reruns_and_decisions_are_persistent(tmp_path):
    path = tmp_path / 'intern.sqlite'
    parent = connect(path)
    rows = pd.DataFrame([
        {'ID': '001', 'Firma': 'Test Elektro AG', 'Strasse': 'Hauptstrasse 7', 'PLZ': '6000', 'Ort': 'Luzern'},
        {'ID': '002', 'Firma': 'Test Elektro AG', 'Strasse': 'Hauptstrasse 7', 'PLZ': '6000', 'Ort': 'Luzern'},
        {'ID': '003', 'Firma': 'Sonstiges Unternehmen', 'Strasse': 'Schlossweg 40', 'PLZ': '9000', 'Ort': 'St. Gallen'},
    ])
    pid = create_internal_project(parent, 'Thread-Test', rows, MAP)
    run_matching(parent, pid, minimum=45)

    def filter_fragment(score):
        # Mimics the rerun of the @st.fragment when moving the Score slider.
        with fragment_db(path) as db:
            meta = db.execute('SELECT mode FROM projects WHERE project_id=?', (pid,)).fetchone()['mode']
            total, _, _, matches = pair_page(db, pid, 'Alle', score, '', 'Alle', 1, 25)
            return meta, total, matches[0] if matches else None

    with ThreadPoolExecutor(max_workers=1) as executor:
        mode, count, first = executor.submit(filter_fragment, 0).result()
        assert mode == 'internal'
        assert count >= 1
        assert first['score'] >= 45

        def save_in_fragment():
            with fragment_db(path) as db:
                set_review(db, pid, first['a_pk'], first['b_pk'], 'unklar')

        executor.submit(save_in_fragment).result()
        assert executor.submit(filter_fragment, 100).result()[1] <= count
        mode, count_again, _ = executor.submit(filter_fragment, 0).result()
        assert mode == 'internal' and count_again == count

    state = parent.execute('SELECT state FROM pairs WHERE project_id=? AND a_pk=? AND b_pk=?',
                           (pid, first['a_pk'], first['b_pk'])).fetchone()['state']
    assert state == 'unklar'
    parent.close()


def test_every_streamlit_fragment_uses_thread_local_connection():
    review = (ROOT / 'app' / 'review_ui.py').read_text(encoding='utf-8')
    focus = (ROOT / 'app' / 'focus_ui.py').read_text(encoding='utf-8')
    main = (ROOT / 'app' / 'ui.py').read_text(encoding='utf-8')
    internal = (ROOT / 'app' / 'internal_ui.py').read_text(encoding='utf-8')
    assert '@st.fragment\ndef render_review(db_path,' in review
    assert '@st.fragment\ndef _render_focus(db_path):' in focus
    assert 'with fragment_db(db_path) as db:' in review
    assert 'with fragment_db(db_path) as db:' in focus
    assert 'render_focus_workspace(db_path)' in main
    assert 'render_review(db_path,pid,' in internal
    assert 'render_review(db_path,pid,' in main
