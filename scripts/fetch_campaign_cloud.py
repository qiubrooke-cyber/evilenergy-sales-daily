#!/usr/bin/env python3
"""
Cloud campaign performance calculator — runs inside GitHub Actions.

Reads campaign_config.json (campaign schedule, maintained in the repo),
computes each campaign's performance directly from Shopify
(ShopifyQL for KPIs + REST for order-level stats), and writes
campaign_data.json in the exact same format the web dashboard expects.

No DingTalk, no local computer needed. This makes the campaign page
on GitHub Pages 100% cloud-automated.

Usage:
    python fetch_campaign_cloud.py

Config resolution (mirrors realtime_fetch.py CI pattern):
    - shopify_config.json at repo root (created by workflow from Secrets)
      takes precedence over local scripts/shopify_config.json.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

CONFIG_PATH = os.path.join(REPO_ROOT, "campaign_config.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "campaign_data.json")

SHOP_TZ_NAME = "America/New_York"
API_VERSION = "2024-07"

# shopify_config.json location: CI puts it at repo root; local dev uses scripts/
_CONFIG_PATH = None


def get_shop_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(SHOP_TZ_NAME)
    except Exception:
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year
        march1 = datetime(year, 3, 1, tzinfo=timezone(timedelta(hours=-5)))
        dst_start = march1
        while dst_start.weekday() != 6 or dst_start.day < 8:
            dst_start += timedelta(days=1)
        nov1 = datetime(year, 11, 1, tzinfo=timezone(timedelta(hours=-4)))
        dst_end = nov1
        while dst_end.weekday() != 6 or dst_end.day < 1:
            dst_end += timedelta(days=1)
        offset = -4 if dst_start <= now_utc < dst_end else -5
        return timezone(timedelta(hours=offset))


def load_shop_config():
    """Load Shopify credentials. In CI, shopify_config.json is at repo root."""
    global _CONFIG_PATH
    candidates = [
        os.path.join(REPO_ROOT, "shopify_config.json"),
        os.path.join(SCRIPT_DIR, "shopify_config.json"),
    ]
    cfg_path = None
    for p in candidates:
        if os.path.exists(p):
            cfg_path = p
            break
    if not cfg_path:
        print("  [ERROR] shopify_config.json not found")
        return None
    _CONFIG_PATH = cfg_path
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    shop = cfg.get("shopify", {})
    return {
        "shop_domain": shop.get("shop_domain", ""),
        "access_token": shop.get("access_token", ""),
        "client_id": shop.get("client_id", ""),
        "client_secret": shop.get("client_secret", ""),
        "currency": shop.get("currency", "USD"),
        "iana_timezone": shop.get("iana_timezone", SHOP_TZ_NAME),
        "config_path": cfg_path,
    }


def refresh_token_via_client_credentials(config):
    if not config["client_id"] or not config["client_secret"]:
        return None
    url = f"https://{config['shop_domain']}/admin/oauth/access_token"
    data = json.dumps({
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "client_credentials"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            new_token = result.get("access_token", "")
            print(f"  Token refreshed (expires in {result.get('expires_in', 0)}s)")
            cfg_path = config.get("config_path")
            if cfg_path and os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg["shopify"]["access_token"] = new_token
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    print(f"  Warning: could not update config file: {e}")
            return new_token
    except Exception as e:
        print(f"  Token refresh failed: {e}")
        return None


def shopifyql_query(shop_domain, access_token, shopifyql_str):
    graphql_url = "https://" + shop_domain + "/admin/api/" + API_VERSION + "/graphql.json"
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
        print(f"  [ShopifyQL Error] HTTP {e.code}: {err_body[:300]}")
        return None
    except Exception as e:
        print(f"  [ShopifyQL Error] {e}")
        return None

    if "errors" in body:
        print(f"  [ShopifyQL Error] GraphQL: {json.dumps(body['errors'])[:300]}")
        return None

    data = body.get("data", {}).get("shopifyqlQuery", {})
    if data.get("parseErrors"):
        print(f"  [ShopifyQL Parse Errors] {data['parseErrors'][:300]}")
        return None
    return data.get("tableData", {}).get("rows", [])


def fetch_campaign_sales_kpi(shop_domain, access_token, start_date_str, end_date_str):
    shopifyql = (
        "FROM sales SHOW total_sales, net_sales, gross_sales, discounts, "
        "shipping_charges, taxes, orders, returns "
        "SINCE " + start_date_str + " UNTIL " + end_date_str
    )
    rows = shopifyql_query(shop_domain, access_token, shopifyql)
    if not rows:
        print(f"  [ShopifyQL] No data for {start_date_str} ~ {end_date_str}")
        return None

    row = rows[0]
    kpi = {}
    for key in ("total_sales", "net_sales", "gross_sales", "discounts",
                "shipping_charges", "taxes", "orders", "returns"):
        val = row.get(key, "0")
        kpi[key] = float(val) if key != "orders" else int(val)

    print(f"  [ShopifyQL] {start_date_str}~{end_date_str}: Total sales ${kpi['total_sales']:,.2f} | Orders {kpi['orders']}")
    return kpi


def shopify_get(shop_domain, access_token, endpoint, params=None):
    url = f"https://{shop_domain}/admin/api/{API_VERSION}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"  [Shopify API Error] {e.code} {e.reason}: {body[:300]}")
        return None
    except Exception as e:
        print(f"  [Request Error] {e}")
        return None


def fetch_orders_range(shop_domain, access_token, start_utc, end_utc):
    """Fetch all orders in a UTC range via Link header pagination."""
    all_orders = []
    params = {
        "created_at_min": start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "created_at_max": end_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,total_price,financial_status,customer,line_items",
    }
    base_url = f"https://{shop_domain}/admin/api/{API_VERSION}/orders.json"
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
        except urllib.error.HTTPError as e:
            print(f"    [Shopify API Error] {e.code}: {e.reason}")
            break
        except Exception as e:
            print(f"    [Request Error] {e}")
            break

        orders = result.get("orders", [])
        all_orders.extend(orders)
        print(f"    Page {page}: {len(orders)} orders (total: {len(all_orders)})")

        next_url = None
        if link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    s = part.find("<")
                    e = part.find(">")
                    if s >= 0 and e >= 0:
                        next_url = part[s + 1:e]
                    break
        if not next_url:
            break
        url = next_url
        page += 1
    return all_orders


def fetch_checkouts_range(shop_domain, access_token, start_utc, end_utc):
    params = {
        "created_at_min": start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "created_at_max": end_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "limit": 250,
    }
    base_url = f"https://{shop_domain}/admin/api/{API_VERSION}/checkouts.json"
    url = base_url + "?" + urllib.parse.urlencode(params)
    all_checkouts = []
    page = 1
    while url:
        req = urllib.request.Request(url, headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                link_header = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            print(f"    [Shopify API Error] {e.code}: {e.reason}")
            break
        except Exception as e:
            print(f"    [Request Error] {e}")
            break

        checkouts = result.get("checkouts", [])
        all_checkouts.extend(checkouts)
        print(f"    Checkouts page {page}: {len(checkouts)} (total: {len(all_checkouts)})")

        next_url = None
        if link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    s = part.find("<")
                    e = part.find(">")
                    if s >= 0 and e >= 0:
                        next_url = part[s + 1:e]
                    break
        if not next_url:
            break
        url = next_url
        page += 1
    return all_checkouts


def campaign_status(start_date_str, end_date_str, shop_now):
    """Auto-compute status from dates (matches DingTalk conventions)."""
    today = shop_now.date()
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "未排期"
    if today < start:
        return "未开始"
    if start <= today <= end:
        return "进行中"
    return "已完成"


def compute_campaign(config, campaign_cfg, shop_now):
    """Compute full performance metrics for one campaign."""
    name = campaign_cfg.get("name", "")
    start_s = campaign_cfg.get("start_date", "")
    end_s = campaign_cfg.get("end_date", "")

    campaign = {
        "name": name,
        "theme": campaign_cfg.get("theme", ""),
        "level": campaign_cfg.get("level", ""),
        "type": campaign_cfg.get("type", ""),
        "start_date": start_s,
        "end_date": end_s,
        "days": campaign_cfg.get("days", 0),
        "status": campaign_status(start_s, end_s, shop_now),
        "orders": 0, "revenue": 0, "aov": 0,
        "daily_revenue": 0, "daily_orders": 0,
        "conversion_rate": 0,
        "new_customers": 0, "new_customer_rate": "--",
        "returning_customers": 0, "total_customers": 0,
        "product_count": 0,
        "net_sales": 0,
        "data_source": "",
    }

    # Days
    try:
        start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_s, "%Y-%m-%d").date()
        duration_days = (end_d - start_d).days + 1
        campaign["days"] = duration_days
    except (ValueError, TypeError):
        duration_days = 0

    # Future campaigns: no Shopify fetch
    if campaign["status"] == "未开始":
        print(f"    Not started yet (starts {start_s}), skipping Shopify fetch")
        return campaign

    shop_domain = config["shop_domain"]
    access_token = config["access_token"]

    # UTC range from shop-local dates
    shop_tz = get_shop_tz()
    try:
        start_local = datetime.strptime(start_s, "%Y-%m-%d").replace(tzinfo=shop_tz)
        end_local = datetime.strptime(end_s, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=shop_tz)
    except ValueError:
        return campaign
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    # KPI via ShopifyQL
    kpi = fetch_campaign_sales_kpi(shop_domain, access_token, start_s, end_s)
    if kpi:
        total_revenue = kpi["total_sales"]
        net_sales = kpi["net_sales"]
        order_count = kpi["orders"]
        data_source = "shopifyql"
    else:
        total_revenue = 0.0
        net_sales = 0.0
        order_count = 0
        data_source = ""

    # Orders for new/returning + product stats
    orders = fetch_orders_range(shop_domain, access_token, start_utc, end_utc)

    # Fallback: REST accumulation if ShopifyQL failed
    if not kpi:
        total_revenue = 0.0
        for order in orders:
            fin_status = order.get("financial_status", "")
            if fin_status in ("paid", "partially_paid", "partially_refunded"):
                total_revenue += float(order.get("total_price", 0))
        order_count = len(orders)
        data_source = "rest_api"

    checkouts = fetch_checkouts_range(shop_domain, access_token, start_utc, end_utc)

    new_cust = 0
    ret_cust = 0
    product_set = set()
    for order in orders:
        customer = order.get("customer")
        if customer:
            cust_created = customer.get("created_at", "")
            order_created = order.get("created_at", "")
            if cust_created and order_created:
                if cust_created[:10] == order_created[:10]:
                    new_cust += 1
                else:
                    ret_cust += 1
            else:
                new_cust += 1
        else:
            new_cust += 1
        for item in order.get("line_items", []):
            pid = item.get("product_id")
            if pid:
                product_set.add(pid)

    aov = total_revenue / order_count if order_count > 0 else 0
    checkout_started = len(checkouts) + order_count
    conv_rate = order_count / checkout_started if checkout_started > 0 else 0
    new_ratio = new_cust / order_count if order_count > 0 else 0
    daily_rev = total_revenue / duration_days if duration_days > 0 else 0
    daily_ord = order_count / duration_days if duration_days > 0 else 0

    campaign["revenue"] = round(total_revenue, 2)
    campaign["net_sales"] = round(net_sales, 2)
    campaign["orders"] = order_count
    campaign["aov"] = round(aov, 2)
    campaign["daily_revenue"] = round(daily_rev, 2)
    campaign["daily_orders"] = round(daily_ord, 1)
    campaign["conversion_rate"] = round(conv_rate, 4)
    campaign["new_customers"] = new_cust
    campaign["returning_customers"] = ret_cust
    campaign["total_customers"] = new_cust + ret_cust
    campaign["new_customer_rate"] = str(round(new_ratio * 100, 1)) + "%"
    campaign["product_count"] = len(product_set)
    campaign["data_source"] = data_source

    print(f"    Revenue: ${total_revenue:,.2f} (source: {data_source}) | Orders: {order_count} | AOV: ${aov:.2f}")
    print(f"    Conversion: {conv_rate*100:.2f}% | New: {new_cust} | Ret: {ret_cust} | Products: {len(product_set)}")
    return campaign


def build_output(campaigns):
    """Merge same-period campaigns, filter, sort, group by month (same as local)."""
    # Merge identical date ranges
    merged = []
    merged_names = set()
    for i, c1 in enumerate(campaigns):
        if c1["name"] in merged_names:
            continue
        partner = None
        for j, c2 in enumerate(campaigns):
            if i != j and c2["name"] not in merged_names \
               and c1.get("start_date") and c2.get("start_date") \
               and c1["start_date"] == c2["start_date"] \
               and c1["end_date"] == c2["end_date"]:
                partner = c2
                break
        if partner:
            combined = dict(c1)
            combined["name"] = c1["name"] + " & " + partner["name"]
            combined["theme"] = (c1.get("theme", "") + " | " + partner.get("theme", "")).strip(" |")
            combined["revenue"] = max(c1.get("revenue", 0) or 0, partner.get("revenue", 0) or 0)
            combined["net_sales"] = max(c1.get("net_sales", 0) or 0, partner.get("net_sales", 0) or 0)
            combined["orders"] = max(c1.get("orders", 0) or 0, partner.get("orders", 0) or 0)
            combined["new_customers"] = (c1.get("new_customers", 0) or 0) + (partner.get("new_customers", 0) or 0)
            combined["returning_customers"] = (c1.get("returning_customers", 0) or 0) + (partner.get("returning_customers", 0) or 0)
            combined["total_customers"] = combined["new_customers"] + combined["returning_customers"]
            merged_names.add(c1["name"])
            merged_names.add(partner["name"])
            merged.append(combined)
            print(f"  [MERGE] '{c1['name']}' + '{partner['name']}' -> '{combined['name']}'")
        elif c1["name"] not in merged_names:
            merged.append(c1)

    campaigns = merged

    # Filter: completed without dates
    campaigns = [c for c in campaigns if not (
        c.get("status") == "已完成" and (not c.get("start_date") or not c.get("end_date"))
    )]

    # Sort by start_date, then name
    campaigns.sort(key=lambda c: (c.get("start_date") or "9999", c.get("name", "")))

    monthly_groups = {}
    for c in campaigns:
        sd = c.get("start_date")
        if not sd:
            continue
        month_key = sd[:7]
        if month_key not in monthly_groups:
            monthly_groups[month_key] = {
                "month_label": month_key.replace("-", "年") + "月",
                "campaigns": [],
                "total_revenue": 0,
                "total_orders": 0,
            }
        monthly_groups[month_key]["campaigns"].append(c)
        monthly_groups[month_key]["total_revenue"] += c.get("revenue", 0) or 0
        monthly_groups[month_key]["total_orders"] += c.get("orders", 0) or 0

    monthly_groups = dict(sorted(monthly_groups.items()))

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "currency": "USD",
        "total_count": len(campaigns),
        "campaigns": campaigns,
        "monthly_groups": monthly_groups,
    }


def main():
    print("=" * 60)
    print("  EVIL ENERGY Campaign Cloud Compute (Shopify-only)")
    print("=" * 60)

    if not os.path.exists(CONFIG_PATH):
        print(f"  [ERROR] {CONFIG_PATH} not found")
        return 1

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        campaign_config = json.load(f)

    config = load_shop_config()
    if not config:
        return 1

    # Refresh token (client credentials, 24h validity)
    refresh_token_via_client_credentials(config)
    config = load_shop_config()  # reload with fresh token

    shop_tz = get_shop_tz()
    shop_now = datetime.now(shop_tz)
    print(f"  Shopify timezone: {SHOP_TZ_NAME}")
    print(f"  Current Shopify time: {shop_now.strftime('%Y-%m-%d %H:%M:%S')}")

    cfg_campaigns = campaign_config.get("campaigns", [])
    print(f"  Campaigns in config: {len(cfg_campaigns)}")

    campaigns = []
    for cc in cfg_campaigns:
        name = cc.get("name", "?")
        print(f"\n  >> {name}")
        try:
            campaigns.append(compute_campaign(config, cc, shop_now))
        except Exception as e:
            print(f"    [ERROR] compute failed: {e}")
            campaigns.append({
                "name": name,
                "theme": cc.get("theme", ""),
                "level": cc.get("level", ""),
                "type": cc.get("type", ""),
                "start_date": cc.get("start_date", ""),
                "end_date": cc.get("end_date", ""),
                "days": 0, "status": "计算失败",
                "orders": 0, "revenue": 0, "aov": 0,
                "daily_revenue": 0, "daily_orders": 0,
                "conversion_rate": 0, "new_customers": 0,
                "new_customer_rate": "--", "returning_customers": 0,
                "total_customers": 0, "product_count": 0,
                "net_sales": 0, "data_source": "",
            })

    output = build_output(campaigns)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n  [OK] Written {output['total_count']} campaigns to campaign_data.json")
    for mk, mg in output["monthly_groups"].items():
        print(f"    {mk}: {len(mg['campaigns'])} campaigns, ${mg['total_revenue']:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
