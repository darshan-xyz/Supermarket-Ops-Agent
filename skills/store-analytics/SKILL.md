---
name: store-analytics
description: >
  Use when the owner asks about sales, revenue, stock health, GST, or wants
  to close/review the day. Trigger phrases: "aaj kitna hua", "today's sales",
  "innaiku sales evvalavu", "innaiku vilpanai", "din band karo", "close the day",
  "is hafte ka report", "inda vaaram report", "weekly report",
  "kya khatam ho raha hai", "stock health", "GST kitna bana", "top items",
  "best selling", "pichle 7 din", "last month report", "kitna kamaya", "இன்றைய விற்பனை எவ்வளவு".
---

## WHEN to use

Activate whenever the owner asks about:
- Today's revenue, bill count, or a summary of the day
- Sales over a date range (week, month, custom)
- Which items sold the most
- GST collected in a period
- Which items are running low or out of stock

## Tool call sequence

### Today's summary ("aaj kitna hua", "close the day", "today's sales")

```
daily_summary()           # ALWAYS call with NO arguments for today's sales
daily_summary(date)       # date = "YYYY-MM-DD" ONLY for a specific past day
```

IMPORTANT: When the owner asks for today's sales, invoke `daily_summary()` with NO arguments. Do NOT pass a date argument for today (the tool automatically uses today's local IST date from the database).

Returns: total sales, number of bills, cash/UPI/credit split, top items. Always show at end of business day when owner says "band karo".

### Sales report for a date range

```
sales_report(start, end)
```

`start` and `end` are `"YYYY-MM-DD"` strings in IST. Convert phrases like "this week" to the appropriate Monday–Sunday range. "Last 7 days" = today minus 6 days through today.

### Top-selling items

```
top_items(start, end, limit?)
```

Default `limit` is 10. Call after `sales_report` if the owner also wants to know what sold most.

### GST collected

```
gst_collected(start, end)
```

Returns CGST, SGST, and IGST totals by slab. Use for monthly GST filing prep.

### Stock health

```
stock_health()
```

Returns a categorised view: out-of-stock, below reorder point, healthy. Complements `list_low_stock` (which the receiving-stock skill uses for targeted queries).

## Combining calls

For a comprehensive "close the day" response, call in parallel where possible:
1. `daily_summary()` — revenue and bill count
2. `stock_health()` — items needing attention
3. `list_khata_debtors()` (from khata tools) — outstanding credit (optional, if owner asks)

## Tips

- Dates are always IST. Do not use UTC.
- "This week" = current ISO week (Monday–Sunday). "Last week" = the previous complete ISO week.
- For monthly GST, use the first and last day of the calendar month.
- Surface rupee values formatted (₹1,234.50), not raw paise.
- Do not compute revenue totals yourself; relay exactly what the repo returns.
- For "what's expiring / about to spoil", use `list_expiring` (in the receiving-stock skill).
