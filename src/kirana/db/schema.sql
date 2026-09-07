-- Kirana Ops Agent schema. All money in integer paise; quantities in integer
-- base units (display_qty * 1000). WAL for concurrent readers + a single writer.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- Catalog ----------
CREATE TABLE IF NOT EXISTS products (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  brand         TEXT,
  aliases       TEXT NOT NULL DEFAULT '',
  sku_code      TEXT UNIQUE,
  hsn_code      TEXT,
  unit          TEXT NOT NULL,
  is_loose      INTEGER NOT NULL DEFAULT 0,
  gst_rate      INTEGER NOT NULL DEFAULT 0,
  cost_paise    INTEGER NOT NULL DEFAULT 0,
  mrp_paise     INTEGER NOT NULL DEFAULT 0,
  sell_paise    INTEGER NOT NULL DEFAULT 0,
  stock_base    INTEGER NOT NULL DEFAULT 0,
  reorder_base  INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  CHECK (stock_base >= 0)
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);

CREATE TABLE IF NOT EXISTS stock_movements (
  id          INTEGER PRIMARY KEY,
  product_id  INTEGER NOT NULL REFERENCES products(id),
  delta_base  INTEGER NOT NULL,
  reason      TEXT NOT NULL,          -- stock_in | sale | adjustment | void_reversal
  ref_type    TEXT,
  ref_id      INTEGER,
  cost_paise  INTEGER,
  note        TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_product ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_created ON stock_movements(created_at);

-- Stock batches for expiry / FEFO. Each stock-in is a batch; sales consume the
-- earliest-expiry batch first. SUM(batches.qty_base) is kept equal to
-- products.stock_base (which remains the authoritative oversell guard).
CREATE TABLE IF NOT EXISTS batches (
  id          INTEGER PRIMARY KEY,
  product_id  INTEGER NOT NULL REFERENCES products(id),
  qty_base    INTEGER NOT NULL,       -- remaining quantity in this batch
  expiry      TEXT,                   -- 'YYYY-MM-DD' or NULL (non-perishable)
  cost_paise  INTEGER,
  note        TEXT,
  created_at  TEXT NOT NULL,
  CHECK (qty_base >= 0)
);
CREATE INDEX IF NOT EXISTS idx_batches_fefo ON batches(product_id, expiry);

-- ---------- Customers ----------
CREATE TABLE IF NOT EXISTS customers (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  phone       TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (name COLLATE NOCASE)
);

-- ---------- Bills (draft == cart; finalized == invoice) ----------
CREATE TABLE IF NOT EXISTS bills (
  id               INTEGER PRIMARY KEY,
  chat_id          INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'draft',   -- draft | finalized | void
  invoice_no       TEXT UNIQUE,
  customer_id      INTEGER REFERENCES customers(id),
  customer_phone   TEXT,
  billed_by        TEXT,
  payment_mode     TEXT,                            -- cash | upi | card | khata
  payment_ref      TEXT,
  subtotal_paise   INTEGER NOT NULL DEFAULT 0,
  cgst_paise       INTEGER NOT NULL DEFAULT 0,
  sgst_paise       INTEGER NOT NULL DEFAULT 0,
  round_off_paise  INTEGER NOT NULL DEFAULT 0,
  total_paise      INTEGER NOT NULL DEFAULT 0,
  idempotency_key  TEXT UNIQUE,
  daily_seq        INTEGER DEFAULT 0,
  created_at       TEXT NOT NULL,
  finalized_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bills_chat_status ON bills(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_bills_finalized ON bills(finalized_at);
CREATE INDEX IF NOT EXISTS idx_bills_customer ON bills(customer_id);

CREATE TABLE IF NOT EXISTS bill_items (
  id                INTEGER PRIMARY KEY,
  bill_id           INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  product_id        INTEGER NOT NULL REFERENCES products(id),
  name_snapshot     TEXT NOT NULL,
  hsn_snapshot      TEXT,
  qty_base          INTEGER NOT NULL,
  unit              TEXT NOT NULL,
  unit_sell_paise   INTEGER NOT NULL,
  gst_rate          INTEGER NOT NULL,
  taxable_paise     INTEGER NOT NULL,
  cgst_paise        INTEGER NOT NULL,
  sgst_paise        INTEGER NOT NULL,
  line_total_paise  INTEGER NOT NULL,
  UNIQUE (bill_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);

-- ---------- Khata (credit ledger) ----------
CREATE TABLE IF NOT EXISTS khata_entries (
  id           INTEGER PRIMARY KEY,
  customer_id  INTEGER NOT NULL REFERENCES customers(id),
  entry_type   TEXT NOT NULL,                        -- debit (credit given) | credit (payment)
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  note         TEXT,
  bill_id      INTEGER REFERENCES bills(id),
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_khata_customer ON khata_entries(customer_id);

-- ---------- Durable memory ----------
CREATE TABLE IF NOT EXISTS preferences (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_profile (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  name           TEXT,
  gstin          TEXT,
  address        TEXT,
  phone          TEXT,
  state_code     TEXT NOT NULL DEFAULT '29',
  invoice_prefix TEXT NOT NULL DEFAULT 'INV',
  logo_path      TEXT,
  updated_at     TEXT
);


-- ---------- Transport idempotency / sessions ----------
CREATE TABLE IF NOT EXISTS processed_updates (
  update_id  INTEGER PRIMARY KEY,
  chat_id    INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
  chat_id    INTEGER PRIMARY KEY,
  session_id TEXT,
  updated_at TEXT NOT NULL
);
