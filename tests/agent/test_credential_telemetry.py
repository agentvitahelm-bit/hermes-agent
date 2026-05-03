"""Tests for agent.credential_telemetry."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import hermes_constants
from agent import credential_telemetry as ct


@pytest.fixture
def telem(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    ct._reset_singleton_for_tests()
    yield ct.get_telemetry()
    ct._reset_singleton_for_tests()


def test_persistence_round_trip(telem, tmp_path, monkeypatch):
    telem.record_cooldown(
        "openai-codex",
        "device_code",
        status_code=429,
        reason="rate_limit",
        cooldown_until=12345.0,
        label="device_code",
    )
    telem.record_tokens(
        "openai-codex", "device_code", "gpt-5",
        input_tokens=10, output_tokens=20, label="device_code",
    )
    # Drop singleton, re-load from disk.
    ct._reset_singleton_for_tests()
    fresh = ct.get_telemetry()
    snap = fresh.snapshot()
    acct = snap["accounts"]["openai-codex::device_code"]
    assert acct["cooldown_count"] == 1
    assert acct["last_status_code"] == 429
    assert acct["tokens_lifetime"]["gpt-5"] == {"input": 10, "output": 20, "total": 30}
    # tokens_since_last_cooldown is empty because record_cooldown ran before record_tokens?
    # Actually we recorded cooldown first, then tokens — so since-last should have the tokens.
    assert acct["tokens_since_last_cooldown"]["gpt-5"]["total"] == 30


def test_record_cooldown_increments_deterministically(telem):
    for _ in range(100):
        telem.record_cooldown(
            "openai-codex", "acct1",
            status_code=429, reason="rate_limit", cooldown_until=None,
        )
    acct = telem.get_account("openai-codex", "acct1")
    assert acct is not None
    assert acct["cooldown_count"] == 100


def test_record_tokens_sums_across_models_and_accounts(telem):
    telem.record_tokens("p", "a", "model-A", input_tokens=10, output_tokens=5)
    telem.record_tokens("p", "a", "model-A", input_tokens=2, output_tokens=3)
    telem.record_tokens("p", "a", "model-B", input_tokens=7, output_tokens=1)
    telem.record_tokens("p", "b", "model-A", input_tokens=100, output_tokens=200)
    a = telem.get_account("p", "a")
    b = telem.get_account("p", "b")
    assert a["tokens_lifetime"]["model-A"] == {"input": 12, "output": 8, "total": 20}
    assert a["tokens_lifetime"]["model-B"] == {"input": 7, "output": 1, "total": 8}
    assert b["tokens_lifetime"]["model-A"] == {"input": 100, "output": 200, "total": 300}


def test_cooldown_resets_since_but_not_lifetime(telem):
    telem.record_tokens("p", "a", "m", input_tokens=10, output_tokens=20)
    telem.record_cooldown("p", "a", status_code=429, reason="rate_limit", cooldown_until=None)
    acct = telem.get_account("p", "a")
    assert acct["tokens_since_last_cooldown"] == {}
    assert acct["tokens_lifetime"]["m"]["total"] == 30
    telem.record_tokens("p", "a", "m", input_tokens=1, output_tokens=2)
    acct = telem.get_account("p", "a")
    assert acct["tokens_since_last_cooldown"]["m"]["total"] == 3
    assert acct["tokens_lifetime"]["m"]["total"] == 33


def test_format_guidance_lines_includes_labels(telem):
    import time as _t
    telem.record_cooldown(
        "openai-codex", "acct1",
        status_code=429, reason="rate_limit",
        cooldown_until=_t.time() + 3600,
        label="device_code",
    )
    lines = telem.format_guidance_lines(fallback_active=True, primary_provider="openai-codex")
    assert lines
    text = "\n".join(lines)
    assert "openai-codex" in text
    assert "device_code" in text
    assert "Fallback (Claude): ACTIVE" in text
    assert "Total cooldowns" in text


def test_concurrent_writes(telem):
    def hit():
        telem.record_cooldown(
            "p", "shared",
            status_code=429, reason="rate_limit", cooldown_until=None,
        )

    threads = [threading.Thread(target=hit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    acct = telem.get_account("p", "shared")
    assert acct["cooldown_count"] == 10
