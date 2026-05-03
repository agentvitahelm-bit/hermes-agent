"""Per-account credential cooldown + token telemetry.

Persists deterministic counters and per-model token usage to
``<HERMES_HOME>/state/credential_telemetry.json`` so Hermes can show
operators exactly which credentials are cooling down, how often they hit
429s, and how many tokens have been spent against each account/model.

The on-disk schema is versioned; see ``SCHEMA_VERSION``. Writes are atomic
(``.tmp`` → ``os.replace``) and protected by an ``fcntl`` advisory file
lock on POSIX (best-effort fallback on platforms without ``fcntl``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # POSIX-only file locking; fallback to no-op on Windows.
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - Windows path
    fcntl = None  # type: ignore

import hermes_constants

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_STATE_DIRNAME = "state"
_STATE_FILENAME = "credential_telemetry.json"


def _account_key(provider: str, account_id: str) -> str:
    return f"{provider}::{account_id}"


def _empty_state() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "accounts": {}}


def _empty_account(provider: str, account_id: str, label: Optional[str]) -> Dict[str, Any]:
    return {
        "provider": provider,
        "account_id": account_id,
        "label": label or account_id,
        "cooldown_count": 0,
        "last_cooldown_at": None,
        "last_status_code": None,
        "last_cooldown_reason": None,
        "current_cooldown_until": None,
        "tokens_since_last_cooldown": {},
        "tokens_lifetime": {},
    }


def _format_relative_future(seconds: float) -> str:
    """``in 2d 1h`` style for a positive seconds delta. <=0 ⇒ ``now``."""
    s = int(seconds)
    if s <= 0:
        return "now"
    hours, rem = divmod(s, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    if minutes > 0:
        seconds_part = s - minutes * 60
        if seconds_part:
            return f"in {minutes}m {seconds_part}s"
        return f"in {minutes}m"
    return f"in {s}s"


def _format_relative_past(seconds: float) -> str:
    """``5m ago`` style for a positive seconds delta. <=0 ⇒ ``just now``."""
    s = int(seconds)
    if s <= 0:
        return "just now"
    hours, rem = divmod(s, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    if minutes > 0:
        return f"{minutes}m ago"
    return f"{s}s ago"


class CredentialTelemetry:
    """Singleton-ish telemetry store. Use :func:`get_telemetry` to access."""

    def __init__(self, *, state_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._state_path_override = state_path

    # ---- paths ----
    def _state_path(self) -> Path:
        if self._state_path_override is not None:
            return self._state_path_override
        home = hermes_constants.get_hermes_home()
        return Path(home) / _STATE_DIRNAME / _STATE_FILENAME

    # ---- file I/O with locking ----
    def _load(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return _empty_state()
        try:
            with open(path, "r", encoding="utf-8") as f:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except Exception:
                        pass
                data = json.load(f)
            if not isinstance(data, dict) or "accounts" not in data:
                return _empty_state()
            data.setdefault("schema_version", SCHEMA_VERSION)
            data.setdefault("accounts", {})
            return data
        except Exception:
            logger.debug("credential_telemetry: failed to load state", exc_info=True)
            return _empty_state()

    def _save(self, state: Dict[str, Any]) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("credential_telemetry: mkdir failed", exc_info=True)
            return
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except Exception:
                        pass
                json.dump(state, f, indent=2, sort_keys=True)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp_path, path)
        except Exception:
            logger.debug("credential_telemetry: save failed", exc_info=True)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    # ---- mutators ----
    def record_cooldown(
        self,
        provider: str,
        account_id: str,
        *,
        status_code: Optional[int],
        reason: Optional[str],
        cooldown_until: Optional[float],
        label: Optional[str] = None,
    ) -> None:
        with self._lock:
            state = self._load()
            accounts = state.setdefault("accounts", {})
            key = _account_key(provider, account_id)
            entry = accounts.get(key) or _empty_account(provider, account_id, label)
            if label:
                entry["label"] = label
            entry["cooldown_count"] = int(entry.get("cooldown_count", 0)) + 1
            entry["last_cooldown_at"] = time.time()
            entry["last_status_code"] = status_code
            entry["last_cooldown_reason"] = reason
            entry["current_cooldown_until"] = cooldown_until
            entry["tokens_since_last_cooldown"] = {}
            accounts[key] = entry
            self._save(state)

    def record_tokens(
        self,
        provider: str,
        account_id: str,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        label: Optional[str] = None,
    ) -> None:
        if not model:
            model = "unknown"
        i = int(input_tokens or 0)
        o = int(output_tokens or 0)
        if i == 0 and o == 0:
            # Still count zero-token calls? Skip to keep store small.
            return
        with self._lock:
            state = self._load()
            accounts = state.setdefault("accounts", {})
            key = _account_key(provider, account_id)
            entry = accounts.get(key) or _empty_account(provider, account_id, label)
            if label:
                entry["label"] = label
            for bucket_name in ("tokens_since_last_cooldown", "tokens_lifetime"):
                bucket = entry.setdefault(bucket_name, {})
                model_bucket = bucket.setdefault(
                    model, {"input": 0, "output": 0, "total": 0}
                )
                model_bucket["input"] = int(model_bucket.get("input", 0)) + i
                model_bucket["output"] = int(model_bucket.get("output", 0)) + o
                model_bucket["total"] = (
                    int(model_bucket.get("total", 0)) + i + o
                )
            accounts[key] = entry
            self._save(state)

    # ---- accessors ----
    def get_account(self, provider: str, account_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._load()
            entry = state.get("accounts", {}).get(_account_key(provider, account_id))
            return copy.deepcopy(entry) if entry else None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load())

    # ---- formatting ----
    def format_guidance_lines(
        self,
        *,
        fallback_active: bool,
        primary_provider: Optional[str],
    ) -> List[str]:
        state = self.snapshot()
        accounts = state.get("accounts", {})
        provider_label = primary_provider or "primary"
        lines: List[str] = [f"🚧 Credential pool status ({provider_label}):"]

        # Filter to primary provider when given; otherwise show all.
        if primary_provider:
            relevant = [
                (k, v) for k, v in accounts.items()
                if v.get("provider") == primary_provider
            ]
        else:
            relevant = list(accounts.items())

        # Stable order: by account_id.
        relevant.sort(key=lambda kv: kv[1].get("account_id") or kv[0])
        now = time.time()
        total_cooldowns = 0
        if not relevant:
            lines.append("  • (no accounts recorded yet)")
        for idx, (_, entry) in enumerate(relevant, start=1):
            label = entry.get("label") or entry.get("account_id") or "?"
            count = int(entry.get("cooldown_count", 0))
            total_cooldowns += count
            cooldown_until = entry.get("current_cooldown_until")
            last_at = entry.get("last_cooldown_at")
            status_code = entry.get("last_status_code")
            cooling = bool(cooldown_until and float(cooldown_until) > now)
            since = entry.get("tokens_since_last_cooldown", {}) or {}
            in_tokens = sum(int((m or {}).get("input", 0)) for m in since.values())
            out_tokens = sum(int((m or {}).get("output", 0)) for m in since.values())

            if cooling:
                resets_in = _format_relative_future(float(cooldown_until) - now)
                last_phrase = ""
                if last_at:
                    last_phrase = (
                        f" (last {status_code or 'cooldown'} "
                        f"at {_format_relative_past(now - float(last_at))})"
                    )
                lines.append(
                    f"  • Account #{idx} ({label}): cooling down — resets {resets_in}{last_phrase}"
                )
            elif count == 0:
                lines.append(f"  • Account #{idx} ({label}): healthy")
            else:
                lines.append(f"  • Account #{idx} ({label}): healthy (recovered)")
            lines.append(
                f"    cooldowns hit: {count} lifetime · "
                f"tokens since last cooldown: {in_tokens} in / {out_tokens} out"
            )

        if fallback_active:
            lines.append("Fallback (Claude): ACTIVE")
        else:
            lines.append("Fallback (Claude): inactive (no fallback_model configured)")
        lines.append(f"Total cooldowns across all accounts: {total_cooldowns}")
        return lines


_singleton_lock = threading.Lock()
_singleton: Optional[CredentialTelemetry] = None


def get_telemetry() -> CredentialTelemetry:
    """Return the process-wide singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CredentialTelemetry()
    return _singleton


def _reset_singleton_for_tests() -> None:
    """Test hook: drop the singleton so tests can rebind HERMES_HOME."""
    global _singleton
    with _singleton_lock:
        _singleton = None
