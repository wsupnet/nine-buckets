# Nine Buckets — Technical documentation

[← README](../README.md) · [User guide](USER-GUIDE.md) · [Architecture](../ARCHITECTURE.md) ·
[NAS deployment](../DEPLOY-NAS.md) · [Demo video tooling](../scripts/demo/README.md)

The reference for running, configuring and understanding the app: installation, every setting,
how values are calculated, how data is stored and backed up, and what to do when something goes
wrong. For a plain-language walkthrough of using the app, read the [User guide](USER-GUIDE.md).
For the design decisions behind the code, read [ARCHITECTURE.md](../ARCHITECTURE.md).

## Contents

1. [Stack](#1-stack)
2. [Install and run](#2-install-and-run)
3. [Configuration reference](#3-configuration-reference)
4. [Security and access](#4-security-and-access)
5. [Data storage, backups and restore](#5-data-storage-backups-and-restore)
6. [Data model](#6-data-model)
7. [Prices, currencies and valuation](#7-prices-currencies-and-valuation)
8. [Allocation math](#8-allocation-math)
9. [Cost basis and the lot ledger](#9-cost-basis-and-the-lot-ledger)
10. [Moving money and holdings](#10-moving-money-and-holdings)
11. [Spreadsheet import format](#11-spreadsheet-import-format)
12. [History and snapshots](#12-history-and-snapshots)
13. [Interface behaviour](#13-interface-behaviour)
14. [Operations](#14-operations)
15. [Troubleshooting](#15-troubleshooting)
16. [Known limitations](#16-known-limitations)

---

## 1. Stack

FastAPI + Jinja2 templates + SQLite, served by uvicorn, in a single Docker container. No frontend
build step and no CDN assets. The only outbound requests the app makes are price and exchange-rate
lookups; there are no bank integrations and nothing is sent to a third-party cloud.

## 2. Install and run

You need **Docker** with Compose. Run everything from the project folder.

**1. Create `.env`** and set a passphrase (min. 8 characters; the placeholder is refused):

```bash
cp .env.example .env
openssl rand -base64 24      # paste the output into APP_PASSWORD in .env
```

**2. Check the two host folders** in `docker-compose.yml` under `volumes:`. They hold the live
database and the backups, and must live **outside** the project folder (see
[§5](#5-data-storage-backups-and-restore)). On a NAS with `/volume1` shares the defaults are fine; elsewhere,
point them at folders you create.

**3. Start it:**

```bash
docker compose up -d --build
```

Open `http://<host>:8000` and sign in with the passphrase. On a NAS, follow
[DEPLOY-NAS.md](../DEPLOY-NAS.md), which also covers folder ownership (`user:`), deploying
with `scripts/deploy.sh`, and safe updates.

### Network exposure

`docker-compose.yml` publishes port 8000 on **every interface** of the host (`"8000:8000"`), so
anything on your LAN can reach it and the passphrase is the only thing in front of your data.
Don't port-forward it to the internet; reach it from outside over Tailscale or another VPN. To
restrict it to the host itself, change the mapping to `"127.0.0.1:8000:8000"`.

### Everyday commands

```bash
docker compose logs -f          # follow logs
docker compose restart          # restart
docker compose down             # stop (data is kept)
docker compose up -d --build    # rebuild after changing the code
```

### Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_PASSWORD=dev-passphrase SEED_DEMO=1 DB_PATH=/tmp/nb-dev/networth.db \
  .venv/bin/uvicorn app.main:app --reload
```

The app doesn't load `.env` on its own outside Docker; export it first with
`set -a; . ./.env; set +a`. Production runs Python 3.12 (see the `Dockerfile`), so develop on
3.12 where possible. There is no automated test suite yet.

## 3. Configuration reference

`APP_PASSWORD`, `SECRET_KEY` and `TZ` are read from `.env`. Everything else is set inline under
`environment:` in `docker-compose.yml`.

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | *(required, `.env`)* | Sign-in passphrase, min. 8 characters. Compose refuses to start without it. |
| `SECRET_KEY` | *(generated, `.env`)* | Session-signing key. Empty = generated once and stored next to the database. |
| `TZ` | `UTC` (`.env`) | Container timezone; sets backup filenames and log times. |
| `AUTH_ENABLED` | `1` | `0` runs with no login at all. Only for a network you fully trust. |
| `SESSION_DAYS` | `30` | How long a sign-in lasts. |
| `SESSION_HTTPS_ONLY` | `0` | `1` stops the session cookie being sent over plain HTTP. Use it behind HTTPS. |
| `DB_PATH` | `/data/networth.db` | The live SQLite database. |
| `SEED_DEMO` | `0` | `1` inserts a fictional sample portfolio into an **empty** database. |
| `REFRESH_ENABLED` | `1` | Scheduled price and FX refresh. |
| `REFRESH_ON_START` | `1` | Also refresh when the app starts. |
| `REFRESH_INTERVAL_HOURS` | `24` | Refresh cadence. |
| `BACKUP_ENABLED` | `1` | Scheduled backups. |
| `BACKUP_INTERVAL_HOURS` | `24` | Backup cadence. |
| `BACKUP_RETENTION_DAYS` | `30` | Days of backups to keep. |
| `BACKUP_DIR` | `/backups` | Where backups are written. Mount it outside the project folder. |
| `LOG_LEVEL` | `INFO` | Python log level. |

The published port is the `ports:` mapping in `docker-compose.yml`, not a variable.

## 4. Security and access

- **One shared passphrase**, compared in constant time, then a signed session cookie
  (`SameSite=Lax`, 30 days by default). There are no user accounts and no 2FA.
- **Failed logins are throttled per connecting address:** 8 free attempts, then one attempt per
  2 minutes until 15 minutes pass with no failure. It's a cooldown rather than a lockout because,
  behind Docker's port mapping, every client may appear as the same gateway address.
  `X-Forwarded-For` is never trusted (uvicorn runs with `--no-proxy-headers`), and the throttle is
  in memory, so a restart clears it.
- **Fails closed:** the app refuses to start with a missing, short or placeholder passphrase.
- **Container user:** the image defines a non-root user (uid 10001), and `docker-compose.yml`
  overrides it with `user: "1000:1000"`, a common owner of NAS shared folders. Set `user:` to
  whoever owns the data and backup folders (see [DEPLOY-NAS.md](../DEPLOY-NAS.md)).
  `"0:0"` (root) sidesteps permission problems but gives up the non-root protection.
- **Treat the network as the main control.** Keep it on your LAN or behind a VPN, and set
  `SESSION_HTTPS_ONLY=1` if you put HTTPS in front of it.

## 5. Data storage, backups and restore

| What | Where (in the container) | Notes |
|---|---|---|
| Live database | `DB_PATH` (`/data/networth.db`) | SQLite in WAL mode |
| Session secret | `/data/.session_secret` | Only when `SECRET_KEY` is empty |
| Backups | `BACKUP_DIR` (`/backups`) | `networth-YYYY-MM-DD.db`, plus `-manual` and `-pre-restore` copies |

Both `/data` and `/backups` are bind-mounted from folders **outside** the project folder, so
replacing the project folder during an update can't delete either. A database or backup kept
inside the folder you replace is lost in exactly the situation you most need it.

**Automatic backups** run at startup and every `BACKUP_INTERVAL_HOURS`, using SQLite's backup API
(safe while the app is writing), and are pruned after `BACKUP_RETENTION_DAYS`. A backup is
**skipped** while the database has no items, and the log says so loudly. Otherwise a data volume
that failed to mount would be snapshotted as an empty file and become your "latest" backup. Every
backup is listed with its **item count** for the same reason.

**In the app (History → Backups & export):**

- **Back up now**, **Download** (a single self-contained `.db` to your device, the only copy that
  survives losing the host), **Delete**.
- **Restore** a listed backup: it's integrity-checked first, your current data is saved as a
  `pre-restore` copy, and the schema migration re-runs so an older backup works with newer code.
- **Restore from file**: upload a `.db` or `.json` export. It's validated and summarised (item
  counts and the net worth it contains) before you type `RESTORE`. A JSON file is rebuilt in a
  staging database first, so a malformed export can't half-populate the live one.
- **JSON export**: every table, readable regardless of future schema changes.

**From the command line**, if the app won't start:

```bash
docker compose down
cp /path/to/backups/networth-YYYY-MM-DD.db /path/to/data/networth.db
docker compose up -d
```

**Clearing all data** (keeps the nine buckets unless you add `--buckets-too`; takes a safety
backup first):

```bash
docker compose run --rm networth python -m app.reset          # dry run, shows counts
docker compose run --rm networth python -m app.reset --yes    # wipe
```

## 6. Data model

**Account and bucket are independent dimensions of one item.** An *account* is where something
sits (a bank, a broker, a wallet, a safe); a *bucket* is what role it plays in your allocation.
Because the bucket lives on the item, not the account, a brokerage account holding a settlement
fund (Cash bucket) and an index ETF (Stocks bucket) is two items with different buckets. The
Accounts and Allocation views are re-groupings of the same list, so their totals reconcile to the
same net worth by construction.

Accounts are a managed table, not free text, so a typo can't fork one account into two. Deleting
an account never deletes its items; they move to "No account".

### Item kinds

| Kind | Value is | Notes |
|---|---|---|
| `cash` | typed amount | Bank and savings balances, money market funds |
| `priced` | quantity × market price | Stocks, ETFs, crypto, precious metals |
| `property` | equity = value − loan balance | The mortgage lives on the property |
| `manual` | typed amount | Collectibles, business equity, anything without a price feed |
| `liability` | typed amount | Credit cards, personal loans. **No bucket**: reduces net worth only |

- **Liabilities never have a bucket.** Bucket percentages describe assets only. Because of that,
  the allocation base (all bucketed assets) is higher than net worth by what you owe, and the
  Allocation page says so.
- **A mortgage belongs on its property**, never as a separate liability, or the debt is counted
  twice. The Real Estate bucket therefore shows equity, not gross leveraged value; the original
  loan amount drives a payoff-progress readout.
- **Closed positions:** a priced holding whose quantity reaches zero is hidden from the main tables
  and listed under **Closed positions** on Holdings. It's never deleted, since its lots still carry
  realized gain. An **oversold** holding (more sold than the ledger holds) stays in the main table
  with a red tag instead, because that's a gap in the record, not a closed position.
- **Possible duplicates:** the same ticker twice **within one account** is flagged on Holdings
  (usually a hand-typed row plus an imported one). The same asset in two different accounts is a
  legitimate split and isn't flagged.

### Buckets

Nine buckets are created in an empty database with example targets of 12.5% each:
Safety, Cash, Job Income, Business, Real Estate, Precious Metals, Crypto, Stocks, Collectibles.
**Job Income** is marked `excluded`: income is a flow, not a balance, so it holds $0 and stays out
of the percentage math, and the other eight sum to 100%. Set your own targets on the Allocation
page.

## 7. Prices, currencies and valuation

All prices are keyless and free.

| Asset class | Source | Ticker format |
|---|---|---|
| Stocks / ETFs | Yahoo Finance | `VT`, `MSFT` |
| Crypto | CoinGecko | `BTC`, `ETH`, `SOL` |
| Metals | gold-api.com **spot** (CoinGecko cross-rate fallback for gold and silver) | `XAU`, `XAG`, `XPT`, `XPD`, `HG` |
| FX | Yahoo Finance | automatic, per currency |

- **Refresh** runs in-process at startup and every `REFRESH_INTERVAL_HOURS`, or on demand with
  **Refresh all prices & FX** or a single holding's **Fetch price**. Work is grouped by ticker,
  and crypto goes out as one batched request.
- **Base currency is USD.** Each item keeps its own currency and is converted at the stored rate.
  A new database starts with USD, EUR, GBP, JPY, CAD, AUD and CHF, with rough rates that the first
  refresh replaces.
- **FX precision:** the app requests `USD{ccy}=X` and inverts it. Yahoo rounds to 4 decimal
  places, so the direct `JPYUSD=X` pair would keep only two significant figures and misvalue a
  large yen balance by hundreds of dollars.
- **Metals are valued on spot, never futures.** Gold futures run around 1.3% above spot, and the
  gap moves with contract roll. Legacy `GC=F` / `SI=F` items are migrated to `XAU` / `XAG`
  automatically.
- **Weights:** metals can be entered in troy ounces, grams, kilos or pounds and are normalised to
  troy ounces internally, then displayed in the unit you chose. Copper is quoted per pound
  upstream and converted (otherwise it would read 14.58× too high).

## 8. Allocation math

- Percentages are of the **allocation base**, the sum of all bucketed asset values, not of net
  worth.
- **Drift** = actual % − target %. Drift beyond ±3 points is highlighted.
- **To rebalance** converts drift into a dollar amount to buy or sell for each bucket.
- Targets of the included buckets should sum to 100%; the page shows the running total and warns
  when it doesn't.

## 9. Cost basis and the lot ledger

Each priced holding can have a **lot ledger** of buys and sells: date, quantity, price, fee, and
for metals a dealer premium per unit.

- Sells match buys **oldest-first (FIFO)**.
- Fees **increase** a buy's cost and **reduce** a sell's proceeds.
- Metals: basis = `spot + premium` (+ fees). Premium paid is tracked separately and shown as a
  percentage over spot, so dealer markup stays visible.
- **Hybrid quantity:** a holding *with* lots takes its quantity from them (the field goes
  read-only); one *without* keeps its typed quantity. You can add ledgers one position at a time,
  and deleting every lot falls back to the typed quantity.
- **Overselling is flagged** on the position and when you add the lot, never given an invented
  basis.

Worked example: 5 toz bought at $1,925 all-in with a $12 fee is a $1,927.40/toz basis; a FIFO sell
of 2 toz at $2,600 realizes **$1,400.20**.

## 10. Moving money and holdings

**Every lot needs a "Paid from" choice**, deliberately with no default:

- **A cash item:** a buy debits it by the full cost (premium and fees included); a sell credits the
  proceeds ("Deposit to"). Deleting the lot puts the money back exactly.
- **External funds:** records the holding only. Use it for purchases paid by fresh transfer and
  for back-history: a cash balance is a point-in-time figure, so a years-old purchase must not move
  today's balance.

**Move money** (Assets & Liabilities) moves value between manually-valued items: bank → brokerage
cash, cash → a manual asset, cash → a liability to pay it down, or cash → a property as a mortgage
payment. Transfers between assets leave net worth unchanged; paying a liability reduces both
sides. Cross-currency moves convert at the stored FX rate and are rounded to cents. Overdrawing is
allowed but flagged. Every move is listed under **Recent activity** and can be reversed.

**Move holdings** moves *units* of a priced holding (cold wallet → exchange, broker → broker):

- It isn't a sale. The source's FIFO queue is consumed oldest-first, and the destination gets one
  lot per matched source lot **at the original trade dates**, recorded as
  `transfer_out` / `transfer_in`. Nothing is realized.
- **Network fees:** enter what was sent and what arrived; the full basis rides on the smaller
  quantity, so its unit cost rises.
- Both sides must be the same ticker and asset class. A new destination inherits the source's
  ticker, asset class, bucket and unit, and is priced straight away.
- If the source has no lots, the quantity moves alone and the move says so; nothing is invented.
- Reversing a move drops both lots together, and removes a holding the move created unless it has
  since been traded or moved.

## 11. Spreadsheet import format

**Holdings → Import trade history** reads `.xlsx` / `.xlsm` workbooks (via `openpyxl`):

- A **trade tab** named `Crypto`, `Stocks` or `Trades`, one row per transaction (`Stocks` rows
  import as stocks; `Crypto` and `Trades` rows as crypto). Columns are found
  by header name: `Date`, `Ticker`/`Symbol`, `Quantity`/`Qty` (required), plus
  `Platform`/`Exchange`/`Account`, `Crypto`/`Asset`/`Name` and `Total Paid`/`Cost`. A **negative
  quantity is a sell**.
- A **Precious Metals tab** (any tab whose name contains "Precious Metal", "Metals" or "Bullion")
  with side-by-side
  blocks. Each block has the metal's name (`SILVER`, `GOLD`, `PLATINUM`, `PALLADIUM`, `COPPER`) in
  the row above a `Date Purchased` header, then `Troy Oz`, `Cost`, `Est. Premium`, and optionally
  `Item Description` and `From`. `Est. Premium` is a **total** for the row, so spot is
  `(cost − premium) ÷ ounces`.

Flow: upload → set a destination account per ticker → preview → type `IMPORT`. Nothing is written
before that.

- **Reconciliation:** each holding shows the quantity the ledger computes against what the sheet's
  rows add up to; any skipped row is listed with its reason and row number.
- A holding whose sells outrun its buys is **held back entirely** rather than imported with a gap.
  A buy with no amount imports at zero basis and is flagged; a sell with no amount is skipped.
- Undated rows take a fallback date you choose.
- **Every run is one reversible batch.** Roll back before re-importing an updated sheet, or the
  same trades land twice.

## 12. History and snapshots

- **From current values** records assets, liabilities, every bucket and every item at that moment
  (optionally backdated). These snapshots drive the per-bucket bars and the hover breakdown.
- **Add past figures** records a date, total assets and total liabilities typed from an old
  spreadsheet, so history can reach back before you started using the app. These have totals only.
- The chart plots on real dates (1Y / 3Y / 5Y / 10Y / All), so sparse yearly and dense monthly
  history share one axis. Snapshots can be edited and deleted.

## 13. Interface behaviour

- **Inline editing:** balances, values, quantities, loan balances, bucket assignments and targets
  save on blur or Enter; Escape cancels. Every save shows a toast with **Undo**, and the server
  returns every recomputed figure so the page updates without a reload. Only whitelisted fields
  (an item's `amount`, `quantity`, `bucket_id`, `loan_balance` and `loan_original`, and a
  bucket's `target_pct`) accept inline writes; names,
  tickers and accounts go through the Edit panel.
- **Row menus (⋯)** hold Edit, Fetch price and Delete. They're positioned `fixed` so table
  scrolling can't clip them, and close on any scroll.
- **Tables:** click a header to sort (remembered per table in the browser); long tables paginate
  at 10 / 15 / 25 / 50 rows (remembered per table). Lot ledgers and transactions are newest-first.
- **Accounts and Allocation views** toggle between List, Matrix and Both, remembered in
  `localStorage`. The Accounts List view is the quickest place for a monthly update: every
  balance is an inline field in the item's own currency.

## 14. Operations

- **Healthcheck:** `GET /healthz`; `docker compose ps` shows healthy/unhealthy.
- **SQLite WAL mode** with a 15 s busy timeout, so scheduled jobs and requests don't collide.
  A database connection is never held across a network fetch.
- **Scheduled work runs in-process**: no cron container. A failed refresh logs and retries next
  cycle.
- **Static assets are mtime-stamped** (`style.css?v=…`), so an update never leaves a browser on
  stale CSS or JS.
- **Startup checks:** the app names the exact fix for an unwritable data folder (folder owner,
  container uid, both remedies), and warns loudly if it starts on an empty database while backups
  on disk still hold items.

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Set APP_PASSWORD in .env` (compose error) | No `.env`, or it wasn't read | Create `.env` next to `docker-compose.yml`; on a NAS, deploy over SSH |
| `APP_PASSWORD is still the placeholder` | `.env` not edited | Set a real passphrase |
| Container restarts in a loop | Placeholder password or unwritable data folder | `docker logs networth`: the error names the fix |
| `data directory is not writable` | `user:` doesn't match the folder owner | See [DEPLOY-NAS.md §2](../DEPLOY-NAS.md) |
| App is empty after an update | The data volume didn't mount | Don't re-enter data: restore the newest backup with a non-zero item count |
| Prices show "not fetched" | No internet from the host, or a wrong ticker | Check outbound access, then **Refresh all prices & FX** |
| "Too many attempts" at sign-in | Throttle after 8 failures | Wait the seconds shown; a restart also clears it |
| Signed out after every restart | Session secret can't be saved | Make the data folder writable, or set `SECRET_KEY` |

More NAS-specific cases are in [DEPLOY-NAS.md](../DEPLOY-NAS.md#if-something-goes-wrong).

## 16. Known limitations

- **One shared passphrase**, no user accounts or 2FA. Keep it on a private network.
- **Historical FX isn't stored:** non-USD cost basis (and a cross-currency holding move's basis)
  converts at today's rate rather than the rate on the trade date.
- **Hand-entered past snapshots have totals only**, no bucket or item breakdown.
- **No currency editor yet:** the currency list comes from the `fx_rates` table.
- **Snapshots are manual:** there's no scheduled monthly snapshot.
- **No historical price backfill**, and no automated off-host backup (use **Download**, or point
  your NAS's backup job at the data and backup folders).
- **No automated test suite.**
