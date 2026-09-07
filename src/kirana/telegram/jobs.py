"""Scheduled jobs: a weekly sales-analysis deck and daily khata reminders,
pushed to the owner's chat(s) via the bot's JobQueue. Neither calls the LLM —
the deck is rendered from the DB and the reminder is plain text.
"""

from __future__ import annotations

import logging
from datetime import date, time, timedelta

from telegram.ext import Application, ContextTypes

from ..artifacts.analysis_deck import render_deck
from ..config import DECK_HOUR, DECK_WEEKDAY, OWNER_CHAT_ID, REMINDER_HOUR
from ..db.connection import IST, today_iso
from ..domain import money
from ..tools.context import get_repo

log = logging.getLogger("kirana.jobs")


def target_chat_ids(repo) -> list[int]:
    if OWNER_CHAT_ID is not None:
        return [OWNER_CHAT_ID]
    return repo.known_chat_ids()


def khata_reminder_text(repo) -> str | None:
    """A summary of who owes money, or None if there's nothing outstanding."""
    debtors = repo.list_khata_debtors()
    if not debtors:
        return None
    lines = ["📒 <b>Khata reminders</b> — outstanding balances:"]
    for s in debtors[:20]:
        lines.append(f"• {s.customer.name}: {money.format_inr(s.balance_paise)}")
    total = sum(s.balance_paise for s in debtors)
    lines.append(f"<b>Total outstanding: {money.format_inr(total)}</b>")
    return "\n".join(lines)


async def _send_weekly_deck(context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..tools.context import current_chat
    repo_default = get_repo()
    chats = target_chat_ids(repo_default)
    if not chats:
        return
    end = today_iso()
    start = (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    from ..db.master import get_master_registry
    registry = get_master_registry()

    for chat_id in chats:
        tenant = registry.get_tenant_for_chat(chat_id)
        if not tenant:
            continue
        token = current_chat.set(chat_id)
        try:
            repo = get_repo()
            path = render_deck(repo, start, end, title="Weekly Sales Analysis")
            with open(path, "rb") as fh:
                await context.bot.send_document(
                    chat_id=chat_id, document=fh,
                    caption=f"📊 Weekly analysis ({start} to {end})")
        except Exception as exc:  # noqa: BLE001
            log.error("weekly deck send to %s failed: %s", chat_id, exc)
        finally:
            current_chat.reset(token)


async def _send_khata_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..db.master import get_master_registry
    from ..tools.context import current_chat
    registry = get_master_registry()
    repo_default = get_repo()
    chats = target_chat_ids(repo_default)
    for chat_id in chats:
        tenant = registry.get_tenant_for_chat(chat_id)
        if not tenant:
            continue
        token = current_chat.set(chat_id)
        try:
            repo = get_repo()
            text = khata_reminder_text(repo)
            if text is not None:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001
            log.error("khata reminder send to %s failed: %s", chat_id, exc)
        finally:
            current_chat.reset(token)


async def _nightly_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..config import BACKUP_CHAT_ID
    from ..services.backup import upload_backup_to_telegram

    if not BACKUP_CHAT_ID:
        return
    try:
        await upload_backup_to_telegram(
            context.bot,
            BACKUP_CHAT_ID,
            pin=True,
            caption_prefix="📦 #kirana_backup [Nightly Schedule]",
        )
        log.info("Nightly backup uploaded and pinned to chat %s", BACKUP_CHAT_ID)
    except Exception as exc:  # noqa: BLE001
        log.error("Nightly backup failed: %s", exc, exc_info=True)


def register_jobs(application: Application) -> None:
    from ..config import BACKUP_HOUR

    jq = application.job_queue
    if jq is None:
        log.warning(
            "JobQueue unavailable — install python-telegram-bot[job-queue]. "
            "Scheduled deck/reminders disabled."
        )
        return
    jq.run_daily(
        _send_weekly_deck,
        time=time(hour=DECK_HOUR, tzinfo=IST),
        days=(DECK_WEEKDAY,),
        name="weekly_deck",
    )
    jq.run_daily(
        _send_khata_reminders,
        time=time(hour=REMINDER_HOUR, tzinfo=IST),
        name="khata_reminders",
    )
    jq.run_daily(
        _nightly_backup,
        time=time(hour=BACKUP_HOUR, tzinfo=IST),
        name="nightly_backup",
    )
    log.info(
        "Scheduled jobs registered: weekly deck (day=%d %02d:00 IST), "
        "khata reminders (daily %02d:00 IST), nightly backup (%02d:00 IST).",
        DECK_WEEKDAY,
        DECK_HOUR,
        REMINDER_HOUR,
        BACKUP_HOUR,
    )
