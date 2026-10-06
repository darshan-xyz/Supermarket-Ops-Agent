"""Authentication and Multi-Tenant Isolation tests for the Web Dashboard."""

import pytest
from fastapi.testclient import TestClient

from kirana.db.master import MasterRegistry
from kirana.security.password import hash_password
from kirana.web.app import create_web_app
from kirana.web.auth import COOKIE_NAME, create_session_token


@pytest.fixture
def multi_store_setup(tmp_path):
    """Fixture with two distinct, isolated stores."""
    master_db_path = tmp_path / "master.db"
    reg = MasterRegistry(master_db_path)

    # Store A
    store_a = reg.register_tenant(
        store_name="Store Alpha",
        owner_name="Owner A",
        phone_e164="+919000000001",
        address="10 Alpha Street",
        owner_pin_hash=hash_password("1111"),
    )

    # Store B
    store_b = reg.register_tenant(
        store_name="Store Beta",
        owner_name="Owner B",
        phone_e164="+919000000002",
        address="20 Beta Avenue",
        owner_pin_hash=hash_password("2222"),
    )

    return reg, store_a, store_b


def test_unauthenticated_redirect(multi_store_setup):
    """Verify unauthenticated page visits redirect to /login."""
    reg, store_a, store_b = multi_store_setup
    app = create_web_app()

    from kirana.db.master import get_master_registry
    app.dependency_overrides[get_master_registry] = lambda: reg

    with TestClient(app, follow_redirects=False) as client:
        # Protected web pages redirect with 303
        for path in ["/dashboard", "/inventory", "/billing", "/khata", "/analytics", "/employees", "/settings"]:
            resp = client.get(path)
            assert resp.status_code == 303, f"Expected 303 on {path}, got {resp.status_code}"
            assert resp.headers["location"] == "/login"

        # Protected API routes return 401 JSON
        for api_path in ["/api/v1/summary", "/api/v1/inventory", "/api/v1/khata"]:
            resp_api = client.get(api_path)
            assert resp_api.status_code == 401


def test_tampered_cookie_rejection(multi_store_setup):
    """Verify tampered or invalid session cookies are rejected."""
    reg, store_a, store_b = multi_store_setup
    app = create_web_app()

    from kirana.db.master import get_master_registry
    app.dependency_overrides[get_master_registry] = lambda: reg

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(COOKIE_NAME, "fake.tampered.token.12345")
        resp = client.get("/dashboard")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_strict_multi_tenant_isolation(multi_store_setup):
    """Verify Store A session physically only sees Store A data, never Store B."""
    reg, store_a, store_b = multi_store_setup
    app = create_web_app()

    from kirana.db.master import get_master_registry
    app.dependency_overrides[get_master_registry] = lambda: reg

    # Seed unique product in Store A and Store B
    repo_a = reg.get_store_repo(store_a.store_code)
    repo_a.add_product(
        name="Unique Alpha Mango Juice",
        unit="packet",
        gst_rate=12,
        cost_paise=3000,
        sell_paise=4000,
        mrp_paise=4000,
    )

    repo_b = reg.get_store_repo(store_b.store_code)
    repo_b.add_product(
        name="Unique Beta Almond Milk",
        unit="packet",
        gst_rate=5,
        cost_paise=5000,
        sell_paise=6500,
        mrp_paise=6500,
    )

    with TestClient(app, follow_redirects=False) as client:
        # 1. Log in as Store A
        token_a = create_session_token(store_a.store_code)
        client.cookies.set(COOKIE_NAME, token_a)

        resp_a = client.get("/inventory")
        assert resp_a.status_code == 200
        assert "Unique Alpha Mango Juice" in resp_a.text
        assert "Unique Beta Almond Milk" not in resp_a.text  # Physical DB isolation

        # 2. Switch to Store B
        token_b = create_session_token(store_b.store_code)
        client.cookies.set(COOKIE_NAME, token_b)

        resp_b = client.get("/inventory")
        assert resp_b.status_code == 200
        assert "Unique Beta Almond Milk" in resp_b.text
        assert "Unique Alpha Mango Juice" not in resp_b.text  # Physical DB isolation
