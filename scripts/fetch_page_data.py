#!/usr/bin/env python3
"""
Fetch page-level performance data for campaign dashboard.
Merges two data sources by landing page path:

  1) ShopifyQL `sessions` dataset GROUP BY landing_page_path
     -> sessions, pageviews, bounce_rate, conversion_rate (traffic metrics)
  2) REST Orders API field `landing_site`
     -> orders, total_sales (Σ total_price) attributed to the landing page

Traffic metrics come from Shopify's native sessions analytics; sales
figures come from order-level landing_site attribution (note: not identical
to ShopifyQL total_sales, but consistent within this module).

CI-compatible: when run from <repo>/scripts/, reads shopify_config.json and
campaign_data.json from the repo root and outputs page_data.json to the repo
root (same convention as fetch_channel_data.py). Locally (workspace root)
behaves the same with the script dir as root.

When no args given, auto-detects the active campaign period from campaign_data.json.

Output: page_data.json (used by campaign.html page section)

Usage:
    python fetch_page_data.py                            # auto-detect active campaign
    python fetch_page_data.py --start 2026-08-06 --end 2026-08-06
    python fetch_page_data.py --month 2026-07
    python fetch_page_data.py --config <path>
"""

import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# CI layout: <repo>/scripts/fetch_page_data.py -> repo root is one level up.
# Local layout: <workspace>/fetch_page_data.py -> repo root is the script dir.
REPO_ROOT = os.path.dirname(SCRIPT_DIR) if os.path.basename(SCRIPT_DIR) == "scripts" else SCRIPT_DIR
API_VERSION = "2024-07"
SHOP_TZ_NAME = "America/New_York"

OUTPUT_FILE = os.path.join(REPO_ROOT, "page_data.json")

# Fixed on-site campaign landing page (tracked separately with history comparison)
DEALS_PATH = "/pages/evilenergy-deals"
DEALS_CACHE_FILE = os.path.join(REPO_ROOT, "deals_history_cache.json")

# Landing page -> page type classification
PAGE_TYPE_LABELS = {
    "home": "首页",
    "product": "产品页",
    "collection": "集合页",
    "page": "自定义页",
    "blog": "博客",
    "cart": "购物车",
    "search": "搜索",
    "policy": "政策页",
    "other": "其他",
    "unattributed": "未记录",
}


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
        while dst_end.weekday() != 6:
            dst_end += timedelta(days=1)
        now_est = now_utc.astimezone(timezone(timedelta(hours=-5)))
        if now_est >= dst_start and now_est < dst_end:
            return timezone(timedelta(hours=-4))
        else:
            return timezone(timedelta(hours=-5))


def _find_config():
    """Find shopify_config.json: repo root first (CI), then script dir (local)."""
    candidates = [
        os.path.join(REPO_ROOT, "shopify_config.json"),
        os.path.join(SCRIPT_DIR, "shopify_config.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def load_config(config_path=None):
    if config_path is None:
        config_path = _find_config()
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    shop = cfg.get("shopify", {})
    return {
        "shop_domain": shop.get("shop_domain", ""),
        "access_token": shop.get("access_token", ""),
        "client_id": shop.get("client_id", ""),
        "client_secret": shop.get("client_secret", ""),
        "config_path": config_path,
    }


def refresh_token(config):
    if not config["client_id"] or not config["client_secret"]:
        return config["access_token"]
    url = f"https://{config['shop_domain']}/admin/oauth/access_token"
    data = json.dumps({
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "client_credentials"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            new_token = result.get("access_token", "")
            if new_token:
                cfg_path = config.get("config_path")
                if cfg_path:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg["shopify"]["access_token"] = new_token
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2, ensure_ascii=False)
                return new_token
    except Exception as e:
        print(f"  Token refresh: {e}")
    return config["access_token"]


def shopifyql_query(shop_domain, access_token, shopifyql_str):
    """Execute ShopifyQL query via GraphQL API."""
    graphql_url = f"https://{shop_domain}/admin/api/{API_VERSION}/graphql.json"
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"  [ShopifyQL Error] HTTP {e.code}: {err_body[:300]}")
        return []
    except Exception as e:
        print(f"  [ShopifyQL Error] {e}")
        return []

    if "errors" in body:
        print(f"  [GraphQL Error] {json.dumps(body['errors'], indent=2)[:300]}")
        return []

    data = body.get("data", {}).get("shopifyqlQuery", {})
    parse_errors = data.get("parseErrors", "")
    if parse_errors:
        print(f"  [ShopifyQL Parse] {parse_errors}")
        return []

    return data.get("tableData", {}).get("rows", [])


def detect_active_campaign():
    """Read campaign_data.json and find the active (进行中) campaign."""
    campaign_path = os.path.join(REPO_ROOT, "campaign_data.json")
    if not os.path.exists(campaign_path):
        return None

    try:
        with open(campaign_path, "r", encoding="utf-8") as f:
            cd = json.load(f)
    except Exception:
        return None

    for c in cd.get("campaigns", []):
        if c.get("status") == "进行中" or c.get("status") == "active":
            start = c.get("start_date")
            end = c.get("end_date")
            if not start:
                return None

            # End date capped at today (Shopify timezone)
            tz = get_shop_tz()
            today = datetime.now(tz).date()

            start_date = None
            end_date = None
            try:
                start_date = datetime.strptime(start[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
            if end and len(end) >= 10:
                try:
                    end_date = datetime.strptime(end[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass

            if start_date is None:
                return None
            if end_date is None or end_date > today:
                end_date = today

            return {
                "name": c.get("name", ""),
                "theme": c.get("theme", ""),
                "start_date": start_date,
                "end_date": end_date,
                "status": c.get("status", "进行中"),
            }

    return None


def detect_previous_campaign(before_date):
    """Find the most recent campaign that ENDED before `before_date`.

    Used as the comparison baseline (上个活动) for the page view.
    """
    campaign_path = os.path.join(REPO_ROOT, "campaign_data.json")
    if not os.path.exists(campaign_path):
        return None

    try:
        with open(campaign_path, "r", encoding="utf-8") as f:
            cd = json.load(f)
    except Exception:
        return None

    best = None  # (campaign_dict, start_date, end_date)
    for c in cd.get("campaigns", []):
        start = c.get("start_date")
        end = c.get("end_date")
        if not start or not end:
            continue
        try:
            start_date = datetime.strptime(start[:10], "%Y-%m-%d").date()
            end_date = datetime.strptime(end[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if end_date < before_date:
            if best is None or start_date > best[1]:
                best = (c, start_date, end_date)

    if best is None:
        return None
    c, sd, ed = best
    return {
        "name": c.get("name", ""),
        "theme": c.get("theme", ""),
        "start_date": sd,
        "end_date": ed,
        "status": c.get("status", "已完成"),
    }


def _serialize_campaign(ci):
    """Convert date objects in campaign info to strings."""
    out = {}
    for k, v in ci.items():
        if isinstance(v, date):
            out[k] = v.strftime("%Y-%m-%d")
        else:
            out[k] = v
    return out


def classify_page(path):
    """Classify a landing page path into a page type."""
    p = (path or "").lower()
    if not p or p == "/":
        return "home"
    if p.startswith("/products/"):
        return "product"
    if p.startswith("/collections/"):
        return "collection"
    if p.startswith("/blogs/"):
        return "blog"
    if p.startswith("/pages/"):
        return "page"
    if p.startswith("/cart"):
        return "cart"
    if p.startswith("/search"):
        return "search"
    if p.startswith("/policies/"):
        return "policy"
    return "other"


def normalize_path(raw):
    """Extract the path part of a landing URL and normalize cart/search pages."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Handle full URLs
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = urlparse(raw).path
    elif raw.startswith("/"):
        raw = raw.split("?", 1)[0].split("#", 1)[0]
    else:
        return None
    if not raw:
        return "/"
    if raw.startswith("/cart"):
        return "/cart"
    if raw.startswith("/search"):
        return "/search"
    return raw


def fetch_sessions_by_page(shop_domain, access_token, start_str, end_str):
    """Query ShopifyQL sessions dataset grouped by landing_page_path."""
    ql = (
        "FROM sessions SHOW sessions, pageviews, bounce_rate, conversion_rate "
        "GROUP BY landing_page_path "
        f"SINCE {start_str} UNTIL {end_str} "
        "ORDER BY sessions DESC LIMIT 1000"
    )
    rows = shopifyql_query(shop_domain, access_token, ql)
    out = {}
    for row in rows:
        path = row.get("landing_page_path") or "/"
        path = normalize_path(path) or "/"
        try:
            sessions = int(float(row.get("sessions", "0")))
        except (TypeError, ValueError):
            sessions = 0
        try:
            pageviews = int(float(row.get("pageviews", "0")))
        except (TypeError, ValueError):
            pageviews = 0
        try:
            bounce_rate = float(row.get("bounce_rate", "0"))
        except (TypeError, ValueError):
            bounce_rate = 0.0
        try:
            conv_rate = float(row.get("conversion_rate", "0"))
        except (TypeError, ValueError):
            conv_rate = 0.0
        if sessions <= 0:
            continue
        out[path] = {
            "sessions": sessions,
            "pageviews": pageviews,
            "bounce_rate": round(bounce_rate, 4),
            "conversion_rate": round(conv_rate, 4),
        }
    return out


def fetch_sessions_total(shop_domain, access_token, start_str, end_str):
    """Query overall sessions metrics for the period (no GROUP BY, exact totals)."""
    ql = (
        "FROM sessions SHOW sessions, pageviews, bounce_rate, conversion_rate "
        f"SINCE {start_str} UNTIL {end_str}"
    )
    rows = shopifyql_query(shop_domain, access_token, ql)
    if not rows:
        return {"sessions": 0, "pageviews": 0, "bounce_rate": None, "conversion_rate": None}
    r = rows[0]
    try:
        sessions = int(float(r.get("sessions", "0")))
    except (TypeError, ValueError):
        sessions = 0
    try:
        pageviews = int(float(r.get("pageviews", "0")))
    except (TypeError, ValueError):
        pageviews = 0
    try:
        bounce_rate = float(r.get("bounce_rate", "0"))
    except (TypeError, ValueError):
        bounce_rate = 0.0
    try:
        conv_rate = float(r.get("conversion_rate", "0"))
    except (TypeError, ValueError):
        conv_rate = 0.0
    return {
        "sessions": sessions,
        "pageviews": pageviews,
        "bounce_rate": round(bounce_rate, 4) if bounce_rate else None,
        "conversion_rate": round(conv_rate, 4) if conv_rate else None,
    }


def fetch_orders_landing(shop_domain, access_token, start_date, end_date, shop_tz):
    """Pull all orders in [start, end] (shop tz) via REST.

    Returns (agg, stats):
      agg[path] = {orders, total_sales, total_discounts}
      stats = {orders, total_sales, total_discounts, subtotal_sum,
               discount_orders, discount_codes: {code: {orders, sales}}}
    """
    start_utc = datetime(start_date.year, start_date.month, start_date.day,
                         tzinfo=shop_tz).astimezone(timezone.utc)
    end_utc = datetime(end_date.year, end_date.month, end_date.day,
                       tzinfo=shop_tz).astimezone(timezone.utc) + timedelta(days=1)

    all_orders = []
    params = {
        "created_at_min": start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "created_at_max": end_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "status": "any",
        "limit": 250,
        "fields": "id,name,created_at,landing_site,total_price,total_discounts,"
                  "subtotal_price,discount_codes,cancelled_at,financial_status",
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
            print(f"  [Error] Orders page {page}: {e}")
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
                        next_url = part[s + 1:e2]
                    break
        if not next_url:
            break
        url = next_url
        page += 1

    # Aggregate by landing path + discount stats
    agg = {}
    stats = {
        "orders": 0,
        "total_sales": 0.0,
        "total_discounts": 0.0,
        "subtotal_sum": 0.0,
        "discount_orders": 0,
        "discount_codes": {},  # code -> {orders, sales}
    }
    for o in all_orders:
        if o.get("cancelled_at"):
            continue
        path = normalize_path(o.get("landing_site"))
        try:
            price = float(o.get("total_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            disc = float(o.get("total_discounts") or 0)
        except (TypeError, ValueError):
            disc = 0.0
        try:
            sub = float(o.get("subtotal_price") or 0)
        except (TypeError, ValueError):
            sub = 0.0

        stats["orders"] += 1
        stats["total_sales"] += price
        stats["total_discounts"] += disc
        stats["subtotal_sum"] += sub
        if disc > 0:
            stats["discount_orders"] += 1
        for dc in (o.get("discount_codes") or []):
            code = (dc.get("code") or "").strip()
            if code:
                c = stats["discount_codes"].setdefault(code, {"orders": 0, "sales": 0.0})
                c["orders"] += 1
                c["sales"] += price

        key = path if path is not None else "__unattributed__"
        if key not in agg:
            agg[key] = {"orders": 0, "total_sales": 0.0, "total_discounts": 0.0}
        agg[key]["orders"] += 1
        agg[key]["total_sales"] += price
        agg[key]["total_discounts"] += disc

    return agg, stats


def fetch_sales_kpi(shop_domain, access_token, start_str, end_str):
    """Full-store sales KPI from ShopifyQL sales dataset (official attribution).

    Includes discount rate = |discounts| / gross_sales.
    """
    ql = (f"FROM sales SHOW total_sales, net_sales, gross_sales, discounts, orders "
          f"SINCE {start_str} UNTIL {end_str}")
    rows = shopifyql_query(shop_domain, access_token, ql)
    if not rows:
        return {"total_sales": 0, "net_sales": 0, "gross_sales": 0, "discounts": 0,
                "orders": 0, "aov": 0, "discount_rate": None}
    r = rows[0]
    total_sales = float(r.get("total_sales", "0") or 0)
    net_sales = float(r.get("net_sales", "0") or 0)
    gross = float(r.get("gross_sales", "0") or 0)
    discounts = abs(float(r.get("discounts", "0") or 0))
    orders = int(float(r.get("orders", "0") or 0))
    return {
        "total_sales": round(total_sales, 2),
        "net_sales": round(net_sales, 2),
        "gross_sales": round(gross, 2),
        "discounts": round(discounts, 2),
        "orders": orders,
        "aov": round(total_sales / orders, 2) if orders > 0 else 0,
        "discount_rate": round(discounts / gross * 100, 2) if gross > 0 else None,
    }


def fetch_deals_sessions(shop_domain, access_token, start_str, end_str):
    """Single-path ShopifyQL query for the fixed deals landing page (WHERE filter)."""
    ql = (f"FROM sessions SHOW sessions, pageviews, bounce_rate, conversion_rate "
          f"WHERE landing_page_path = '{DEALS_PATH}' "
          f"SINCE {start_str} UNTIL {end_str}")
    rows = shopifyql_query(shop_domain, access_token, ql)
    if not rows:
        return {"sessions": 0, "pageviews": 0, "bounce_rate": None, "conversion_rate": None}
    r = rows[0]

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "sessions": int(float(r.get("sessions", "0") or 0)),
        "pageviews": int(float(r.get("pageviews", "0") or 0)),
        "bounce_rate": _f(r.get("bounce_rate")),
        "conversion_rate": _f(r.get("conversion_rate")),
    }


def _load_deals_cache():
    try:
        with open(DEALS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("campaigns"), dict):
            return data
    except Exception:
        pass
    return {"_version": 2, "campaigns": {}}


def _save_deals_cache(cache):
    try:
        with open(DEALS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [Deals] WARN cache save failed: {e}")


def fetch_deals_history(config, before_date):
    """Historical deals-page performance for every ENDED campaign (cached).

    History is static once a campaign ends, so results are cached in
    deals_history_cache.json and only fetched on cache miss.
    Returns list sorted by start date DESC (most recent first).
    """
    campaign_path = os.path.join(REPO_ROOT, "campaign_data.json")
    if not os.path.exists(campaign_path):
        return []

    try:
        with open(campaign_path, "r", encoding="utf-8") as f:
            cd = json.load(f)
    except Exception:
        return []

    domain = config["shop_domain"]
    token = config["access_token"]
    shop_tz = get_shop_tz()
    cache = _load_deals_cache()
    cache_dirty = False
    results = []

    for c in cd.get("campaigns", []):
        name = c.get("name", "")
        start, end = c.get("start_date"), c.get("end_date")
        if not name or not start or not end:
            continue
        try:
            sd = datetime.strptime(start[:10], "%Y-%m-%d").date()
            ed = datetime.strptime(end[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if ed >= before_date:
            continue  # not ended yet (or current campaign) - handled live

        cached = cache["campaigns"].get(name)
        if cached and cached.get("period", {}).get("start") == sd.strftime("%Y-%m-%d"):
            results.append(cached)
            continue

        # Cache miss -> fetch (sessions via WHERE filter + REST orders)
        print(f"  [Deals] fetching history for '{name}' ({sd} → {ed})")
        try:
            sess = fetch_deals_sessions(domain, token, sd.strftime("%Y-%m-%d"),
                                        ed.strftime("%Y-%m-%d"))
            agg, _stats = fetch_orders_landing(domain, token, sd, ed, shop_tz)
            d = agg.get(DEALS_PATH, {"orders": 0, "total_sales": 0.0, "total_discounts": 0.0})
            orders = d.get("orders", 0)
            sales = d.get("total_sales", 0.0)
            entry = {
                "campaign": {
                    "name": name,
                    "theme": c.get("theme", ""),
                    "start_date": sd.strftime("%Y-%m-%d"),
                    "end_date": ed.strftime("%Y-%m-%d"),
                },
                "period": {
                    "start": sd.strftime("%Y-%m-%d"),
                    "end": ed.strftime("%Y-%m-%d"),
                    "duration_days": (ed - sd).days + 1,
                },
                "sessions": sess.get("sessions", 0),
                "pageviews": sess.get("pageviews", 0),
                "conversion_rate": sess.get("conversion_rate"),
                "orders": orders,
                "total_sales": round(sales, 2),
                "aov": round(sales / orders, 2) if orders > 0 else 0,
                "discount_rate": round(d["total_discounts"] / (sales + d["total_discounts"]) * 100, 2)
                                 if (sales + d.get("total_discounts", 0)) > 0 else None,
            }
            cache["campaigns"][name] = entry
            cache_dirty = True
            results.append(entry)
        except Exception as e:
            print(f"  [Deals] WARN history fetch failed for '{name}': {e}")

    if cache_dirty:
        _save_deals_cache(cache)

    results.sort(key=lambda x: x["period"]["start"], reverse=True)
    return results


def fetch_page_data(config, start_date, end_date, campaign_info=None):
    token = config["access_token"]
    domain = config["shop_domain"]
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    duration = (end_date - start_date).days + 1
    label = f"{start_str} → {end_str}（{duration}天）"
    print(f"  Fetching page data: {start_str} to {end_str}")

    # 1) Sessions traffic by landing page (ShopifyQL)
    sessions_map = fetch_sessions_by_page(domain, token, start_str, end_str)
    print(f"  [ShopifyQL sessions] {len(sessions_map)} landing pages")

    # 1b) Overall sessions totals (exact, no GROUP BY truncation)
    sessions_total = fetch_sessions_total(domain, token, start_str, end_str)
    print(f"  [ShopifyQL total] {sessions_total['sessions']:,} sessions, {sessions_total['pageviews']:,} pageviews")

    # 1c) Full-store sales KPI (ShopifyQL sales: total_sales/gross/discounts/orders)
    sales_kpi = fetch_sales_kpi(domain, token, start_str, end_str)
    print(f"  [ShopifyQL sales] ${sales_kpi['total_sales']:,.2f} · {sales_kpi['orders']} orders · "
          f"discount_rate {sales_kpi['discount_rate']}%")

    # 2) Orders by landing site (REST, incl. discount fields)
    shop_tz = get_shop_tz()
    order_agg, ostats = fetch_orders_landing(domain, token, start_date, end_date, shop_tz)
    total_orders = ostats["orders"]
    total_sales = ostats["total_sales"]
    print(f"  [REST orders] {total_orders} orders, ${total_sales:,.2f}, "
          f"discounts ${ostats['total_discounts']:,.2f} ({ostats['discount_orders']} discounted orders)")

    # 3) Merge
    all_paths = set(sessions_map.keys()) | {k for k in order_agg.keys() if k != "__unattributed__"}
    pages = []
    for path in all_paths:
        s = sessions_map.get(path, {})
        o = order_agg.get(path, {})
        page_type = classify_page(path)
        pages.append({
            "landing_page": path,
            "page_type": page_type,
            "page_type_label": PAGE_TYPE_LABELS.get(page_type, page_type),
            "sessions": s.get("sessions", 0),
            "pageviews": s.get("pageviews", 0),
            "bounce_rate": s.get("bounce_rate", None),
            "conversion_rate": s.get("conversion_rate", None),
            "orders": o.get("orders", 0),
            "total_sales": round(o.get("total_sales", 0.0), 2),
            "aov": round(o.get("total_sales", 0.0) / o["orders"], 2) if o.get("orders", 0) > 0 else 0,
        })

    # Unattributed orders (no landing_site recorded)
    unatt = order_agg.get("__unattributed__")
    if unatt and unatt.get("orders", 0) > 0:
        pages.append({
            "landing_page": "(未记录)",
            "page_type": "unattributed",
            "page_type_label": PAGE_TYPE_LABELS["unattributed"],
            "sessions": 0,
            "pageviews": 0,
            "bounce_rate": None,
            "conversion_rate": None,
            "orders": unatt["orders"],
            "total_sales": round(unatt["total_sales"], 2),
            "aov": round(unatt["total_sales"] / unatt["orders"], 2),
        })

    pages.sort(key=lambda p: (-p["sessions"], -p["total_sales"]))

    total_sessions = sessions_total["sessions"] or sum(p["sessions"] for p in pages)
    total_pageviews = sessions_total["pageviews"] or sum(p["pageviews"] for p in pages)

    for p in pages:
        p["session_share_pct"] = round(p["sessions"] / total_sessions * 100, 1) if total_sessions > 0 else 0

    # 4) Page type rollup
    type_rollup = {}
    for p in pages:
        t = p["page_type"]
        if t not in type_rollup:
            type_rollup[t] = {
                "page_type": t,
                "label": PAGE_TYPE_LABELS.get(t, t),
                "page_count": 0, "sessions": 0, "pageviews": 0,
                "orders": 0, "total_sales": 0.0, "bounce_sum": 0.0,
                "conv_sum": 0.0, "bounce_sessions": 0, "conv_sessions": 0,
            }
        r = type_rollup[t]
        r["page_count"] += 1
        r["sessions"] += p["sessions"]
        r["pageviews"] += p["pageviews"]
        r["orders"] += p["orders"]
        r["total_sales"] += p["total_sales"]
        if p["bounce_rate"] is not None:
            r["bounce_sum"] += p["bounce_rate"] * p["sessions"]
            r["bounce_sessions"] += p["sessions"]
        if p["conversion_rate"] is not None:
            r["conv_sum"] += p["conversion_rate"] * p["sessions"]
            r["conv_sessions"] += p["sessions"]

    type_rollup_sorted = []
    for r in type_rollup.values():
        type_rollup_sorted.append({
            "page_type": r["page_type"],
            "label": r["label"],
            "page_count": r["page_count"],
            "sessions": r["sessions"],
            "pageviews": r["pageviews"],
            "bounce_rate": round(r["bounce_sum"] / r["bounce_sessions"], 4) if r["bounce_sessions"] > 0 else None,
            "conversion_rate": round(r["conv_sum"] / r["conv_sessions"], 4) if r["conv_sessions"] > 0 else None,
            "orders": r["orders"],
            "total_sales": round(r["total_sales"], 2),
            "aov": round(r["total_sales"] / r["orders"], 2) if r["orders"] > 0 else 0,
            "session_share_pct": round(r["sessions"] / total_sessions * 100, 1) if total_sessions > 0 else 0,
        })
    type_rollup_sorted.sort(key=lambda r: -r["sessions"])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 5) Deals page spotlight (fixed on-site campaign page, live current period)
    deals_sess = sessions_map.get(DEALS_PATH)
    if deals_sess is None:
        try:
            deals_sess = fetch_deals_sessions(domain, token, start_str, end_str)
        except Exception:
            deals_sess = {"sessions": 0, "pageviews": 0,
                          "bounce_rate": None, "conversion_rate": None}
    deals_orders = order_agg.get(DEALS_PATH, {"orders": 0, "total_sales": 0.0,
                                              "total_discounts": 0.0})
    d_orders = deals_orders.get("orders", 0)
    d_sales = deals_orders.get("total_sales", 0.0)
    d_disc = deals_orders.get("total_discounts", 0.0)
    deals_current = {
        "campaign": _serialize_campaign(campaign_info) if campaign_info else {},
        "period": {
            "start": start_str,
            "end": end_str,
            "duration_days": duration,
        },
        "sessions": deals_sess.get("sessions", 0),
        "pageviews": deals_sess.get("pageviews", 0),
        "bounce_rate": deals_sess.get("bounce_rate"),
        "conversion_rate": deals_sess.get("conversion_rate"),
        "orders": d_orders,
        "total_sales": round(d_sales, 2),
        "aov": round(d_sales / d_orders, 2) if d_orders > 0 else 0,
        "discount_rate": round(d_disc / (d_sales + d_disc) * 100, 2)
                         if (d_sales + d_disc) > 0 else None,
        "sales_share_pct": round(d_sales / sales_kpi["total_sales"] * 100, 2)
                           if sales_kpi.get("total_sales") else None,
        "session_share_pct": round(deals_sess.get("sessions", 0) / total_sessions * 100, 2)
                             if total_sessions > 0 else None,
    }
    print(f"  [Deals] current: {deals_current['sessions']} sessions / "
          f"{d_orders} orders / ${d_sales:,.2f} ({deals_current['sales_share_pct']}% of store)")

    # 6) Discount summary (REST order-level)
    code_list = [{"code": k, "orders": v["orders"], "sales": round(v["sales"], 2)}
                 for k, v in ostats["discount_codes"].items()]
    code_list.sort(key=lambda x: -x["orders"])
    discount_summary = {
        "total_discounts": round(ostats["total_discounts"], 2),
        "discount_orders": ostats["discount_orders"],
        "discount_order_pct": round(ostats["discount_orders"] / total_orders * 100, 1)
                              if total_orders > 0 else 0,
        "discount_rate": round(ostats["total_discounts"] /
                               (ostats["subtotal_sum"] + ostats["total_discounts"]) * 100, 2)
                         if (ostats["subtotal_sum"] + ostats["total_discounts"]) > 0 else None,
        "avg_discount_per_order": round(ostats["total_discounts"] / ostats["discount_orders"], 2)
                                  if ostats["discount_orders"] > 0 else 0,
        "code_count": len(code_list),
        "codes": code_list[:10],
    }

    return {
        "campaign": _serialize_campaign(campaign_info) if campaign_info else {},
        "period": {
            "start": start_str,
            "end": end_str,
            "duration_days": duration,
            "label": label,
        },
        "attribution_note": "流量: ShopifyQL sessions by landing_page_path | 销售: REST Orders by landing_site (实付总额) | 总数据: ShopifyQL sales 全店口径",
        "sales_kpi": sales_kpi,
        "discount_summary": discount_summary,
        "deals_page": {
            "path": DEALS_PATH,
            "current": deals_current,
            "history": [],  # filled by main() via fetch_deals_history()
        },
        "total": {
            "sessions": total_sessions,
            "pageviews": total_pageviews,
            "bounce_rate": sessions_total["bounce_rate"],
            "conversion_rate": sessions_total["conversion_rate"],
            "orders": total_orders,
            "total_sales": round(total_sales, 2),
            "aov": round(total_sales / total_orders, 2) if total_orders > 0 else 0,
            "page_count": len([p for p in pages if p["sessions"] > 0]),
        },
        "pages": pages,
        "page_type_rollup": type_rollup_sorted,
        "updated_at": now_str,
        "page_count": len(pages),
        "data_source": "shopifyql sessions + sales + rest orders",
    }


def fetch_comparison_data(config, prev_camp, prev_start, prev_end, matched_days):
    """Fetch the previous campaign's SAME-PERIOD page data (first N days).

    Returns a lightweight comparison payload: overall KPIs + page-type rollup.
    """
    token = config["access_token"]
    domain = config["shop_domain"]
    start_str = prev_start.strftime("%Y-%m-%d")
    end_str = prev_end.strftime("%Y-%m-%d")

    print(f"  [Compare] Previous campaign: {prev_camp['name']} "
          f"same-period {start_str} → {end_str} (first {matched_days}d)")

    sessions_map = fetch_sessions_by_page(domain, token, start_str, end_str)
    sessions_total = fetch_sessions_total(domain, token, start_str, end_str)
    sales_kpi = fetch_sales_kpi(domain, token, start_str, end_str)
    shop_tz = get_shop_tz()
    order_agg, ostats = fetch_orders_landing(domain, token, prev_start, prev_end, shop_tz)
    total_orders = ostats["orders"]
    total_sales = ostats["total_sales"]
    print(f"  [Compare] {sessions_total['sessions']:,} sessions, "
          f"{total_orders} orders, ${total_sales:,.2f}, "
          f"discount_rate {sales_kpi['discount_rate']}%")

    total_sessions = sessions_total["sessions"] or sum(s.get("sessions", 0) for s in sessions_map.values())

    # Page-type rollup (sessions from ShopifyQL + orders from REST)
    type_rollup = {}
    for path, s in sessions_map.items():
        t = classify_page(path)
        r = type_rollup.setdefault(t, {
            "page_type": t, "label": PAGE_TYPE_LABELS.get(t, t),
            "sessions": 0, "orders": 0, "total_sales": 0.0, "page_count": 0,
        })
        r["sessions"] += s.get("sessions", 0)
        r["page_count"] += 1
    for path, o in order_agg.items():
        if path == "__unattributed__":
            continue
        t = classify_page(path)
        r = type_rollup.setdefault(t, {
            "page_type": t, "label": PAGE_TYPE_LABELS.get(t, t),
            "sessions": 0, "orders": 0, "total_sales": 0.0, "page_count": 0,
        })
        r["orders"] += o.get("orders", 0)
        r["total_sales"] += o.get("total_sales", 0.0)
    unatt = order_agg.get("__unattributed__")
    if unatt and unatt.get("orders", 0) > 0:
        r = type_rollup.setdefault("unattributed", {
            "page_type": "unattributed", "label": PAGE_TYPE_LABELS["unattributed"],
            "sessions": 0, "orders": 0, "total_sales": 0.0, "page_count": 0,
        })
        r["orders"] += unatt["orders"]
        r["total_sales"] += unatt["total_sales"]

    rollup = []
    for r in type_rollup.values():
        rollup.append({
            "page_type": r["page_type"],
            "label": r["label"],
            "page_count": r["page_count"],
            "sessions": r["sessions"],
            "orders": r["orders"],
            "total_sales": round(r["total_sales"], 2),
            "session_share_pct": round(r["sessions"] / total_sessions * 100, 1) if total_sessions > 0 else 0,
        })
    rollup.sort(key=lambda x: -x["sessions"])

    comp_duration = (prev_end - prev_start).days + 1
    return {
        "campaign": _serialize_campaign(prev_camp),
        "period": {
            "start": start_str,
            "end": end_str,
            "duration_days": comp_duration,
            "label": f"{start_str} → {end_str}（{comp_duration}天·同期）",
        },
        "matched_days": matched_days,
        "note": f"对比口径：上个活动「{prev_camp['name']}」同期前 {matched_days} 天累计",
        "total": {
            "sessions": sessions_total["sessions"],
            "pageviews": sessions_total["pageviews"],
            "bounce_rate": sessions_total["bounce_rate"],
            "conversion_rate": sessions_total["conversion_rate"],
            "orders": total_orders,
            "total_sales": round(total_sales, 2),
            "aov": round(total_sales / total_orders, 2) if total_orders > 0 else 0,
        },
        "sales_kpi": sales_kpi,
        "discount_summary": {
            "total_discounts": round(ostats["total_discounts"], 2),
            "discount_orders": ostats["discount_orders"],
            "discount_order_pct": round(ostats["discount_orders"] / total_orders * 100, 1)
                                  if total_orders > 0 else 0,
            "discount_rate": round(ostats["total_discounts"] /
                                   (ostats["subtotal_sum"] + ostats["total_discounts"]) * 100, 2)
                             if (ostats["subtotal_sum"] + ostats["total_discounts"]) > 0 else None,
        },
        "page_type_rollup": rollup,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_data(data):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [OK] Saved to {OUTPUT_FILE}")


def main():
    start_date = None
    end_date = None
    config_path = None
    campaign_info = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--start" and i + 1 < len(args):
            try:
                start_date = datetime.strptime(args[i + 1][:10], "%Y-%m-%d").date()
            except ValueError:
                print(f"Invalid --start date: {args[i + 1]}")
                sys.exit(1)
            i += 2
        elif args[i] == "--end" and i + 1 < len(args):
            try:
                end_date = datetime.strptime(args[i + 1][:10], "%Y-%m-%d").date()
            except ValueError:
                print(f"Invalid --end date: {args[i + 1]}")
                sys.exit(1)
            i += 2
        elif args[i] == "--month" and i + 1 < len(args):
            parts = args[i + 1].split("-")
            year, month = int(parts[0]), int(parts[1])
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = date(year, month + 1, 1) - timedelta(days=1)
            i += 2
        elif args[i] == "--config" and i + 1 < len(args):
            config_path = args[i + 1]
            i += 2
        else:
            i += 1

    # Auto-detect active campaign if no date range specified
    if start_date is None:
        campaign_info = detect_active_campaign()
        if campaign_info:
            start_date = campaign_info["start_date"]
            end_date = campaign_info["end_date"]
            print(f"[Auto] 检测到进行中活动: {campaign_info['name']} ({start_date} → {end_date})")
        else:
            tz = get_shop_tz()
            today = datetime.now(tz).date()
            # Fallback 1: most recently ENDED campaign (within 14 days) -> full period
            recent = detect_previous_campaign(today)
            if recent and (today - recent["end_date"]).days <= 14:
                campaign_info = recent
                start_date = recent["start_date"]
                end_date = recent["end_date"]
                print(f"[Auto] 无进行中活动，回退最近已结束活动全期: "
                      f"{recent['name']} ({start_date} → {end_date})")
            else:
                # Fallback 2: last 7 days
                start_date = today - timedelta(days=6)
                end_date = today
                print(f"[Auto] 无进行中活动，默认最近7天: {start_date} → {end_date}")

    if end_date is None:
        tz = get_shop_tz()
        end_date = datetime.now(tz).date()

    config = load_config(config_path)
    if not config["shop_domain"] or not config["access_token"]:
        print("ERROR: Shopify credentials not configured")
        sys.exit(1)

    config["access_token"] = refresh_token(config)

    data = fetch_page_data(config, start_date, end_date, campaign_info)

    # Comparison with previous campaign (same-period, first N days)
    comparison = None
    prev_camp = detect_previous_campaign(start_date)
    if prev_camp:
        n_days = (end_date - start_date).days + 1
        prev_start = prev_camp["start_date"]
        prev_end = min(prev_start + timedelta(days=n_days - 1), prev_camp["end_date"])
        if prev_end >= prev_start:
            try:
                comparison = fetch_comparison_data(
                    config, prev_camp, prev_start, prev_end, n_days)
            except Exception as ex:
                print(f"  [Compare] WARN comparison fetch failed: {ex}")
                comparison = None
        else:
            print(f"  [Compare] WARN invalid previous period, skip")
    else:
        print("  [Compare] No previous campaign found, skip")

    data["comparison"] = comparison

    # Deals page history comparison (ended campaigns, cached)
    try:
        data["deals_page"]["history"] = fetch_deals_history(config, start_date)
        hist_n = len(data["deals_page"]["history"])
        print(f"[Deals] history campaigns: {hist_n}")
    except Exception as ex:
        print(f"[Deals] WARN history fetch failed: {ex}")
        data["deals_page"]["history"] = []

    save_data(data)

    total = data["total"]
    camp = data.get("campaign", {})
    if camp.get("name"):
        print(f"[Page] 活动: {camp['name']}")
    print(f"[Page] {data['period']['label']}")
    print(f"[Page] Sessions: {total['sessions']:,} | Pageviews: {total['pageviews']:,} | Conv: {total['conversion_rate']}")
    print(f"[Page] Orders: {total['orders']:,} | Sales: ${total['total_sales']:,.2f} | AOV: ${total['aov']:,.2f}")
    print(f"[Page] {len(data['pages'])} landing pages")
    print(f"[Page] Top 8:")
    for p in data["pages"][:8]:
        print(f"  {p['landing_page']}: {p['sessions']:,} sess / ${p['total_sales']:,.2f} / {p['orders']} ord")
    print(f"[Page] Type rollup:")
    for r in data["page_type_rollup"][:6]:
        print(f"  {r['label']}: {r['page_count']}页 / {r['sessions']:,} sess / ${r['total_sales']:,.2f}")

    comp = data.get("comparison")
    if comp:
        ct = comp["total"]
        print(f"[Compare] {comp['note']}")
        print(f"[Compare] Sessions: {ct['sessions']:,} | Orders: {ct['orders']:,} | "
              f"Sales: ${ct['total_sales']:,.2f}")
        cur_t = data["total"]
        for k, fmt in [("sessions", "{:,.0f}"), ("orders", "{:,.0f}"),
                       ("total_sales", "${:,.2f}")]:
            pv, cv = ct.get(k) or 0, cur_t.get(k) or 0
            delta = ((cv - pv) / pv * 100) if pv else None
            ds = f"{delta:+.1f}%" if delta is not None else "n/a"
            print(f"[Compare] {k}: prev {fmt.format(pv)} vs cur {fmt.format(cv)} ({ds})")

    sk = data.get("sales_kpi", {})
    print(f"[KPI] 全店: ${sk.get('total_sales', 0):,.2f} · {sk.get('orders', 0)} orders · "
          f"AOV ${sk.get('aov', 0)} · 折扣率 {sk.get('discount_rate')}% "
          f"(gross ${sk.get('gross_sales', 0):,.2f} - discounts ${sk.get('discounts', 0):,.2f})")
    ds = data.get("discount_summary", {})
    print(f"[KPI] 折扣: {ds.get('discount_orders', 0)} 折扣单 ({ds.get('discount_order_pct', 0)}%) · "
          f"码 {ds.get('code_count', 0)} 个 · TOP: "
          + (", ".join(f"{c['code']}({c['orders']})" for c in (ds.get("codes") or [])[:3]) or "无"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
