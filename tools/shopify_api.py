"""
Lightweight Shopify Admin API helper used by tools in this repo.

No external dependencies; uses urllib from the standard library to match existing tools.
"""

import json
import urllib.parse
import urllib.request
import time
from typing import Any, Dict, Optional


def urlencode_form(data: Dict[str, Any]) -> bytes:
    return urllib.parse.urlencode(data).encode("utf-8")


def http_post(url: str, body: bytes, headers: Optional[Dict[str, str]] = None, timeout: int = 30):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8"), response.status, dict(response.getheaders())


def http_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8"), response.status, dict(response.getheaders())


def get_client_credentials_token(shop: str, client_id: str, client_secret: str) -> Dict[str, Any]:
    body = urlencode_form({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    response_text, status, _ = http_post(
        f"https://{shop}/admin/oauth/access_token",
        body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    data = json.loads(response_text or "{}")
    if status < 200 or status >= 300:
        message = data.get("error_description") or data.get("error") or response_text or "Unknown error"
        raise RuntimeError(f"Shopify client_credentials failed ({status}): {message}")
    if "access_token" not in data:
        raise RuntimeError("Shopify did not return an access_token.")
    return data


def graphql_request(endpoint: str, token: str, query: str, variables: Optional[Dict[str, Any]] = None, retries: int = 3) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
    payload = {"query": query, "variables": variables or {}}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response_text, status, _ = http_post_json(endpoint, payload, headers=headers)
            data = json.loads(response_text or "{}")
            if status < 200 or status >= 300:
                raise RuntimeError(f"GraphQL request failed ({status}): {response_text}")
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data.get("data") or {}
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1 * attempt)
                continue
            raise
    raise last_exc


def paginate_products(endpoint: str, token: str, query: str, variables: Optional[Dict[str, Any]] = None, page_size: int = 100):
    """
    Helper to paginate typical Shopify product list GraphQL queries that return `products(first:$first, after:$after)`.
    The `query` should request `products(first:$first, after:$after)` and return `nodes` and `pageInfo`.
    Yields nodes one by one.
    """
    vars = dict(variables or {})
    after = None
    while True:
        vars.update({"first": page_size, "after": after})
        data = graphql_request(endpoint, token, query, vars)
        if not data or "products" not in data:
            break
        products = data["products"]
        nodes = products.get("nodes") or []
        for n in nodes:
            yield n
        page_info = products.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")


if __name__ == "__main__":
    print("shopify_api helper loaded. Import and use its functions from other tools.")
