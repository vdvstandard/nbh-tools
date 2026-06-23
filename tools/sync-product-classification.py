#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_API_VERSION = '2026-04'
DEFAULT_REVIEW_FILE = Path('.tmp') / 'product-classification-review.json'
COLOR_NAMESPACE = 'shopify'
COLOR_KEY = 'color-pattern'
COLOR_METAFIELD_TYPE = 'list.metaobject_reference'
MAX_METAFIELDS_PER_MUTATION = 25
CATEGORY_CHUNK_SIZE = 100


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
    if not cleaned:
        return ''
    return cleaned if '.' in cleaned else f'{cleaned}.myshopify.com'


def urlencode_form(data):
    return urllib.parse.urlencode(data).encode('utf-8')


def http_post(url, body, headers=None, timeout=30):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status, dict(response.getheaders())


def http_post_json(url, payload, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers or {},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8'), response.status, dict(response.getheaders())


def strip_bom(value):
    return value[1:] if value and value[0] == '\ufeff' else value


def is_metaobject_gid(value):
    return isinstance(value, str) and value.startswith('gid://shopify/Metaobject/')


def normalize_optional_string(value):
    return str(value or '').strip()


def normalize_lookup_key(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower().replace('&', ' and '))).strip()


def unique(values):
    return list(dict.fromkeys(values))


def same_string_set(left, right):
    return set(left) == set(right)


def increment(object_, key):
    object_[key] = object_.get(key, 0) + 1


def delay(seconds):
    time.sleep(seconds)


def get_client_credentials_token(shop, client_id, client_secret):
    body = urlencode_form({
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
    })
    response_text, status, _ = http_post(
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
        try:
            payload = json.loads(response_text or '{}')
        except json.JSONDecodeError:
            payload = {'raw': response_text}

        retry_after = int(headers.get('Retry-After', '0') or '0')

        if status == 429 or status >= 500:
            if attempt < 4:
                delay(retry_after or attempt)
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
        delay(max(wait_ms / 1000.0, 0.25))


def print_validation(validation):
    print(f'Warnings: {len(validation["warnings"]) }')
    for warning in validation['warnings'][:20]:
        print(f'- WARNING: {warning}')
    if len(validation['warnings']) > 20:
        print(f'...and {len(validation["warnings"]) - 20} more warnings.')
    print(f'Errors: {len(validation["errors"]) }')
    for error in validation['errors'][:30]:
        print(f'- ERROR: {error}')
    if len(validation['errors']) > 30:
        print(f'...and {len(validation["errors"]) - 30} more errors.')
    if validation['colorResolver']['available']:
        print(f'Color references available: {len(validation["colorResolver"]["references"]) }')


def build_review_document(products, shop, api_version):
    items = [build_review_item(product) for product in products]
    summary = summarize_review_items(items)
    return {
        'schemaVersion': 1,
        'generatedAt': datetime.utcnow().isoformat() + 'Z',
        'shop': shop,
        'apiVersion': api_version,
        'mode': 'review-first',
        'instructions': {
            'reviewStatus': 'Set to "approved" to write the proposed category/color, "skip" to ignore, or leave "needs_review".',
            'colorPattern': 'Use proposedColor.names for Shopify-native shopify.color-pattern. If native resolution fails, fill proposedColor.refIds with Shopify metaobject GIDs.',
            'existingData': 'Rows with existing specific category or existing color are not auto-approved. Only approved rows are written.',
        },
        'summary': summary,
        'items': items,
    }


def build_review_item(product):
    category = normalize_category(product.get('category'))
    color_pattern = normalize_color_pattern(product.get('metafield'))
    category_suggestion = suggest_category(product)
    color_suggestion = suggest_colors(product)
    has_specific_category = is_specific_category(category)
    has_color_pattern = bool(color_pattern['refIds'] or color_pattern['value'])
    review_status = 'skip' if has_specific_category and has_color_pattern else 'needs_review'
    return {
        'reviewStatus': review_status,
        'product': {
            'id': product.get('id', ''),
            'legacyResourceId': product.get('legacyResourceId', ''),
            'title': product.get('title', ''),
            'handle': product.get('handle', ''),
            'vendor': product.get('vendor', ''),
            'productType': product.get('productType', ''),
            'tags': product.get('tags', []),
            'options': [
                {
                    'name': option.get('name', ''),
                    'position': option.get('position'),
                    'values': option.get('values', []),
                    'linkedMetafield': option.get('linkedMetafield') or None,
                }
                for option in product.get('options', [])
            ],
        },
        'current': {
            'category': category,
            'colorPattern': color_pattern,
        },
        'proposedCategory': {
            'id': category['id'] if has_specific_category else (category_suggestion.get('id') if category_suggestion else ''),
            'name': category['fullName'] if has_specific_category else (category_suggestion.get('fullName') if category_suggestion else ''),
            'confidence': 1 if has_specific_category else (category_suggestion.get('confidence', 0) if category_suggestion else 0),
            'reason': 'Existing specific Shopify category; left untouched unless approved.' if has_specific_category else (category_suggestion.get('reason', 'No confident category suggestion.') if category_suggestion else 'No confident category suggestion.'),
        },
        'proposedColor': {
            'names': [] if has_color_pattern else color_suggestion['names'],
            'refIds': [],
            'confidence': 1 if has_color_pattern else color_suggestion['confidence'],
            'reason': 'Existing Shopify color-pattern; left untouched unless approved.' if has_color_pattern else color_suggestion['reason'],
        },
        'reviewNotes': '',
    }


def summarize_review_items(items):
    summary = {
        'total': len(items),
        'needsCategoryReview': 0,
        'needsColorReview': 0,
        'hasSpecificCategory': 0,
        'hasColorPattern': 0,
        'suggestedCategories': {},
        'suggestedColors': {},
        'reviewStatus': {},
    }
    for item in items:
        category = item['current']['category']
        color_pattern = item['current']['colorPattern']
        if is_specific_category(category):
            summary['hasSpecificCategory'] += 1
        else:
            summary['needsCategoryReview'] += 1
        if color_pattern['refIds'] or color_pattern['value']:
            summary['hasColorPattern'] += 1
        else:
            summary['needsColorReview'] += 1
        increment(summary['reviewStatus'], item['reviewStatus'])
        if item['proposedCategory']['id']:
            increment(summary['suggestedCategories'], item['proposedCategory']['name'] or item['proposedCategory']['id'])
        for color_name in item['proposedColor'].get('names', []):
            increment(summary['suggestedColors'], color_name)
    return summary


def suggest_category(product):
    haystack = product_search_text(product)
    for rule in CATEGORY_RULES:
        if any(pattern.search(haystack) for pattern in rule['patterns']):
            return {
                'id': rule['id'],
                'fullName': rule['fullName'],
                'confidence': rule['confidence'],
                'reason': f"Matched {rule['label']} in title/handle/type/tags.",
            }
    return None


def suggest_colors(product):
    haystack = color_search_text(product)
    matches = []
    for rule in COLOR_RULES:
        matched = None
        for pattern in rule['patterns']:
            match = pattern.search(haystack)
            if match:
                index = match.start()
                if matched is None or index < matched['index']:
                    matched = {'index': index, 'pattern': pattern}
        if matched is not None:
            matches.append({'name': rule['name'], 'confidence': rule['confidence'], 'index': matched['index'], 'reason': f"Matched {rule['name'].lower()} in title/handle/type/tags."})
    matches.sort(key=lambda item: (item['index'], -item['confidence']))
    unique_matches = []
    for match in matches:
        if not any(normalize_lookup_key(existing['name']) == normalize_lookup_key(match['name']) for existing in unique_matches):
            unique_matches.append(match)
    if not unique_matches:
        return {'names': [], 'confidence': 0, 'reason': 'No color term found in title/handle/type/tags.'}
    return {
        'names': [match['name'] for match in unique_matches[:3]],
        'confidence': min(match['confidence'] for match in unique_matches[:3]),
        'reason': ' '.join(match['reason'] for match in unique_matches[:3]),
    }


def first_match_index(text, patterns):
    best = -1
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            index = match.start()
            if best == -1 or index < best:
                best = index
    return best


def product_search_text(product):
    parts = [
        product.get('title', ''),
        product.get('handle', ''),
        product.get('vendor', ''),
        product.get('productType', ''),
    ]
    parts += [tag for tag in product.get('tags', []) if tag]
    for option in product.get('options', []):
        parts.append(option.get('name', ''))
        parts.extend(option.get('values', []) or [])
    return re.sub(r'[_/]+', ' ', ' '.join(part for part in parts if part).lower())


def color_search_text(product):
    parts = [
        product.get('title', ''),
        product.get('handle', ''),
        product.get('productType', ''),
    ]
    parts += [tag for tag in product.get('tags', []) if tag]
    for option in product.get('options', []):
        parts.append(option.get('name', ''))
        parts.extend(option.get('values', []) or [])
    text = re.sub(r'[_/]+', ' ', ' '.join(part for part in parts if part).lower())
    text = re.sub(r'\bred[-\s]+wing\b', ' ', text)
    text = re.sub(r'\bred[-\s]+listed\b', ' ', text)
    text = re.sub(r'\ball[-\s]+natural\b', ' ', text)
    return text


def validate_approved_items(items):
    errors = []
    warnings = []
    categories_by_id = validate_categories(items, errors, warnings)
    color_resolver = build_color_resolver_for_items(items, errors, warnings)

    for item in items:
        label = item_label(item)
        if not item.get('product', {}).get('id'):
            errors.append(f'{label}: missing product.id.')
        category_id = normalize_optional_string(item.get('proposedCategory', {}).get('id'))
        if category_id and category_id not in categories_by_id:
            errors.append(f'{label}: proposedCategory.id is not a valid taxonomy category: {category_id}')
        color_names = normalize_color_names(item.get('proposedColor', {}))
        color_ref_ids = normalize_color_ref_ids(item.get('proposedColor', {}))
        if color_names and color_ref_ids and len(color_names) != len(color_ref_ids):
            warnings.append(f'{label}: proposedColor has both names and refIds with different counts; refIds will take precedence.')
        if color_names and not color_ref_ids and color_resolver['available']:
            for color_name in color_names:
                if not color_resolver['resolve'](color_name):
                    errors.append(f'{label}: could not resolve Shopify-native color "{color_name}".')
    return {'errors': errors, 'warnings': warnings, 'categoriesById': categories_by_id, 'colorResolver': color_resolver}


def validate_categories(items, errors, warnings):
    ids = unique([normalize_optional_string(item.get('proposedCategory', {}).get('id')) for item in items if normalize_optional_string(item.get('proposedCategory', {}).get('id'))])
    if not ids:
        return {}
    categories_by_id = {}
    for index in range(0, len(ids), CATEGORY_CHUNK_SIZE):
        batch = ids[index:index + CATEGORY_CHUNK_SIZE]
        data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['taxonomyCategoriesByIds'], {'ids': batch})
        for node in data.get('nodes', []):
            if node and node.get('__typename') == 'TaxonomyCategory':
                categories_by_id[node['id']] = node
                if node.get('isArchived'):
                    warnings.append(f"{node['id']} ({node.get('fullName')}) is archived.")
                if not node.get('isLeaf'):
                    warnings.append(f"{node['id']} ({node.get('fullName')}) is not a leaf category.")
    for id_ in ids:
        if id_ not in categories_by_id:
            errors.append(f'Unknown taxonomy category id: {id_}')
    return categories_by_id


def build_color_resolver_for_items(items, errors, warnings):
    needs_resolver = any(normalize_color_names(item.get('proposedColor', {})) and not normalize_color_ref_ids(item.get('proposedColor', {})) for item in items)
    if not needs_resolver:
        return create_color_resolver([])
    try:
        references = get_color_references()
        resolver = create_color_resolver(references)
        if not resolver['available']:
            errors.append('Could not find Shopify color-pattern metaobjects. Fill proposedColor.refIds manually or add read_metaobjects/read_metaobject_definitions scopes.')
        return resolver
    except Exception as error:
        errors.append(f'Could not resolve Shopify-native colors: {error}. The app likely needs read_metaobjects and read_metaobject_definitions scopes, or proposedColor.refIds must be filled manually.')
        warnings.append('Category validation may still be valid, but color apply is blocked until color references are resolvable.')
        return create_color_resolver([])


def get_color_references():
    definition = get_color_metafield_definition()
    definition_ids = color_definition_ids_from_validations(definition.get('validations', []))
    candidates = []
    for definition_id in definition_ids:
        candidates.extend(get_metaobjects_by_definition_id(definition_id))
    if candidates:
        return candidates
    metaobject_definitions = get_metaobject_definitions()
    color_definitions = [definition for definition in metaobject_definitions if is_color_metaobject_definition(definition)]
    for color_definition in color_definitions:
        candidates.extend(get_metaobjects_by_definition_id(color_definition['id']))
    if candidates:
        return candidates
    for type_ in COLOR_METAOBJECT_TYPE_CANDIDATES:
        candidates.extend(get_metaobjects_by_type(type_))
    return candidates


def get_color_metafield_definition():
    data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['colorMetafieldDefinition'], {})
    return data.get('metafieldDefinitions', {}).get('nodes', [{}])[0]


def get_metaobject_definitions():
    definitions = []
    after = None
    while True:
        data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['metaobjectDefinitions'], {'first': 250, 'after': after})
        definitions.extend(data.get('metaobjectDefinitions', {}).get('nodes', []))
        page_info = data.get('metaobjectDefinitions', {}).get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
    return definitions


def get_metaobjects_by_definition_id(id_):
    metaobjects = []
    after = None
    while True:
        data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['metaobjectsByDefinitionId'], {'id': id_, 'first': 250, 'after': after})
        definition = data.get('metaobjectDefinition')
        if not definition:
            break
        metaobjects.extend(definition.get('metaobjects', {}).get('nodes', []))
        page_info = definition.get('metaobjects', {}).get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
    return metaobjects


def get_metaobjects_by_type(type_):
    metaobjects = []
    after = None
    while True:
        data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['metaobjectsByType'], {'type': type_, 'first': 250, 'after': after})
        metaobjects.extend(data.get('metaobjects', {}).get('nodes', []))
        page_info = data.get('metaobjects', {}).get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
    return metaobjects


def create_color_resolver(metaobjects):
    by_name = {}
    references = []
    for metaobject in metaobjects:
        labels = color_labels_for_metaobject(metaobject)
        if not labels:
            continue
        reference = {
            'id': metaobject.get('id'),
            'handle': metaobject.get('handle'),
            'type': metaobject.get('type'),
            'displayName': metaobject.get('displayName'),
            'labels': labels,
        }
        references.append(reference)
        for label in labels:
            key = normalize_lookup_key(label)
            if key and key not in by_name:
                by_name[key] = reference
    def resolve(name):
        key = normalize_lookup_key(name)
        if key in by_name:
            return by_name[key]
        alias = COLOR_ALIASES.get(key)
        if alias:
            return by_name.get(normalize_lookup_key(alias))
        return None
    return {'available': bool(references), 'references': references, 'resolve': resolve}


def color_labels_for_metaobject(metaobject):
    labels = [metaobject.get('displayName') or '', metaobject.get('handle') or '']
    for field in metaobject.get('fields', []) or []:
        if not field:
            continue
        if isinstance(field.get('value'), str):
            labels.append(field['value'])
        if isinstance(field.get('jsonValue'), str):
            labels.append(field['jsonValue'])
        if isinstance(field.get('jsonValue'), list):
            labels.extend([value for value in field['jsonValue'] if isinstance(value, str)])
    return unique([label for label in labels if label])


def color_definition_ids_from_validations(validations):
    ids = []
    for validation in validations:
        value = validation.get('value', '') if validation else ''
        if 'gid://shopify/MetaobjectDefinition/' in value:
            ids.append(value)
            continue
        try:
            parsed = json.loads(value)
            values = parsed if isinstance(parsed, list) else list(parsed.values())
            for candidate in values:
                if isinstance(candidate, str) and 'gid://shopify/MetaobjectDefinition/' in candidate:
                    ids.append(candidate)
        except Exception:
            pass
    return unique(ids)


def is_color_metaobject_definition(definition):
    text = ' '.join(
        str(part or '')
        for part in [
            definition.get('name'),
            definition.get('type'),
            definition.get('displayNameKey'),
            *[field.get('name') for field in definition.get('fieldDefinitions', []) or []],
            *[field.get('key') for field in definition.get('fieldDefinitions', []) or []],
        ]
        if part is not None
    ).lower()
    return 'color' in text or 'colour' in text or 'pattern' in text


def build_category_updates(items, categories_by_id):
    updates = []
    for item in items:
        category_id = normalize_optional_string(item.get('proposedCategory', {}).get('id'))
        if not category_id:
            continue
        current_category = item.get('current', {}).get('category', {})
        if not FORCE_EXISTING and is_specific_category(current_category) and current_category.get('id') != category_id:
            continue
        if current_category.get('id') == category_id:
            continue
        updates.append({
            'productId': item['product']['id'],
            'title': item['product']['title'],
            'categoryId': category_id,
            'categoryName': categories_by_id.get(category_id, {}).get('fullName') or item.get('proposedCategory', {}).get('name') or category_id,
        })
    return updates


def build_color_updates(items, color_resolver):
    updates = []
    for item in items:
        color_ref_ids = normalize_color_ref_ids(item.get('proposedColor', {}))
        color_names = normalize_color_names(item.get('proposedColor', {}))
        current_ref_ids = item.get('current', {}).get('colorPattern', {}).get('refIds', [])
        desired_ref_ids = color_ref_ids or [color_resolver['resolve'](name)['id'] for name in color_names if color_resolver['resolve'](name)]
        if not desired_ref_ids:
            continue
        if not FORCE_EXISTING and current_ref_ids and not same_string_set(current_ref_ids, desired_ref_ids):
            continue
        if same_string_set(current_ref_ids, desired_ref_ids):
            continue
        updates.append({
            'productId': item['product']['id'],
            'title': item['product']['title'],
            'refIds': desired_ref_ids,
        })
    return updates


def select_approved_items(items):
    return [item for item in items if normalize_optional_string(item.get('reviewStatus')) == 'approved']


def read_review_document(review_file):
    path = Path(review_file)
    if not path.exists():
        raise RuntimeError(f'Review file does not exist: {review_file}. Run --export-review first.')
    review = json.loads(strip_bom(path.read_text(encoding='utf-8')))
    if not isinstance(review.get('items'), list):
        raise RuntimeError(f'Invalid review file: expected an items array in {review_file}.')
    return review


def get_products(page_size, limit):
    products = []
    after = None
    while True:
        first = page_size if limit is None else min(page_size, max(limit - len(products), 0))
        if first == 0:
            break
        data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['products'], {'first': first, 'after': after})
        products.extend(data.get('products', {}).get('nodes', []))
        page_info = data.get('products', {}).get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        after = page_info.get('endCursor')
        if limit is not None and len(products) >= limit:
            break
    return products


def normalize_category(category):
    if not category:
        return {'id': '', 'fullName': '', 'isLeaf': False, 'isArchived': False}
    return {
        'id': category.get('id', ''),
        'fullName': category.get('fullName', ''),
        'isLeaf': bool(category.get('isLeaf')),
        'isArchived': bool(category.get('isArchived')),
    }


def normalize_color_pattern(metafield):
    value = metafield.get('value', '') if metafield else ''
    return {'id': metafield.get('id', '') if metafield else '', 'type': metafield.get('type', '') if metafield else '', 'value': value, 'refIds': parse_reference_ids(value)}


def parse_reference_ids(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [item for item in parsed if is_metaobject_gid(item)]
        if is_metaobject_gid(parsed):
            return [parsed]
    except Exception:
        if is_metaobject_gid(value):
            return [value]
    return []


def normalize_color_names(proposed_color):
    if not proposed_color:
        return []
    if isinstance(proposed_color.get('names'), list):
        return unique([normalize_optional_string(name) for name in proposed_color['names'] if normalize_optional_string(name)])
    if proposed_color.get('name'):
        return [normalize_optional_string(proposed_color['name'])]
    return []


def normalize_color_ref_ids(proposed_color):
    if not proposed_color:
        return []
    if isinstance(proposed_color.get('refIds'), list):
        return unique([normalize_optional_string(ref_id) for ref_id in proposed_color['refIds'] if normalize_optional_string(ref_id)])
    if proposed_color.get('refId'):
        return [normalize_optional_string(proposed_color['refId'])]
    return []


def is_specific_category(category):
    return bool(category and category.get('id') and category['id'] != 'gid://shopify/TaxonomyCategory/na')


def item_label(item):
    product = item.get('product', {})
    return f"{product.get('title', '(untitled)')} ({product.get('handle') or product.get('id') or 'unknown'})"


def print_sample(updates, metafield_type):
    if not updates:
        return
    sample = updates[:15]
    print('\nSample changes:')
    for item in sample:
        product = item['product']
        print(f"- {product['title']} ({product['handle']}): {format_value(item['currentValue'])} -> {format_value(item['desiredValue'])}")
    if len(updates) > len(sample):
        print(f'...and {len(updates) - len(sample)} more.')
    if metafield_type.startswith('list.'):
        print('List metafield values are written as JSON arrays.')


def format_value(value):
    return '(blank)' if value == '' else json.dumps(value)


def print_usage():
    usage = '''Usage:
  python tools/sync-product-classification.py --export-review
  python tools/sync-product-classification.py --validate-review
  python tools/sync-product-classification.py --apply
  python tools/sync-product-classification.py --debug-colors

Modes:
  --export-review         Export .tmp/product-classification-review.json with suggestions.
  --validate-review       Validate approved rows without writing Shopify changes.
  --apply                 Apply only rows with reviewStatus "approved".
  --debug-colors          Inspect color-pattern definitions/references without writing.

Options:
  --review-file <path>    Review JSON path. Default: .tmp/product-classification-review.json.
  --limit <number>        Export/apply only the first N products or approved rows.
  --page-size <number>    Product page size, 1-250. Default: 100.
  --force-existing        Allow approved rows to overwrite existing specific category/color.
  --shop <domain>         Store domain, for example neighbourhood-arnhem.myshopify.com.
  --api-version <value>   Shopify Admin API version. Default: 2026-04.
  --token <value>         Admin access token. If omitted, client_credentials is used.
  --api-key <value>       Shopify app client ID/API key.
  --api-secret <value>    Shopify app client secret.

Environment:
  SHOPIFY_SHOP, SHOPIFY_API_KEY, SHOPIFY_API_SECRET.
  If no Admin token is set, SHOPIFY_API_KEY and SHOPIFY_API_SECRET are used to get a temporary 24-hour token with Shopify's client_credentials grant.
  A .env file in the project root is loaded automatically.'''
    print(usage)


def parse_args(raw_args):
    parsed = {}
    index = 0
    while index < len(raw_args):
        arg = raw_args[index]
        if arg == '--export-review':
            parsed['exportReview'] = True
        elif arg == '--validate-review':
            parsed['validateReview'] = True
        elif arg == '--apply':
            parsed['apply'] = True
        elif arg == '--debug-colors':
            parsed['debugColors'] = True
        elif arg == '--force-existing':
            parsed['forceExisting'] = True
        elif arg == '--help' or arg == '-h':
            parsed['help'] = True
        elif arg.startswith('--'):
            flag, inline_value = arg[2:].split('=', 1) if '=' in arg else (arg[2:], None)
            camel_flag = re.sub(r'-([a-z])', lambda m: m.group(1).upper(), flag)
            if inline_value is None:
                index += 1
                if index >= len(raw_args):
                    raise RuntimeError(f'Missing value for --{flag}')
                value = raw_args[index]
            else:
                value = inline_value
            parsed[camel_flag] = value
        else:
            raise RuntimeError(f'Unknown positional argument: {arg}')
        index += 1
    return parsed


def get_mode(parsed):
    modes = [
        ('exportReview', parsed.get('exportReview')),
        ('validateReview', parsed.get('validateReview')),
        ('apply', parsed.get('apply')),
        ('debugColors', parsed.get('debugColors')),
    ]
    selected = [mode for mode, enabled in modes if enabled]
    if len(selected) != 1:
        raise RuntimeError('Choose exactly one mode: --export-review, --validate-review, --apply, or --debug-colors.')
    return selected[0]


def ensure_token():
    global TOKEN
    if TOKEN:
        return
    print('Getting temporary Shopify Admin token with client_credentials.')
    token_response = get_client_credentials_token(SHOP, API_KEY, API_SECRET)
    TOKEN = token_response['access_token']
    print(f"Temporary token acquired. Scope: {token_response.get('scope', '(not returned)')}. Not stored in .env.")


def export_review(review_file):
    print(f'Exporting classification review for {SHOP}.')
    products = get_products(PAGE_SIZE, LIMIT)
    review = build_review_document(products, SHOP, API_VERSION)
    Path(review_file).parent.mkdir(parents=True, exist_ok=True)
    Path(review_file).write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
    print(f'Products exported: {len(products)}')
    print(f'Needs category review: {review["summary"]["needsCategoryReview"]}')
    print(f'Needs color review: {review["summary"]["needsColorReview"]}')
    print(f'Already has specific category: {review["summary"]["hasSpecificCategory"]}')
    print(f'Already has color pattern: {review["summary"]["hasColorPattern"]}')
    print(f'Review file: {review_file}')
    print('Approve rows by setting reviewStatus to "approved". Use "skip" for rows to ignore.')


def validate_review(review_file):
    print(f'Validating review file for {SHOP}: {review_file}')
    review = read_review_document(review_file)
    approved_items = select_approved_items(review['items'])
    validation = validate_approved_items(approved_items)
    print_validation(validation)
    if validation['errors']:
        raise RuntimeError(f'Review validation failed with {len(validation["errors"])} error(s).')
    print('Review validation passed. No Shopify writes were performed.')


def apply_review(review_file):
    print(f'Applying approved classification rows for {SHOP}: {review_file}')
    review = read_review_document(review_file)
    approved_items = select_approved_items(review['items'])
    selected_items = approved_items if LIMIT is None else approved_items[:LIMIT]
    if not selected_items:
        print('No approved rows to apply.')
        return
    validation = validate_approved_items(selected_items)
    print_validation(validation)
    if validation['errors']:
        raise RuntimeError('Apply stopped because validation failed.')
    category_updates = build_category_updates(selected_items, validation['categoriesById'])
    color_updates = build_color_updates(selected_items, validation['colorResolver'])
    print(f'Approved rows selected: {len(selected_items)}')
    print(f'Category updates planned: {len(category_updates)}')
    print(f'Color updates planned: {len(color_updates)}')
    categories_updated = 0
    for update in category_updates:
        result = shopify_graphql(ENDPOINT, TOKEN, QUERIES['updateProductCategory'], {'product': {'id': update['productId'], 'category': update['categoryId']}})
        user_errors = result.get('productUpdate', {}).get('userErrors', [])
        if user_errors:
            raise RuntimeError(format_user_errors('productUpdate', user_errors))
        categories_updated += 1
        print(f"Category {categories_updated}/{len(category_updates)}: {update['title']} -> {update['categoryName']}")
        delay(0.25)
    colors_updated = 0
    for index in range(0, len(color_updates), MAX_METAFIELDS_PER_MUTATION):
        batch = color_updates[index:index + MAX_METAFIELDS_PER_MUTATION]
        metafields = [
            {
                'ownerId': update['productId'],
                'namespace': COLOR_NAMESPACE,
                'key': COLOR_KEY,
                'type': COLOR_METAFIELD_TYPE,
                'value': json.dumps(update['refIds']),
            }
            for update in batch
        ]
        result = shopify_graphql(ENDPOINT, TOKEN, QUERIES['setMetafields'], {'metafields': metafields})
        user_errors = result.get('metafieldsSet', {}).get('userErrors', [])
        if user_errors:
            raise RuntimeError(format_user_errors('metafieldsSet', user_errors))
        colors_updated += len(result.get('metafieldsSet', {}).get('metafields', []))
        print(f'Colors {colors_updated}/{len(color_updates)}')
        delay(0.25)
    print(f'\nDone. Category updates: {categories_updated}. Color updates: {colors_updated}.')


def debug_colors():
    print(f'Debugging Shopify-native color pattern data for {SHOP}. No writes will be performed.')
    try:
        definitions = get_metaobject_definitions()
        color_definitions = [d for d in definitions if is_color_metaobject_definition(d)]
        print(f'\nMetaobject definitions visible: {len(definitions)}')
        print(f'Color-like metaobject definitions visible: {len(color_definitions)}')
        print(json.dumps(color_definitions[:10], indent=2))
    except Exception as error:
        print(f'\nMetaobject definitions query failed: {error}')
    for type_ in COLOR_METAOBJECT_TYPE_CANDIDATES:
        try:
            metaobjects = get_metaobjects_by_type(type_)
            print(f'\nMetaobjects for type "{type_}": {len(metaobjects)}')
            print(json.dumps(metaobjects[:10], indent=2))
        except Exception as error:
            print(f'\nMetaobjects for type "{type_}" failed: {error}')
    products = get_products(PAGE_SIZE, LIMIT)
    existing_ref_ids = unique([ref_id for product in products for ref_id in parse_reference_ids(product.get('metafield', {}).get('value', '') or '')])
    print(f'\nExisting product color-pattern ref IDs: {len(existing_ref_ids)}')
    print(json.dumps(existing_ref_ids, indent=2))
    if existing_ref_ids:
        try:
            data = shopify_graphql(ENDPOINT, TOKEN, QUERIES['metaobjectNodesByIds'], {'ids': existing_ref_ids})
            print('\nExisting color-pattern metaobjects:')
            print(json.dumps(data.get('nodes', []), indent=2))
        except Exception as error:
            print(f'\nExisting color-pattern metaobject lookup failed: {error}')


def format_user_errors(operation, user_errors):
    details = '\n'.join(
        f"{error.get('code', 'ERROR')} {'.'.join(error.get('field', [])) if error.get('field') else ''}: {error.get('message')}"
        for error in user_errors
    )
    return f'Shopify rejected {operation}:\n{details}'


def main():
    parsed = parse_args(sys.argv[1:])
    global SHOP, TOKEN, API_KEY, API_SECRET, API_VERSION, REVIEW_FILE, LIMIT, PAGE_SIZE, FORCE_EXISTING, ENDPOINT, QUERIES
    SHOP = normalize_shop(parsed.get('shop') or os.environ.get('SHOPIFY_SHOP') or os.environ.get('SHOPIFY_STORE_DOMAIN') or os.environ.get('SHOPIFY_STORE'))
    API_KEY = parsed.get('apiKey') or os.environ.get('SHOPIFY_API_KEY') or os.environ.get('SHOPIFY_CLIENT_ID')
    API_SECRET = parsed.get('apiSecret') or os.environ.get('SHOPIFY_API_SECRET') or os.environ.get('SHOPIFY_CLIENT_SECRET')
    TOKEN = parsed.get('token') or os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN') or os.environ.get('SHOPIFY_ACCESS_TOKEN') or os.environ.get('SHOPIFY_ADMIN_API_PASSWORD') or os.environ.get('SHOPIFY_API_PASSWORD')
    API_VERSION = parsed.get('apiVersion') or os.environ.get('SHOPIFY_API_VERSION') or DEFAULT_API_VERSION
    REVIEW_FILE = parsed.get('reviewFile') or str(DEFAULT_REVIEW_FILE)
    LIMIT = int(parsed.get('limit')) if parsed.get('limit') is not None else None
    PAGE_SIZE = int(parsed.get('pageSize') or 100)
    FORCE_EXISTING = bool(parsed.get('forceExisting'))
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')

    if not SHOP or (not TOKEN and (not API_KEY or not API_SECRET)):
        print_usage()
        raise SystemExit('Missing Shopify credentials. Set SHOPIFY_SHOP and either SHOPIFY_ADMIN_ACCESS_TOKEN or SHOPIFY_API_KEY/SHOPIFY_API_SECRET.')
    if PAGE_SIZE < 1 or PAGE_SIZE > 250:
        raise SystemExit('--page-size must be an integer from 1 to 250.')
    if LIMIT is not None and LIMIT < 1:
        raise SystemExit('--limit must be a positive integer.')

    ENDPOINT = f'https://{SHOP}/admin/api/{API_VERSION}/graphql.json'
    QUERIES = {
        'products': '''
    query ProductsForClassification($first: Int!, $after: String) {
      products(first: $first, after: $after, sortKey: ID) {
        nodes {
          id
          legacyResourceId
          title
          handle
          vendor
          productType
          tags
          category {
            id
            fullName
            isLeaf
            isArchived
          }
          options {
            id
            name
            position
            values
            linkedMetafield {
              namespace
              key
            }
          }
          metafield(namespace: "shopify", key: "color-pattern") {
            id
            namespace
            key
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
  ''',
        'taxonomyCategoriesByIds': '''
    query TaxonomyCategoriesByIds($ids: [ID!]!) {
      nodes(ids: $ids) {
        __typename
        id
        ... on TaxonomyCategory {
          fullName
          isLeaf
          isArchived
        }
      }
    }
  ''',
        'taxonomySearch': '''
    query TaxonomySearch($search: String!) {
      taxonomy {
        categories(first: 10, search: $search) {
          nodes {
            id
            name
            fullName
            isLeaf
            isArchived
          }
        }
      }
    }
  ''',
        'colorMetafieldDefinition': '''
    query ColorMetafieldDefinition {
      metafieldDefinitions(
        ownerType: PRODUCT
        namespace: "shopify"
        key: "color-pattern"
        first: 1
      ) {
        nodes {
          id
          name
          namespace
          key
          type {
            name
          }
          validations {
            name
            value
          }
        }
      }
    }
  ''',
        'metaobjectDefinitions': '''
    query MetaobjectDefinitions($first: Int!, $after: String) {
      metaobjectDefinitions(first: $first, after: $after) {
        nodes {
          id
          type
          name
          displayNameKey
          metaobjectsCount
          fieldDefinitions {
            key
            name
            type {
              name
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  ''',
        'metaobjectsByDefinitionId': '''
    query MetaobjectsByDefinitionId($id: ID!, $first: Int!, $after: String) {
      metaobjectDefinition(id: $id) {
        id
        type
        name
        metaobjects(first: $first, after: $after) {
          nodes {
            id
            handle
            type
            displayName
            fields {
              key
              value
              jsonValue
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
  ''',
        'metaobjectsByType': '''
    query MetaobjectsByType($type: String!, $first: Int!, $after: String) {
      metaobjects(type: $type, first: $first, after: $after) {
        nodes {
          id
          handle
          type
          displayName
          fields {
            key
            value
            jsonValue
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  ''',
        'metaobjectNodesByIds': '''
    query MetaobjectNodesByIds($ids: [ID!]!) {
      nodes(ids: $ids) {
        __typename
        id
        ... on Metaobject {
          handle
          type
          displayName
          fields {
            key
            value
            jsonValue
          }
        }
      }
    }
  ''',
        'updateProductCategory': '''
    mutation UpdateProductCategory($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product {
          id
          title
          category {
            id
            fullName
          }
        }
        userErrors {
          field
          message
        }
      }
    }
  ''',
        'setMetafields': '''
    mutation SetColorPatternMetafields($metafields: [MetafieldsSetInput!]!) {
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
  ''',
    }

    if parsed.get('help'):
        print_usage()
        return

    mode = get_mode(parsed)
    if mode == 'exportReview':
        export_review(REVIEW_FILE)
        return
    if mode == 'validateReview':
        ensure_token()
        validate_review(REVIEW_FILE)
        return
    if mode == 'apply':
        ensure_token()
        apply_review(REVIEW_FILE)
        return
    if mode == 'debugColors':
        ensure_token()
        debug_colors()
        return
    raise RuntimeError(f'Unsupported mode: {mode}')


CATEGORY_RULES = [
    {
        'label': 'facial cleanser',
        'id': 'gid://shopify/TaxonomyCategory/hb-3-2-9-6',
        'fullName': 'Health & Beauty > Personal Care > Cosmetics > Skin Care > Facial Cleansers',
        'confidence': 0.92,
        'patterns': [re.compile(r'\b(face\s*)?cleanser(s)?\b'), re.compile(r'\bcleansing\b')],
    },
    {
        'label': 'face moisturizer',
        'id': 'gid://shopify/TaxonomyCategory/hb-3-2-9-20',
        'fullName': 'Health & Beauty > Personal Care > Cosmetics > Skin Care > Moisturizers > Face Moisturizers',
        'confidence': 0.9,
        'patterns': [re.compile(r'\bmoisturi[sz]er(s)?\b'), re.compile(r'\bface\s+cream\b')],
    },
    {
        'label': 'deodorant',
        'id': 'gid://shopify/TaxonomyCategory/hb-3-5-2',
        'fullName': 'Health & Beauty > Personal Care > Deodorant & Anti-Perspirant',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bdeodorant\b')],
    },
    {
        'label': 'boots',
        'id': 'gid://shopify/TaxonomyCategory/aa-8-3',
        'fullName': 'Apparel & Accessories > Shoes > Boots',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bboot(s)?\b')],
    },
    {
        'label': 'sneakers',
        'id': 'gid://shopify/TaxonomyCategory/aa-8-8',
        'fullName': 'Apparel & Accessories > Shoes > Sneakers',
        'confidence': 0.95,
        'patterns': [re.compile(r'\bsneaker(s)?\b'), re.compile(r'\btrainer(s)?\b')],
    },
    {
        'label': 'shoes',
        'id': 'gid://shopify/TaxonomyCategory/aa-8',
        'fullName': 'Apparel & Accessories > Shoes',
        'confidence': 0.72,
        'patterns': [re.compile(r'\bshoe(s)?\b'), re.compile(r'\bschoen(en)?\b'), re.compile(r'\bloafers?\b'), re.compile(r'\bmoc(s|casins)?\b')],
    },
    {
        'label': 'jeans',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-12-4',
        'fullName': 'Apparel & Accessories > Clothing > Pants > Jeans',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bjean(s)?\b'), re.compile(r'\bdenim\b')],
    },
    {
        'label': 'trousers',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-12-11',
        'fullName': 'Apparel & Accessories > Clothing > Pants > Trousers',
        'confidence': 0.94,
        'patterns': [re.compile(r'\btrouser(s)?\b'), re.compile(r'\bchino(s)?\b')],
    },
    {
        'label': 'pants',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-12',
        'fullName': 'Apparel & Accessories > Clothing > Pants',
        'confidence': 0.82,
        'patterns': [re.compile(r'\bpant(s)?\b'), re.compile(r'\bslack(s)?\b')],
    },
    {
        'label': 'shorts',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-12',
        'fullName': 'Apparel & Accessories > Clothing > Pants',
        'confidence': 0.72,
        'patterns': [re.compile(r'\bshorts\b'), re.compile(r'\bshort\b(?!\s+sleeve)')],
    },
    {
        'label': 'cardigans',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-3',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > Cardigans',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bcardigan(s)?\b')],
    },
    {
        'label': 'sweatshirts',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-14',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts',
        'confidence': 0.92,
        'patterns': [re.compile(r'\bsweatshirt(s)?\b'), re.compile(r'\bhoodie(s)?\b'), re.compile(r'\bcrew\s*neck(s)?\b')],
    },
    {
        'label': 'sweaters',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-12',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > Sweaters',
        'confidence': 0.92,
        'patterns': [re.compile(r'\bsweater(s)?\b'), re.compile(r'\bjumper(s)?\b'), re.compile(r'\bknit(s|wear)?\b'), re.compile(r'\bvee\s*neck(s)?\b')],
    },
    {
        'label': 't-shirts',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-8',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > T-Shirts',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bt[- ]?shirt(s)?\b'), re.compile(r'\btee(s)?\b')],
    },
    {
        'label': 'overshirts',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-5',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > Overshirts',
        'confidence': 0.94,
        'patterns': [re.compile(r'\bovershirt(s)?\b'), re.compile(r'\bover\s+shirt(s)?\b')],
    },
    {
        'label': 'shirts',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-13-7',
        'fullName': 'Apparel & Accessories > Clothing > Shirts & Tops > Shirts',
        'confidence': 0.9,
        'patterns': [re.compile(r'\bshirt(s)?\b'), re.compile(r'\bbutton[- ]?up(s)?\b')],
    },
    {
        'label': 'vests',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-10-6',
        'fullName': 'Apparel & Accessories > Clothing > Outerwear > Vests',
        'confidence': 0.94,
        'patterns': [re.compile(r'\bvest(s)?\b'), re.compile(r'\bbodywarmer(s)?\b'), re.compile(r'\bgilet(s)?\b')],
    },
    {
        'label': 'coats and jackets',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-10-2',
        'fullName': 'Apparel & Accessories > Clothing > Outerwear > Coats & Jackets',
        'confidence': 0.82,
        'patterns': [re.compile(r'\bjacket(s)?\b'), re.compile(r'\bcoat(s)?\b'), re.compile(r'\bparka(s)?\b'), re.compile(r'\bblouson(s)?\b')],
    },
    {
        'label': 'socks',
        'id': 'gid://shopify/TaxonomyCategory/aa-1-18',
        'fullName': 'Apparel & Accessories > Clothing > Socks',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bsock(s)?\b')],
    },
    {
        'label': 'bandanas',
        'id': 'gid://shopify/TaxonomyCategory/aa-2-4',
        'fullName': 'Apparel & Accessories > Clothing Accessories > Bandanas',
        'confidence': 0.96,
        'patterns': [re.compile(r'\bbandana(s)?\b')],
    },
    {
        'label': 'scarves',
        'id': 'gid://shopify/TaxonomyCategory/aa-2-26',
        'fullName': 'Apparel & Accessories > Clothing Accessories > Scarves & Shawls',
        'confidence': 0.94,
        'patterns': [re.compile(r'\bscarf\b'), re.compile(r'\bscarves\b'), re.compile(r'\bshawl(s)?\b')],
    },
    {
        'label': 'hats',
        'id': 'gid://shopify/TaxonomyCategory/aa-2-17',
        'fullName': 'Apparel & Accessories > Clothing Accessories > Hats',
        'confidence': 0.9,
        'patterns': [re.compile(r'\bhat(s)?\b'), re.compile(r'\bcap(s)?\b'), re.compile(r'\bbeanie(s)?\b')],
    },
    {
        'label': 'wallets',
        'id': 'gid://shopify/TaxonomyCategory/aa-5-5',
        'fullName': 'Apparel & Accessories > Handbags, Wallets & Cases > Wallets & Money Clips',
        'confidence': 0.94,
        'patterns': [re.compile(r'\bwallet(s)?\b'), re.compile(r'\bmoney\s+clip(s)?\b')],
    },
    {
        'label': 'belts',
        'id': 'gid://shopify/TaxonomyCategory/aa-2-6',
        'fullName': 'Apparel & Accessories > Clothing Accessories > Belts',
        'confidence': 0.94,
        'patterns': [re.compile(r'\bbelt(s)?\b')],
    },
]

COLOR_RULES = [
    {'name': 'Black', 'confidence': 0.96, 'patterns': [re.compile(r'\bblack\b')]},
    {'name': 'White', 'confidence': 0.94, 'patterns': [re.compile(r'\bwhite\b'), re.compile(r'\boff[- ]?white\b')]},
    {'name': 'Navy', 'confidence': 0.96, 'patterns': [re.compile(r'\bnavy\b'), re.compile(r'\bdark\s+navy\b')]},
    {'name': 'Blue', 'confidence': 0.92, 'patterns': [re.compile(r'\bblue\b'), re.compile(r'\bindigo\b')]},
    {'name': 'Brown', 'confidence': 0.94, 'patterns': [re.compile(r'\bbrown\b'), re.compile(r'\btan\b'), re.compile(r'\bcognac\b')]},
    {'name': 'Grey', 'confidence': 0.94, 'patterns': [re.compile(r'\bgr[ae]y\b'), re.compile(r'\bcharcoal\b'), re.compile(r'\basphalt\b')]},
    {'name': 'Green', 'confidence': 0.92, 'patterns': [re.compile(r'\bgreen\b'), re.compile(r'\bolive\b'), re.compile(r'\bsage\b'), re.compile(r'\bkhaki\b')]},
    {'name': 'Beige', 'confidence': 0.86, 'patterns': [re.compile(r'\bbeige\b'), re.compile(r'\becru\b'), re.compile(r'\bnatural\b'), re.compile(r'\bsand\b')]},
    {'name': 'Red', 'confidence': 0.94, 'patterns': [re.compile(r'\bred\b'), re.compile(r'\bburgundy\b'), re.compile(r'\bmaroon\b')]},
    {'name': 'Yellow', 'confidence': 0.9, 'patterns': [re.compile(r'\byellow\b'), re.compile(r'\bmustard\b'), re.compile(r'\bamber\b')]},
    {'name': 'Orange', 'confidence': 0.9, 'patterns': [re.compile(r'\borange\b'), re.compile(r'\brust\b')]},
    {'name': 'Purple', 'confidence': 0.9, 'patterns': [re.compile(r'\bpurple\b'), re.compile(r'\blilac\b'), re.compile(r'\bviolet\b')]},
    {'name': 'Pink', 'confidence': 0.9, 'patterns': [re.compile(r'\bpink\b'), re.compile(r'\brose\b')]},
]

COLOR_ALIASES = {
    'grey': 'Gray',
    'gray': 'Grey',
    'ecru': 'Beige',
    'natural': 'Beige',
    'sand': 'Beige',
    'tan': 'Brown',
    'cognac': 'Brown',
    'charcoal': 'Grey',
    'asphalt': 'Grey',
    'olive': 'Green',
    'sage': 'Green',
    'khaki': 'Green',
    'indigo': 'Blue',
    'burgundy': 'Red',
    'maroon': 'Red',
    'mustard': 'Yellow',
    'amber': 'Yellow',
    'rust': 'Orange',
    'lilac': 'Purple',
    'violet': 'Purple',
    'rose': 'Pink',
}

COLOR_METAOBJECT_TYPE_CANDIDATES = [
    'shopify--color-pattern',
    'shopify--color',
    'shopify--colour',
    'shopify--color_pattern',
    'shopify--taxonomy-color-pattern',
]

if __name__ == '__main__':
    main()
