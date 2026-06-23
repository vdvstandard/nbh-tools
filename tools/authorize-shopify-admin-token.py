#!/usr/bin/env python3

import argparse
import json
import os
import re
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime, timedelta

DEFAULT_SCOPES = 'write_products'
DEFAULT_PORT = 3456
DEFAULT_CALLBACK_PATH = '/callback'


def load_dotenv(file_path):
    env_path = Path(file_path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding='utf-8').splitlines():
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$', line)
        if not match:
            continue

        key, raw_value = match.group(1), match.group(2)
        if key in os.environ:
            continue
        os.environ[key] = parse_env_value(raw_value)


def upsert_dotenv(file_path, updates):
    env_path = Path(file_path)
    lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
    used_keys = set()
    updated_lines = []

    for line in lines:
        match = re.match(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$', line)
        if not match or match.group(2) not in updates:
            updated_lines.append(line)
            continue

        prefix, key, separator = match.group(1), match.group(2), match.group(3)
        used_keys.add(key)
        updated_lines.append(f'{prefix}{key}{separator}{format_env_value(updates[key])}')

    for key, value in updates.items():
        if key not in used_keys:
            updated_lines.append(f'{key}={format_env_value(value)}')

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text('\n'.join(updated_lines).rstrip('\n') + '\n', encoding='utf-8')


def parse_env_value(value):
    trimmed = value.strip()
    if (trimmed.startswith('"') and trimmed.endswith('"')) or (trimmed.startswith("'") and trimmed.endswith("'")):
        return trimmed[1:-1]
    return trimmed


def format_env_value(value):
    string_value = str(value or '')
    if re.match(r'^[A-Za-z0-9_./:@,-]*$', string_value):
        return string_value
    return json.dumps(string_value)


def normalize_shop(value):
    if not value:
        return ''
    cleaned = value.strip().replace('http://', '').replace('https://', '').split('/')[0]
    if not cleaned:
        return ''
    return cleaned if '.' in cleaned else f'{cleaned}.myshopify.com'


def is_valid_shop(value):
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$', value))


def normalize_scopes(value):
    return ','.join(part.strip() for part in str(value).split(',') if part.strip())


def mask_secret(value):
    if not value:
        return ''
    value = str(value)
    return value if len(value) <= 10 else f'{value[:6]}...{value[-4:]}'


def urlencode_form(data):
    return urllib.parse.urlencode(data).encode('utf-8')


def http_post(url, body, headers=None, timeout=30):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status


def write_json_file(path, data):
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def remove_file_if_exists(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def build_authorize_url(shop, client_id, scopes, redirect_uri, state, token_mode):
    params = {
        'client_id': client_id,
        'scope': scopes,
        'redirect_uri': redirect_uri,
        'state': state,
    }
    if token_mode == 'online':
        params['grant_options[]'] = 'per-user'
    return f'https://{shop}/admin/oauth/authorize?{urllib.parse.urlencode(params)}'


def get_client_credentials_token(shop, client_id, client_secret):
    body = urlencode_form({
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
    })
    response_text, status = http_post(
        f'https://{shop}/admin/oauth/access_token',
        body,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    data = json.loads(response_text or '{}')
    if status < 200 or status >= 300:
        message = data.get('error_description') or data.get('error') or response_text or 'Unknown error'
        raise RuntimeError(f'Shopify client_credentials failed ({status}): {message}')
    if 'access_token' not in data:
        raise RuntimeError('Shopify did not return an access_token.')
    return data


def parse_query_params(query_string):
    raw = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    return {key: values[0] if values else '' for key, values in raw.items()}


def is_valid_hmac(query_params, secret):
    provided_hmac = query_params.get('hmac', '')
    filtered = [(k, v) for k, v in query_params.items() if k not in ('hmac', 'signature')]
    sorted_items = sorted(filtered, key=lambda item: item[0])
    message = '&'.join(f'{k}={urllib.parse.quote(v, safe="")}' for k, v in sorted_items)
    calculated = secrets.pbkdf2_hmac('sha256', message.encode('utf-8'), secret.encode('utf-8'), 1).hex()
    return secrets.compare_digest(calculated, provided_hmac)


def finish_authorization(token, shop, client_secret, scopes, redirect_uri, result_file, env_path, write_env, token_mode, print_token, quiet):
    refreshed_at = datetime.utcnow()
    expires_at = None
    if token.get('expires_in') is not None:
        expires_at = refreshed_at + timedelta(seconds=int(token['expires_in']))

    updates = {
        'SHOPIFY_SHOP': shop,
        'SHOPIFY_SCOPES': scopes,
        'SHOPIFY_OAUTH_REDIRECT_URI': redirect_uri,
        'SHOPIFY_ADMIN_ACCESS_TOKEN': token['access_token'],
        'SHOPIFY_ADMIN_ACCESS_TOKEN_TYPE': token_mode,
        'SHOPIFY_ADMIN_ACCESS_TOKEN_SCOPE': token.get('scope', ''),
        'SHOPIFY_ADMIN_ACCESS_TOKEN_REFRESHED_AT': refreshed_at.isoformat() + 'Z',
        'SHOPIFY_ADMIN_ACCESS_TOKEN_EXPIRES_AT': expires_at.isoformat() + 'Z' if expires_at else '',
    }

    if token.get('refresh_token'):
        updates['SHOPIFY_REFRESH_TOKEN'] = token['refresh_token']
    if token.get('refresh_token_expires_in') is not None:
        refresh_expires = refreshed_at + timedelta(seconds=int(token['refresh_token_expires_in']))
        updates['SHOPIFY_REFRESH_TOKEN_EXPIRES_AT'] = refresh_expires.isoformat() + 'Z'

    if write_env:
        upsert_dotenv(env_path, updates)

    write_json_file(result_file, {
        'status': 'success',
        'completedAt': refreshed_at.isoformat() + 'Z',
        'shop': shop,
        'tokenType': token_mode,
        'scopes': token.get('scope', ''),
        'expiresAt': expires_at.isoformat() + 'Z' if expires_at else None,
        'tokenStoredInEnv': write_env,
        'accessTokenMasked': mask_secret(token['access_token']),
    })

    if not quiet:
        print('\nAuthorization complete.')
        print(f"Access token: {token['access_token'] if print_token else mask_secret(token['access_token'])}")
        print(f"Granted scopes: {token.get('scope', '(not returned)')}")
        print(f"Expires at: {expires_at.isoformat() + 'Z' if expires_at else 'non-expiring token'}")
        print(f"Updated {env_path}" if write_env else 'No token was written to .env.')


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = 'ShopifyOAuthPython/1.0'
    quiet = False
    shop = ''
    client_id = ''
    client_secret = ''
    scopes = ''
    redirect_uri = ''
    callback_path = DEFAULT_CALLBACK_PATH
    state = ''
    token_mode = 'offline'
    write_env = True
    result_file = ''
    env_path = ''
    print_token = False
    error_message = None

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != self.callback_path:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'Not found.')
            return

        try:
            params = parse_query_params(parsed.query)
            token = self.handle_callback(params)
            finish_authorization(
                token,
                self.shop,
                self.client_secret,
                self.scopes,
                self.redirect_uri,
                self.result_file,
                self.env_path,
                self.write_env,
                self.token_mode,
                self.print_token,
                self.quiet,
            )
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'''<!doctype html>
<title>Shopify authorization complete</title>
<body style="font-family: system-ui, sans-serif; margin: 2rem;">
  <h1>Authorization complete</h1>
  <p>{'The Shopify Admin token was written to ' + str(self.env_path) + '.' if self.write_env else 'The Shopify Admin token was generated and was not stored locally.'}</p>
  <p>You can close this tab and return to the terminal.</p>
</body>
'''.encode('utf-8'))
        except Exception as error:
            self.error_message = str(error)
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'Authorization failed: {error}'.encode('utf-8'))
            if not self.quiet:
                print(f'\nAuthorization failed: {error}')
        finally:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def handle_callback(self, query_params):
        return handle_callback_search_params(
            query_params,
            expected_state=self.state,
            require_state=True,
            shop=self.shop,
            client_secret=self.client_secret,
            client_id=self.client_id,
            token_mode=self.token_mode,
        )

    def log_message(self, format, *args):
        if not self.quiet:
            super().log_message(format, *args)


def handle_callback_search_params(query_params, expected_state, require_state, shop, client_secret, client_id, token_mode):
    code = query_params.get('code', '')
    if not code:
        raise RuntimeError('Missing authorization code in callback.')

    hmac = query_params.get('hmac', '')
    if not hmac:
        raise RuntimeError('Missing hmac in callback.')

    returned_state = query_params.get('state', '')
    if require_state and returned_state != expected_state:
        raise RuntimeError('OAuth state mismatch.')

    callback_shop = normalize_shop(query_params.get('shop', shop))
    if callback_shop != shop or not is_valid_shop(callback_shop):
        raise RuntimeError(f'Unexpected callback shop: {callback_shop or "(missing)"}')

    if not is_valid_hmac(query_params, client_secret):
        raise RuntimeError('Invalid Shopify callback hmac.')

    return exchange_authorization_code(
        shop=shop,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        expiring_offline_token=(token_mode == 'offline'),
    )


def exchange_authorization_code(shop, client_id, client_secret, code, expiring_offline_token):
    params = {
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
    }
    if expiring_offline_token:
        params['expiring'] = '1'

    body = urlencode_form(params)
    response_text, status = http_post(
        f'https://{shop}/admin/oauth/access_token',
        body,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    data = json.loads(response_text or '{}')
    if status < 200 or status >= 300:
        message = data.get('error_description') or data.get('error') or response_text or 'Unknown error'
        raise RuntimeError(f'Shopify authorization code exchange failed ({status}): {message}')
    if 'access_token' not in data:
        raise RuntimeError('Shopify did not return an access_token.')
    return data


def read_stdin():
    return sys.stdin.read()


def main():
    parser = argparse.ArgumentParser(description='Shopify OAuth authorization helper.')
    parser.add_argument('--open', action='store_true', help='Open the authorize URL in the browser.')
    parser.add_argument('--no-write-env', action='store_true', help='Generate a token without storing it in .env.')
    parser.add_argument('--from-stdin', action='store_true', help='Read a Shopify OAuth callback URL from stdin.')
    parser.add_argument('--callback-url', help='Process a pasted Shopify OAuth callback URL directly.')
    parser.add_argument('--state', help='Optional state value to validate with manual callback mode.')
    parser.add_argument('--online', action='store_true', help='Request a 24-hour online token. Default is offline.')
    parser.add_argument('--expiring', action='store_true', help='Only for offline tokens: request an expiring offline token.')
    parser.add_argument('--print-token', action='store_true', help='Print the full returned access token.')
    parser.add_argument('--quiet', action='store_true', help='Keep terminal output minimal.')
    parser.add_argument('--shop', help='Shopify store domain.')
    parser.add_argument('--api-key', help='Shopify app client ID/API key.')
    parser.add_argument('--api-secret', help='Shopify app client secret.')
    parser.add_argument('--scopes', help='Comma-separated OAuth scopes.')
    parser.add_argument('--redirect-uri', help='Redirect URI for Shopify OAuth.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='Local callback port. Default: 3456.')
    parser.add_argument('--timeout-seconds', type=int, default=300, help='Stop waiting after n seconds. Default: 300.')
    parser.add_argument('--session-file', help='Where to write authorize URL metadata.')
    parser.add_argument('--result-file', help='Where to write callback result metadata.')
    parser.add_argument('--env-file', help='Path to .env. Default: ../.env relative to this script.')

    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file or project_root / '.env')
    load_dotenv(env_path)

    shop = normalize_shop(args.shop or os.environ.get('SHOPIFY_SHOP') or os.environ.get('SHOPIFY_STORE_DOMAIN') or os.environ.get('SHOPIFY_STORE'))
    client_id = args.api_key or os.environ.get('SHOPIFY_API_KEY') or os.environ.get('SHOPIFY_CLIENT_ID')
    client_secret = args.api_secret or os.environ.get('SHOPIFY_API_SECRET') or os.environ.get('SHOPIFY_CLIENT_SECRET')
    scopes = normalize_scopes(args.scopes or os.environ.get('SHOPIFY_SCOPES') or DEFAULT_SCOPES)
    token_mode = 'online' if args.online else 'offline'
    port = args.port
    timeout_seconds = args.timeout_seconds
    redirect_uri = args.redirect_uri or os.environ.get('SHOPIFY_OAUTH_REDIRECT_URI') or f'http://localhost:{port}{DEFAULT_CALLBACK_PATH}'
    callback_path = urllib.parse.urlsplit(redirect_uri).path or DEFAULT_CALLBACK_PATH
    state = secrets.token_hex(24)
    open_browser = args.open
    print_token = args.print_token
    expiring_offline_token = args.expiring
    write_env = not args.no_write_env
    quiet = args.quiet
    manual_callback_url = args.callback_url or (args.from_stdin and read_stdin().strip()) or ''

    if not shop or not client_id or not client_secret:
        parser.print_help()
        raise SystemExit('Missing credentials. Set SHOPIFY_SHOP, SHOPIFY_API_KEY, and SHOPIFY_API_SECRET.')

    if port < 1 or port > 65535:
        raise SystemExit('--port must be an integer from 1 to 65535.')

    if timeout_seconds < 30:
        raise SystemExit('--timeout-seconds must be an integer of at least 30.')

    if not is_valid_shop(shop):
        raise SystemExit(f'Invalid Shopify shop domain: {shop}')

    project_root = Path(__file__).resolve().parent.parent
    tmp_dir = project_root / '.tmp'
    session_file = Path(args.session_file or tmp_dir / 'shopify-oauth-session.json')
    result_file = Path(args.result_file or tmp_dir / 'shopify-oauth-result.json')

    if manual_callback_url:
        token = handle_manual_callback(manual_callback_url, args.state, shop, client_id, client_secret, token_mode)
        finish_authorization(
            token,
            shop,
            client_secret,
            scopes,
            redirect_uri,
            str(result_file),
            str(env_path),
            write_env,
            token_mode,
            print_token,
            quiet,
        )
        return

    authorize_url = build_authorize_url(
        shop=shop,
        client_id=client_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        state=state,
        token_mode=token_mode,
    )

    session_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(str(session_file), {
        'status': 'waiting',
        'createdAt': datetime.utcnow().isoformat() + 'Z',
        'expiresAt': (datetime.utcnow() + timedelta(seconds=timeout_seconds)).isoformat() + 'Z',
        'processId': os.getpid(),
        'shop': shop,
        'scopes': scopes,
        'tokenType': token_mode,
        'redirectUri': redirect_uri,
        'authorizeUrl': authorize_url,
        'writeEnv': write_env,
        'resultFile': str(result_file),
    })
    remove_file_if_exists(result_file)

    if not quiet:
        print('Shopify standalone OAuth is ready.')
        print(f'Redirect URI: {redirect_uri}')
        print(f'Scopes: {scopes}')
        print(f'Token type: {token_mode}{" (expiring)" if expiring_offline_token else ""}')
        print(f'Store token in .env: {"yes" if write_env else "no"}')
        print(f'Session file: {session_file}')
        print(f'Result file: {result_file}')
        print('\nBefore continuing, this exact redirect URL must be allowed in your Shopify app:')
        print(f'  {redirect_uri}')
        print('\nAuthorize URL:')
        print(authorize_url)

    server = HTTPServer(('localhost', port), CallbackHandler)
    CallbackHandler.quiet = quiet
    CallbackHandler.shop = shop
    CallbackHandler.client_id = client_id
    CallbackHandler.client_secret = client_secret
    CallbackHandler.scopes = scopes
    CallbackHandler.redirect_uri = redirect_uri
    CallbackHandler.callback_path = callback_path
    CallbackHandler.state = state
    CallbackHandler.token_mode = token_mode
    CallbackHandler.write_env = write_env
    CallbackHandler.result_file = str(result_file)
    CallbackHandler.env_path = str(env_path)
    CallbackHandler.print_token = print_token

    def on_timeout():
        if not quiet:
            print(f'\nAuthorization timed out after {timeout_seconds} seconds.')
        write_json_file(str(result_file), {
            'status': 'timeout',
            'completedAt': datetime.utcnow().isoformat() + 'Z',
            'message': f'Authorization timed out after {timeout_seconds} seconds.',
        })
        server.shutdown()

    timer = threading.Timer(timeout_seconds, on_timeout)
    timer.start()

    try:
        if not quiet:
            print(f'\nWaiting for Shopify callback on {redirect_uri}')
        if open_browser:
            webbrowser.open(authorize_url)
        elif not quiet:
            print('Open the Authorize URL above in your browser.')
        server.serve_forever()
    finally:
        timer.cancel()


def handle_manual_callback(callback_url, state, shop, client_id, client_secret, token_mode):
    try:
        parsed = urllib.parse.urlsplit(callback_url)
    except Exception:
        raise RuntimeError('The callback URL is not a valid URL.')

    query_params = parse_query_params(parsed.query)
    return handle_callback_search_params(
        query_params,
        expected_state=state or '',
        require_state=bool(state),
        shop=shop,
        client_secret=client_secret,
        client_id=client_id,
        token_mode=token_mode,
    )


if __name__ == '__main__':
    main()
