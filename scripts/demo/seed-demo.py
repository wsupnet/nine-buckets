"""Fill the demo database with fictional purchase lots and two years of history.

Runs inside the demo app container, after the app has started with SEED_DEMO=1
and fetched prices (see README.md):

    docker exec -i networth-demo python - < scripts/demo/seed-demo.py

Every figure here is invented. The holdings are real tickers so live prices
work, but the quantities, dates and prices paid are made up.
"""
import math
from datetime import date

from app.db import connect, now_iso
from app.model import portfolio

conn = connect()
items = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM items")}
if not items:
    raise SystemExit("No items — start the app with SEED_DEMO=1 first.")
if conn.execute("SELECT COUNT(*) c FROM lots").fetchone()["c"]:
    raise SystemExit("Lots already present — reseed from an empty volume instead.")

# (holding, side, date, quantity, price, premium per unit, fee). Each holding's
# lots net to the quantity the demo seed gives it.
LOTS = [
    ("Bitcoin", "buy", "2023-11-14", 0.21, 41250.37, 0, 6.18),
    ("Bitcoin", "buy", "2024-06-03", 0.1572, 58912.40, 0, 4.63),
    ("Bitcoin", "sell", "2025-01-20", 0.05, 96410.00, 0, 3.86),
    ("Ethereum", "buy", "2023-10-02", 1.9, 1684.20, 0, 2.11),
    ("Ethereum", "buy", "2024-03-11", 0.946, 1906.55, 0, 1.47),
    ("Global index ETF", "buy", "2023-08-21", 150, 98.41, 0, 0),
    ("Global index ETF", "buy", "2024-02-05", 61.3, 104.77, 0, 0),
    ("Example large-cap", "buy", "2023-09-12", 37, 331.20, 0, 0),
    ("Silver rounds", "buy", "2023-12-04", 30, 23.47, 3.15, 0),
    ("Silver rounds", "buy", "2024-08-19", 17, 29.88, 2.90, 0),
]
for name, side, day, qty, price, premium, fee in LOTS:
    it = items[name]
    conn.execute(
        "INSERT INTO lots (item_id, side, trade_date, quantity, price, premium, unit, fee,"
        " currency, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (it["id"], side, day, qty, price, premium, it["unit"], fee, "USD", "demo", now_iso()),
    )
conn.commit()

# Keep every account's net positive in the demo: the car loan sits with the euro
# bank that issued it, at a balance that bank's cash covers.
bank_c = conn.execute("SELECT id FROM accounts WHERE name = 'Bank C'").fetchone()["id"]
conn.execute("UPDATE items SET account_id = ?, amount = 3472.18 WHERE name = 'Example car loan'",
             (bank_c,))
conn.execute("DELETE FROM accounts WHERE name = 'Lender G'"
             " AND id NOT IN (SELECT account_id FROM items WHERE account_id IS NOT NULL)")
conn.commit()

# Monthly history ending last month, shaped from today's bucket values so the
# chart's breakdown matches the current allocation.
p = portfolio(conn)
today = date.today()
months = 24
for k in range(months):
    m = today.month - (months - k)
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    taken = date(y, m, 28).isoformat() + "T12:00:00+00:00"
    progress = k / (months - 1)
    lines, total_assets = [], 0.0
    for b in p["buckets"]:
        wiggle = 1 + 0.035 * math.sin(k * 0.9 + b["id"])
        v = (b["value_usd"] or 0) * (0.71 + 0.25 * progress) * wiggle
        lines.append((b["id"], v))
        if not b["excluded"]:
            total_assets += v
    liab = p["liabilities"] * (1.45 - 0.4 * progress)
    sid = conn.execute(
        "INSERT INTO snapshots (taken_at, label, assets_usd, liabilities_usd, net_worth_usd)"
        " VALUES (?,?,?,?,?)",
        (taken, None, total_assets, liab, total_assets - liab),
    ).lastrowid
    scale = {bid: v / (next(b for b in p["buckets"] if b["id"] == bid)["value_usd"] or 1)
             for bid, v in lines}
    for bid, v in lines:
        conn.execute("INSERT INTO snapshot_lines (snapshot_id, bucket_id, value_usd)"
                     " VALUES (?,?,?)", (sid, bid, v))
    for it in p["items"]:
        if it["value_usd"] is None:
            continue
        f = scale.get(it["bucket_id"], 1.45 - 0.4 * progress)
        conn.execute(
            "INSERT INTO snapshot_items (snapshot_id, item_id, name, kind, bucket_id,"
            " value_usd) VALUES (?,?,?,?,?,?)",
            (sid, it["id"], it["name"], it["kind"], it["bucket_id"], it["value_usd"] * f),
        )
conn.commit()
print(f"demo seed: {len(LOTS)} lots, {months} monthly snapshots")
