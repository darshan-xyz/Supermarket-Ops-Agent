"""Test suite for Kirana Admin Web Dashboard (FastAPI, HTMX, Templates, REST API)."""

import pytest
from fastapi.testclient import TestClient

from kirana.db.master import MasterRegistry
from kirana.security.password import hash_password
from kirana.web.app import create_web_app
from kirana.web.auth import COOKIE_NAME, create_session_token


@pytest.fixture
def master_reg(tmp_path):
    """Fixture providing a fresh MasterRegistry with a seeded store."""
    master_db_path = tmp_path / "master.db"
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)

    reg = MasterRegistry(master_db_path)
    # Monkeypatch STORES_DIR for test isolation

    pin_hash = hash_password("1234")
    tenant = reg.register_tenant(
        store_name="Sri Balaji Supermarket",
        owner_name="Balaji",
        phone_e164="+919876543210",
        address="12 Main Road, Chennai",
        gstin="33ABCDE1234F1Z5",
        owner_pin_hash=pin_hash,
    )
    return reg, tenant


@pytest.fixture
def client(master_reg):
    """FastAPI TestClient with overridden MasterRegistry."""
    reg, tenant = master_reg
    app = create_web_app()

    from kirana.db.master import get_master_registry
    app.dependency_overrides[get_master_registry] = lambda: reg

    with TestClient(app, follow_redirects=False) as c:
        yield c, reg, tenant


def test_health_and_ping(client):
    """Verify /health and /ping return 200 OK JSON for Render keep-alive."""
    c, _, _ = client

    resp_health = c.get("/health")
    assert resp_health.status_code == 200
    data_h = resp_health.json()
    assert data_h["status"] == "ok"
    assert data_h["healthy"] is True

    resp_ping = c.get("/ping")
    assert resp_ping.status_code == 200
    assert resp_ping.json()["healthy"] is True


def test_root_discovery(client):
    """Verify root endpoint handles probe headers and browser redirects."""
    c, _, _ = client

    # JSON probe request
    resp_json = c.get("/", headers={"accept": "application/json"})
    assert resp_json.status_code == 200
    assert resp_json.json()["service"] == "kirana-ops-agent"

    # Browser unauthenticated request -> Redirect to /login
    resp_browser = c.get("/")
    assert resp_browser.status_code == 303
    assert resp_browser.headers["location"] == "/login"


def test_login_flow(client):
    """Test login page rendering, failed login, and successful login with session cookie."""
    c, reg, tenant = client

    # 1. GET /login renders form
    resp = c.get("/login")
    assert resp.status_code == 200
    assert "Store Portal Sign In" in resp.text

    # 2. POST /login with invalid PIN
    resp_bad = c.post("/login", data={"store_code": tenant.store_code, "pin": "0000"})
    assert resp_bad.status_code == 400
    assert "Invalid Store Code or PIN" in resp_bad.text

    # 3. POST /login with valid PIN
    resp_ok = c.post("/login", data={"store_code": tenant.store_code, "pin": "1234"})
    assert resp_ok.status_code == 303
    assert resp_ok.headers["location"] == "/dashboard"
    assert COOKIE_NAME in resp_ok.cookies


def test_dashboard_views(client):
    """Test authenticated dashboard and HTMX partial metric cards."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    # 1. Full Dashboard
    resp = c.get("/dashboard")
    assert resp.status_code == 200
    assert "Store Pulse" in resp.text
    assert "Sri Balaji Supermarket" in resp.text

    # 2. HTMX Partial KPI Cards
    resp_cards = c.get("/dashboard/partials/cards")
    assert resp_cards.status_code == 200
    assert "dashboard-kpi-cards" in resp_cards.text


def test_inventory_and_stock_inward(client):
    """Test inventory listing, search, restock modal, stock receive, and CSV export."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    # 1. Full inventory view
    resp = c.get("/inventory")
    assert resp.status_code == 200
    assert "Active Catalog SKUs" in resp.text

    # 2. Search partial
    resp_search = c.get("/inventory/search?q=Atta")
    assert resp_search.status_code == 200
    assert "Atta" in resp_search.text

    # 3. Restock Modal
    repo = reg.get_store_repo(tenant.store_code)
    prod = repo.list_inventory()[0]
    resp_modal = c.get(f"/inventory/modal/inward?product_id={prod.id}")
    assert resp_modal.status_code == 200
    assert f"Stock Inward: {prod.name}" in resp_modal.text

    # 4. Post Stock Inward
    resp_recv = c.post(
        "/inventory/receive",
        data={
            "product_id": prod.id,
            "qty": 20,
            "cost_rupees": 40.0,
            "sell_rupees": 48.0,
            "note": "Test Restock",
        },
    )
    assert resp_recv.status_code == 200
    assert "inventory-table-container" in resp_recv.text

    # 5. CSV Export
    resp_csv = c.get("/inventory/export/csv")
    assert resp_csv.status_code == 200
    assert resp_csv.headers["content-type"] == "text/csv; charset=utf-8"
    assert "Product ID,Name,Brand" in resp_csv.text


def test_billing_and_receipt_modal(client):
    """Test billing feed, search, receipt preview modal, and CSV exports."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    repo = reg.get_store_repo(tenant.store_code)
    # Create a draft and finalize a bill
    repo.cart_add_item(chat_id=123, product_id=1, qty_display="2", unit="kg")
    repo.cart_set_payment(chat_id=123, mode="cash")
    bill = repo.finalize_bill(chat_id=123, idempotency_key="web-test-bill-1")

    # 1. Billing Page
    resp = c.get("/billing")
    assert resp.status_code == 200
    assert "Billing History" in resp.text
    assert bill.invoice_no in resp.text

    # 2. Receipt Modal
    resp_receipt = c.get(f"/billing/modal/receipt?bill_id={bill.id}")
    assert resp_receipt.status_code == 200
    assert "Tax Invoice Receipt" in resp_receipt.text
    assert bill.invoice_no in resp_receipt.text

    # 3. CSV Export
    resp_csv = c.get("/billing/export/csv")
    assert resp_csv.status_code == 200
    assert "Invoice Number" in resp_csv.text


def test_khata_ledger_and_payment(client):
    """Test khata page, debtor search, ledger modal, payment recording, and CSV export."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    repo = reg.get_store_repo(tenant.store_code)
    repo.find_or_create_customer("Murugan", "9876500000")
    repo.khata_add_credit("Murugan", amount_paise=150000, note="Groceries")

    # 1. Khata Page
    resp = c.get("/khata")
    assert resp.status_code == 200
    assert "Murugan" in resp.text
    assert "1500.00" in resp.text

    # 2. Statement Modal
    resp_stmt = c.get("/khata/modal/statement?customer_name=Murugan")
    assert resp_stmt.status_code == 200
    assert "Khata Ledger: Murugan" in resp_stmt.text

    # 3. Record Payment
    resp_pay = c.post(
        "/khata/payment",
        data={
            "customer_name": "Murugan",
            "amount_rupees": 500.0,
            "note": "Cash repayment",
        },
    )
    assert resp_pay.status_code == 200
    assert "1000.00" in resp_pay.text  # 1500 - 500 = 1000 balance remaining

    # 4. CSV Export
    resp_csv = c.get("/khata/export/csv")
    assert resp_csv.status_code == 200
    assert "Murugan" in resp_csv.text


def test_analytics_page(client):
    """Test analytics view with date range filter and chart datasets."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    resp = c.get("/analytics")
    assert resp.status_code == 200
    assert "Sales Performance" in resp.text
    assert "revenueChart" in resp.text


def test_employee_management(client):
    """Test listing staff, registering new staff, and deactivation."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    # 1. Staff Page
    resp = c.get("/employees")
    assert resp.status_code == 200
    assert "Staff & Cashiers" in resp.text

    # 2. Add Employee
    resp_add = c.post(
        "/employees/add",
        data={
            "name": "Karthik",
            "role": "cashier",
            "phone": "9998887776",
        },
    )
    assert resp_add.status_code == 200
    assert "Karthik" in resp_add.text

    # 3. Deactivate Employee
    repo = reg.get_store_repo(tenant.store_code)
    emp = repo.get_employee("Karthik")
    assert emp is not None

    resp_deact = c.post("/employees/deactivate", data={"emp_code": emp.emp_code})
    assert resp_deact.status_code == 200
    assert "Deactivated" in resp_deact.text


def test_store_settings_and_pin_change(client):
    """Test updating store profile and changing security PIN."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    # 1. Update Profile
    resp_prof = c.post(
        "/settings/profile",
        data={
            "name": "Balaji Grand Supermarket",
            "address": "99 Cross Street, Chennai",
            "phone": "+919876543210",
            "gstin": "33ABCDE1234F1Z5",
            "invoice_prefix": "BAL",
        },
    )
    assert resp_prof.status_code == 200
    assert "Store profile updated successfully!" in resp_prof.text

    # 2. Change PIN (wrong current PIN)
    resp_bad_pin = c.post(
        "/settings/password",
        data={
            "current_pin": "9999",
            "new_pin": "5678",
        },
    )
    assert resp_bad_pin.status_code == 200
    assert "Current PIN verification failed" in resp_bad_pin.text

    # 3. Change PIN (valid current PIN)
    resp_ok_pin = c.post(
        "/settings/password",
        data={
            "current_pin": "1234",
            "new_pin": "5678",
        },
    )
    assert resp_ok_pin.status_code == 200
    assert "Owner Security PIN changed successfully!" in resp_ok_pin.text


def test_json_rest_api(client):
    """Test REST API v1 endpoints for JSON integration."""
    c, reg, tenant = client
    token = create_session_token(tenant.store_code)
    c.cookies.set(COOKIE_NAME, token)

    # 1. API Summary
    resp_sum = c.get("/api/v1/summary")
    assert resp_sum.status_code == 200
    data = resp_sum.json()
    assert data["store_code"] == tenant.store_code
    assert "inventory" in data
    assert "khata" in data

    # 2. API Inventory
    resp_inv = c.get("/api/v1/inventory")
    assert resp_inv.status_code == 200
    items = resp_inv.json()
    assert len(items) > 0
    assert "name" in items[0]

    # 3. API Khata
    resp_khata = c.get("/api/v1/khata")
    assert resp_khata.status_code == 200
    assert isinstance(resp_khata.json(), list)
