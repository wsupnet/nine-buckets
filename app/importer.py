"""Load a maintained trade-history spreadsheet into the lot ledger.

The workbook is a hand-maintained balance-sheet spreadsheet, not an exchange export,
in this shape:

- a **trade tab** (Crypto, Stocks or Trades) with one row per transaction, sells
  written as a negative quantity and a negative amount paid
- a **Precious Metals tab** with one block per metal side by side, each with its
  own Date / Troy Oz / Cost / Est. Premium columns

Nothing is written until a preview has been reviewed. The two rules that matter:
a holding whose ledger would come out **oversold is held back entirely** rather than
imported with an invented basis, and every row that is skipped is reported with the
reason and its spreadsheet row number, so nothing disappears quietly.
"""

import os
from datetime import date, datetime

from .db import now_iso
from .model import EPS, fifo_summary

# Sheets read as one-row-per-transaction, matched by tab name.
TRADE_SHEETS = {
    "crypto": "crypto",
    "stocks": "stock",
    "trades": None,          # asset class taken from the row instead
}

METAL_SHEET_HINTS = ("precious metal", "metals", "bullion")

# The metal block headings map onto the spot tickers the price feed uses.
METAL_TICKERS = {
    "SILVER": "XAG", "GOLD": "XAU", "PLATINUM": "XPT",
    "PALLADIUM": "XPD", "COPPER": "HG",
}

BUCKET_FOR_CLASS = {
    "crypto": "crypto",
    "metal": "precious_metals",
    "stock": "stocks",
}


class ImportError_(ValueError):
    """Raised for a workbook that can't be read at all, as opposed to bad rows."""


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def _text(v):
    v = _clean(v)
    return "" if v is None else str(v).strip()


def _number(v):
    """Spreadsheet cells are already typed; reject text that isn't a number."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v):
    """Returns YYYY-MM-DD, or None for a blank or an 'N/A' placeholder."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = _text(v)
    if not s or s.upper() in ("N/A", "NA", "-", "?", "UNKNOWN"):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _header_map(row):
    """Column index by lowercased header text, so column order can move."""
    out = {}
    for i, cell in enumerate(row):
        t = _text(cell).lower()
        if t:
            out.setdefault(t, i)
    return out


def _find(headers, *candidates):
    for c in candidates:
        for name, idx in headers.items():
            if name.startswith(c):
                return idx
    return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def read_workbook(path, undated_fallback="2026-01-01"):
    """Parse every recognised sheet into normalised rows.

    Returns {"rows": [...], "problems": [...], "sheets": [...]}. A row carries its
    sheet name and spreadsheet row number throughout, so anything reported later can
    be found in the original file.
    """
    try:
        import openpyxl
    except ImportError:                                   # pragma: no cover
        raise ImportError_(
            "Reading .xlsx needs the openpyxl package — add it to requirements.txt "
            "and rebuild the container."
        )

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ImportError_(f"That file couldn't be opened as a spreadsheet: {exc}")

    rows, problems, seen = [], [], []
    for ws in wb.worksheets:
        name = (ws.title or "").strip()
        key = name.lower()
        if key in TRADE_SHEETS:
            found = _parse_trades(ws, name, TRADE_SHEETS[key], problems,
                                  undated_fallback)
        elif any(h in key for h in METAL_SHEET_HINTS):
            found = _parse_metals(ws, name, problems, undated_fallback)
        else:
            continue
        seen.append({"name": name, "rows": len(found)})
        rows.extend(found)

    wb.close()
    if not seen:
        raise ImportError_(
            "No recognised sheets. Expected a tab named Crypto, Stocks or Trades, "
            "and/or one named Precious Metals."
        )
    return {"rows": rows, "problems": problems, "sheets": seen}


def _parse_trades(ws, sheet, default_class, problems, undated_fallback="2026-01-01"):
    grid = [r for r in ws.iter_rows(values_only=True)]
    if not grid:
        return []
    headers = _header_map(grid[0])
    c_date = _find(headers, "date")
    c_plat = _find(headers, "platform", "exchange", "location", "account")
    c_tick = _find(headers, "ticker", "symbol")
    c_name = _find(headers, "crypto", "asset", "name")
    c_qty = _find(headers, "quantity", "qty", "amount")
    c_paid = _find(headers, "total paid", "cost", "total")

    missing = [n for n, c in (("Date", c_date), ("Ticker", c_tick),
                              ("Quantity", c_qty)) if c is None]
    if missing:
        problems.append({
            "sheet": sheet, "row": 1, "kind": "sheet",
            "message": f"Skipped the whole sheet — no {', '.join(missing)} column",
        })
        return []

    out = []
    for n, raw in enumerate(grid[1:], start=2):
        def cell(i):
            return raw[i] if i is not None and i < len(raw) else None

        ticker = _text(cell(c_tick)).upper()
        qty = _number(cell(c_qty))
        if not ticker or qty is None:
            # A row that had *something* in the quantity cell but didn't parse is a
            # typo, not a blank. Dropping it silently loses real holdings and the
            # reconciliation can't catch it either: sheet_net is summed downstream
            # in plan(), so a row discarded here never reaches the comparison that
            # is supposed to prove nothing went missing.
            if ticker and _text(cell(c_qty)):
                problems.append({
                    "sheet": sheet, "row": n, "kind": "skipped",
                    "message": f"{ticker}: quantity {_text(cell(c_qty))!r} isn't a "
                               f"number — row not imported",
                })
            continue                       # blank row or a stray summary cell
        if abs(qty) < EPS:
            problems.append({"sheet": sheet, "row": n, "kind": "skipped",
                             "message": f"{ticker}: quantity is zero"})
            continue

        # lots.trade_date is NOT NULL, and FIFO orders by it. A row whose date is
        # blank or unrecognised has to be given one here or the whole import dies
        # on the insert — long after the preview said it was fine.
        when = _date(cell(c_date))
        if when is None:
            problems.append({
                "sheet": sheet, "row": n, "kind": "dated",
                "message": f"{ticker}: no purchase date, using {undated_fallback}",
            })
            when = undated_fallback

        out.append({
            "sheet": sheet, "row": n,
            "date": when,
            "location": _text(cell(c_plat)),
            "ticker": ticker,
            "asset_name": _text(cell(c_name)) or ticker,
            "asset_class": default_class or "crypto",
            "quantity": qty,
            "total": _number(cell(c_paid)),
            "premium_total": None,
            "unit": None,
            "note": "",
        })
    return out


def _parse_metals(ws, sheet, problems, undated_fallback):
    """Read the side-by-side metal blocks.

    Each block starts at a 'Date Purchased' header and runs until the next one, so
    any number of metals can sit next to each other.
    """
    grid = [r for r in ws.iter_rows(values_only=True)]
    if len(grid) < 3:
        return []

    header_row = None
    for i, r in enumerate(grid[:6]):
        if any(_text(c).lower().startswith("date purchased") for c in r):
            header_row = i
            break
    if header_row is None:
        problems.append({"sheet": sheet, "row": 1, "kind": "sheet",
                         "message": "Skipped — no 'Date Purchased' header found"})
        return []

    starts = [i for i, c in enumerate(grid[header_row])
              if _text(c).lower().startswith("date purchased")]

    # The metal's name sits in the row above, at or before its block's first column.
    labels = grid[header_row - 1] if header_row else []

    out = []
    for b, start in enumerate(starts):
        end = starts[b + 1] if b + 1 < len(starts) else len(grid[header_row])
        head = _header_map(grid[header_row][start:end])

        label = ""
        for i in range(min(start, len(labels) - 1), -1, -1):
            if _text(labels[i]):
                label = _text(labels[i]).upper()
                break
        ticker = METAL_TICKERS.get(label)
        if not ticker:
            problems.append({
                "sheet": sheet, "row": header_row, "kind": "sheet",
                "message": f"Skipped a block headed {label or '(blank)'} — not a "
                           f"metal I have a spot price for",
            })
            continue

        c_date = _find(head, "date")
        c_qty = _find(head, "troy oz", "oz", "weight", "quantity")
        c_desc = _find(head, "item desc", "desc", "item")
        c_from = _find(head, "from", "dealer", "source")
        c_cost = _find(head, "cost", "total")
        c_prem = _find(head, "est. premium", "premium")
        if c_qty is None or c_cost is None:
            continue

        for n, raw in enumerate(grid[header_row + 1:], start=header_row + 2):
            seg = raw[start:end]

            def cell(i):
                return seg[i] if i is not None and i < len(seg) else None

            qty = _number(cell(c_qty))
            cost = _number(cell(c_cost))
            if qty is None or abs(qty) < EPS:
                # Same trap as the trade sheets — but these blocks are mostly empty
                # cells by design, so only a cell with unparseable *content* is
                # worth reporting.
                if qty is None and _text(cell(c_qty)):
                    problems.append({
                        "sheet": sheet, "row": n, "kind": "skipped",
                        "message": f"{label.title()}: quantity "
                                   f"{_text(cell(c_qty))!r} isn't a number — row "
                                   f"not imported",
                    })
                continue
            when = _date(cell(c_date))
            if when is None:
                problems.append({
                    "sheet": sheet, "row": n, "kind": "dated",
                    "message": f"{label.title()}: no purchase date, using "
                               f"{undated_fallback}",
                })
                when = undated_fallback

            desc = _text(cell(c_desc))
            dealer = _text(cell(c_from))
            note = " · ".join(p for p in (desc, dealer) if p)

            out.append({
                "sheet": sheet, "row": n,
                "date": when,
                "location": "",
                "ticker": ticker,
                "asset_name": label.title(),
                "asset_class": "metal",
                "quantity": qty,
                "total": cost,
                "premium_total": _number(cell(c_prem)),
                "unit": "toz",
                "note": note[:200],
            })
    return out


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def group_key(row, mapping):
    """(ticker, destination account name) — one holding per asset per location."""
    return (row["ticker"], (mapping or {}).get(row["ticker"], row["location"]).strip())


def default_locations(rows):
    """Suggested destination per ticker: whichever location it appears in most.

    Only a starting point. A buy is recorded where it was *bought*, which for anything
    later withdrawn to cold storage is not where it lives now — so the preview asks.
    """
    # One canonical spelling per place, chosen across the whole sheet. Done globally
    # rather than per ticker, or "Exchange A" on one row and "exchange a" on another end
    # up as two different accounts.
    spellings = {}
    for r in rows:
        loc = (r["location"] or "").strip()
        if loc:
            spellings.setdefault(loc.lower(), {})
            spellings[loc.lower()][loc] = spellings[loc.lower()].get(loc, 0) + 1
    canonical = {k: max(v, key=v.get) for k, v in spellings.items()}

    tally = {}
    for r in rows:
        loc = (r["location"] or "").strip().lower()
        if not loc:
            continue
        counts = tally.setdefault(r["ticker"], {})
        counts[loc] = counts.get(loc, 0) + 1

    return {ticker: canonical[max(counts, key=counts.get)]
            for ticker, counts in tally.items()}


def plan(rows, mapping, conn):
    """Work out what the import would do, without doing any of it.

    Every holding is run through the same FIFO the app uses, so the preview shows the
    quantity and cost basis you would actually end up with — and catches a ledger that
    comes out oversold before it reaches the database.
    """
    groups = {}
    skipped = []

    for r in rows:
        is_sell = r["quantity"] < 0
        qty = abs(r["quantity"])
        total = r["total"]

        key = group_key(r, mapping)
        g = groups.setdefault(key, {
            "ticker": r["ticker"],
            "account": key[1],
            "asset_name": r["asset_name"],
            "asset_class": r["asset_class"],
            "unit": r["unit"],
            "lots": [],
            "zero_cost": 0,
            "premium_rows": 0,
            # Every row's signed quantity, skipped ones included. Compared against
            # what actually gets imported, this is the reconciliation against your
            # spreadsheet's own total — a difference means a row didn't make it.
            "sheet_net": 0.0,
            "skipped": 0,
        })
        g["sheet_net"] += r["quantity"]

        if total is None and is_sell:
            # A sell with no proceeds would book its whole basis as a loss.
            skipped.append({**r, "reason": "sell with no amount recorded"})
            g["skipped"] += 1
            continue
        if total is None:
            total = 0.0                    # a buy with no cost: basis of zero, flagged

        gross = abs(total)
        premium_unit = 0.0
        if r["premium_total"] and qty > EPS and not is_sell:
            premium_unit = abs(r["premium_total"]) / qty
        # Spot is what's left once the dealer's markup is taken out of the total.
        unit_price = (gross / qty) - premium_unit if qty > EPS else 0.0

        if total == 0.0 and not is_sell:
            g["zero_cost"] += 1
        if premium_unit:
            g["premium_rows"] += 1
        g["lots"].append({
            "sheet": r["sheet"], "row": r["row"],
            "side": "sell" if is_sell else "buy",
            "trade_date": r["date"],
            "quantity": qty,
            "price": unit_price,
            "premium": premium_unit,
            "unit": r["unit"],
            "fee": 0.0,
            "note": r["note"],
        })

    holdings, held_back = [], []
    for key in sorted(groups, key=lambda k: (k[0], k[1])):
        g = groups[key]
        g["lots"].sort(key=lambda l: (l["trade_date"] or "", l["row"]))
        summary = fifo_summary([
            {"side": l["side"], "quantity": l["quantity"],
             "price": l["price"] + l["premium"], "fee": l["fee"]}
            for l in g["lots"]
        ])

        existing = conn.execute(
            "SELECT i.id, i.name, i.quantity, a.name AS account_name,"
            " (SELECT COUNT(*) FROM lots WHERE item_id = i.id) AS lot_count"
            " FROM items i LEFT JOIN accounts a ON a.id = i.account_id"
            " WHERE i.kind='priced' AND UPPER(COALESCE(i.ticker,'')) = ?"
            "   AND COALESCE(a.name,'') = ?",
            (g["ticker"], g["account"]),
        ).fetchone()

        entry = {
            **g,
            "buy_count": sum(1 for l in g["lots"] if l["side"] == "buy"),
            "sell_count": sum(1 for l in g["lots"] if l["side"] == "sell"),
            "quantity": summary["quantity"],
            "cost_basis": summary["cost_basis"],
            "avg_cost": summary["avg_cost"],
            "realized": summary["realized"],
            "oversold": summary["oversold"],
            "existing_id": existing["id"] if existing else None,
            "existing_name": existing["name"] if existing else None,
            "existing_quantity": existing["quantity"] if existing else None,
            "existing_lots": existing["lot_count"] if existing else 0,
        }
        # Reconciliation: what the sheet's own rows add up to, versus what would
        # actually be imported. Any gap is a skipped row, and it is the difference
        # between the app agreeing with your spreadsheet and quietly not.
        entry["sheet_diff"] = summary["quantity"] - g["sheet_net"]
        entry["reconciles"] = abs(entry["sheet_diff"]) <= EPS

        if summary["oversold"] > EPS:
            # Held back rather than imported with a gap: the sells outrun the buys, so
            # some purchase is missing from the sheet and any basis here would be made
            # up. Adding the missing buys and re-importing fixes it by itself.
            entry["reason"] = (
                f"sells exceed buys by {summary['oversold']:.8f}".rstrip("0").rstrip(".")
                + " — a purchase is missing from the sheet"
            )
            held_back.append(entry)
        else:
            holdings.append(entry)

    return {
        "holdings": holdings,
        "held_back": held_back,
        "skipped": skipped,
        "lot_total": sum(len(h["lots"]) for h in holdings),
        "zero_cost_total": sum(h["zero_cost"] for h in holdings),
    }


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------

def commit(conn, planned, source, mapping_note=""):
    """Write the planned holdings and lots as one reversible batch."""
    cur = conn.execute(
        "INSERT INTO import_batches (source, imported_at, note) VALUES (?,?,?)",
        (source, now_iso(), mapping_note or None),
    )
    batch = cur.lastrowid

    buckets = {r["key"]: r["id"] for r in conn.execute("SELECT key, id FROM buckets")}
    made_items = 0
    made_lots = 0

    for h in planned["holdings"]:
        item_id = h["existing_id"]
        if not item_id:
            account_id = None
            if h["account"]:
                conn.execute("INSERT OR IGNORE INTO accounts (name) VALUES (?)",
                             (h["account"],))
                account_id = conn.execute(
                    "SELECT id FROM accounts WHERE name = ?", (h["account"],)
                ).fetchone()["id"]
            conn.execute(
                "INSERT INTO items (name, kind, bucket_id, account_id, currency,"
                " ticker, asset_class, unit, import_batch_id, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (h["asset_name"], "priced",
                 buckets.get(BUCKET_FOR_CLASS.get(h["asset_class"])),
                 account_id, "USD", h["ticker"], h["asset_class"],
                 h["unit"] if h["asset_class"] == "metal" else None,
                 batch, now_iso()),
            )
            item_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            made_items += 1

        for l in h["lots"]:
            conn.execute(
                "INSERT INTO lots (item_id, side, trade_date, quantity, price,"
                " premium, unit, fee, currency, note, import_batch_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, l["side"], l["trade_date"], l["quantity"], l["price"],
                 l["premium"], l["unit"], l["fee"], "USD",
                 l["note"] or None, batch, now_iso()),
            )
            made_lots += 1

    conn.execute(
        "UPDATE import_batches SET lot_count=?, item_count=? WHERE id=?",
        (made_lots, made_items, batch),
    )
    conn.commit()
    return {"batch": batch, "lots": made_lots, "items": made_items}


def rollback(conn, batch_id):
    """Undo an import run.

    Holdings the run created are removed only when nothing is left in them — one that
    has since been traded or transferred stays, and is reported instead of being
    deleted out from under that history.
    """
    conn.execute("DELETE FROM lots WHERE import_batch_id=?", (batch_id,))

    removed, kept = 0, []
    for r in conn.execute(
        "SELECT id, name FROM items WHERE import_batch_id=?", (batch_id,)
    ).fetchall():
        lots_left = conn.execute(
            "SELECT COUNT(*) c FROM lots WHERE item_id=?", (r["id"],)
        ).fetchone()["c"]
        moved = conn.execute(
            "SELECT COUNT(*) c FROM holding_transfers"
            " WHERE from_item_id=? OR to_item_id=?", (r["id"], r["id"])
        ).fetchone()["c"]
        if lots_left or moved:
            kept.append(r["name"])
            continue
        conn.execute("DELETE FROM items WHERE id=?", (r["id"],))
        removed += 1

    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    conn.commit()
    return {"items_removed": removed, "items_kept": kept}


def staged_path(directory):
    return os.path.join(directory, "import-staged.xlsx")
