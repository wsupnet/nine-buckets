# Nine Buckets — User guide

[← README](../README.md) · [Technical documentation](DOCUMENTATION.md) ·
[NAS deployment](../DEPLOY-NAS.md)

How to use Nine Buckets day to day, in plain language. You don't need to know anything about how
it's built. If the app isn't running yet, ask whoever set it up, or follow the install steps in
the [technical documentation](DOCUMENTATION.md#2-install-and-run).

## Contents

- [The idea in one minute](#the-idea-in-one-minute)
- [Signing in](#signing-in)
- [A quick tour](#a-quick-tour)
- [First-time setup](#first-time-setup)
- [Your monthly routine](#your-monthly-routine)
- [How to…](#how-to)
- [Understanding the numbers](#understanding-the-numbers)
- [Keeping your data safe](#keeping-your-data-safe)
- [Questions and fixes](#questions-and-fixes)

---

## The idea in one minute

Nine Buckets is a **balance sheet**: a list of everything you own and everything you owe, with
today's value of each. Add them up and you get your **net worth**.

On top of that, every asset goes into one of nine **buckets**: Safety, Cash, Job Income,
Business, Real Estate, Precious Metals, Crypto, Stocks and Collectibles. You decide what share of
your money each bucket *should* hold (your **targets**), and the app shows what it *actually*
holds and how far off you are.

Two words you'll see a lot:

- An **account** is *where* something sits: a bank, a broker, a crypto wallet, a home safe.
- A **bucket** is *what role* it plays in your plan.

They're separate on purpose. One brokerage account can hold cash (Cash bucket) and an index fund
(Stocks bucket) at the same time.

Nine Buckets doesn't connect to your bank and never asks for bank logins. You type in balances
yourself; prices for stocks, crypto and metals are looked up automatically.

## Signing in

Open the app's address in your browser (for example `http://your-nas:8000`) and enter the
passphrase. You stay signed in for 30 days on that device. **Sign out** is at the top right.

After several wrong passphrases the page asks you to wait a little before trying again. That's
normal and protects your data.

## A quick tour

The menu across the top:

| Page | What it's for |
|---|---|
| **Dashboard** | Your net worth, what you own and owe, a chart over time, and each bucket against its target |
| **Assets & Liabilities** | The full list of everything, grouped by type. Add items, edit balances, move money |
| **Holdings** | Stocks, crypto and metals: live prices, what you paid, gains and losses, every trade |
| **Accounts** | The same items grouped by *where* they sit. The easiest page for monthly updates |
| **Allocation** | Your targets, how far off each bucket is, and what to buy or sell to get back on track |
| **History** | Your net worth over time, monthly snapshots, and backups |

Handy things that work everywhere:

- **Click a number to edit it.** Type the new value and press Enter (or click away). Escape
  cancels. A small message pops up with **Undo** in case you got it wrong.
- **The ⋯ button** at the end of a row holds that row's actions: Edit, Fetch price, Delete.
- **Click a column heading** to sort by it. The app remembers how you left each table.
- **Long tables** show 10 rows at a time; change it to 15, 25 or 50 at the bottom.

## First-time setup

### 1. Set your targets

Go to **Allocation**. In the **Target %** column, click each bucket's number and type the share
you want it to hold. Job Income is left out of the percentages (it's income, not something you
own), so the other eight should add up to 100%. The page shows the running total below the table.

### 2. Add what you own and owe

Go to **Assets & Liabilities** and click **+ Add an item**. Pick a **Type**:

| Type | Use it for | You enter |
|---|---|---|
| Cash / savings balance | Bank, savings and money market accounts | The balance |
| Priced holding (ticker) | Stocks, ETFs, crypto, gold and silver | The ticker (like `VT` or `BTC`) or metal, and how much you hold |
| Real estate (property + mortgage) | A home or rental | Its current value, plus the loan's original amount and what's still owed |
| Manual value (collectible, business) | Watches, art, a share in a business | Your best estimate of its value |
| Liability (credit card, personal loan) | Debts *other than* a mortgage | What you owe |

Also choose its **Bucket**, its **Account** (pick one or type a new name), and its **Currency**.
Everything is converted to US dollars for the totals.

Tips:

- **Put a mortgage on its property**, not as a separate liability. The app counts the home's
  *equity* (value minus loan), so adding the mortgage twice would count the debt twice.
- **Liabilities don't get a bucket.** They lower your net worth, but your bucket percentages only
  describe what you own.
- For metals, pick the **Weight unit** you think in (ounces, grams, kilos or pounds).

### 3. (Optional) Import your trade history

If you track your crypto or metal purchases in a spreadsheet, go to **Holdings → Import trade
history from a spreadsheet**. Upload the file, choose where each asset lives today, and check the
preview. Nothing is saved until you type `IMPORT`, and you can undo the whole import later with
**Roll back**. The spreadsheet layout it expects is described in the
[technical documentation](DOCUMENTATION.md#11-spreadsheet-import-format).

### 4. (Optional) Add your past net worth

If you have old year-end figures, go to **History → + Add past figures** and enter the date, total
assets and total liabilities. Your chart can then reach back before you started using the app.

### 5. Take your first snapshot

Go to **History → + From current values → Record current values**. That saves today's numbers so
next month has something to compare against.

## Your monthly routine

About 10–15 minutes, with your statements open:

1. **Refresh prices.** Click **Refresh all prices & FX** (on the Dashboard, Holdings or Assets &
   Liabilities). Prices also update by themselves once a day.
2. **Update your balances.** Go to **Accounts**. Each account lists its items with the balance in
   an editable box. Work down your statements: click, type, Enter.
3. **Record any purchases or sales** of stocks, crypto or metals on **Holdings** (see
   [record a purchase](#record-a-purchase-or-sale)).
4. **Record any money you moved** between accounts (see [move money](#move-money-between-accounts)).
5. **Take a snapshot:** **History → + From current values → Record current values**. Give it a label
   like "October" if you like.
6. **Check your buckets** on **Allocation**. The **To rebalance** column says roughly how much to
   buy or sell in each bucket to match your targets.
7. **Download a backup** now and then: **History → Backups & export → ⋯ → Download**.

## How to…

### Record a purchase or sale

1. Go to **Holdings** and find the holding. Click the **▸ lots** button under its name (or
   **Add lots** if it has none yet). Its ledger opens just below.
2. Choose **Buy** or **Sell**, the trade date, quantity, price per unit and any fee. For metals
   there's also a box for the dealer premium.
3. Choose **Paid from** (for a buy) or **Deposit to** (for a sale):
   - pick the **cash account** the money came from or went to, and its balance updates for you; or
   - pick **external funds** for money that came from outside the app, or for an old purchase you're
     adding as history. (Old purchases shouldn't change today's cash balance.)
4. Click **Add lot**.

The app works out your cost and your gain or loss automatically. If you ever enter a sale for more
than you hold, the holding is flagged in red so you can find the missing purchase.

To add a brand-new holding, first create it with **+ Add an item** on Assets & Liabilities.

### Move money between accounts

On **Assets & Liabilities**, click **⇄ Move money**, then choose **From**, **To** and the
**Amount**. Use it for:

- moving savings into a brokerage account,
- paying down a credit card or loan (pick the liability as **To**),
- a mortgage payment (pick the property as **To**: the cash goes down and so does the loan).

Your net worth doesn't change when you move money between your own accounts; only where it sits
changes. Different currencies are converted for you.

### Move coins or shares to another place

For example, from an exchange to a cold wallet: on **Assets & Liabilities**, click **⇄ Move
holdings**, choose the holding to move from, and where it goes (an existing holding with the same
ticker, or **a new holding** in another account). Enter the amount sent and, if a network fee took
a little, the amount that arrived. This isn't a sale: what you originally paid travels with the
units.

### Fix a mistake

- **Just edited a number?** Click **Undo** in the message that pops up.
- **Wrong name, ticker or account?** Use **⋯ → Edit** on that row.
- **A money or holding move you didn't mean?** Click **Recent activity** on Assets & Liabilities
  and **Reverse** it.
- **A wrong trade?** Click **Delete** on that row of the holding's ledger. If it was paid from a
  cash account, the money goes back.
- **A bad import?** Use **Roll back** on the Import page.

### Find a holding that disappeared

When you sell all of something, it moves to **Closed positions** at the bottom of Holdings. It
isn't deleted; its past gains still count. If you buy it again, it comes back on its own.

## Understanding the numbers

| Term | Meaning |
|---|---|
| **Net worth** | Everything you own minus everything you owe, in US dollars |
| **Equity** (real estate) | The property's value minus what's still owed on the mortgage |
| **Allocation base** | The total of everything in a bucket. It's higher than net worth because debts (other than a mortgage) aren't in a bucket |
| **Actual %** | The share of the allocation base a bucket holds |
| **Drift** | Actual % minus Target %. Orange means more than 3 points off |
| **To rebalance** | Roughly how much to buy (green) or sell (red) to bring a bucket back to its target |
| **Cost basis** | What you paid for what you still hold, including fees |
| **Unrealized** | Today's value minus cost basis: gain or loss you'd make if you sold now |
| **Realized** | Gain or loss you've actually locked in by selling. The oldest purchases are counted as sold first |
| **Premium** (metals) | What the dealer charged above the metal's market price |

## Keeping your data safe

- **Automatic backups** are taken every day and kept for 30 days. See them under **History →
  Backups & export**.
- **Back up now** makes one immediately, for example before a big change.
- **⋯ → Download** saves a single backup file to your computer. Do this now and then, so you have a
  copy even if the device running the app fails.
- **⋯ → Restore** brings a backup back. Your current data is saved first, so restoring is itself
  reversible. You'll be asked to type `RESTORE` to confirm.
- **Restore from file** brings back a backup you downloaded earlier.

## Questions and fixes

**A price says "not fetched".** Click **Refresh all prices & FX**. If it stays, check the ticker on
that row (**⋯ → Edit**); stocks use their exchange ticker (`VT`), crypto its symbol (`BTC`).

**I was asked to wait before signing in again.** Too many wrong passphrases in a row. Wait the time
shown, then try again.

**The app is empty after an update.** Don't re-enter anything. Your data is almost certainly still
safe in a backup: ask whoever runs the app to check the data folder, or restore the newest backup
that shows items under **History → Backups & export**.

**My debts don't show in the bucket chart.** That's by design. Debts lower your net worth, but the
buckets describe only what you own. The Allocation page notes the difference at the top.

**Can I add a currency that isn't in the list?** Not from the app yet. The list includes USD, EUR,
GBP, JPY, CAD, AUD and CHF.

**I want the details.** The [technical documentation](DOCUMENTATION.md) explains exactly how every
number is calculated.
