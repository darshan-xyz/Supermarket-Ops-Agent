"""Telegram long-polling adapter for the kirana store agent.

Wires python-telegram-bot v22 to the Gemini session runner. Each text message
goes through dedup (mark_update_processed) then run_turn, with typing action
shown while the model thinks. Documents queued by tools are sent afterwards.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html as _html
import io
import logging
import os
from pathlib import Path
import re
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from datetime import datetime, timezone

from ..agent.session import reset_session, run_turn
from ..config import (
    AUTO_RESTORE_ON_BOOT,
    BACKUP_CHAT_ID,
    PORT,
    PROJECT_ROOT,
    ensure_dirs,
    require_telegram_token,
)
from ..db.master import get_master_registry
from ..services.backup import (
    auto_restore_if_needed,
    checkpoint_all_databases,
    create_backup_archive,
    restore_backup_archive,
    upload_backup_to_telegram,
    verify_backup_archive,
)
from ..security.password import check_pin_rate_limit, record_failed_attempt, reset_rate_limit
from ..services.logo_service import validate_and_process_logo
from ..tools.context import get_repo, init_context, take_output_files
from ..web.app import create_web_app
from .jobs import register_jobs
from .onboarding import (
    OnboardingStep,
    clear_onboarding_session,
    format_inventory_message,
    get_onboarding_session,
    handle_onboarding_step,
    normalize_lang,
    prompt_phone_verification,
    start_onboarding,
)


logger = logging.getLogger(__name__)

_LANG_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🇮🇳  English", callback_data="lang:English"),
    ],
    [
        InlineKeyboardButton("🇮🇳  தமிழ் (Tamil / Tanglish)", callback_data="lang:Tamil"),
    ],
    [
        InlineKeyboardButton("🇮🇳  हिंदी (Hindi / Hinglish)", callback_data="lang:Hindi"),
    ],
])

_chat_locks: dict[int, asyncio.Lock] = {}


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Ensure strictly sequential message processing per chat to prevent race conditions and duplicate billing."""
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


_WELCOME = (
    "🏪 <b>Kirana Operations Agent</b>\n"
    "<i>Your Autonomous Supermarket Co-Pilot</i>\n"
    "வணக்கம் · नमस्ते · Welcome\n\n"
    "Manage your store effortlessly from Telegram:\n"
    "• 📦 <b>Inventory</b>: Restock, batch expiry, reorder alerts\n"
    "• 🧾 <b>Billing</b>: Fast multi-item billing & GST tax invoices\n"
    "• 📒 <b>Khata</b>: Customer credit tabs & payment reminders\n"
    "• 📊 <b>Analytics</b>: Daily closings & weekly sales decks\n\n"
    "🌐 <b>Please choose your preferred language:</b>\n"
    "மொழியைத் தேர்ந்தெடுக்கவும் · भाषा चुनें"
)

_NEW_CONVERSATION = (
    "🔄 <b>Fresh Conversation Started</b>\n"
    "<i>புதிய உரையாடல் தொடங்கியது · नया सत्र शुरू</i>\n\n"
    "Your catalog, inventory, and khata ledger remain safe.\n\n"
    "🌐 <b>Select your language for this session:</b>\n"
    "மொழியைத் தேர்ந்தெடுக்கவும் · भाषा चुनें"
)

_CHANGE_LANG = (
    "🌐 <b>Select your preferred language:</b>\n"
    "மொழியைத் தேர்ந்தெடுக்கவும் · भाषा चुनें"
)

_HELP = (
    "Examples / உதாரணங்கள்:\n\n"
    "📦 Inventory / சரக்கு\n"
    "• \"Maggi add karo, ₹14 sell price, 50 packet stock\"\n"
    "• \"50 packet Maggi vanthuchu, cost ₹12\"\n"
    "• \"Stock enna kammiya irukku? / Low stock kya hai?\"\n\n"
    "🧾 Billing / பில்லிங்\n"
    "• \"2 packet Maggi aur 1 kg sugar bill mein daalo\"\n"
    "• \"1 kg arisi, 2 packet maggi bill podunga\"\n"
    "• \"UPI payment, bill mudichidu / finalize karo\"\n\n"
    "📒 Khata (Credit) / கடன்\n"
    "• \"Ramesh ka ₹500 udhaar add karo / Ramesh kadan la ₹500 podu\"\n"
    "• \"Ramesh ne ₹200 diye / Ramesh ₹200 kuduthaaru\"\n"
    "• \"Ramesh balance enna? / Ramesh ka balance kya hai?\"\n\n"
    "📊 Reports / அறிக்கைகள்\n"
    "• \"Innaiku sales evvalavu? / Aaj ki sales batao\"\n"
    "• \"Is hafte ki report dikhao / Inda vaaram report kattu\"\n\n"
    "/new — naya conversation / புதிய உரையாடல்\n"
    "/backup — Download full store backup (.zip)\n"
    "/restore — Restore database from backup .zip"
)


_TAG_PATTERN = re.compile(
    r"(</?(?:b|i|u|s|code|pre|a|strong|em)(?:\s+href=\"[^\"]*\")?\s*>)",
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_MD_CODE_BLOCK = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<!\*)\*([^* \n][^*\n]*?[^* \n])\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", re.DOTALL)
_MD_HEADER = re.compile(r"^(?:#{1,6})\s+(.+)$", re.MULTILINE)
_MD_BULLET = re.compile(r"^[ \t]*[*\-][ \t]+", re.MULTILINE)


def _strip_html(text: str) -> str:
    """Strip all HTML tags and unescape entities for plain-text fallback."""
    clean = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(clean)


def _to_telegram_html(text: str) -> str:
    """Render markdown and pre-existing HTML into Telegram-compliant HTML safely."""
    if not text:
        return ""

    # 1. Protect code blocks first with placeholders
    code_blocks: list[str] = []

    def _save_block(m: re.Match) -> str:
        code_blocks.append(f"<pre>{_html.escape(m.group(1).rstrip(), quote=False)}</pre>")
        return f"\x00BLOCK{len(code_blocks)-1}\x00"

    t = _MD_CODE_BLOCK.sub(_save_block, text)

    # 2. Protect inline code
    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(f"<code>{_html.escape(m.group(1), quote=False)}</code>")
        return f"\x00INLINE{len(inline_codes)-1}\x00"

    t = _MD_INLINE_CODE.sub(_save_inline, t)

    # 3. Handle markdown links: [text](url) -> <a href="url">text</a>
    t = _MD_LINK.sub(lambda m: f'<a href="{m.group(2)}">{_html.escape(m.group(1), quote=False)}</a>', t)

    # 4. Handle headers -> <b>Header</b>
    t = _MD_HEADER.sub(r"<b>\1</b>", t)

    # 5. Handle bullet lists: "- item" or "* item" -> "• item"
    t = _MD_BULLET.sub("• ", t)

    # 6. Handle markdown bold: **text** -> <b>text</b>
    t = _MD_BOLD.sub(r"<b>\1</b>", t)

    # 7. Handle markdown italic: _text_ or *text* -> <i>text</i>
    t = _MD_ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", t)

    # 8. Tokenize by valid HTML tags so we escape loose & < > outside existing tags
    parts = _TAG_PATTERN.split(t)
    out: list[str] = []
    for part in parts:
        if _TAG_PATTERN.fullmatch(part):
            out.append(part)
        else:
            out.append(_html.escape(part, quote=False))

    result = "".join(out)

    # 9. Restore code blocks & inline code
    for i, block in enumerate(code_blocks):
        result = result.replace(f"\x00BLOCK{i}\x00", block)
    for i, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{i}\x00", code)

    return result


async def _reply(message, text: str) -> None:
    """Send a reply rendered as HTML; gracefully fall back to clean plain text if rejected."""
    for chunk in _split_message(text):
        html_chunk = _to_telegram_html(chunk)
        try:
            await message.reply_text(html_chunk, parse_mode=ParseMode.HTML)
        except BadRequest:
            # Fallback to plain text with HTML tags stripped
            plain_chunk = _strip_html(chunk)
            await message.reply_text(plain_chunk)


@asynccontextmanager
async def _typing_heartbeat(bot, chat_id: int):
    """Pulsing typing indicator to keep Telegram active during multi-second LLM turns."""
    stop_event = asyncio.Event()

    async def _pulse():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)

    if tenant:
        user = registry.get_user_for_chat(chat_id)
        lang = normalize_lang(user.preferred_language if user else "en")
        store_repo = registry.get_store_repo(tenant.store_code)

        intro = {
            "ta": (
                f"🏪 <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n"
                f"👤 உரிமையாளர்: {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
                f"📍 {tenant.address}\n\n"
                "🌐 <b>மொழியை மாற்ற விரும்பினால் கீழே தேர்ந்தெடுக்கவும்:</b>"
            ),
            "hi": (
                f"🏪 <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n"
                f"👤 मालिक: {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
                f"📍 {tenant.address}\n\n"
                "🌐 <b>भाषा बदलने के लिए नीचे टैप करें:</b>"
            ),
            "en": (
                f"🏪 <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n"
                f"👤 Owner: {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
                f"📍 {tenant.address}\n\n"
                "🌐 <b>Select or change your language anytime:</b>"
            ),
        }
        await update.message.reply_text(
            intro.get(lang, intro["en"]),
            reply_markup=_LANG_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
        inv_msg = format_inventory_message(store_repo, lang)
        if inv_msg:
            await update.message.reply_text(inv_msg, parse_mode=ParseMode.HTML)
        return

    # Fresh Onboarding for new user
    clear_onboarding_session(chat_id)
    start_onboarding(chat_id=chat_id, user_id=user_id, step=OnboardingStep.AWAITING_LANGUAGE)
    await update.message.reply_text(
        _WELCOME,
        reply_markup=_LANG_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(_HELP)


async def _new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new — reset the Gemini session while keeping persistent data."""
    assert update.message is not None
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    reset_session(chat_id)
    await update.message.reply_text(
        _NEW_CONVERSATION,
        reply_markup=_LANG_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )


async def _lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /language or /lang — prompt for language selection."""
    assert update.message is not None
    await update.message.reply_text(
        _CHANGE_LANG,
        reply_markup=_LANG_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )


async def _inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /inventory or /stock — show current store inventory."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)
    if not tenant:
        await update.message.reply_text(
            "ℹ️ No store registered to this chat yet. Type /start to register your supermarket!",
            reply_markup=_LANG_KEYBOARD,
        )
        return
    user = registry.get_user_for_chat(chat_id)
    lang = user.preferred_language if user else "en"
    store_repo = registry.get_store_repo(tenant.store_code)
    inv_msg = format_inventory_message(store_repo, lang)
    await update.message.reply_text(inv_msg or "Inventory is empty.", parse_mode=ParseMode.HTML)


async def _language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactive button click for language selection."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("lang:"):
        return
    lang_raw = data.replace("lang:", "", 1)
    lang = normalize_lang(lang_raw)
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    user_id = update.effective_user.id if update.effective_user else 0

    registry = get_master_registry()
    registry.set_user_language(chat_id, lang)

    # Dismiss inline buttons so user cannot trigger duplicate clicks
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Check if this chat is already registered to a tenant store
    tenant = registry.get_tenant_for_chat(chat_id)
    if tenant:
        store_repo = registry.get_store_repo(tenant.store_code)
        store_repo.set_preference("language", lang_raw)
        confirmations = {
            "English": (
                f"✅ <b>Language set to English!</b>\n\n"
                f"🏪 Store: <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n\n"
                "I'm ready to manage your store. You can:\n"
                "• <b>Inventory</b>: <i>\"50 packets of Maggi came in, cost ₹12, MRP ₹14\"</i>\n"
                "• <b>Billing</b>: <i>\"2kg sugar, 1 Aashirvaad atta 5kg, UPI\"</i>\n"
                "• <b>Khata</b>: <i>\"Put ₹500 on Ramesh's credit\"</i>\n"
                "• <b>Reports</b>: <i>\"Today's sales?\"</i> or <i>\"Send me that bill as PDF\"</i>"
            ),
            "Tamil": (
                f"✅ <b>மொழி தமிழ் / Tanglish என அமைக்கப்பட்டது!</b>\n\n"
                f"🏪 கடை: <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n\n"
                "நீங்கள் தமிழ் அல்லது Tanglish-ல் பேசலாம்:\n"
                "• <b>சரக்கு வரவு</b>: <i>\"50 பாக்கெட் மேகி வந்தது, cost ₹12\"</i>\n"
                "• <b>பில்லிங்</b>: <i>\"2 கிலோ சர்க்கரை, 1 பாக்கெட் ஆசீர்வாத் கோதுமை, UPI\"</i>\n"
                "• <b>கடன் கணக்கு</b>: <i>\"ரமேஷ் கணக்கில் ₹500 கடன் சேர்\"</i>\n"
                "• <b>விற்பனை அறிக்கை</b>: <i>\"இன்றைய விற்பனை என்ன?\"</i>"
            ),
            "Hindi": (
                f"✅ <b>भाषा हिंदी / Hinglish सेट हो गई है!</b>\n\n"
                f"🏪 स्टोर: <b>{tenant.store_name}</b> (<code>{tenant.store_code}</code>)\n\n"
                "आप हिंदी या Hinglish में बात कर सकते हैं:\n"
                "• <b>स्टॉक जोड़ें</b>: <i>\"50 पैकेट मैगी आई, लागत ₹12, MRP ₹14\"</i>\n"
                "• <b>बिल बनाएं</b>: <i>\"2 किलो चीनी, 1 पैकेट आशीर्वाद आटा, UPI\"</i>\n"
                "• <b>खाता उधारी</b>: <i>\"रमेश के खाते में ₹500 उधार लिखो\"</i>\n"
                "• <b>रिपोर्ट</b>: <i>\"आज की बिक्री बताओ\"</i>"
            ),
        }
        reply_text = confirmations.get(lang_raw, f"✅ <b>Language set to {lang_raw}!</b>")
        if query.message:
            await _reply(query.message, reply_text)
            inv_msg = format_inventory_message(store_repo, lang)
            if inv_msg:
                await _reply(query.message, inv_msg)
        return

    # Chat is not yet onboarded to a store -> Advance to Phone Verification in chosen language!
    start_onboarding(chat_id=chat_id, user_id=user_id, language=lang, step=OnboardingStep.AWAITING_PHONE)
    await prompt_phone_verification(update, lang)


async def _store_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display current store details, unique store code, and database status."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)
    if not tenant:
        await update.message.reply_text(
            "ℹ️ No store registered to this chat yet. Type /start to register your supermarket!",
            reply_markup=_LANG_KEYBOARD,
        )
        return
    repo = registry.get_store_repo(tenant.store_code)
    profile = repo.get_shop_profile()
    gstin_line = f"🏷️ <b>GSTIN:</b> {tenant.gstin}\n" if tenant.gstin else ""
    logo_status = "🖼️ <b>Logo:</b> Active ✅ (Use /logo to view or change)\n" if (profile.logo_path and os.path.exists(profile.logo_path)) else "🖼️ <b>Logo:</b> Not configured (Use /logo to upload)\n"
    await update.message.reply_text(
        f"🏪 <b>{tenant.store_name}</b>\n\n"
        f"🔑 <b>Store Code:</b> <code>{tenant.store_code}</code>\n"
        f"👤 <b>Owner:</b> {tenant.owner_name}\n"
        f"📱 <b>Mobile:</b> {tenant.phone_e164}\n"
        f"📍 <b>Address:</b> {tenant.address}\n"
        f"{gstin_line}"
        f"{logo_status}"
        f"💾 <b>Database:</b> <code>{tenant.db_filename}</code> (Isolated & Active)",
        parse_mode=ParseMode.HTML,
    )


async def _logo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View current store logo or prompt to upload a new one."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)
    if not tenant:
        await update.message.reply_text(
            "ℹ️ No store registered to this chat yet. Type /start to register your supermarket!",
            reply_markup=_LANG_KEYBOARD,
        )
        return

    repo = registry.get_store_repo(tenant.store_code)
    profile = repo.get_shop_profile()
    lang = repo.get_preference("language") or "en"
    context.chat_data["awaiting_logo_upload"] = True

    if profile.logo_path and os.path.exists(profile.logo_path):
        caption = {
            "ta": (
                "🖼️ <b>தற்போதைய கடையின் லோகோ</b>\n\n"
                "புதிய லோகோவை மாற்ற விரும்பினால், இப்போது புதிய படத்தை (<b>PNG, JPG அல்லது WEBP</b>) அனுப்பவும்.\n"
                "<i>(ரத்து செய்ய 'cancel' என அனுப்பவும்)</i>"
            ),
            "hi": (
                "🖼️ <b>वर्तमान स्टोर लोगो</b>\n\n"
                "नया लोगो सेट करने के लिए, कृपया नई इमेज (<b>PNG, JPG या WEBP</b>) भेजें।\n"
                "<i>(रद्द करने के लिए 'cancel' लिखें)</i>"
            ),
            "en": (
                "🖼️ <b>Current Store Logo</b>\n\n"
                "To update your logo, please send a new photo or image file (<b>PNG, JPG, or WEBP</b>).\n"
                "<i>(Type 'cancel' to keep current logo)</i>"
            ),
        }
        try:
            with open(profile.logo_path, "rb") as fh:
                await update.message.reply_photo(
                    photo=fh,
                    caption=caption.get(lang, caption["en"]),
                    parse_mode=ParseMode.HTML,
                )
            return
        except Exception as exc:
            logger.warning("Failed to reply with logo photo: %s", exc)

    prompts = {
        "ta": (
            "🖼️ <b>கடையின் லோகோவை பதிவேற்றவும்:</b>\n\n"
            "உங்கள் கடையின் லோகோவை (<b>PNG, JPG அல்லது WEBP</b> படமாக) இப்போது அனுப்பவும். "
            "இது உங்கள் அனைத்து ஜிஎஸ்டி பில்கள், ரசீதுகள் மற்றும் விற்பனை அறிக்கைகளில் அச்சிடப்படும்!\n\n"
            "<i>(ரத்து செய்ய 'cancel' என அனுப்பவும்)</i>"
        ),
        "hi": (
            "🖼️ <b>स्टोर लोगो अपलोड करें:</b>\n\n"
            "कृपया अपने स्टोर का लोगो इमेज (<b>PNG, JPG या WEBP</b>) भेजें। "
            "यह आपके सभी जीएसटी इनवॉइस, बिल रसीदों और बिक्री रिपोर्टों पर दिखेगा!\n\n"
            "<i>(रद्द करने के लिए 'cancel' लिखें)</i>"
        ),
        "en": (
            "🖼️ <b>Upload Store Logo:</b>\n\n"
            "Please send your store logo as a photo or image file (<b>PNG, JPG, or WEBP</b>). "
            "This will appear on all your printed GST invoices, bill receipts, and reports!\n\n"
            "<i>(Type 'cancel' to cancel)</i>"
        ),
    }
    await update.message.reply_text(prompts.get(lang, prompts["en"]), parse_mode=ParseMode.HTML)


async def _execute_store_deletion(
    chat_id: int,
    store_code: str,
    lang: str,
    context: ContextTypes.DEFAULT_TYPE,
    reply_target: Any,
    is_callback: bool = False,
) -> None:
    """Execute atomic 7-phase deletion under the chat's serialization lock."""
    registry = get_master_registry()
    async with _get_chat_lock(chat_id):
        tenant = registry.get_tenant_by_code(store_code)
        if not tenant:
            if hasattr(reply_target, "reply_text"):
                await reply_target.reply_text("ℹ️ Store not found or already deleted.")
            return

        store_name = tenant.store_name
        phone = tenant.phone_e164

        # 1. Purge from MasterRegistry (databases, logos, invoices, master DB rows)
        registry.delete_tenant(store_code=store_code)

        # 2. Reset conversational memory and AI context
        reset_session(chat_id)

        # 3. Clear onboarding state machine and Telegram chat context
        clear_onboarding_session(chat_id)
        context.chat_data.clear()
        take_output_files(chat_id)

        success_texts = {
            "ta": (
                "🗑️ <b>கடை மற்றும் கணக்கு வெற்றிகரமாக நீக்கப்பட்டது!</b>\n\n"
                f"🏪 <b>{store_name}</b> (<code>{store_code}</code>) முற்றிலுமாக அழிக்கப்பட்டது.\n"
                "• தனிப்பட்ட தரவுத்தளம், சரக்கு மற்றும் பில்லிங்: <b>அழிக்கப்பட்டது</b> ✅\n"
                "• கடன் கணக்குகள் (Khata) மற்றும் அறிக்கைகள்: <b>அழிக்கப்பட்டது</b> ✅\n"
                "• லோகோ மற்றும் கோப்புகள்: <b>அழிக்கப்பட்டது</b> ✅\n"
                f"• மொபைல் எண் <code>{phone}</code>: <b>இணைப்பு நீக்கப்பட்டது</b> ✅\n\n"
                "✨ <i>புதிய கடையைத் தொடங்க விரும்பும் போது, /start என அனுப்பவும்.</i>"
            ),
            "hi": (
                "🗑️ <b>स्टोर और खाता सफलतापूर्वक हटा दिया गया!</b>\n\n"
                f"🏪 <b>{store_name}</b> (<code>{store_code}</code>) पूरी तरह से हटा दिया गया है।\n"
                "• डेटाबेस, स्टॉक और बिलिंग रिकॉर्ड्स: <b>हटा दिए गए</b> ✅\n"
                "• खाता उधारी और इनवॉइस रिपोर्ट्स: <b>हटा दिए गए</b> ✅\n"
                "• लोगो और फ़ाइलें: <b>हटा दी गईं</b> ✅\n"
                f"• मोबाइल नंबर <code>{phone}</code>: <b>अनलिंक हो गया</b> ✅\n\n"
                "✨ <i>जब भी आप नया स्टोर शुरू करना चाहें, बस /start भेजें।</i>"
            ),
            "en": (
                "🗑️ <b>Store & Account Successfully Deleted!</b>\n\n"
                f"🏪 <b>{store_name}</b> (<code>{store_code}</code>) has been completely removed.\n"
                "• Dedicated database, inventory & billing records: <b>Purged</b> ✅\n"
                "• Customer khata credit balances & reports: <b>Purged</b> ✅\n"
                "• Store logo & asset files: <b>Purged</b> ✅\n"
                f"• Mobile number <code>{phone}</code>: <b>Unlinked</b> ✅\n\n"
                "✨ <i>Whenever you're ready to start fresh, simply type /start to create a new store.</i>"
            ),
        }

        start_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("/start")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )

        msg = success_texts.get(lang, success_texts["en"])
        if is_callback and hasattr(reply_target, "edit_text"):
            await reply_target.edit_text(msg, parse_mode=ParseMode.HTML)
            await context.bot.send_message(
                chat_id=chat_id,
                text="👉 Type /start to register a new supermarket anytime.",
                reply_markup=start_keyboard,
            )
        elif hasattr(reply_target, "reply_text"):
            await reply_target.reply_text(msg, reply_markup=start_keyboard, parse_mode=ParseMode.HTML)


async def prompt_deletion_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tenant: Any,
    lang: str = "en",
) -> None:
    """Prompt store owner with a high-friction confirmation card to delete store and account."""
    if not update.message or not update.effective_chat:
        return

    # Set confirmation challenge in context
    context.chat_data["awaiting_delete_confirm"] = tenant.store_code

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔴 Yes, Delete Everything Permanently",
                callback_data=f"del_acc:confirm:{tenant.store_code}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel (Keep Store Safe)",
                callback_data="del_acc:cancel",
            )
        ],
    ])

    warning_text = {
        "ta": (
            "⚠️ <b>கடை மற்றும் கணக்கை நிரந்தரமாக நீக்குதல் (DANGER: Permanent Deletion)</b>\n\n"
            f"🏪 <b>கடை:</b> {tenant.store_name} (<code>{tenant.store_code}</code>)\n"
            f"👤 <b>உரிமையாளர்:</b> {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
            f"📍 <b>முகவரி:</b> {tenant.address}\n\n"
            "🚨 <b>இந்த செயல்முறை திரும்பப்பெற முடியாதது (IRREVERSIBLE). பின்வரும் அனைத்தும் நிரந்தரமாக அழிக்கப்படும்:</b>\n"
            "• 📦 அனைத்து பொருட்கள், சரக்கு இருப்பு & காலாவதி (FEFO) தரவு\n"
            "• 🧾 அனைத்து விற்பனை வரலாறு, பில்கள் & ஜிஎஸ்டி அறிக்கைகள்\n"
            "• 📒 அனைத்து வாடிக்கையாளர் கடன் கணக்குகள் & நிலுவைத் தொகைகள்\n"
            "• 🖼️ கடையின் லோகோ மற்றும் தனிப்பயன் அமைப்புகள்\n"
            f"• 💾 தனிப்பட்ட தரவுத்தளம் (<code>{tenant.db_filename}</code>)\n\n"
            "👉 <b>நீக்குவதை உறுதிப்படுத்த கீழே உள்ள சிவப்பு பட்டனை அழுத்தவும், அல்லது:</b>\n"
            f"<code>CONFIRM DELETE {tenant.store_code}</code> என தட்டச்சு செய்யவும்.\n\n"
            "<i>(ரத்து செய்ய 'cancel' என அனுப்பவும் அல்லது Cancel பட்டனை அழுத்தவும்)</i>"
        ),
        "hi": (
            "⚠️ <b>स्टोर और खाता स्थायी रूप से हटाएं (DANGER: Permanent Deletion)</b>\n\n"
            f"🏪 <b>स्टोर:</b> {tenant.store_name} (<code>{tenant.store_code}</code>)\n"
            f"👤 <b>मालिक:</b> {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
            f"📍 <b>पता:</b> {tenant.address}\n\n"
            "🚨 <b>यह प्रक्रिया अपरिवर्तनीय है (IRREVERSIBLE)। निम्न सभी डेटा हमेशा के लिए मिट जाएगा:</b>\n"
            "• 📦 सभी प्रोडक्ट्स, स्टॉक और एक्सपायरी (FEFO) डेटा\n"
            "• 🧾 सभी बिक्री इतिहास, बिल और जीएसटी रिपोर्ट्स\n"
            "• 📒 सभी ग्राहक उधारी खाते और बकाया राशि\n"
            "• 🖼️ स्टोर का लोगो और सेटिंग्स\n"
            f"• 💾 आपका व्यक्तिगत डेटाबेस (<code>{tenant.db_filename}</code>)\n\n"
            "👉 <b>हटाने की पुष्टि के लिए नीचे लाल बटन दबाएं, या:</b>\n"
            f"<code>CONFIRM DELETE {tenant.store_code}</code> टाइप करें।\n\n"
            "<i>(रद्द करने के लिए 'cancel' लिखें या Cancel बटन दबाएं)</i>"
        ),
        "en": (
            "⚠️ <b>DANGER: Permanent Store & Account Deletion</b>\n\n"
            f"🏪 <b>Store:</b> {tenant.store_name} (<code>{tenant.store_code}</code>)\n"
            f"👤 <b>Owner:</b> {tenant.owner_name} | 📱 {tenant.phone_e164}\n"
            f"📍 <b>Address:</b> {tenant.address}\n\n"
            "🚨 <b>This action is IRREVERSIBLE. It will permanently purge:</b>\n"
            "• 📦 All products, current inventory, batches & FEFO expiry tracking\n"
            "• 🧾 All sales records, finalized bills & printed tax invoices\n"
            "• 📒 All customer khata accounts, ledgers & outstanding balances\n"
            "• 🖼️ Store logo and custom settings\n"
            f"• 💾 Your dedicated store database (<code>{tenant.db_filename}</code>)\n\n"
            "👉 <b>To confirm deletion, tap the red button below, or reply:</b>\n"
            f"<code>CONFIRM DELETE {tenant.store_code}</code>\n\n"
            "<i>(To abort, tap Cancel or reply 'cancel')</i>"
        ),
    }

    await update.message.reply_text(
        warning_text.get(lang, warning_text["en"]),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def _delete_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt store owner with a high-friction confirmation card to delete store and account."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id

    # Always clear any active onboarding session if present
    clear_onboarding_session(chat_id)

    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)

    if not tenant:
        await update.message.reply_text(
            "ℹ️ No active store is registered to this chat.\n"
            "Type /start to register a new store!",
            reply_markup=_LANG_KEYBOARD,
        )
        return

    user = registry.get_user_for_chat(chat_id)
    lang = normalize_lang(user.preferred_language if user else "en")
    await prompt_deletion_confirmation(update, context, tenant, lang)


async def _cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — abort onboarding, active cart, or delete confirmation."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    registry = get_master_registry()
    user = registry.get_user_for_chat(chat_id)
    lang = normalize_lang(user.preferred_language if user else "en")

    # 1. Clear deletion confirm or PIN if active
    if context.chat_data.pop("awaiting_delete_confirm", None) or context.chat_data.pop("awaiting_delete_pin", None):
        tenant = registry.get_tenant_for_chat(chat_id)
        name = tenant.store_name if tenant else "Your store"
        await update.message.reply_text(f"✅ Deletion cancelled. {name} remains safe.")
        return

    # 2. Clear logo upload if active
    if context.chat_data.pop("awaiting_logo_upload", None):
        await update.message.reply_text("✅ Logo upload cancelled.")
        return

    # 3. Clear onboarding if active
    state = get_onboarding_session(chat_id)
    if state:
        clear_onboarding_session(chat_id)
        cancel_msgs = {
            "ta": "❌ <b>கடை பதிவு ரத்து செய்யப்பட்டது.</b>\n\nபுதிய கடையை அமைக்க அல்லது தொடங்க /start என தட்டச்சு செய்யவும்.",
            "hi": "❌ <b>स्टोर सेटअप रद्द कर दिया गया है।</b>\n\nनया स्टोर शुरू करने के लिए /start टाइप करें।",
            "en": "❌ <b>Store setup cancelled.</b>\n\nType /start anytime to begin store registration again.",
        }
        await update.message.reply_text(
            cancel_msgs.get(lang, cancel_msgs["en"]),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.HTML,
        )
        return

    # 4. Reset active shopping cart if any
    tenant = registry.get_tenant_for_chat(chat_id)
    if tenant:
        repo = registry.get_store_repo(tenant.store_code)
        repo.cart_clear(chat_id)
        await update.message.reply_text("🛒 Current cart / action cleared. Ready for next command.")
    else:
        await update.message.reply_text("ℹ️ Nothing to cancel. Type /start to begin.")


async def _delete_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks for delete account confirmation."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    registry = get_master_registry()
    user = registry.get_user_for_chat(chat_id)
    lang = normalize_lang(user.preferred_language if user else "en")

    if data == "del_acc:cancel":
        context.chat_data.pop("awaiting_delete_confirm", None)
        context.chat_data.pop("awaiting_delete_pin", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        tenant = registry.get_tenant_for_chat(chat_id)
        name = tenant.store_name if tenant else "Your store"
        code = f" ({tenant.store_code})" if tenant else ""
        cancel_msgs = {
            "ta": f"✅ <b>நீக்குதல் ரத்து செய்யப்பட்டது:</b> உங்கள் கடை <b>{name}</b>{code} மற்றும் அனைத்து தரவுகளும் பாதுகாப்பாக உள்ளன.",
            "hi": f"✅ <b>हटाना रद्द किया गया:</b> आपका स्टोर <b>{name}</b>{code} और सभी डेटा पूरी तरह सुरक्षित हैं।",
            "en": f"✅ <b>Deletion Cancelled:</b> Your store <b>{name}</b>{code} and all data remain completely safe and active.",
        }
        if query.message:
            await _reply(query.message, cancel_msgs.get(lang, cancel_msgs["en"]))
        return

    if data.startswith("del_acc:confirm:"):
        store_code = data.replace("del_acc:confirm:", "", 1).strip()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        repo = registry.get_store_repo(store_code)
        if repo and repo.has_owner_password():
            context.chat_data["awaiting_delete_pin"] = store_code
            context.chat_data.pop("awaiting_delete_confirm", None)
            pin_prompts = {
                "ta": (
                    "🔐 <b>உரிமையாளர் பாதுகாப்பு PIN தேவை:</b>\n\n"
                    "கடை மற்றும் அனைத்து தரவுகளையும் நிரந்தரமாக நீக்க உங்கள் <b>உரிமையாளர் பாதுகாப்பு PIN அல்லது கடவுச்சொல்லை</b> உள்ளிடவும்:\n\n"
                    "<i>(ரத்து செய்ய 'cancel' என தட்டச்சு செய்யவும்)</i>"
                ),
                "hi": (
                    "🔐 <b>स्टोर मालिक सुरक्षा पिन आवश्यक:</b>\n\n"
                    "स्टोर और सभी डेटा को स्थायी रूप से हटाने के लिए कृपया अपना <b>सुरक्षा पिन या पासवर्ड</b> दर्ज करें:\n\n"
                    "<i>(रद्द करने के लिए 'cancel' टाइप करें)</i>"
                ),
                "en": (
                    "🔐 <b>Store Owner Security PIN Required:</b>\n\n"
                    "To authorize permanent deletion of your store and database, please enter your <b>Store Owner Security PIN / Password</b>:\n\n"
                    "<i>(To abort, reply 'cancel')</i>"
                ),
            }
            if query.message:
                await query.message.reply_text(pin_prompts.get(lang, pin_prompts["en"]), parse_mode=ParseMode.HTML)
            return

        if query.message:
            await _execute_store_deletion(chat_id, store_code, lang, context, query.message, is_callback=True)


async def _backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create and send an on-demand database backup archive to the caller."""
    if not update.effective_message:
        return
    msg = update.effective_message
    status_msg = await msg.reply_text("⏳ <i>Generating secure backup snapshot of your store databases...</i>", parse_mode=ParseMode.HTML)
    try:
        archive_path = create_backup_archive()
        registry = get_master_registry()
        tenants = registry.list_all_tenants()

        caption = (
            "📦 <b>Supermarket Ops Agent Backup Snapshot</b>\n\n"
            f"• 🏪 Active Stores: <b>{len(tenants)}</b>\n"
            f"• 💾 Size: <b>{round(archive_path.stat().st_size / 1024, 1)} KB</b>\n"
            f"• 📅 Created: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n\n"
            "<i>Keep this archive safe. You can restore this state anytime by replying to this file with <code>/restore</code>.</i>"
        )
        with open(archive_path, "rb") as fh:
            await msg.reply_document(
                document=fh,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.error("Failed creating manual backup: %s", exc, exc_info=True)
        await status_msg.edit_text(f"⚠️ Failed to generate backup snapshot: {exc}")


async def _restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore database state from an attached or replied zip backup."""
    if not update.effective_message:
        return
    msg = update.effective_message

    doc = msg.document
    # Check if this command was sent as a reply to a message containing a document
    if not doc and msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document

    if not doc or not doc.file_name or not doc.file_name.endswith(".zip"):
        await msg.reply_text(
            "ℹ️ <b>How to Restore Your Database:</b>\n\n"
            "1. Upload your backup <code>.zip</code> file with caption <code>/restore</code>\n"
            "<b>OR</b>\n"
            "2. Reply to any previously sent backup message with <code>/restore</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await msg.reply_text("⏳ <i>Downloading and verifying backup archive...</i>", parse_mode=ParseMode.HTML)
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        doc_file = await context.bot.get_file(doc.file_id)
        await doc_file.download_to_drive(custom_path=str(tmp_path))

        valid, reason = verify_backup_archive(tmp_path)
        if not valid:
            await status_msg.edit_text(
                f"❌ <b>Restoration Failed:</b>\nArchive verification failed: {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        res = restore_backup_archive(tmp_path)
        await status_msg.edit_text(
            "✅ <b>Database Successfully Restored!</b>\n\n"
            f"• 🏪 Stores Restored: <b>{res.get('stores_restored', 0)}</b>\n"
            f"• 📁 Files Restored: <b>{res.get('files_restored', 0)}</b>\n\n"
            "All inventory catalogs, batches, customer tabs, and khata records are active and loaded.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.error("Restore failed: %s", exc, exc_info=True)
        await status_msg.edit_text(f"⚠️ Failed to restore database: {exc}")
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


async def _message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text, voice notes, and photos — the main entry point for the agent."""
    if update.message is None:
        return

    chat_id: int = update.effective_chat.id  # type: ignore[union-attr]

    # 1. Intercept onboarding state machine (phone verification, OTP, store questionnaire, logo)
    if await handle_onboarding_step(update, context):
        return

    # 2. Check if this store has completed onboarding
    registry = get_master_registry()
    tenant = registry.get_tenant_for_chat(chat_id)
    if not tenant:
        clear_onboarding_session(chat_id)
        start_onboarding(
            chat_id=chat_id,
            user_id=update.effective_user.id if update.effective_user else 0,
            step=OnboardingStep.AWAITING_LANGUAGE,
        )
        await update.message.reply_text(
            _WELCOME,
            reply_markup=_LANG_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
        return

    msg_text_lower = (update.message.text or "").strip().lower()

    # 2.5 Check for active deletion PIN challenge
    awaiting_pin_store = context.chat_data.get("awaiting_delete_pin")
    if awaiting_pin_store:
        user = registry.get_user_for_chat(chat_id)
        lang = normalize_lang(user.preferred_language if user else "en")
        if msg_text_lower in ("cancel", "no", "stop", "abort", "ரத்து", "रद्द"):
            context.chat_data.pop("awaiting_delete_pin", None)
            t_name = tenant.store_name if tenant else "Your store"
            t_code = f" ({tenant.store_code})" if tenant else ""
            cancel_msgs = {
                "ta": f"✅ <b>நீக்குதல் ரத்து செய்யப்பட்டது:</b> உங்கள் கடை <b>{t_name}</b>{t_code} பாதுகாப்பாக உள்ளது.",
                "hi": f"✅ <b>हटाना रद्द किया गया:</b> आपका स्टोर <b>{t_name}</b>{t_code} पूरी तरह सुरक्षित है।",
                "en": f"✅ <b>Deletion Cancelled:</b> Your store <b>{t_name}</b>{t_code} remains completely safe.",
            }
            await update.message.reply_text(cancel_msgs.get(lang, cancel_msgs["en"]), parse_mode=ParseMode.HTML)
            return

        rl = check_pin_rate_limit(chat_id)
        if rl.is_locked:
            mins = int(rl.lockout_remaining_seconds // 60) + 1
            await update.message.reply_text(f"⚠️ Too many failed security attempts. Rate-limited for {mins} minutes.")
            return

        entered_pin = (update.message.text or "").strip()
        repo = registry.get_store_repo(awaiting_pin_store)
        if not repo.verify_owner_password(entered_pin):
            rl = record_failed_attempt(chat_id)
            err_pin = {
                "ta": f"❌ தவறான பாதுகாப்பு PIN / கடவுச்சொல். (மீதமுள்ள முயற்சிகள்: {rl.remaining_attempts})",
                "hi": f"❌ अमान्य सुरक्षा पिन / पासवर्ड। (शेष प्रयास: {rl.remaining_attempts})",
                "en": f"❌ Incorrect Store Owner Security PIN / Password. ({rl.remaining_attempts} attempts remaining)",
            }
            await update.message.reply_text(err_pin.get(lang, err_pin["en"]))
            return

        reset_rate_limit(chat_id)
        context.chat_data.pop("awaiting_delete_pin", None)
        user = registry.get_user_for_chat(chat_id)
        lang = normalize_lang(user.preferred_language if user else "en")
        await _execute_store_deletion(chat_id, awaiting_pin_store, lang, context, update.message, is_callback=False)
        return

    # 3. Check for active deletion confirmation challenge
    awaiting_delete = context.chat_data.get("awaiting_delete_confirm")
    if awaiting_delete:
        if msg_text_lower in ("cancel", "no", "stop", "abort", "ரத்து", "ரद्द"):
            context.chat_data.pop("awaiting_delete_confirm", None)
            user = registry.get_user_for_chat(chat_id)
            lang = normalize_lang(user.preferred_language if user else "en")
            cancel_msgs = {
                "ta": f"✅ <b>நீக்குதல் ரத்து செய்யப்பட்டது:</b> உங்கள் கடை <b>{tenant.store_name}</b> ({tenant.store_code}) பாதுகாப்பாக உள்ளது.",
                "hi": f"✅ <b>हटाना रद्द किया गया:</b> आपका स्टोर <b>{tenant.store_name}</b> ({tenant.store_code}) पूरी तरह सुरक्षित है।",
                "en": f"✅ <b>Deletion Cancelled:</b> Your store <b>{tenant.store_name}</b> ({tenant.store_code}) remains completely safe.",
            }
            await update.message.reply_text(cancel_msgs.get(lang, cancel_msgs["en"]), parse_mode=ParseMode.HTML)
            return

        expected_phrases = [
            "confirm delete",
            f"confirm delete {awaiting_delete.lower()}",
            f"delete {awaiting_delete.lower()}",
        ]
        if any(msg_text_lower == p for p in expected_phrases):
            context.chat_data.pop("awaiting_delete_confirm", None)
            user = registry.get_user_for_chat(chat_id)
            lang = normalize_lang(user.preferred_language if user else "en")
            repo = registry.get_store_repo(awaiting_delete)
            if repo and repo.has_owner_password():
                context.chat_data["awaiting_delete_pin"] = awaiting_delete
                pin_prompts = {
                    "ta": (
                        "🔐 <b>உரிமையாளர் பாதுகாப்பு PIN தேவை:</b>\n\n"
                        "கடை மற்றும் அனைத்து தரவுகளையும் நிரந்தரமாக நீக்க உங்கள் <b>உரிமையாளர் பாதுகாப்பு PIN அல்லது கடவுச்சொல்லை</b> உள்ளிடவும்:\n\n"
                        "<i>(ரத்து செய்ய 'cancel' என தட்டச்சு செய்யவும்)</i>"
                    ),
                    "hi": (
                        "🔐 <b>स्टोर मालिक सुरक्षा पिन आवश्यक:</b>\n\n"
                        "स्टोर और सभी डेटा को स्थायी रूप से हटाने के लिए कृपया अपना <b>सुरक्षा पिन या पासवर्ड</b> दर्ज करें:\n\n"
                        "<i>(रद्द करने के लिए 'cancel' टाइप करें)</i>"
                    ),
                    "en": (
                        "🔐 <b>Store Owner Security PIN Required:</b>\n\n"
                        "To authorize permanent deletion of your store and database, please enter your <b>Store Owner Security PIN / Password</b>:\n\n"
                        "<i>(To abort, reply 'cancel')</i>"
                    ),
                }
                await update.message.reply_text(pin_prompts.get(lang, pin_prompts["en"]), parse_mode=ParseMode.HTML)
                return

            await _execute_store_deletion(chat_id, awaiting_delete, lang, context, update.message, is_callback=False)
            return

    # 4. Check for natural language store deletion intent
    deletion_keywords = (
        "delete account",
        "delete my account",
        "delete store",
        "delete my store",
        "/delete_account",
        "/deleteaccount",
        "remove my store",
        "remove store",
        "கணக்கை நீக்கு",
        "கடையை நீக்கு",
        "खाता हटाएं",
        "स्टोर हटाएं",
    )
    if any(msg_text_lower == kw for kw in deletion_keywords):
        await _delete_account_command(update, context)
        return

    # 3. Check if this message is a logo upload for an existing store
    caption = (update.message.caption or "").strip().lower()
    msg_text_lower = (update.message.text or "").strip().lower()
    is_logo_flow = context.chat_data.get("awaiting_logo_upload") or any(
        k in caption for k in ("logo", "லோகோ", "लोगो", "store logo")
    )

    if context.chat_data.get("awaiting_logo_upload") and msg_text_lower in ("cancel", "stop", "close", "ரத்து", "रद्द"):
        context.chat_data.pop("awaiting_logo_upload", None)
        await update.message.reply_text("✅ Logo update cancelled.")
        return

    if is_logo_flow and (update.message.photo or update.message.document):
        repo = get_repo()
        lang = repo.get_preference("language") or "en"
        stream = io.BytesIO()

        if update.message.photo:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            await photo_file.download_to_memory(out=stream)
        elif update.message.document:
            doc = update.message.document
            if doc.file_size and doc.file_size > 5 * 1024 * 1024:
                size_mb = doc.file_size / (1024 * 1024)
                await update.message.reply_text(f"⚠️ The file is too large ({size_mb:.1f} MB). Logos must be under 5 MB.")
                return
            doc_file = await doc.get_file()
            await doc_file.download_to_memory(out=stream)

        stream.seek(0)
        dest_logo = PROJECT_ROOT / "data" / "stores" / "logos" / f"store_{tenant.store_code}_logo.png"
        res = validate_and_process_logo(stream, dest_logo)
        if not res.is_valid:
            await update.message.reply_text(res.get_message(lang), parse_mode=ParseMode.HTML)
            return

        repo.set_shop_profile(logo_path=res.saved_path)
        context.chat_data.pop("awaiting_logo_upload", None)

        success_msgs = {
            "ta": (
                "✅ <b>கடையின் லோகோ வெற்றிகரமாகப் புதுப்பிக்கப்பட்டது!</b>\n\n"
                "உங்கள் லோகோ சேமிக்கப்பட்டது. இது உங்கள் அனைத்து ஜிஎஸ்டி பில்கள், ரசீதுகள் மற்றும் விற்பனை அறிக்கைகளில் பிரதிபலிக்கும்."
            ),
            "hi": (
                "✅ <b>स्टोर लोगो सफलतापूर्वक अपडेट हो गया!</b>\n\n"
                "आपका लोगो सेव हो गया है। यह आपके सभी जीएसटी इनवॉइस, बिल रसीदों और बिक्री रिपोर्टों पर दिखेगा।"
            ),
            "en": (
                "✅ <b>Store Logo Updated Successfully!</b>\n\n"
                "Your logo is now active and will reflect on all printed GST invoices, bill receipts, and reports."
            ),
        }
        await update.message.reply_text(success_msgs.get(lang, success_msgs["en"]), parse_mode=ParseMode.HTML)
        return

    text: str = update.message.text or update.message.caption or ""
    media_bytes: bytes | None = None
    media_mime: str | None = None

    # Handle voice notes or audio recordings (transcribe & order)
    if update.message.voice:
        if update.message.voice.file_size and update.message.voice.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Voice file exceeds the 20 MB size limit.")
            return
        try:
            voice_file = await update.message.voice.get_file()
            buf = bytearray()
            await voice_file.download_as_bytearray(buf)
            media_bytes = bytes(buf)
            media_mime = update.message.voice.mime_type or "audio/ogg"
            if not text:
                text = "Please process this voice order or instructions from the store owner."
        except Exception as e:
            logger.warning("Failed to download voice note: %s", e)

    elif update.message.audio:
        if update.message.audio.file_size and update.message.audio.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Audio file exceeds the 20 MB size limit.")
            return
        try:
            audio_file = await update.message.audio.get_file()
            buf = bytearray()
            await audio_file.download_as_bytearray(buf)
            media_bytes = bytes(buf)
            media_mime = update.message.audio.mime_type or "audio/mpeg"
            if not text:
                text = "Please process this audio order or instructions from the store owner."
        except Exception as e:
            logger.warning("Failed to download audio file: %s", e)

    # Handle photos / barcodes
    elif update.message.photo:
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Photo file exceeds the 20 MB size limit.")
            return
        try:
            photo_file = await photo.get_file()
            buf = bytearray()
            await photo_file.download_as_bytearray(buf)
            media_bytes = bytes(buf)
            media_mime = "image/jpeg"
            if not text:
                text = "Identify the product or barcode in this image and report details or bill it."
        except Exception as e:
            logger.warning("Failed to download photo: %s", e)

    if not text and not media_bytes:
        return

    # Idempotency: skip duplicate Telegram redeliveries
    repo = get_repo()
    if not repo.mark_update_processed(update.update_id, chat_id):
        logger.info("Skipping duplicate update_id=%d for chat_id=%d", update.update_id, chat_id)
        return

    async with _get_chat_lock(chat_id):
        try:
            async with _typing_heartbeat(context.bot, chat_id):
                res = await run_turn(chat_id, text, media_bytes=media_bytes, media_mime=media_mime)
        except Exception as exc:
            logger.error(
                "run_turn failed for chat_id=%d update_id=%d: %s",
                chat_id, update.update_id, exc,
                exc_info=True,
            )
            err_text = str(exc)
            lang = repo.get_preference("language") or "en"
            if "429" in err_text or "RESOURCE_EXHAUSTED" in err_text:
                if "ta" in lang:
                    msg = (
                        "⚠️ <b>கூகிள் ஏஐ வரம்பு நிறைவடைந்தது (Quota Limit)</b>\n\n"
                        "இலவச ஏஐ சேவை தற்காலிகமாக நிறைவடைந்துள்ளது. 1 நிமிடத்தில் மீண்டும் முயற்சிக்கவும்."
                    )
                elif "hi" in lang:
                    msg = (
                        "⚠️ <b>Google AI कोटा सीमा समाप्त (Quota Limit)</b>\n\n"
                        "AI सेवा की सीमा पूरी हो गई है। कृपया 1 मिनट प्रतीक्षा करें और दोबारा प्रयास करें।"
                    )
                else:
                    msg = (
                        "⚠️ <b>Google AI Free Quota Limit Reached</b>\n\n"
                        "The Gemini API free-tier quota is cooling down. Please wait 1 minute and try again."
                    )
            elif "503" in err_text or "UNAVAILABLE" in err_text:
                if "ta" in lang:
                    msg = "⚠️ AI சேவையகம் தற்போது அதிக சுமையிலுள்ளது. தயவுசெய்து சிறிது நொடிகள் கழித்து முயற்சிக்கவும்."
                elif "hi" in lang:
                    msg = "⚠️ AI सर्वर पर अभी अधिक लोड है। कृपया कुछ सेकंड बाद पुनः प्रयास करें।"
                else:
                    msg = "⚠️ The AI server is experiencing temporary high demand. Please try again in a moment."
            else:
                if "ta" in lang:
                    msg = "மன்னிக்கவும், ஒரு சிறிய பிழை ஏற்பட்டது. தயவுசெய்து மீண்டும் முயற்சிக்கவும்."
                elif "hi" in lang:
                    msg = "माफ़ करें, कोई समस्या आई। कृपया दोबारा प्रयास करें।"
                else:
                    msg = "Sorry, something went wrong processing your request. Please try again."

            await _reply(update.message, msg)
            return

        if res.reply:
            await _reply(update.message, res.reply)

        for f in res.files:
            try:
                with open(f.path, "rb") as fh:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=fh,
                        caption=f.caption or None,
                    )
            except Exception as exc:
                logger.error("Failed to send document %s: %s", f.path, exc, exc_info=True)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global PTB error handler — logs internally and sends sanitized message to user."""
    logger.error("Unhandled PTB error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        lang = "en"
        try:
            registry = get_master_registry()
            user = registry.get_user_for_chat(chat_id)
            if user and user.preferred_language:
                lang = normalize_lang(user.preferred_language)
        except Exception:
            pass

        safe_err_msgs = {
            "ta": "⚠️ மன்னிக்கவும், உங்கள் கோரிக்கையைச் செயலாக்குவதில் ஒரு எதிர்பாராத பிழை ஏற்பட்டது. தயவுசெய்து சிறிது நேரம் கழித்து மீண்டும் முயற்சிக்கவும்.",
            "hi": "⚠️ माफ़ करें, आपके अनुरोध को प्रोसेस करने में एक अनपेक्षित समस्या आई। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
            "en": "⚠️ Sorry, an unexpected error occurred while processing your request. Please try again in a moment.",
        }
        await update.effective_message.reply_text(
            safe_err_msgs.get(lang, safe_err_msgs["en"])
        )


async def _post_init(application: Application) -> None:
    """Run boot state recovery and launch FastAPI admin web server on PORT."""
    if AUTO_RESTORE_ON_BOOT:
        try:
            await auto_restore_if_needed(application.bot)
        except Exception as exc:
            logger.warning("Error during boot auto-restore check: %s", exc)

    # Start FastAPI + Uvicorn server for health monitoring, Keep-Alive, and Admin Dashboard
    import uvicorn

    web_app = create_web_app(application)
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    application.bot_data["web_server"] = server
    server_task = asyncio.create_task(server.serve())
    application.bot_data["web_server_task"] = server_task
    logger.info("FastAPI Admin Dashboard & Keep-Alive server listening on PORT=%d", PORT)


async def _post_shutdown(application: Application) -> None:
    """Graceful shutdown: stop web server, flush WAL checkpoints, and upload emergency backup."""
    server = application.bot_data.get("web_server")
    if server:
        server.should_exit = True
    server_task = application.bot_data.get("web_server_task")
    if server_task and not server_task.done():
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except Exception:
            pass

    checkpoint_all_databases()

    # Emergency backup snapshot on shutdown if configured
    if BACKUP_CHAT_ID:
        try:
            await upload_backup_to_telegram(
                application.bot,
                BACKUP_CHAT_ID,
                pin=True,
                caption_prefix="📦 #kirana_backup [Graceful Shutdown Snapshot]",
            )
            logger.info("Shutdown emergency backup completed successfully.")
        except Exception as exc:
            logger.warning("Failed uploading shutdown backup: %s", exc)


def main() -> None:
    """Entry point: configure logging, init DB, build and run the bot."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    ensure_dirs()
    init_context()

    token = require_telegram_token()

    application = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", _start))
    application.add_handler(CommandHandler("help", _help))
    application.add_handler(CommandHandler("new", _new))
    application.add_handler(CommandHandler("language", _lang))
    application.add_handler(CommandHandler("lang", _lang))
    application.add_handler(CommandHandler("store", _store_info))
    application.add_handler(CommandHandler("profile", _store_info))
    application.add_handler(CommandHandler("logo", _logo_command))
    application.add_handler(CommandHandler("inventory", _inventory))
    application.add_handler(CommandHandler("stock", _inventory))
    application.add_handler(CommandHandler("backup", _backup_command))
    application.add_handler(CommandHandler("restore", _restore_command))
    application.add_handler(CommandHandler("cancel", _cancel_command))
    application.add_handler(CommandHandler("delete_account", _delete_account_command))
    application.add_handler(CommandHandler("deleteaccount", _delete_account_command))
    application.add_handler(CallbackQueryHandler(_language_callback, pattern=r"^lang:"))
    application.add_handler(CallbackQueryHandler(_delete_account_callback, pattern=r"^del_acc:"))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.VOICE | filters.AUDIO | filters.PHOTO | filters.CONTACT | filters.Document.ALL)
            & ~filters.COMMAND,
            _message,
        )
    )
    application.add_error_handler(_error_handler)

    register_jobs(application)

    logger.info("Kirana bot starting — long-polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
