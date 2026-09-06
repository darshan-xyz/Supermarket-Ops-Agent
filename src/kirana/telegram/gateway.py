"""Telegram Gateway API client for sending verification codes to phone numbers.

Uses the official Telegram Gateway API (gateway.telegram.org) to send
official Telegram verification codes directly to a phone number.
"""

from __future__ import annotations

import logging
import httpx

from ..config import TELEGRAM_GATEWAY_TOKEN

logger = logging.getLogger("kirana.gateway")

GATEWAY_API_URL = "https://gatewayapi.telegram.org/sendVerificationMessage"


async def send_verification_code_to_phone(
    phone_number: str,
    code: str,
    ttl: int = 300,
) -> tuple[bool, str]:
    """Send an official Telegram verification code to a phone number.

    If TELEGRAM_GATEWAY_TOKEN is configured in environment/.env, sends directly
    to the user's phone number via Telegram Gateway.
    If not configured, operates in local development mode and logs to server.
    """
    if not TELEGRAM_GATEWAY_TOKEN:
        logger.info(
            "[DEV MODE] Telegram Gateway token not set. Verification code for %s is %s",
            phone_number,
            code,
        )
        return True, "dev_mode"

    headers = {
        "Authorization": f"Bearer {TELEGRAM_GATEWAY_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "phone_number": phone_number,
        "code": code,
        "ttl": ttl,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(GATEWAY_API_URL, headers=headers, json=payload)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                req_id = data.get("result", {}).get("request_id", "")
                logger.info("Telegram Gateway sent OTP to %s (request_id=%s)", phone_number, req_id)
                return True, str(req_id)
            err = data.get("description") or resp.text
            logger.error("Telegram Gateway error for %s: %s", phone_number, err)
            return False, str(err)
    except Exception as exc:
        logger.error("Failed to connect to Telegram Gateway: %s", exc)
        return False, str(exc)
