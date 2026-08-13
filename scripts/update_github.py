#!/usr/bin/env python3
"""
update_github.py - GitHub Actions version with backfill logic.
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
REALTIME_PATH = os.path.join(REPO_ROOT, "dashboard_realtime.json")
ALL_DATA_PATH = os.path.join(REPO_ROOT, "dashboard_all.json")
INDEX_PATH = os.path.join(REPO_ROOT, "index.html")
BUILD_SCRIPT = os.path.join(SCRIPTS_DIR, "build_standalone.js")
REALTIME_SCRIPT = os.path.join(SCRIPTS_DIR, "realtime_fetch.py")

BACKFILL_DAYS = 3


def run_realtime_for_date(date_str=None):
    cmd = [sys.executable, REALTIME_SCRIPT]
    if date_str:
        cmd.extend(["--date", date_str])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)


def step1_fetch_realtime():
    print("=" * 60)
    print("  Step 1: Fetch realtime data from Shopify (today)")
    print("=" * 60)
    result = run_realtime_for_date()
    print(result.stdout)
    if result.returncode != 0:
        print(f"  [WARNING] realtime_fetch.py returned {result.returncode}")
        return False
    return os.path.exists(REALTIME_PATH)


def merge_realtime_into_all_data():
    if not os.path.exists(REALTIME_PATH):
        return False
    with open(REALTIME_PATH, "r", encoding="utf-8") as f:
        realtime = json.load(f)
    with open(ALL_DATA_PATH, "r", encoding="utf-8") as f:
        all_data = json.load(f)
    today_str = realtime.get("date", "")
    if not today_str:
        return False
    kpi = realtime.get("kpi", {})
    today_entry = {
        "date": today_str,
        "total_orders": kpi.get("total_orders", 0),
        "total_revenue": kpi.get("total_revenue", 0),
        "total_tax": kpi.get("total_tax", 0),
        "total_shipping": kpi.get("total_shipping", 0),
        "total_discounts": kpi.get("total_discounts", 0),
        "net_sales": kpi.get("net_sales", 0),
        "gross_sales": kpi.get("gross_sales", 0),
        "returns": kpi.get("returns", 0),
        "avg_order_value": kpi.get("avg_order_value", 0),
        "total_customers": kpi.get("total_customers", 0),
        "new_customers": kpi.get("new_customers", 0),
        "returning_customers": kpi.get("returning_customers", 0),
        "new_customer_rate": kpi.get("new_customer_rate", 0),
        "hourly_orders": realtime.get("hourly_orders", [0] * 24),
        "top_products": [{"title": p.get("title") or p.get("name", "Unknown"), "sku": p.get("sku", ""), "qty": p.get("qty") or p.get("quantity", 0), "revenue": p.get("revenue", 0)} for p in realtime.get("top_products", [])],
        "discount_codes": realtime.get("discount_codes", []),
        "data_source": realtime.get("data_source", "shopifyql"),
        "is_realtime": True,
        "updated_at": realtime.get("updated_at", ""),
    }
    if "dates" not in all_data:
        all_data["dates"] = {}
    all_data["dates"][today_str] = today_entry
    all_dates = sorted(all_data["dates"].keys())
    if all_dates:
        all_data["date_range"] = {"start": all_dates[0], "end": all_dates[-1]}
    all_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ALL_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    print(f"  [OK] Merged {today_str}: {today_entry['total_orders']} orders, ${today_entry['total_revenue']:,.2f}")
    return True


def step2_merge_into_all_data():
    print("\n" + "=" * 60)
    print("  Step 2: Merge + backfill dashboard_all.json")
    print("=" * 60)
    if not merge_realtime_into_all_data():
        return False
    print(f"\n  [Backfill] Checking previous {BACKFILL_DAYS} days for missing/stale data...")
    today = datetime.now()
    for i in range(1, BACKFILL_DAYS + 1):
        target = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        with open(ALL_DATA_PATH, "r", encoding="utf-8") as f:
            all_data = json.load(f)
        existing = all_data.get("dates", {}).get(target)
        need_fetch = (not existing) or (existing.get("is_realtime") and existing.get("total_orders", 0) < 10)
        if not need_fetch:
            print(f"    {target}: OK ({existing.get('total_orders', 0)} orders), skip")
            continue
        print(f"    {target}: missing/stale, fetching from Shopify...")
        res = run_realtime_for_date(target)
        if res.returncode == 0 and os.path.exists(REALTIME_PATH):
            merge_realtime_into_all_data()
            with open(REALTIME_PATH, "r", encoding="utf-8") as f:
                rt = json.load(f)
            print(f"    {target}: updated to {rt.get('kpi', {}).get('total_orders', 0)} orders")
        else:
            print(f"    {target}: fetch failed, skipping")
    return True


def step3_build_standalone(node_exe="node"):
    print("\n" + "=" * 60)
    print("  Step 3: Build standalone HTML (index.html)")
    print("=" * 60)
    result = subprocess.run([node_exe, BUILD_SCRIPT], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
    print(result.stdout)
    if result.returncode != 0:
        print(f"  [ERROR] build_standalone.js failed: {result.stderr}")
        return False
    return os.path.exists(INDEX_PATH)


def main():
    print("\n" + "=" * 60)
    print("  EVIL ENERGY Dashboard - GitHub Actions Update")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60 + "\n")
    if not step1_fetch_realtime():
        print("\n[FATAL] Step 1 failed, aborting.")
        sys.exit(1)
    if not step2_merge_into_all_data():
        print("\n[FATAL] Step 2 failed, aborting.")
        sys.exit(1)
    node_exe = os.environ.get("NODE_EXE", "node")
    if not step3_build_standalone(node_exe):
        print("\n[FATAL] Step 3 failed, aborting.")
        sys.exit(1)
    config_path = os.path.join(REPO_ROOT, "shopify_config.json")
    if os.path.exists(config_path):
        os.remove(config_path)
        print("\n  [OK] shopify_config.json removed (secrets cleanup)")
    print("\n" + "=" * 60)
    print("  ALL STEPS COMPLETE")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
