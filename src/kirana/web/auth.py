"""Authentication & multi-tenant session management for the Admin Web Dashboard.

Provides:
- PBKDF2 PIN verification against master.db tenant records
- Signed, tamper-proof HttpOnly session cookies via itsdangerous
- Sliding-window rate limiting on login attempts
- FastAPI dependency injection resolving (Tenant, Repository)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import SECRET_KEY, SESSION_MAX_AGE
from ..db.master import MasterRegistry, Tenant, get_master_registry
from ..db.repository import Repository
from ..security.password import RateLimiter, verify_password

log = logging.getLogger("kirana.web.auth")

COOKIE_NAME = "kirana_session"
_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="kirana-dashboard-auth")
_login_limiter = RateLimiter(max_attempts=5, window_seconds=900)  # 5 attempts per 15 mins


def create_session_token(store_code: str, role: str = "owner") -> str:
    """Generate a signed cryptographic session token containing tenant identity."""
    payload = {
        "store_code": store_code.upper().strip(),
        "role": role,
    }
    return _serializer.dumps(payload)


def decode_session_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a session token within its max age."""
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        if isinstance(data, dict) and "store_code" in data:
            return data
    except (SignatureExpired, BadSignature, Exception) as e:
        log.debug("Session token verification failed: %s", e)
    return None


def authenticate_store_credentials(
    store_code: str,
    pin: str,
    registry: MasterRegistry | None = None,
    client_ip: str = "unknown",
) -> tuple[bool, Tenant | None, str]:
    """Verify store code and owner PIN with rate limiting.

    Returns: (is_success, tenant_object, error_message_or_empty)
    """
    clean_code = store_code.strip().upper()
    clean_pin = pin.strip()

    # Rate limiting by IP and Store Code
    rate_key = f"{client_ip}:{clean_code}"
    if not _login_limiter.is_allowed(rate_key):
        remaining = _login_limiter.lockout_remaining(rate_key)
        mins = max(1, remaining // 60)
        return False, None, f"Too many failed attempts. Locked out for {mins} more minutes."

    if not clean_code or not clean_pin:
        return False, None, "Store code and PIN are required."

    if registry is None:
        registry = get_master_registry()

    tenant = registry.get_tenant_by_code(clean_code)
    if not tenant:
        _login_limiter.record_failure(rate_key)
        return False, None, "Invalid Store Code or PIN."

    # If store has no PIN configured yet, reject or check tenant repo
    pin_hash = tenant.owner_pin_hash
    if not pin_hash:
        try:
            repo = registry.get_store_repo(tenant.store_code)
            profile = repo.get_shop_profile()
            pin_hash = profile.owner_pin_hash
        except Exception:
            pin_hash = None

    if not pin_hash:
        _login_limiter.record_failure(rate_key)
        return False, None, "Store PIN is not configured yet. Set it in Telegram via /store or onboard first."

    if not verify_password(clean_pin, pin_hash):
        _login_limiter.record_failure(rate_key)
        return False, None, "Invalid Store Code or PIN."

    _login_limiter.record_success(rate_key)
    return True, tenant, ""


class TenantContext:
    """Encapsulates authenticated tenant identity and active database repository."""

    def __init__(self, tenant: Tenant, repo: Repository, role: str = "owner"):
        self.tenant = tenant
        self.repo = repo
        self.role = role

    @property
    def store_code(self) -> str:
        return self.tenant.store_code

    @property
    def store_name(self) -> str:
        return self.tenant.store_name

    @property
    def owner_name(self) -> str:
        return self.tenant.owner_name


async def get_current_tenant_context(
    request: Request,
    registry: MasterRegistry = Depends(get_master_registry),
) -> TenantContext:
    """FastAPI Dependency: Authenticates request via session cookie or redirects to /login."""
    cookie = request.cookies.get(COOKIE_NAME)
    token_data = decode_session_token(cookie) if cookie else None

    is_api = request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")

    if not token_data:
        if is_api:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please log in with Store Code and PIN.",
            )
        # HTML request -> redirect to login
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )

    store_code = token_data["store_code"]
    tenant = registry.get_tenant_by_code(store_code)
    if not tenant:
        if is_api:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Tenant not found or inactive.",
            )
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )

    repo = registry.get_store_repo(store_code)
    return TenantContext(tenant=tenant, repo=repo, role=token_data.get("role", "owner"))


async def get_optional_tenant_context(
    request: Request,
    registry: MasterRegistry = Depends(get_master_registry),
) -> TenantContext | None:
    """FastAPI Dependency: Returns TenantContext if logged in, otherwise None (no redirection)."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    token_data = decode_session_token(cookie)
    if not token_data:
        return None
    store_code = token_data.get("store_code")
    if not store_code:
        return None
    tenant = registry.get_tenant_by_code(store_code)
    if not tenant:
        return None
    try:
        repo = registry.get_store_repo(store_code)
        return TenantContext(tenant=tenant, repo=repo, role=token_data.get("role", "owner"))
    except Exception:
        return None
