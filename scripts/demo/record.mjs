// Scripted walkthrough of the tracker, captured as a frame-accurate screencast.
// Runs inside the recorder image against a throwaway demo container (see README.md).
//
//   node record.mjs            → frames + timings in $OUT (default /out)
//   SHOTS=1 node record.mjs    → dry run: one screenshot per scene, no screencast
//
// The page gets a fake cursor and caption banner injected, since headless
// Chromium draws neither. Every click goes through the real UI.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const BASE = process.env.BASE_URL || "http://networth-demo:8000";
const PASSWORD = process.env.DEMO_PASSWORD || "demo-passphrase-2026";
const SHEET = process.env.DEMO_SHEET || "/rec/src/demo-trades.xlsx";
const OUT = process.env.OUT || "/out";
const SHOTS = process.env.SHOTS === "1";
const W = 1280, H = 800, DPR = 1.5;

fs.mkdirSync(path.join(OUT, SHOTS ? "shots" : "frames"), { recursive: true });

// ───────────── overlay (cursor + captions), re-injected on every document ─────────────
const OVERLAY = () => {
  const ensure = () => {
    if (!document.body || document.getElementById("__demo_cursor")) return;
    const st = document.createElement("style");
    st.id = "__demo_style";
    st.textContent = `
      #__demo_cursor{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
        transition:transform .6s cubic-bezier(.45,.05,.25,1);filter:drop-shadow(0 2px 3px rgba(0,0,0,.35))}
      .__demo_ripple{position:fixed;width:36px;height:36px;margin:-18px 0 0 -18px;border-radius:50%;z-index:2147483646;
        pointer-events:none;background:rgba(37,99,235,.35);animation:__demo_r .5s ease-out forwards}
      @keyframes __demo_r{from{transform:scale(.3);opacity:1}to{transform:scale(1.6);opacity:0}}
      #__demo_cap{position:fixed;left:50%;bottom:28px;transform:translate(-50%,12px);z-index:2147483645;pointer-events:none;
        max-width:900px;padding:14px 24px;border-radius:14px;background:rgba(15,23,42,.92);color:#fff;text-align:center;
        font:600 21px/1.35 Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.25);opacity:0;
        transition:opacity .35s ease,transform .35s ease}
      #__demo_cap.on{opacity:1;transform:translate(-50%,0)}
      #__demo_cap small{display:block;margin-top:3px;font-weight:400;font-size:15px;color:#cbd5e1}
      /* The app's undo toast also sits bottom-centre; lift it above the caption. */
      .toast-live{bottom:128px !important}`;
    document.head.appendChild(st);
    const c = document.createElement("div");
    c.id = "__demo_cursor";
    c.innerHTML = `<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l15 10.5-6.6 1.2 3.9 7.3-2.9 1.5-3.9-7.4L4 20z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
    const p = JSON.parse(sessionStorage.getItem("__demo_pos") || "[640,400]");
    c.style.transition = "none";
    c.style.transform = `translate(${p[0]}px,${p[1]}px)`;
    if (/^\/__demo\//.test(location.pathname)) c.style.display = "none";
    document.body.appendChild(c);
    const cap = document.createElement("div");
    cap.id = "__demo_cap";
    document.body.appendChild(cap);
    const saved = sessionStorage.getItem("__demo_cap_html");
    if (saved) { cap.innerHTML = saved; cap.classList.add("on"); }
  };
  window.__demo = {
    move(x, y, ms = 600) {
      ensure();
      const c = document.getElementById("__demo_cursor");
      c.style.transition = `transform ${ms}ms cubic-bezier(.45,.05,.25,1)`;
      c.style.transform = `translate(${x}px,${y}px)`;
      sessionStorage.setItem("__demo_pos", JSON.stringify([x, y]));
    },
    ripple(x, y) {
      const r = document.createElement("div");
      r.className = "__demo_ripple";
      r.style.left = x + "px"; r.style.top = y + "px";
      document.body.appendChild(r);
      setTimeout(() => r.remove(), 600);
    },
    caption(html) {
      ensure();
      const cap = document.getElementById("__demo_cap");
      if (!html) { cap.classList.remove("on"); sessionStorage.removeItem("__demo_cap_html"); return; }
      cap.innerHTML = html; cap.classList.add("on");
      sessionStorage.setItem("__demo_cap_html", html);
    },
  };
  setInterval(ensure, 200);
  document.addEventListener("DOMContentLoaded", ensure);
};

// ───────────── helpers ─────────────
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let page;
let shot = 0;
const marks = [];

async function caption(title, sub) {
  await page.evaluate((h) => window.__demo.caption(h), title ? `${title}${sub ? `<small>${sub}</small>` : ""}` : "");
}

async function scene(name) {
  if (SHOTS) await page.screenshot({ path: path.join(OUT, "shots", `${String(++shot).padStart(2, "0")}-${name}.png`) });
  marks.push({ name, t: Date.now() / 1000 });
  console.log(`scene: ${name}`);
}

/** Scroll the element into view if it isn't comfortably on screen. The app closes
 *  open ⋯ menus on any scroll, so scrolling always finishes before a click. */
async function reveal(loc) {
  await loc.waitFor({ state: "visible" });
  const box = await loc.boundingBox();
  if (box && box.y > 90 && box.y + box.height < H - 150) return;
  await loc.evaluate((el) => {
    const r = el.getBoundingClientRect();
    window.scrollBy({ top: r.top - (window.innerHeight / 2 - r.height / 2), behavior: "smooth" });
  });
  await sleep(850);
}

async function pointAt(loc, ms = 480, scroll = true) {
  if (scroll) await reveal(loc);
  const b = await loc.boundingBox();
  const x = b.x + Math.min(b.width / 2, 40), y = b.y + b.height / 2;
  await page.evaluate(([x, y, ms]) => window.__demo.move(x, y, ms), [x, y, ms]);
  await sleep(ms + 80);
  return { x, y };
}

async function click(loc, after = 500) {
  const { x, y } = await pointAt(loc);
  await page.evaluate(([x, y]) => window.__demo.ripple(x, y), [x, y]);
  await loc.click();
  await sleep(after);
}

async function type(loc, text, delay = 45) {
  await click(loc, 150);
  await loc.fill("");
  await loc.pressSequentially(text, { delay });
  await sleep(300);
}

async function choose(loc, value, after = 450) {
  await pointAt(loc);
  await loc.selectOption(value);
  await sleep(after);
}

/** Pick the option whose text matches `re` (labels carry live balances). */
async function chooseText(loc, re, after = 450) {
  const v = await loc.locator("option").evaluateAll((os, src) => {
    const r = new RegExp(src);
    const o = os.find((o) => r.test(o.textContent));
    return o ? o.value : null;
  }, re.source);
  if (v == null) throw new Error(`no option matching ${re}`);
  await choose(loc, v, after);
}

async function hover(loc, after = 1600) {
  await pointAt(loc);
  await loc.hover();
  await sleep(after);
}

async function scrollBy(dy, after = 900) {
  await page.evaluate((dy) => window.scrollBy({ top: dy, behavior: "smooth" }), dy);
  await sleep(after);
}

async function scrollTop(after = 600) {
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await sleep(after);
}

const nav = (name) => page.locator("header nav").getByRole("link", { name, exact: true });

// ───────────── title / end cards, served on the app's origin ─────────────
const CARD_CSS = `
  *{box-sizing:border-box;margin:0}
  body{height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px;
    background:radial-gradient(1200px 600px at 30% 20%,#14532d 0%,#0f172a 62%);color:#fff;font-family:Inter,system-ui,sans-serif}
  h1{font-size:68px;font-weight:800;letter-spacing:-2px}
  p{font-size:26px;color:#cbd5e1;max-width:920px;text-align:center;line-height:1.4}
  .tag{margin-top:10px;font-size:16px;color:#86efac;border:1px solid #334155;border-radius:999px;padding:6px 16px}
  ul{list-style:none;display:flex;gap:14px;flex-wrap:wrap;justify-content:center;margin-top:8px;padding:0;max-width:1000px}
  li{font-size:17px;color:#e2e8f0;background:#1e293b;border-radius:10px;padding:8px 14px}`;
const TITLE = `<!doctype html><meta charset="utf-8"><title>Nine Buckets</title><style>${CARD_CSS}</style>
  <h1>Nine Buckets</h1>
  <p>A self-hosted balance sheet: everything you own and owe, in any currency, grouped into buckets with allocation targets</p>
  <span class="tag">Demo · all data is made up</span>`;
const END = `<!doctype html><meta charset="utf-8"><title>Nine Buckets</title><style>${CARD_CSS}</style>
  <h1>Nine Buckets</h1>
  <p>Self-hosted with one <b>docker compose up</b>. No bank logins, no cloud — your data stays on your machine.</p>
  <ul><li>Live stock, crypto &amp; metal prices</li><li>Multi-currency</li><li>FIFO cost basis</li>
  <li>Targets &amp; rebalancing</li><li>Spreadsheet import</li><li>History &amp; snapshots</li><li>Backups &amp; restore</li></ul>`;

// ───────────── the walkthrough ─────────────
async function run() {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: W, height: H }, deviceScaleFactor: DPR, locale: "en-US", timezoneId: "UTC",
  });
  await context.addInitScript(OVERLAY);
  for (const [p, body] of [["/__demo/title", TITLE], ["/__demo/end", END]]) {
    await context.route(`${BASE}${p}`, (r) => r.fulfill({ contentType: "text/html; charset=utf-8", body }));
  }
  page = await context.newPage();
  page.on("dialog", (d) => d.accept());
  page.on("pageerror", (e) => console.error("pageerror:", e.message));

  const frames = [];
  let cdp;
  if (!SHOTS) {
    cdp = await context.newCDPSession(page);
    let n = 0;
    cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
      const file = `f${String(n++).padStart(6, "0")}.jpg`;
      fs.writeFileSync(path.join(OUT, "frames", file), Buffer.from(data, "base64"));
      frames.push({ file, t: metadata.timestamp });
      cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
    });
  }

  await page.goto(`${BASE}/__demo/title`);
  await sleep(300);
  if (cdp) await cdp.send("Page.startScreencast", { format: "jpeg", quality: 90, maxWidth: W * DPR, maxHeight: H * DPR });
  await sleep(3400);
  await scene("title");

  // 1 ─ Sign in
  await page.goto(`${BASE}/login`);
  await caption("One passphrase in front of everything", "Self-hosted: it runs in Docker on your own machine or NAS");
  await type(page.locator('input[name="password"]'), PASSWORD, 30);
  await scene("login");
  await page.locator('input[name="password"]').press("Enter");
  await page.waitForURL(`${BASE}/`);

  // 2 ─ Dashboard
  await caption("Your net worth at a glance", "Every account and currency, converted to USD with live FX");
  await sleep(2400);
  await scene("dashboard");
  await caption("Nine Buckets: where your money actually sits", "Actual allocation vs. your targets, with drift highlighted");
  await scrollBy(560, 2600);
  await scrollBy(330, 2000);
  await scene("dashboard-buckets");

  // 3 ─ Assets & liabilities: inline editing
  await scrollTop(300);
  await click(nav("Assets & Liabilities"), 1200);
  await caption("Assets and liabilities, edited in place", "Click a balance, type, press Enter: no forms, no page reloads");
  await sleep(1200);
  const everyday = page.getByLabel("Value of Everyday account");
  await type(everyday, "4105.27", 110);
  await everyday.press("Enter");
  await sleep(700);
  await caption("Every save comes with Undo", "Net worth and bucket totals update instantly");
  await pointAt(page.locator(".toast-live button"), 480, false);  // fixed-position: never scroll for it
  await sleep(1300);
  await scene("inline-edit");
  {
    const undo = page.locator(".toast-live button");
    const { x, y } = await pointAt(undo, 300, false);
    await page.evaluate(([x, y]) => window.__demo.ripple(x, y), [x, y]);
    await undo.click();
    await sleep(1600);
  }

  // ⋯ row menus
  await caption("Row actions live behind a ⋯ menu", "Fetch one price, edit or delete without cluttering the table");
  await click(page.getByRole("button", { name: "Actions for Bitcoin" }), 900);
  await scene("row-menu");
  await click(page.locator(".menu-list:not([hidden])").getByRole("button", { name: "Fetch price" }), 1800);

  // Move money
  await caption("Move money between accounts", "Net worth stays the same; only where the money sits changes. Reversible.");
  await click(page.getByRole("button", { name: "⇄ Move money" }), 700);
  const xfer = page.locator("#xferpanel form");
  await chooseText(xfer.locator('select[name="from_item"]'), /Everyday account/);
  await chooseText(xfer.locator('select[name="to_item"]'), /Settlement fund/);
  await type(xfer.locator('input[name="amount"]'), "750", 120);
  await type(xfer.locator('input[name="note"]'), "funded the brokerage", 30);
  await scene("move-money");
  await click(xfer.getByRole("button", { name: "Move money" }), 1600);
  await pointAt(page.locator(".toast.ok").first());
  await sleep(1600);

  // Real estate equity
  await caption("Real estate counts as equity", "Current value minus the mortgage, with payoff progress");
  await pointAt(page.locator("#property-table tbody tr").first().locator("strong"));
  await sleep(2600);
  await scene("property");

  // 4 ─ Holdings
  await scrollTop(300);
  await click(nav("Holdings"), 1300);
  await caption("Holdings with live prices", "Stocks from Yahoo, crypto from CoinGecko, metals at spot. Cost basis by FIFO.");
  await sleep(2600);
  await scene("holdings");
  await caption("Record a purchase in the lot ledger", "Buying debits the cash it was paid from, so nothing is counted twice");
  const vt = page.locator("#securities-table tr", { hasText: "Global index ETF" });
  await click(vt.getByRole("button", { name: /lots/ }), 900);
  const ledger = page.locator("tr.ledger-row:not([hidden])").first();
  await choose(ledger.locator('select[name="side"]'), "buy", 300);
  await pointAt(ledger.locator('input[name="trade_date"]'));
  await ledger.locator('input[name="trade_date"]').fill("2026-10-15");
  await sleep(400);
  await type(ledger.locator('input[name="quantity"]'), "4", 120);
  await type(ledger.locator('input[name="price"]'), "157.12", 90);
  await type(ledger.locator('input[name="fee"]'), "0.85", 90);
  await chooseText(ledger.locator('select[name="funded_from"]'), /Settlement fund/, 700);
  await scene("lot-form");
  await click(ledger.getByRole("button", { name: "Add lot" }), 1500);
  await pointAt(page.locator(".toast.ok").first());
  await sleep(1500);

  await caption("Precious metals at spot, dealer premium tracked separately", "Grams, kilos and ounces in one ledger");
  await pointAt(page.locator("#metals-table tbody tr").first());
  await sleep(2600);
  await scene("metals");

  await caption("Every trade in one table", "Filter by side or holding, sort any column, page through it");
  const tx = page.locator("#tx-table");
  await reveal(page.getByLabel("Filter by side"));
  await choose(page.getByLabel("Filter by side"), "sell", 1300);
  await choose(page.getByLabel("Filter by side"), "", 500);
  await choose(page.getByLabel("Filter by holding"), "Bitcoin", 1500);
  await scene("transactions");
  await choose(page.getByLabel("Filter by holding"), "", 600);
  await click(tx.locator("th", { hasText: "Value (USD)" }), 1400);

  // 5 ─ Import
  await scrollTop(300);
  await caption("Import trade history from your own spreadsheet", "Previewed and reconciled before anything is written");
  await click(page.getByRole("link", { name: /Import trade history/ }), 1300);
  await page.locator('input[type="file"]').setInputFiles(SHEET);
  await pointAt(page.locator('input[type="file"]'));
  await sleep(700);
  await click(page.getByRole("button", { name: "Read spreadsheet" }), 1400);
  await caption("Quantities checked against the sheet, run through the same FIFO", "Nothing changes until you type IMPORT, and every import can be rolled back");
  await pointAt(page.locator("#import-preview"));
  await sleep(3200);
  await scene("import-preview");
  await click(page.getByRole("button", { name: "Discard staged file" }), 900);

  // 6 ─ Accounts
  await scrollTop(300);
  await click(nav("Accounts"), 1300);
  await caption("The same items, grouped by where they sit", "One account can span several buckets");
  await sleep(2200);
  await scene("accounts");
  await caption("Accounts × buckets", "A matrix that reconciles to the same net worth");
  await click(page.getByRole("button", { name: "Matrix", exact: true }), 2800);
  await scene("accounts-matrix");
  await click(page.getByRole("button", { name: "List", exact: true }), 500);

  // 7 ─ Allocation
  await click(nav("Allocation"), 1300);
  await caption("Targets, drift and what to buy or sell to rebalance", "Inspired by Economic Ninja's bucket approach");
  await sleep(2600);
  await scene("allocation");
  await caption("Targets save as you type", "The total is checked against 100% as you go");
  const cash = page.getByLabel("Cash target percent");
  await type(cash, "15", 140);
  await cash.press("Enter");
  await sleep(1700);
  const biz = page.getByLabel("Business target percent");
  await type(biz, "10", 140);
  await biz.press("Enter");
  await pointAt(page.locator(".toast-live"), 400, false);
  await sleep(2400);
  await scene("targets");

  // 8 ─ History
  await scrollTop(300);
  await click(nav("History"), 1400);
  await caption("Net worth history, broken down by bucket", "Hover any month to see exactly what it was made of");
  const canvas = page.locator("[data-role=canvas]").first();
  const cb = await canvas.boundingBox();
  for (const fx of [0.35, 0.55, 0.72]) {
    const x = cb.x + cb.width * fx, y = cb.y + cb.height * 0.55;
    await page.evaluate(([x, y]) => window.__demo.move(x, y, 700), [x, y]);
    await page.mouse.move(x, y, { steps: 12 });
    await sleep(1500);
  }
  await scene("history-hover");
  await page.mouse.move(cb.x + cb.width * 0.5, cb.y - 60);
  await click(page.getByRole("button", { name: "1Y", exact: true }), 1500);
  await click(page.getByRole("button", { name: "All", exact: true }), 1000);
  await caption("Snapshot this month in one click", "Or type in past year-end figures to backfill history");
  await click(page.getByRole("button", { name: "+ From current values" }), 600);
  await type(page.locator('#snapnow input[name="label"]'), "October month end", 35);
  await click(page.getByRole("button", { name: "Record current values" }), 1600);
  await sleep(800);
  await scene("snapshot");

  await caption("Automatic nightly backups, plus download and restore", "Restores are checked before they replace anything");
  await click(page.getByRole("button", { name: "Back up now" }), 1500);
  await pointAt(page.locator("#backups-table tbody tr").first());
  await sleep(2600);
  await scene("backups");

  // End card
  await caption("");
  await page.goto(`${BASE}/__demo/end`);
  await sleep(4200);
  await scene("end");

  if (cdp) {
    await cdp.send("Page.stopScreencast");
    fs.writeFileSync(path.join(OUT, "frames.json"), JSON.stringify({ frames, marks, end: Date.now() / 1000 }, null, 1));
  }
  await browser.close();
}

run().catch((e) => { console.error(e); process.exit(1); });
