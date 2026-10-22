# Nine Buckets — Architecture

[← README](README.md) · [User guide](docs/USER-GUIDE.md) ·
[Technical documentation](docs/DOCUMENTATION.md) · [NAS deployment](DEPLOY-NAS.md)

How the app is built, and the design decisions that shouldn't be silently reversed. The
[README](README.md) says what the app is, the [user guide](docs/USER-GUIDE.md) covers day-to-day
use, the [technical documentation](docs/DOCUMENTATION.md) is the full reference, and
[DEPLOY-NAS.md](DEPLOY-NAS.md) covers NAS deployment.

---

## 1. Scope

A self-hosted, Docker-deployed balance sheet (assets, liabilities, net worth) and
asset-allocation tracker. Budgeting and transaction ledgers are deliberately out of scope;
pair it with a separate budgeting tool.

### Requirements

- Self-hosted, single `docker compose up`, data in a volume you control
- **No bank integrations** (no Plaid or aggregators), by design
- **Custom buckets as a first-class feature**, able to hold non-market assets (real estate,
  collectibles, business equity)
- Target allocation % per bucket, with actual-vs-target drift
- Multi-currency with a USD base and automatic FX
- Net worth history over time

### Explicitly out of scope

Budgeting · envelopes · a spending transaction ledger · bank syncing · tax reporting ·
multi-user accounts · performance analytics (TTWROR, IRR, Sharpe).

A **purchase-lot ledger** for cost basis exists; that is not the excluded spending ledger.

---

## 2. Stack

FastAPI + Jinja2 + SQLite + vanilla JS. No frontend build step, no CDN. Single container.

```
app/main.py      routes, filters, form handling
app/model.py     valuation, buckets, FIFO, views
app/db.py        schema + migrations
app/jobs.py      scheduled refresh, backups, restore
app/importer.py  spreadsheet parsing, preview planning, batch commit/rollback
app/prices.py    Yahoo / CoinGecko / gold-api
app/auth.py      single-password gate
app/config.py    env config + validation
app/reset.py     data wipe CLI
```

Templates: `dashboard · items · holdings · accounts · allocation · history · import · base · login`
Static: `app.js` (menus/panels) · `inline.js` (click-to-edit) · `chart.js` (net worth chart) ·
`tables.js` (sorting, filtering and pagination — one module, because all three decide
which rows are visible and split apart they fight over it) · `style.css`

---

## 3. Core data model

### Account and bucket are independent dimensions of one item

Classification belongs on the individual holding, not the account it sits in. A brokerage account can hold a settlement fund (**cash** bucket) and
an index ETF (**stocks** bucket). Because the bucket lives on the *item*, both the Accounts view
and the Allocation view are re-groupings of one dataset — their totals reconcile to the same net
worth **by construction**, not by agreement between separate calculations.

### Tables

`buckets · accounts · items · lots · transfers · holding_transfers · import_batches · fx_rates ·
snapshots · snapshot_lines · snapshot_items · coin_ids`

`transfers` moves money between manually-valued items; `holding_transfers` moves *units* of a
priced holding between two locations. `lots.transfer_id` links the pair of lots a holding move
writes.

### Default buckets

Nine buckets are created in an empty database (`DEFAULT_BUCKETS` in `app/db.py`) with example
targets. A bucket with `excluded = 1` holds no weight in the percentage math. Set real targets on
the Allocation page.

### Item kinds

```python
KINDS = ["cash", "priced", "property", "manual", "liability"]
ASSET_CLASSES = ["stock", "crypto", "metal"]
```

| Kind | Value is | Notes |
|---|---|---|
| `cash` | typed `amount` | money market funds go here, **not** `priced` |
| `priced` | quantity × market price | stocks, crypto, metals |
| `property` | **equity** = `amount − loan_balance` | mortgage lives on the property |
| `manual` | typed `amount` | collectibles, business equity |
| `liability` | typed `amount` | **no bucket** — reduces net worth only |

### Decisions that must not be silently reversed

1. **Liabilities have no bucket.** They reduce net worth and nothing else. Bucket percentages
   describe assets only. The inline endpoint actively rejects setting a bucket on a liability.
2. **A property's value IS its equity.** The Real Estate bucket shows ownership, not gross
   leveraged value. A mortgage belongs on its property, never as a standalone liability, or the
   debt is counted twice.
3. **Metals are valued on SPOT, never futures.** Gold futures run ~1.3% above spot and the gap
   moves with contract roll.
4. **Copper is quoted per POUND upstream** (14.5833 troy oz/lb) and must be converted.
   Everything normalises to troy ounces internally.
5. **Hybrid quantity:** a holding *with* lots derives quantity from them (field goes read-only);
   one *without* keeps its typed quantity. The manual value is preserved, so deleting all lots
   falls back cleanly.
6. **Buying must debit the funding cash.** Every lot requires an explicit "Paid from" — a cash
   item, or "external funds". **No default, deliberately**: a backdated purchase must not move
   today's balance, because a cash balance is point-in-time, not a running ledger.
7. **Moving a holding carries its cost basis, and realizes nothing.** Moving coins from a cold
   wallet to an exchange is not a sale. The source's FIFO queue is consumed oldest-first and the
   destination gets one lot per matched source lot **at the original trade dates**, so a later
   sell still matches the genuinely oldest units. Recorded as `transfer_out`/`transfer_in` lots,
   never as a sell-at-cost. Where there are no lots to draw basis from, the quantity moves alone
   and the move says so; nothing is ever invented.
8. **The importer reconciles against the source sheet, and holds back what it can't verify.**
   Every holding shows computed quantity vs. the spreadsheet's own net, because a silent gap in
   cost basis is the one failure mode that isn't visible later. A holding whose sells outrun its
   buys is held back whole rather than imported with a hole. Every run is one reversible batch —
   roll back before re-importing an updated sheet.

---

## 4. Cost basis / lot ledger

- **FIFO** matching, oldest buys first
- Fees **increase** a buy's cost, **reduce** a sell's proceeds
- Metals additionally record **premium over spot** per unit; basis = `spot + premium`, and
  premium paid is tracked separately so dealer markup is visible
- Weight units: `toz · g · kg · lb`, all normalised to troy ounces
- **Overselling is flagged**, not silently given an invented basis
- **Holding transfers** consume the queue like a sell but realize nothing; a network fee is
  absorbed into basis, so the smaller quantity that arrived carries the whole cost and its unit
  cost rises

Worked examples (illustrative figures): 5 toz @ $1,925 all-in with $12 fee → $1,927.40/toz
basis; FIFO sell of 2 toz @ $2,600 → realized **$1,400.20**. Transfer path: 200 units @ $0.50
with a $5 fee → $0.525/unit; moving 100 with 99.9 arriving carries $52.50 of basis onto 99.9
units ($0.525526 each), and a later sell at $3.00 with a $1 fee realizes **$246.20**.

---

## 5. Import design

`/import` reads a maintained balance-sheet spreadsheet (not an exchange export) via `openpyxl`:

- a trade tab (`Crypto`, `Stocks` or `Trades`), one row per transaction, negative quantity = sell
- a precious-metals tab with per-metal blocks, each starting at a `Date Purchased` header

Flow: upload → destination per ticker → preview with reconciliation → type `IMPORT`. Nothing is
written before confirmation, and every run is one batch that can be rolled back.

**Standing rule:** never hold a database connection across a network fetch. SQLite allows one
writer; fetching a price inside the import's write transaction once deadlocked against the
coin-id cache. `_price_items` in `main.py` is the shared helper that does it correctly.

---

## 6. Price sources (all keyless, free)

| Asset class | Source | Format |
|---|---|---|
| Stocks / ETFs | Yahoo Finance | `VT`, `MSFT` |
| Crypto | CoinGecko | `BTC`, `ETH`, `SOL` |
| Metals | **gold-api.com spot** (CoinGecko cross-rate fallback for gold/silver) | `XAU XAG XPT XPD HG` |
| FX | Yahoo Finance | automatic per currency |

FX quotes `USD{ccy}=X` and **inverts** it — Yahoo rounds to 4dp, so a direct pair such as
`JPYUSD=X` gives only two significant figures.

Refresh runs **daily in-process** plus on startup. No cron container.

---

## 7. Deployment

**Single `docker-compose.yml`.** Do not reintroduce a second compose file; picking the wrong one
is an easy way to start the app without its configuration.

```bash
cp .env.example .env          # set APP_PASSWORD
cd /path/to/networth && docker compose up -d --build
```

The compose file bind-mounts two host folders that live **outside** the project folder, so
replacing the project folder during an update can't delete them:

```
/path/to/networth-data     -> /data      live database + session secret
/path/to/networth-backups  -> /backups   nightly backups
```

`user:` in the compose file must match the owner of those folders.

### Operational properties

- Image builds a uid-10001 user; `user:` in compose overrides it
- Healthcheck on `/healthz`; SQLite **WAL** with 15s busy timeout
- Failed logins throttled per connecting address: 8 free attempts, then one per 2 minutes until
  15 minutes pass with no failure. A cooldown rather than a lockout, because behind Docker every
  client may share one address. Forwarded headers are not trusted.
- **Static assets are mtime-stamped** (`app.js?v=…`) so a stale cached JS can't mask a fix
- **Fails fast with actionable errors**: missing/placeholder `APP_PASSWORD`, unwritable data
  directory (names the folder's owner, the container's uid, and both fixes)

### Config

Secrets (`APP_PASSWORD`, optional `SECRET_KEY`) come from `.env`; everything else is inline in
`docker-compose.yml`:

`AUTH_ENABLED · SESSION_DAYS · SESSION_HTTPS_ONLY · SEED_DEMO · REFRESH_ENABLED ·
REFRESH_ON_START · REFRESH_INTERVAL_HOURS · BACKUP_ENABLED · BACKUP_INTERVAL_HOURS ·
BACKUP_RETENTION_DAYS · BACKUP_DIR`

---

## 8. Backups

- `BACKUP_DIR=/backups`, mounted **outside** the project folder
- **Back up now** / **Download** (single self-contained `.db`) / **Restore** / **JSON export**
- **Restore from file** — upload a `.db` or `.json`; it's integrity-checked and **summarised
  before committing**, then you type `RESTORE`
- Restore always takes a `pre-restore` backup first; corrupt files are rejected before touching
  live data; path traversal blocked (`jobs.resolve_backup`)
- JSON import builds a **staging database** first so a malformed export can't half-populate the
  live one
- Automatic backups are skipped while the database is empty, so a failed volume mount can't
  overwrite good backups with an empty one

---

## 9. Standing patterns

- `safe_round` + `num` filters are null-safe and `templates.env.finalize` renders any stray
  `None` as `—`. Sorting happens in Python (`by_value_desc`), not in templates.
- Jinja `sort(attribute=…)` and `round()` raise on `None` — handle nulls at the filter layer.
- Use bracket access for dict keys named like methods (`staged["items"]`).
- `_float` accepts currency symbols, thousands separators and accounting negatives.

---

## 10. Known limitations

- **Metal cost basis converts at today's FX rate**, not the historical rate.
- **Backfilled history has no bucket detail** — hand-entered figures show totals only.
- **Per-item snapshot detail** exists only for snapshots taken after `snapshot_items` was added.
- **No off-host automated backup.** Use **Download**, or point a host backup job at the data and
  backup folders.
- **A holding transfer's basis converts at today's FX rate** if the two sides are priced in
  different currencies.
- **Reversing a holding move you have already sold from** leaves the destination flagged as
  oversold — the honest outcome, but order of operations matters when undoing.
- **No automated tests.** Verification has been manual.
