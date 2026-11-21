"""Regression: internal CRM swisstopo validation accepts sqlite3.Row records."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from engine import connect, import_project
from internal_dupes import create_internal_project
from swisstopo import ensure_schema, import_snapshot, validate_record, validate_project, validation_counts, export_validation

MAP = {
    'id': 'ID', 'company': 'Firma', 'first_name': '', 'last_name': '',
    'street': 'Strasse', 'house_number': '', 'postcode': 'PLZ',
    'city': 'Ort', 'country': 'Land', 'email': '', 'phone': '', 'uid': '',
}


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / 'local.sqlite')
    ensure_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def snapshot(db):
    sid, count = import_snapshot(db, ROOT/'examples'/'TEST-SWISSTOPO-KLEIN.csv', '2026-11-20')
    assert count == 3
    return sid


def test_actual_sqlite_row_is_accepted(db, snapshot):
    frame = pd.DataFrame([{
        'ID': 'X-001', 'Firma': 'Fiktive AG', 'Strasse': 'Matthof 24',
        'PLZ': '6014', 'Ort': 'Luzern', 'Land': 'CH',
    }])
    pid = create_internal_project(db, 'Interner CRM Export', frame, MAP)
    row = db.execute('SELECT * FROM records WHERE project_id=? AND side=\'A\'', (pid,)).fetchone()
    assert isinstance(row, sqlite3.Row)
    result, hint, egaid = validate_record(db, snapshot, row)
    assert result == 'amtlich_aktuell'
    assert egaid == 'E-SYN-1'
    assert validate_record(db, snapshot, dict(row)) == (result, hint, egaid)


def test_swistopo_tab_internal_click_checks_all_originals(db, snapshot):
    # This is the exact backend call used by the screenshot's button:
    # 'Originaladressen des Exports prüfen'.
    frame = pd.DataFrame([
        {'ID': '0001', 'Firma': 'Test AG', 'Strasse': 'Matthof 24', 'PLZ': '6014', 'Ort': 'Luzern', 'Land': 'CH'},
        {'ID': '0002', 'Firma': 'Test SA', 'Strasse': 'Steinenvorstadt 59', 'PLZ': '4051', 'Ort': 'Basel', 'Land': 'CH'},
        {'ID': '0003', 'Firma': 'Test GmbH', 'Strasse': 'Hauptstrasse 80', 'PLZ': '8000', 'Ort': 'Zürich', 'Land': 'CH'},
        {'ID': '0004', 'Firma': 'Test Co.', 'Strasse': 'Matthof 24', 'PLZ': '6014', 'Ort': 'Luzern', 'Land': 'DE'},
    ])
    pid = create_internal_project(db, 'Internal swisstopo', frame, MAP)
    validate_project(db, pid)
    counts = {row['result']: row['n'] for row in validation_counts(db, pid)}
    assert sum(counts.values()) == 4
    assert counts['amtlich_aktuell'] == 1
    assert counts['hausnummer_unbekannt'] == 1
    assert counts['nicht_gefunden'] == 1
    assert counts['ausland'] == 1
    exported = list(csv.DictReader(io.StringIO(export_validation(db, pid).decode('utf-8-sig')), delimiter=';'))
    assert len(exported) == 4
    assert {r['A_ID'] for r in exported} == {'0001','0002','0003','0004'}
    # Repeat checks just like a user clicking the button a second time.
    validate_project(db, pid)
    assert db.execute('SELECT count(*) FROM swisstopo_validation WHERE project_id=?', (pid,)).fetchone()[0] == 4


def test_dual_crm_swisstopo_validation_uses_same_fixed_path(db, snapshot):
    a = pd.DataFrame([
        {'ID': 'A1', 'Firma': 'Fiktive AG', 'Strasse': 'Matthof 24', 'PLZ': '6014', 'Ort': 'Luzern', 'Land': 'CH', 'B_Referenz': ''},
    ])
    b = pd.DataFrame([
        {'ID': 'B1', 'Firma': 'Fiktive AG', 'Strasse': 'Matthof 24', 'PLZ': '6014', 'Ort': 'Luzern', 'Land': 'CH'},
    ])
    pid = import_project(db, 'A-B Test', a, MAP, b, MAP, mode='dual', a_ref_column='B_Referenz')
    validate_project(db, pid)
    count = db.execute('SELECT count(*) FROM swisstopo_validation WHERE project_id=?', (pid,)).fetchone()[0]
    assert count == 1
    assert db.execute('SELECT result FROM swisstopo_validation WHERE project_id=?', (pid,)).fetchone()[0] == 'amtlich_aktuell'


def test_realistic_59_row_internal_export(db, snapshot):
    # Same count as in the user's screenshot, using the bundled synthetic CRM B file.
    frame = pd.read_csv(ROOT/'examples'/'CRM_B_Testdaten_Fokusmodus.csv',
                        sep=';', dtype=str, keep_default_na=False)
    assert len(frame) == 59
    mapping = dict(MAP, house_number='Hausnummer')
    pid = create_internal_project(db, '59 imported rows', frame, mapping)
    validate_project(db, pid)
    assert db.execute('SELECT count(*) FROM swisstopo_validation WHERE project_id=?',
                      (pid,)).fetchone()[0] == 59
