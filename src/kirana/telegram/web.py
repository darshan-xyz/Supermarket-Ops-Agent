"""Lightweight, native asyncio HTTP server for free cloud hosting (Render.com, etc.).

Runs concurrently with Telegram long-polling on the same asyncio event loop.
Serves:
  - GET /health, GET /ping: Fast 200 OK for uptime monitoring (UptimeRobot, cron-job.org).
  - GET /: Operational dashboard JSON showing bot uptime, registered tenants, and DB status.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any

from ..config import MODEL, PORT

logger = logging.getLogger(__name__)

_START_TIME = time.time()


def get_uptime_seconds() -> int:
    return int(time.time() - _START_TIME)


def _get_system_status() -> dict[str, Any]:
    """Gather live health and tenancy metrics."""
    stores_count = 0
    try:
        from ..db.master import get_master_registry

        registry = get_master_registry()
        stores_count = len(registry.list_all_tenants())
    except Exception as exc:
        logger.debug("Failed to query stores count for status page: %s", exc)

    return {
        "service": "kirana-ops-agent",
        "status": "healthy",
        "uptime_seconds": get_uptime_seconds(),
        "model": MODEL,
        "stores_count": stores_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle an incoming HTTP request."""
    try:
        data = await asyncio.wait_for(reader.read(2048), timeout=5.0)
        if not data:
            return

        request_line = data.decode("utf-8", errors="ignore").split("\r\n")[0]
        parts = request_line.split(" ")
        method = parts[0].upper() if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        # Strip query parameters if any
        path = path.split("?")[0]

        if method not in ("GET", "HEAD"):
            status_code = "405 Method Not Allowed"
            body_bytes = b'{"error": "Method Not Allowed"}'
            content_type = "application/json"
        elif path in ("/health", "/ping"):
            status_code = "200 OK"
            payload = {
                "status": "ok",
                "healthy": True,
                "uptime_seconds": get_uptime_seconds(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            body_bytes = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        elif path == "/":
            status_code = "200 OK"
            payload = _get_system_status()
            body_bytes = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"
        else:
            status_code = "404 Not Found"
            body_bytes = b'{"error": "Not Found"}'
            content_type = "application/json"

        response_headers = [
            f"HTTP/1.1 {status_code}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body_bytes)}",
            "Connection: close",
            "Access-Control-Allow-Origin: *",
            "\r\n",
        ]
        writer.write("\r\n".join(response_headers).encode("utf-8"))
        if method != "HEAD":
            writer.write(body_bytes)
        await asyncio.wait_for(writer.drain(), timeout=5.0)
    except (asyncio.TimeoutError, ConnectionResetError):
        pass
    except Exception as exc:
        logger.debug("Error handling HTTP request: %s", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class HealthWebServer:
    """Manages the background asyncio HTTP server."""

    def __init__(self, host: str = "0.0.0.0", port: int = PORT):
        self.host = host
        self.port = port
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        """Start listening on host:port."""
        if self.server is not None:
            return
        self.server = await asyncio.start_server(_handle_client, self.host, self.port)
        logger.info("Health web server started on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Gracefully stop the server."""
        if self.server is None:
            return
        logger.info("Stopping health web server on %s:%d...", self.host, self.port)
        self.server.close()
        await self.server.wait_closed()
        self.server = None
