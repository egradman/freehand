"""User-approved T3 OAuth connection, using PKCE and a loopback callback."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.request


def credential_path():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'freehand/t3-oauth.json'


def load_credential(resource):
    try:
        data = json.loads(credential_path().read_text())
    except FileNotFoundError:
        raise RuntimeError('Connect freehand to T3 first: python -m freehand.t3_auth') from None
    if data.get('resource') != resource or data.get('expires_at', 0) <= time.time() + 60:
        raise RuntimeError('T3 connection expired or server changed; reconnect with python -m freehand.t3_auth')
    return data['access_token'], data['expires_at']


def save_credential(data, resource):
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {'access_token': data['access_token'], 'expires_at': time.time() + data['expires_in'],
              'resource': resource}
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.t3-oauth-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(stored, f)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def post(url, data, form=False):
    body = urlencode(data).encode() if form else json.dumps(data).encode()
    content_type = 'application/x-www-form-urlencoded' if form else 'application/json'
    req = urllib.request.Request(url, body, {'Content-Type': content_type})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def callback_code(path, expected_state):
    parsed = urlsplit(path)
    if parsed.path != '/callback':
        raise ValueError('Unknown callback path')
    query = parse_qs(parsed.query)
    state = query.get('state', [''])[0]
    if not secrets.compare_digest(state, expected_state):
        raise ValueError('Invalid OAuth state')
    if 'error' in query:
        raise ValueError('Authorization was declined')
    code = query.get('code', [''])[0]
    if not code:
        raise ValueError('Missing authorization code')
    return code


def connect(resource='http://127.0.0.1:3773/mcp'):
    base = resource.removesuffix('/mcp')
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # callback URLs contain authorization codes

        def do_GET(self):
            try:
                code = callback_code(self.path, state)
            except ValueError:
                self.send_error(400, 'Invalid or declined authorization')
                return
            try:
                token = post(base + '/oauth/mcp/token', {
                    'grant_type': 'authorization_code', 'code': code,
                    'client_id': client['client_id'], 'redirect_uri': redirect,
                    'code_verifier': verifier, 'resource': resource,
                }, form=True)
                save_credential(token, resource)
            except Exception:
                outcome['error'] = True
                self.send_error(502, 'Could not save the T3 connection. Run setup again.')
                return
            outcome['connected'] = True
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'Freehand connected to T3. You can close this tab and try the remote.')

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        redirect = f'http://127.0.0.1:{server.server_port}/callback'
        client = post(base + '/oauth/mcp/register', {
            'client_name': 'freehand remote', 'redirect_uris': [redirect],
            'token_endpoint_auth_method': 'none',
        })
        url = base + '/oauth/mcp/authorize?' + urlencode({
            'response_type': 'code', 'client_id': client['client_id'],
            'redirect_uri': redirect, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'state': state, 'resource': resource,
        })
        subprocess.run(['xdg-open', url], check=True, stdout=subprocess.DEVNULL)
        print('Approval opened. Choose Auto access for freehand remote.', flush=True)
        server.timeout = 1
        deadline = time.monotonic() + 900
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
        if not outcome.get('connected'):
            raise RuntimeError('T3 authorization did not complete. Run setup again.')
        print('T3 connection saved securely.', flush=True)


if __name__ == '__main__':
    connect()
