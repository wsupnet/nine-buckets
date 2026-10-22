"""Scheduled work that runs inside the app container: price refresh and backups.

Kept in-process deliberately so deployment stays a single `docker compose up` with
no cron container or external scheduler.
"""

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from . import config, prices
from .db import DB_PATH, connect, now_iso

log = logging.getLogger("networth.jobs")


def refresh_all_prices():
    """Refresh every priced holding and every non-USD FX rate.

    Returns (ok_count, failures) where failures is [(symbol, reason)]. Never raises
    for a single bad symbol — one delisted ticker must not stop the rest updating.

    Work is grouped by ticker, not by holding. The same asset held in two places is
    one price, so fetching it twice buys nothing and costs a request; and crypto
    goes out as a single batched call. Refreshing per row instead used to spend
    roughly two requests per holding, exhaust CoinGecko's keyless allowance partway
    through, and then report every remaining coin as failed — which looked like bad
    tickers rather than what it was.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT id, ticker, asset_class FROM items"
        " WHERE kind='priced' AND ticker IS NOT NULL AND TRIM(ticker) != ''"
    ).fetchall()
    ccys = [
        r["currency"]
        for r in conn.execute("SELECT currency FROM fx_rates WHERE currency != 'USD'")
    ]
    conn.close()

    # ticker -> the item ids that share it, so one price updates all of them.
    groups = {}
    for row in rows:
        key = (row["ticker"].strip().upper(), row["asset_class"] or "")
        groups.setdefault(key, []).append(row["id"])

    quotes, failures = {}, {}

    crypto = [t for (t, cls) in groups if cls == "crypto"]
    if crypto:
        got, errs = prices.fetch_crypto_many(crypto)
        for t, pair in got.items():
            quotes[(t, "crypto")] = pair
        for t, reason in errs.items():
            failures[t] = reason

    for (ticker, cls) in groups:
        if cls == "crypto":
            continue
        try:
            quotes[(ticker, cls)] = prices.fetch_price(ticker, cls)
        except prices.PriceError as exc:
            failures[ticker] = str(exc)

    ok = 0
    updates = []
    stamp = now_iso()
    for key, item_ids in groups.items():
        quote = quotes.get(key)
        if not quote:
            continue
        price, ccy = quote
        for item_id in item_ids:
            updates.append((price, ccy, stamp, item_id))
        ok += len(item_ids)

    if updates:
        conn = connect()
        try:
            conn.executemany(
                "UPDATE items SET last_price=?, price_currency=?, last_price_at=?"
                " WHERE id=?", updates)
            conn.commit()
        finally:
            conn.close()

    for ccy in ccys:
        try:
            prices.refresh_fx(ccy)
        except prices.PriceError as exc:
            failures[ccy] = str(exc)

    return ok, sorted(failures.items())


BACKUP_PREFIX = "networth-"


def backup_dir():
    return config.BACKUP_DIR or os.path.join(os.path.dirname(DB_PATH) or ".", "backups")


def count_items(path):
    """How many items a backup file holds, or None if it can't be read.

    Size alone doesn't tell you whether a backup is any good: an empty database
    still carries the full schema and pages out to much the same size as a
    populated one. The row count is what distinguishes a real backup from a
    snapshot of nothing.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def list_backups():
    """Newest first, with size, modified time and item count."""
    d = backup_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not (name.startswith(BACKUP_PREFIX) and name.endswith(".db")):
            continue
        path = os.path.join(d, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "name": name,
            "bytes": st.st_size,
            "item_count": count_items(path),
            "mtime": st.st_mtime,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
            .isoformat(timespec="seconds"),
        })
    # By modification time, not name: a manual backup taken today sorts as
    # "networth-2025-03-14-121458-manual.db", which is lexicographically *below*
    # the day's plain "networth-2025-03-14.db" even though it's newer.
    return sorted(out, key=lambda b: (b["mtime"], b["name"]), reverse=True)


def resolve_backup(name):
    """Map a user-supplied filename to a real backup, refusing anything outside
    the backup directory."""
    if not name or "/" in name or "\\" in name or name != os.path.basename(name):
        return None
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(".db")):
        return None
    d = os.path.realpath(backup_dir())
    path = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(path) != d or not os.path.isfile(path):
        return None
    return path


UPLOAD_NAME = "uploaded-restore"


def inspect_db(path):
    """Summarise a candidate .db so you can confirm it's the right file first."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("File failed its integrity check — it may be corrupt")
        have = {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "items" not in have or "buckets" not in have:
            raise ValueError("That doesn't look like a Net Worth database "
                             "(no items/buckets tables)")

        def count(table):
            return con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] \
                if table in have else 0

        rates = {r["currency"]: r["usd_per_unit"]
                 for r in con.execute("SELECT * FROM fx_rates")} if "fx_rates" in have else {}
        rates["USD"] = 1.0
        assets = liabilities = 0.0
        for r in con.execute("SELECT * FROM items"):
            keys = r.keys()
            rate = rates.get(r["currency"], 1.0)
            if r["kind"] == "priced":
                q, p = r["quantity"], r["last_price"]
                v = (q * p * rates.get(r["price_currency"] or "USD", 1.0)) if q and p else 0.0
            elif r["kind"] == "property" and "loan_balance" in keys:
                v = ((r["amount"] or 0) - (r["loan_balance"] or 0)) * rate
            else:
                v = (r["amount"] or 0) * rate
            if r["kind"] == "liability":
                liabilities += v
            else:
                assets += v
        return {
            "items": count("items"), "accounts": count("accounts"),
            "lots": count("lots"), "snapshots": count("snapshots"),
            "transfers": count("transfers") + count("holding_transfers"),
            "assets": assets, "liabilities": liabilities,
            "net_worth": assets - liabilities,
        }
    finally:
        con.close()


def import_json(path):
    """Rebuild the database from a JSON export.

    Written into a fresh database file first, so a malformed export can't leave the
    live one half-populated.
    """
    import json
    with open(path) as fh:
        payload = json.load(fh)
    tables = payload.get("tables")
    if not isinstance(tables, dict) or "items" not in tables:
        raise ValueError("That JSON isn't a Net Worth export (no 'tables.items')")

    staged = os.path.join(backup_dir(), f"{BACKUP_PREFIX}{UPLOAD_NAME}-staged.db")
    if os.path.exists(staged):
        os.remove(staged)

    from .db import SCHEMA
    con = sqlite3.connect(staged)
    con.row_factory = sqlite3.Row      # PRAGMA rows are read by column name below
    try:
        con.executescript(SCHEMA)
        for table, rows in tables.items():
            if not rows:
                continue
            valid = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if not valid:
                continue          # a table this version no longer has
            for row in rows:
                cols = [c for c in row if c in valid]
                if not cols:
                    continue
                con.execute(
                    f"INSERT OR REPLACE INTO {table} ({','.join(cols)})"
                    f" VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
        con.commit()
    finally:
        con.close()
    return staged


def restore_backup(name):
    """Replace the live database with a backup, after saving what's there now.

    Uses SQLite's backup API rather than a file copy, so it is safe while the app
    is serving and doesn't leave stale WAL files behind.
    """
    src_path = resolve_backup(name)
    if not src_path:
        raise ValueError(f"No such backup: {name}")

    safety = backup_database(label="pre-restore")

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(DB_PATH, timeout=15)
    try:
        # Sanity-check the file before letting it overwrite live data.
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"{name} failed its integrity check — not restoring")
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    # A backup carries the schema it was taken with. Restoring one from before an
    # update leaves the live database missing whatever that update added, and the
    # first page to touch it dies with "no such table"/"no such column" — the same
    # migration that runs at startup has to run here too.
    upgrade_restored()

    return {"restored": name, "safety_copy": safety["file"]}


def upgrade_restored():
    """Bring the freshly restored database up to the running app's schema."""
    from .db import init_db
    init_db()


def backup_database(label=None, skip_if_empty=False):
    """Consistent online copy via SQLite's backup API.

    Copying the file with `cp` while the app is writing can capture a torn database;
    the backup API takes a proper snapshot even under concurrent writes.

    `skip_if_empty` guards the automatic runs. If the live database has no items —
    which is what you are looking at after the data volume goes missing during an
    update — backing it up anyway writes an empty file that is now the *newest*
    backup, so "restore the latest" restores nothing and the good copy is one row
    further down a list that gives no hint which is which. Refuse instead, and say
    so loudly: nothing has been lost yet at that point, and the older backups are
    still the way back.
    """
    if skip_if_empty:
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            items = None
        if items == 0:
            log.warning(
                "Skipping the automatic backup: the database has no items in it. "
                "If you expected data here, the /data volume did not survive the "
                "last update — restore the newest backup that shows a non-zero "
                "item count rather than starting over."
            )
            return {"file": None, "bytes": 0, "pruned": 0, "skipped": "empty database"}

    dest_dir = backup_dir()
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if label:
        # Labelled copies carry a timestamp so a manual one never clobbers the
        # day's scheduled backup.
        stamp += "-" + datetime.now(timezone.utc).strftime("%H%M%S") + "-" + label
    dest = os.path.join(dest_dir, f"{BACKUP_PREFIX}{stamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    pruned = prune_backups(dest_dir)
    size = os.path.getsize(dest)
    return {"file": os.path.basename(dest), "bytes": size, "pruned": pruned}


def prune_backups(dest_dir=None):
    dest_dir = dest_dir or backup_dir()
    if not os.path.isdir(dest_dir):
        return 0
    cutoff = time.time() - config.BACKUP_RETENTION_DAYS * 86400
    removed = 0
    for name in os.listdir(dest_dir):
        if not (name.startswith("networth-") and name.endswith(".db")):
            continue
        path = os.path.join(dest_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            log.warning("could not prune backup %s", name)
    return removed


async def _run_once(name, fn):
    try:
        result = await asyncio.to_thread(fn)
        log.info("%s ok: %s", name, result)
        return result
    except Exception:
        # A failure here must never kill the loop — the next run should still happen.
        log.exception("%s failed", name)
        return None


async def _every(name, fn, interval_hours, run_at_start):
    if run_at_start:
        await _run_once(name, fn)
    interval = max(1, interval_hours) * 3600
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        await _run_once(name, fn)


def start(app):
    """Launch background loops; returns the tasks so shutdown can cancel them."""
    tasks = []

    if config.REFRESH_ENABLED:
        log.info(
            "price refresh: every %sh (on start: %s)",
            config.REFRESH_INTERVAL_HOURS, config.REFRESH_ON_START,
        )
        tasks.append(asyncio.create_task(
            _every("price-refresh", refresh_all_prices,
                   config.REFRESH_INTERVAL_HOURS, config.REFRESH_ON_START),
            name="price-refresh",
        ))
    else:
        log.info("price refresh: disabled")

    if config.BACKUP_ENABLED:
        log.info(
            "backups: every %sh into %s, keeping %s days",
            config.BACKUP_INTERVAL_HOURS, backup_dir(), config.BACKUP_RETENTION_DAYS,
        )
        tasks.append(asyncio.create_task(
            _every("backup", lambda: backup_database(skip_if_empty=True),
                   config.BACKUP_INTERVAL_HOURS, True),
            name="backup",
        ))
    else:
        log.info("backups: disabled")

    return tasks


async def stop(tasks):
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
