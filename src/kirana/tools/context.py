"""Shared context for tools: the Repository singleton and a per-turn file outbox.

Tools never construct their own DB connection — they call `get_repo()`. Document
tools that produce a PDF/PPTX call `queue_output_file(path, caption)`; the Telegram
layer drains the outbox after the turn and sends the files. Routing uses a
ContextVar set by the session runner around each `query()` call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from ..config import DB_PATH
from ..db.connection import Database
from ..db.repository import Repository

_repo: Repository | None = None

# set by agent/session.py immediately before each query() so tools know the chat
current_chat: ContextVar[int | None] = ContextVar("current_chat", default=None)
# optional override for explicit tenant repository per turn
current_tenant_repo: ContextVar[Repository | None] = ContextVar("current_tenant_repo", default=None)


@dataclass
class OutFile:
    path: str
    caption: str


_outbox: dict[int, list[OutFile]] = {}


def init_context(repo: Repository | None = None) -> Repository:
    """Initialise (or replace) the process-wide repository. Idempotent."""
    global _repo
    if repo is not None:
        _repo = repo
    elif _repo is None:
        db = Database(DB_PATH)
        _repo = Repository(db)
        _repo.init()
    return _repo


def get_repo() -> Repository:
    # 1. First check explicit tenant repo context
    tenant_repo = current_tenant_repo.get()
    if tenant_repo is not None:
        return tenant_repo

    # 2. Check if active chat belongs to a registered tenant in MasterRegistry
    chat = current_chat.get()
    if chat is not None:
        try:
            from ..db.master import get_master_registry
            registry_repo = get_master_registry().get_repo_for_chat(chat)
            if registry_repo is not None:
                return registry_repo
        except Exception:
            pass

    # 3. Default fallback for test suites and single-store standalone scripts
    if _repo is None:
        return init_context()
    return _repo


def queue_output_file(path: str, caption: str = "") -> None:
    chat = current_chat.get()
    if chat is None:
        chat = 0  # standalone / test context
    _outbox.setdefault(chat, []).append(OutFile(path=path, caption=caption))


def take_output_files(chat_id: int) -> list[OutFile]:
    """Return and clear queued files for a chat (called by the bot post-turn)."""
    files = _outbox.pop(chat_id, [])
    # also drain the standalone bucket in case chat wasn't set
    files += _outbox.pop(0, [])
    return files
