/**
 * build_standalone.js (GitHub Actions version)
 *
 * Reads dashboard.html + dashboard_all.json + shopify_config.json from the
 * repo root, embeds the JSON data directly into the HTML, and outputs
 * index.html (self-contained dashboard for GitHub Pages).
 *
 * Usage: node scripts/build_standalone.js
 */

const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const HTML_PATH = path.join(REPO_ROOT, 'dashboard.html');
const JSON_PATH = path.join(REPO_ROOT, 'dashboard_all.json');
const CONFIG_PATH = path.join(REPO_ROOT, 'shopify_config.json');
const OUTPUT_PATH = path.join(REPO_ROOT, 'index.html');

try {
  let html = fs.readFileSync(HTML_PATH, 'utf-8');
  const jsonStr = fs.readFileSync(JSON_PATH, 'utf-8');
  const configStr = fs.readFileSync(CONFIG_PATH, 'utf-8');

  // Validate JSON
  JSON.parse(jsonStr);
  const config = JSON.parse(configStr);
  const shopify = config.shopify || {};

  // Step 1: Inject Shopify config as window.__SHOPIFY_CONFIG__
  const stateSectionIdx = html.indexOf('// ─────────────────────────── State');
  if (stateSectionIdx === -1) {
    console.error('[Build] Could not find State section in HTML.');
    process.exit(1);
  }
  const scriptStart = html.lastIndexOf('<script>', stateSectionIdx);

  const shopifyConfigJs = `<script>
    // Shopify config for browser-side API calls (CloudStudio realtime)
    // Note: client_secret intentionally excluded — browser CORS blocks Shopify API anyway,
    // and embedding secrets in public repos triggers GitHub push protection.
    window.__SHOPIFY_CONFIG__ = ${JSON.stringify({
      shop_domain: shopify.shop_domain || '',
      client_id: shopify.client_id || '',
      iana_timezone: shopify.iana_timezone || 'America/New_York',
    })};
  </script>\n`;

  html = html.substring(0, scriptStart) + shopifyConfigJs + html.substring(scriptStart);

  // Step 2: Replace init block with standalone version
  const initStartMarker = '    // ─────────────────────────── Init ───────────────────────────';
  const initEndMarker = '    })();';

  const startIdx = html.indexOf(initStartMarker);
  if (startIdx === -1) {
    console.error('[Build] Could not find the init start marker. Dashboard HTML may have changed.');
    process.exit(1);
  }

  const endIdx = html.indexOf(initEndMarker, startIdx);
  if (endIdx === -1) {
    console.error('[Build] Could not find the init end marker.');
    process.exit(1);
  }

  const newInit = `    // ─────────────────────────── Init (standalone) ───────────────────────────
    (async function() {
      console.log('[Dashboard] Starting init (standalone mode)...');

      // Historical data is embedded directly (includes today's merged realtime data)
      allData = __DASHBOARD_DATA__;

      if (allData && allData.dates) {
        dates = Object.keys(allData.dates).sort();
        document.getElementById('rangeStart').textContent = allData.date_range?.start || dates[0];
        document.getElementById('rangeEnd').textContent = allData.date_range?.end || dates[dates.length - 1];
        document.getElementById('rangeCount').textContent = dates.length + ' \u5929';
      }

      // Show most recent date (includes today's data, updated every 30 min by GitHub Actions)
      if (dates.length > 0) {
        setHistoricalDate(dates.length - 1);
        const lastDate = dates[dates.length - 1];
        const dst = getDstName();
        const modeBadge = document.getElementById('modeBadge');
        if (modeBadge) {
          modeBadge.textContent = '\u{1F4C5} ' + lastDate + ' \u00B7 ' + dst + ' \u00B7 GitHub Actions \u81EA\u52A8\u66F4\u65B0';
          modeBadge.className = 'mode-badge hist';
        }
        const titleEl = document.getElementById('pageTitle');
        if (titleEl) titleEl.textContent = 'EVIL ENERGY \u6BCF\u65E5\u4E1A\u7EE9 \u00B7 \u4E91\u7AEF\u7248';
      } else {
        document.querySelectorAll('.kpi-value').forEach(el => el.textContent = '--');
      }

      // Try browser-side Shopify API for live refresh (may fail due to CORS — that's OK)
      try {
        await refreshRealtimeData();
        if (realtimeData) {
          showRealtimeMode();
          document.getElementById('rangeCount').textContent = dates.length + ' \u5929 + \u5B9E\u65F6';
        }
      } catch (e) {
        console.log('[Dashboard] Browser Shopify API not available (CORS), using embedded data');
      }

      console.log('[Dashboard] Init complete (standalone), dates:', dates.length, 'realtime:', !!realtimeData);
    })();`;

  html = html.substring(0, startIdx) + newInit + html.substring(endIdx + initEndMarker.length);

  // Step 3: Embed JSON data
  html = html.replace('__DASHBOARD_DATA__', jsonStr);

  // Step 4: Write output
  fs.writeFileSync(OUTPUT_PATH, html, 'utf-8');

  const sizeKB = (Buffer.byteLength(html, 'utf-8') / 1024).toFixed(1);
  console.log('[Build] Standalone dashboard created: ' + OUTPUT_PATH);
  console.log('[Build] Size: ' + sizeKB + ' KB');
  console.log('[Build] Features: embedded historical data + browser-side Shopify realtime API');
} catch (err) {
  console.error('[Build] Error:', err.message);
  process.exit(1);
}
