"""Tests verifying CredentialPool wires CredentialTelemetry on cooldown."""

from __future__ import annotations

import json

import pytest

import hermes_constants


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


@pytest.fixture
def telemetry_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    from agent import credential_telemetry as ct
    ct._reset_singleton_for_tests()
    yield home
    ct._reset_singleton_for_tests()


def _seed_pool(tmp_path):
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    },
                ]
            },
        },
    )


def test_mark_exhausted_records_telemetry(tmp_path, telemetry_home):
    _seed_pool(tmp_path)
    from agent.credential_pool import load_pool
    from agent.credential_telemetry import get_telemetry

    pool = load_pool("anthropic")
    entry = pool.select()
    assert entry is not None

    pool._mark_exhausted(entry, 429)
    snap = get_telemetry().snapshot()
    accounts = snap["accounts"]
    key = "anthropic::cred-1"
    assert key in accounts
    assert accounts[key]["cooldown_count"] == 1
    assert accounts[key]["last_status_code"] == 429
    assert accounts[key]["last_cooldown_reason"] == "rate_limit"

    # Re-fetch entry (it was replaced in-place).
    entry2 = next(e for e in pool.entries() if e.id == "cred-1")
    pool._mark_exhausted(entry2, 429)
    snap2 = get_telemetry().snapshot()
    assert snap2["accounts"][key]["cooldown_count"] == 2


def test_account_descriptors_includes_active_flag(tmp_path, telemetry_home):
    _seed_pool(tmp_path)
    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    pool.select()
    descs = pool.account_descriptors()
    assert len(descs) == 1
    assert descs[0]["id"] == "cred-1"
    assert descs[0]["label"] == "primary"
    assert descs[0]["is_active"] is True
    assert descs[0]["exhausted_until"] is None
