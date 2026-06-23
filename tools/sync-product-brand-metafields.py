#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

DEFAULT_API_VERSION = '2026-04'
DEFAULT_NAMESPACE = 'custom'
DEFAULT_KEY = 'brands'
MAX_METAFIELDS_PER_MUTATION = 25


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


def parse_env_value(value):
    trimmed = value.strip()
    if (trimmed.startswith('"') and trimmed.endswith('"')) or (trimmed.startswith("'") and trimmed.endswith("'")):
        return trimmed[1:-1]
    return trimmed


def normalize_shop(value):
    if not value:
        return ''
    cleaned = value.strip().replace('http://', '').replace('https://', '').split('/')[0]
    return cleaned if '.' in cleaned else f'{cleaned}.myshopify.com'


def urlencode_form(data):
    return urllib.parse.urlencode(data).encode('utf-8')


def http_post(url, body, headers=None, timeout=30):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status


def http_post_json(url, payload, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers or {},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status, dict(response.getheaders())


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


def format_env_value(value):
    string_value = str(value or '')
    if re.match(r'^[A-Za-z0-9_./:@,-]*$', string_value):
        return string_value
    return json.dumps(string_value)


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


def shopify_graphql(endpoint, token, query, variables):
    for attempt in range(1, 5):
        response_text, status, headers = http_post_json(
            endpoint,
            {'query': query, 'variables': variables},
            headers={
                'Content-Type': 'application/json',
                'X-Shopify-Access-Token': token,
            },
        )
        payload = json.loads(response_text or '{}')
        retry_after = int(headers.get('Retry-After', '0') or '0')

        if status == 429 or status >= 500:
            if attempt < 4:
                time.sleep(retry_after or attempt)
                continue

        if status < 200 or status >= 300:
            raise RuntimeError(f'Shopify API request failed with HTTP {status}: {json.dumps(payload)}')

        if payload.get('errors'):
            raise RuntimeError('Shopify GraphQL errors: ' + '; '.join(error.get('message', '') for error in payload['errors']))

        wait_for_throttle_budget(payload.get('extensions', {}).get('cost', {}).get('throttleStatus'))
        return payload.get('data', {})

    raise RuntimeError('Shopify API request failed after retries.')


def wait_for_throttle_budget(throttle_status):
    if not throttle_status:
        return
    currently_available = float(throttle_status.get('currentlyAvailable', 0))
    restore_rate = float(throttle_status.get('restoreRate', 0))
    if currently_available < 100 and restore_rate > 0:
        wait_ms = int(((100 - currently_available) / restore_rate) * 1000)
        time.sleep(max(wait_ms / 1000.0, 0.25))


def print_sample(updates, metafield_type):
    if not updates:
        return
    sample = updates[:15]
    print('\nSample changes:')
    for item in sample:
        product = item['product']
        current_value = item['currentValue']
        desired_value = item['desiredValue']
        print(f"- {product['title']} ({product['handle']}): {format_value(current_value)} -> {format_value(desired_value)}")
    if len(updates) > len(sample):
        print(f'...and {len(updates) - len(sample)} more.')
    if metafield_type.startswith('list.'):
        print('List metafield values are written as JSON arrays.')


def format_value(value):
    return '(blank)' if value == '' else json.dumps(value)


def build_plan(products, metafield_type):
    plan = {'correct': [], 'updates': [], 'skippedBlankVendor': []}
    for product in products:
        vendor = (product.get('vendor') or '').strip()
        if not vendor:
            plan['skippedBlankVendor'].append(product)
            continue
        desired_value = metafield_value_for_vendor(vendor, metafield_type)
        current_value = product.get('metafield', {}).get('value', '')
        if current_value == desired_value:
            plan['correct'].append(product)
            continue
        plan['updates'].append({'product': product, 'currentValue': current_value, 'desiredValue': desired_value})
    return plan


def metafield_value_for_vendor(vendor, metafield_type):
    if metafield_type.startswith('list.'):
        return json.dumps([vendor])
    if metafield_type == 'json':
        return json.dumps(vendor)
    return vendor


def validate_supported_type(metafield_type):
    supported_types = {
        'single_line_text_field',
        'multi_line_text_field',
        'list.single_line_text_field',
        'json',
    }
    if metafield_type not in supported_types:
        raise RuntimeError(f'{metafield_type} is not supported. This script only safely syncs text/list text/json metafields to vendor names.')


def get_definition_type(endpoint, token, namespace, key):
    query = '''
    query BrandMetafieldDefinition($namespace: String!, $key: String!) {
      metafieldDefinitions(
        ownerType: PRODUCT
        namespace: $namespace
        key: $key
        first: 1
      ) {
        nodes {
          name
          namespace
          key
          type {
            name
          }
        }
      }
    }
  '''
    data = shopify_graphql(endpoint, token, query, {'namespace': namespace, 'key': key})
    return data.get('metafieldDefinitions', {}).get('nodes', [{}])[0].get('type', {}).get('name', '')


def get_products(endpoint, token, namespace, key, page_size, limit):
    products = []
    after = None
    while True:
        first = page_size if limit is None else min(page_size, max(limit - len(products), 0))
        if first == 0:
            break
        query = '''
    query ProductsForBrandSync(
      $first: Int!
      $after: String
      $namespace: String!
      $key: String!
    ) {
      products(first: $first, after: $after, sortKey: ID) {
        nodes {
          id
          title
          handle
          vendor
          metafield(namespace: $namespace, key: $key) {
            id
            type
            value
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  '''
        data = shopify_graphql(endpoint, token, query, {'first': first, 'after': after, 'namespace': namespace, 'key': key})
        products.extend(data.get('products', {}).get('nodes', []))
        page_info = data.get('products', {}).get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
        if limit is not None and len(products) >= limit:
            break
    return products


def main():
    parser = argparse.ArgumentParser(description='Sync product brand metafields from Shopify vendor values.')
    parser.add_argument('--apply', action='store_true', help='Apply changes to Shopify.')
    parser.add_argument('--shop', help='Shopify store domain.')
    parser.add_argument('--token', help='Admin access token.')
    parser.add_argument('--api-key', help='Shopify app client ID/API key.')
    parser.add_argument('--api-secret', help='Shopify app client secret.')
    parser.add_argument('--api-version', default=DEFAULT_API_VERSION, help='Shopify Admin API version.')
    parser.add_argument('--namespace', default=DEFAULT_NAMESPACE, help='Metafield namespace.')
    parser.add_argument('--key', default=DEFAULT_KEY, help='Metafield key.')
    parser.add_argument('--type', help='Explicit metafield type override.')
    parser.add_argument('--limit', type=int, help='Limit number of products to scan.')
    parser.add_argument('--page-size', type=int, default=100, help='Product page size (1-250).')
    parser.add_argument('--env-file', help='Path to .env. Default: ../.env relative to this script.')

    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    env_path = Path(args.env_file or project_root / '.env')
    load_dotenv(env_path)

    shop = normalize_shop(args.shop or os.environ.get('SHOPIFY_SHOP') or os.environ.get('SHOPIFY_STORE_DOMAIN') or os.environ.get('SHOPIFY_STORE'))
    token = args.token or os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN') or os.environ.get('SHOPIFY_ACCESS_TOKEN') or os.environ.get('SHOPIFY_ADMIN_API_PASSWORD') or os.environ.get('SHOPIFY_API_PASSWORD')
    client_id = args.api_key or os.environ.get('SHOPIFY_API_KEY') or os.environ.get('SHOPIFY_CLIENT_ID')
    client_secret = args.api_secret or os.environ.get('SHOPIFY_API_SECRET') or os.environ.get('SHOPIFY_CLIENT_SECRET')
    api_version = args.api_version or os.environ.get('SHOPIFY_API_VERSION') or DEFAULT_API_VERSION
    namespace = args.namespace or os.environ.get('METAFIELD_NAMESPACE') or DEFAULT_NAMESPACE
    key = args.key or os.environ.get('METAFIELD_KEY') or DEFAULT_KEY
    explicit_type = args.type or os.environ.get('METAFIELD_TYPE') or ''
    limit = args.limit
    page_size = args.page_size

    if not shop or (not token and (not client_id or not client_secret)):
        parser.print_help()
        raise SystemExit('Missing Shopify credentials. Set SHOPIFY_SHOP and either SHOPIFY_ADMIN_ACCESS_TOKEN or SHOPIFY_API_KEY/SHOPIFY_API_SECRET.')
    if page_size < 1 or page_size > 250:
        raise SystemExit('--page-size must be an integer from 1 to 250.')
    if limit is not None and limit < 1:
        raise SystemExit('--limit must be a positive integer.')

    endpoint = f'https://{shop}/admin/api/{api_version}/graphql.json'

    if not token:
        print('Getting temporary Shopify Admin token with client_credentials.')
        token_response = get_client_credentials_token(shop, client_id, client_secret)
        token = token_response['access_token']
        print(f"Temporary token acquired. Scope: {token_response.get('scope', '(not returned)')}. Not stored in .env.")

    print(f"{ 'Apply' if args.apply else 'Dry run' }: syncing {namespace}.{key} to product vendor on {shop}")

    definition_type = get_definition_type(endpoint, token, namespace, key)
    products = get_products(endpoint, token, namespace, key, page_size, limit)
    existing_type = next((p.get('metafield', {}).get('type') for p in products if p.get('metafield', {}).get('type')), '')
    metafield_type = definition_type or existing_type or explicit_type

    if not metafield_type:
        raise RuntimeError(f'Could not determine the metafield type for {namespace}.{key}. Add a Shopify metafield definition or pass --type.')

    validate_supported_type(metafield_type)
    plan = build_plan(products, metafield_type)

    print(f'Metafield type: {metafield_type}')
    if not definition_type and existing_type:
        print('Type source: existing product metafield value.')
    elif not definition_type and explicit_type:
        print('Type source: --type/env override.')
    else:
        print('Type source: Shopify metafield definition.')

    print(f'Products scanned: {len(products)}')
    print(f'Already correct: {len(plan['correct'])}')
    print(f'Needs update: {len(plan['updates'])}')
    print(f'Skipped without vendor: {len(plan['skippedBlankVendor'])}')

    print_sample(plan['updates'], metafield_type)

    if not args.apply:
        print('\nNo changes were written. Re-run with --apply to update Shopify.')
        return

    if not plan['updates']:
        print('\nNothing to update.')
        return

    updated = 0
    for index in range(0, len(plan['updates']), MAX_METAFIELDS_PER_MUTATION):
        batch = plan['updates'][index:index + MAX_METAFIELDS_PER_MUTATION]
        metafields = [
            {
                'ownerId': item['product']['id'],
                'namespace': namespace,
                'key': key,
                'type': metafield_type,
                'value': item['desiredValue'],
            }
            for item in batch
        ]
        result = shopify_graphql(endpoint, token, '''
    mutation SetBrandMetafields($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields {
          id
          ownerType
          namespace
          key
          type
          value
          updatedAt
        }
        userErrors {
          field
          message
          code
        }
      }
    }
  ''', {'metafields': metafields})
        user_errors = result.get('metafieldsSet', {}).get('userErrors', [])
        if user_errors:
            details = '\n'.join(f"{error.get('code', 'ERROR')} {'.'.join(error.get('field', [])) if error.get('field') else ''}: {error.get('message')}" for error in user_errors)
            raise RuntimeError(f'Shopify rejected a metafieldsSet batch:\n{details}')
        updated += len(result.get('metafieldsSet', {}).get('metafields', []))
        print(f'Updated {updated}/{len(plan['updates'])}')
        time.sleep(0.25)

    print(f'\nDone. Updated {updated} product metafields.')


if __name__ == '__main__':
    main()
