"""Prevent accidental network calls to Ollama until explicitly permitted."""
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import ai_control
import llm


def test_initial_state_disabled_even_after_schema_creation(tmp_path, monkeypatch):
    path = tmp_path / 'app.sqlite'
    monkeypatch.setenv('DB_PATH', str(path))
    with sqlite3.connect(path) as db:
        ai_control.ensure_schema(db)
        assert not ai_control.is_enabled(db)
    assert not ai_control.enabled_from_path()


def test_disabled_does_not_fetch_models_or_send_chat(tmp_path, monkeypatch):
    path = tmp_path / 'app.sqlite'
    monkeypatch.setenv('DB_PATH', str(path))
    with sqlite3.connect(path) as db:
        ai_control.ensure_schema(db)
    requests = []
    monkeypatch.setattr(llm._session, 'get', lambda *a, **k: requests.append(('get',a,k)))
    monkeypatch.setattr(llm._session, 'post', lambda *a, **k: requests.append(('post',a,k)))
    assert llm.models() == []
    with pytest.raises(RuntimeError, match='deaktiviert'):
        llm.assess({'company':'Private AG'}, {'company':'Private SA'})
    assert requests == []


def test_setting_is_persistent_and_disable_is_immediate(tmp_path, monkeypatch):
    path = tmp_path / 'app.sqlite'
    monkeypatch.setenv('DB_PATH', str(path))
    with sqlite3.connect(path) as db:
        ai_control.ensure_schema(db)
        ai_control.set_enabled(db, True)
        assert ai_control.is_enabled(db)
    assert ai_control.enabled_from_path()
    with sqlite3.connect(path) as db:
        ai_control.set_enabled(db, False)
    assert not ai_control.enabled_from_path()
    assert llm.models() == []


def test_no_database_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'none.sqlite'))
    assert not ai_control.enabled_from_path()
    assert llm.models() == []
    with pytest.raises(RuntimeError, match='deaktiviert'):
        llm.assess({}, {})


def test_review_controls_are_conditionally_displayed():
    source = (Path(__file__).resolve().parents[1] / 'app' / 'ui.py').read_text()
    assert 'installed = []' in source
    assert 'installed = models()' in source
    assert 'if not ai_enabled:' in source
    review = (Path(__file__).resolve().parents[1] / 'app' / 'review_ui.py').read_text()
    assert 'installed and ai_is_enabled(db)' in review
    assert 'ai_enabled and ai_is_enabled(db)' in review
