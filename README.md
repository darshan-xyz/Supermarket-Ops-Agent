# 🛒 Supermarket Operations Agent

An autonomous conversational agent designed to manage an Indian supermarket / kirana store entirely through **Telegram**. Operating in natural English, Hindi, Hinglish, and Tamil, the agent manages stock receipts, cuts GST-compliant invoices, maintains the customer credit ledger (khata), compiles daily business closings, and produces presentation decks and PDF receipts on demand.

> **Live Telegram Bot**: Message **[@Marketopsagentbot](https://t.me/Marketopsagentbot)** on Telegram to test real interactions live, or explore locally via `scripts/sim_chat.py`.

---

## Capabilities Overview

The agent handles store operations directly from conversational requests without predefined command menus:

| Domain | Natural Language Request Example | Core Operations |
|---|---|---|
| **Inventory Restock** | *"50 packets of Maggi came in, cost ₹12, MRP ₹14"* | Stock intake, cost/MRP adjustment, batch creation |
| **Catalog Expansion** | *"new item: Amul Butter 100g, GST 12%, MRP ₹62"* | SKU creation with HSN code and tax slab |
| **Point-of-Sale Billing** | *"make a bill: 2kg sugar, 1 Aashirvaad atta 5kg, 4 Maggi, 1 Amul butter, UPI"* | Multi-item cart building with live GST tax split |
| **In-Flight Bill Edits** | *"drop the butter, make it 6 Maggi"* | Add, update quantities, remove items dynamically |
| **Stock Inquiries** | *"how much sugar is left?"* · *"what's running out?"* | Stock queries, low-stock alerts, velocity-based reorder |
| **Credit Ledger (Khata)** | *"put ₹500 on Ramesh's credit"* · *"Ramesh paid ₹300"* | Customer credit tabs, payment tracking, balance inquiries |
| **Day Closing & Reports** | *"today's sales?"* · *"close the day"* | Gross revenue, tax breakup, payment mode breakdown |
| **Invoice Documents** | *"send me that bill as a PDF"* | Branded tax invoice PDF with per-line HSN & CGST/SGST |
| **Business Analysis** | *"make this week's sales analysis deck"* | 8-slide PowerPoint deck with 4 native editable charts |
| **Persistent Preferences** | *"always assume UPI unless I say cash"* | Durable shop settings retained across `/new` chats |

When an order is ambiguous (e.g. *"add atta"* when both packaged and loose atta exist), the agent actively clarifies candidate choices rather than assuming.

---

## Agent Architecture & Control Loop

Built on the **Google Gemini API** (`gemini-3.6-flash` via `google-genai`), the system uses model-orchestrated tool dispatch where every action passes through an observe-reason-act cycle:

```
Telegram Message ──▶ Deduplication (update_id) ──▶ session.run_turn(chat_id, text)
                                                         │
                                                         ▼
                                 Gemini Agent Loop: Observe ──▶ Reason ──▶ Act (Tool Call)
                                                         │
                                                         ▼
                                   tools/*.py (31 in-process typed adapters)
                                                         │
                                                         ▼
                                 db/repository.py (Transactional Invariant Guard)
                                                         │
                                                         ▼
                                 SQLite Backend (WAL Mode + BEGIN IMMEDIATE)
                                                         │
                                                         ▼
                                 Direct Reply + Queued Attachments (PDF / PPTX)
```

One inbound message triggers a turn that can chain multiple domain tool calls before generating the final reply. Session history persists in SQLite, surviving server restarts.

---

## Tool Surface (31 Tools Across 6 Domains)

Tools serve as thin adapters that translate agent parameters into transactional database operations:

* **`inventory`** (7 tools): `add_product`, `receive_stock`, `search_products`, `get_stock`, `list_low_stock`, `reorder_suggestions`, `list_expiring`
* **`billing`** (8 tools): `cart_add_item`, `cart_update_item`, `cart_remove_item`, `cart_view`, `cart_set_payment`, `cart_set_customer`, `cart_finalize`, `cart_cancel`
* **`khata`** (5 tools): `khata_add_credit`, `khata_record_payment`, `khata_balance`, `khata_statement`, `list_khata_debtors`
* **`analytics`** (5 tools): `daily_summary`, `sales_report`, `top_items`, `gst_collected`, `stock_health`
* **`documents`** (2 tools): `generate_invoice_pdf`, `generate_analysis_deck`
* **`preferences`** (4 tools): `set_preference`, `get_preferences`, `set_shop_profile`, `get_shop_profile`

## Technical Invariants & Solutions ("The Hard Parts")

1. **Strict Data Grounding**: Product catalog, prices, GST slabs, and stock quantities are fetched live through tools. The system prompt strictly prohibits fabricating numbers or SKUs.
2. **Deterministic Oversell Guard**: Enforced at the SQL query level (`UPDATE products SET stock_base = stock_base - :q WHERE id = :id AND stock_base >= :q`). Refused at the tool layer, never relying on prompt compliance.
3. **Exact GST Accounting**: `domain/gst.py` reverse-calculates taxable value from inclusive retail price, performs equal CGST/SGST split, allocates odd paise to CGST for exact penny reconciliation, and computes rupee rounding. All monetary values are integer paise.
4. **Persistent Draft Bills**: Unfinalized carts are stored as `draft` bills in SQLite. Mid-build additions, edits, and cancellations persist across restarts. Stock decrements **only on `cart_finalize`**.
5. **Transport & Finalize Idempotency**: Telegram redeliveries are filtered via `processed_updates(update_id)`. Duplicate `cart_finalize` calls resolve via unique `idempotency_key`, returning the existing invoice with zero double-deduction.
6. **Concurrency Protection**: SQLite operates in WAL mode with `BEGIN IMMEDIATE` transaction locking, ensuring zero stock corruption during simultaneous billing or restocking.
7. **Operational Guardrails**: Refuses below-cost sales or credit overpayments without explicit owner confirmation; unknown customer repayments are rejected; no hard deletions of products.
8. **Real Document Artifacts**: Invoices generated as clean, branded PDFs via ReportLab. Store analytics generated as 8-slide PowerPoint decks with 4 native editable python-pptx charts.
9. **Cross-Session Persistence**: Store memory, preferences, and shop identity are stored in SQLite outside conversational context. `/new` clears chat turns but preserves store settings.

---

## Stretch Capabilities Implemented (§7)

| Stretch Feature | Architecture & Implementation |
|---|---|
| **Branded Invoice PDFs** | Generates formal GST tax invoice with shop identity, HSN breakdown, amount-in-words, and ₹ symbol (`src/kirana/artifacts/invoice_pdf.py`). |
| **Automated Weekly Deck** | Scheduled `JobQueue` job delivers a weekly PPTX sales analysis deck every Monday at 09:00 IST (`src/kirana/telegram/jobs.py`). |
| **Khata Payment Reminders** | Daily scheduled task sends outstanding debtor summaries at 10:00 IST without LLM overhead (`src/kirana/telegram/jobs.py`). |
| **Velocity-Based Reordering** | Computes 7-day movement velocity to project cover days and recommend restock quantities (`src/kirana/tools/inventory.py:reorder_suggestions`). |
| **Expiry Tracking & FEFO** | Perishable batches track expiry dates; sales draw down oldest inventory first via First-Expiry-First-Out (`src/kirana/db/repository.py`). |
| **Voice-Note Ordering** | Telegram voice notes (`audio/ogg`) are forwarded directly to the Gemini multimodal API for transcription and billing (`src/kirana/telegram/bot.py`). |
| **Photo & Barcode Recognition** | Product photos and barcodes (`image/jpeg`) sent via Telegram are identified using Gemini vision (`src/kirana/telegram/bot.py`). |
| **Multi-Lingual Interaction** | Understands and answers naturally in English, Hindi, Hinglish, Tamil, and Tanglish (`src/kirana/agent/system_prompt.py`). |

---

## Quickstart & Setup

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Configure environment (.env)
cp .env.example .env
# Set GEMINI_API_KEY (from https://aistudio.google.com/) and TELEGRAM_BOT_TOKEN

# 3. Seed sample kirana store with 7-day sales history
uv run python scripts/seed.py --reset --with-history

# 4a. Run the Telegram bot (long polling)
uv run python -m kirana.telegram.bot

# 4b. Or run local interactive chat without Telegram
uv run python scripts/sim_chat.py

# 4c. Run the end-to-end automated demo script
uv run python scripts/demo.py
```

---

## Project Structure

```
src/kirana/
  domain/      money.py · units.py · gst.py · models.py     # Pure business logic, integer paise
  db/          schema.sql · connection.py · repository.py    # SQLite WAL transactional storage & FEFO
  tools/       inventory · billing · khata · analytics · documents · preferences + registry, context, _helpers
  artifacts/   invoice_pdf.py (ReportLab) · analysis_deck.py (python-pptx native charts)
  agent/       system_prompt.py · session.py                 # Gemini tool-loop & SQLite session persistence
  telegram/    bot.py (long-polling adapter) · jobs.py       # Scheduled deck & khata reminders
skills/        domain playbooks (billing, stock, khata, analytics, documents, memory)
scripts/       seed.py · demo.py · sim_chat.py
tests/         67 tests — GST, units, money, oversell, idempotency, concurrency, khata, artifacts, FEFO, tools
```

---

## Verification

```bash
# Run test suite (67 passed in ~18s)
uv run pytest -q

# Run code linter (0 errors)
uv run ruff check src/ tests/ scripts/
```

---

## Submission Info

* **Telegram Bot Handle**: [@Marketopsagentbot](https://t.me/Marketopsagentbot)
* **Harness**: Google Gemini API (`gemini-3.6-flash` via `google-genai` v2.22.0)
* **Collaborator Access**: Add `Aswath363`, `akshaiP`, and `ashwanthnebula` on GitHub.

