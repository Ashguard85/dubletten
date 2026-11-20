from pathlib import Path
import sys
import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'app'))
import llm


@pytest.mark.parametrize('name', ['stack-portainer-git.yml', 'stack-portainer-local.yml'])
def test_compose_uses_existing_host_ollama_and_local_data(name):
    config = yaml.safe_load((ROOT/name).read_text())
    assert set(config['services']) == {'app'}
    app = config['services']['app']
    assert 'host.docker.internal:host-gateway' in app['extra_hosts']
    assert app['environment']['OLLAMA_BASE_URL'] == 'http://host.docker.internal:11434'
    assert app['environment']['OLLAMA_TEXT_MODEL'] == 'qwen3:14b'
    assert app['read_only'] is True
    assert app['cap_drop'] == ['ALL']
    assert '/home/buebi/docker/crm-dubletten/data:/app/data' in app['volumes']
    assert '${APP_BIND_IP:-127.0.0.1}' in app['ports'][0]
    assert 'APP_PASSWORD' in app['environment']
    assert config['networks']['appnet']['driver_opts']['com.docker.network.bridge.name'] == 'br-crm-dub'
    if name == 'stack-portainer-git.yml':
        assert app['pull_policy'] == 'build'
        assert app['build']['context'].endswith('.git#main')
    else:
        assert app['pull_policy'] == 'never'
        assert 'build' not in app


def test_llm_refuses_remote_endpoint(monkeypatch):
    monkeypatch.setenv('OLLAMA_BASE_URL', 'https://api.provider.com/v1')
    with pytest.raises(ValueError):
        llm._ollama_url()


def test_llm_no_proxy(monkeypatch):
    assert llm._session.trust_env is False
    assert llm.OLLAMA == 'http://host.docker.internal:11434'
    assert llm.MODEL == 'qwen3:14b'


def test_local_models_only(monkeypatch, tmp_path):
    import sqlite3
    import ai_control
    dbpath=tmp_path/'crm.sqlite'
    with sqlite3.connect(dbpath) as db:
        ai_control.set_enabled(db, True)
    monkeypatch.setenv('DB_PATH',str(dbpath))
    class Response:
        is_redirect = False
        def raise_for_status(self): pass
        def json(self):
            return {'models':[{'name':'qwen3:14b'}, {'name':'bad:cloud'}, {'name':'gemma3:12b'}]}
    called=[]
    def get(url, timeout, allow_redirects):
        called.append((url, allow_redirects))
        return Response()
    monkeypatch.setattr(llm._session, 'get', get)
    assert llm.models() == ['qwen3:14b', 'gemma3:12b']
    assert called == [('http://host.docker.internal:11434/api/tags', False)]
