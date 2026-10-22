import os
import sqlite3
from datetime import datetime, timezone

# Docker sets DB_PATH=/data/networth.db; this default is for running outside a container.
DB_PATH = os.environ.get("DB_PATH", "./data/networth.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS buckets (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    target_pct  REAL NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    excluded    INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    institution TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    bucket_id     INTEGER REFERENCES buckets(id),
    account_id    INTEGER REFERENCES accounts(id),
    currency      TEXT NOT NULL DEFAULT 'USD',
    amount        REAL,
    ticker        TEXT,
    asset_class   TEXT,
    unit          TEXT,
    -- Real estate: the mortgage lives on the property so equity is derived, and
    -- ordinary liabilities can stay out of the bucket system entirely.
    loan_original REAL,
    loan_balance  REAL,
    quantity      REAL,
    import_batch_id INTEGER,
    last_price    REAL,
    price_currency TEXT,
    last_price_at TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS lots (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    side       TEXT NOT NULL DEFAULT 'buy',
    trade_date TEXT NOT NULL,
    quantity   REAL NOT NULL,
    price      REAL NOT NULL,
    premium    REAL NOT NULL DEFAULT 0,
    unit       TEXT,
    fee        REAL NOT NULL DEFAULT 0,
    currency   TEXT NOT NULL DEFAULT 'USD',
    note       TEXT,
    -- Set on the pair of lots a holding transfer writes, so reversing the move can
    -- find them again and so neither half can be deleted on its own.
    transfer_id INTEGER,
    import_batch_id INTEGER,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_lots_item ON lots(item_id, trade_date, id);

-- Units of a priced holding moved between two locations (cold wallet -> exchange,
-- broker -> broker, safe -> vault). Not a buy or a sell: quantity and cost basis
-- both leave one holding and arrive at the other, so net worth only changes by
-- whatever the network or transfer fee ate.
CREATE TABLE IF NOT EXISTS holding_transfers (
    id           INTEGER PRIMARY KEY,
    from_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    to_item_id   INTEGER REFERENCES items(id) ON DELETE SET NULL,
    qty_sent     REAL NOT NULL,
    qty_received REAL NOT NULL,
    unit         TEXT,
    -- NULL when the source had no lots: quantity moved but there was no recorded
    -- basis to carry, and inventing one would be worse than admitting the gap.
    basis_moved  REAL,
    currency     TEXT,
    moved_on     TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT,
    -- 1 when the move created its destination holding, so reversing it can remove
    -- that holding again if nothing else has happened in it.
    to_item_created INTEGER NOT NULL DEFAULT 0
);

-- Money moved between two manually-valued items (bank -> brokerage cash, cash ->
-- a manual asset, paying down a liability). Recorded so it can be reversed.
CREATE TABLE IF NOT EXISTS transfers (
    id           INTEGER PRIMARY KEY,
    from_item_id INTEGER REFERENCES items(id),
    to_item_id   INTEGER REFERENCES items(id),
    from_amount  REAL NOT NULL,
    to_amount    REAL NOT NULL,
    moved_on     TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT
);

-- One run of the spreadsheet importer. Everything it wrote points back here, so a
-- run that turns out wrong can be rolled back whole rather than unpicked by hand —
-- which is also what makes re-importing an updated sheet safe.
CREATE TABLE IF NOT EXISTS import_batches (
    id          INTEGER PRIMARY KEY,
    source      TEXT,
    imported_at TEXT NOT NULL,
    lot_count   INTEGER NOT NULL DEFAULT 0,
    item_count  INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency     TEXT PRIMARY KEY,
    usd_per_unit REAL NOT NULL,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    taken_at        TEXT NOT NULL,
    label           TEXT,
    assets_usd      REAL,
    liabilities_usd REAL,
    net_worth_usd   REAL
);

CREATE TABLE IF NOT EXISTS snapshot_lines (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    bucket_id   INTEGER NOT NULL REFERENCES buckets(id),
    value_usd   REAL NOT NULL
);

-- Per-item detail per snapshot, so the history tooltip can show individual
-- holdings under each bucket. Only written for snapshots taken from current
-- values; hand-entered historical figures have totals only.
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    item_id     INTEGER,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    bucket_id   INTEGER,
    value_usd   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapitems ON snapshot_items(snapshot_id);

CREATE TABLE IF NOT EXISTS coin_ids (
    ticker  TEXT PRIMARY KEY,
    coin_id TEXT NOT NULL
);
"""

# Example targets only — an even split across the eight included buckets. Set your
# own on the Allocation page; these are inserted only into an empty buckets table.
DEFAULT_BUCKETS = [
    ("safety", "Safety", 12.5, 0),
    ("cash", "Cash", 12.5, 0),
    ("job_income", "Job Income", 0.0, 1),
    ("business", "Business", 12.5, 0),
    ("real_estate", "Real Estate", 12.5, 0),
    ("precious_metals", "Precious Metals", 12.5, 0),
    ("crypto", "Crypto", 12.5, 0),
    ("stocks", "Stocks", 12.5, 0),
    ("collectibles", "Collectibles", 12.5, 0),
]


# Currencies a new database starts with, as (code, approximate USD per unit).
DEFAULT_FX = [
    ("USD", 1.0), ("EUR", 1.08), ("GBP", 1.27), ("JPY", 0.0067),
    ("CAD", 0.73), ("AUD", 0.66), ("CHF", 1.12),
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    # timeout: background jobs and a request can want the write lock at the same
    # moment; wait for it rather than failing with "database is locked".
    #
    # check_same_thread=False: a connection opened by the db_session dependency is
    # created in whichever worker thread resolves it, then used by the handler —
    # which for an `async def` route is the event loop thread. SQLite's default
    # guard rejects that outright. Safe here because a connection is scoped to one
    # request and closed at the end of it, so no two threads ever hold it at once.
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def db_session():
    """A connection that is closed even when the handler raises.

    Used as a FastAPI dependency, so the `finally` runs after the response — or
    after an exception — rather than depending on when the garbage collector gets
    round to it. Without this, a handler that raises part-way through leaves its
    connection alive inside the traceback, still holding SQLite's write lock, and
    the *next* write fails with "database is locked". That failure lands on an
    unrelated request and clears up on its own, which makes it look intermittent
    and unreproducible.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def migrate(conn):
    """Promote the old free-text items.account into first-class accounts rows."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}

    if "account_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN account_id INTEGER REFERENCES accounts(id)")

    if "account" in cols:
        rows = conn.execute(
            "SELECT DISTINCT TRIM(account) n FROM items"
            " WHERE account IS NOT NULL AND TRIM(account) != ''"
        ).fetchall()
        for r in rows:
            conn.execute("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (r["n"],))
        conn.execute(
            "UPDATE items SET account_id ="
            " (SELECT id FROM accounts WHERE accounts.name = TRIM(items.account))"
            " WHERE account IS NOT NULL AND TRIM(account) != ''"
        )
        conn.commit()
        try:
            conn.execute("ALTER TABLE items DROP COLUMN account")
        except sqlite3.OperationalError:
            # SQLite < 3.35 can't drop columns; the stale column is simply unused.
            pass

    # Precious metals: weight unit on items, premium and unit on lots.
    if "unit" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN unit TEXT")

    for col in ("loan_original", "loan_balance"):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} REAL")

    lot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(lots)")}
    if "premium" not in lot_cols:
        conn.execute("ALTER TABLE lots ADD COLUMN premium REAL NOT NULL DEFAULT 0")
    if "unit" not in lot_cols:
        conn.execute("ALTER TABLE lots ADD COLUMN unit TEXT")
    if "funding_item_id" not in lot_cols:
        # Which cash item paid for this lot, so deleting the lot can put the money back.
        conn.execute(
            "ALTER TABLE lots ADD COLUMN funding_item_id INTEGER REFERENCES items(id)"
        )
    if "transfer_id" not in lot_cols:
        # Links the two lots a holding transfer writes to the transfer that made them.
        conn.execute("ALTER TABLE lots ADD COLUMN transfer_id INTEGER")
    # Which import run created this row, so the run can be rolled back as a unit.
    if "import_batch_id" not in lot_cols:
        conn.execute("ALTER TABLE lots ADD COLUMN import_batch_id INTEGER")
    if "import_batch_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN import_batch_id INTEGER")

    # Existing metal holdings were priced off futures tickers (GC=F). Point them at
    # the spot feed and default them to troy ounces.
    conn.execute(
        "UPDATE items SET unit = COALESCE(unit, 'toz') WHERE asset_class = 'metal'"
    )
    for legacy, code in (("GC=F", "XAU"), ("SI=F", "XAG"), ("PL=F", "XPT"),
                         ("PA=F", "XPD"), ("HG=F", "HG")):
        conn.execute(
            "UPDATE items SET ticker=?, last_price=NULL, price_currency=NULL,"
            " last_price_at=NULL WHERE asset_class='metal' AND UPPER(ticker)=?",
            (code, legacy),
        )
    conn.execute("UPDATE lots SET unit = COALESCE(unit, 'toz') WHERE unit IS NULL")

    # Liabilities leave the bucket system: they now only reduce net worth. Clearing
    # the link raises the affected buckets' totals (they stop being netted down), so
    # log exactly what moved rather than letting the numbers change silently.
    orphaned = conn.execute(
        "SELECT i.id, i.name, i.amount, i.currency, b.name AS bucket_name"
        " FROM items i JOIN buckets b ON b.id = i.bucket_id"
        " WHERE i.kind = 'liability'"
    ).fetchall()
    if orphaned:
        import logging
        log = logging.getLogger("networth.migrate")
        log.warning(
            "Liabilities no longer use buckets — clearing the bucket on %d item(s). "
            "Net worth is unchanged; those buckets will now read higher.",
            len(orphaned),
        )
        for r in orphaned:
            note = ""
            if (r["bucket_name"] or "").lower().startswith("real estate"):
                note = (" — looks like a mortgage: fold it into the property's "
                        "'current loan balance' and delete this row, or the debt "
                        "gets counted twice")
            log.warning(
                "  %s (%s %s) was in %s%s",
                r["name"], f'{r["amount"] or 0:,.2f}', r["currency"],
                r["bucket_name"], note,
            )
        conn.execute("UPDATE items SET bucket_id = NULL WHERE kind = 'liability'")

    conn.commit()


def reconcile_schema(conn):
    """Add any column SCHEMA declares that the live database is missing.

    `migrate()` handles the migrations that need judgement — moving data, rewriting
    tickers. This is the blunt backstop for the rest: restoring a backup taken
    before an update leaves tables that exist but are a column short, and
    `CREATE TABLE IF NOT EXISTS` won't touch a table that is already there. Without
    this, the first page to select that column dies with "no such column".

    Returns the list of columns added, so a restore can say what it changed.
    """
    ref = sqlite3.connect(":memory:")
    ref.row_factory = sqlite3.Row
    added = []
    try:
        ref.executescript(SCHEMA)
        tables = [r["name"] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            live = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not live:
                continue                      # table doesn't exist; SCHEMA created it
            for col in ref.execute(f"PRAGMA table_info({table})"):
                if col["name"] in live:
                    continue
                decl = f"{col['name']} {col['type']}"
                # ALTER TABLE ADD COLUMN can't add NOT NULL without a default, and
                # can't add a PRIMARY KEY at all. Everything SCHEMA adds after the
                # fact is nullable or defaulted; anything else is left alone rather
                # than failing the whole restore.
                if col["dflt_value"] is not None:
                    decl += f" DEFAULT {col['dflt_value']}"
                    if col["notnull"]:
                        decl += " NOT NULL"
                elif col["notnull"] or col["pk"]:
                    import logging
                    logging.getLogger("networth.migrate").warning(
                        "Cannot add %s.%s automatically (NOT NULL with no default)",
                        table, col["name"],
                    )
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
                    added.append(f"{table}.{col['name']}")
                except sqlite3.OperationalError as exc:
                    import logging
                    logging.getLogger("networth.migrate").warning(
                        "Could not add %s.%s: %s", table, col["name"], exc,
                    )
        conn.commit()
    finally:
        ref.close()
    return added


def check_writable():
    """Turn a permission problem into an actionable message.

    On a NAS the data folder is bind-mounted from a share whose ownership is set by
    the host, so a container running as the wrong uid fails with SQLite's opaque
    "unable to open database file".
    """
    directory = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create the data directory {directory!r}: {exc}\n"
            f"  The container runs as uid {os.getuid()}:{os.getgid()}.\n"
            f"  Make the mounted folder writable by that user, e.g. on the NAS:\n"
            f"      chown -R {os.getuid()}:{os.getgid()} /path/to/networth/data\n"
            f"  or change `user:` in docker-compose to match the folder's owner."
        ) from exc

    probe = os.path.join(directory, ".write-probe")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        import stat as _stat
        try:
            st = os.stat(directory)
            owner = f"{st.st_uid}:{st.st_gid}"
            mode = _stat.filemode(st.st_mode)
        except OSError:
            owner, mode = "unknown", "unknown"
        raise RuntimeError(
            f"The data directory {directory!r} is not writable: {exc}\n"
            f"  It is owned by {owner} (mode {mode}), but this container runs as "
            f"uid {os.getuid()}:{os.getgid()}.\n"
            f"  Fix it one of two ways:\n"
            f"    1. On the NAS:  chown -R {os.getuid()}:{os.getgid()} /path/to/networth/data\n"
            f"    2. Or set `user: \"{owner}\"` in docker-compose.yml to match the folder."
        ) from exc


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    # migrate() first: its legacy paths read the table as it was actually shaped,
    # and reconcile_schema() would otherwise hide that shape from them.
    migrate(conn)
    reconcile_schema(conn)
    if conn.execute("SELECT COUNT(*) c FROM buckets").fetchone()["c"] == 0:
        for i, (key, name, target, excluded) in enumerate(DEFAULT_BUCKETS):
            conn.execute(
                "INSERT INTO buckets (key, name, target_pct, sort_order, excluded)"
                " VALUES (?,?,?,?,?)",
                (key, name, target, i, excluded),
            )
    # The currency dropdowns list fx_rates rows and the refresh only updates rows that
    # exist, so a new database needs some to start with. Rough rates only — the first
    # FX refresh replaces them. An existing database's currencies are left alone.
    if conn.execute("SELECT COUNT(*) c FROM fx_rates").fetchone()["c"] == 0:
        for ccy, rate in DEFAULT_FX:
            conn.execute(
                "INSERT INTO fx_rates (currency, usd_per_unit, updated_at) VALUES (?,?,?)",
                (ccy, rate, now_iso()),
            )
    conn.execute(
        "INSERT OR IGNORE INTO fx_rates (currency, usd_per_unit, updated_at)"
        " VALUES (?,?,?)",
        ("USD", 1.0, now_iso()),
    )
    conn.commit()
    conn.close()


def seed_demo_data():
    """Populate example items so the mockup has something to look at.

    Entirely fictional: placeholder institutions, deliberately odd figures.
    """
    conn = connect()
    if conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] > 0:
        conn.close()
        return
    buckets = {r["key"]: r["id"] for r in conn.execute("SELECT key, id FROM buckets")}
    # (name, kind, bucket, account, currency, amount, ticker, asset_class, quantity,
    #  unit, loan_original, loan_balance)
    rows = [
        ("Emergency fund", "cash", "safety", "Bank A", "USD", 13417.29,
         None, None, None, None, None, None),
        ("Everyday account", "cash", "cash", "Bank A", "USD", 3862.41,
         None, None, None, None, None, None),
        ("Euro current account", "cash", "cash", "Bank C", "EUR", 4718.63,
         None, None, None, None, None, None),
        ("Sterling savings", "cash", "safety", "Bank D", "GBP", 6143.82,
         None, None, None, None, None, None),
        # Broker B deliberately spans two buckets: settlement cash plus equities.
        ("Settlement fund", "cash", "cash", "Broker B", "USD", 1937.58,
         None, None, None, None, None, None),
        ("Global index ETF", "priced", "stocks", "Broker B", "USD", None,
         "VT", "stock", 211.3, None, None, None),
        ("Example large-cap", "priced", "stocks", "Broker B", "USD", None,
         "MSFT", "stock", 37, None, None, None),
        ("Bitcoin", "priced", "crypto", "Wallet E", "USD", None,
         "BTC", "crypto", 0.3172, None, None, None),
        ("Ethereum", "priced", "crypto", "Wallet E", "USD", None,
         "ETH", "crypto", 2.846, None, None, None),
        ("Silver rounds", "priced", "precious_metals", "Vault F", "USD", None,
         "XAG", "metal", 47, "toz", None, None),
        # The mortgage lives on the property, so its value counts as equity.
        ("Example Apartment", "property", "real_estate", "Example Mortgage", "EUR", 287413,
         None, None, None, None, 231870, 168342.55),
        ("Example LLC equity", "manual", "business", None, "USD", 41377,
         None, None, None, None, None, None),
        ("Art prints", "manual", "collectibles", None, "USD", 7316,
         None, None, None, None, None, None),
        ("Credit card", "liability", None, "Bank A", "USD", 1284.36,
         None, None, None, None, None, None),
        ("Example car loan", "liability", None, "Lender G", "EUR", 9472.18,
         None, None, None, None, None, None),
    ]
    for (name, kind, bkey, account, ccy, amount, ticker, aclass, qty,
         unit, loan_original, loan_balance) in rows:
        account_id = None
        if account:
            conn.execute("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (account,))
            account_id = conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (account,)
            ).fetchone()["id"]
        conn.execute(
            "INSERT INTO items (name, kind, bucket_id, account_id, currency, amount,"
            " ticker, asset_class, quantity, unit, loan_original, loan_balance, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, kind, buckets.get(bkey), account_id, ccy, amount, ticker, aclass, qty,
             unit, loan_original, loan_balance, now_iso()),
        )
    conn.commit()
    conn.close()
