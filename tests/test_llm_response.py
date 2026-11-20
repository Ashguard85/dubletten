from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import llm

@pytest.fixture(autouse=True)
def enable_ai_for_legacy_llm_tests(tmp_path, monkeypatch):
    """Legacy response tests exercise enabled AI; default-off is tested separately."""
    import sqlite3
    import ai_control
    dbpath = tmp_path / 'crm.sqlite'
    with sqlite3.connect(dbpath) as db:
        ai_control.set_enabled(db, True)
    monkeypatch.setenv('DB_PATH', str(dbpath))


A = {'company': 'Müller AG', 'street': 'Bahnhofstrasse 5', 'postcode': '6003', 'city': 'Luzern'}
B = {'company': 'Mueller AG', 'street': 'Bahnhofstr. 5', 'postcode': '6003', 'city': 'Luzern'}


class Response:
    is_redirect = False
    status_code = 200
    def __init__(self, value):
        self.value = value
    def raise_for_status(self):
        return None
    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def mock_post(monkeypatch, reply):
    calls = []
    monkeypatch.setattr(llm, 'models', lambda: ['qwen3:14b'])
    def post(url, json, timeout, allow_redirects):
        calls.append((url, json, timeout, allow_redirects))
        return Response(reply)
    monkeypatch.setattr(llm._session, 'post', post)
    return calls


def test_qwen3_disables_thinking_and_returns_decision(monkeypatch):
    calls = mock_post(monkeypatch, {'message': {'content': '{"entscheidung":"gleich","begruendung":"Gleiche Adresse"}'}, 'done': True})
    decision, reason = llm.assess(A, B, 'qwen3:14b')
    assert (decision, reason) == ('gleich', 'Gleiche Adresse')
    url, body, timeout, redirects = calls[0]
    assert url == 'http://host.docker.internal:11434/api/chat'
    assert body['think'] is False
    assert body['stream'] is False
    assert body['format'] == 'json'
    assert body['options']['num_predict'] >= 400
    assert redirects is False
    assert 'Müller AG' in body['messages'][1]['content']


def test_empty_content_explains_problem_without_leaking_data(monkeypatch):
    mock_post(monkeypatch, {'message': {'content': '', 'thinking': 'sensitive chain'}, 'done_reason': 'length'})
    with pytest.raises(RuntimeError, match='leeren Antworttext') as exc:
        llm.assess(A, B)
    assert 'Thinking-Ausgabe: ja' in str(exc.value)
    assert 'sensitive chain' not in str(exc.value)
    assert 'Müller' not in str(exc.value)


def test_invalid_json_explains_problem_without_raw_text(monkeypatch):
    mock_post(monkeypatch, {'message': {'content': 'not json with private text'}})
    with pytest.raises(RuntimeError, match='kein gültiges Ergebnis-JSON') as exc:
        llm.assess(A, B)
    assert 'private text' not in str(exc.value)


def test_invalid_api_response(monkeypatch):
    mock_post(monkeypatch, ValueError('malformed response'))
    with pytest.raises(RuntimeError, match='keine gültige API-JSON-Antwort'):
        llm.assess(A, B)


def test_invalid_json_shape(monkeypatch):
    mock_post(monkeypatch, {'message': {'content': '[1,2]'}})
    with pytest.raises(RuntimeError, match='kein JSON-Objekt'):
        llm.assess(A, B)


def test_missing_model_is_explicit(monkeypatch):
    monkeypatch.setattr(llm, 'models', lambda: [])
    with pytest.raises(ValueError, match='nicht installiert'):
        llm.assess(A, B)
