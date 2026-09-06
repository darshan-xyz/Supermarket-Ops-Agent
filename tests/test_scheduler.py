"""Scheduled-job helpers (the pure parts — chat targeting and reminder text).
The JobQueue scheduling itself is exercised at runtime by the bot."""

from __future__ import annotations

from kirana.telegram import jobs

CHAT = 4242


def test_known_chat_ids(repo):
    assert repo.known_chat_ids() == []
    repo.set_session(CHAT, "sess-1")
    repo.set_session(777, "sess-2")
    assert set(repo.known_chat_ids()) == {CHAT, 777}


def test_target_chats_default_all(repo, monkeypatch):
    monkeypatch.setattr(jobs, "OWNER_CHAT_ID", None)
    repo.set_session(CHAT, "s")
    assert jobs.target_chat_ids(repo) == [CHAT]


def test_target_chats_pinned_owner(repo, monkeypatch):
    monkeypatch.setattr(jobs, "OWNER_CHAT_ID", 999)
    repo.set_session(CHAT, "s")  # ignored when owner is pinned
    assert jobs.target_chat_ids(repo) == [999]


def test_khata_reminder_text(repo):
    assert jobs.khata_reminder_text(repo) is None  # nobody owes anything
    repo.khata_add_credit("Ramesh", 50000)
    repo.khata_add_credit("Suresh", 20000)
    text = jobs.khata_reminder_text(repo)
    assert "Ramesh" in text and "Suresh" in text
    assert "₹700.00" in text  # total outstanding (500 + 200)
