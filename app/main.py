import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2.filters import do_round
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, importer, jobs, prices
from .db import (DB_PATH, check_writable, connect, db_session, init_db, now_iso,
                  seed_demo_data)
from .model import (
    ASSET_CLASSES,
    BUSINESS_SIDES,
    KINDS,
    TROY_OZ_PER_UNIT,
    UNIT_LABELS,
    adjust_amount,
    adjust_loan,
    adjust_quantity,
    by_value_desc,
    convert_amount,
    fifo_summary,
    from_troy_oz,
    movable_holdings,
    movable_items,
    normalise_lot,
    plan_holding_transfer,
    to_troy_oz,
    accounts_view,
    buckets_view,
    load_items,
    portfolio,
    snapshot_series,
    snapshot_series_monthly,
    take_snapshot,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("networth")


# Checked at import, before the session secret is generated below — otherwise a
# misconfigured deploy logs a confusing "could not persist session secret" warning
# first and the real cause second.
config.validate()
check_writable()
auth.ensure_password()

# Recorded before anything opens the database, which would create the file.
DB_EXISTED_AT_START = os.path.exists(DB_PATH)


def warn_if_data_lost():
    """Shout if we came up empty but the backups say we shouldn't have.

    An empty database and a working app look identical at startup, so a data
    volume that didn't survive an update announces itself as "all my stuff is
    gone" long after the fact. If there are backups with items in them and the
    live database has none, that is the moment to say so — while every backup is
    still on disk and nothing has overwritten them.
    """
    if config.SEED_DEMO:
        return
    try:
        conn = connect()
        try:
            live = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        finally:
            conn.close()
        if live:
            return
        best = max((b["item_count"] or 0 for b in jobs.list_backups()), default=0)
    except Exception:
        return
    if best:
        # Overrides the "new database" message: this is the more specific diagnosis.
        log.warning(
            "=" * 72 + "\n"
            "  The database is EMPTY, but a backup on disk holds %d item(s).\n"
            "  The /data volume probably did not survive the last update.\n"
            "  DO NOT re-enter your data. Go to History -> Backups & export and\n"
            "  restore the newest backup with a non-zero item count.\n"
            "  Automatic backups are paused while the database is empty, so the\n"
            "  good copies will not be overwritten.\n" + "=" * 72,
            best,
        )


def in_container():
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def report_data_location():
    """Say plainly, once at startup, which data the app is about to serve.

    A first install, a data folder that didn't get mounted and a mount pointing at
    the wrong host folder all start up as an empty database. Spell out which case
    this is, and where the file lives, so the wrong one is caught on day one.
    """
    data_dir = os.path.dirname(os.path.abspath(DB_PATH))
    # Set by docker-compose.yml to the host folder it mounts; empty outside compose.
    host_dir = os.environ.get("NETWORTH_DATA", "").strip()
    where = f"{DB_PATH} (host folder: {host_dir})" if host_dir else DB_PATH

    if in_container() and not os.path.ismount(data_dir):
        log.warning(
            "=" * 72 + "\n"
            "  %s is NOT a mounted folder. Your data is being written inside the\n"
            "  container and will be LOST when the container is removed or rebuilt.\n"
            "  Start it with `docker compose up -d` from the project folder, or mount\n"
            "  a host folder at %s (docker run -v /some/folder:%s).\n" + "=" * 72,
            data_dir, data_dir, data_dir,
        )
    if not DB_EXISTED_AT_START:
        log.warning(
            "No database found at %s, so a new, empty one was created.\n"
            "  This is normal on a first install. If you already had data, stop the\n"
            "  app now: it is not looking at the data folder you used before. Check\n"
            "  NETWORTH_DATA in .env points at the folder that holds your data.",
            where,
        )
        return
    try:
        conn = connect()
        try:
            items = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        finally:
            conn.close()
    except Exception:
        return
    log.info("Using the database at %s, %d item(s)", where, items)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    report_data_location()
    if config.SEED_DEMO:
        log.warning("SEED_DEMO is on — inserting demo data into an empty database")
        seed_demo_data()
    if not config.AUTH_ENABLED:
        log.warning("AUTH_ENABLED=0 — the app is serving with no login")
    warn_if_data_lost()

    tasks = jobs.start(app)
    try:
        yield
    finally:
        await jobs.stop(tasks)


# No interactive API docs: nothing uses them, and they load scripts from a CDN.
app = FastAPI(title="Nine Buckets", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# Order matters: the session must be decoded before the gate inspects it.
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie="networth_session",
    max_age=config.SESSION_DAYS * 86400,
    same_site="lax",
    https_only=config.SESSION_HTTPS_ONLY,
)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def money(v, places=0):
    # Sign before the symbol ("−$91", not "$-91"), matching how liabilities are
    # shown. A loss that rounds to nothing shows as "$0", not "−$0".
    if v is None:
        return "—"
    sign = "−" if round(v, places) < 0 else ""
    return f"{sign}${abs(v):,.{places}f}"


def pct(v):
    return "—" if v is None else f"{v:.1f}%"


def num(v):
    """Plain decimal for edit fields. '%g' flips to scientific notation around 1e6,
    which turns a ¥1,234,567 balance into '1.23457e+06'."""
    if v is None or v == "":
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v.is_integer():
        return str(int(v))
    return f"{v:.10f}".rstrip("0").rstrip(".")


def safe_round(v, precision=0, method="common"):
    """Null-safe replacement for Jinja's round.

    A holding with no quantity yet, or a price that failed to fetch, carries None
    all the way into the template. Jinja's built-in round raises TypeError on it and
    takes the whole page down with a 500 — so pass None through instead.
    """
    if v is None or v == "":
        return None
    try:
        return do_round(float(v), precision, method)
    except (TypeError, ValueError):
        return None


def show(v):
    """Rendered for any value that reaches output as None.

    Without this a null prints the literal string "None" in the page.
    """
    return "—" if v is None else v


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["num"] = num
templates.env.filters["round"] = safe_round
templates.env.finalize = show


def asset(path: str) -> str:
    """Static URL stamped with the file's mtime.

    Without this a browser keeps serving a cached style.css / app.js after an update,
    so a deployed change appears to do nothing until a hard refresh.
    """
    full = os.path.join(BASE, "static", path)
    try:
        stamp = int(os.path.getmtime(full))
    except OSError:
        stamp = 0
    return f"/static/{path}?v={stamp}"


templates.env.globals["asset"] = asset

BUCKET_COLORS = {
    "safety": "#4da3ff",
    "cash": "#63c7d6",
    "job_income": "#8d99a8",
    "business": "#9b7fe0",
    "real_estate": "#e08b4f",
    "precious_metals": "#e5c04a",
    "crypto": "#e5734a",
    "stocks": "#3fbf7f",
    "collectibles": "#d76fa8",
}


def render(request, name, **ctx):
    # Not a route, so it can't take the db_session dependency — but a template that
    # raises must not strand the connection either.
    conn = connect()
    try:
        p = portfolio(conn)
        currencies = [
            r["currency"]
            for r in conn.execute(
                "SELECT currency FROM fx_rates ORDER BY currency = 'USD' DESC, currency"
            )
        ]
        rates = {r["currency"]: dict(r) for r in conn.execute("SELECT * FROM fx_rates")}
        accounts = [
            dict(a) for a in conn.execute("SELECT * FROM accounts ORDER BY sort_order, name")
        ]
    finally:
        conn.close()
    return templates.TemplateResponse(
        name,
        {
            "request": request,
            "p": p,
            "kinds": KINDS,
            "asset_classes": ASSET_CLASSES,
            "currencies": currencies,
            "rates": rates,
            "accounts": accounts,
            "colors": BUCKET_COLORS,
            "metals": prices.METALS,
            "units": UNIT_LABELS,
            "auth_enabled": config.AUTH_ENABLED,
            "flash": request.query_params.get("msg"),
            "error": request.query_params.get("err"),
            **ctx,
        },
    )


@app.get("/healthz")
def healthz(conn=Depends(db_session)):
    """Used by the container healthcheck; must not require a session."""
    try:
        conn.execute("SELECT 1 FROM buckets LIMIT 1").fetchone()
    except Exception as exc:
        # Reachable without signing in, so the detail goes to the log only.
        log.error("healthcheck failed: %s", exc)
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@app.get("/login")
def login_form(request: Request, next: str = "/", error: str = ""):
    next = auth.safe_next(next)
    if not config.AUTH_ENABLED or request.session.get("auth"):
        return RedirectResponse(next, 303)
    # `error` is deliberately not rendered from the URL: only this app's own
    # messages belong on the sign-in page.
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": ""}
    )


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    # Only ever redirect within this app — an absolute URL here would be an open redirect.
    nxt = auth.safe_next(form.get("next"))

    ip = auth._client_ip(request)
    wait = auth.is_throttled(ip)
    if wait:
        return templates.TemplateResponse(
            request, "login.html",
            {"next": nxt, "error": f"Too many attempts. Try again in {wait} seconds."},
            status_code=429,
        )

    if auth.check_password(form.get("password") or ""):
        auth.clear_failures(ip)
        request.session["auth"] = True
        request.session["since"] = now_iso()
        return RedirectResponse(nxt, 303)

    auth.record_failure(ip)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": nxt, "error": "Incorrect passphrase."},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/")
def dashboard(request: Request, conn=Depends(db_session)):
    series = snapshot_series_monthly(conn, BUCKET_COLORS)
    return render(request, "dashboard.html", nav="dashboard", series=series)


@app.get("/items")
def items_page(request: Request, conn=Depends(db_session)):
    movable = movable_items(conn)
    transfers = [
        dict(r) for r in conn.execute(
            "SELECT t.*, f.name AS from_name, f.currency AS from_ccy,"
            " d.name AS to_name, d.currency AS to_ccy"
            " FROM transfers t"
            " LEFT JOIN items f ON f.id = t.from_item_id"
            " LEFT JOIN items d ON d.id = t.to_item_id"
            # Paged in the browser, so the cap is only a guard against an
            # unbounded page rather than the thing that decides what you see.
            " ORDER BY t.moved_on DESC, t.id DESC LIMIT 500"
        )
    ]
    holdings = movable_holdings(conn)
    holding_moves = [
        dict(r) for r in conn.execute(
            "SELECT h.*, f.name AS from_name, d.name AS to_name,"
            " fa.name AS from_account, da.name AS to_account"
            " FROM holding_transfers h"
            " LEFT JOIN items f ON f.id = h.from_item_id"
            " LEFT JOIN items d ON d.id = h.to_item_id"
            " LEFT JOIN accounts fa ON fa.id = f.account_id"
            " LEFT JOIN accounts da ON da.id = d.account_id"
            " ORDER BY h.moved_on DESC, h.id DESC LIMIT 500"
        )
    ]
    return render(request, "items.html", nav="items",
                  movable=movable, transfers=transfers,
                  holdings=holdings, holding_moves=holding_moves)


@app.get("/allocation")
def allocation_page(request: Request, conn=Depends(db_session)):
    bv = buckets_view(conn)
    liabilities = [i for i in load_items(conn) if i["kind"] == "liability"]
    return render(request, "allocation.html", nav="allocation", bv=bv,
                  liabilities=by_value_desc(liabilities))


@app.get("/buckets")
def buckets_redirect(request: Request):
    """Buckets and Allocation merged into one page; keep old links working."""
    frag = request.url.fragment
    return RedirectResponse("/allocation" + (f"#{frag}" if frag else ""), 301)


@app.get("/holdings")
def holdings_page(request: Request, conn=Depends(db_session)):
    items = [i for i in load_items(conn) if i["kind"] == "priced"]
    rates = {r["currency"]: r["usd_per_unit"] for r in conn.execute("SELECT * FROM fx_rates")}
    rates["USD"] = 1.0

    lots = {}
    transactions = []
    rows = conn.execute(
        "SELECT l.*, i.name AS item_name, i.ticker, a.name AS account_name"
        " FROM lots l"
        " JOIN items i ON i.id = l.item_id"
        " LEFT JOIN accounts a ON a.id = i.account_id"
        " ORDER BY l.trade_date DESC, l.id DESC"
    ).fetchall()
    for r in rows:
        d = dict(r)
        lots.setdefault(r["item_id"], []).append(d)
        gross = d["quantity"] * d["price"]
        # A buy costs the fee on top; a sell nets it out of the proceeds.
        d["net_value"] = gross + d["fee"] if d["side"] == "buy" else gross - d["fee"]
        d["net_value_usd"] = d["net_value"] * rates.get(d["currency"], 1.0)
        transactions.append(d)
    cash_sources = [m for m in movable_items(conn) if m["kind"] in ("cash", "manual")]

    tx_totals = {
        "buys": sum(t["net_value_usd"] for t in transactions if t["side"] == "buy"),
        "sells": sum(t["net_value_usd"] for t in transactions if t["side"] == "sell"),
        "buy_count": sum(1 for t in transactions if t["side"] == "buy"),
        "sell_count": sum(1 for t in transactions if t["side"] == "sell"),
        # Transfers move basis between holdings without buying or selling anything,
        # so they are counted apart rather than folded into either total.
        "move_count": sum(1 for t in transactions if t["side"].startswith("transfer")),
    }

    # Positions bought and sold to nothing are kept out of the main tables — they
    # aren't holdings any more — but stay reachable, because their lots still hold
    # the realized gain and the record of what was traded.
    live = [i for i in items if not i["is_closed"]]
    securities = by_value_desc([i for i in live if not i["is_metal"]])
    metals = by_value_desc([i for i in live if i["is_metal"]])
    closed = by_value_desc([i for i in items if i["is_closed"]])
    # Summed here, not with Jinja's sum(): a holding zeroed by hand with no lots is
    # closed but has no realized figure at all (None), which crashed the page.
    closed_realized = sum(i["realized_usd"] or 0 for i in closed)

    totals = {
        "value": sum(i["value_usd"] or 0 for i in items),
        "cost": sum(i["cost_basis_usd"] or 0 for i in items if i["has_lots"]),
        "unrealized": sum(i["unrealized_usd"] or 0 for i in items if i["has_lots"]),
        "realized": sum(i["realized_usd"] or 0 for i in items if i["has_lots"]),
    }
    metal_totals = {
        "value": sum(i["value_usd"] or 0 for i in metals),
        "cost": sum(i["cost_basis_usd"] or 0 for i in metals if i["has_lots"]),
        "premium": sum(i["premium_paid_usd"] or 0 for i in metals if i["has_lots"]),
        "toz": sum(i["qty_toz"] or 0 for i in metals),
    }
    # Keyed on ticker *and* account. The same asset in two different accounts is a
    # legitimate split and is deliberately not flagged — flagging it would fire on
    # every asset held in more than one place, and a warning you learn to ignore has
    # stopped working.
    # Twice in one account is the real fault: a hand-typed row and an imported one,
    # with net worth counting the asset twice.
    seen = {}
    for i in live:
        seen.setdefault(((i["ticker"] or "").upper(), i["account_id"]), []).append(i)
    duplicates = [
        {"ticker": key[0], "account": group[0]["account_name"],
         "items": by_value_desc(group)}
        for key, group in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))
        if key[0] and len(group) > 1
    ]

    return render(request, "holdings.html", nav="holdings",
                  holdings=items, securities=securities, metals_held=metals,
                  closed=closed, closed_realized=closed_realized, duplicates=duplicates,
                  lots=lots, totals=totals, metal_totals=metal_totals,
                  transactions=transactions, tx_totals=tx_totals,
                  cash_items=cash_sources)


@app.post("/items/{item_id}/lots")
def create_lot(
    item_id: int,
    side: str = Form("buy"),
    trade_date: str = Form(...),
    quantity: str = Form(...),
    price: str = Form(...),
    premium: str = Form(""),
    unit: str = Form(""),
    fee: str = Form(""),
    note: str = Form(""),
    funded_from: str = Form(""),
    conn=Depends(db_session),
):
    iso = _date_to_iso(trade_date)
    if iso is None:
        return RedirectResponse("/holdings?err=Use+a+YYYY-MM-DD+date", 303)
    try:
        nums = _floats(quantity=quantity, price=price, premium=premium, fee=fee)
    except ValueError as exc:
        return RedirectResponse("/holdings?err=" + quote_plus(str(exc)), 303)

    qty, px = nums["quantity"], nums["price"]
    prem, fe = nums["premium"] or 0.0, nums["fee"] or 0.0
    if not qty or qty <= 0:
        return RedirectResponse("/holdings?err=Quantity+must+be+greater+than+zero", 303)
    if px is None or px < 0:
        return RedirectResponse("/holdings?err=Price+is+required", 303)

    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return RedirectResponse("/holdings?err=Holding+not+found", 303)

    metal = item["kind"] == "priced" and item["asset_class"] == "metal"
    if metal:
        lot_unit = (unit or item["unit"] or "toz").strip()
        if lot_unit not in TROY_OZ_PER_UNIT:
            return RedirectResponse("/holdings?err=Unknown+weight+unit", 303)
    else:
        lot_unit = None
        prem = 0.0

    is_buy = side != "sell"
    lot_ccy = item["price_currency"] or item["currency"] or "USD"

    # "Paid from" must be chosen explicitly on every lot — there is no safe default,
    # because a backdated purchase should not move today's cash balance.
    funding = (funded_from or "").strip()
    if funding == "":
        return RedirectResponse(
            "/holdings?err=" + quote_plus("Choose where the money came from (or 'external')"),
            303,
        )
    funding_id = None
    warning = ""
    if funding != "external":
        try:
            funding_id = int(funding)
        except ValueError:
            return RedirectResponse("/holdings?err=Unknown+funding+account", 303)
        src = conn.execute(
            "SELECT * FROM items WHERE id=? AND kind IN ('cash','manual')", (funding_id,)
        ).fetchone()
        if not src or _sheet_valued(conn, funding_id):
            return RedirectResponse("/holdings?err=Unknown+funding+account", 303)

        # A buy takes money out; a sell puts the proceeds back in.
        gross = qty * (px + prem)
        cash_delta = -(gross + fe) if is_buy else (gross - fe)
        # Rounded to cents, as Move money is; delete_lot rounds its refund the same way.
        in_src_ccy = round(convert_amount(conn, cash_delta, lot_ccy, src["currency"]), 2)
        new_balance, negative = adjust_amount(conn, funding_id, in_src_ccy)
        if negative:
            warning = f" — {src['name']} is now negative ({new_balance:,.2f})"

    conn.execute(
        "INSERT INTO lots (item_id, side, trade_date, quantity, price, premium, unit,"
        " fee, currency, note, funding_item_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, "buy" if is_buy else "sell", trade_date.strip(), qty, px,
         prem, lot_unit, fe, lot_ccy, note.strip() or None, funding_id, now_iso()),
    )
    conn.commit()
    msg = "Lot added" + (" and cash moved" if funding_id else "") + warning
    url = "/holdings?msg=" + quote_plus(msg)

    # Selling more than the ledger holds is allowed but must not pass quietly: the
    # unmatched units have no basis, so the realized gain is understated until the
    # missing purchase is recorded.
    if not is_buy:
        summary = fifo_summary([
            normalise_lot(r, metal) for r in conn.execute(
                "SELECT * FROM lots WHERE item_id=? ORDER BY trade_date, id", (item_id,)
            )
        ])
        if summary["oversold"] > _QTY_EPS:
            over = from_troy_oz(summary["oversold"], lot_unit) if metal else summary["oversold"]
            url += "&err=" + quote_plus(
                f"{item['name']} has now sold {num(safe_round(over, 8))}"
                f"{(' ' + lot_unit) if metal else ''} more than its ledger holds. "
                f"Record the missing purchase so those units get a real cost basis"
            )
    return RedirectResponse(url, 303)


@app.post("/lots/{lot_id}/delete")
def delete_lot(lot_id: int, conn=Depends(db_session)):
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    if not lot:
        return RedirectResponse("/holdings?err=Lot+not+found", 303)
    if lot["transfer_id"]:
        # Deleting one half of a move would leave the units duplicated or vanished.
        return RedirectResponse(
            "/holdings?err=" + quote_plus(
                "This lot is one half of a holding move. Reverse the move itself "
                "under Recent activity on the Assets & Liabilities page, and both "
                "sides come back together"
            ), 303,
        )

    moved = ""
    if lot["funding_item_id"]:
        src = conn.execute(
            "SELECT * FROM items WHERE id=?", (lot["funding_item_id"],)
        ).fetchone()
        if src:
            # Put the money back exactly as it was taken.
            gross = (lot["quantity"] or 0) * ((lot["price"] or 0) + (lot["premium"] or 0))
            fee = lot["fee"] or 0
            original = -(gross + fee) if lot["side"] == "buy" else (gross - fee)
            back = round(convert_amount(conn, -original, lot["currency"], src["currency"]), 2)
            adjust_amount(conn, src["id"], back)
            moved = f" and {src['name']} restored"

    conn.execute("DELETE FROM lots WHERE id=?", (lot_id,))
    conn.commit()
    return RedirectResponse("/holdings?msg=" + quote_plus("Lot deleted" + moved), 303)


@app.post("/transfers")
def create_transfer(
    from_item: str = Form(...),
    to_item: str = Form(...),
    amount: str = Form(...),
    moved_on: str = Form(""),
    note: str = Form(""),
    conn=Depends(db_session),
):
    """Move value between two manually-valued items. Net worth is unchanged by a
    transfer between assets; paying a liability reduces both sides."""
    try:
        value = _float(amount)
    except ValueError as exc:
        return RedirectResponse("/items?err=" + quote_plus(str(exc)), 303)
    if not value or value <= 0:
        return RedirectResponse("/items?err=Enter+an+amount+greater+than+zero", 303)

    try:
        src_id, dst_id = int(from_item), int(to_item)
    except ValueError:
        return RedirectResponse("/items?err=Choose+both+sides+of+the+transfer", 303)
    if src_id == dst_id:
        return RedirectResponse("/items?err=Pick+two+different+items", 303)

    movable_kinds = "('cash','manual','liability','property')"
    src = conn.execute(
        f"SELECT * FROM items WHERE id=? AND kind IN {movable_kinds}", (src_id,)
    ).fetchone()
    dst = conn.execute(
        f"SELECT * FROM items WHERE id=? AND kind IN {movable_kinds}", (dst_id,)
    ).fetchone()
    if not src or not dst:
        return RedirectResponse("/items?err=Transfers+need+two+manually-valued+items", 303)
    for side in (src, dst):
        if _sheet_valued(conn, side["id"]):
            return RedirectResponse("/items?err=" + quote_plus(
                f"{side['name']} is valued from its business balance sheet, so money "
                f"can't be moved in or out of it here. Change the sheet's lines instead"
            ), 303)

    # An asset loses value when money leaves it; a liability grows when you draw on it.
    from_delta = value if src["kind"] == "liability" else -value
    # Rounded to cents: a converted amount otherwise lands in the balance (and the
    # transfer record) as 3224.7569273115.
    to_value = round(convert_amount(conn, value, src["currency"], dst["currency"]), 2)
    # Money arriving at an asset increases it; arriving at a liability pays it down.
    to_delta = -to_value if dst["kind"] == "liability" else to_value

    # A property's market value never moves here — only its mortgage does.
    # adjust_loan(payment) reduces the balance by `payment`, so:
    #   money INTO a property  = paying the mortgage down -> positive payment
    #   money OUT of a property = borrowing against it     -> negative payment
    if src["kind"] == "property":
        adjust_loan(conn, src_id, -value)
        src_negative = False
    else:
        _, src_negative = adjust_amount(conn, src_id, from_delta)
    if dst["kind"] == "property":
        adjust_loan(conn, dst_id, to_value)
    else:
        adjust_amount(conn, dst_id, to_delta)

    conn.execute(
        "INSERT INTO transfers (from_item_id, to_item_id, from_amount, to_amount,"
        " moved_on, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (src_id, dst_id, value, to_value,
         (_date_to_iso(moved_on) or now_iso())[:10], note.strip() or None, now_iso()),
    )
    conn.commit()

    msg = f"Moved {value:,.2f} {src['currency']} from {src['name']} to {dst['name']}"
    if src_negative:
        msg += f" — {src['name']} is now negative"
    return RedirectResponse("/items?msg=" + quote_plus(msg), 303)


@app.post("/transfers/{transfer_id}/delete")
def delete_transfer(transfer_id: int, conn=Depends(db_session)):
    t = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
    if not t:
        return RedirectResponse("/items?err=Transfer+not+found", 303)

    for item_id, amount, sign in (
        (t["from_item_id"], t["from_amount"], 1),
        (t["to_item_id"], t["to_amount"], -1),
    ):
        row = conn.execute("SELECT kind FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            continue
        delta = sign * amount
        if row["kind"] == "property":
            # Undo the loan movement. The original applied -value on the source side
            # and +to_value on the destination side; `delta` already carries that
            # sign convention, so applying it directly reverses it.
            adjust_loan(conn, item_id, delta)
            continue
        if row["kind"] == "liability":
            delta = -delta
        adjust_amount(conn, item_id, delta)

    conn.execute("DELETE FROM transfers WHERE id=?", (transfer_id,))
    conn.commit()
    return RedirectResponse("/items?msg=Transfer+reversed", 303)


def _sheet_valued(conn, item_id):
    """True when the item's value comes from a linked business balance sheet."""
    return conn.execute(
        "SELECT 1 FROM business_sheets WHERE item_id=? AND linked=1", (item_id,)
    ).fetchone() is not None


# Quantities are compared with a tolerance so a value that round-tripped through a
# unit conversion isn't rejected for being a billionth short.
_QTY_EPS = 1e-9


def _items_err(conn, message):
    if conn is not None:
        conn.close()
    return RedirectResponse("/items?err=" + quote_plus(message), 303)


def _find_holding(holdings, raw):
    try:
        wanted = int(raw)
    except (TypeError, ValueError):
        return None
    return next((h for h in holdings if h["id"] == wanted), None)


def _in_item_unit(qty, typed_unit, item_unit, metal):
    """Restate a typed quantity in the unit the holding itself is tracked in.

    Only metals have units; everything else is already in its own terms.
    """
    if not metal or qty is None:
        return qty
    return from_troy_oz(to_troy_oz(qty, typed_unit), item_unit)


@app.post("/holding-transfers")
def create_holding_transfer(
    from_item: str = Form(...),
    to_item: str = Form(...),
    new_name: str = Form(""),
    new_account_id: str = Form(""),
    new_account: str = Form(""),
    quantity: str = Form(...),
    received: str = Form(""),
    unit: str = Form(""),
    moved_on: str = Form(""),
    note: str = Form(""),
    conn=Depends(db_session),
):
    """Move units of a priced holding between two locations.

    Neither a buy nor a sell: the units and their cost basis both change hands, so
    nothing is realized and net worth only falls by whatever the network fee ate.
    Basis travels as a `transfer_out` on the source and one `transfer_in` per
    matched source lot on the destination, each keeping its original trade date so
    FIFO order survives the move.
    """
    try:
        nums = _floats(quantity=quantity, received=received)
    except ValueError as exc:
        return _items_err(None, str(exc))

    qty_sent = nums["quantity"]
    if not qty_sent or qty_sent <= 0:
        return _items_err(None, "Enter a quantity greater than zero")
    qty_recv = nums["received"]
    if qty_recv is None:
        qty_recv = qty_sent
    if qty_recv <= 0:
        return _items_err(None, "The quantity that arrived must be greater than zero")
    if qty_recv > qty_sent + _QTY_EPS:
        return _items_err(None, "More can't arrive than was sent — check both quantities")

    if moved_on.strip() and _date_to_iso(moved_on) is None:
        return _items_err(None, "Use a YYYY-MM-DD date")
    moved_day = moved_on.strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    holdings = movable_holdings(conn)
    src = _find_holding(holdings, from_item)
    if not src:
        return _items_err(conn, "Choose the holding the units are leaving")

    # Destination: an existing holding of the same thing, or a new one that inherits
    # the source's ticker, asset class, bucket and unit so the two stay comparable.
    dest_new = to_item.strip() == "new"
    if dest_new:
        account_id, bad = resolve_account(conn, new_account_id, new_account)
        if bad:
            return _items_err(conn, bad)
        if account_id is None:
            return _items_err(conn, "Pick or name an account for the new holding")
        conn.execute(
            "INSERT INTO items (name, kind, bucket_id, account_id, currency, ticker,"
            " asset_class, unit, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (new_name.strip() or src["ticker"] or src["name"], "priced",
             src["bucket_id"], account_id, src["currency"], src["ticker"],
             src["asset_class"], src["unit"], now_iso()),
        )
        conn.commit()
        dst_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        dst = _find_holding(movable_holdings(conn), dst_id)
    else:
        dst = _find_holding(holdings, to_item)
    if not dst:
        return _items_err(conn, "Choose where the units are going")
    if dst["id"] == src["id"]:
        return _items_err(conn, "Pick two different holdings")
    if (src["ticker"] or "").upper() != (dst["ticker"] or "").upper():
        return _items_err(
            conn,
            f"{src['name']} is {src['ticker'] or 'untickered'} but {dst['name']} is "
            f"{dst['ticker'] or 'untickered'} — a transfer moves the same asset, "
            f"it doesn't convert one into another",
        )
    if src["asset_class"] != dst["asset_class"]:
        return _items_err(conn, "Both holdings must be the same asset class")

    # Metals are entered in whatever unit suits the bar or coin; everything is
    # compared and matched in troy ounces.
    if src["is_metal"]:
        typed_unit = (unit or src["unit"] or "toz").strip()
        if typed_unit not in TROY_OZ_PER_UNIT:
            return _items_err(conn, "Unknown weight unit")
    else:
        typed_unit = None
    sent_norm = to_troy_oz(qty_sent, typed_unit) if src["is_metal"] else qty_sent
    recv_norm = to_troy_oz(qty_recv, typed_unit) if src["is_metal"] else qty_recv
    label = f" {typed_unit}" if src["is_metal"] else f" {src['ticker'] or ''}"

    src_lots = [
        normalise_lot(r, src["is_metal"])
        for r in conn.execute(
            "SELECT * FROM lots WHERE item_id=? ORDER BY trade_date, id", (src["id"],)
        )
    ]

    slices, basis_moved, lot_ccy = [], None, None
    if src["has_lots"]:
        slices, basis_moved, shortfall = plan_holding_transfer(
            src_lots, sent_norm, recv_norm
        )
        if shortfall > _QTY_EPS:
            held = from_troy_oz(sent_norm - shortfall, typed_unit) \
                if src["is_metal"] else sent_norm - shortfall
            return _items_err(
                conn,
                f"{src['name']}'s ledger only holds {num(safe_round(held, 8))}"
                f"{label} — record the missing purchase before moving it, so the "
                f"units arrive with a real cost basis",
            )
        # Adding basis-bearing lots to a holding that is still on a typed quantity
        # would switch it to the ledger, and its typed number would silently drop
        # out of the total.
        if not dst["has_lots"] and (dst["typed_quantity"] or 0) > _QTY_EPS:
            return _items_err(
                conn,
                f"{dst['name']} has a typed quantity of "
                f"{num(dst['typed_quantity'])} and no lots. Moving basis-tracked "
                f"units in would switch it to its lot ledger and drop that number. "
                f"Record a lot for what's already there first, or move into a new "
                f"holding",
            )
        lot_ccy = src["price_currency"] or src["currency"] or "USD"
    else:
        available = to_troy_oz(src["typed_quantity"] or 0, src["unit"]) \
            if src["is_metal"] else (src["typed_quantity"] or 0)
        if sent_norm > available + _QTY_EPS:
            return _items_err(
                conn,
                f"{src['name']} holds {num(src['typed_quantity'])}"
                f"{(' ' + src['unit']) if src['is_metal'] else ''}, which is less "
                f"than you're moving",
            )
        if dst["has_lots"]:
            return _items_err(
                conn,
                f"{dst['name']} tracks its quantity from a lot ledger, but "
                f"{src['name']} has no lots, so there's no cost basis to move with "
                f"the units. Record a purchase lot on {src['name']} first",
            )

    cur = conn.execute(
        "INSERT INTO holding_transfers (from_item_id, to_item_id, qty_sent,"
        " qty_received, unit, basis_moved, currency, moved_on, note, created_at,"
        " to_item_created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (src["id"], dst["id"], qty_sent, qty_recv, typed_unit, basis_moved,
         lot_ccy, moved_day, note.strip() or None, now_iso(), 1 if dest_new else 0),
    )
    tid = cur.lastrowid

    if src["has_lots"]:
        # Price the outgoing lot at what those units cost, so the ledger row reads as
        # basis leaving rather than as a disposal at today's market price.
        factor = TROY_OZ_PER_UNIT.get(typed_unit, 1.0) if src["is_metal"] else 1.0
        out_price = (basis_moved / sent_norm * factor) if sent_norm > _QTY_EPS else 0.0
        conn.execute(
            "INSERT INTO lots (item_id, side, trade_date, quantity, price, premium,"
            " unit, fee, currency, note, transfer_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (src["id"], "transfer_out", moved_day, qty_sent, out_price, 0.0,
             typed_unit, 0.0, lot_ccy, f"Moved to {dst['name']}", tid, now_iso()),
        )

        dst_ccy = dst["price_currency"] or dst["currency"] or "USD"
        for s in slices:
            price, premium = s["price"], s["premium"]
            from_ccy = s["currency"] or lot_ccy
            if from_ccy != dst_ccy:
                # Today's rate, not the rate on the original trade date — the same
                # limitation the metal cost basis already carries.
                price = convert_amount(conn, price, from_ccy, dst_ccy)
                premium = convert_amount(conn, premium, from_ccy, dst_ccy)
            conn.execute(
                "INSERT INTO lots (item_id, side, trade_date, quantity, price,"
                " premium, unit, fee, currency, note, transfer_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dst["id"], "transfer_in", s["trade_date"] or moved_day,
                 s["quantity"], price, premium,
                 "toz" if dst["is_metal"] else None, 0.0, dst_ccy,
                 f"Moved from {src['name']}", tid, now_iso()),
            )
    else:
        adjust_quantity(
            conn, src["id"],
            -_in_item_unit(qty_sent, typed_unit, src["unit"], src["is_metal"]),
        )
        adjust_quantity(
            conn, dst["id"],
            _in_item_unit(qty_recv, typed_unit, dst["unit"], dst["is_metal"]),
        )

    conn.commit()

    # Priced only after the connection is closed — fetching under an open write
    # transaction deadlocks against the coin-id cache. See _price_items.
    if dest_new and dst["ticker"]:
        _price_items([{"id": dst["id"], "ticker": dst["ticker"],
                       "asset_class": dst["asset_class"]}])

    msg = f"Moved {num(qty_sent)}{label} from {src['name']} to {dst['name']}"
    if qty_recv < qty_sent - _QTY_EPS:
        msg += (f" — {num(safe_round(qty_sent - qty_recv, 8))}{label} went to the "
                f"transfer fee, absorbed into the remaining cost basis")
    if basis_moved is not None:
        msg += f"; {basis_moved:,.2f} {lot_ccy} of cost basis moved with it"
    else:
        msg += f". {src['name']} has no lots, so no cost basis travelled"
    return RedirectResponse("/items?msg=" + quote_plus(msg), 303)


@app.post("/holding-transfers/{transfer_id}/delete")
def delete_holding_transfer(transfer_id: int, conn=Depends(db_session)):
    t = conn.execute(
        "SELECT * FROM holding_transfers WHERE id=?", (transfer_id,)
    ).fetchone()
    if not t:
        return _items_err(conn, "Move not found")

    paired = conn.execute(
        "SELECT COUNT(*) c FROM lots WHERE transfer_id=?", (transfer_id,)
    ).fetchone()["c"]
    if paired:
        # Dropping both halves restores the source queue and removes the units that
        # arrived, in one step.
        conn.execute("DELETE FROM lots WHERE transfer_id=?", (transfer_id,))
    else:
        for item_id, qty in ((t["from_item_id"], t["qty_sent"]),
                             (t["to_item_id"], -t["qty_received"])):
            if not item_id:
                continue
            row = conn.execute(
                "SELECT unit, asset_class FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                continue
            metal = row["asset_class"] == "metal"
            adjust_quantity(
                conn, item_id, _in_item_unit(qty, t["unit"], row["unit"], metal)
            )

    conn.execute("DELETE FROM holding_transfers WHERE id=?", (transfer_id,))

    # A holding this move created is removed with it, but only while nothing else
    # has happened there — as import rollback does. One that has since been traded,
    # moved again or given a quantity by hand stays.
    msg = "Holding move reversed"
    dst_id = t["to_item_id"]
    if t["to_item_created"] and dst_id:
        lots_left = conn.execute(
            "SELECT COUNT(*) c FROM lots WHERE item_id=?", (dst_id,)
        ).fetchone()["c"]
        moved = conn.execute(
            "SELECT COUNT(*) c FROM holding_transfers"
            " WHERE from_item_id=? OR to_item_id=?", (dst_id, dst_id)
        ).fetchone()["c"]
        dst = conn.execute(
            "SELECT name, quantity FROM items WHERE id=?", (dst_id,)
        ).fetchone()
        if dst and not lots_left and not moved and abs(dst["quantity"] or 0) <= _QTY_EPS:
            conn.execute("DELETE FROM items WHERE id=?", (dst_id,))
            msg += f" and {dst['name']}, which it created, removed"
    conn.commit()
    return RedirectResponse("/items?msg=" + quote_plus(msg), 303)


# ------------------------------------------------------------------------
# Business balance sheets
# ------------------------------------------------------------------------

def _biz_err(message):
    return RedirectResponse("/business?err=" + quote_plus(message), 303)


def _pct(raw):
    """Ownership as 0-100. Blank means the whole business."""
    value = _float(raw)
    if value is None:
        return 100.0
    if not 0 <= value <= 100:
        raise ValueError("Ownership must be between 0 and 100%")
    return value


def _freeze_value(conn, sheet):
    """Before a linked sheet stops feeding the main sheet, write its current value
    into the item as the typed amount, so net worth doesn't jump at the switch."""
    item = next((i for i in load_items(conn) if i["id"] == sheet["item_id"]), None)
    if item and item["sheet_linked"] and item["native_value"] is not None:
        conn.execute("UPDATE items SET amount=?, updated_at=? WHERE id=?",
                     (round(item["native_value"], 2), now_iso(), item["id"]))


@app.get("/business")
def business_page(request: Request, conn=Depends(db_session)):
    items = load_items(conn)
    business_bucket = conn.execute(
        "SELECT id FROM buckets WHERE key='business'").fetchone()
    bucket_id = business_bucket["id"] if business_bucket else None
    # Offered for a new sheet: manual-value items without one, Business bucket first.
    candidates = sorted(
        (i for i in items if i["kind"] == "manual" and not i["sheet"]),
        key=lambda i: (i["bucket_id"] != bucket_id, i["name"].lower()),
    )
    return render(request, "business.html", nav="business",
                  bv=business_view(items), candidates=candidates)


@app.post("/business")
def create_business_sheet(
    item_id: str = Form(""),
    new_name: str = Form(""),
    currency: str = Form("USD"),
    ownership_pct: str = Form(""),
    linked: str = Form(""),
    conn=Depends(db_session),
):
    try:
        pct_owned = _pct(ownership_pct)
    except ValueError as exc:
        return _biz_err(str(exc))

    name = new_name.strip()
    if item_id.strip() == "new":
        # A business not on the main sheet yet: record it there too, in the
        # Business bucket, so the sheet has a row to belong to.
        if not name:
            return _biz_err("Give the new business a name")
        bad = validate_item(conn, name, "manual", currency, "")
        if bad:
            return _biz_err(bad)
        bucket = conn.execute("SELECT id FROM buckets WHERE key='business'").fetchone()
        iid = conn.execute(
            "INSERT INTO items (name, kind, bucket_id, currency, updated_at)"
            " VALUES (?,?,?,?,?)",
            (name, "manual", bucket["id"] if bucket else None, currency, now_iso()),
        ).lastrowid
    else:
        try:
            iid = int(item_id)
        except ValueError:
            return _biz_err("Choose the business this sheet is for")
        row = conn.execute("SELECT kind FROM items WHERE id=?", (iid,)).fetchone()
        if not row or row["kind"] != "manual":
            return _biz_err("A balance sheet can only be added to a Manual value item")
        if conn.execute("SELECT 1 FROM business_sheets WHERE item_id=?", (iid,)).fetchone():
            return _biz_err("That business already has a balance sheet")

    conn.execute(
        "INSERT INTO business_sheets (item_id, ownership_pct, linked, created_at)"
        " VALUES (?,?,?,?)", (iid, pct_owned, 1 if linked else 0, now_iso()))
    conn.commit()
    return RedirectResponse("/business?msg=" + quote_plus("Balance sheet added"), 303)


@app.post("/business/{sheet_id}")
def update_business_sheet(
    sheet_id: int,
    ownership_pct: str = Form(""),
    linked: str = Form(""),
    conn=Depends(db_session),
):
    sheet = conn.execute("SELECT * FROM business_sheets WHERE id=?", (sheet_id,)).fetchone()
    if not sheet:
        return _biz_err("Balance sheet not found")
    try:
        pct_owned = _pct(ownership_pct)
    except ValueError as exc:
        return _biz_err(str(exc))

    now_linked = 1 if linked else 0
    if sheet["linked"] and not now_linked:
        _freeze_value(conn, sheet)
    conn.execute("UPDATE business_sheets SET ownership_pct=?, linked=? WHERE id=?",
                 (pct_owned, now_linked, sheet_id))
    conn.commit()
    msg = "Saved"
    if now_linked and not sheet["linked"]:
        msg = "Saved — the main sheet now takes this business's value from here"
    elif sheet["linked"] and not now_linked:
        msg = ("Saved — the sheet is reference only again; the main sheet keeps "
               "the last value as a typed amount")
    return RedirectResponse("/business?msg=" + quote_plus(msg), 303)


@app.post("/business/{sheet_id}/delete")
def delete_business_sheet(sheet_id: int, conn=Depends(db_session)):
    sheet = conn.execute("SELECT * FROM business_sheets WHERE id=?", (sheet_id,)).fetchone()
    if not sheet:
        return _biz_err("Balance sheet not found")
    _freeze_value(conn, sheet)
    conn.execute("DELETE FROM business_sheets WHERE id=?", (sheet_id,))
    conn.commit()
    return RedirectResponse("/business?msg=" + quote_plus(
        "Balance sheet deleted — the business itself stays on the main sheet"), 303)


def _line_fields(conn, side, name, currency):
    """(error) for a balance sheet line's descriptive fields."""
    if side not in BUSINESS_SIDES:
        return "Choose whether the line is an asset or a liability"
    if not (name or "").strip():
        return "Give the line a name"
    known = {r["currency"] for r in conn.execute("SELECT currency FROM fx_rates")}
    if currency not in known:
        return (f"There is no exchange rate for '{currency}'. "
                f"Known currencies: {', '.join(sorted(known))}")
    return None


@app.post("/business/{sheet_id}/lines")
def create_business_line(
    sheet_id: int,
    side: str = Form("asset"),
    name: str = Form(""),
    amount: str = Form(""),
    currency: str = Form("USD"),
    conn=Depends(db_session),
):
    if not conn.execute("SELECT 1 FROM business_sheets WHERE id=?", (sheet_id,)).fetchone():
        return _biz_err("Balance sheet not found")
    bad = _line_fields(conn, side, name, currency)
    if bad:
        return _biz_err(bad)
    try:
        value = _float(amount)
    except ValueError as exc:
        return _biz_err(str(exc))
    conn.execute(
        "INSERT INTO business_lines (sheet_id, side, name, amount, currency, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (sheet_id, side, name.strip(), value, currency, now_iso()))
    conn.commit()
    return RedirectResponse(
        f"/business?msg={quote_plus('Added ' + name.strip())}#sheet-{sheet_id}", 303)


@app.post("/business/lines/{line_id}")
def update_business_line(
    line_id: int,
    side: str = Form("asset"),
    name: str = Form(""),
    currency: str = Form("USD"),
    conn=Depends(db_session),
):
    line = conn.execute("SELECT * FROM business_lines WHERE id=?", (line_id,)).fetchone()
    if not line:
        return _biz_err("Line not found")
    bad = _line_fields(conn, side, name, currency)
    if bad:
        return _biz_err(bad)
    conn.execute(
        "UPDATE business_lines SET side=?, name=?, currency=?, updated_at=? WHERE id=?",
        (side, name.strip(), currency, now_iso(), line_id))
    conn.commit()
    return RedirectResponse(
        f"/business?msg={quote_plus('Saved ' + name.strip())}#sheet-{line['sheet_id']}", 303)


@app.post("/business/lines/{line_id}/inline")
async def inline_business_line(line_id: int, request: Request, conn=Depends(db_session)):
    form = await request.form()
    if (form.get("field") or "") != "amount":
        return JSONResponse({"ok": False, "error": "Only the amount is editable here"}, 400)
    value = (form.get("value") or "").strip()
    try:
        parsed = _float(value)
    except ValueError:
        return JSONResponse({"ok": False, "error": f"'{value}' is not a number"}, 400)
    line = conn.execute("SELECT * FROM business_lines WHERE id=?", (line_id,)).fetchone()
    if not line:
        return JSONResponse({"ok": False, "error": "Line not found"}, 404)
    conn.execute("UPDATE business_lines SET amount=?, updated_at=? WHERE id=?",
                 (parsed, now_iso(), line_id))
    conn.commit()
    return JSONResponse({"ok": True, "prev": num(line["amount"]),
                         "patch": patch_payload(conn)})


@app.post("/business/lines/{line_id}/delete")
def delete_business_line(line_id: int, conn=Depends(db_session)):
    line = conn.execute("SELECT * FROM business_lines WHERE id=?", (line_id,)).fetchone()
    if not line:
        return _biz_err("Line not found")
    conn.execute("DELETE FROM business_lines WHERE id=?", (line_id,))
    conn.commit()
    return RedirectResponse(
        f"/business?msg={quote_plus('Deleted ' + line['name'])}#sheet-{line['sheet_id']}", 303)


@app.get("/accounts")
def accounts_page(request: Request, conn=Depends(db_session)):
    av = accounts_view(conn)
    return render(request, "accounts.html", nav="accounts", av=av)


@app.get("/history")
def history_page(request: Request, conn=Depends(db_session)):
    snaps = [
        dict(r)
        for r in conn.execute("SELECT * FROM snapshots ORDER BY taken_at DESC, id DESC")
    ]
    series = snapshot_series_monthly(conn, BUCKET_COLORS)

    # A previously uploaded file waiting for confirmation.
    staged = None
    staged_path = jobs.resolve_backup(f"{jobs.BACKUP_PREFIX}{jobs.UPLOAD_NAME}.db")
    if staged_path:
        try:
            staged = jobs.inspect_db(staged_path)
            staged["name"] = f"{jobs.BACKUP_PREFIX}{jobs.UPLOAD_NAME}.db"
        except Exception:
            staged = None

    for s in snaps:
        s["date_only"] = (s["taken_at"] or "")[:10]
    return render(request, "history.html", nav="history", snaps=snaps, series=series,
                  backups=jobs.list_backups(), backup_dir=jobs.backup_dir(),
                  staged=staged,
                  backup_retention=config.BACKUP_RETENTION_DAYS,
                  backup_hours=config.BACKUP_INTERVAL_HOURS)


# Symbols people paste in alongside a number. Stripped before parsing.
_CURRENCY_CHARS = "$¥€£₹₩₪฿₫₱₽¤"
_SPACE_CHARS = " \t   '"


def _float(v):
    """Parse a number the way a person types it.

    '$1,234.56' -> 1234.56, '¥1 234 567' -> 1234567.0, '(1,200)' -> -1200.0
    (accounting notation for a negative), '25%' -> 25.0.

    Returns None for blank input; raises ValueError for anything that isn't a
    number once the decoration is removed.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Something was typed but it contains no digits ("$$", "-", "abc"). Reject it
    # rather than quietly storing a blank.
    if not any(ch.isdigit() for ch in s):
        raise ValueError(f"{v!r} is not a number")

    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()

    for ch in _CURRENCY_CHARS:
        s = s.replace(ch, "")
    for ch in _SPACE_CHARS:
        s = s.replace(ch, "")
    # Thousands separators and a trailing percent sign.
    s = s.replace(",", "").replace("_", "").rstrip("%")

    if not s or s in {"-", "+", ".", "-.", "+."}:
        return None

    value = float(s)
    # '1e400' parses to inf, which would poison every total it touches.
    if not math.isfinite(value):
        raise ValueError(f"{v!r} is out of range")
    return -value if negative else value


def _floats(**fields):
    """Parse several user-entered numbers, naming the offending field on failure."""
    out = {}
    for name, raw in fields.items():
        try:
            out[name] = _float(raw)
        except ValueError:
            label = name.replace("_", " ")
            raise ValueError(f"'{raw}' is not a valid number for {label}")
    return out


def resolve_account(conn, account_id: str, new_account: str):
    """(id, error). A typed-in new account name wins over the dropdown selection.

    The id arrives as raw form text, so it can be anything at all. Left unchecked,
    a non-numeric one raises ValueError out of int() and one that names a row that
    no longer exists trips the foreign key — both of which reach the user as a
    traceback rather than as the "not found" message every other route gives.
    """
    name = (new_account or "").strip()
    if name:
        conn.execute("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
        return row["id"], None

    raw = (account_id or "").strip()
    if not raw:
        return None, None
    try:
        aid = int(raw)
    except ValueError:
        return None, f"'{account_id}' is not a valid account"
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
        return None, "That account no longer exists — reload the page and try again"
    return aid, None


def resolve_bucket(conn, bucket_id: str):
    """(id, error). Blank means deliberately unassigned; see resolve_account."""
    raw = (bucket_id or "").strip()
    if not raw:
        return None, None
    try:
        bid = int(raw)
    except ValueError:
        return None, f"'{bucket_id}' is not a valid bucket"
    if not conn.execute("SELECT 1 FROM buckets WHERE id=?", (bid,)).fetchone():
        return None, "That bucket no longer exists — reload the page and try again"
    return bid, None


def validate_item(conn, name, kind, currency, asset_class):
    """Returns a message if these can't be stored, or None if they're fine.

    The dropdowns can only offer valid values, so this only fires on a hand-made
    request — but both of these fail *quietly* rather than loudly if they get in,
    which is what makes them worth refusing at the door:

      * an unknown `kind` matches none of the sections on the items page, so the row
        is invisible and uneditable there while still counting toward net worth.
      * an unknown currency has no `fx_rates` row, and valuation falls back to a rate
        of 1.0 — so ¥1,000,000 under a typo'd code reads as $1,000,000 rather than
        as an obvious error.
    """
    # `required` on the form input is browser-side only, and a nameless row is
    # near-impossible to identify again once it is in the table.
    if not (name or "").strip():
        return "Give the item a name"
    if kind not in KINDS:
        return f"'{kind}' is not a valid type. Pick one of: {', '.join(KINDS)}"
    if asset_class and asset_class not in ASSET_CLASSES:
        return (f"'{asset_class}' is not a valid asset class. Pick one of: "
                f"{', '.join(ASSET_CLASSES)}")
    known = {r["currency"] for r in conn.execute("SELECT currency FROM fx_rates")}
    if currency not in known:
        return (f"There is no exchange rate for '{currency}', so it can't be valued. "
                f"Known currencies: {', '.join(sorted(known))}")
    return None


@app.post("/items")
def create_item(
    name: str = Form(...),
    kind: str = Form(...),
    bucket_id: str = Form(""),
    account_id: str = Form(""),
    new_account: str = Form(""),
    currency: str = Form("USD"),
    amount: str = Form(""),
    ticker: str = Form(""),
    asset_class: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    loan_original: str = Form(""),
    loan_balance: str = Form(""),
    conn=Depends(db_session),
):
    try:
        nums = _floats(amount=amount, quantity=quantity,
                       loan_original=loan_original, loan_balance=loan_balance)
    except ValueError as exc:
        return RedirectResponse("/items?err=" + quote_plus(str(exc)), 303)

    bad = validate_item(conn, name, kind, currency, asset_class)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)

    bucket, bad = resolve_bucket(conn, bucket_id)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)
    account, bad = resolve_account(conn, account_id, new_account)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)

    conn.execute(
        "INSERT INTO items (name, kind, bucket_id, account_id, currency, amount, ticker,"
        " asset_class, quantity, unit, loan_original, loan_balance, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name.strip(),
            kind,
            None if kind == "liability" else bucket,
            account,
            currency,
            nums["amount"],
            ticker.strip().upper() or None,
            asset_class or None,
            nums["quantity"],
            (unit or "toz") if asset_class == "metal" else None,
            nums["loan_original"] if kind == "property" else None,
            nums["loan_balance"] if kind == "property" else None,
            now_iso(),
        ),
    )
    conn.commit()
    item_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    if kind == "priced":
        return refresh_price(item_id)
    return RedirectResponse("/items?msg=" + quote_plus(f"Added {name.strip()}"), 303)


@app.post("/items/{item_id}")
def update_item(
    item_id: int,
    name: str = Form(...),
    kind: str = Form(...),
    bucket_id: str = Form(""),
    account_id: str = Form(""),
    new_account: str = Form(""),
    currency: str = Form("USD"),
    amount: str = Form(""),
    ticker: str = Form(""),
    asset_class: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    loan_original: str = Form(""),
    loan_balance: str = Form(""),
    refetch: str = Form(""),
    drop_loan: str = Form(""),
    conn=Depends(db_session),
):
    try:
        nums = _floats(amount=amount, quantity=quantity,
                       loan_original=loan_original, loan_balance=loan_balance)
    except ValueError as exc:
        return RedirectResponse("/items?err=" + quote_plus(str(exc)), 303)

    bad = validate_item(conn, name, kind, currency, asset_class)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)

    bucket, bad = resolve_bucket(conn, bucket_id)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)
    account, bad = resolve_account(conn, account_id, new_account)
    if bad:
        return RedirectResponse("/items?err=" + quote_plus(bad), 303)

    prev = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not prev:
        return RedirectResponse("/items?err=Item+not+found", 303)
    # Only a property keeps loan figures, so any other kind would silently erase
    # the mortgage and raise net worth by the whole balance.
    if (prev["kind"] == "property" and kind != "property" and not drop_loan
            and (prev["loan_balance"] or prev["loan_original"])):
        return RedirectResponse(
            "/items?err=" + quote_plus(
                f"{prev['name']} still has a loan recorded — changing its type would "
                f"delete it. Confirm the change in the Edit panel, or keep it as "
                f"Real estate"
            ), 303,
        )

    if prev["kind"] == "manual" and kind != "manual" and conn.execute(
            "SELECT 1 FROM business_sheets WHERE item_id=?", (item_id,)).fetchone():
        return RedirectResponse(
            "/items?err=" + quote_plus(
                f"{prev['name']} has a business balance sheet, which only a Manual "
                f"value item can carry. Delete the sheet on the Business page first"
            ), 303,
        )

    new_ticker = ticker.strip().upper() or None
    conn.execute(
        "UPDATE items SET name=?, kind=?, bucket_id=?, account_id=?, currency=?, amount=?,"
        " ticker=?, asset_class=?, quantity=?, unit=?, loan_original=?, loan_balance=?,"
        " updated_at=? WHERE id=?",
        (
            name.strip(),
            kind,
            None if kind == "liability" else bucket,
            account,
            currency,
            nums["amount"],
            new_ticker,
            asset_class or None,
            nums["quantity"],
            (unit or prev["unit"] or "toz") if asset_class == "metal" else None,
            nums["loan_original"] if kind == "property" else None,
            nums["loan_balance"] if kind == "property" else None,
            now_iso(),
            item_id,
        ),
    )
    # A changed ticker leaves the old symbol's price attached, which would silently
    # value the holding off the wrong security.
    ticker_changed = new_ticker != prev["ticker"]
    if kind == "priced" and ticker_changed:
        conn.execute(
            "UPDATE items SET last_price=NULL, price_currency=NULL, last_price_at=NULL"
            " WHERE id=?",
            (item_id,),
        )
    conn.commit()

    if kind == "priced" and (refetch or ticker_changed):
        return refresh_price(item_id)
    return RedirectResponse("/items?msg=" + quote_plus(f"Saved {name.strip()}"), 303)


def patch_payload(conn):
    """Formatted values keyed by `data-bind`, so an inline save can refresh every
    figure it affects without reloading the page."""
    p = portfolio(conn)
    out = {
        "total-assets": money(p["assets"]),
        "total-liabilities": money(p["liabilities"]),
        "total-networth": money(p["net_worth"]),
        "allocation-base": money(p["allocation_base"]),
    }
    for i in p["items"]:
        out[f"item-usd-{i['id']}"] = money(i["value_usd"])
        out[f"item-native-{i['id']}"] = (
            f"{i['native_value']:,.0f}" if i["native_value"] is not None else "—"
        )
        if i["kind"] == "property":
            out[f"item-equity-{i['id']}"] = num(i["equity"])
        if i["kind"] == "priced":
            out[f"holding-cost-{i['id']}"] = money(i["cost_basis_usd"])
            out[f"holding-unreal-{i['id']}"] = money(i["unrealized_usd"])
            out[f"holding-real-{i['id']}"] = money(i["realized_usd"])
    for b in p["buckets"]:
        out[f"bucket-value-{b['id']}"] = money(b["value_usd"])
        if b["excluded"]:
            out[f"bucket-actual-{b['id']}"] = "—"
            out[f"bucket-drift-{b['id']}"] = "—"
            out[f"bucket-rebalance-{b['id']}"] = "—"
        else:
            out[f"bucket-actual-{b['id']}"] = pct(b["actual_pct"])
            sign = "+" if b["drift_pct"] > 0 else ""
            out[f"bucket-drift-{b['id']}"] = f"{sign}{b['drift_pct']:.1f}%"
            out[f"bucket-rebalance-{b['id']}"] = (
                ("buy " if b["drift_usd"] > 0 else "sell ") + money(abs(b["drift_usd"]))
            )
    # Accounts page: balances are edited there account by account, so each account's
    # subtotals and its matrix row follow along. "none" is the synthetic No account group.
    av = accounts_view(conn)
    out["accounts-assets"] = money(av["grand_assets"])
    out["accounts-liabilities"] = money(av["grand_liabilities"])
    out["accounts-net"] = money(av["grand_assets"] - av["grand_liabilities"])
    for r in av["matrix_rows"]:
        a = r["id"] if r["id"] is not None else "none"
        out[f"acct-{a}-assets"] = money(r["assets_total"])
        out[f"acct-{a}-liabilities"] = money(r["liabilities_total"])
        out[f"acct-{a}-net"] = money(r["net"])
        for bid, v in r["cells"].items():
            out[f"acct-{a}-cell-{bid}"] = money(v) if v else "·"
        for sp in r["splits"]:
            out[f"acct-{a}-split-{sp['bucket_key'] or 'none'}"] = (
                f"{money(sp['value_usd'])} ({sp['pct']:.0f}%)")
    for bid, v in av["col_totals"].items():
        out[f"accounts-col-{bid}"] = money(v)
    out.update(business_patch(p["items"]))
    return out


def business_view(items):
    """The Business page: one entry per balance sheet, biggest first."""
    out = []
    for i in items:
        s = i.get("sheet")
        if not s:
            continue
        out.append({
            **s,
            "item": i,
            "linked": i["sheet_linked"],
            # What the main sheet counts for this business right now, and how far
            # a hand-entered figure sits from your share of book equity.
            "main_usd": i["value_usd"],
            "gap_usd": None if i["sheet_linked"] or i["value_usd"] is None
            else i["value_usd"] - s["share_usd"],
        })
    out.sort(key=lambda b: (-(b["share_usd"] or 0.0), b["item"]["name"].lower()))
    return {
        "sheets": out,
        "equity_usd": sum(b["equity_usd"] for b in out),
        "share_usd": sum(b["share_usd"] for b in out),
        "linked_usd": sum(b["share_usd"] for b in out if b["linked"]),
    }


def _signed_money(v):
    return "—" if v is None else ("+" if v > 0 else "") + money(v)


def business_patch(items):
    bv = business_view(items)
    out = {
        "biz-total-equity": money(bv["equity_usd"]),
        "biz-total-share": money(bv["share_usd"]),
        "biz-total-linked": money(bv["linked_usd"]),
    }
    for b in bv["sheets"]:
        k = f"biz-{b['id']}"
        out[f"{k}-assets"] = money(b["assets_usd"])
        out[f"{k}-liabilities"] = money(b["liabilities_usd"])
        out[f"{k}-equity"] = money(b["equity_usd"])
        out[f"{k}-share"] = money(b["share_usd"])
        out[f"{k}-main"] = money(b["main_usd"])
        out[f"{k}-gap"] = _signed_money(b["gap_usd"])
        for line in b["assets"] + b["liabilities"]:
            out[f"bizline-{line['id']}-usd"] = money(line["value_usd"])
    return out


# Fields safe to edit inline. Anything else needs the full edit form.
INLINE_ITEM_FIELDS = {"amount", "quantity", "bucket_id", "loan_balance", "loan_original"}


@app.post("/items/{item_id}/inline")
async def inline_item(item_id: int, request: Request, conn=Depends(db_session)):
    form = await request.form()
    field = (form.get("field") or "").strip()
    value = (form.get("value") or "").strip()

    if field not in INLINE_ITEM_FIELDS:
        return JSONResponse({"ok": False, "error": f"Field '{field}' is not editable here"}, 400)

    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Item not found"}, 404)

    if field == "bucket_id":
        if row["kind"] == "liability":
            return JSONResponse(
                {"ok": False,
                 "error": "Liabilities aren't bucketed — they reduce net worth only"}, 400)
        prev = str(row["bucket_id"] or "")
        bucket, bad = resolve_bucket(conn, value)
        if bad:
            return JSONResponse({"ok": False, "error": bad}, 400)
        conn.execute("UPDATE items SET bucket_id=?, updated_at=? WHERE id=?",
                     (bucket, now_iso(), item_id))
    else:
        if row["kind"] == "priced" and field == "quantity":
            has_lots = conn.execute(
                "SELECT COUNT(*) c FROM lots WHERE item_id=?", (item_id,)
            ).fetchone()["c"]
            if has_lots:
                return JSONResponse(
                    {"ok": False,
                     "error": "Quantity comes from this holding's lots — edit the ledger instead"},
                    400,
                )
        if field == "amount" and _sheet_valued(conn, item_id):
            return JSONResponse(
                {"ok": False,
                 "error": "Value comes from this business's balance sheet — edit it "
                          "on the Business page"}, 400)
        try:
            parsed = _float(value)
        except ValueError:
            return JSONResponse({"ok": False, "error": f"'{value}' is not a number"}, 400)
        # num() not '%g': a ¥1,234,567 balance would come back as '1.23457e+06' in the
        # field after an undo.
        prev = num(row[field])
        conn.execute(f"UPDATE items SET {field}=?, updated_at=? WHERE id=?",
                     (parsed, now_iso(), item_id))

    conn.commit()
    payload = patch_payload(conn)
    return JSONResponse({"ok": True, "prev": prev, "patch": payload})


@app.post("/buckets/{bucket_id}/inline")
async def inline_bucket(bucket_id: int, request: Request, conn=Depends(db_session)):
    form = await request.form()
    if (form.get("field") or "") != "target_pct":
        return JSONResponse({"ok": False, "error": "Only target_pct is editable"}, 400)
    value = (form.get("value") or "").strip()
    try:
        parsed = _float(value) or 0.0
    except ValueError:
        return JSONResponse({"ok": False, "error": f"'{value}' is not a number"}, 400)

    row = conn.execute("SELECT target_pct FROM buckets WHERE id=?", (bucket_id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Bucket not found"}, 404)
    prev = f"{row['target_pct']:g}"
    conn.execute("UPDATE buckets SET target_pct=? WHERE id=?", (parsed, bucket_id))
    conn.commit()
    payload = patch_payload(conn)
    total = conn.execute(
        "SELECT SUM(target_pct) t FROM buckets WHERE excluded = 0"
    ).fetchone()["t"] or 0
    payload["target-total"] = f"{total:.1f}%"
    return JSONResponse({"ok": True, "prev": prev, "patch": payload,
                         "warn": None if abs(total - 100) < 0.05
                         else f"Targets now sum to {total:.1f}%"})


@app.post("/accounts")
def create_account(name: str = Form(...), conn=Depends(db_session)):
    name = name.strip()
    if not name:
        return RedirectResponse("/accounts?err=Account+needs+a+name", 303)
    dup = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if dup:
        return RedirectResponse("/accounts?err=Account+already+exists", 303)
    conn.execute("INSERT INTO accounts (name) VALUES (?)", (name,))
    conn.commit()
    return RedirectResponse("/accounts?msg=" + quote_plus(f"Added {name}"), 303)


@app.post("/accounts/{account_id}")
def rename_account(account_id: int, name: str = Form(...), conn=Depends(db_session)):
    name = name.strip()
    if not name:
        return RedirectResponse("/accounts?err=Account+needs+a+name", 303)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        return RedirectResponse("/accounts?err=Account+not+found", 303)
    clash = conn.execute(
        "SELECT id FROM accounts WHERE name = ? AND id != ?", (name, account_id)
    ).fetchone()
    if clash:
        return RedirectResponse("/accounts?err=Another+account+has+that+name", 303)
    conn.execute("UPDATE accounts SET name=? WHERE id=?", (name, account_id))
    conn.commit()
    return RedirectResponse("/accounts?msg=Renamed", 303)


@app.post("/accounts/{account_id}/delete")
def delete_account(account_id: int, conn=Depends(db_session)):
    """Deleting an account never deletes its holdings — they fall back to 'No account'."""
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        return RedirectResponse("/accounts?err=Account+not+found", 303)
    n = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE account_id=?", (account_id,)
    ).fetchone()["c"]
    conn.execute("UPDATE items SET account_id=NULL WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()
    msg = f"Account deleted — {n} item(s) moved to No account" if n else "Account deleted"
    return RedirectResponse("/accounts?msg=" + quote_plus(msg), 303)


@app.post("/items/{item_id}/delete")
def delete_item(item_id: int, conn=Depends(db_session)):
    if not conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        return RedirectResponse("/items?err=Item+not+found", 303)
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    return RedirectResponse("/items?msg=Deleted", 303)


@app.post("/items/{item_id}/refresh")
def refresh_price(item_id: int):
    # Keeps its own connection rather than the db_session dependency: create_item
    # and update_item call this directly as a function, where a FastAPI dependency
    # would never be resolved.
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return RedirectResponse("/items?err=Item+not+found", 303)
        try:
            price, ccy = prices.fetch_price(row["ticker"], row["asset_class"])
        except prices.PriceError as e:
            return RedirectResponse("/items?err=" + quote_plus(str(e)), 303)
        conn.execute(
            "UPDATE items SET last_price=?, price_currency=?, last_price_at=? WHERE id=?",
            (price, ccy, now_iso(), item_id),
        )
        conn.commit()
        return RedirectResponse(
            "/items?msg=" + quote_plus(f"{row['ticker']} updated"), 303)
    finally:
        conn.close()


@app.post("/prices/refresh-all")
def refresh_all():
    ok, failures = jobs.refresh_all_prices()
    msg = f"Refreshed {ok} holdings"
    if failures:
        for symbol, reason in failures:
            log.warning("price refresh failed for %s: %s", symbol, reason)
        reasons = sorted({r for _, r in failures})
        msg += f" — failed: {', '.join(s for s, _ in failures)}"
        # One shared reason is nearly always the real story (a rate limit hits
        # everything at once). Say it, rather than leaving a bare list of symbols
        # that reads as though the tickers are wrong.
        msg += f" — {reasons[0]}" if len(reasons) == 1 else f" — e.g. {reasons[0]}"
    return RedirectResponse("/items?msg=" + quote_plus(msg), 303)


@app.post("/buckets/targets")
async def update_targets(request: Request, conn=Depends(db_session)):
    form = await request.form()
    try:
        targets = {
            int(key.removeprefix("target_")): (_float(value) or 0.0)
            for key, value in form.items()
            if key.startswith("target_")
        }
    except ValueError as exc:
        return RedirectResponse("/allocation?err=" + quote_plus(str(exc)), 303)

    for bucket_id, target in targets.items():
        conn.execute("UPDATE buckets SET target_pct=? WHERE id=?", (target, bucket_id))
    conn.commit()
    return RedirectResponse("/allocation?msg=Targets+saved", 303)


def _date_to_iso(d: str):
    """'2024-12-31' -> a full UTC timestamp, so backdated and live snapshots sort together."""
    d = (d or "").strip()
    if not d:
        return None
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None
    return f"{d}T00:00:00+00:00"


@app.post("/snapshots")
def create_snapshot(label: str = Form(""), taken_on: str = Form(""), conn=Depends(db_session)):
    taken_at = _date_to_iso(taken_on)
    if taken_on.strip() and taken_at is None:
        return RedirectResponse("/history?err=Use+a+YYYY-MM-DD+date", 303)
    take_snapshot(conn, label.strip() or None, taken_at)
    return RedirectResponse("/history?msg=Snapshot+taken", 303)


@app.post("/snapshots/manual")
def create_manual_snapshot(
    taken_on: str = Form(...),
    assets: str = Form(""),
    liabilities: str = Form(""),
    label: str = Form(""),
    conn=Depends(db_session),
):
    """Type in a past period's figures directly, without the item list needing to be
    historically accurate."""
    taken_at = _date_to_iso(taken_on)
    if taken_at is None:
        return RedirectResponse("/history?err=Use+a+YYYY-MM-DD+date", 303)

    try:
        nums = _floats(assets=assets, liabilities=liabilities)
    except ValueError as exc:
        return RedirectResponse("/history?err=" + quote_plus(str(exc)), 303)
    a = nums["assets"] or 0.0
    li = nums["liabilities"] or 0.0
    if not assets.strip() and not liabilities.strip():
        return RedirectResponse("/history?err=Enter+assets+or+liabilities", 303)

    conn.execute(
        "INSERT INTO snapshots (taken_at, label, assets_usd, liabilities_usd, net_worth_usd)"
        " VALUES (?,?,?,?,?)",
        (taken_at, label.strip() or None, a, li, a - li),
    )
    conn.commit()
    return RedirectResponse("/history?msg=Historical+entry+added", 303)


@app.post("/snapshots/{snap_id}")
def update_snapshot(
    snap_id: int,
    taken_on: str = Form(...),
    assets: str = Form(""),
    liabilities: str = Form(""),
    label: str = Form(""),
    conn=Depends(db_session),
):
    taken_at = _date_to_iso(taken_on)
    if taken_at is None:
        return RedirectResponse("/history?err=Use+a+YYYY-MM-DD+date", 303)
    try:
        nums = _floats(assets=assets, liabilities=liabilities)
    except ValueError as exc:
        return RedirectResponse("/history?err=" + quote_plus(str(exc)), 303)
    if not conn.execute("SELECT 1 FROM snapshots WHERE id=?", (snap_id,)).fetchone():
        return RedirectResponse("/history?err=Snapshot+not+found", 303)
    a = nums["assets"] or 0.0
    li = nums["liabilities"] or 0.0
    conn.execute(
        "UPDATE snapshots SET taken_at=?, label=?, assets_usd=?, liabilities_usd=?,"
        " net_worth_usd=? WHERE id=?",
        (taken_at, label.strip() or None, a, li, a - li, snap_id),
    )
    conn.commit()
    return RedirectResponse("/history?msg=Snapshot+updated", 303)


@app.post("/backups/run")
def run_backup():
    try:
        info = jobs.backup_database(label="manual")
    except Exception as exc:
        return RedirectResponse("/history?err=" + quote_plus(f"Backup failed: {exc}"), 303)

    # Say what actually went into it. A backup of an empty database is the one
    # thing you must not discover later, at the point you try to restore it.
    items = jobs.count_items(os.path.join(jobs.backup_dir(), info["file"]))
    if items == 0:
        return RedirectResponse("/history?err=" + quote_plus(
            f"{info['file']} was written but it is EMPTY — the database has no items "
            "in it. Do not rely on this one; restore an earlier backup instead."), 303)
    return RedirectResponse(
        "/history?msg=" + quote_plus(f"Backup written: {info['file']} ({items} items)"), 303
    )


@app.get("/backups/download/{name}")
def download_backup(name: str):
    path = jobs.resolve_backup(name)
    if not path:
        return RedirectResponse("/history?err=No+such+backup", 303)
    return FileResponse(path, media_type="application/x-sqlite3", filename=name)


@app.post("/backups/delete")
def delete_backup(name: str = Form(...)):
    """Remove one backup file.

    `resolve_backup` is the guard: it only ever returns a path inside the backup
    directory whose name carries the backup prefix, so a crafted name can't reach
    the database itself or anything else on the volume.
    """
    path = jobs.resolve_backup(name)
    if not path:
        return RedirectResponse("/history?err=No+such+backup", 303)

    remaining = [b for b in jobs.list_backups() if b["name"] != name]
    try:
        os.remove(path)
    except OSError as exc:
        return RedirectResponse(
            "/history?err=" + quote_plus(f"Could not delete {name}: {exc}"), 303)

    # Older backups can have -wal/-shm beside them; without this they pile up as
    # orphans that nothing lists and nothing ever cleans.
    jobs.remove_sidecars(path)

    msg = f"Deleted {name}"
    if not remaining:
        msg += " — that was your last backup, so take a fresh one now"
    return RedirectResponse("/history?msg=" + quote_plus(msg), 303)


@app.get("/backups/export.json")
def export_json(conn=Depends(db_session)):
    """Everything, in a portable format that outlives the schema.

    A .db restore only works against a compatible app version; this stays readable
    whatever happens later.
    """
    tables = [
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    # Quoted: after restoring an uploaded file, table names are whatever it held.
    data = {
        t: [dict(r) for r in conn.execute(
            'SELECT * FROM "{}"'.format(t.replace('"', '""')))]
        for t in tables
    }
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return JSONResponse(
        {"exported_at": now_iso(), "tables": data},
        headers={"Content-Disposition":
                 f'attachment; filename="networth-export-{stamp}.json"'},
    )


# Uploads are written to the data folder before they are checked, so an unbounded
# one could fill the disk. Both limits are far above any real file.
MAX_BACKUP_UPLOAD = 100 * 1024 * 1024
MAX_SHEET_UPLOAD = 25 * 1024 * 1024


async def _save_upload(file, dest, limit):
    """Stream an upload to `dest`, refusing it once it passes `limit` bytes."""
    written = 0
    with open(dest, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise ValueError(
                    f"the file is larger than {limit // (1024 * 1024)} MB")
            fh.write(chunk)


@app.post("/backups/upload")
async def upload_backup(file: UploadFile = File(...)):
    """Stage an uploaded .db or JSON export and show what's in it before committing."""
    raw = (file.filename or "").lower()
    if not (raw.endswith(".db") or raw.endswith(".json")):
        return RedirectResponse(
            "/history?err=" + quote_plus("Upload a .db backup or a .json export"), 303)

    os.makedirs(jobs.backup_dir(), exist_ok=True)
    staged = os.path.join(jobs.backup_dir(), f"{jobs.BACKUP_PREFIX}{jobs.UPLOAD_NAME}.db")
    tmp = staged + (".json" if raw.endswith(".json") else ".part")
    try:
        await _save_upload(file, tmp, MAX_BACKUP_UPLOAD)

        if raw.endswith(".json"):
            built = jobs.import_json(tmp)
            os.replace(built, staged)
            os.remove(tmp)
        else:
            os.replace(tmp, staged)

        jobs.inspect_db(staged)          # rejects corrupt or unrelated files
    except Exception as exc:
        for path in (tmp, staged):
            if os.path.exists(path):
                os.remove(path)
        return RedirectResponse("/history?err=" + quote_plus(f"Upload rejected: {exc}"), 303)

    return RedirectResponse(
        "/history?msg=" + quote_plus(f"'{file.filename}' staged — review it below, "
                                     "then type RESTORE to apply") + "#staged", 303)


@app.post("/backups/restore")
def restore_backup(name: str = Form(...), confirm: str = Form("")):
    if confirm.strip().upper() != "RESTORE":
        return RedirectResponse(
            "/history?err=" + quote_plus("Type RESTORE to confirm — nothing was changed"), 303
        )
    try:
        info = jobs.restore_backup(name)
    except Exception as exc:
        return RedirectResponse("/history?err=" + quote_plus(f"Restore failed: {exc}"), 303)
    return RedirectResponse(
        "/history?msg=" + quote_plus(
            f"Restored {info['restored']} — your previous data was saved as {info['safety_copy']}"
        ),
        303,
    )


# ------------------------------------------------------------------------
# Importing a trade-history spreadsheet
# ------------------------------------------------------------------------

UNDATED_DEFAULT = "2026-01-01"


def _price_items(rows):
    """Fetch prices for freshly created holdings, then write them in one go.

    Two rules here, both learned the hard way:

    1. **No database connection is held while fetching.** Pricing an unknown crypto
       ticker makes `resolve_coin_id` open its own connection to cache the CoinGecko
       id, and SQLite allows exactly one writer — so doing this inside an open write
       transaction deadlocks against itself, waits out the 15s busy timeout and
       fails with `database is locked`. `jobs.refresh_all_prices` has always fetched
       with no connection open; this follows it.
    2. **It never raises.** By the time this runs the rows are already saved, so a
       rate-limited feed or an unlisted ticker must not turn a successful import into
       an error page. Whatever fails here the scheduled refresh picks up later.
    """
    priced = [r for r in rows if r.get("ticker")]

    # Grouped by ticker and batched, for the reason spelled out in
    # jobs.refresh_all_prices: an import can create dozens of holdings at once, and
    # pricing them one request at a time is the surest way to be rate limited into
    # having none of them priced.
    quotes = {}
    crypto = [r["ticker"] for r in priced if r["asset_class"] == "crypto"]
    if crypto:
        got, errs = prices.fetch_crypto_many(crypto)
        for t, pair in got.items():
            quotes[(t, "crypto")] = pair
        for t, reason in errs.items():
            log.warning("No price for %s after import: %s", t, reason)

    for key in {(r["ticker"].strip().upper(), r["asset_class"] or "") for r in priced}:
        if key[1] == "crypto" or key in quotes:
            continue
        try:
            quotes[key] = prices.fetch_price(key[0], key[1])
        except Exception as exc:                      # noqa: BLE001 - see docstring
            log.warning("No price for %s after import: %s", key[0], exc)

    stamp = now_iso()
    updates = []
    for r in priced:
        quote = quotes.get((r["ticker"].strip().upper(), r["asset_class"] or ""))
        if quote:
            updates.append((quote[0], quote[1], stamp, r["id"]))

    if not updates:
        return 0
    conn = connect()
    try:
        conn.executemany(
            "UPDATE items SET last_price=?, price_currency=?, last_price_at=?"
            " WHERE id=?", updates)
        conn.commit()
    except Exception as exc:                          # noqa: BLE001
        log.warning("Could not store prices after import: %s", exc)
        return 0
    finally:
        conn.close()
    return len(updates)


def _import_mapping(params, rows):
    """Destination account per ticker, from the form or from the suggestion."""
    suggested = importer.default_locations(rows)
    tickers = {r["ticker"] for r in rows}
    return {t: ((params.get(f"loc_{t}") or "").strip() or suggested.get(t, ""))
            for t in tickers}


def _read_staged(undated):
    """Parse the staged workbook, or return the reason it can't be read."""
    path = importer.staged_path(jobs.backup_dir())
    if not os.path.exists(path):
        return None, None
    try:
        return importer.read_workbook(path, undated_fallback=undated), None
    except Exception as exc:
        return None, str(exc)


@app.get("/import")
def import_page(request: Request, conn=Depends(db_session)):
    undated = (request.query_params.get("undated") or "").strip() or UNDATED_DEFAULT
    if _date_to_iso(undated) is None:
        undated = UNDATED_DEFAULT

    parsed, parse_error = _read_staged(undated)

    batches = [
        dict(r) for r in conn.execute(
            "SELECT * FROM import_batches ORDER BY imported_at DESC, id DESC"
        )
    ]
    mapping, planned, tickers = {}, None, []
    if parsed:
        mapping = _import_mapping(request.query_params, parsed["rows"])
        planned = importer.plan(parsed["rows"], mapping, conn)
        tickers = sorted({r["ticker"] for r in parsed["rows"]})
    account_names = [
        r["name"] for r in conn.execute("SELECT name FROM accounts ORDER BY name")
    ]

    return render(request, "import.html", nav="holdings", parsed=parsed,
                  parse_error=parse_error, planned=planned, mapping=mapping,
                  tickers=tickers, batches=batches, undated=undated,
                  account_names=account_names,
                  staged_name=request.query_params.get("file", ""))


@app.post("/import/upload")
async def import_upload(file: UploadFile = File(...)):
    name = (file.filename or "").strip()
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return RedirectResponse(
            "/import?err=" + quote_plus("Upload the .xlsx spreadsheet itself"), 303)

    os.makedirs(jobs.backup_dir(), exist_ok=True)
    path = importer.staged_path(jobs.backup_dir())
    tmp = path + ".part"
    try:
        await _save_upload(file, tmp, MAX_SHEET_UPLOAD)
        os.replace(tmp, path)
        importer.read_workbook(path, undated_fallback=UNDATED_DEFAULT)
    except Exception as exc:
        for p in (tmp, path):
            if os.path.exists(p):
                os.remove(p)
        return RedirectResponse("/import?err=" + quote_plus(str(exc)), 303)

    return RedirectResponse(
        "/import?file=" + quote_plus(name) + "&msg="
        + quote_plus(f"Read '{name}' — nothing has been written yet. Check the "
                     f"destinations and the preview below."), 303)


@app.post("/import/commit")
async def import_commit(request: Request, conn=Depends(db_session)):
    form = await request.form()
    if (form.get("confirm") or "").strip().upper() != "IMPORT":
        return RedirectResponse(
            "/import?err=" + quote_plus("Type IMPORT to confirm — nothing was changed"),
            303)

    undated = (form.get("undated") or "").strip() or UNDATED_DEFAULT
    parsed, parse_error = _read_staged(undated)
    if parse_error or not parsed:
        return RedirectResponse(
            "/import?err=" + quote_plus(parse_error or "No spreadsheet is staged"), 303)

    mapping = _import_mapping(form, parsed["rows"])
    planned = importer.plan(parsed["rows"], mapping, conn)
    if not planned["holdings"]:
        return RedirectResponse(
            "/import?err=" + quote_plus("Nothing to import — every holding was held "
                                        "back or skipped"), 303)

    note = "; ".join(f"{t}→{a}" for t, a in sorted(mapping.items()) if a)
    result = importer.commit(conn, planned, form.get("source") or "spreadsheet", note)
    fresh = [
        dict(r) for r in conn.execute(
            "SELECT id, ticker, asset_class FROM items WHERE import_batch_id=?",
            (result["batch"],),
        )
    ]

    # Pricing happens with no connection held — see _price_items.
    _price_items(fresh)

    path = importer.staged_path(jobs.backup_dir())
    if os.path.exists(path):
        os.remove(path)

    held = len(planned["held_back"])
    msg = (f"Imported {result['lots']} lots into {len(planned['holdings'])} holdings "
           f"({result['items']} newly created)")
    if held:
        msg += f" — {held} holding(s) held back, see below"
    return RedirectResponse("/import?msg=" + quote_plus(msg), 303)


@app.post("/import/discard")
def import_discard():
    path = importer.staged_path(jobs.backup_dir())
    if os.path.exists(path):
        os.remove(path)
    return RedirectResponse("/import?msg=Staged+spreadsheet+discarded", 303)


@app.post("/import/batches/{batch_id}/rollback")
def import_rollback(batch_id: int, conn=Depends(db_session)):
    row = conn.execute(
        "SELECT * FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not row:
        return RedirectResponse("/import?err=Import+not+found", 303)
    result = importer.rollback(conn, batch_id)

    msg = (f"Rolled back {row['lot_count']} lots; "
           f"{result['items_removed']} holding(s) removed")
    if result["items_kept"]:
        msg += (" — kept " + ", ".join(result["items_kept"])
                + " because they still hold lots or transfers")
    return RedirectResponse("/import?msg=" + quote_plus(msg), 303)


@app.post("/snapshots/{snap_id}/delete")
def delete_snapshot(snap_id: int, conn=Depends(db_session)):
    if not conn.execute("SELECT 1 FROM snapshots WHERE id=?", (snap_id,)).fetchone():
        return RedirectResponse("/history?err=Snapshot+not+found", 303)
    conn.execute("DELETE FROM snapshot_lines WHERE snapshot_id=?", (snap_id,))
    conn.execute("DELETE FROM snapshots WHERE id=?", (snap_id,))
    conn.commit()
    return RedirectResponse("/history?msg=Snapshot+deleted", 303)
