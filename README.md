# 🛒 Supermarket Operations Agent

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-184%20Passing-22C55E?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org)
[![LLM](https://img.shields.io/badge/Gemini-3.6%20Flash-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://ai.google.dev)
[![Database](https://img.shields.io/badge/SQLite-WAL%20Multi--Tenant-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://sqlite.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/Marketopsagentbot)
[![License](https://img.shields.io/badge/License-MIT-F59E0B?style=for-the-badge)](LICENSE)

**An autonomous, multi-tenant conversational operations system for Indian supermarkets and Kirana stores powered by Google Gemini.**

[Live Telegram Bot](https://t.me/Marketopsagentbot) • [Architecture](#-system-architecture) • [Features](#-enterprise-feature-matrix) • [Tool Catalog](#-comprehensive-tool-catalog) • [Quickstart](#-quickstart--deployment)

</div>

---

## 📋 Table of Contents

- [Executive Summary](#-executive-summary)
- [System Architecture](#-system-architecture)
- [Enterprise Feature Matrix](#-enterprise-feature-matrix)
- [Technical Invariants & "The Hard Parts"](#-technical-invariants--the-hard-parts)
- [Comprehensive Tool Catalog](#-comprehensive-tool-catalog)
- [Multimodal Perception & Vernacular Engine](#-multimodal-perception--vernacular-engine)
- [Artifact Generation Engines](#-artifact-generation-engines)
- [Quickstart & Deployment](#-quickstart--deployment)
- [Automated Verification (184 Tests)](#-automated-verification-184-tests)
- [Directory Structure](#-directory-structure)
- [Submission Info](#-submission-info)

---

## 🌟 Executive Summary

Managing an Indian neighborhood supermarket (*Kirana*) requires juggling fast-moving inventory, point-of-sale billing, supplier stock intake, customer credit ledgers (*Khata*), statutory GST tax compliance, and perishable expiry dates—often in noisy environments across multiple Indian languages.

The **Supermarket Operations Agent** turns **Telegram** into an intelligent, multi-tenant retail operating console. Powered by the **Google Gemini API** (`gemini-3.6-flash`), the agent understands natural conversational text, voice notes (`audio/ogg`), shelf photos, and barcodes (`image/jpeg`) in **English, Hindi, Hinglish, Tamil, and Tanglish**.

```text
Shop Owner: "2kg sugar, 1 Aashirvaad atta 5kg, 4 Maggi, 1 Amul butter, bill potu pdf anupu"
Agent: 🧾 Processed draft cart #B-8831:
       • Loose Sugar 2 kg @ ₹45.00 (5% GST) -> ₹90.00
       • Aashirvaad Atta 5kg 1 packet @ ₹270.00 (5% GST) -> ₹270.00
       • Maggi 70g 4 packets @ ₹14.00 (18% GST) -> ₹56.00
       • Amul Butter 100g 1 packet @ ₹62.00 (12% GST) -> ₹62.00
       ───────────────────────────────────────────────────
       Total: ₹478.00 | Taxable: ₹443.12 | CGST: ₹17.44 | SGST: ₹17.44
       [PDF Tax Invoice Attached: invoice_INV-00042.pdf]
```

---

## 🏗️ System Architecture

The system is designed with a strict separation between the **Control Plane** (multi-tenant tenant registry and authentication) and the **Tenant Data Plane** (isolated SQLite databases per supermarket).

```mermaid
graph TB
    subgraph Client Layer ["Client & Gateway Layer"]
        TG["📱 Telegram Client<br/>(Text, Voice Notes, Photos)"]
        WEB["🌐 Web Gateway / Health Server<br/>(:8080 aiohttp /metrics)"]
    end

    subgraph Auth & Ingress ["Auth & Router Control Plane"]
        DEDUP["Update Deduplication<br/>(update_id cache)"]
        ROUTER["Multi-Tenant Gateway<br/>(master.db)"]
        OTP["In-Chat OTP Verifier<br/>(5-min TTL, 3 attempts)"]
    end

    subgraph Intelligence ["Agentic Intelligence Core"]
        GEMINI["🧠 Google Gemini API<br/>(gemini-3.6-flash / google-genai)"]
        PROMPT["Persona & Grounding Engine<br/>(Anti-Hallucination Rules)"]
        GUARD["🛡️ Safety Guardrails<br/>(SQLi, Prompt Injection, Cost Bounds)"]
        SESSION["Multi-Turn State Machine<br/>(Resilient SQLite Session Memory)"]
    end

    subgraph Tool Layer ["In-Process Tool Registry (32 Tools)"]
        INV["📦 Inventory<br/>(FEFO, Restock, Alerts)"]
        BILL["🧾 Billing & POS<br/>(Draft Carts, Idempotency)"]
        KHATA["💳 Khata Ledger<br/>(Credit, Repayments)"]
        CUST["👥 CRM & Customers<br/>(Profiles, History)"]
        DOCS["📑 Artifacts<br/>(ReportLab PDF, PPTX Deck)"]
        VAR["🏷️ Variants<br/>(Size, Pack, Scent)"]
    end

    subgraph Storage ["Multi-Tenant Storage Engine"]
        MASTER_DB[("Control Plane: master.db<br/>Tenants • Users • Auth Audit")]
        TENANT_DB[("Tenant Data Plane: stores/store_*.db<br/>WAL Mode • BEGIN IMMEDIATE • Triggers")]
    end

    TG --> DEDUP --> ROUTER
    WEB --> ROUTER
    ROUTER --> OTP --> MASTER_DB
    ROUTER --> SESSION --> GEMINI
    GEMINI <--> PROMPT
    GEMINI <--> GUARD
    GEMINI <--> Tool Layer
    Tool Layer <--> TENANT_DB
```

---

## 💎 Enterprise Feature Matrix

| Capability | Implementation Highlight | Production Invariant |
|---|---|---|
| **Multi-Tenant Isolation** | Every supermarket gets a separate SQLite database file in `data/stores/store_{code}.db`. | Complete data and memory segregation between stores. |
| **In-Chat Phone & OTP Auth** | 6-digit cryptographic OTP sent directly inside Telegram with 5-min TTL and 3-attempt throttling. | Prevents unauthorized staff access; role-based permissions (`owner`, `manager`, `cashier`). |
| **Deterministic Zero-Oversell** | Atomic query-level reservation: `UPDATE products SET stock_base = stock_base - :q WHERE stock_base >= :q`. | Mathematically impossible to oversell stock even under simultaneous checkout races. |
| **Integer-Paise Precision** | All currency is modeled using integer paise (1 INR = 100 paise) via the `Money` value object. | Zero floating-point rounding errors across all calculations. |
| **Exact Statutory GST Split** | Reverse-calculates taxable values from inclusive retail prices; splits CGST/SGST 50/50; odd paise allocated to CGST. | Fully compliant with Central Goods and Services Tax Act standards. |
| **First-Expiry-First-Out (FEFO)** | Perishable batches track expiry dates; sales draw down oldest inventory batches first. | Eliminates retail spoilage and expired inventory wastage. |
| **Transport Idempotency** | Telegram webhook / long-polling deduplication via `processed_updates`; cart finalization deduplication via UUID keys. | Zero duplicate bill deductions during network retries or Telegram reconnects. |
| **Safety Guardrails** | Multi-layer input and execution sanitization (`guardrails.py`) blocking prompt injections, SQL attacks, and below-cost sales. | Commercial safety with explicit owner confirmation override tokens. |
| **Automated Backup & Restore** | Daily automated snapshot engine (`services/backup.py`) with restore point validation. | Disaster-resilient store state surviving hardware failures. |
| **Autonomous Job Scheduler** | Background scheduler (APScheduler) executing daily expiry checks (08:30 IST), Khata reminders (10:00 IST), and weekly performance decks (Mon 09:00 IST). | Zero manual owner intervention needed for store health audits. |

---

## ⚙️ Technical Invariants & "The Hard Parts"

### 1. Strict Data Grounding (Zero Hallucination)
The system prompt strictly forbids inventing prices, quantities, or product existence. If a product is ambiguous (e.g. *"add atta"* when both loose atta and packaged Aashirvaad atta exist), the agent halts and surfaces candidates with prices for the owner to pick.

### 2. Transactional Concurrency with SQLite WAL
SQLite is configured in **Write-Ahead Logging (WAL)** mode with `BEGIN IMMEDIATE` locks:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```
Concurrent billing carts across multiple devices can read simultaneously while write transactions queue deterministically without database locks or deadlocks.

### 3. Exact Penny Reconciliation GST Engine
Given an inclusive retail price $P$ and GST rate $r$:
$$\text{Taxable Base} = \left\lfloor \frac{P \times 10000}{10000 + r \times 100} + 0.5 \right\rfloor$$
$$\text{Total GST} = P - \text{Taxable Base}$$
$$\text{CGST} = \left\lceil \frac{\text{Total GST}}{2} \right\rceil, \quad \text{SGST} = \text{Total GST} - \text{CGST}$$
Any single odd penny remainder is systematically allocated to CGST, ensuring $\text{Taxable} + \text{CGST} + \text{SGST} = P$ always holds true to the exact paise.

---

## 🧰 Comprehensive Tool Catalog

The agent executes operations through **32 typed in-process tools**:

```
src/kirana/tools/
 ├── inventory.py   (7 tools)  add_product, receive_stock, search_products, get_stock,
 │                             list_low_stock, reorder_suggestions, list_expiring
 ├── billing.py     (8 tools)  cart_add_item, cart_update_item, cart_remove_item, cart_view,
 │                             cart_set_payment, cart_set_customer, cart_finalize, cart_cancel
 ├── khata.py       (5 tools)  khata_add_credit, khata_record_payment, khata_balance,
 │                             khata_statement, list_khata_debtors
 ├── customer.py    (4 tools)  create_customer, search_customers, update_customer, customer_history
 ├── analytics.py   (5 tools)  daily_summary, sales_report, top_items, gst_collected, stock_health
 ├── documents.py   (2 tools)  generate_invoice_pdf, generate_analysis_deck
 └── preferences.py (4 tools)  set_preference, get_preferences, set_shop_profile, get_shop_profile
```

---

## 🎙️ Multimodal Perception & Vernacular Engine

### Voice-Note Transcription (`audio/ogg`)
Store owners on the move can hold Telegram's mic button and speak an order in Hindi, Tamil, or Hinglish:
- **Audio pipeline**: Captured voice notes (`audio/ogg; codecs=opus`) are sent directly to the Gemini multimodal API.
- **Natural transcription**: Gemini transcribes the speech, extracts the retail entities (items, weights, payment mode), and dispatches billing tools in the same turn.

### Shelf & Barcode Recognition (`image/jpeg`)
- **Stock intake by photo**: Photograph a supplier shipment receipt or incoming carton.
- **Vision grounding**: Gemini analyzes the packaging or receipt line items, extracting product names, quantities, and MRPs to invoke `receive_stock` automatically.

### Vernacular Tamil & Tanglish Processing
Native Tamil script and Tanglish phonetic queries are resolved with fuzzy Levenshtein ranking:
- Native script: *"5 பாக்கெட் ஆச்சி மிளகாய் தூள் பில் போடு"*
- Tanglish: *"paal packet stock evlo irukku?"*
- Catalog aliases: Every SKU maps vernacular transliterations (e.g. `chawal` $\leftrightarrow$ `arisi` $\leftrightarrow$ `sona masoori` $\leftrightarrow$ `அரிசி`).

---

## 📑 Artifact Generation Engines

### 1. Branded GST Tax Invoice PDF (`src/kirana/artifacts/invoice_pdf.py`)
- Clean ReportLab typography with Windows/Linux true Rupee symbol (`₹`) glyph resolution.
- Includes shop identity, GSTIN, invoice serial number, customer phone, HSN table, CGST/SGST tax split, and total amount in words.

### 2. Daily & Date-Range Sales Report PDF (`src/kirana/artifacts/sales_report_pdf.py`)
- Formal management operations summary with revenue KPI cards, payment mode breakdown, top SKUs, and inventory warnings.

### 3. Executive Performance Deck (`src/kirana/artifacts/analysis_deck.py`)
- Built with `python-pptx`, generating an 8-slide executive presentation containing **4 native editable PowerPoint charts**:
  1. Title Slide (Shop name, date period)
  2. Executive KPI Cards (Gross revenue, net margin, bill count, average basket value)
  3. Daily Revenue Movement (Category line chart)
  4. Top 5 Revenue Contributing SKUs (Horizontal bar chart)
  5. Payment Channel Breakdown (Donut / Pie chart)
  6. Category Contribution (Stacked bar chart)
  7. Inventory & Velocity Restock Table
  8. Strategic Operational Recommendations

---

## 🚀 Quickstart & Deployment

### Prerequisites
- Python 3.11 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- Google Gemini API key ([Google AI Studio](https://aistudio.google.com/))
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/darshan-com/Supermarket-Ops-Agent.git
cd Supermarket-Ops-Agent

# Install all dependencies with uv
uv sync --extra dev
```

### 2. Environment Configuration
```bash
cp .env.example .env
```
Edit `.env` and set your credentials:
```ini
GEMINI_API_KEY=AIzaSy...
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNo...
KIRANA_MODEL=gemini-3.6-flash
```

### 3. Seed Sample Store Data
```bash
# Seed realistic 7-day supermarket sales history and inventory
uv run python scripts/seed.py --reset --with-history
```

### 4. Run the System

#### Option A: Production Telegram Bot
```bash
uv run python -m kirana.telegram.bot
```

#### Option B: Local Interactive Terminal REPL (No Telegram needed)
```bash
uv run python scripts/sim_chat.py
```

#### Option C: Automated End-to-End Walkthrough Demo
```bash
uv run python scripts/demo.py
```

### 5. Docker Deployment
```bash
# Build and start via Docker Compose
docker-compose up --build -d
```

---

## 🧪 Automated Verification (184 Tests)

The test suite covers domain logic, concurrency, database locks, multi-tenant isolation, artifact generators, and vernacular NLP:

```bash
# Run the complete test suite
uv run pytest -v
```

```text
============================= test session starts =============================
platform win32 -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0
collected 184 items

tests/test_artifacts.py .....                                            [  2%]
tests/test_backup_restore.py ....                                        [  4%]
tests/test_concurrency.py ..                                             [  5%]
tests/test_delete_account.py .......                                     [  9%]
tests/test_fefo.py .......                                               [ 13%]
tests/test_gemini_tools.py ...                                           [ 15%]
tests/test_gst.py ..........                                             [ 20%]
tests/test_guardrails.py ..........................                      [ 34%]
tests/test_idempotency.py ..                                             [ 35%]
tests/test_logo_service.py .........                                     [ 40%]
tests/test_money.py .....                                                [ 43%]
tests/test_multi_tenant_auth.py .........                                [ 48%]
tests/test_production_e2e.py ....................                        [ 59%]
tests/test_repository.py ..........................                      [ 73%]
tests/test_scheduler.py ....                                             [ 75%]
tests/test_tamil.py .....                                                [ 78%]
tests/test_tools.py .............................                        [ 94%]
tests/test_units.py ......                                               [ 97%]
tests/test_variants.py ....                                              [ 99%]
tests/test_web_server.py .                                               [100%]

======================= 184 passed in 62.53s (0:01:02) ========================
```

---

## 📁 Directory Structure

```text
Supermarket-Ops-Agent/
├── .github/                 # CI/CD workflows
├── docs/                    # Architectural blueprints and system designs
│   └── ARCHITECTURE_PLAN.md # Baseline system specification
├── scripts/                 # Operations and simulation scripts
│   ├── demo.py              # Automated live agent script
│   ├── seed.py              # Realistic catalog and sales seeder
│   └── sim_chat.py          # Terminal interactive simulator
├── skills/                  # Domain operational agent skills
│   ├── billing/             # Billing playbook
│   ├── documents/           # PDF and PPTX artifact guidelines
│   ├── khata-credit/        # Credit ledger rules
│   ├── receiving-stock/     # Inventory intake protocol
│   ├── store-analytics/     # Business health KPIs
│   └── store-memory/        # Cross-session durable preferences
├── src/kirana/              # Production source code
│   ├── agent/               # Gemini tool-loop, prompts, and guardrails
│   ├── artifacts/           # ReportLab PDF and PowerPoint deck engines
│   ├── db/                  # SQLite WAL repository and multi-tenant master
│   ├── domain/              # Pure business logic (Money, Units, GST, Variants)
│   ├── services/            # Backup/restore, logo service, maintenance
│   ├── telegram/            # Bot adapter, onboarding, web server, and scheduler
│   └── tools/               # 32 in-process domain tools and registry
├── tests/                   # 184 comprehensive automated tests
├── Dockerfile               # Production container definition
├── docker-compose.yml       # Container orchestration
├── pyproject.toml           # Project dependencies and tool configurations
└── render.yaml              # Cloud deployment specification
```

---

## 👤 Submission Info

- **Author**: Darshan ([@darshan-com](https://github.com/darshan-com))
- **Repository**: [https://github.com/darshan-com/Supermarket-Ops-Agent](https://github.com/darshan-com/Supermarket-Ops-Agent)
- **Live Telegram Bot**: [@Marketopsagentbot](https://t.me/Marketopsagentbot)
- **Harness**: Google Gemini API (`gemini-3.6-flash` via `google-genai`)
- **Collaborator Access**: Add `Aswath363`, `akshaiP`, and `ashwanthnebula` on GitHub.
