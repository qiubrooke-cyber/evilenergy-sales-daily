#!/usr/bin/env python3
"""
Fetch Shopify channel attribution data for campaign dashboard.
Pulls sales data grouped by real Shopify channel dimensions:
  referring_channel + traffic_type + referring_platform

No UTM reverse-engineering — uses Shopify's native channel classification.
Data source: ShopifyQL Analytics (Sales dataset, default attribution = LAST_NON_DIRECT_CLICK).

CI-compatible (2026-08-24): when run from <repo>/scripts/, outputs channel_data.json
to the REPO ROOT and reads shopify_config.json / campaign_data.json from the repo root
(same convention as realtime_fetch.py). Locally (workspace root) behaves as before,
additionally writing deploy/channel_data.json when a deploy/ dir exists.

When no args given, auto-detects the active campaign period from campaign_data.json.

Output: channel_data.json (used by campaign.html channel section)

Usage:
    python fetch_channel_data.py                            # auto-detect active campaign
    python fetch_channel_data.py --start 2026-08-06 --end 2026-08-06
    python fetch_channel_data.py --month 2026-07
    python fetch_channel_data.py --config <path>
"""

import os
import sys
import json
import shutil
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# CI layout: <repo>/scripts/fetch_channel_data.py → repo root is one level up.
# Local layout: <workspace>/fetch_channel_data.py → repo root is the script dir.
REPO_ROOT = os.path.dirname(SCRIPT_DIR) if os.path.basename(SCRIPT_DIR) == "scripts" else SCRIPT_DIR
API_VERSION = "2024-07"
SHOP_TZ_NAME = "America/New_York"

OUTPUT_FILE = os.path.join(REPO_ROOT, "channel_data.json")
DEPLOY_FILE = os.path.join(REPO_ROOT, "deploy", "channel_data.json")


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
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try: err_body = e.read().decode("utf-8")
        except: pass
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


def _find_campaign_data():
    """campaign_data.json: repo root first (CI), then script dir (local)."""
    candidates = [
        os.path.join(REPO_ROOT, "campaign_data.json"),
        os.path.join(SCRIPT_DIR, "campaign_data.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def detect_active_campaign():
    """Read campaign_data.json and find the active (进行中) campaign."""
    campaign_path = _find_campaign_data()
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


def _serialize_campaign(ci):
    """Convert date objects in campaign info to strings."""
    out = {}
    for k, v in ci.items():
        if isinstance(v, date):
            out[k] = v.strftime("%Y-%m-%d")
        else:
            out[k] = v
    return out


def fetch_channel_data(config, start_date, end_date, campaign_info=None):
    """Fetch channel-attributed sales data for the given date range."""
    token = config["access_token"]
    domain = config["shop_domain"]
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    duration = (end_date - start_date).days + 1
    label = f"{start_str} → {end_str}（{duration}天）"

    print(f"  Fetching channel data: {start_str} to {end_str}")

    # Query 1: Total KPI
    ql_total = f"FROM sales SHOW total_sales, net_sales, orders, gross_sales, discounts, shipping_charges, taxes SINCE {start_str} UNTIL {end_str}"
    rows_total = shopifyql_query(domain, token, ql_total)
    total_kpi = {}
    if rows_total:
        r = rows_total[0]
        total_kpi = {
            "total_sales": float(r.get("total_sales", "0")),
            "net_sales": float(r.get("net_sales", "0")),
            "orders": int(r.get("orders", "0")),
            "gross_sales": float(r.get("gross_sales", "0")),
            "discounts": float(r.get("discounts", "0")),
            "shipping_charges": float(r.get("shipping_charges", "0")),
            "taxes": float(r.get("taxes", "0")),
        }

    # Query 2: Channel breakdown
    ql_channel = (
        "FROM sales SHOW total_sales, net_sales, orders "
        "GROUP BY referring_channel, traffic_type, referring_platform "
        f"SINCE {start_str} UNTIL {end_str}"
    )
    rows_channel = shopifyql_query(domain, token, ql_channel)

    # Query 3: Hourly today (for realtime feel)
    ql_hourly = ""
    rows_hourly = []
    if start_date <= end_date:
        ql_hourly = f"FROM sales SHOW orders TIMESERIES hour SINCE {end_str} UNTIL {end_str}"
        rows_hourly = shopifyql_query(domain, token, ql_hourly)

    # Parse channels
    channels = []
    for row in rows_channel:
        ch = row.get("referring_channel") or "Unattributed"
        tt = row.get("traffic_type") or "Unknown"
        rp = row.get("referring_platform") or "Unknown"

        ts = float(row.get("total_sales", "0"))
        ns = float(row.get("net_sales", "0"))
        ords = int(row.get("orders", "0"))

        if ts == 0 and ords == 0:
            continue

        channels.append({
            "referring_channel": ch,
            "traffic_type": tt,
            "referring_platform": rp,
            "total_sales": round(ts, 2),
            "net_sales": round(ns, 2),
            "orders": ords,
            "aov": round(ts / ords, 2) if ords > 0 else 0,
        })

    channels.sort(key=lambda c: -c["total_sales"])

    # Channel rollup
    channel_rollup = {}
    for c in channels:
        key = c["referring_channel"]
        if key not in channel_rollup:
            channel_rollup[key] = {"total_sales": 0, "net_sales": 0, "orders": 0}
        channel_rollup[key]["total_sales"] += c["total_sales"]
        channel_rollup[key]["net_sales"] += c["net_sales"]
        channel_rollup[key]["orders"] += c["orders"]

    channel_rollup_sorted = [
        {"referring_channel": k, "total_sales": round(v["total_sales"], 2),
         "net_sales": round(v["net_sales"], 2), "orders": v["orders"]}
        for k, v in sorted(channel_rollup.items(), key=lambda x: -x[1]["total_sales"])
    ]

    # Traffic type rollup
    traffic_rollup = {}
    for c in channels:
        key = c["traffic_type"]
        if key not in traffic_rollup:
            traffic_rollup[key] = {"total_sales": 0, "net_sales": 0, "orders": 0}
        traffic_rollup[key]["total_sales"] += c["total_sales"]
        traffic_rollup[key]["net_sales"] += c["net_sales"]
        traffic_rollup[key]["orders"] += c["orders"]

    traffic_rollup_sorted = [
        {"traffic_type": k, "total_sales": round(v["total_sales"], 2),
         "net_sales": round(v["net_sales"], 2), "orders": v["orders"]}
        for k, v in sorted(traffic_rollup.items(), key=lambda x: -x[1]["total_sales"])
    ]

    # Verify
    channel_total = sum(c["total_sales"] for c in channels)
    channel_orders = sum(c["orders"] for c in channels)

    # Hourly data
    hourly = []
    for row in rows_hourly:
        hr_str = row.get("hour", "")
        ords = int(row.get("orders", "0"))
        hourly.append({"hour": hr_str, "orders": ords})

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "campaign": _serialize_campaign(campaign_info) if campaign_info else {},
        "period": {
            "start": start_str,
            "end": end_str,
            "duration_days": duration,
            "label": label,
        },
        "attribution_model": "LAST_NON_DIRECT_CLICK (ShopifyQL Sales default)",
        "note": "实时渠道归因 · Shopify 原生维度 · 未使用UTM反推",
        "total": {
            "total_sales": round(total_kpi.get("total_sales", channel_total), 2),
            "net_sales": round(total_kpi.get("net_sales", 0), 2),
            "orders": total_kpi.get("orders", channel_orders),
            "aov": round(total_kpi.get("total_sales", channel_total) / max(total_kpi.get("orders", channel_orders), 1), 2),
        },
        "channels": channels,
        "channel_rollup": channel_rollup_sorted,
        "traffic_rollup": traffic_rollup_sorted,
        "hourly": hourly,
        "updated_at": now_str,
        "channel_count": len(channels),
        "data_source": "shopifyql",
    }


def save_data(data):
    out_files = [OUTPUT_FILE]
    # deploy/channel_data.json only for the local workspace layout (deploy/ exists)
    if os.path.isdir(os.path.dirname(DEPLOY_FILE)):
        out_files.append(DEPLOY_FILE)
    for fp in out_files:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  [OK] Saved to {os.path.basename(os.path.dirname(fp))}/{os.path.basename(fp)}")


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
            # Fallback to today only
            tz = get_shop_tz()
            start_date = datetime.now(tz).date()
            end_date = start_date
            print(f"[Auto] 无进行中活动，默认显示今天: {start_date}")

    if end_date is None:
        tz = get_shop_tz()
        end_date = datetime.now(tz).date()

    config = load_config(config_path)
    if not config["shop_domain"] or not config["access_token"]:
        print("ERROR: Shopify credentials not configured")
        sys.exit(1)

    config["access_token"] = refresh_token(config)

    data = fetch_channel_data(config, start_date, end_date, campaign_info)
    save_data(data)

    # Summary
    total = data["total"]
    camp = data.get("campaign", {})
    if camp.get("name"):
        print(f"[Channel] 活动: {camp['name']}")
    print(f"[Channel] {data['period']['label']}")
    print(f"[Channel] Total: ${total['total_sales']:,.2f} | Net: ${total['net_sales']:,.2f} | Orders: {total['orders']} | AOV: ${total['aov']:,.2f}")
    print(f"[Channel] {data['channel_count']} 渠道维度")
    print(f"[Channel] Top 5:")
    for c in data["channels"][:5]:
        print(f"  {c['referring_channel']}/{c['traffic_type']}: ${c['total_sales']:,.2f} | {c['orders']} orders")

    return 0


if __name__ == "__main__":
    sys.exit(main())
