import json
import stat
import time

import pytest

from freehand import t3_auth
from freehand.t3 import T3Client


def test_callback_checks_state_path_and_code():
    assert t3_auth.callback_code('/callback?state=expected&code=abc', 'expected') == 'abc'
    for path in ['/callback?state=wrong&code=abc', '/other?state=expected&code=abc',
                 '/callback?state=expected', '/callback?state=expected&error=access_denied']:
        with pytest.raises(ValueError):
            t3_auth.callback_code(path, 'expected')


def test_credential_is_private_and_checks_expiry_and_server(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    resource = 'http://127.0.0.1:3773/mcp'
    t3_auth.save_credential({'access_token': 'test-token', 'expires_in': 3600}, resource)
    assert stat.S_IMODE(t3_auth.credential_path().stat().st_mode) == 0o600
    token, expiry = t3_auth.load_credential(resource)
    assert token == 'test-token' and expiry > time.time()
    with pytest.raises(RuntimeError, match='server changed'):
        t3_auth.load_credential('http://other/mcp')
    t3_auth.save_credential({'access_token': 'test-token', 'expires_in': 0}, resource)
    with pytest.raises(RuntimeError, match='expired'):
        t3_auth.load_credential(resource)


def test_creation_uses_approved_credential_without_minting(monkeypatch):
    monkeypatch.setattr(t3_auth, 'load_credential', lambda resource: ('approved', 9999999999))
    c = T3Client(scope='orchestration:operate')
    c._mint()
    assert c.token == 'approved' and c.token_expires == 9999999999
    c.pool.shutdown()


def test_missing_credential_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    c = T3Client(scope='orchestration:operate')
    with pytest.raises(RuntimeError, match='Connect freehand'):
        c._mint()
    assert c.token is None
    c.pool.shutdown()
