"""Uniform tool response helpers + a guard decorator.

Every tool returns the MCP content shape:
    {"content": [{"type": "text", "text": ...}], "is_error": bool}

Use `ok(summary, **data)` for success and let `@guard` translate any RepoError
or unexpected exception into a clean `is_error` result the model can relay or
turn into a clarifying question. Business rules live in the repository; tools
are thin adapters.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable

from ..db.repository import RepoError


def ok(summary: str, **data: Any) -> dict:
    """Success result. `summary` is human/agent-readable; `data` is compact JSON
    appended for the model to reason over (keep it small)."""
    text = summary
    if data:
        text += "\n" + json.dumps(data, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}]}


def err(message: str, code: str = "error") -> dict:
    return {
        "content": [{"type": "text", "text": f"[{code}] {message}"}],
        "is_error": True,
    }


Handler = Callable[[dict], Awaitable[dict]]


def guard(fn: Handler) -> Handler:
    """Wrap a tool handler: RepoError -> actionable is_error; other exceptions ->
    generic is_error. Put directly under @tool."""

    @functools.wraps(fn)
    async def wrapper(args: dict) -> dict:
        try:
            return await fn(args)
        except RepoError as e:
            return err(e.message, e.code)
        except (ValueError, KeyError) as e:
            return err(str(e), "bad_input")
        except Exception as e:  # noqa: BLE001 - defensive: never crash the loop
            return err(f"Unexpected error: {e}", "internal")

    return wrapper


def arg(args: dict, key: str, default: Any = None) -> Any:
    v = args.get(key, default)
    return default if v is None else v
