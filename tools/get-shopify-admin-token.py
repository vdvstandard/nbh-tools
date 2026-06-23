#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


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


def urlencode_form(data):
    return urllib.parse.urlencode(data).encode('utf-8')


def http_post(url, body, headers=None, timeout=30):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status


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


def print_usage_and_exit(parser, message=None):
    parser.print_help()
    if message:
        raise SystemExit(message)
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description='Request a Shopify Admin token using client_credentials.')
    parser.add_argument('--write-env', action='store_true', help='Store the returned token in .env.')
    parser.add_argument('--print-token', action='store_true', help='Print the full token.')
    parser.add_argument('--shop', help='Store domain.')
    parser.add_argument('--api-key', help='Shopify app client ID/API key.')
    parser.add_argument('--api-secret', help='Shopify app client secret.')
    parser.add_argument('--env-file', help='Path to .env. Default: ../.env relative to this script.')

    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file or project_root / '.env')
    load_dotenv(env_path)

    shop = normalize_shop(args.shop or os.environ.get('SHOPIFY_SHOP') or os.environ.get('SHOPIFY_STORE_DOMAIN') or os.environ.get('SHOPIFY_STORE'))
    client_id = args.api_key or os.environ.get('SHOPIFY_API_KEY') or os.environ.get('SHOPIFY_CLIENT_ID')
    client_secret = args.api_secret or os.environ.get('SHOPIFY_API_SECRET') or os.environ.get('SHOPIFY_CLIENT_SECRET')

    if not shop or not client_id or not client_secret:
        print_usage_and_exit(parser, 'Missing credentials. Set SHOPIFY_SHOP, SHOPIFY_API_KEY, and SHOPIFY_API_SECRET.')

    token = get_client_credentials_token(shop, client_id, client_secret)
    refreshed_at = datetime.utcnow() if 'datetime' in globals() else None
    expires_at = None
    if token.get('expires_in') is not None:
        expires_at = datetime.utcnow() + timedelta(seconds=int(token['expires_in']))

    print('Shopify Admin token acquired with client_credentials.')
    print(f'Shop: {shop}')
    print(f"Scope: {token.get('scope', '(not returned)')}")
    print(f"Expires at: {expires_at.isoformat() + 'Z' if expires_at else '(not returned)'}")
    print(f"Access token: {token['access_token'] if args.print_token else mask_secret(token['access_token'])}")

    if args.write_env:
        upsert_dotenv(env_path, {
            'SHOPIFY_ADMIN_ACCESS_TOKEN': token['access_token'],
            'SHOPIFY_ADMIN_ACCESS_TOKEN_SCOPE': token.get('scope', ''),
            'SHOPIFY_ADMIN_ACCESS_TOKEN_REFRESHED_AT': datetime.utcnow().isoformat() + 'Z',
            'SHOPIFY_ADMIN_ACCESS_TOKEN_EXPIRES_AT': expires_at.isoformat() + 'Z' if expires_at else '',
        })
        print(f'Updated {env_path}')
    else:
        print('No token was written to .env.')


def mask_secret(value):
    value = str(value or '')
    if len(value) <= 10:
        return '***'
    return f'{value[:6]}...{value[-4:]}'


if __name__ == '__main__':
    from datetime import datetime, timedelta
    main()
