"""Unit tests for the native async health web server."""

from __future__ import annotations

import httpx
import pytest

from kirana.telegram.web import HealthWebServer


@pytest.mark.asyncio
async def test_health_web_server_endpoints():
    server = HealthWebServer(host="127.0.0.1", port=19877)
    await server.start()

    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:19877") as client:
            # 1. Health endpoint
            res_health = await client.get("/health")
            assert res_health.status_code == 200
            data_health = res_health.json()
            assert data_health["status"] == "ok"
            assert data_health["healthy"] is True
            assert "uptime_seconds" in data_health

            # 2. Ping endpoint
            res_ping = await client.get("/ping")
            assert res_ping.status_code == 200
            assert res_ping.json()["healthy"] is True

            # 3. Root status dashboard
            res_root = await client.get("/")
            assert res_root.status_code == 200
            data_root = res_root.json()
            assert data_root["service"] == "kirana-ops-agent"
            assert data_root["status"] == "healthy"
            assert "stores_count" in data_root

            # 4. Method Not Allowed
            res_post = await client.post("/health", json={"test": 1})
            assert res_post.status_code == 405

            # 5. Not Found
            res_404 = await client.get("/random-unknown-endpoint")
            assert res_404.status_code == 404
    finally:
        await server.stop()
