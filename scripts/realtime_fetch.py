#!/usr/bin/env python3
"""
Real-time Shopify data fetcher for EVIL ENERGY dashboard.
Fetches TODAY's data using ShopifyQL Analytics API for core KPIs,
and REST Orders API for product/hourly/discount detail.

ShopifyQL provides EXACT numbers matching Shopify backend "Total sales over time".
  total_sales = gross_sales - discounts - returns + shipping_charges + taxes

Shopify backend timezone: America/New_York (EDT UTC-4 in summer, EST UTC-5 in winter)
All dates and hours in this dashboard are based on Shopify's timezone, NOT China time.

Usage:
    python realtime_fetch.py [--config shopify_config.json]
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

API_VERSION = "2024-07"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)  # Parent of scripts/

# Shopify store timezone — America/New_York
SHOP_TZ_NAME = "America/New_York"

# Module-level config path (set in main, used by refresh_token to write back)
_CONFIG_PATH = None

def get_shop_tz():
    """Get Shopify's IANA timezone as a Python timezone object."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(SHOP_TZ_NAME)
    except Exception:
        # Fallback: ZoneInfo not available (tzdata missing on Windows)
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year
        march1 = datetime(year, 3, 1, 2, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        dst_start = march1
        for _ in range(14):
            if dst_start.weekday() == 6 and dst_start.day >= 8 and dst_start.day <= 14:
                break
            dst_start += timedelta(days=1)
        nov1 = datetime(year, 11, 1, 2, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        dst_end = nov1
        for _ in range(7):
            if dst_end.weekday() == 6 and dst_end.day <= 7:
                break
            dst_end += timedelta(days=1)
        now_est = now_utc.astimezone(timezone(timedelta(hours=-5)))
        if now_est >= dst_start and now_est < dst_end:
            return timezone(timedelta(hours=-4))  # EDT
        else:
            return timezone(timedelta(hours=-5))  # EST


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    shop = cfg.get("shopify", {})
    return {
        "shop_domain": shop.get("shop_domain", ""),
        "access_token": shop.get("access_token", ""),
        "client_id": shop.get("client_id", ""),
        "client_secret": shop.get("client_secret", ""),
        "currency": shop.get("currency", "USD"),
        "iana_timezone": shop.get("iana_timezone", SHOP_TZ_NAME),
    }


def refresh_token(config):
    """Refresh access token via client_credentials OAuth flow."""
    if not config["client_id"] or not config["client_secret"]:
        return config["access_token"]
    url = "https://" + config["shop_domain"] + "/admin/oauth/access_token"
    data = json.dumps({
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "client_credentials"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            new_token = result.get("access_token", "")
            if new_token and _CONFIG_PATH and os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    full_cfg = json.load(f)
                full_cfg["shopify"]["access_token"] = new_token
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(full_cfg, f, indent=2, ensure_ascii=False)
                return new_token
    except Exception as e:
        print(f"Token refresh failed: {e}")
    return config["access_token"]


# ───── ShopifyQL (Analytics GraphQL API) ─────

def shopifyql_query(shop_domain, access_token, shopifyql_str):
    """Execute a ShopifyQL query via the Shopify Analytics GraphQL API.

    Returns the parsed rows as a list of dicts, or None on error.
    Each row dict has keys matching the SHOW fields (e.g. 'total_sales', 'orders').
    For TIMESERIES queries, rows include a 'day' or 'hour' key.
    """
    graphql_url = "https://" + shop_domain + "/admin/api/" + API_VERSION + "/graphql.json"
    # GraphQL query — rows is a JSON scalar, columns provides metadata
    gql_query = '{ shopifyqlQuery(query: "' + shopifyql_str + '") { tableData { columns { name dataType displayName } rows } parseErrors } }'

    req = urllib.request.Request(
        graphql_url,
        data=json.dumps({"query": gql_query}).encode("utf-8"),
        headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"  [ShopifyQL Error] HTTP {e.code}: {err_body[:500]}")
        return None
    except Exception as e:
        print(f"  [ShopifyQL Error] {e}")
        return None

    # Check for GraphQL errors
    if "errors" in body:
        print(f"  [ShopifyQL Error] GraphQL: {json.dumps(body['errors'], indent=2)[:500]}")
        return None

    data = body.get("data", {}).get("shopifyqlQuery", {})
    parse_errors = data.get("parseErrors", "")
    if parse_errors:
        print(f"  [ShopifyQL Parse Errors] {parse_errors}")
        return None

    table_data = data.get("tableData", {})
    rows = table_data.get("rows", [])
    return rows


def fetch_sales_kpi(shop_domain, access_token, date_str):
    """Fetch core sales KPI for a single date via ShopifyQL.

    Returns dict with: total_sales, net_sales, gross_sales, discounts,
    shipping_charges, taxes, orders, returns.
    All values are strings from ShopifyQL (need float conversion).
    """
    shopifyql = (
        "FROM sales SHOW total_sales, net_sales, gross_sales, discounts, "
        "shipping_charges, taxes, orders, returns "
        "SINCE " + date_str + " UNTIL " + date_str
    )
    rows = shopifyql_query(shop_domain, access_token, shopifyql)
    if not rows or len(rows) == 0:
        print(f"  [ShopifyQL] No sales data returned for {date_str}")
        return None

    # Non-TIMESERIES query returns one row with all totals
    row = rows[0]
    # Convert string values to float
    kpi = {}
    for key in ("total_sales", "net_sales", "gross_sales", "discounts",
                "shipping_charges", "taxes", "orders", "returns"):
        val = row.get(key, "0")
        kpi[key] = float(val) if key != "orders" else int(val)

    # Verify formula: total_sales = gross_sales - discounts - returns + shipping_charges + taxes
    # Note: discounts and returns are negative values in ShopifyQL
    expected = kpi["gross_sales"] + kpi["discounts"] + kpi["returns"] + kpi["shipping_charges"] + kpi["taxes"]
    diff = abs(kpi["total_sales"] - expected)
    if diff > 0.02:
        print(f"  [ShopifyQL] Formula check: total_sales={kpi['total_sales']} vs computed={expected} (diff={diff:.2f})")

    print(f"  [ShopifyQL] {date_str}: Total sales ${kpi['total_sales']:,.2f} | Net sales ${kpi['net_sales']:,.2f} | Orders {kpi['orders']}")
    return kpi


def fetch_hourly_orders_shopifyql(shop_domain, access_token, date_str):
    """Fetch hourly order distribution via ShopifyQL TIMESERIES hour.

    Returns a dict mapping {local_hour: order_count} where local_hour is
    in Shopify timezone (America/New_York).
    ShopifyQL returns hours in UTC; we convert to Shopify local hour.
    """
    shopifyql = "FROM sales SHOW orders TIMESERIES hour SINCE " + date_str + " UNTIL " + date_str
    rows = shopifyql_query(shop_domain, access_token, shopifyql)
    if not rows:
        return {}

    shop_tz = get_shop_tz()
    hourly = {}
    for row in rows:
        utc_hour_str = row.get("hour", "")
        orders = int(row.get("orders", "0"))
        if not utc_hour_str:
            continue
        # Parse UTC hour and convert to Shopify timezone
        try:
            utc_dt = datetime.fromisoformat(utc_hour_str.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(shop_tz)
            local_hour = local_dt.hour
            hourly[local_hour] = hourly.get(local_hour, 0) + orders
        except Exception:
            continue

    return hourly


# ───── REST Orders API (for product detail + discount codes) ─────

def fetch_orders_today(shop_domain, access_token, shop_tz):
    """Fetch all orders created today (in Shopify's timezone) via REST API.

    Used for product-level breakdown and discount code tracking,
    NOT for revenue calculation (which comes from ShopifyQL).
    """
    now_shop = datetime.now(shop_tz)
    start_shop = now_shop.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_shop.astimezone(timezone.utc)
    end_utc = now_shop.astimezone(timezone.utc)

    all_orders = []
    params = {
        "created_at_min": start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "created_at_max": end_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,financial_status,customer,line_items,order_number,discount_codes",
    }
    base_url = "https://" + shop_domain + "/admin/api/" + API_VERSION + "/orders.json"
    url = base_url + "?" + urllib.parse.urlencode(params)
    page = 1
    while url:
        req = urllib.request.Request(url, headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body)
                link_header = resp.headers.get("Link", "")
        except Exception as e:
            print(f"  [Error] Page {page}: {e}")
            break

        orders = result.get("orders", [])
        all_orders.extend(orders)

        next_url = None
        if link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    s = part.find("<")
                    e2 = part.find(">")
                    if s >= 0 and e2 >= 0:
                        next_url = part[s+1:e2]
                    break
        if not next_url:
            break
        url = next_url
        page += 1

    return all_orders, now_shop


def calculate_product_detail(orders, shop_tz):
    """Calculate product-level breakdown and discount codes from REST orders.

    Revenue numbers come from ShopifyQL, not from order-level accumulation.
    Product revenue here is line_items price * qty (approximate, not exact Shopify total_sales).
    """
    products = {}
    discount_codes_used = set()
    new_cust = 0
    ret_cust = 0

    for order in orders:
        # Discount codes
        for dc in order.get("discount_codes", []):
            code = dc.get("code", "").strip()
            if code:
                discount_codes_used.add(code)

        # New vs returning customer
        customer = order.get("customer")
        if customer:
            cust_created = customer.get("created_at", "")
            order_created = order.get("created_at", "")
            if cust_created and order_created:
                try:
                    cust_dt = datetime.fromisoformat(cust_created.replace("Z", "+00:00")).astimezone(shop_tz)
                    order_dt = datetime.fromisoformat(order_created.replace("Z", "+00:00")).astimezone(shop_tz)
                    if cust_dt.strftime("%Y-%m-%d") == order_dt.strftime("%Y-%m-%d"):
                        new_cust += 1
                    else:
                        ret_cust += 1
                except Exception:
                    new_cust += 1
            else:
                new_cust += 1
        else:
            new_cust += 1

        # Product-level breakdown
        for item in order.get("line_items", []):
            pid = item.get("product_id") or item.get("title", "unknown")
            if pid not in products:
                products[pid] = {
                    "name": item.get("title", "Unknown"),
                    "sku": item.get("sku") or "",
                    "qty": 0,
                    "revenue": 0.0,
                }
            qty = int(item.get("quantity", 0))
            price = float(item.get("price", 0))
            products[pid]["qty"] += qty
            products[pid]["revenue"] += price * qty

    order_count = len(orders)
    return {
        "products": products,
        "discount_codes": sorted(discount_codes_used),
        "order_count": order_count,
        "new_customers": new_cust,
        "returning_customers": ret_cust,
    }


def main():
    global _CONFIG_PATH
    # Try scripts/ first (local dev setup), then repo root (CI/GitHub Actions)
    config_path = os.path.join(SCRIPT_DIR, "shopify_config.json")
    if not os.path.exists(config_path):
        config_path = os.path.join(REPO_ROOT, "shopify_config.json")
    _CONFIG_PATH = config_path
    config = load_config(config_path)

    if not config["shop_domain"] or not config["access_token"]:
        print("ERROR: Shopify credentials not configured")
        sys.exit(1)

    # Auto-refresh token
    access_token = refresh_token(config)

    # Get Shopify timezone
    shop_tz = get_shop_tz()
    now_shop = datetime.now(shop_tz)
    date_str = now_shop.strftime("%Y-%m-%d")

    utc_offset = now_shop.utcoffset()
    offset_h = utc_offset.total_seconds() / 3600 if utc_offset else -5
    dst_name = "EDT" if offset_h == -4 else "EST"
    print(f"[Realtime] Shopify timezone: {SHOP_TZ_NAME} ({dst_name} UTC{int(offset_h)})")
    print(f"[Realtime] Current Shopify time: {now_shop.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[Realtime] Fetching today's data for {date_str}...")

    # ───── 1. ShopifyQL: Core KPI (exact match with Shopify backend) ─────
    print("\n[1/3] Fetching sales KPI via ShopifyQL...")
    kpi = fetch_sales_kpi(config["shop_domain"], access_token, date_str)

    if kpi is None:
        # Fallback: use REST Orders API if ShopifyQL fails
        print("  [Fallback] ShopifyQL unavailable, using REST Orders API...")
        orders, now_shop = fetch_orders_today(config["shop_domain"], access_token, shop_tz)
        total_revenue = 0.0
        total_tax = 0.0
        total_shipping = 0.0
        for order in orders:
            fin_status = order.get("financial_status", "")
            if fin_status in ("paid", "partially_paid", "partially_refunded"):
                total_revenue += float(order.get("total_price", 0) or 0)
                total_tax += float(order.get("total_tax", 0) or 0)
                total_shipping += sum(float(sl.get("price", 0) or 0) for sl in order.get("shipping_lines", []))
        net_sales = total_revenue - total_tax - total_shipping
        kpi = {
            "total_sales": total_revenue,
            "net_sales": net_sales,
            "gross_sales": 0,
            "discounts": 0,
            "shipping_charges": total_shipping,
            "taxes": total_tax,
            "orders": len(orders),
            "returns": 0,
        }
        date_str = now_shop.strftime("%Y-%m-%d")

    # ───── 2. ShopifyQL: Hourly order distribution ─────
    print("\n[2/3] Fetching hourly distribution via ShopifyQL...")
    hourly_dict = fetch_hourly_orders_shopifyql(config["shop_domain"], access_token, date_str)

    # Convert to 24-element array (hourly_orders[0..23])
    hourly_orders = [0] * 24
    for h, count in hourly_dict.items():
        if 0 <= h < 24:
            hourly_orders[h] = count

    # ───── 3. REST Orders: Product detail + discount codes ─────
    print("\n[3/3] Fetching product detail via REST Orders API...")
    orders, now_shop_rest = fetch_orders_today(config["shop_domain"], access_token, shop_tz)
    print(f"  [REST] {len(orders)} orders fetched for product breakdown")

    detail = calculate_product_detail(orders, shop_tz)

    # ───── Build result ─────
    # Determine UTC offset string for display
    utc_offset = now_shop.utcoffset()
    offset_hours = utc_offset.total_seconds() / 3600 if utc_offset else -5
    offset_sign = "+" if offset_hours >= 0 else "-"
    offset_str = f"UTC{offset_sign}{abs(int(offset_hours))}"
    dst_name = "EDT" if offset_hours == -4 else "EST"

    total_orders = kpi["orders"]
    total_sales = kpi["total_sales"]
    net_sales = kpi["net_sales"]
    taxes = kpi["taxes"]
    shipping = kpi["shipping_charges"]
    gross_sales = kpi["gross_sales"]
    discounts = kpi["discounts"]
    returns = kpi["returns"]
    aov = total_sales / total_orders if total_orders > 0 else 0

    result = {
        "date": date_str,
        "updated_at": now_shop.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_timezone": f"{dst_name} ({offset_str})",
        "is_realtime": True,
        "current_hour": now_shop.hour,
        "currency": "USD",
        "timezone": {
            "iana": SHOP_TZ_NAME,
            "utc_offset": offset_hours,
            "dst_name": dst_name,
        },
        "kpi": {
            "total_orders": total_orders,
            "total_revenue": round(total_sales, 2),
            "total_tax": round(taxes, 2),
            "total_shipping": round(shipping, 2),
            "total_discounts": round(abs(discounts), 2),
            "net_sales": round(net_sales, 2),
            "gross_sales": round(gross_sales, 2),
            "returns": round(abs(returns), 2),
            "avg_order_value": round(aov, 2),
            "total_customers": detail["new_customers"] + detail["returning_customers"],
            "new_customers": detail["new_customers"],
            "returning_customers": detail["returning_customers"],
            "new_customer_rate": round(detail["new_customers"] / max(total_orders, 1) * 100, 1),
        },
        "hourly_orders": hourly_orders,
        "top_products": [
            {"title": p["name"], "sku": p["sku"], "qty": p["qty"], "revenue": round(p["revenue"], 2)}
            for p in sorted(detail["products"].values(), key=lambda x: x["revenue"], reverse=True)[:10]
        ],
        "discount_codes": detail["discount_codes"],
        "data_source": "shopifyql",  # Indicates revenue comes from ShopifyQL
    }

    # Write realtime JSON
    # Write realtime JSON to repo root (so update_github.py finds it)
    realtime_path = os.path.join(REPO_ROOT, "dashboard_realtime.json")
    with open(realtime_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n[Realtime] Written to {realtime_path}")
    print(f"[Realtime] Revenue: ${result['kpi']['total_revenue']:,.2f} (Total sales via ShopifyQL)")
    print(f"[Realtime] Net sales: ${result['kpi']['net_sales']:,.2f} | Tax: ${result['kpi']['total_tax']:,.2f} | Shipping: ${result['kpi']['total_shipping']:,.2f}")
    print(f"[Realtime] Orders: {result['kpi']['total_orders']} | AOV: ${result['kpi']['avg_order_value']:,.2f}")
    print(f"[Realtime] Date shown: {result['date']} ({dst_name})")
    print(f"[Realtime] Hourly orders: {hourly_orders}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
