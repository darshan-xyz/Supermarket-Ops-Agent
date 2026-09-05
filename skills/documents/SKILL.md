---
name: documents
description: >
  Use when the owner wants to generate or send a document: a bill PDF, an
  analysis deck, or any printable output. Trigger phrases: "bill PDF bhejo",
  "invoice send karo", "PDF banao", "woh bill PDF mein chahiye", "analysis
  deck banao", "is hafte ka deck", "report PDF", "send that bill as PDF",
  "make this week's analysis".
---

## WHEN to use

Activate whenever the owner:
- Wants a PDF copy of a specific bill/invoice
- Wants a daily sales document or summary/analysis deck for a date range
- Refers to "sending" or "sharing" a document from the current session

## Tool call sequence

### Daily sales document ("daily sales document", "send today's sales report", "sales file")

**IMPORTANT: File type confirmation for Daily Sales Document:**
- When the owner asks for a daily sales document or report without specifying the file type:
  Ask the user whether they want it as a **PDF** or **PPT**:
  > Would you like the daily sales document as a PDF or PPT? / உங்களுக்கு தினசரி விற்பனை ஆவணம் PDF அல்லது PPT வடிவில் வேண்டுமா? / आपको दैनिक बिक्री दस्तावेज़ PDF में चाहिए या PPT में?
  > 1. PDF (Executive Daily Sales Report)
  > 2. PPT (Presentation Deck with Charts & Stock Breakdown)
- If **PDF** is selected:
  Call `generate_sales_report_pdf(start=today, end=today)`.
- If **PPT** is selected:
  Ask in which language they want it (1. English, 2. தமிழ் / Tamil, 3. हिंदी / Hindi) if not already specified, then call `generate_analysis_deck(start=today, end=today, language=...)`.

### Generate sales report PDF ("sales report PDF", "daily sales PDF")

```
generate_sales_report_pdf(start, end)
```

Generates a branded, executive Daily Sales & Operations Report PDF with:
- Store profile header and executive KPI summary cards
- Payment mode breakdown table (Cash, UPI, Khata, Card)
- GST collection table by tax slab (0%, 5%, 12%, 18%, 28%)
- Top 10 selling products table with revenue & quantity
- Store health & inventory status (retail value, low stock, out of stock, khata debt)

### Generate invoice PDF ("woh bill PDF de do")

```
generate_invoice_pdf(bill_id)
```

**How to get `bill_id`:**
- If the owner just finalised a bill this session, the `bill_id` is in the `finalize_bill` response — use it directly.
- If the owner refers to an older bill by invoice number (e.g. "INV-00042"), search context or ask the owner to confirm the bill ID first.

The tool renders the PDF and queues it as an output file. Relay the confirmation ("Invoice INV-00042 PDF is ready") and any file-delivery message.

### Generate analysis deck ("is hafte ka deck banao", "daily sales PPT")

```
generate_analysis_deck(start, end, title?, language?)
```

`start` and `end` are `"YYYY-MM-DD"` IST strings. Convert natural language ranges:
- "daily sales PPT / today's PPT" → `start=today`, `end=today`
- "this week" → Monday–Sunday of current ISO week
- "last 7 days" → today minus 6 through today
- "last month" → first and last day of the previous calendar month

`language` is `"en"`, `"ta"`, or `"hi"`.

**IMPORTANT: Language confirmation for Daily Sales PPT:**
- When the owner asks for a daily sales PPT (e.g. "daily sales PPT", "PPT presentation", "send PPT"), first ask the user in which language they want it:
  1. English
  2. தமிழ் (Tamil)
  3. हिंदी (Hindi)
- Do NOT generate the PPT until the user tells you their choice (unless they already mentioned the language in their request).
- Once chosen, call `generate_analysis_deck(start=today, end=today, language=...)`.

## Tips

- Documents are generated server-side and delivered via the file-queue mechanism — there is no browser download link. Inform the owner the file is ready and will be sent.
- Both tools are non-destructive; generating a document does not alter any store data.
- If the owner asks for a PDF right after billing, `bill_id` is available from the just-returned `Bill.id` — do not ask the owner for it.
- Do not call `generate_analysis_deck` for a single bill; use `generate_invoice_pdf` for that.
- If the owner asks for "all today's bills as PDF", generate one invoice per bill in sequence (one `generate_invoice_pdf` call per bill_id).
