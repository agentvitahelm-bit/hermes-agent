"""Blocker identity and recurrence routing for the Kanban database facade."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

_BLOCK_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


def _block_digest(value: Optional[str], *, domain: str) -> Optional[str]:
    """Return an opaque digest suitable for durable state and event payloads."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{domain} identity must be a non-empty string or None")
    value = value.strip()
    if _BLOCK_DIGEST_RE.fullmatch(value):
        return value.lower()
    material = f"hermes-kanban:{domain}:v1\0{value}".encode()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _blocker_fingerprint(
    kind: Optional[str], reason: Optional[str], explicit: Optional[str],
) -> str:
    """Canonical explicit identity, or a privacy-safe kind+reason fallback."""
    if explicit is not None:
        return _block_digest(explicit, domain="blocker") or ""
    normalized_reason = re.sub(r"\s+", " ", reason or "").strip().casefold()
    derived = json.dumps([kind, normalized_reason], ensure_ascii=False, separators=(",", ":"))
    return _block_digest(derived, domain="derived-blocker") or ""


def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_fingerprint: Optional[str], fingerprint: str,
    prev_precondition: Optional[str], precondition: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """Return the task status, event, SQL fragment, parameters, and payload.

    Dependency waits stay in ``todo`` and do not count recurrences. Every other
    block recurs only when its effective kind, opaque fingerprint, and optional
    precondition digest all match. The fallback fingerprint hashes normalized
    reason text, while an explicit fingerprint can remain stable as prose changes.
    """
    from hermes_cli import kanban_db as kb

    effective_limit = kb.BLOCK_RECURRENCE_LIMIT
    payload = {
        "reason": reason,
        "kind": kind,
        "source_status": source_status,
        "previous_blocker_fingerprint": prev_fingerprint,
        "blocker_fingerprint": fingerprint,
        "previous_precondition_digest": prev_precondition,
        "precondition_digest": precondition,
        "effective_limit": effective_limit,
    }
    if kind == "dependency":
        set_sql = (
            "block_kind    = ?,\n"
            "                       blocker_fingerprint = ?,\n"
            "                       block_precondition_digest = ?,\n"
            "                       block_recurrences = 0"
        )
        payload["recurrences"] = 0
        return "todo", "dependency_wait", set_sql, (kind, fingerprint, precondition), payload
    same_cause = (
        prev_kind == kind
        and prev_fingerprint == fingerprint
        and prev_precondition == precondition
    )
    recurrences = prev_recurrences + 1 if same_cause else 1
    set_sql = (
        "block_kind    = ?,\n"
        "                       blocker_fingerprint = ?,\n"
        "                       block_precondition_digest = ?,\n"
        "                       block_recurrences = ?"
    )
    payload["recurrences"] = recurrences
    if recurrences >= effective_limit:
        payload["limit"] = effective_limit
        params = (kind, fingerprint, precondition, recurrences)
        return "triage", "block_loop_detected", set_sql, params, payload
    return "blocked", "blocked", set_sql, (kind, fingerprint, precondition, recurrences), payload
