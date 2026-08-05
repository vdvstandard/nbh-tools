#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from shopify_api import get_client_credentials_token


DEFAULT_API_VERSION = "2026-04"
EXPECTED_POLICY_HANDLES = {
    "contact-information",
    "legal-notice",
    "privacy-policy",
    "refund-policy",
    "shipping-policy",
    "terms-of-service",
}


def parse_env_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = parse_env_value(match.group(2))


def normalize_shop(value):
    cleaned = (
        (value or "")
        .strip()
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )
    return cleaned if "." in cleaned else f"{cleaned}.myshopify.com"


def request_json(url, token, method="GET", payload=None, attempts=5):
    headers = {
        "Accept": "application/json",
        "X-Shopify-Access-Token": token,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            last_error = error
            response_text = error.read().decode("utf-8", errors="replace")
            if error.code != 429 and error.code < 500:
                raise RuntimeError(
                    f"Shopify request failed ({error.code}) for {url}: {response_text}"
                ) from error
            if attempt < attempts:
                retry_after = float(error.headers.get("Retry-After") or min(attempt, 4))
                time.sleep(max(retry_after, 0.75))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(attempt, 4))
    raise RuntimeError(f"Shopify request failed after {attempts} attempts: {url}") from last_error


def graphql(endpoint, token, query):
    response = request_json(
        endpoint,
        token,
        method="POST",
        payload={"query": query, "variables": {}},
    )
    if response.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {response['errors']}")
    return response.get("data") or {}


def optional_read(label, callback, errors):
    try:
        return callback()
    except RuntimeError as error:
        errors[label] = str(error)
        return None


def acquire_token(args):
    stored_token = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN") or os.environ.get(
        "SHOPIFY_ACCESS_TOKEN"
    )
    client_id = os.environ.get("SHOPIFY_API_KEY") or os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_API_SECRET") or os.environ.get(
        "SHOPIFY_CLIENT_SECRET"
    )
    if args.use_stored_token and stored_token:
        return stored_token, "stored_admin_token"
    if client_id and client_secret:
        token_data = get_client_credentials_token(args.shop, client_id, client_secret)
        return token_data["access_token"], "client_credentials"
    if stored_token:
        return stored_token, "stored_admin_token"
    raise SystemExit(
        "Missing Shopify credentials. Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET, "
        "or SHOPIFY_ADMIN_ACCESS_TOKEN."
    )


def clean_html(value):
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def is_blank(value):
    return not str(value or "").strip()


def mask_email(value):
    if not value or "@" not in value:
        return value or ""
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def summarize_policy(policy):
    text = clean_html(policy.get("body"))
    return {
        "handle": policy.get("handle"),
        "title": policy.get("title"),
        "url": policy.get("url"),
        "updatedAt": policy.get("updated_at"),
        "bodyCharacters": len(text),
        "bodyPreview": text[:180],
    }


def summarize_shipping_zone(zone):
    price_rates = zone.get("price_based_shipping_rates") or []
    weight_rates = zone.get("weight_based_shipping_rates") or []
    carrier_rates = zone.get("carrier_shipping_rate_providers") or []
    return {
        "id": zone.get("id"),
        "name": zone.get("name"),
        "profileId": zone.get("profile_id"),
        "locationGroupId": zone.get("location_group_id"),
        "countries": [country.get("code") for country in zone.get("countries") or []],
        "countryTaxRates": [
            {
                "country": country.get("code"),
                "tax": country.get("tax"),
                "taxName": country.get("tax_name"),
            }
            for country in zone.get("countries") or []
        ],
        "priceRates": [
            {
                "name": rate.get("name"),
                "price": rate.get("price"),
                "minimumOrderSubtotal": rate.get("min_order_subtotal"),
                "maximumOrderSubtotal": rate.get("max_order_subtotal"),
            }
            for rate in price_rates
        ],
        "weightRates": [
            {
                "name": rate.get("name"),
                "price": rate.get("price"),
                "weightLow": rate.get("weight_low"),
                "weightHigh": rate.get("weight_high"),
            }
            for rate in weight_rates
        ],
        "carrierRateProviderCount": len(carrier_rates),
        "activeRateCount": len(price_rates) + len(weight_rates) + len(carrier_rates),
    }


def summarize_delivery_profiles(data):
    profiles = []
    for edge in ((data or {}).get("deliveryProfiles") or {}).get("edges") or []:
        node = edge.get("node") or {}
        zones = []
        for location_group in node.get("profileLocationGroups") or []:
            for zone_edge in (location_group.get("locationGroupZones") or {}).get("edges") or []:
                zone_node = zone_edge.get("node") or {}
                zone = zone_node.get("zone") or {}
                methods = [
                    method_edge.get("node") or {}
                    for method_edge in (zone_node.get("methodDefinitions") or {}).get("edges") or []
                ]
                zones.append(
                    {
                        "id": zone.get("id"),
                        "name": zone.get("name"),
                        "countries": [
                            (country.get("code") or {}).get("countryCode")
                            or "REST_OF_WORLD"
                            for country in zone.get("countries") or []
                        ],
                        "methods": methods,
                        "activeMethodCount": sum(bool(item.get("active")) for item in methods),
                    }
                )
        profiles.append(
            {
                "id": node.get("id"),
                "activeMethodDefinitionsCount": sum(
                    zone["activeMethodCount"] for zone in zones
                ),
                "zonesWithoutActiveMethods": [
                    zone["name"] for zone in zones if not zone["activeMethodCount"]
                ],
                "zones": zones,
            }
        )
    return profiles


def summarize_order(order, qa_tag):
    tags = [part.strip() for part in (order.get("tags") or "").split(",") if part.strip()]
    shipping_titles = [line.get("title") for line in order.get("shipping_lines") or []]
    discount_codes = [discount.get("code") for discount in order.get("discount_codes") or []]
    return {
        "name": order.get("name"),
        "createdAt": order.get("created_at"),
        "cancelledAt": order.get("cancelled_at"),
        "financialStatus": order.get("financial_status"),
        "fulfillmentStatus": order.get("fulfillment_status"),
        "sourceName": order.get("source_name"),
        "tags": tags,
        "isTaggedQaOrder": qa_tag.lower() in {tag.lower() for tag in tags},
        "shippingLines": shipping_titles,
        "discountCodes": discount_codes,
        "refundCount": len(order.get("refunds") or []),
        "taxLineCount": len(order.get("tax_lines") or []),
        "taxesIncluded": order.get("taxes_included"),
        "totalDiscounts": order.get("total_discounts"),
    }


def check(key, status, detail, evidence=None):
    result = {"key": key, "status": status, "detail": detail}
    if evidence is not None:
        result["evidence"] = evidence
    return result


def main():
    parser = argparse.ArgumentParser(description="Run read-only Shopify Phase 7 operational QA.")
    parser.add_argument("--shop", help="Shopify store domain.")
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--env-file", help="Path to .env.")
    parser.add_argument(
        "--output",
        default=".tmp/phase7-operational-qa-20260805.json",
        help="JSON report path.",
    )
    parser.add_argument("--qa-tag", default="phase7-qa")
    parser.add_argument("--use-stored-token", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(Path(args.env_file or project_root / ".env"))
    args.shop = normalize_shop(
        args.shop
        or os.environ.get("SHOPIFY_SHOP")
        or os.environ.get("SHOPIFY_STORE_DOMAIN")
        or os.environ.get("SHOPIFY_STORE")
    )
    if not args.shop or args.shop == ".myshopify.com":
        raise SystemExit("Missing SHOPIFY_SHOP.")

    token, auth_source = acquire_token(args)
    rest_base = f"https://{args.shop}/admin/api/{args.api_version}"
    graphql_endpoint = f"{rest_base}/graphql.json"
    errors = {}

    shop_data = optional_read(
        "shop",
        lambda: graphql(
            graphql_endpoint,
            token,
            """
            query Phase7Shop {
              shop {
                name
                email
                contactEmail
                currencyCode
                taxesIncluded
                taxShipping
                ianaTimezone
                url
                primaryDomain { host url }
                shipsToCountries
                countriesInShippingZones { countryCodes includeRestOfWorld }
                shopAddress {
                  address1
                  address2
                  city
                  province
                  provinceCode
                  country
                  countryCodeV2
                  zip
                  phone
                }
              }
            }
            """,
        ),
        errors,
    )
    location_data = optional_read(
        "locations",
        lambda: graphql(
            graphql_endpoint,
            token,
            """
            query Phase7Locations {
              locations(first: 100, includeInactive: true, includeLegacy: true) {
                nodes {
                  id
                  legacyResourceId
                  name
                  isActive
                  isFulfillmentService
                  fulfillsOnlineOrders
                  shipsInventory
                  hasActiveInventory
                  addressVerified
                  localPickupSettingsV2 { pickupTime instructions }
                  address {
                    address1
                    address2
                    city
                    province
                    provinceCode
                    country
                    countryCode
                    zip
                    phone
                  }
                }
              }
            }
            """,
        ),
        errors,
    )
    delivery_data = optional_read(
        "delivery_profiles",
        lambda: graphql(
            graphql_endpoint,
            token,
            """
            query Phase7DeliveryProfiles {
              deliveryProfiles(first: 10) {
                edges {
                  node {
                    id
                    profileLocationGroups {
                      locationGroupZones(first: 50) {
                        edges {
                          node {
                            zone {
                              id
                              name
                              countries {
                                code { countryCode restOfWorld }
                              }
                            }
                            methodDefinitions(first: 50) {
                              edges {
                                node {
                                  id
                                  active
                                  description
                                  methodConditions {
                                    field
                                    operator
                                    conditionCriteria {
                                      __typename
                                      ... on MoneyV2 { amount currencyCode }
                                      ... on Weight { unit value }
                                    }
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
        ),
        errors,
    )

    policies_data = optional_read(
        "policies",
        lambda: request_json(f"{rest_base}/policies.json", token),
        errors,
    )
    zones_data = optional_read(
        "shipping_zones",
        lambda: request_json(f"{rest_base}/shipping_zones.json", token),
        errors,
    )
    order_fields = ",".join(
        [
            "id",
            "name",
            "created_at",
            "cancelled_at",
            "financial_status",
            "fulfillment_status",
            "source_name",
            "tags",
            "shipping_lines",
            "discount_codes",
            "refunds",
            "tax_lines",
            "taxes_included",
            "total_discounts",
        ]
    )
    order_query = urllib.parse.urlencode(
        {"status": "any", "limit": 50, "order": "created_at desc", "fields": order_fields}
    )
    orders_data = optional_read(
        "orders",
        lambda: request_json(f"{rest_base}/orders.json?{order_query}", token),
        errors,
    )
    webhooks_data = optional_read(
        "webhooks",
        lambda: request_json(f"{rest_base}/webhooks.json?limit=250", token),
        errors,
    )
    scopes_data = optional_read(
        "access_scopes",
        lambda: request_json(f"https://{args.shop}/admin/oauth/access_scopes.json", token),
        errors,
    )

    shop = (shop_data or {}).get("shop") or {}
    locations = ((location_data or {}).get("locations") or {}).get("nodes") or []
    policies = [summarize_policy(item) for item in (policies_data or {}).get("policies") or []]
    zones = [summarize_shipping_zone(item) for item in (zones_data or {}).get("shipping_zones") or []]
    delivery_profiles = summarize_delivery_profiles(delivery_data)
    orders = [summarize_order(item, args.qa_tag) for item in (orders_data or {}).get("orders") or []]
    qa_orders = [item for item in orders if item["isTaggedQaOrder"]]
    pickup_locations = [item for item in locations if item.get("localPickupSettingsV2")]
    policy_handles = {item["handle"] for item in policies}
    missing_policies = sorted(EXPECTED_POLICY_HANDLES - policy_handles)
    empty_policies = sorted(
        item["handle"] for item in policies if item["bodyCharacters"] == 0
    )
    zones_without_rates = sorted(zone["name"] for zone in zones if not zone["activeRateCount"])
    active_delivery_methods = sum(
        profile.get("activeMethodDefinitionsCount") or 0 for profile in delivery_profiles
    )
    delivery_zones_without_methods = sorted(
        zone_name
        for profile in delivery_profiles
        for zone_name in profile.get("zonesWithoutActiveMethods") or []
    )
    qa_coverage = {
        "shipping": any(item["shippingLines"] for item in qa_orders),
        "pickup": any(
            re.search(r"pickup|pick up|afhalen|ophalen", " ".join(item["shippingLines"]), re.I)
            for item in qa_orders
        ),
        "discount": any(item["discountCodes"] or item["totalDiscounts"] not in {None, "0.00", "0"} for item in qa_orders),
        "refund": any(item["refundCount"] for item in qa_orders),
        "cancellation": any(item["cancelledAt"] for item in qa_orders),
    }
    address = shop.get("shopAddress") or {}
    required_address_fields = {
        "address1": address.get("address1"),
        "city": address.get("city"),
        "zip": address.get("zip"),
        "countryCodeV2": address.get("countryCodeV2"),
        "phone": address.get("phone"),
    }
    missing_shop_identity_fields = sorted(
        field for field, value in required_address_fields.items() if is_blank(value)
    )
    if is_blank(shop.get("contactEmail")):
        missing_shop_identity_fields.append("contactEmail")

    checks = []
    checks.append(
        check(
            "tax_configuration",
            "pass" if shop else "blocked",
            "Shop tax flags and configured shipping countries were read successfully."
            if shop
            else "Shop tax configuration could not be read.",
            {
                "taxesIncluded": shop.get("taxesIncluded"),
                "taxShipping": shop.get("taxShipping"),
                "currencyCode": shop.get("currencyCode"),
                "shipsToCountries": shop.get("shipsToCountries") or [],
            }
            if shop
            else None,
        )
    )
    checks.append(
        check(
            "shop_contact_identity",
            "pass" if shop and not missing_shop_identity_fields else "warn" if shop else "blocked",
            "The primary shop address and contact fields are complete."
            if shop and not missing_shop_identity_fields
            else "The primary shop configuration is missing address or contact fields.",
            {
                "missingFields": missing_shop_identity_fields,
                "address": address,
                "contactEmail": mask_email(shop.get("contactEmail")),
            }
            if shop
            else None,
        )
    )
    checks.append(
        check(
            "shipping_zones",
            "pass"
            if delivery_profiles and active_delivery_methods and not delivery_zones_without_methods
            else "warn"
            if delivery_profiles or zones
            else "blocked",
            "Every delivery-profile zone has at least one active shipping method."
            if delivery_profiles and active_delivery_methods and not delivery_zones_without_methods
            else "One or more delivery-profile zones has no active shipping method, or profiles could not be read.",
            {
                "zoneCount": len(zones),
                "restZonesWithoutRates": zones_without_rates,
                "deliveryProfileCount": len(delivery_profiles),
                "activeDeliveryMethods": active_delivery_methods,
                "deliveryZonesWithoutMethods": delivery_zones_without_methods,
            },
        )
    )
    checks.append(
        check(
            "pickup_location",
            "pass" if pickup_locations else "warn" if locations else "blocked",
            "At least one location has local pickup enabled."
            if pickup_locations
            else "No API-visible local pickup configuration was found.",
            {
                "pickupLocations": [
                    {
                        "name": item.get("name"),
                        "isActive": item.get("isActive"),
                        "fulfillsOnlineOrders": item.get("fulfillsOnlineOrders"),
                        "settings": item.get("localPickupSettingsV2"),
                    }
                    for item in pickup_locations
                ]
            },
        )
    )
    checks.append(
        check(
            "policies",
            "pass" if policies and not missing_policies and not empty_policies else "warn",
            "All expected policies contain content."
            if policies and not missing_policies and not empty_policies
            else "Expected policies are missing or empty.",
            {"missing": missing_policies, "empty": empty_policies},
        )
    )
    checks.append(
        check(
            "order_notifications",
            "manual",
            "Notification templates and delivered email contents require Shopify previews or tagged test orders.",
        )
    )
    checks.append(
        check(
            "tagged_test_orders",
            "pass" if qa_orders and all(qa_coverage.values()) else "manual",
            "Tagged Phase 7 orders cover shipping, pickup, discount, refund and cancellation."
            if qa_orders and all(qa_coverage.values())
            else "No complete tagged Phase 7 order set was found; no orders were created or modified by this audit.",
            {"tag": args.qa_tag, "taggedOrderCount": len(qa_orders), "coverage": qa_coverage},
        )
    )
    checks.append(
        check(
            "inventory_sync",
            "manual",
            "Run the catalog drift audit with fresh Lightspeed product and inventory exports.",
        )
    )
    checks.append(
        check(
            "analytics_and_consent",
            "manual",
            "Use the browser audit to confirm consent gating and duplicate conversion events.",
        )
    )

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_operational_qa",
        "shop": args.shop,
        "apiVersion": args.api_version,
        "authentication": {"source": auth_source, "tokenStored": False},
        "summary": {
            "checks": len(checks),
            "pass": sum(item["status"] == "pass" for item in checks),
            "warn": sum(item["status"] == "warn" for item in checks),
            "manual": sum(item["status"] == "manual" for item in checks),
            "blocked": sum(item["status"] == "blocked" for item in checks),
            "readErrors": len(errors),
        },
        "checks": checks,
        "shopConfiguration": {
            "name": shop.get("name"),
            "ownerEmail": mask_email(shop.get("email")),
            "contactEmail": mask_email(shop.get("contactEmail")),
            "currencyCode": shop.get("currencyCode"),
            "taxesIncluded": shop.get("taxesIncluded"),
            "taxShipping": shop.get("taxShipping"),
            "ianaTimezone": shop.get("ianaTimezone"),
            "url": shop.get("url"),
            "primaryDomain": shop.get("primaryDomain"),
            "shippingCountries": shop.get("countriesInShippingZones"),
            "address": address,
        },
        "locations": locations,
        "policies": policies,
        "shippingZones": zones,
        "deliveryProfiles": delivery_profiles,
        "recentOrderPatterns": {
            "ordersInspected": len(orders),
            "taggedQaOrders": qa_orders,
            "historicalCoverage": {
                "shipping": any(item["shippingLines"] for item in orders),
                "pickup": any(
                    re.search(r"pickup|pick up|afhalen|ophalen", " ".join(item["shippingLines"]), re.I)
                    for item in orders
                ),
                "discount": any(item["discountCodes"] for item in orders),
                "refund": any(item["refundCount"] for item in orders),
                "cancellation": any(item["cancelledAt"] for item in orders),
            },
        },
        "webhooks": [
            {
                "topic": item.get("topic"),
                "format": item.get("format"),
                "apiVersion": item.get("api_version"),
                "endpointHost": urllib.parse.urlparse(item.get("address") or "").hostname,
            }
            for item in (webhooks_data or {}).get("webhooks") or []
        ],
        "accessScopes": sorted(
            item.get("handle")
            for item in (scopes_data or {}).get("access_scopes") or []
            if item.get("handle")
        ),
        "readErrors": errors,
    }
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {output_path}")
    if report["summary"]["blocked"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
