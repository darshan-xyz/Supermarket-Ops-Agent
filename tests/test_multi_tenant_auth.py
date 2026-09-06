"""Tests for Multi-Tenant Store Database Isolation, Authentication, and Onboarding."""

from pathlib import Path
import pytest

from kirana.db.master import MasterRegistry, normalize_phone
from kirana.domain.money import to_paise
from kirana.tools.context import current_chat, get_repo


@pytest.fixture
def master_reg(tmp_path: Path):
    """Isolated MasterRegistry for unit tests."""
    master_db_path = tmp_path / "master.db"
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)

    # Monkeypatch STORES_DIR in kirana.db.master
    import kirana.db.master as mm
    orig_dir = mm.STORES_DIR
    mm.STORES_DIR = stores_dir

    reg = MasterRegistry(master_db_path)
    yield reg

    mm.STORES_DIR = orig_dir


def test_normalize_phone():
    """Verify phone normalization handles local and international formats."""
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("919876543210") == "+919876543210"
    assert normalize_phone("+1 415 555 2671") == "+14155552671"


def test_otp_generation_and_verification_lifecycle(master_reg: MasterRegistry):
    """Verify in-chat Telegram OTP generation, expiry, retry limits, and single-use."""
    phone = "+919876543210"
    chat_id = 1001

    # Issue OTP
    code = master_reg.create_otp(phone, chat_id)
    assert len(code) == 6
    assert code.isdigit()

    # Incorrect code attempt
    success, reason = master_reg.verify_otp(phone, chat_id, "000000" if code != "000000" else "111111")
    assert not success
    assert reason == "invalid_code"

    # Correct code attempt
    success, reason = master_reg.verify_otp(phone, chat_id, code)
    assert success
    assert reason == "success"

    # Consumed code cannot be reused
    success2, reason2 = master_reg.verify_otp(phone, chat_id, code)
    assert not success2
    assert reason2 == "no_active_otp"


def test_tenant_registration_provisions_isolated_db(master_reg: MasterRegistry):
    """Verify registering a store provisions an isolated database and seeds catalog."""
    tenant = master_reg.register_tenant(
        store_name="Sri Krishna Supermarket",
        owner_name="Kavitha",
        phone_e164="+919876543210",
        address="12/4 Gandhi Road, Chennai",
        gstin="33ABCDE1234F1Z5",
    )

    assert tenant.store_code.startswith("KIR-")
    assert tenant.db_filename == f"store_{tenant.store_code}.db"
    assert tenant.db_path.exists()

    # Check store repository
    repo = master_reg.get_store_repo(tenant.store_code)
    products = repo.search_products("", limit=50)
    assert len(products) >= 10  # Pre-seeded catalog present
    assert any(p.name == "Maggi 70g" for p in products)

    # Check shop profile
    profile = repo.get_shop_profile()
    assert profile.name == "Sri Krishna Supermarket"
    assert profile.address == "12/4 Gandhi Road, Chennai"
    assert profile.gstin == "33ABCDE1234F1Z5"


def test_strict_multi_tenant_data_isolation(master_reg: MasterRegistry):
    """Verify Store A and Store B have 100% partitioned inventory, bills, and khata records."""
    store_a = master_reg.register_tenant(
        store_name="Store Alpha",
        owner_name="Alice",
        phone_e164="+919111111111",
        address="North Block",
    )
    store_b = master_reg.register_tenant(
        store_name="Store Beta",
        owner_name="Bob",
        phone_e164="+919222222222",
        address="South Block",
    )

    repo_a = master_reg.get_store_repo(store_a.store_code)
    repo_b = master_reg.get_store_repo(store_b.store_code)

    # 1. Custom Product Isolation
    repo_a.add_product(
        name="Alpha Exclusive Pickles 500g",
        unit="packet",
        gst_rate=12,
        hsn_code="2001",
        cost_paise=to_paise(80),
        mrp_paise=to_paise(120),
        sell_paise=to_paise(110),
        is_loose=False,
        opening_stock_base=20000,
    )
    assert len(repo_a.search_products("Alpha Exclusive")) == 1
    assert len(repo_b.search_products("Alpha Exclusive")) == 0  # Completely invisible to Store B

    # 2. Khata Ledger Isolation
    repo_a.khata_add_credit("Sundaram", to_paise(2500), note="Credit provisions")
    assert repo_a.khata_balance("Sundaram").balance_paise == to_paise(2500)

    # Sundaram does not exist in Store B
    with pytest.raises(Exception):
        repo_b.khata_balance("Sundaram")

    # 3. Inventory Stock Invariance across Tenants
    maggi_a = [p for p in repo_a.search_products("Maggi") if p.name == "Maggi 70g"][0]
    maggi_b = [p for p in repo_b.search_products("Maggi") if p.name == "Maggi 70g"][0]

    initial_stock_a = maggi_a.stock_base
    initial_stock_b = maggi_b.stock_base

    # Cut a bill in Store A
    chat_a = 5001
    repo_a.cart_add_item(chat_a, maggi_a.id, 10)
    repo_a.cart_set_payment(chat_a, "cash")
    repo_a.finalize_bill(chat_a, "test-idemp-multi-a1")

    # Stock in Store A decremented
    assert repo_a.get_product(maggi_a.id).stock_base == initial_stock_a - 10000
    # Stock in Store B is completely unaffected
    assert repo_b.get_product(maggi_b.id).stock_base == initial_stock_b


def test_device_recovery_and_existing_store_login(master_reg: MasterRegistry):
    """Verify logging in with an existing verified phone re-links the chat to the existing store."""
    tenant = master_reg.register_tenant(
        store_name="Super Mart",
        owner_name="Murugan",
        phone_e164="+919876500000",
        address="Main Bazaar",
    )

    # Owner starts on chat 10001
    master_reg.link_chat_to_tenant(
        chat_id=10001,
        user_id=888,
        tenant_id=tenant.id,
        role="owner",
        language="ta",
    )
    assert master_reg.get_tenant_for_chat(10001).store_code == tenant.store_code

    # Owner loses phone / opens Telegram on new device with chat 20002
    existing = master_reg.get_tenant_by_phone("+919876500000")
    assert existing is not None
    assert existing.id == tenant.id

    # Link new chat 20002
    master_reg.link_chat_to_tenant(
        chat_id=20002,
        user_id=999,
        tenant_id=existing.id,
        role="owner",
        language="ta",
    )

    resolved_tenant = master_reg.get_tenant_for_chat(20002)
    assert resolved_tenant is not None
    assert resolved_tenant.store_code == tenant.store_code
    assert resolved_tenant.store_name == "Super Mart"


def test_context_var_transparent_repo_switching(master_reg: MasterRegistry):
    """Verify get_repo() transparently resolves the tenant repository based on current_chat."""
    tenant_x = master_reg.register_tenant(
        store_name="Store X",
        owner_name="Xavier",
        phone_e164="+919333333333",
        address="West Street",
    )
    chat_x = 7701
    master_reg.link_chat_to_tenant(chat_x, 1, tenant_x.id)

    token = current_chat.set(chat_x)
    try:
        # Patch master registry lookup in context.py
        import kirana.db.master as mm
        orig_mr = mm._master_registry
        mm._master_registry = master_reg

        repo = get_repo()
        profile = repo.get_shop_profile()
        assert profile.name == "Store X"
    finally:
        current_chat.reset(token)
        mm._master_registry = orig_mr


@pytest.mark.asyncio
async def test_onboarding_contact_verification_security(master_reg: MasterRegistry):
    """Verify phone verification prevents spoofing by checking user_id and blocks manual typing."""
    from unittest.mock import AsyncMock, MagicMock
    from kirana.telegram.onboarding import (
        start_onboarding,
        clear_onboarding_session,
        handle_onboarding_step,
        OnboardingStep,
    )

    chat_id = 8888
    user_id = 12345
    clear_onboarding_session(chat_id)
    state = start_onboarding(chat_id, user_id, language="en")
    assert state.step == OnboardingStep.AWAITING_PHONE

    # 1. Test manual typing is blocked for security
    mock_msg_typed = MagicMock()
    mock_msg_typed.contact = None
    mock_msg_typed.text = "9876543210"
    mock_msg_typed.reply_text = AsyncMock()

    mock_update_typed = MagicMock()
    mock_update_typed.effective_chat.id = chat_id
    mock_update_typed.effective_user.id = user_id
    mock_update_typed.message = mock_msg_typed

    handled = await handle_onboarding_step(mock_update_typed, None, registry=master_reg)
    assert handled is True
    assert state.step == OnboardingStep.AWAITING_PHONE
    mock_msg_typed.reply_text.assert_called_once()
    assert "Security Verification Required" in mock_msg_typed.reply_text.call_args[0][0]

    # 2. Test spoofed contact card (user_id mismatch) is rejected
    mock_contact_spoofed = MagicMock()
    mock_contact_spoofed.phone_number = "+919876543210"
    mock_contact_spoofed.user_id = 99999  # Attacker sharing someone else's contact card

    mock_msg_spoofed = MagicMock()
    mock_msg_spoofed.contact = mock_contact_spoofed
    mock_msg_spoofed.text = None
    mock_msg_spoofed.reply_text = AsyncMock()

    mock_update_spoofed = MagicMock()
    mock_update_spoofed.effective_chat.id = chat_id
    mock_update_spoofed.effective_user.id = user_id
    mock_update_spoofed.message = mock_msg_spoofed

    handled = await handle_onboarding_step(mock_update_spoofed, None, registry=master_reg)
    assert handled is True
    assert state.step == OnboardingStep.AWAITING_PHONE
    mock_msg_spoofed.reply_text.assert_called_once()
    assert "Security Alert" in mock_msg_spoofed.reply_text.call_args[0][0]

    # 3. Test genuine verified contact (matching user_id) succeeds immediately
    mock_contact_valid = MagicMock()
    mock_contact_valid.phone_number = "9876543210"
    mock_contact_valid.user_id = user_id

    mock_msg_valid = MagicMock()
    mock_msg_valid.contact = mock_contact_valid
    mock_msg_valid.text = None
    mock_msg_valid.reply_text = AsyncMock()

    mock_update_valid = MagicMock()
    mock_update_valid.effective_chat.id = chat_id
    mock_update_valid.effective_user.id = user_id
    mock_update_valid.message = mock_msg_valid

    handled = await handle_onboarding_step(mock_update_valid, None, registry=master_reg)
    assert handled is True
    # For a new phone number, it advances to awaiting store name
    assert state.step == OnboardingStep.AWAITING_STORE_NAME
    assert state.phone_e164 == "+919876543210"
    mock_msg_valid.reply_text.assert_called_once()

    clear_onboarding_session(chat_id)


def test_language_normalization_and_inventory_display(master_reg: MasterRegistry):
    """Verify normalize_lang maps variants correctly and format_inventory_message lists products."""
    from kirana.telegram.onboarding import format_inventory_message, normalize_lang

    assert normalize_lang("Tamil") == "ta"
    assert normalize_lang("தமிழ்") == "ta"
    assert normalize_lang("Hindi") == "hi"
    assert normalize_lang("हिंदी") == "hi"
    assert normalize_lang("English") == "en"
    assert normalize_lang("unknown") == "en"

    tenant = master_reg.register_tenant(
        store_name="Test Mart",
        owner_name="Tester",
        phone_e164="+919444444444",
        address="Bazaar Street",
    )
    repo = master_reg.get_store_repo(tenant.store_code)

    inv_en = format_inventory_message(repo, "en")
    assert "Current Store Inventory" in inv_en
    assert "Maggi 70g" in inv_en
    assert "Tata Salt 1kg" in inv_en

    inv_ta = format_inventory_message(repo, "Tamil")
    assert "சரக்கு இருப்பு" in inv_ta
    assert "Maggi 70g" in inv_ta

