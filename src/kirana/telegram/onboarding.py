"""Conversational Store Onboarding & Authentication State Machine for Telegram.

Guides the store owner through:
  1. Phone verification (1-tap Telegram native contact sharing OR manual phone + 6-digit Telegram OTP)
  2. Automatic branch: Existing store login vs. New store registration
  3. Store questionnaire:
     - Store Name
     - Owner Name
     - Address & Billing location
     - Optional GSTIN (or skip)
  4. Automatic provisioning of isolated tenant database (`data/stores/store_{KIR-XXXX}.db`)
  5. Multi-lingual prompts in English, Tamil, and Hindi.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..agent.guardrails import is_valid_gstin
from ..config import PROJECT_ROOT
from ..db.master import MasterRegistry, get_master_registry, normalize_phone
from ..domain.money import format_inr
from ..domain.units import format_qty
from ..security.password import hash_password
from ..services.logo_service import validate_and_process_logo

logger = logging.getLogger("kirana.onboarding")


def _parse_employee_names(text: str) -> list[str]:
    """Extract individual employee names from freeform text input."""
    parts = re.split(r"[,;\n\r]+|\b(?:and|மற்றும்|और)\b|[&+]", text, flags=re.IGNORECASE)
    names: list[str] = []
    for p in parts:
        cleaned = re.sub(r"^[\s\d\.\-\)\*:]+", "", p).strip()
        if cleaned and len(cleaned) >= 2 and cleaned.lower() not in ("skip", "none", "employee", "employees"):
            names.append(cleaned)
    return names


def normalize_lang(val: str | None) -> str:
    """Normalize language name or code to 'en', 'ta', or 'hi'."""
    if not val:
        return "en"
    s = str(val).strip().lower()
    if any(k in s for k in ("ta", "tamil", "தமிழ்", "tanglish", "2")):
        return "ta"
    if any(k in s for k in ("hi", "hindi", "हिंदी", "hinglish", "3")):
        return "hi"
    return "en"


def format_inventory_message(store_repo, lang: str = "en") -> str:
    """Render a clean, formatted table of all products in the store's inventory."""
    canonical = normalize_lang(lang)
    products = store_repo.search_products("", limit=50)
    if not products:
        return ""

    headers = {
        "ta": "📦 <b>கடையின் தற்போதைய சரக்கு இருப்பு (Current Inventory):</b>\n",
        "hi": "📦 <b>स्टोर की वर्तमान इन्वेंटरी (Current Inventory):</b>\n",
        "en": "📦 <b>Current Store Inventory:</b>\n",
    }
    lines = [headers.get(canonical, headers["en"])]
    for p in products:
        qty_str = format_qty(p.stock_base, p.unit)
        price_str = format_inr(p.sell_paise)
        mrp_str = format_inr(p.mrp_paise) if p.mrp_paise else ""
        mrp_tag = f" (MRP {mrp_str})" if p.mrp_paise and p.mrp_paise != p.sell_paise else ""
        exp_row = store_repo.db.query_one(
            "SELECT expiry FROM batches WHERE product_id=? AND qty_base > 0 AND expiry IS NOT NULL ORDER BY expiry ASC LIMIT 1",
            (p.id,),
        )
        exp_tag = f" · ⏳ Exp: <code>{exp_row['expiry']}</code>" if exp_row and exp_row["expiry"] else ""
        lines.append(f"• <b>{p.name}</b> — <code>{qty_str}</code> | {price_str}{mrp_tag}{exp_tag}")

    total_skus = len(products)
    footers = {
        "ta": f"\n<i>மொத்தம் {total_skus} பொருட்கள் உள்ளன. நீங்கள் உடனே பில்லிங் தொடங்கலாம்!</i>",
        "hi": f"\n<i>कुल {total_skus} आइटम उपलब्ध हैं। आप तुरंत बिलिंग शुरू कर सकते हैं!</i>",
        "en": f"\n<i>Total {total_skus} products ready for billing & restock!</i>",
    }
    lines.append(footers.get(canonical, footers["en"]))
    return "\n".join(lines)


class OnboardingStep(str, Enum):
    AWAITING_LANGUAGE = "awaiting_language"
    AWAITING_PHONE = "awaiting_phone"
    AWAITING_OTP = "awaiting_otp"
    AWAITING_STORE_NAME = "awaiting_store_name"
    AWAITING_OWNER_NAME = "awaiting_owner_name"
    AWAITING_OWNER_PASSWORD = "awaiting_owner_password"
    AWAITING_EMPLOYEE_COUNT = "awaiting_employee_count"
    AWAITING_EMPLOYEE_NAMES = "awaiting_employee_names"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_GSTIN = "awaiting_gstin"
    AWAITING_LOGO = "awaiting_logo"


@dataclass
class OnboardingState:
    chat_id: int
    user_id: int
    step: OnboardingStep
    language: str = "en"
    phone_e164: str = ""
    store_name: str = ""
    owner_name: str = ""
    owner_pin_hash: str | None = None
    target_employee_count: int = 0
    employees: list[dict[str, Any]] = field(default_factory=list)
    address: str = ""
    gstin: str | None = None
    logo_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)



# In-memory onboarding sessions keyed by chat_id
_ONBOARDING_SESSIONS: dict[int, OnboardingState] = {}


def get_onboarding_session(chat_id: int) -> OnboardingState | None:
    return _ONBOARDING_SESSIONS.get(chat_id)


def start_onboarding(
    chat_id: int,
    user_id: int,
    language: str = "en",
    step: OnboardingStep | None = None,
) -> OnboardingState:
    chosen_step = step or OnboardingStep.AWAITING_PHONE
    state = OnboardingState(
        chat_id=chat_id,
        user_id=user_id,
        step=chosen_step,
        language=normalize_lang(language),
    )
    _ONBOARDING_SESSIONS[chat_id] = state
    return state


def clear_onboarding_session(chat_id: int) -> None:
    _ONBOARDING_SESSIONS.pop(chat_id, None)


def _lang_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply buttons for language selection."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🇮🇳 English")],
            [KeyboardButton("🇮🇳 தமிழ் (Tamil / Tanglish)")],
            [KeyboardButton("🇮🇳 हिंदी (Hindi / Hinglish)")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def prompt_language_selection(update: Update) -> None:
    """Prompt the user to select their preferred language."""
    assert update.message or update.callback_query
    msg = update.message or update.callback_query.message  # type: ignore[union-attr]
    text = (
        "🌐 <b>Select your preferred language / மொழியைத் தேர்ந்தெடுக்கவும் / भाषा चुनें:</b>\n\n"
        "👉 Please tap a button below to choose your language:\n"
        "1. <b>English</b>\n"
        "2. <b>தமிழ் (Tamil / Tanglish)</b>\n"
        "3. <b>हिंदी (Hindi / Hinglish)</b>"
    )
    await msg.reply_text(
        text,
        reply_markup=_lang_reply_keyboard(),
        parse_mode=ParseMode.HTML,
    )


def _phone_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """Button for 1-tap Telegram native contact verification."""
    canonical = normalize_lang(lang)
    btn_text = {
        "ta": "📱 சரிபார்க்கப்பட்ட எண்ணைப் பகிரவும்",
        "hi": "📱 वेरिफाइड मोबाइल नंबर साझा करें",
    }.get(canonical, "📱 Share Verified Mobile Number")
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=btn_text, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def prompt_phone_verification(update: Update, lang: str) -> None:
    """Ask user to verify mobile number via 1-tap verified contact button."""
    assert update.message or update.callback_query
    msg = update.message or update.callback_query.message  # type: ignore[union-attr]
    canonical = normalize_lang(lang)

    texts = {
        "ta": (
            "📱 <b>மொபைல் எண் சரிபார்ப்பு</b>\n\n"
            "உங்கள் பல்பொருள் அங்காடியை அமைக்க அல்லது இருக்கும் கடையை அணுக, உங்கள் மொபைல் எண்ணை சரிபார்க்கவும்:\n\n"
            "🔒 <i>பாதுகாப்பு மற்றும் அங்கீகாரத்திற்காக, உங்கள் டெலிகிராம் கணக்குடன் இணைக்கப்பட்ட எண்ணைப் பகிரவும்:</i>\n\n"
            "👉 கீழே உள்ள <b>[📱 சரிபார்க்கப்பட்ட எண்ணைப் பகிரவும்]</b> பொத்தானைத் தட்டவும் (1-Tap instant)."
        ),
        "hi": (
            "📱 <b>मोबाइल नंबर सत्यापन</b>\n\n"
            "अपना स्टोर सेटअप करने या मौजूदा स्टोर से जुड़ने के लिए कृपया अपना मोबाइल नंबर वेरिफाई करें:\n\n"
            "🔒 <i>सुरक्षा और सत्यापन के लिए, कृपया अपने टेलीग्राम खाते से जुड़े नंबर को साझा करें:</i>\n\n"
            "👉 नीचे दिए गए <b>[📱 वेरिफाइड मोबाइल नंबर साझा करें]</b> बटन पर टैप करें (1-टैप तुरंत)।"
        ),
        "en": (
            "📱 <b>Mobile Number Verification</b>\n\n"
            "To set up your supermarket or access your existing store, please verify your mobile number:\n\n"
            "🔒 <i>For store security and fraud prevention, please verify using your official Telegram phone number:</i>\n\n"
            "👉 Tap the <b>[📱 Share Verified Mobile Number]</b> button below (1-Tap instant)."
        ),
    }
    await msg.reply_text(
        texts.get(canonical, texts["en"]),
        reply_markup=_phone_keyboard(canonical),
        parse_mode=ParseMode.HTML,
    )


async def handle_onboarding_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    registry: MasterRegistry | None = None,
) -> bool:
    """Process an incoming update through the onboarding flow.

    Returns True if the update was handled by onboarding, False if not in onboarding.
    """
    if not update.effective_chat or not update.message:
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    state = get_onboarding_session(chat_id)
    if not state:
        return False

    if registry is None:
        registry = get_master_registry()

    lang = normalize_lang(state.language)
    msg = update.message
    text = (msg.text or msg.caption or "").strip()
    text_lower = text.lower()

    # 1. Global Interception: Cancellation
    cancel_keywords = {"cancel", "/cancel", "exit", "quit", "stop", "abort", "ரத்து", "रद्द"}
    if text_lower in cancel_keywords:
        clear_onboarding_session(chat_id)
        cancel_msgs = {
            "ta": "❌ <b>கடை பதிவு ரத்து செய்யப்பட்டது.</b>\n\nபுதிய கடையை அமைக்க அல்லது தொடங்க /start என தட்டச்சு செய்யவும்.",
            "hi": "❌ <b>स्टोर सेटअप रद्द कर दिया गया है।</b>\n\nनया स्टोर शुरू करने के लिए /start टाइप करें।",
            "en": "❌ <b>Store setup cancelled.</b>\n\nType /start anytime to begin store registration again.",
        }
        await msg.reply_text(
            cancel_msgs.get(lang, cancel_msgs["en"]),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.HTML,
        )
        return True

    # 2. Global Interception: Account / Store Deletion
    deletion_keywords = {
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
    }
    if text_lower in deletion_keywords:
        clear_onboarding_session(chat_id)
        tenant = registry.get_tenant_for_chat(chat_id)
        if tenant:
            from .bot import prompt_deletion_confirmation
            await prompt_deletion_confirmation(update, context, tenant, lang)
        else:
            no_store_msgs = {
                "ta": "ℹ️ <b>கடை பதிவு ரத்து செய்யப்பட்டது.</b>\n\nஇன்னும் எந்தக் கடையும் உருவாக்கப்படவில்லை. புதிய கடையை அமைக்க /start தட்டச்சு செய்யவும்.",
                "hi": "ℹ️ <b>स्टोर सेटअप रद्द कर दिया गया है।</b>\n\nअभी तक कोई स्टोर नहीं बनाया गया था। नया स्टोर बनाने के लिए /start टाइप करें।",
                "en": "ℹ️ <b>Store registration cancelled.</b>\n\nNo store was created yet, so there is nothing to delete. Type /start anytime to begin registration.",
            }
            await msg.reply_text(
                no_store_msgs.get(lang, no_store_msgs["en"]),
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.HTML,
            )
        return True

    # --- Step 0: Awaiting Language Selection ---
    if state.step == OnboardingStep.AWAITING_LANGUAGE:
        chosen = normalize_lang(msg.text or "")
        state.language = chosen
        registry.set_user_language(chat_id, chosen)
        state.step = OnboardingStep.AWAITING_PHONE
        await prompt_phone_verification(update, chosen)
        return True

    # --- Step 1: Awaiting Phone Number ---
    if state.step == OnboardingStep.AWAITING_PHONE:
        # Case 1: 1-Tap verified Telegram contact
        if msg.contact and msg.contact.phone_number:
            # Security verification: Ensure the shared contact belongs to the current user
            if msg.contact.user_id and msg.contact.user_id != user_id:
                err = {
                    "ta": (
                        "❌ <b>பாதுகாப்பு எச்சரிக்கை:</b>\n\n"
                        "தயவுசெய்து கீழே உள்ள பொத்தானைப் பயன்படுத்தி உங்கள் சொந்த சரிபார்க்கப்பட்ட மொபைல் எண்ணைப் பகிரவும். "
                        "பிறரின் தொடர்புக் குறிப்புகளை ஏற்க முடியாது."
                    ),
                    "hi": (
                        "❌ <b>सुरक्षा चेतावनी:</b>\n\n"
                        "कृपया नीचे दिए गए बटन का उपयोग करके अपना स्वयं का सत्यापित मोबाइल नंबर साझा करें। "
                        "अन्य व्यक्तियों के संपर्क स्वीकार्य नहीं हैं।"
                    ),
                    "en": (
                        "❌ <b>Security Alert:</b>\n\n"
                        "Please share your own verified mobile number using the button below. "
                        "Contact cards belonging to someone else cannot be accepted."
                    ),
                }
                await msg.reply_text(
                    err.get(lang, err["en"]),
                    reply_markup=_phone_keyboard(lang),
                    parse_mode=ParseMode.HTML,
                )
                return True

            state.phone_e164 = normalize_phone(msg.contact.phone_number)
            await _on_phone_verified(update, state, registry)
            return True

        # Case 2: User manually typed a number or text
        # Security protection: Prevent impersonating someone else's mobile number
        manual_security_msg = {
            "ta": (
                "🔒 <b>பாதுகாப்பு சரிபார்ப்பு தேவை</b>\n\n"
                "கடையின் தரவைப் பாதுகாக்கவும், பிறர் உங்கள் எண்ணைத் தட்டச்சு செய்து உங்கள் கடையை அணுகுவதைத் தடுக்கவும், "
                "மொபைல் எண்களை கைமுறையாக தட்டச்சு செய்ய இயலாது.\n\n"
                "👉 கீழே உள்ள <b>[📱 சரிபார்க்கப்பட்ட எண்ணைப் பகிரவும்]</b> பொத்தானைத் தட்டவும்."
            ),
            "hi": (
                "🔒 <b>सुरक्षा सत्यापन आवश्यक</b>\n\n"
                "स्टोर डेटा की सुरक्षा और अनधिकृत पहुंच को रोकने के लिए, "
                "मोबाइल नंबर मैन्युअल रूप से टाइप नहीं किया जा सकता है।\n\n"
                "👉 कृपया नीचे दिए गए <b>[📱 वेरिफाइड मोबाइल नंबर साझा करें]</b> बटन पर टैप करें।"
            ),
            "en": (
                "🔒 <b>Security Verification Required</b>\n\n"
                "To protect store data and prevent anyone from accessing your store by typing your mobile number, "
                "manual phone entry is disabled.\n\n"
                "👉 Please tap the <b>[📱 Share Verified Mobile Number]</b> button below to verify your account securely."
            ),
        }
        await msg.reply_text(
            manual_security_msg.get(lang, manual_security_msg["en"]),
            reply_markup=_phone_keyboard(lang),
            parse_mode=ParseMode.HTML,
        )
        return True

    # --- Step 2: Awaiting In-Chat OTP ---
    if state.step == OnboardingStep.AWAITING_OTP:
        entered_code = re.sub(r"\D", "", msg.text or "")
        if len(entered_code) != 6:
            err = {
                "ta": "❌ தயவுசெய்து 6 இலக்க OTP குறியீட்டை மட்டும் உள்ளிடவும்.",
                "hi": "❌ कृपया केवल 6 अंकों का OTP कोड दर्ज करें।",
            }.get(lang, "❌ Please enter the 6-digit OTP code.")
            await msg.reply_text(err)
            return True

        valid, reason = registry.verify_otp(state.phone_e164, chat_id, entered_code)
        if not valid:
            fail_texts = {
                "invalid_code": {
                    "ta": "❌ தவறான குறியீடு. தயவுசெய்து மீண்டும் சரிபார்த்து உள்ளிடவும்.",
                    "hi": "❌ अमान्य कोड। कृपया दोबारा जांच कर दर्ज करें।",
                    "en": "❌ Incorrect code. Please check and try again.",
                },
                "expired": {
                    "ta": "❌ குறியீட்டின் காலாவதி முடிந்தது. /start கொடுத்து புதிய குறியீட்டைப் பெறவும்.",
                    "hi": "❌ कोड समाप्त हो गया है। नया कोड पाने के लिए /start भेजें।",
                    "en": "❌ Code expired. Send /start to request a new code.",
                },
                "max_attempts": {
                    "ta": "❌ அதிகபட்ச முயற்சிகள் முடிந்தது. /start மூலம் மீண்டும் தொடங்கவும்.",
                    "hi": "❌ अधिकतम प्रयास समाप्त। कृपया /start से पुनः प्रयास करें।",
                    "en": "❌ Too many failed attempts. Please restart with /start.",
                },
            }
            reason_dict = fail_texts.get(reason, fail_texts["invalid_code"])
            await msg.reply_text(reason_dict.get(lang, reason_dict["en"]))
            return True

        await _on_phone_verified(update, state, registry)
        return True

    # --- Step 3: Awaiting Store Name ---
    if state.step == OnboardingStep.AWAITING_STORE_NAME:
        store_name = (msg.text or "").strip()
        if len(store_name) < 2:
            await msg.reply_text("Please enter a valid store name (at least 2 characters).")
            return True
        state.store_name = store_name
        state.step = OnboardingStep.AWAITING_OWNER_NAME

        texts = {
            "ta": (
                f"🏬 கடை பெயர்: <b>{store_name}</b>\n\n"
                "👤 <b>உரிமையாளரின் பெயர் என்ன?</b>\n"
                "<i>(எ.கா: கவிதா / சுந்தரம்)</i>"
            ),
            "hi": (
                f"🏬 स्टोर का नाम: <b>{store_name}</b>\n\n"
                "👤 <b>स्टोर मालिक का नाम क्या है?</b>\n"
                "<i>(उदा: राजेश कुमार / कविता)</i>"
            ),
            "en": (
                f"🏬 Store Name: <b>{store_name}</b>\n\n"
                "👤 <b>What is the Owner's Name?</b>\n"
                "<i>(e.g., Rajesh Kumar / Kavitha)</i>"
            ),
        }
        await msg.reply_text(texts.get(lang, texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 4: Awaiting Owner Name ---
    if state.step == OnboardingStep.AWAITING_OWNER_NAME:
        owner_name = (msg.text or "").strip()
        if len(owner_name) < 2:
            await msg.reply_text("Please enter a valid owner name.")
            return True
        state.owner_name = owner_name
        state.step = OnboardingStep.AWAITING_OWNER_PASSWORD

        texts = {
            "ta": (
                f"👤 உரிமையாளர்: <b>{owner_name}</b>\n\n"
                "🔐 <b>உரிமையாளர் பாதுகாப்பு PIN / கடவுச்சொல்:</b>\n\n"
                "ஒரு பாதுகாப்பான 4-12 இலக்க PIN அல்லது கடவுச்சொல்லை உள்ளிடவும்.\n"
                "<i>(பணியாளர்களை நீக்குதல், கணக்கை நீக்குதல் போன்ற முக்கிய செயல்பாடுகளை அங்கீகரிக்க இது தேவைப்படும்.)</i>\n\n"
                "👉 <b>உங்கள் பாதுகாப்பு PIN அல்லது கடவுச்சொல்லை தட்டச்சு செய்யவும்:</b>"
            ),
            "hi": (
                f"👤 मालिक: <b>{owner_name}</b>\n\n"
                "🔐 <b>स्टोर मालिक सुरक्षा पिन / पासवर्ड सेट करें:</b>\n\n"
                "एक सुरक्षित 4 से 12 अंकों का पिन या पासवर्ड दर्ज करें।\n"
                "<i>(कर्मचारियों को हटाने या महत्वपूर्ण गतिविधियों को अधिकृत करने के लिए यह पिन आवश्यक होगा।)</i>\n\n"
                "👉 <b>अपना सुरक्षा पिन या पासवर्ड दर्ज करें:</b>"
            ),
            "en": (
                f"👤 Owner: <b>{owner_name}</b>\n\n"
                "🔐 <b>Set Store Owner Security PIN / Password:</b>\n\n"
                "Create a secure 4 to 12 digit PIN or password.\n"
                "<i>(This will be required to authorize critical actions like deleting employees, resetting store data, or sensitive operations.)</i>\n\n"
                "👉 <b>Enter your security PIN or password:</b>"
            ),
        }
        await msg.reply_text(texts.get(lang, texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 4b: Awaiting Owner Password ---
    if state.step == OnboardingStep.AWAITING_OWNER_PASSWORD:
        pin = (msg.text or "").strip()
        if len(pin) < 4:
            err = {
                "ta": "⚠️ கடவுச்சொல்/PIN குறைந்தபட்சம் 4 எழுத்துக்கள் அல்லது இலக்கங்கள் கொண்டிருக்க வேண்டும். தயவுசெய்து பாதுகாப்பான PIN-ஐ உள்ளிடவும்.",
                "hi": "⚠️ सुरक्षा पिन/पासवर्ड कम से कम 4 अक्षरों या अंकों का होना चाहिए। कृपया सुरक्षित पिन दर्ज करें।",
                "en": "⚠️ Security PIN/Password must be at least 4 characters long. Please enter a secure PIN or password (min 4 chars).",
            }
            await msg.reply_text(err.get(lang, err["en"]))
            return True

        state.owner_pin_hash = hash_password(pin)
        state.step = OnboardingStep.AWAITING_EMPLOYEE_COUNT

        texts = {
            "ta": (
                "🔒 <b>பாதுகாப்பு PIN வெற்றிகரமாக அமைக்கப்பட்டது!</b>\n\n"
                "👥 <b>கடை பணியாளர்கள் & ஊழியர்கள்:</b>\n\n"
                "உங்கள் கடையில் எத்தனை ஊழியர்கள்/காசாளர்கள் பணிபுரிகிறார்கள்?\n"
                "<i>(பில்லிங்கிற்காக EMP-001, EMP-002 போன்ற தனித்துவமான பணியாளர் குறியீடுகள் உருவாக்கப்படும்.)</i>\n\n"
                "👉 ஊழியர்களின் <b>எண்ணிக்கையை</b> உள்ளிடவும் (எ.கா: <code>2</code>), அல்லது தனியாக நிர்வகித்தால் <b>'0'</b> / <b>'skip'</b> என அனுப்பவும்."
            ),
            "hi": (
                "🔒 <b>सुरक्षा पिन सफलतापूर्वक सेट हो गया!</b>\n\n"
                "👥 <b>स्टोर कर्मचारी व कैशियर:</b>\n\n"
                "आपके स्टोर में कितने कर्मचारी या कैशियर काम करते हैं?\n"
                "<i>(बिलिंग के लिए EMP-001, EMP-002 जैसी विशिष्ट आईडी बनाई जाएगी।)</i>\n\n"
                "👉 कर्मचारियों की <b>संख्या</b> दर्ज करें (उदा: <code>2</code>), या यदि आप अकेले स्टोर चलाते हैं तो <b>'0'</b> या <b>'skip'</b> टाइप करें।"
            ),
            "en": (
                "🔒 <b>Security PIN set successfully!</b>\n\n"
                "👥 <b>Store Staff & Employees:</b>\n\n"
                "How many employees or cashiers work at your store?\n"
                "<i>(We will generate unique Employee IDs like EMP-001, EMP-002 for billing attribution.)</i>\n\n"
                "👉 Enter the <b>number of employees</b> (e.g., <code>2</code>), or type <b>'skip'</b> / <b>'0'</b> if you manage the store alone."
            ),
        }
        await msg.reply_text(texts.get(lang, texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 4c: Awaiting Employee Count ---
    if state.step == OnboardingStep.AWAITING_EMPLOYEE_COUNT:
        raw = (msg.text or "").strip()
        lowered = raw.lower()

        # Option A: Skip or 0
        if lowered in ("0", "skip", "none", "no", "illai", "இல்லை", "nahi", "नहीं", "-"):
            state.employees = []
            state.step = OnboardingStep.AWAITING_ADDRESS
            addr_texts = {
                "ta": (
                    "ℹ️ பணியாளர்கள் பதிவு தவிர்க்கப்பட்டது (பின்னரும் நீங்கள் பணியாளர்களைச் சேர்க்கலாம்).\n\n"
                    "📍 <b>கடையின் முகவரி & ஊர் என்ன?</b>\n"
                    "<i>(ரசீது மற்றும் பில்லிங்கில் அச்சிடப்படும். எ.கா: 12/4 காந்தி ரோடு, மதுரை)</i>"
                ),
                "hi": (
                    "ℹ️ कर्मचारी पंजीकरण छोड़ दिया गया (आप बाद में भी कर्मचारी जोड़ सकते हैं)।\n\n"
                    "📍 <b>स्टोर का पता व शहर क्या है?</b>\n"
                    "<i>(बिल व टैक्स इनवॉइस पर प्रिंट होगा। उदा: 12/4 गांधी रोड, जयपुर)</i>"
                ),
                "en": (
                    "ℹ️ Staff registration skipped (you can add employees anytime later via chat).\n\n"
                    "📍 <b>What is the Store Address & City?</b>\n"
                    "<i>(This will appear on GST invoices & receipts. e.g., 12/4 Gandhi Road, Chennai)</i>"
                ),
            }
            await msg.reply_text(addr_texts.get(lang, addr_texts["en"]), parse_mode=ParseMode.HTML)
            return True

        # Option B: Directly typed names (e.g. "Ramesh, Priya")
        parsed_names = _parse_employee_names(raw)
        if not raw.isdigit() and len(parsed_names) >= 1:
            state.employees = []
            for i, name in enumerate(parsed_names, 1):
                state.employees.append({
                    "emp_code": f"EMP-{i:03d}",
                    "name": name,
                    "role": "cashier",
                    "phone": None,
                })
            state.step = OnboardingStep.AWAITING_ADDRESS
            emp_lines = "\n".join(f"• <b>{e['emp_code']}</b>: {e['name']} (Cashier)" for e in state.employees)
            confirm_texts = {
                "ta": (
                    f"✅ <b>{len(state.employees)} பணியாளர்கள் பதிவு செய்யப்பட்டனர்:</b>\n{emp_lines}\n\n"
                    "📍 <b>கடையின் முகவரி & ஊர் என்ன?</b>\n"
                    "<i>(ரசீது மற்றும் பில்லிங்கில் அச்சிடப்படும். எ.கா: 12/4 காந்தி ரோடு, மதுரை)</i>"
                ),
                "hi": (
                    f"✅ <b>{len(state.employees)} कर्मचारी सफलतापूर्वक जोड़े गए:</b>\n{emp_lines}\n\n"
                    "📍 <b>स्टोर का पता व शहर क्या है?</b>\n"
                    "<i>(बिल व टैक्स इनवॉइस पर प्रिंट होगा। उदा: 12/4 गांधी रोड, जयपुर)</i>"
                ),
                "en": (
                    f"✅ <b>{len(state.employees)} Employee(s) Registered:</b>\n{emp_lines}\n\n"
                    "📍 <b>What is the Store Address & City?</b>\n"
                    "<i>(This will appear on GST invoices & receipts. e.g., 12/4 Gandhi Road, Chennai)</i>"
                ),
            }
            await msg.reply_text(confirm_texts.get(lang, confirm_texts["en"]), parse_mode=ParseMode.HTML)
            return True

        # Option C: Count specified
        try:
            count = int(re.sub(r"\D", "", raw))
        except (ValueError, TypeError):
            count = -1

        if count <= 0 or count > 50:
            err = {
                "ta": "⚠️ தயவுசெய்து சரியான பணியாளர்களின் எண்ணிக்கையை உள்ளிடவும் (1-50), அல்லது '0'/'skip' என அனுப்பவும்.",
                "hi": "⚠️ कृपया कर्मचारियों की सही संख्या दर्ज करें (1-50), या '0'/'skip' टाइप करें।",
                "en": "⚠️ Please enter a valid number of employees (1-50), or type 'skip' / '0'.",
            }
            await msg.reply_text(err.get(lang, err["en"]))
            return True

        state.target_employee_count = count
        state.step = OnboardingStep.AWAITING_EMPLOYEE_NAMES

        prompt_names = {
            "ta": (
                f"👥 <b>{count} பணியாளர்களின் பெயர்களை உள்ளிடவும்:</b>\n\n"
                f"பணியாளர்களின் பெயர்களை கமா (,) அல்லது புதிய வரியில் பிரித்து அனுப்பவும்:\n"
                f"<i>(எ.கா: ரமேஷ், பிரியா)</i>"
            ),
            "hi": (
                f"👥 <b>{count} कर्मचारियों के नाम दर्ज करें:</b>\n\n"
                f"कृपया कर्मचारियों के नाम कॉमा (,) या नई लाइन में लिखकर भेजें:\n"
                f"<i>(उदा: रमेश, प्रिया)</i>"
            ),
            "en": (
                f"👥 <b>Enter Names for {count} Employee(s):</b>\n\n"
                f"Please enter their names separated by commas or on new lines:\n"
                f"<i>(e.g., Ramesh, Priya)</i>"
            ),
        }
        await msg.reply_text(prompt_names.get(lang, prompt_names["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 4d: Awaiting Employee Names ---
    if state.step == OnboardingStep.AWAITING_EMPLOYEE_NAMES:
        raw = (msg.text or "").strip()
        names = _parse_employee_names(raw)
        if not names:
            err = {
                "ta": "⚠️ தயவுசெய்து குறைந்தபட்சம் ஒரு பணியாளரின் பெயரை உள்ளிடவும், அல்லது 'skip' என அனுப்பவும்.",
                "hi": "⚠️ कृपया कम से कम एक कर्मचारी का नाम दर्ज करें, या 'skip' टाइप करें।",
                "en": "⚠️ Please enter at least one employee name, or type 'skip'.",
            }
            await msg.reply_text(err.get(lang, err["en"]))
            return True

        state.employees = []
        for i, name in enumerate(names, 1):
            state.employees.append({
                "emp_code": f"EMP-{i:03d}",
                "name": name,
                "role": "cashier",
                "phone": None,
            })

        state.step = OnboardingStep.AWAITING_ADDRESS
        emp_lines = "\n".join(f"• <b>{e['emp_code']}</b>: {e['name']} (Cashier)" for e in state.employees)
        confirm_texts = {
            "ta": (
                f"✅ <b>{len(state.employees)} பணியாளர்கள் பதிவு செய்யப்பட்டனர்:</b>\n{emp_lines}\n\n"
                "📍 <b>கடையின் முகவரி & ஊர் என்ன?</b>\n"
                "<i>(ரசீது மற்றும் பில்லிங்கில் அச்சிடப்படும். எ.கா: 12/4 காந்தி ரோடு, மதுரை)</i>"
            ),
            "hi": (
                f"✅ <b>{len(state.employees)} कर्मचारी सफलतापूर्वक जोड़े गए:</b>\n{emp_lines}\n\n"
                "📍 <b>स्टोर का पता व शहर क्या है?</b>\n"
                "<i>(बिल व टैक्स इनवॉइस पर प्रिंट होगा। उदा: 12/4 गांधी रोड, जयपुर)</i>"
            ),
            "en": (
                f"✅ <b>{len(state.employees)} Employee(s) Registered:</b>\n{emp_lines}\n\n"
                "📍 <b>What is the Store Address & City?</b>\n"
                "<i>(This will appear on GST invoices & receipts. e.g., 12/4 Gandhi Road, Chennai)</i>"
            ),
        }
        await msg.reply_text(confirm_texts.get(lang, confirm_texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 5: Awaiting Address ---
    if state.step == OnboardingStep.AWAITING_ADDRESS:
        address = (msg.text or "").strip()
        if len(address) < 3:
            await msg.reply_text("Please enter a valid address.")
            return True
        state.address = address
        state.step = OnboardingStep.AWAITING_GSTIN

        texts = {
            "ta": (
                f"📍 முகவரி: <b>{address}</b>\n\n"
                "🏷️ <b>GSTIN உள்ளதா?</b>\n"
                "உங்களிடம் GSTIN எண் இருந்தால் அதை தட்டச்சு செய்யவும், இல்லையென்றால் <b>'skip'</b> அல்லது <b>'இல்லை'</b> என அனுப்பவும்."
            ),
            "hi": (
                f"📍 पता: <b>{address}</b>\n\n"
                "🏷️ <b>क्या आपके पास GSTIN नंबर है?</b>\n"
                "यदि है तो दर्ज करें, अन्यथा <b>'skip'</b> या <b>'नहीं'</b> भेजें।"
            ),
            "en": (
                f"📍 Address: <b>{address}</b>\n\n"
                "🏷️ <b>Do you have a GSTIN number?</b>\n"
                "If yes, enter your 15-character GSTIN. Otherwise, type <b>'skip'</b>."
            ),
        }
        await msg.reply_text(texts.get(lang, texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 6: Awaiting GSTIN -> Advance to Logo ---
    if state.step == OnboardingStep.AWAITING_GSTIN:
        raw_text = (msg.text or "").strip()
        clean_gstin = re.sub(r"\s+", "", raw_text).upper()

        if raw_text.lower() in ("skip", "illai", "இல்லை", "nahi", "नहीं", "none", "no", "-"):
            state.gstin = None
            state.step = OnboardingStep.AWAITING_LOGO
        elif is_valid_gstin(clean_gstin):
            state.gstin = clean_gstin
            state.step = OnboardingStep.AWAITING_LOGO
        else:
            invalid_gstin_msgs = {
                "ta": (
                    "⚠️ <b>தவறான ஜிஎஸ்டி எண் (Invalid GSTIN):</b>\n\n"
                    "இந்திய ஜிஎஸ்டி எண் 15 எழுத்து/எண்களைக் கொண்டிருக்க வேண்டும் (எ.கா: <code>33AAAAA0000A1Z5</code>).\n\n"
                    "👉 சரியான ஜிஎஸ்டி எண்ணை உள்ளிடவும், அல்லது உங்களிடம் ஜிஎஸ்டி இல்லையென்றால் <b>'skip'</b> அல்லது <b>'இல்லை'</b> என தட்டச்சு செய்யவும்."
                ),
                "hi": (
                    "⚠️ <b>अमान्य जीएसटी नंबर (Invalid GSTIN):</b>\n\n"
                    "भारतीय जीएसटी नंबर 15 अक्षरों/अंकों का होना चाहिए (उदा: <code>33AAAAA0000A1Z5</code>)।\n\n"
                    "👉 कृपया सही GSTIN दर्ज करें, या यदि आपके पास नहीं है तो <b>'skip'</b> या <b>'नहीं'</b> टाइप करें।"
                ),
                "en": (
                    "⚠️ <b>Invalid GSTIN Format:</b>\n\n"
                    "An Indian GSTIN must be a 15-character alphanumeric code (e.g., <code>33AAAAA0000A1Z5</code>).\n\n"
                    "👉 Please enter a valid 15-digit GSTIN, or type <b>'skip'</b> if you do not have one."
                ),
            }
            await msg.reply_text(
                invalid_gstin_msgs.get(lang, invalid_gstin_msgs["en"]),
                parse_mode=ParseMode.HTML,
            )
            return True

        gstin = state.gstin
        gstin_note = f"🏷️ GSTIN: <b>{gstin}</b>\n\n" if gstin else "🏷️ GSTIN: <i>(Skipped / தவிர்க்கப்பட்டது / छोड़ दिया)</i>\n\n"

        texts = {
            "ta": (
                f"{gstin_note}"
                "🖼️ <b>கடையின் லோகோ (கட்டாயமானது):</b>\n\n"
                "உங்கள் **கடையின் லோகோவை** (புகைப்படம் அல்லது படமாக) அனுப்பவும். "
                "கடை அமைப்பை முடிக்க லோகோ பதிவேற்றம் கட்டாயமாகும். இது உங்கள் அனைத்து ஜிஎஸ்டி பில்கள், ரசீதுகள் மற்றும் விற்பனை அறிக்கைகளில் நேர்த்தியாக அச்சிடப்படும்!\n\n"
                "👉 உங்கள் லோகோ படத்தை (<b>PNG, JPG அல்லது WEBP</b>) இப்போது அனுப்பவும்."
            ),
            "hi": (
                f"{gstin_note}"
                "🖼️ <b>स्टोर लोगो (अनिवार्य):</b>\n\n"
                "कृपया अपने **स्टोर का लोगो** (फ़ोटो या इमेज फ़ाइल) भेजें। "
                "स्टोर सेटअप पूरा करने के लिए लोगो अपलोड अनिवार्य है। यह आपके सभी जीएसटी इनवॉइस, बिल रसीदों और रिपोर्टों पर दिखेगा!\n\n"
                "👉 अपने लोगो की इमेज (<b>PNG, JPG या WEBP</b>) अभी भेजें।"
            ),
            "en": (
                f"{gstin_note}"
                "🖼️ <b>Store Logo (Mandatory):</b>\n\n"
                "Please upload your **Store Logo** (send a photo or image file). "
                "Uploading a store logo is mandatory to proceed with store setup. This will appear on all your printed GST invoices, receipts, and reports!\n\n"
                "👉 Send your logo image now (<b>PNG, JPG, or WEBP</b>)."
            ),
        }
        await msg.reply_text(texts.get(lang, texts["en"]), parse_mode=ParseMode.HTML)
        return True

    # --- Step 7: Awaiting Logo (Mandatory) & Provision Tenant Store Database ---
    if state.step == OnboardingStep.AWAITING_LOGO:
        logo_path: str | None = None

        # Case 1: User sent Photo
        if msg.photo:
            photo = msg.photo[-1]
            photo_file = await context.bot.get_file(photo.file_id)
            stream = io.BytesIO()
            await photo_file.download_to_memory(out=stream)
            stream.seek(0)

            clean_phone = re.sub(r"\D", "", state.phone_e164) or "store"
            dest_logo = PROJECT_ROOT / "data" / "stores" / "logos" / f"logo_{clean_phone}.png"
            res = validate_and_process_logo(stream, dest_logo)
            if not res.is_valid:
                await msg.reply_text(res.get_message(lang), parse_mode=ParseMode.HTML)
                return True
            logo_path = res.saved_path

        # Case 2: User sent Document (image file or non-image document)
        elif msg.document:
            doc = msg.document
            if doc.file_size and doc.file_size > 5 * 1024 * 1024:
                size_mb = doc.file_size / (1024 * 1024)
                err_size = {
                    "ta": f"⚠️ கோப்பின் அளவு மிக அதிகம் ({size_mb:.1f} MB). லோகோ 5 MB-க்குள் இருக்க வேண்டும். தயவுசெய்து சிறிய படத்தை அனுப்பவும்.",
                    "hi": f"⚠️ फ़ाइल का आकार बहुत बड़ा है ({size_mb:.1f} MB)। लोगो 5 MB से कम होना चाहिए। कृपया छोटी इमेज भेजें।",
                    "en": f"⚠️ The file is too large ({size_mb:.1f} MB). Logos must be under 5 MB. Please upload a smaller image file.",
                }
                await msg.reply_text(err_size.get(lang, err_size["en"]))
                return True

            doc_file = await context.bot.get_file(doc.file_id)
            stream = io.BytesIO()
            await doc_file.download_to_memory(out=stream)
            stream.seek(0)

            clean_phone = re.sub(r"\D", "", state.phone_e164) or "store"
            dest_logo = PROJECT_ROOT / "data" / "stores" / "logos" / f"logo_{clean_phone}.png"
            res = validate_and_process_logo(stream, dest_logo)
            if not res.is_valid:
                await msg.reply_text(res.get_message(lang), parse_mode=ParseMode.HTML)
                return True
            logo_path = res.saved_path

        # Case 3: User sent Audio / Voice / Video
        elif msg.voice or msg.audio or msg.video or msg.video_note:
            media_type = "voice message" if msg.voice else ("audio recording" if msg.audio else "video")
            err_media = {
                "ta": (
                    f"⚠️ <b>தவறான கோப்பு:</b> நீங்கள் {media_type} அனுப்பியுள்ளீர்கள்.\n\n"
                    "கடை பதிவை முடிக்க லோகோ படம் (<b>PNG, JPG அல்லது WEBP</b>) கட்டாயமாக பதிவேற்றப்பட வேண்டும்.\n\n"
                    "👉 தயவுசெய்து லோகோ படத்தை அனுப்பவும்."
                ),
                "hi": (
                    f"⚠️ <b>अमान्य फ़ाइल:</b> आपने {media_type} भेजा है।\n\n"
                    "स्टोर सेटअप पूरा करने के लिए लोगो इमेज (<b>PNG, JPG या WEBP</b>) अपलोड करना अनिवार्य है।\n\n"
                    "👉 कृपया लोगो की इमेज भेजें।"
                ),
                "en": (
                    f"⚠️ <b>Invalid media:</b> We received a {media_type}.\n\n"
                    "Uploading a store logo image (<b>PNG, JPG, or WEBP</b>) is mandatory to proceed.\n\n"
                    "👉 Please upload a photo or image file of your store logo."
                ),
            }
            await msg.reply_text(err_media.get(lang, err_media["en"]), parse_mode=ParseMode.HTML)
            return True

        # Case 4: User sent Text (e.g. skip attempt or regular text) -> Reject and demand logo
        elif msg.text:
            prompt_mandatory = {
                "ta": (
                    "⚠️ <b>லோகோ பதிவேற்றம் கட்டாயமானது:</b>\n\n"
                    "கடையை அமைத்து அடுத்த நிலைக்குச் செல்ல லோகோ பதிவேற்றம் அவசியமாகும் (skip செய்ய முடியாது).\n\n"
                    "👉 தயவுசெய்து உங்கள் கடையின் லோகோ படத்தை (<b>PNG, JPG அல்லது WEBP</b>) அனுப்பவும்."
                ),
                "hi": (
                    "⚠️ <b>लोगो अपलोड अनिवार्य है:</b>\n\n"
                    "स्टोर सेटअप पूरा करके आगे बढ़ने के लिए लोगो अपलोड करना अनिवार्य है (skip नहीं किया जा सकता)।\n\n"
                    "👉 कृपया अपने स्टोर का लोगो इमेज (<b>PNG, JPG या WEBP</b>) भेजें।"
                ),
                "en": (
                    "⚠️ <b>Store Logo is Required to Proceed:</b>\n\n"
                    "Uploading a store logo is mandatory in order to complete store setup (cannot be skipped).\n\n"
                    "👉 Please upload a photo or image file (<b>PNG, JPG, or WEBP</b>) of your store logo."
                ),
            }
            await msg.reply_text(prompt_mandatory.get(lang, prompt_mandatory["en"]), parse_mode=ParseMode.HTML)
            return True

        if not logo_path:
            return True

        state.logo_path = logo_path


        # Provision Tenant Store Database
        tenant = registry.register_tenant(
            store_name=state.store_name,
            owner_name=state.owner_name,
            phone_e164=state.phone_e164,
            address=state.address,
            gstin=state.gstin,
            tagline=f"Welcome to {state.store_name}!",
            logo_path=state.logo_path,
            preferred_language=lang,
            owner_pin_hash=state.owner_pin_hash,
            employees=state.employees,
        )

        # Link chat to newly created tenant
        registry.link_chat_to_tenant(
            chat_id=chat_id,
            user_id=user_id,
            tenant_id=tenant.id,
            role="owner",
            language=lang,
        )

        clear_onboarding_session(chat_id)

        gstin_line = f"🏷️ <b>GSTIN:</b> {tenant.gstin}\n" if tenant.gstin else ""
        logo_line = "🖼️ <b>லோகோ:</b> இணைக்கப்பட்டது ✅\n" if state.logo_path else ""
        logo_line_hi = "🖼️ <b>लोगो:</b> सेट हो गया ✅\n" if state.logo_path else ""
        logo_line_en = "🖼️ <b>Store Logo:</b> Configured & Ready ✅\n" if state.logo_path else ""

        emp_summary_ta = ""
        emp_summary_hi = ""
        emp_summary_en = ""
        if state.employees:
            emp_list_str = ", ".join(f"<code>{e['emp_code']}</code> {e['name']}" for e in state.employees)
            emp_summary_ta = f"👥 <b>பணியாளர்கள்:</b> {emp_list_str}\n"
            emp_summary_hi = f"👥 <b>कर्मचारी:</b> {emp_list_str}\n"
            emp_summary_en = f"👥 <b>Staff:</b> {emp_list_str}\n"

        # Celebratory completion message with quickstart commands
        congrats = {
            "ta": (
                f"🎉 <b>உங்கள் அங்காடி வெற்றிகரமாக உருவாக்கப்பட்டது!</b>\n\n"
                f"🏪 <b>கடை:</b> {tenant.store_name}\n"
                f"🔑 <b>கடை குறியீடு (Store Code):</b> <code>{tenant.store_code}</code>\n"
                f"📱 <b>மொபைல்:</b> {tenant.phone_e164}\n"
                f"📍 <b>முகவரி:</b> {tenant.address}\n"
                f"{gstin_line}"
                f"{logo_line}"
                f"{emp_summary_ta}"
                f"💾 <i>தனிப்பட்ட தரவுத்தளம் (Isolated Database) தயாராக உள்ளது.</i>\n\n"
                "<b>இப்போது நீங்கள் தொடங்கலாம்:</b>\n"
                "• <b>பில்லிங்</b>: <i>\"2 கிலோ சர்க்கரை, 1 மேகி, UPI பில் போடு\"</i>\n"
                "• <b>சரக்கு இருப்பு</b>: <i>\"கடையில அரிசி எவ்வளவு இருக்கு?\"</i>\n"
                "• <b>கடன் கணக்கு</b>: <i>\"ரமேஷ் கணக்கில் ₹500 கடன் சேர்\"</i>\n"
                "• <b>அறிக்கை</b>: <i>\"இன்றைய விற்பனை என்ன?\"</i>"
            ),
            "hi": (
                f"🎉 <b>आपका सुपरमार्केट सफलतापूर्वक सेट हो गया है!</b>\n\n"
                f"🏪 <b>स्टोर:</b> {tenant.store_name}\n"
                f"🔑 <b>स्टोर कोड (Store Code):</b> <code>{tenant.store_code}</code>\n"
                f"📱 <b>मोबाइल:</b> {tenant.phone_e164}\n"
                f"📍 <b>पता:</b> {tenant.address}\n"
                f"{gstin_line}"
                f"{logo_line_hi}"
                f"{emp_summary_hi}"
                f"💾 <i>आपका व्यक्तिगत डेटाबेस सुरक्षित रूप से तैयार है।</i>\n\n"
                "<b>आप तुरंत शुरू कर सकते हैं:</b>\n"
                "• <b>बिल बनाएं</b>: <i>\"2 किलो चीनी, 1 आशीर्वाद आटा, UPI बिल बनाओ\"</i>\n"
                "• <b>स्टॉक जांचें</b>: <i>\"चावल का कितना स्टॉक है?\"</i>\n"
                "• <b>खाता उधारी</b>: <i>\"रमेश के खाते में ₹500 उधार लिखो\"</i>\n"
                "• <b>रिपोर्ट</b>: <i>\"आज की बिक्री दिखाओ\"</i>"
            ),
            "en": (
                f"🎉 <b>Your Supermarket is Successfully Set Up!</b>\n\n"
                f"🏪 <b>Store:</b> {tenant.store_name}\n"
                f"🔑 <b>Store Code:</b> <code>{tenant.store_code}</code>\n"
                f"📱 <b>Mobile:</b> {tenant.phone_e164}\n"
                f"📍 <b>Address:</b> {tenant.address}\n"
                f"{gstin_line}"
                f"{logo_line_en}"
                f"{emp_summary_en}"
                f"💾 <i>Dedicated store database provisioned & isolated.</i>\n\n"
                "<b>You can start right away:</b>\n"
                "• <b>Billing</b>: <i>\"2kg sugar, 1 Aashirvaad atta 5kg, UPI\"</i>\n"
                "• <b>Stock</b>: <i>\"What is the stock of rice?\"</i>\n"
                "• <b>Khata</b>: <i>\"Put ₹500 on Ramesh's credit\"</i>\n"
                "• <b>Reports</b>: <i>\"Today's sales?\"</i> or <i>\"Send me that bill as PDF\"</i>"
            ),
        }
        await msg.reply_text(congrats.get(lang, congrats["en"]), parse_mode=ParseMode.HTML)
        # Display the newly seeded store inventory immediately!
        store_repo = registry.get_store_repo(tenant.store_code)
        inv_msg = format_inventory_message(store_repo, lang)
        if inv_msg:
            await msg.reply_text(inv_msg, parse_mode=ParseMode.HTML)
        return True


    return False


async def _on_phone_verified(update: Update, state: OnboardingState, registry: MasterRegistry) -> None:
    """Triggered immediately when phone is verified. Branches to Login or New Store Registration."""
    chat_id = state.chat_id
    user_id = state.user_id
    lang = normalize_lang(state.language)
    msg = update.message
    assert msg is not None

    # Check if this phone number is already registered to an existing store
    existing_tenant = registry.get_tenant_by_phone(state.phone_e164)

    if existing_tenant:
        # Existing Store Owner -> Login & Link device!
        registry.link_chat_to_tenant(
            chat_id=chat_id,
            user_id=user_id,
            tenant_id=existing_tenant.id,
            role="owner",
            language=lang,
        )
        clear_onboarding_session(chat_id)

        welcome_back = {
            "ta": (
                f"🎉 <b>மீண்டும் வருக, {existing_tenant.owner_name}!</b>\n\n"
                f"🏪 <b>கடை:</b> {existing_tenant.store_name} (<code>{existing_tenant.store_code}</code>)\n"
                f"📍 <b>முகவரி:</b> {existing_tenant.address}\n\n"
                "உங்கள் பழைய கணக்கு, இருப்பு மற்றும் பில்லிங் தரவு அனைத்தும் இணைக்கப்பட்டு தயாராக உள்ளது!"
            ),
            "hi": (
                f"🎉 <b>वापसी पर स्वागत है, {existing_tenant.owner_name}!</b>\n\n"
                f"🏪 <b>स्टोर:</b> {existing_tenant.store_name} (<code>{existing_tenant.store_code}</code>)\n"
                f"📍 <b>पता:</b> {existing_tenant.address}\n\n"
                "आपका पुराना खाता, स्टॉक और बिलिंग डेटा सुरक्षित रूप से कनेक्ट हो गया है!"
            ),
            "en": (
                f"🎉 <b>Welcome back, {existing_tenant.owner_name}!</b>\n\n"
                f"🏪 <b>Store:</b> {existing_tenant.store_name} (<code>{existing_tenant.store_code}</code>)\n"
                f"📍 <b>Address:</b> {existing_tenant.address}\n\n"
                "Your existing store catalog, stock, and credit ledgers are connected and ready!"
            ),
        }
        await msg.reply_text(
            welcome_back.get(lang, welcome_back["en"]),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.HTML,
        )
        # Display current inventory for returning owner!
        store_repo = registry.get_store_repo(existing_tenant.store_code)
        inv_msg = format_inventory_message(store_repo, lang)
        if inv_msg:
            await msg.reply_text(inv_msg, parse_mode=ParseMode.HTML)
        return

    # New Store Owner -> Advance to Store Questionnaire Step
    state.step = OnboardingStep.AWAITING_STORE_NAME
    texts = {
        "ta": (
            f"✅ மொபைல் எண் <b>{state.phone_e164}</b> சரிபார்க்கப்பட்டது!\n\n"
            "🏪 <b>உங்கள் கடையின் பெயர் என்ன?</b>\n"
            "<i>(எ.கா: ஸ்ரீ கிருஷ்ணா சூப்பர்மார்க்கெட் / அன்னபூர்ணா ஸ்டோர்ஸ்)</i>"
        ),
        "hi": (
            f"✅ मोबाइल नंबर <b>{state.phone_e164}</b> सत्यापित हो गया!\n\n"
            "🏪 <b>आपके स्टोर का नाम क्या है?</b>\n"
            "<i>(उदा: श्री कृष्णा सुपरमार्केट / अन्नपूर्णा किराना)</i>"
        ),
        "en": (
            f"✅ Mobile number <b>{state.phone_e164}</b> verified!\n\n"
            "🏪 <b>What is your Store Name?</b>\n"
            "<i>(e.g., Sri Krishna Supermarket / Daily Needs Mart)</i>"
        ),
    }
    await msg.reply_text(
        texts.get(lang, texts["en"]),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML,
    )
