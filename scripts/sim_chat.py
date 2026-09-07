"""Local Telegram-less REPL to drive the real agent end-to-end.

    uv run python scripts/sim_chat.py

Type messages like you would to the bot. Commands: /new (reset session), /quit.
Any generated PDF/PPTX files are listed with their paths. Requires GEMINI_API_KEY.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kirana.config import GEMINI_API_KEY, ensure_dirs  # noqa: E402
from kirana.agent.session import reset_session, run_turn  # noqa: E402
from kirana.tools.context import init_context  # noqa: E402

CHAT_ID = 42  # a single synthetic chat for the REPL


async def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY in .env first (get free key from https://aistudio.google.com/).")
    ensure_dirs()
    init_context()
    print("🛒 Kirana Ops Agent REPL")
    print("Type a message, '/new' to reset, '/quit' to exit.")
    print("\n🌐 Choose your preferred language / உங்கள் மொழியைத் தேர்ந்தெடுக்கவும்:")
    print("   1. English | 2. தமிழ் (Tamil / Tanglish) | 3. हिंदी (Hindi / Hinglish)\n")
    loop = asyncio.get_event_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, input, "owner> ")
        except (EOFError, KeyboardInterrupt):
            break
        text = text.strip()
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/new":
            reset_session(CHAT_ID)
            print("bot> (started a fresh chat — stock, khata & settings remembered)")
            print("🌐 Choose language: 1. English | 2. தமிழ் (Tamil / Tanglish) | 3. हिंदी (Hindi / Hinglish)\n")
            continue
        res = await run_turn(CHAT_ID, text)
        print(f"bot> {res.reply}")
        for f in res.files:
            print(f"     📎 {f.caption or 'file'}: {f.path}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
