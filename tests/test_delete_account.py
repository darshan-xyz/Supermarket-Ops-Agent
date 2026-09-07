"""Tests for Production-Grade Store & Account Deletion."""

import threading
from pathlib import Path
import pytest

from kirana.db.master import MasterRegistry
import kirana.db.master as mm


@pytest.fixture
def isolated_registry(tmp_path: Path):
    """Isolated MasterRegistry with redirected STORES_DIR, ARTIFACTS_DIR, and PROJECT_ROOT."""
    master_db_path = tmp_path / "master.db"
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    logos_dir = stores_dir / "logos"
    logos_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = tmp_path / "artifacts_out"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    orig_stores = mm.STORES_DIR
    orig_artifacts = mm.ARTIFACTS_DIR
    orig_project_root = mm.PROJECT_ROOT

    mm.STORES_DIR = stores_dir
    mm.ARTIFACTS_DIR = artifacts_dir
    mm.PROJECT_ROOT = tmp_path

    # Also create the data/stores/logos directory under tmp_path
    (tmp_path / "data" / "stores" / "logos").mkdir(parents=True, exist_ok=True)

    reg = MasterRegistry(master_db_path)
    yield reg, tmp_path

    mm.STORES_DIR = orig_stores
    mm.ARTIFACTS_DIR = orig_artifacts
    mm.PROJECT_ROOT = orig_project_root


def test_atomic_tenant_deletion_and_file_purge(isolated_registry):
    """Verify delete_tenant wipes isolated DB, logos, invoices, and master DB rows."""
    reg, base_dir = isolated_registry

    phone = "+919876543210"
    clean_phone = "919876543210"
    chat_id = 99991

    # 1. Register tenant
    tenant = reg.register_tenant(
        store_name="Royal Supermarket",
        owner_name="Murugan",
        phone_e164=phone,
        address="10 Main Road, Coimbatore",
    )
    store_code = tenant.store_code
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=123, tenant_id=tenant.id)

    # 2. Add custom data in tenant DB
    repo = reg.get_store_repo(store_code)
    repo.khata_add_credit("Ramesh", 50000)
    p = repo.search_products("Sugar")[0]
    repo.cart_add_item(chat_id, p.id, 1, "kg")
    repo.cart_set_payment(chat_id, "cash")
    bill = repo.finalize_bill(chat_id, "test-bill-key")
    invoice_no = bill.invoice_no

    # 3. Create simulated logo and invoice files
    logo_file1 = base_dir / "data" / "stores" / "logos" / f"logo_{clean_phone}.png"
    logo_file1.write_bytes(b"dummy logo png bytes")
    logo_file2 = base_dir / "data" / "stores" / "logos" / f"store_{store_code}_logo.png"
    logo_file2.write_bytes(b"dummy logo png bytes 2")

    inv_pdf = mm.ARTIFACTS_DIR / f"invoice_{invoice_no}.pdf"
    inv_pdf.write_bytes(b"%PDF-1.4 dummy invoice")

    db_path = mm.STORES_DIR / tenant.db_filename
    assert db_path.exists()
    assert logo_file1.exists()
    assert logo_file2.exists()
    assert inv_pdf.exists()

    # 4. Issue OTP and audit logs
    reg.create_otp(phone, chat_id)

    # 5. Execute deletion
    res = reg.delete_tenant(store_code=store_code)
    assert res["success"] is True
    assert res["store_code"] == store_code

    # 6. Verify files are completely unlinked from disk
    assert not db_path.exists()
    assert not logo_file1.exists()
    assert not logo_file2.exists()
    assert not inv_pdf.exists()

    # 7. Verify control plane in master.db is purged
    assert reg.get_tenant_by_code(store_code) is None
    assert reg.get_tenant_by_phone(phone) is None
    assert reg.get_tenant_for_chat(chat_id) is None
    assert reg.get_user_for_chat(chat_id) is None

    # Check raw tables in master.db
    assert reg.db.query_one("SELECT id FROM tenants WHERE id=?", (tenant.id,)) is None
    assert reg.db.query_one("SELECT id FROM tenant_users WHERE tenant_id=?", (tenant.id,)) is None
    assert reg.db.query_one("SELECT id FROM auth_otps WHERE phone_e164=?", (phone,)) is None


def test_returning_user_clean_reonboarding(isolated_registry):
    """Verify a user whose account was deleted can re-onboard with the same phone and chat without issues or contradictions."""
    reg, _ = isolated_registry

    phone = "+919500382086"
    chat_id = 77777

    # Phase 1: First Store Lifecycle
    tenant_a = reg.register_tenant(
        store_name="First Mart",
        owner_name="Rajesh",
        phone_e164=phone,
        address="10 Anna Salai, Chennai",
    )
    code_a = tenant_a.store_code
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=101, tenant_id=tenant_a.id)

    # First store transacts
    repo_a = reg.get_store_repo(code_a)
    repo_a.khata_add_credit("Suresh", 20000)

    # Verify lookups work for first store
    assert reg.get_tenant_for_chat(chat_id).store_code == code_a
    assert reg.get_tenant_by_phone(phone).store_code == code_a

    # Phase 2: Permanent Deletion
    del_res = reg.delete_tenant(chat_id=chat_id)
    assert del_res["success"] is True

    # Lookups now return None
    assert reg.get_tenant_for_chat(chat_id) is None
    assert reg.get_tenant_by_phone(phone) is None

    # Phase 3: Returning User Re-Registers with Same Phone and Chat
    # Onboarding checks get_tenant_by_phone(phone) -> None -> prompts for new store name
    tenant_b = reg.register_tenant(
        store_name="Brand New Supermarket",
        owner_name="Rajesh Kumar",
        phone_e164=phone,  # SAME phone!
        address="50 Gandhi Road, Madurai",  # Different address
    )
    code_b = tenant_b.store_code
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=101, tenant_id=tenant_b.id)

    # Must be a distinct store code
    assert code_b != code_a
    assert tenant_b.store_name == "Brand New Supermarket"
    assert tenant_b.address == "50 Gandhi Road, Madurai"

    # Verify newly isolated DB is fresh and contains standard seeded catalog
    repo_b = reg.get_store_repo(code_b)
    customers_b = repo_b.list_khata_debtors()
    assert len(customers_b) == 0  # Suresh from Store A does NOT exist in Store B!

    # Active chat now resolves to new store
    active_tenant = reg.get_tenant_for_chat(chat_id)
    assert active_tenant is not None
    assert active_tenant.store_code == code_b
    assert active_tenant.store_name == "Brand New Supermarket"


def test_multithreaded_connection_lock_release_on_deletion(isolated_registry):
    """Verify Database.close() safely terminates connections opened across multiple threads on Windows."""
    reg, _ = isolated_registry

    tenant = reg.register_tenant(
        store_name="Thread Test Mart",
        owner_name="Tester",
        phone_e164="+919876599999",
        address="1 Thread Way",
    )
    store_code = tenant.store_code
    repo = reg.get_store_repo(store_code)

    # Spawn worker threads reading from the tenant DB
    errors = []

    def worker_read():
        try:
            for _ in range(5):
                repo.db.query_all("SELECT count(*) FROM products")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker_read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    # Now delete tenant: must close all thread connections and unlink cleanly
    del_res = reg.delete_tenant(store_code=store_code)
    assert del_res["success"] is True

    db_path = mm.STORES_DIR / tenant.db_filename
    assert not db_path.exists()


def test_delete_non_existent_tenant(isolated_registry):
    """Verify delete_tenant returns graceful error on non-existent store."""
    reg, _ = isolated_registry
    res = reg.delete_tenant(store_code="KIR-NONEXISTENT")
    assert res["success"] is False
    assert res["error"] == "Store not found"


@pytest.mark.asyncio
async def test_telegram_delete_account_cancel_flow(isolated_registry, monkeypatch):
    """Verify triggering /delete_account and tapping Cancel preserves the store."""
    from unittest.mock import AsyncMock, MagicMock
    import kirana.telegram.bot as bot_module

    reg, _ = isolated_registry
    monkeypatch.setattr(bot_module, "get_master_registry", lambda: reg)

    tenant = reg.register_tenant(
        store_name="Safe Mart",
        owner_name="Anand",
        phone_e164="+919876540001",
        address="1 Safe Way",
    )
    chat_id = 55501
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=1, tenant_id=tenant.id)

    # 1. Trigger /delete_account command
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_chat.id = chat_id
    context = MagicMock()
    context.chat_data = {}

    await bot_module._delete_account_command(update, context)

    # Verify confirmation card and buttons sent
    assert msg.reply_text.called
    sent_text = msg.reply_text.call_args[0][0]
    assert "DANGER: Permanent Store & Account Deletion" in sent_text
    assert tenant.store_code in sent_text
    assert context.chat_data.get("awaiting_delete_confirm") == tenant.store_code

    # 2. User taps Cancel
    cb_msg = MagicMock()
    cb_msg.reply_text = AsyncMock()
    query = MagicMock()
    query.data = "del_acc:cancel"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = cb_msg

    cb_update = MagicMock()
    cb_update.callback_query = query
    cb_update.effective_chat.id = chat_id

    await bot_module._delete_account_callback(cb_update, context)

    # Verify cancel notification sent and store remains active
    assert cb_msg.reply_text.called
    assert "Deletion Cancelled" in cb_msg.reply_text.call_args[0][0]
    assert reg.get_tenant_for_chat(chat_id) is not None
    assert context.chat_data.get("awaiting_delete_confirm") is None


@pytest.mark.asyncio
async def test_telegram_delete_account_confirm_flow(isolated_registry, monkeypatch):
    """Verify confirming deletion permanently wipes the store and unlinks the account."""
    from unittest.mock import AsyncMock, MagicMock
    import kirana.telegram.bot as bot_module

    reg, _ = isolated_registry
    monkeypatch.setattr(bot_module, "get_master_registry", lambda: reg)

    tenant = reg.register_tenant(
        store_name="Doomed Mart",
        owner_name="Bala",
        phone_e164="+919876540002",
        address="2 Doomed Lane",
    )
    chat_id = 55502
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=2, tenant_id=tenant.id)

    context = MagicMock()
    context.chat_data = {"awaiting_delete_confirm": tenant.store_code}
    context.bot.send_message = AsyncMock()

    # User taps confirm button
    cb_msg = MagicMock()
    cb_msg.reply_text = AsyncMock()
    cb_msg.edit_text = AsyncMock()
    query = MagicMock()
    query.data = f"del_acc:confirm:{tenant.store_code}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = cb_msg

    cb_update = MagicMock()
    cb_update.callback_query = query
    cb_update.effective_chat.id = chat_id

    await bot_module._delete_account_callback(cb_update, context)

    # Verify store is deleted
    assert reg.get_tenant_for_chat(chat_id) is None
    assert reg.get_tenant_by_code(tenant.store_code) is None
    assert reg.get_tenant_by_phone(tenant.phone_e164) is None

    # Verify confirmation text was sent
    assert cb_msg.edit_text.called
    confirm_text = cb_msg.edit_text.call_args[0][0]
    assert "Store & Account Successfully Deleted" in confirm_text
    assert "Unlinked" in confirm_text


@pytest.mark.asyncio
async def test_telegram_text_delete_account_and_confirm(isolated_registry, monkeypatch):
    """Verify conversational text 'delete my account' triggers warning, and 'CONFIRM DELETE {code}' executes deletion."""
    from unittest.mock import AsyncMock, MagicMock
    import kirana.telegram.bot as bot_module

    reg, _ = isolated_registry
    monkeypatch.setattr(bot_module, "get_master_registry", lambda: reg)

    tenant = reg.register_tenant(
        store_name="Text Mart",
        owner_name="Chitra",
        phone_e164="+919876540003",
        address="3 Text Lane",
    )
    chat_id = 55503
    reg.link_chat_to_tenant(chat_id=chat_id, user_id=3, tenant_id=tenant.id)

    # 1. User sends text "delete my store"
    msg1 = MagicMock()
    msg1.text = "delete my store"
    msg1.reply_text = AsyncMock()
    up1 = MagicMock()
    up1.message = msg1
    up1.effective_chat.id = chat_id
    context = MagicMock()
    context.chat_data = {}

    await bot_module._message(up1, context)

    assert msg1.reply_text.called
    assert "DANGER: Permanent Store & Account Deletion" in msg1.reply_text.call_args[0][0]
    assert context.chat_data.get("awaiting_delete_confirm") == tenant.store_code

    # 2. User confirms with text "CONFIRM DELETE <store_code>"
    msg2 = MagicMock()
    msg2.text = f"CONFIRM DELETE {tenant.store_code}"
    msg2.reply_text = AsyncMock()
    up2 = MagicMock()
    up2.message = msg2
    up2.effective_chat.id = chat_id

    await bot_module._message(up2, context)

    # Store must be permanently purged
    assert reg.get_tenant_for_chat(chat_id) is None
    assert reg.get_tenant_by_code(tenant.store_code) is None
    assert msg2.reply_text.called
    assert "Store & Account Successfully Deleted" in msg2.reply_text.call_args[0][0]


