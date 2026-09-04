# Architecture & System Design Plan: Supermarket Operations Agent

**Author**: Darshan <darshanrstudy@gmail.com>
**Date**: September 4, 2026  
**Status**: Approved & Baseline Architecture  
**Target Platform**: Telegram Bot, Google Gemini API, Multi-Tenant SQLite  

---

## 1. Executive Summary & Vision

The **Supermarket Operations Agent** is an autonomous, domain-grounded conversational agent designed to manage everyday retail operations for Indian supermarkets and neighborhood Kirana stores. Operating natively through **Telegram**, the agent allows store owners and staff to manage stock intake, point-of-sale billing, customer credit ledgers (*khata*), tax compliance, and business analytics using natural everyday language (English, Hindi, Hinglish, Tamil, and Tanglish), voice notes, and shelf/receipt photos.

### Core Objectives
1. **Zero Operational Friction**: Turn voice notes and natural text messages into transactional retail records.
2. **Absolute Financial & Inventory Integrity**: Guarantee integer-paise precision, exact GST reverse-tax calculations, strict First-Expiry-First-Out (FEFO) batch consumption, and deterministic zero-oversell protections.
3. **Multi-Tenant Isolation**: Provide every supermarket with an isolated SQLite database backed by a unified control plane (`master.db`) and secure phone/OTP authentication.
4. **Autonomous Operational Intelligence**: Proactively notify owners about expiring perishables, low stock thresholds, outstanding debtor balances, and weekly visual PowerPoint performance decks.

---

## 2. System Architecture & Control Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Telegram Bot Interface                                │
│        (Text Messages, Voice Notes [audio/ogg], Photos/Barcodes [image/jpeg])    │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Telegram Gateway & Access Gatekeeper                         │
│   • Update Deduplication (update_id cache)                                       │
│   • Multi-Tenant Session Routing (telegram_chat_id -> Tenant Store)             │
│   • Interactive Onboarding Flow (Phone E.164 verification + in-chat OTP)        │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Gemini Agent Orchestration Loop                         │
│   • Google Gemini API (gemini-2.5-flash / gemini-3.6-flash via google-genai)    │
│   • Multimodal Processing: Audio voice note transcription & Photo recognition   │
│   • Multi-turn tool execution loop: Observe ──▶ Reason ──▶ Tool Call ──▶ Reply  │
│   • Session State & Conversation History persisted in SQLite                    │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     In-Process Tool Registry (31 Tools)                         │
│   ┌───────────────┬───────────────┬───────────────┬─────────────────────────┐   │
│   │   Inventory   │    Billing    │ Khata Credit  │ Analytics & Preferences │   │
│   │   (7 tools)   │   (8 tools)   │   (5 tools)   │       (9 tools)         │   │
│   └───────┬───────┴───────┬───────┴───────┬───────┴────────────┬────────────┘   │
│           │               │               │                    │                │
│           ▼               ▼               ▼                    ▼                │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │        Artifact Generators: ReportLab PDF Invoices & python-pptx Decks  │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                 Data Access Layer: Repository & Invariant Guards                │
│   • Domain Models: Money (Paise), Units (Base Conversion), GST Engine           │
│   • Transactional Invariant Guard (BEGIN IMMEDIATE, SQLite WAL mode)            │
│   • FEFO Stock Batch Allocation & Deterministic SQL-level Oversell Guard        │
└────────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                   ┌─────────────────────┴─────────────────────┐
                   ▼                                           ▼
┌──────────────────────────────────────┐    ┌──────────────────────────────────────┐
│       Control Plane: master.db       │    │    Tenant Data Plane: stores/*.db    │
│  • tenants (metadata, store_code)    │    │  • products, batches, inventory_log  │
│  • tenant_users (chat_id, roles)     │    │  • customers, khata_ledger           │
│  • auth_otps (5-min TTL, attempts)   │    │  • bills, bill_items, payments       │
│  • auth_audit (security logs)        │    │  • agent_sessions, shop_preferences  │
└──────────────────────────────────────┘    └──────────────────────────────────────┘
```

---

## 3. Layered Design Specification

### 3.1 Domain Layer (`src/kirana/domain/`)
The domain layer encapsulates pure business logic with zero external framework dependencies:
- **`Money` (`money.py`)**: Stores currency as integer paise (1 INR = 100 paise) to eliminate floating-point rounding errors. Supports arithmetic, safe division, Banker's rounding, and formatted rupee rendering (`₹1,234.50`).
- **`Units` (`units.py`)**: Handles unit normalization and conversion between bulk packaging and loose retail quantities (e.g., `kg` to `g`, `l` to `ml`, `pcs`, `packs`).
- **`TaxEngine` (`gst.py`)**: Implements Indian Goods & Services Tax (GST) calculation. Reverse-calculates taxable value from inclusive Maximum Retail Price (MRP), applies statutory GST slabs (0%, 5%, 12%, 18%, 28%), performs an equal 50/50 split between CGST and SGST, resolves odd-paise discrepancies by allocating the remainder to CGST, and calculates rupee-level rounding.
- **`Models` (`models.py`)**: Typed dataclasses representing domain entities: `Product`, `Batch`, `Customer`, `KhataEntry`, `DraftBill`, `BillItem`, and `SaleRecord`.

### 3.2 Database & Persistence Layer (`src/kirana/db/`)
- **Master Control Plane (`master.py`)**:
  - Central SQLite database at `data/master.db`.
  - Manages tenant lifecycle, store registration, phone E.164 verification, in-chat OTP generation (secure 6-digit numeric hash, 5-minute TTL, max 3 attempts), and store code routing (e.g., `KIR-61269`).
- **Tenant Data Plane (`repository.py`, `schema.sql`, `connection.py`)**:
  - Independent SQLite file per store: `data/stores/store_{store_code}.db`.
  - SQLite configured with `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`, `PRAGMA busy_timeout = 5000`, and `PRAGMA foreign_keys = ON`.
  - Transactions explicitly locked with `BEGIN IMMEDIATE` to prevent race conditions during concurrent POS checkouts and inventory intake.
- **Inventory & FEFO Allocation Invariant**:
  - Perishable products track batches with manufacturing and expiry dates.
  - Sales decrement stock using **First-Expiry-First-Out (FEFO)** order.
  - Zero-oversell protection is enforced directly in SQL:
    ```sql
    UPDATE products 
    SET stock_base = stock_base - :qty 
    WHERE id = :id AND stock_base >= :qty;
    ```
    If rows affected equals 0, the repository immediately aborts with an `InsufficientStockError`.

### 3.3 Artifact Generation Layer (`src/kirana/artifacts/`)
- **GST Tax Invoice PDF (`invoice_pdf.py`)**:
  - Built with ReportLab using clean typography, true Rupee symbol (`₹`) font fallbacks for Windows/Linux, shop header, customer details, itemized HSN table, CGST/SGST tax split table, and amount-in-words.
- **Executive Performance Deck (`analysis_deck.py`)**:
  - Built with `python-pptx`. Generates an 8-slide presentation with 4 native editable charts:
    1. Title Slide (Shop name, date period)
    2. Executive Summary (KPI summary cards)
    3. Daily Sales Trend (Category line chart)
    4. Top 5 Revenue SKUs (Horizontal bar chart)
    5. Payment Mode Breakdown (Donut/Pie chart)
    6. Category Contribution (Stacked bar chart)
    7. Inventory & Restock Alerts (Tabular summary)
    8. Recommendations & Next Steps

### 3.4 Tool Registry & Operational Surface (`src/kirana/tools/`)
The agent interacts with the store exclusively through 31 typed tools:
1. **Inventory Management (7 tools)**: `add_product`, `receive_stock`, `search_products`, `get_stock`, `list_low_stock`, `reorder_suggestions`, `list_expiring`.
2. **Billing & POS Carts (8 tools)**: `cart_add_item`, `cart_update_item`, `cart_remove_item`, `cart_view`, `cart_set_payment`, `cart_set_customer`, `cart_finalize`, `cart_cancel`.
   - Carts are stored as durable drafts in SQLite. In-flight adjustments persist across restarts. Stock decrements strictly upon `cart_finalize` with idempotency key deduplication.
3. **Khata Credit Ledger (5 tools)**: `khata_add_credit`, `khata_record_payment`, `khata_balance`, `khata_statement`, `list_khata_debtors`.
4. **Analytics & Reporting (5 tools)**: `daily_summary`, `sales_report`, `top_items`, `gst_collected`, `stock_health`.
5. **Documents (2 tools)**: `generate_invoice_pdf`, `generate_analysis_deck`.
6. **Preferences & Shop Settings (4 tools)**: `set_preference`, `get_preferences`, `set_shop_profile`, `get_shop_profile`.

### 3.5 Agentic Loop & AI Orchestration (`src/kirana/agent/`)
- **Engine**: Powered by Google Gemini API (`gemini-2.5-flash` / `gemini-3.6-flash` via `google-genai`).
- **Loop Architecture (`session.py`)**:
  - Implements an Observe-Reason-Act loop.
  - Multi-turn tool execution automatically chains up to 10 sequential calls per user turn.
  - Windows file locking handling and session persistence stored in SQLite.
- **System Prompt & Persona (`system_prompt.py`)**:
  - Strict grounding: The model is forbidden from inventing products, quantities, or prices.
  - Natural Indian retail communication: Supports English, Hindi, Hinglish, Tamil, and Tanglish.
  - Clarification over presumption: Resolves ambiguities (e.g., loose vs. branded atta) before taking action.

### 3.6 Telegram Bot, Multimodal Gateway & Scheduler (`src/kirana/telegram/`)
- **Gateway & Onboarding (`gateway.py`, `onboarding.py`)**:
  - Telegram chat ID mapping to store tenants.
  - In-chat verification flow for new store owners.
  - Role-based authorization (`owner`, `manager`, `cashier`).
- **Multimodal Support (`bot.py`)**:
  - Voice notes (`audio/ogg`) are forwarded to Gemini for transcription and intent extraction.
  - Photos/Barcodes (`image/jpeg`) are parsed via Gemini vision for stock receiving or item lookup.
- **Proactive Scheduler (`jobs.py`)**:
  - Background scheduler (APScheduler) executing automated jobs:
    - 08:30 IST: Daily low-stock and expiry alert digest.
    - 10:00 IST: Khata credit reminder and debtor summary.
    - Monday 09:00 IST: Automated weekly PowerPoint sales deck generation and delivery.

### 3.7 Vernacular & Tamil Language Support (`tests/test_tamil.py`)
- Tamil script normalization and Tanglish phonetic alias matching.
- Catalog alias resolution with Levenshtein fuzzy distance matching, enabling Tamil queries like:
  - *"5 பாக்கெட் ஆச்சி மிளகாய் தூள் பில் போடு"* (5 packets Aachi chilli powder bill)
  - *"paal packet stock evlo irukku?"* (How much milk packet stock is left?)

---

## 4. Technical Invariants ("The Hard Parts")

| Invariant | Problem Solved | Enforcement Mechanism |
|---|---|---|
| **Exact Integer Accounting** | Floating-point inaccuracies in currency math | Integer paise (`Money` class), zero float operations in DB. |
| **Zero-Oversell Guarantee** | Race conditions selling more stock than available | SQL-level atomic constraint: `WHERE stock_base >= :qty`. |
| **FEFO Inventory Rotation** | Spoilage of perishables | Repository draws down stock from nearest expiry batches first. |
| **Idempotent Billing** | Network retries creating duplicate invoices | `idempotency_key` unique index on `bills`. Duplicate calls return existing invoice. |
| **Concurrent Safety** | Multiple users billing simultaneously | SQLite WAL mode with `BEGIN IMMEDIATE` serializable write locks. |
| **Strict Data Grounding** | LLM hallucinating prices or products | Live DB tool calls required for all catalog/inventory queries. |

---

## 5. Phased Implementation Roadmap

1. **Milestone 1: Project Scaffolding & Architecture Planning**  
   Repository setup, dependencies, configuration, and baseline system architecture documentation.
2. **Milestone 2: Domain Layer & Value Objects**  
   Money, Units, GST TaxEngine, and pure domain models with unit test coverage.
3. **Milestone 3: Database & Transactional Repository**  
   SQLite schema, connection management, FEFO batch allocation, and concurrency safeguards.
4. **Milestone 4: Artifact Generation Engines**  
   ReportLab GST PDF invoice and python-pptx executive presentation deck generators.
5. **Milestone 5: Operational Tool Suite & Declarative Skills**  
   31 domain tools across inventory, billing, khata, analytics, documents, and preferences.
6. **Milestone 6: Gemini Agentic Loop & Session Persistence**  
   Google Gemini integration, multi-turn tool execution loop, and resilient SQLite session state.
7. **Milestone 7: Multi-Tenant Master DB & Onboarding Gateway**  
   Master control plane, phone verification, in-chat OTP authentication, and tenant isolation.
8. **Milestone 8: Telegram Bot, Multimodal Perception & Background Jobs**  
   Telegram long-polling adapter, voice/photo handling, and APScheduler proactive alerts.
9. **Milestone 9: Vernacular NLP & Tamil/Tanglish Support**  
   Tamil phonetic matching, alias normalization, and multilingual dialog tests.
10. **Milestone 10: E2E Integration Suite & Simulation Scripts**  
    Comprehensive production tests, supermarket database seeder, and interactive demo scripts.
11. **Milestone 11: Production Documentation & Operations Manual**  
    Comprehensive README, operational guidelines, and deployment configuration.
