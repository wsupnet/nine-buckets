"""Write a small fictional trade-history workbook for the import demo.

Runs inside the demo app container (it has openpyxl):

    docker exec networth-demo python - < scripts/demo/make-sample-sheet.py
    docker cp networth-demo:/tmp/demo-trades.xlsx demo-out/

Same shape the importer expects: a Crypto tab (one row per trade, negative
quantity = sell) and a Precious Metals tab with a GOLD block. All made up.
"""
from datetime import date

from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Crypto"
ws.append(["Date", "Platform", "Crypto", "Ticker", "Quantity", "Total Paid"])
for row in [
    (date(2024, 2, 9), "Exchange H", "Solana", "SOL", 14.5, 1517.42),
    (date(2024, 9, 23), "Exchange H", "Solana", "SOL", 6.25, 893.17),
    (date(2025, 3, 4), "Exchange H", "Solana", "SOL", -4.0, 571.36),
    (date(2024, 5, 17), "Exchange H", "Cardano", "ADA", 1830, 823.51),
]:
    ws.append(list(row))

pm = wb.create_sheet("Precious Metals")
pm.append(["GOLD"])
pm.append(["Date Purchased", "Item Description", "From", "Troy Oz", "Cost", "Est. Premium"])
pm.append([date(2024, 4, 22), "1 oz bar", "Dealer J", 1, 2412.65, 96.40])
pm.append([date(2025, 6, 11), "1/2 oz coin", "Dealer J", 0.5, 1731.80, 88.15])

wb.save("/tmp/demo-trades.xlsx")
print("wrote /tmp/demo-trades.xlsx")
