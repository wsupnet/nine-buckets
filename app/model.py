"""Valuation: turns stored items into USD values, bucket totals and drift."""

from .db import connect, now_iso

KINDS = ["cash", "priced", "property", "manual", "liability"]
ASSET_CLASSES = ["stock", "crypto", "metal"]


EPS = 1e-9

GRAMS_PER_TROY_OZ = 31.1034768

# Everything is stored and valued in troy ounces internally; these convert whatever
# unit was typed. A 100 g bar and a 1 oz coin can then sit in the same ledger.
TROY_OZ_PER_UNIT = {
    "toz": 1.0,
    "g": 1.0 / GRAMS_PER_TROY_OZ,
    "kg": 1000.0 / GRAMS_PER_TROY_OZ,
    "lb": 14.583333333333334,
}

UNIT_LABELS = {
    "toz": "troy oz",
    "g": "grams",
    "kg": "kilos",
    "lb": "pounds",
}


def to_troy_oz(qty, unit):
    if qty is None:
        return None
    return qty * TROY_OZ_PER_UNIT.get(unit or "toz", 1.0)


def from_troy_oz(toz, unit):
    if toz is None:
        return None
    return toz / TROY_OZ_PER_UNIT.get(unit or "toz", 1.0)


def is_metal(item):
    return item["kind"] == "priced" and item["asset_class"] == "metal"


def is_property(item):
    return item["kind"] == "property"


def is_closed(kind, quantity, oversold=0.0):
    """A priced holding that has been fully sold or moved away.

    Worth hiding rather than deleting: the lots behind it still carry the realized
    gain and the record of what was bought, which deleting the holding would throw
    away. A position bought and sold to nothing is history, not a holding.

    An oversold ledger also reads as zero, but that is a gap in the record, not a
    closed position — it stays in view with its warning instead of being tucked away.
    """
    if (oversold or 0.0) > EPS:
        return False
    return kind == "priced" and quantity is not None and abs(quantity) <= EPS


# Sides that take units off the front of the FIFO queue. A sell realizes a gain; a
# transfer out does not — the units are still owned, just somewhere else.
CONSUMING_SIDES = ("sell", "transfer_out")


def fifo_state(lots):
    """One pass of FIFO matching, oldest buys first.

    Returns the buy slices still held — each keeping its trade date and unit cost so
    a transfer can carry both to the destination — plus realized gain and any
    oversold quantity (disposals with no buy left to match, meaning the ledger is
    incomplete). Fees increase the cost of a buy and reduce the proceeds of a sell.

    A `transfer_out` consumes the queue exactly like a sell but recognises no gain:
    the basis leaves with the units and reappears on the destination holding.
    """
    queue = []
    realized = 0.0
    oversold = 0.0
    proceeds_total = 0.0

    for l in lots:
        qty = float(l["quantity"] or 0)
        price = float(l["price"] or 0)
        fee = float(l["fee"] or 0)
        if qty <= 0:
            continue

        if l["side"] in CONSUMING_SIDES:
            sold = l["side"] == "sell"
            remaining = qty
            proceeds = (qty * price - fee) if sold else 0.0
            if sold:
                proceeds_total += proceeds
            cost = 0.0
            while remaining > EPS and queue:
                head = queue[0]
                take = min(remaining, head["quantity"])
                cost += take * head["unit_cost"]
                head["quantity"] -= take
                remaining -= take
                if head["quantity"] <= EPS:
                    queue.pop(0)
            if remaining > EPS:
                # Disposing of more than the ledger says was bought: count the
                # matched portion only, and surface the gap rather than inventing
                # a basis.
                oversold += remaining
                if sold:
                    proceeds -= remaining * price
            if sold:
                realized += proceeds - cost
        else:
            queue.append({
                "trade_date": l.get("trade_date"),
                "quantity": qty,
                # Fee-inclusive, so it is the whole basis of these units.
                "unit_cost": (qty * price + fee) / qty,
                "premium": float(l.get("premium") or 0.0),
                "currency": l.get("currency"),
            })

    return {"queue": queue, "realized": realized,
            "oversold": oversold, "proceeds": proceeds_total}


def fifo_summary(lots):
    """Held quantity, remaining cost basis, realized gain and oversold quantity."""
    st = fifo_state(lots)
    held = sum(s["quantity"] for s in st["queue"])
    basis = sum(s["quantity"] * s["unit_cost"] for s in st["queue"])
    return {
        "quantity": held,
        "cost_basis": basis,
        "avg_cost": (basis / held) if held > EPS else None,
        "realized": st["realized"],
        "oversold": st["oversold"],
        "proceeds": st["proceeds"],
    }


def normalise_lot(row, metal=False):
    """Restate a lot in the units FIFO works in.

    For metals that means troy ounces, with the premium folded into the unit price
    so cost basis includes what you paid over spot. Quantity and price are scaled
    together, so the lot's total cost is unchanged by the conversion. The premium is
    also carried separately, so a transfer can hand the dealer markup to the
    destination instead of burying it in the price.
    """
    qty = float(row["quantity"] or 0)
    price = float(row["price"] or 0)
    premium = float(row["premium"] or 0) if "premium" in row.keys() else 0.0
    unit = (row["unit"] if "unit" in row.keys() else None) or "toz"
    common = {
        "side": row["side"],
        "fee": row["fee"],
        "trade_date": row["trade_date"] if "trade_date" in row.keys() else None,
        "currency": row["currency"] if "currency" in row.keys() else None,
    }

    if not metal:
        return {**common, "quantity": qty, "price": price + premium,
                "premium": premium}

    factor = TROY_OZ_PER_UNIT.get(unit, 1.0)
    qty_toz = qty * factor
    # (price + premium) is per entered unit; divide by the factor to get per troy oz.
    price_toz = (price + premium) / factor if factor else 0.0
    return {**common, "quantity": qty_toz, "price": price_toz,
            "premium": (premium / factor if factor else 0.0)}


def lot_summaries(conn):
    """{item_id: fifo summary} for every item that has lots.

    Metal summaries are expressed in troy ounces.
    """
    metals = {
        r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE kind='priced' AND asset_class='metal'"
        )
    }
    rows = conn.execute(
        "SELECT * FROM lots ORDER BY item_id, trade_date, id"
    ).fetchall()

    by_item = {}
    premium_paid = {}
    for r in rows:
        is_m = r["item_id"] in metals
        by_item.setdefault(r["item_id"], []).append(normalise_lot(r, is_m))
        # A transfer in brings its premium with it — those ounces really did cost
        # that much over spot, whichever vault they are sitting in now.
        if is_m and r["side"] in ("buy", "transfer_in"):
            premium_paid[r["item_id"]] = (
                premium_paid.get(r["item_id"], 0.0)
                + float(r["quantity"] or 0) * float(r["premium"] or 0)
            )

    out = {}
    for iid, lots in by_item.items():
        s = fifo_summary(lots)
        s["premium_paid"] = premium_paid.get(iid)
        out[iid] = s
    return out


def fx_rates(conn):
    rates = {r["currency"]: r["usd_per_unit"] for r in conn.execute("SELECT * FROM fx_rates")}
    rates["USD"] = 1.0
    return rates


def item_value_usd(item, rates, qty=None):
    if item["kind"] == "priced":
        if item["last_price"] is None or qty is None:
            return None
        rate = rates.get(item["price_currency"] or "USD", 1.0)
        return qty * item["last_price"] * rate
    if item["kind"] == "property":
        # A property is worth its equity: market value less what's still owed. The
        # mortgage lives here rather than as a separate liability, so the Real Estate
        # bucket reflects ownership instead of gross leveraged value.
        if item["amount"] is None:
            return None
        equity = item["amount"] - (item["loan_balance"] or 0.0)
        return equity * rates.get(item["currency"], 1.0)
    if item["amount"] is None:
        return None
    return item["amount"] * rates.get(item["currency"], 1.0)


def load_items(conn):
    rates = fx_rates(conn)
    summaries = lot_summaries(conn)
    rows = conn.execute(
        "SELECT i.*, b.name AS bucket_name, b.key AS bucket_key, a.name AS account_name"
        " FROM items i"
        " LEFT JOIN buckets b ON b.id = i.bucket_id"
        " LEFT JOIN accounts a ON a.id = i.account_id"
        " ORDER BY i.kind, i.name"
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        s = summaries.get(r["id"])
        # Hybrid rule: a holding with lots derives its quantity from them; one
        # without lots keeps the manually entered quantity.
        d["has_lots"] = s is not None
        d["lots"] = s
        metal = is_metal(r)
        d["is_metal"] = metal
        unit = (r["unit"] or "toz") if metal else None
        d["unit"] = unit
        d["unit_label"] = UNIT_LABELS.get(unit, unit) if metal else None

        if metal:
            # Lot summaries are already troy ounces; a manual quantity is in the
            # holding's own unit and has to be converted before valuing it.
            qty_toz = s["quantity"] if s else to_troy_oz(r["quantity"], unit)
            d["qty_toz"] = qty_toz
            # What to show in the weight column, in the unit that was entered.
            d["effective_quantity"] = from_troy_oz(qty_toz, unit)
            qty = qty_toz
        else:
            qty = s["quantity"] if s else r["quantity"]
            d["qty_toz"] = None
            d["effective_quantity"] = qty

        d["is_closed"] = is_closed(r["kind"], d["effective_quantity"],
                                   s["oversold"] if s else 0.0)
        d["value_usd"] = item_value_usd(r, rates, qty)
        if r["kind"] == "priced":
            d["native_value"] = (
                qty * r["last_price"]
                if qty is not None and r["last_price"] is not None
                else None
            )
        else:
            d["native_value"] = r["amount"]

        # Property: equity and payoff progress are derived, never typed.
        d["is_property"] = is_property(r)
        if d["is_property"]:
            value = r["amount"]
            balance = r["loan_balance"] or 0.0
            original = r["loan_original"] or 0.0
            rate = rates.get(r["currency"], 1.0)
            d["property_value_usd"] = value * rate if value is not None else None
            d["loan_balance_usd"] = balance * rate
            d["loan_original_usd"] = original * rate
            d["equity"] = (value - balance) if value is not None else None
            d["equity_pct"] = (
                (value - balance) / value * 100 if value else None
            )
            d["paid_off_pct"] = (
                (original - balance) / original * 100 if original else None
            )
        else:
            d["property_value_usd"] = None
            d["loan_balance_usd"] = None
            d["loan_original_usd"] = None
            d["equity"] = None
            d["equity_pct"] = None
            d["paid_off_pct"] = None

        # Spot is stored per troy ounce; show it in the unit the holding uses.
        d["spot_per_unit"] = (
            r["last_price"] * TROY_OZ_PER_UNIT.get(unit, 1.0)
            if metal and r["last_price"] is not None else None
        )

        rate = rates.get(r["price_currency"] or r["currency"] or "USD", 1.0)
        if s:
            d["cost_basis_usd"] = s["cost_basis"] * rate
            d["realized_usd"] = s["realized"] * rate
            d["unrealized_usd"] = (
                d["value_usd"] - d["cost_basis_usd"] if d["value_usd"] is not None else None
            )
            d["premium_paid_usd"] = (
                s["premium_paid"] * rate if s.get("premium_paid") else None
            )
        else:
            d["cost_basis_usd"] = None
            d["realized_usd"] = None
            d["unrealized_usd"] = None
            d["premium_paid_usd"] = None
        items.append(d)
    return items


def portfolio(conn):
    items = load_items(conn)
    buckets = [dict(b) for b in conn.execute("SELECT * FROM buckets ORDER BY sort_order")]

    totals = {b["id"]: 0.0 for b in buckets}
    assets = liabilities = 0.0
    unbucketed_liabilities = 0.0

    property_debt = 0.0
    for it in items:
        v = it["value_usd"]
        if v is None:
            continue
        if it["kind"] == "liability":
            # Liabilities sit outside the bucket system entirely — they reduce net
            # worth and nothing else.
            liabilities += v
            unbucketed_liabilities += v
        else:
            # A property's value_usd is already its equity, so the bucket and net
            # worth both reflect ownership rather than gross value.
            assets += v
            if it["kind"] == "property":
                property_debt += it["loan_balance_usd"] or 0.0
            if it["bucket_id"] in totals:
                totals[it["bucket_id"]] += v

    net_worth = assets - liabilities
    base = sum(totals[b["id"]] for b in buckets if not b["excluded"])

    for b in buckets:
        b["value_usd"] = totals[b["id"]]
        b["actual_pct"] = (b["value_usd"] / base * 100) if base and not b["excluded"] else 0.0
        b["drift_pct"] = 0.0 if b["excluded"] else b["actual_pct"] - b["target_pct"]
        b["drift_usd"] = 0.0 if b["excluded"] else (b["target_pct"] / 100 * base) - b["value_usd"]

    return {
        "items": items,
        "buckets": buckets,
        "assets": assets,
        "liabilities": liabilities,
        "net_worth": net_worth,
        "allocation_base": base,
        "unbucketed_liabilities": unbucketed_liabilities,
        "property_debt": property_debt,
        "gross_assets": assets + property_debt,
        "target_total": sum(b["target_pct"] for b in buckets if not b["excluded"]),
        "stale": [i for i in items if i["kind"] == "priced" and i["last_price"] is None],
    }


def accounts_view(conn):
    """Group the same items by account instead of by bucket.

    Account and bucket are independent dimensions of an item, so an account can
    legitimately span several buckets. Liabilities are kept in their own list per
    account rather than netted into the asset subtotal.
    """
    items = load_items(conn)
    buckets = [dict(b) for b in conn.execute("SELECT * FROM buckets ORDER BY sort_order")]
    accounts = [dict(a) for a in conn.execute("SELECT * FROM accounts ORDER BY sort_order, name")]

    groups = []
    for a in accounts + [{"id": None, "name": "No account"}]:
        mine = [i for i in items if i["account_id"] == a["id"]]
        # Named accounts stay visible even when empty so they can be renamed or removed;
        # the synthetic "No account" group only appears when something is actually in it.
        if a["id"] is None and not mine:
            continue
        # Closed positions are worth nothing, so they change no total — they would
        # just be empty rows in every grouped view.
        assets = [i for i in mine if i["kind"] != "liability" and not i["is_closed"]]
        liabs = [i for i in mine if i["kind"] == "liability"]

        assets_total = sum(i["value_usd"] or 0 for i in assets)
        liabs_total = sum(i["value_usd"] or 0 for i in liabs)

        splits = []
        for b in buckets:
            v = sum(i["value_usd"] or 0 for i in assets if i["bucket_id"] == b["id"])
            if v:
                splits.append({
                    "bucket_name": b["name"],
                    "bucket_key": b["key"],
                    "value_usd": v,
                    "pct": v / assets_total * 100 if assets_total else 0,
                })
        unbucketed = sum(i["value_usd"] or 0 for i in assets if i["bucket_id"] is None)
        if unbucketed:
            splits.append({
                "bucket_name": "Unassigned", "bucket_key": None,
                "value_usd": unbucketed,
                "pct": unbucketed / assets_total * 100 if assets_total else 0,
            })

        groups.append({
            "id": a["id"],
            "name": a["name"],
            "institution": a.get("institution"),
            "assets": assets,
            "liabilities": liabs,
            "assets_total": assets_total,
            "liabilities_total": liabs_total,
            "net": assets_total - liabs_total,
            "splits": splits,
            "bucket_count": len(splits),
        })

    # Matrix: accounts down, buckets across. Cells are assets only; liabilities
    # get their own column so they stay visible without being blended in.
    used = [b for b in buckets
            if any(i["bucket_id"] == b["id"] and i["kind"] != "liability" and i["value_usd"]
                   for i in items)]
    matrix_rows = []
    for g in groups:
        cells = {}
        for b in used:
            cells[b["id"]] = sum(
                i["value_usd"] or 0 for i in g["assets"] if i["bucket_id"] == b["id"]
            )
        # Bucketless assets need their own column, or a row's cells silently fail to
        # add up to its Assets total.
        cells["none"] = sum(
            i["value_usd"] or 0 for i in g["assets"] if i["bucket_id"] is None
        )
        matrix_rows.append({**g, "cells": cells})

    col_totals = {b["id"]: sum(r["cells"][b["id"]] for r in matrix_rows) for b in used}
    col_totals["none"] = sum(r["cells"]["none"] for r in matrix_rows)

    return {
        "groups": groups,
        "matrix_buckets": used,
        "matrix_rows": matrix_rows,
        "col_totals": col_totals,
        "has_unbucketed": col_totals["none"] > 0,
        "grand_assets": sum(g["assets_total"] for g in groups),
        "grand_liabilities": sum(g["liabilities_total"] for g in groups),
    }


def group_by_ticker(items, total=None):
    """Collapse holdings of the same ticker into one row per asset.

    The same coin held in three places is one position split across three
    locations, but the bucket table listed it as three unrelated rows, so the
    Crypto bucket read as if it held three different things. Grouping by ticker
    puts the position back together and keeps the split as `parts`.

    Only tickered holdings merge. Cash and manual items have no ticker, and two of
    them sharing a name is a coincidence rather than the same asset, so they are
    always left alone.

    Each row carries `parts` (per account, biggest first) and `is_group`. A row's
    value is None only when nothing in it has been priced yet — otherwise an
    unpriced member contributes nothing rather than voiding the whole total.
    """
    rows, groups, order = [], {}, []
    for i in items:
        ticker = (i["ticker"] or "").strip().upper()
        if not ticker:
            rows.append({
                "name": i["name"], "ticker": i["ticker"], "kind": i["kind"],
                "account_name": i["account_name"], "value_usd": i["value_usd"],
                "is_group": False, "parts": [],
            })
            continue
        if ticker not in groups:
            groups[ticker] = []
            order.append(ticker)
        groups[ticker].append(i)

    for ticker in order:
        members = groups[ticker]
        priced = [m["value_usd"] for m in members if m["value_usd"] is not None]
        value = sum(priced) if priced else None

        # Per account, so two rows that happen to sit in the same place count once.
        per_account, seen = {}, []
        for m in members:
            key = m["account_name"] or "No account"
            if key not in per_account:
                per_account[key] = None
                seen.append(key)
            if m["value_usd"] is not None:
                per_account[key] = (per_account[key] or 0) + m["value_usd"]
        parts = [{"account_name": k, "value_usd": per_account[k]} for k in seen]
        parts.sort(key=lambda p: -(p["value_usd"] or 0))

        # The name shown is the biggest holding's, so the row reads the way the
        # position is usually thought of rather than by whichever was added first.
        lead = max(members, key=lambda m: (m["value_usd"] or 0))
        rows.append({
            "name": lead["name"], "ticker": lead["ticker"], "kind": lead["kind"],
            "account_name": parts[0]["account_name"] if len(parts) == 1 else None,
            "value_usd": value,
            "is_group": len(parts) > 1,
            "parts": parts,
        })

    rows.sort(key=lambda r: -(r["value_usd"] or 0))
    # Always set, never conditionally: a bucket whose holdings haven't been priced
    # yet totals zero, and a missing key here is an UndefinedError in the template
    # rather than a blank cell.
    for r in rows:
        r["pct"] = (r["value_usd"] or 0) / total * 100 if total else 0.0
        for part in r["parts"]:
            part["pct"] = (part["value_usd"] or 0) / total * 100 if total else 0.0
    return rows


def by_value_desc(items):
    """Sort biggest-first, tolerating items with no value yet.

    Jinja's sort filter compares raw values, so a holding whose price hasn't been
    fetched (value_usd is None) crashes the page as soon as there's a second item
    to compare it against. Sorting here keeps that out of the templates.
    """
    return sorted(items, key=lambda i: (i.get("value_usd") is not None,
                                        i.get("value_usd") or 0.0), reverse=True)


def convert_amount(conn, amount, from_ccy, to_ccy):
    """Convert between two currencies via the stored USD rates."""
    if amount is None:
        return None
    if (from_ccy or "USD") == (to_ccy or "USD"):
        return amount
    rates = fx_rates(conn)
    usd = amount * rates.get(from_ccy or "USD", 1.0)
    return usd / rates.get(to_ccy or "USD", 1.0)


def movable_items(conn):
    """Items whose value is a stored amount, so money can be moved in or out.

    Priced holdings are excluded: their value is quantity x price, so you can't
    add dollars to them — buying more is a lot, not a transfer.
    """
    return [
        dict(r) for r in conn.execute(
            "SELECT i.*, a.name AS account_name, b.name AS bucket_name"
            " FROM items i"
            " LEFT JOIN accounts a ON a.id = i.account_id"
            " LEFT JOIN buckets b ON b.id = i.bucket_id"
            " WHERE i.kind IN ('cash','manual','liability','property')"
            " ORDER BY a.name IS NULL, a.name, i.name"
        )
    ]


def movable_holdings(conn):
    """Priced holdings, as the two ends of a holding transfer.

    These are the ones `movable_items` deliberately leaves out: their value is
    quantity x price, so moving one is a move of *units*, not of dollars.
    """
    out = []
    for i in load_items(conn):
        if i["kind"] != "priced":
            continue
        out.append({
            "id": i["id"],
            "name": i["name"],
            "ticker": i["ticker"],
            "asset_class": i["asset_class"],
            "account_id": i["account_id"],
            "account_name": i["account_name"],
            "bucket_id": i["bucket_id"],
            "currency": i["currency"],
            "price_currency": i["price_currency"],
            "is_metal": i["is_metal"],
            "unit": i["unit"],
            "unit_label": i["unit_label"],
            "has_lots": i["has_lots"],
            "typed_quantity": i["quantity"],
            "quantity": i["effective_quantity"],
        })
    out.sort(key=lambda h: ((h["ticker"] or "").upper(), h["account_name"] or "",
                            h["name"]))
    return out


def plan_holding_transfer(lots, qty_sent, qty_received):
    """Work out what basis leaves the source and what lots the destination gets.

    Consumes the source's FIFO queue oldest-first for `qty_sent`, then rescales the
    slices so that `qty_received` carries the *whole* basis of what was sent. That
    is what makes a network fee land as a higher unit cost on the destination rather
    than as a phantom gain, a loss, or a hole in the basis.

    Each slice keeps its original trade date, so FIFO ordering survives the move and
    a later sell still matches the genuinely oldest units first.

    Returns (slices, basis_moved, shortfall). A non-zero shortfall means the ledger
    holds fewer units than were sent — the caller should refuse rather than move
    units it has no recorded basis for.
    """
    queue = fifo_state(lots)["queue"]

    taken = []
    remaining = qty_sent
    for slice_ in queue:
        if remaining <= EPS:
            break
        take = min(remaining, slice_["quantity"])
        taken.append({**slice_, "quantity": take})
        remaining -= take
    shortfall = remaining if remaining > EPS else 0.0

    basis_moved = sum(t["quantity"] * t["unit_cost"] for t in taken)

    # Scale quantity down and unit cost up by the same factor, so each slice's total
    # cost comes out the other side unchanged.
    k = (qty_received / qty_sent) if qty_sent > EPS else 1.0
    slices = []
    for t in taken:
        qty = t["quantity"] * k
        if qty <= EPS:
            continue
        unit_cost = t["unit_cost"] / k if k > EPS else t["unit_cost"]
        premium = t["premium"] / k if k > EPS else t["premium"]
        slices.append({
            "trade_date": t["trade_date"],
            "quantity": qty,
            # unit_cost is fee-inclusive and always at least the premium, so the
            # remainder is a valid base price.
            "price": unit_cost - premium,
            "premium": premium,
            "currency": t["currency"],
        })
    return slices, basis_moved, shortfall


def adjust_quantity(conn, item_id, delta):
    """Add `delta` to a holding's typed quantity.

    Only valid for a holding with no lots — one with lots derives its quantity from
    them, and writing the field would be silently overruled.
    """
    row = conn.execute("SELECT quantity FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None, False
    new = (row["quantity"] or 0.0) + delta
    conn.execute("UPDATE items SET quantity=?, updated_at=? WHERE id=?",
                 (new, now_iso(), item_id))
    return new, new < -EPS


def adjust_amount(conn, item_id, delta):
    """Add `delta` (in the item's own currency) to a stored amount.

    Returns (new_amount, went_negative). A liability's `amount` is the size of the
    debt, so callers pass a negative delta to pay it down.
    """
    row = conn.execute("SELECT amount FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None, False
    current = row["amount"] or 0.0
    new = current + delta
    conn.execute("UPDATE items SET amount=?, updated_at=? WHERE id=?",
                 (new, now_iso(), item_id))
    return new, new < -EPS


def adjust_loan(conn, item_id, payment):
    """Reduce a property's mortgage by `payment` (in the property's currency).

    Paying the loan raises net worth by the principal paid, because the property's
    value is unchanged while the debt against it shrinks.
    """
    row = conn.execute("SELECT loan_balance FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None
    new = (row["loan_balance"] or 0.0) - payment
    conn.execute("UPDATE items SET loan_balance=?, updated_at=? WHERE id=?",
                 (new, now_iso(), item_id))
    return new


def buckets_view(conn):
    """The transpose of accounts_view: group by bucket, split each by account.

    Answers "where is this bucket's money actually sitting?"
    """
    items = load_items(conn)
    p = portfolio(conn)
    accounts = [dict(a) for a in conn.execute("SELECT * FROM accounts ORDER BY sort_order, name")]
    by_id = {b["id"]: b for b in p["buckets"]}

    groups = []
    for b in p["buckets"] + [{"id": None, "name": "Unassigned", "key": None, "excluded": 1}]:
        mine = [i for i in items if i["bucket_id"] == b["id"]]
        if b["id"] is None and not mine:
            continue
        assets = [i for i in mine if i["kind"] != "liability" and not i["is_closed"]]
        liabs = [i for i in mine if i["kind"] == "liability"]
        assets_total = sum(i["value_usd"] or 0 for i in assets)
        liabs_total = sum(i["value_usd"] or 0 for i in liabs)

        splits = []
        for a in accounts + [{"id": None, "name": "No account"}]:
            v = sum(i["value_usd"] or 0 for i in assets if i["account_id"] == a["id"])
            if v:
                splits.append({
                    "account_name": a["name"],
                    "value_usd": v,
                    "pct": v / assets_total * 100 if assets_total else 0,
                })
        splits.sort(key=lambda s: -s["value_usd"])

        full = by_id.get(b["id"], {})
        groups.append({
            "id": b["id"],
            "name": b["name"],
            "key": b.get("key"),
            "excluded": b.get("excluded", 0),
            "assets": by_value_desc(assets),
            "rows": group_by_ticker(assets, assets_total),
            "liabilities": liabs,
            "assets_total": assets_total,
            "liabilities_total": liabs_total,
            "net": assets_total - liabs_total,
            "splits": splits,
            "account_count": len(splits),
            "target_pct": full.get("target_pct"),
            "actual_pct": full.get("actual_pct"),
            "drift_pct": full.get("drift_pct"),
            "drift_usd": full.get("drift_usd"),
        })

    used_accounts = [
        a for a in accounts
        if any(i["account_id"] == a["id"] and i["kind"] != "liability" and i["value_usd"]
               for i in items)
    ]
    matrix_rows = []
    for g in groups:
        cells = {a["id"]: sum(i["value_usd"] or 0 for i in g["assets"]
                              if i["account_id"] == a["id"]) for a in used_accounts}
        cells["none"] = sum(i["value_usd"] or 0 for i in g["assets"]
                            if i["account_id"] is None)
        matrix_rows.append({**g, "cells": cells})

    col_totals = {a["id"]: sum(r["cells"][a["id"]] for r in matrix_rows) for a in used_accounts}
    col_totals["none"] = sum(r["cells"]["none"] for r in matrix_rows)

    return {
        "groups": groups,
        "matrix_accounts": used_accounts,
        "matrix_rows": matrix_rows,
        "col_totals": col_totals,
        "has_no_account": col_totals["none"] > 0,
        "grand_assets": sum(g["assets_total"] for g in groups),
        "grand_liabilities": sum(g["liabilities_total"] for g in groups),
        "allocation_base": p["allocation_base"],
    }


def snapshot_series_monthly(conn, colors=None):
    """One point per calendar month, using that month's most recent snapshot.

    Months with no snapshot are skipped rather than interpolated — an invented
    figure between two real ones would be indistinguishable from a recorded one.
    Backfilled entries carry totals but no breakdown, which the chart renders as a
    plain bar.
    """
    from datetime import datetime

    colors = colors or {}
    buckets = {b["id"]: b for b in conn.execute("SELECT * FROM buckets ORDER BY sort_order")}

    rows = conn.execute("SELECT * FROM snapshots ORDER BY taken_at ASC, id ASC").fetchall()
    by_month = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["taken_at"])
        except (TypeError, ValueError):
            continue
        by_month[f"{dt.year:04d}-{dt.month:02d}"] = (r, dt)   # later rows win

    lines, items = {}, {}
    for r in conn.execute("SELECT * FROM snapshot_lines"):
        lines.setdefault(r["snapshot_id"], []).append(r)
    try:
        for r in conn.execute("SELECT * FROM snapshot_items"):
            items.setdefault(r["snapshot_id"], []).append(r)
    except Exception:
        items = {}

    out = []
    for month in sorted(by_month):
        r, dt = by_month[month]
        sid = r["id"]

        asset_parts = []
        for line in lines.get(sid, []):
            b = buckets.get(line["bucket_id"])
            if not b or not line["value_usd"]:
                continue
            kids = [
                {"name": i["name"], "value": i["value_usd"]}
                for i in items.get(sid, [])
                if i["bucket_id"] == line["bucket_id"] and i["kind"] != "liability"
                and i["value_usd"]
            ]
            asset_parts.append({
                "name": b["name"],
                "value": line["value_usd"],
                "color": colors.get(b["key"], "#8d99a8"),
                "children": sorted(kids, key=lambda k: -k["value"]),
            })
        asset_parts.sort(key=lambda s: -s["value"])

        liab_parts = [
            {"name": i["name"], "value": i["value_usd"], "color": "#ef5f68", "children": []}
            for i in items.get(sid, []) if i["kind"] == "liability" and i["value_usd"]
        ]
        liab_parts.sort(key=lambda s: -s["value"])

        out.append({
            "month": month,
            "t": int(dt.timestamp() * 1000),
            "label": r["label"] or "",
            "assets": r["assets_usd"] or 0.0,
            "liabilities": r["liabilities_usd"] or 0.0,
            "nw": r["net_worth_usd"] or 0.0,
            "assetParts": asset_parts,
            "liabParts": liab_parts,
            # No stored breakdown: the bar draws as one solid block.
            "detailed": bool(asset_parts),
        })
    return out


def snapshot_series(conn):
    """Chart-ready points. Time-scaled on real dates, so sparse yearly history and
    denser monthly history can sit on the same axis without distortion."""
    from datetime import datetime

    out = []
    rows = conn.execute(
        "SELECT * FROM snapshots ORDER BY taken_at ASC, id ASC"
    ).fetchall()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["taken_at"])
        except (TypeError, ValueError):
            continue
        out.append({
            "t": int(dt.timestamp() * 1000),
            "nw": r["net_worth_usd"],
            "assets": r["assets_usd"],
            "liab": r["liabilities_usd"],
            "label": r["label"] or "",
        })
    return out


def take_snapshot(conn, label=None, taken_at=None):
    from .db import now_iso

    p = portfolio(conn)
    cur = conn.execute(
        "INSERT INTO snapshots (taken_at, label, assets_usd, liabilities_usd, net_worth_usd)"
        " VALUES (?,?,?,?,?)",
        (taken_at or now_iso(), label, p["assets"], p["liabilities"], p["net_worth"]),
    )
    sid = cur.lastrowid
    for b in p["buckets"]:
        conn.execute(
            "INSERT INTO snapshot_lines (snapshot_id, bucket_id, value_usd) VALUES (?,?,?)",
            (sid, b["id"], b["value_usd"]),
        )
    # Per-item detail as well, so the history tooltip can break a bucket down into the
    # holdings behind it. Names are copied rather than referenced, so the breakdown
    # still reads correctly after an item is renamed or deleted.
    for it in p["items"]:
        if it["value_usd"] is None:
            continue
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, item_id, name, kind, bucket_id,"
            " value_usd) VALUES (?,?,?,?,?,?)",
            (sid, it["id"], it["name"], it["kind"], it["bucket_id"], it["value_usd"]),
        )
    conn.commit()
    return sid
