#!/usr/bin/env python3
"""
update_github.py — GitHub Actions version of update_realtime_deploy.py

Runs inside the GitHub repo (repo root = cwd):
1. Run realtime_fetch.py (fetches fresh Shopify data)
2. Merge today's realtime data into dashboard_all.json
3. Run build_standalone.js (embeds data into index.html)

No deploy copy step needed — the repo IS the deploy directory.
No git push needed — the workflow handles that separately.

Usage:
    python scripts/update_github.py
"""

import os
import sys
import json
import subprocess
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
REALTIME_PATH = os.path.join(REPO_ROOT, "dashboard_realtime.json")
ALL_DATA_PATH = os.path.join(REPO_ROOT, "dashboard_all.json")
INDEX_PATH = os.path.join(REPO_ROOT, "index.html")
BUILD_SCRIPT = os.path.join(SCRIPTS_DIR, "build_standalone.js")
REALTIME_SCRIPT = os.path.join(SCRIPTS_DIR, "realtime_fetch.py")


def step1_fetch_realtime():
    """Run realtime_fetch.py to get fresh Shopify data."""
    print("=" * 60)
    print("  Step 1: Fetch realtime data from Shopify")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, REALTIME_SCRIPT],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"  [WARNING] realtime_fetch.py returned {result.returncode}")
        print(f"  stderr: {result.stderr[:500]}")
        return False
    return os.path.exists(REALTIME_PATH)


def step2_merge_into_all_data():
    """Merge today's realtime data into dashboard_all.json."""
    print("\n" + "=" * 60)
    print("  Step 2: Merge realtime data into dashboard_all.json")
    print("=" * 60)

    if not os.path.exists(REALTIME_PATH):
        print("  [SKIP] dashboard_realtime.json not found")
        return False

    with open(REALTIME_PATH, "r", encoding="utf-8") as f:
        realtime = json.load(f)

    with open(ALL_DATA_PATH, "r", encoding="utf-8") as f:
        all_data = json.load(f)

    today_str = realtime.get("date", "")
    if not today_str:
        print("  [SKIP] No date in realtime data")
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
        "top_products": [
            {
                "title": p.get("title") or p.get("name", "Unknown"),
                "sku": p.get("sku", ""),
                "qty": p.get("qty") or p.get("quantity", 0),
                "revenue": p.get("revenue", 0),
            }
            for p in realtime.get("top_products", [])
        ],
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
        all_data["date_range"] = {
            "start": all_dates[0],
            "end": all_dates[-1],
        }

    all_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(ALL_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print(f"  [OK] Merged {today_str}: {today_entry['total_orders']} orders, ${today_entry['total_revenue']:,.2f}")
    print(f"  [OK] dashboard_all.json now has {len(all_dates)} dates ({all_dates[0]} to {all_dates[-1]})")
    return True


def step3_build_standalone(node_exe="node"):
    """Run build_standalone.js to create self-contained HTML."""
    print("\n" + "=" * 60)
    print("  Step 3: Build standalone HTML (index.html)")
    print("=" * 60)
    result = subprocess.run(
        [node_exe, BUILD_SCRIPT],
        capture_output=True, text=True, timeout=30,
        cwd=REPO_ROOT,
    )
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

    # Clean up shopify_config.json (contains secrets — should not be committed)
    config_path = os.path.join(REPO_ROOT, "shopify_config.json")
    if os.path.exists(config_path):
        os.remove(config_path)
        print("\n  [OK] shopify_config.json removed (secrets cleanup)")

    print("\n" + "=" * 60)
    print("  ALL STEPS COMPLETE")
    print("  index.html updated with latest Shopify data")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
