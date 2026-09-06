"""Telegram long-polling adapter for the kirana store agent.

Wires python-telegram-bot v22 to the Gemini session runner. Each text message
goes through dedup (mark_update_processed) then run_turn, with typing action
shown while the model thinks. Documents queued by tools are sent afterwards.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html as _html
import logging
import re
import traceback

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from ..agent.session import reset_session, run_turn
from ..config import ensure_dirs, require_telegram_token
from ..db.master import get_master_registry
from ..tools.context import get_repo, init_context
from .jobs import register_jobs
from .onboarding import (
    OnboardingStep,
    clear_onboarding_session,
    format_inventory_message,
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
    "/new — naya conversation / புதிய உரையாடல்"
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
    gstin_line = f"🏷️ <b>GSTIN:</b> {tenant.gstin}\n" if tenant.gstin else ""
    await update.message.reply_text(
        f"🏪 <b>{tenant.store_name}</b>\n\n"
        f"🔑 <b>Store Code:</b> <code>{tenant.store_code}</code>\n"
        f"👤 <b>Owner:</b> {tenant.owner_name}\n"
        f"📱 <b>Mobile:</b> {tenant.phone_e164}\n"
        f"📍 <b>Address:</b> {tenant.address}\n"
        f"{gstin_line}"
        f"💾 <b>Database:</b> <code>{tenant.db_filename}</code> (Isolated & Active)",
        parse_mode=ParseMode.HTML,
    )


async def _message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text, voice notes, and photos — the main entry point for the agent."""
    if update.message is None:
        return

    chat_id: int = update.effective_chat.id  # type: ignore[union-attr]

    # 1. Intercept onboarding state machine (phone verification, OTP, store questionnaire)
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
    text: str = update.message.text or update.message.caption or ""
    media_bytes: bytes | None = None
    media_mime: str | None = None

    # Handle voice notes or audio recordings (transcribe & order)
    if update.message.voice:
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
        try:
            # highest resolution photo is the last item
            photo = update.message.photo[-1]
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
    """Global PTB error handler — logs and notifies the user if possible."""
    logger.error("Unhandled PTB error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__ if context.error else None))
        short = str(context.error)[:150] if context.error else "unknown error"
        logger.debug("Traceback:\n%s", tb)
        await update.effective_message.reply_text(
            f"Maafi chahta hoon, kuch error aa gayi: {short}"
        )


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
        .build()
    )

    application.add_handler(CommandHandler("start", _start))
    application.add_handler(CommandHandler("help", _help))
    application.add_handler(CommandHandler("new", _new))
    application.add_handler(CommandHandler("language", _lang))
    application.add_handler(CommandHandler("lang", _lang))
    application.add_handler(CommandHandler("store", _store_info))
    application.add_handler(CommandHandler("profile", _store_info))
    application.add_handler(CommandHandler("inventory", _inventory))
    application.add_handler(CommandHandler("stock", _inventory))
    application.add_handler(CallbackQueryHandler(_language_callback, pattern=r"^lang:"))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.VOICE | filters.AUDIO | filters.PHOTO | filters.CONTACT) & ~filters.COMMAND, _message)
    )
    application.add_error_handler(_error_handler)

    register_jobs(application)

    logger.info("Kirana bot starting — long-polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
