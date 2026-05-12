"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state and appends it to the FINAL
message of an agent turn when enabled.  Off by default to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true
        style: telegram_status
        fields: [model, context_tokens, context_bar, session_elapsed, response_elapsed]

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.
"""

from __future__ import annotations

import ast
import os
import time
from datetime import datetime
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_TELEGRAM_STATUS_FIELDS: tuple[str, ...] = (
    "model",
    "context_tokens",
    "context_bar",
    "session_elapsed",
    "response_elapsed",
)
_SEP = " · "
_STATUS_SEP = " │ "


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``. Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def _coerce_fields(value: Any, fallback: Iterable[str]) -> list[str]:
    """Return a field-name list from YAML list or legacy string config.

    Some older config writers persisted values as the literal string
    ``"[model, context_pct]"``. Treat that as a list instead of silently
    ignoring it.
    """
    if isinstance(value, (list, tuple)) and value:
        return [str(f).strip() for f in value if str(f).strip()]
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, (list, tuple)) and parsed:
                return [str(f).strip() for f in parsed if str(f).strip()]
        except Exception:
            pass
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        parts = [p.strip().strip("'\"") for p in raw.split(",")]
        return [p for p in parts if p]
    return [str(f) for f in fallback]


def _format_token_count(tokens: Optional[int]) -> str:
    try:
        n = int(tokens or 0)
    except (TypeError, ValueError):
        n = 0
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _context_pct(context_tokens: int, context_length: Optional[int]) -> Optional[int]:
    if context_length and context_length > 0 and context_tokens >= 0:
        return max(0, min(100, round((context_tokens / context_length) * 100)))
    return None


def _context_bar(pct: Optional[int], width: int = 10) -> str:
    if pct is None:
        return ""
    filled = max(0, min(width, int((pct / 100) * width)))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + f"] {pct}%"


def _format_duration(seconds: Optional[float]) -> str:
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def _coerce_epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved: dict[str, Any] = {
        "enabled": False,
        "fields": list(_DEFAULT_FIELDS),
        "style": "default",
    }
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if "fields" in global_cfg:
            resolved["fields"] = _coerce_fields(global_cfg.get("fields"), resolved["fields"])
        if global_cfg.get("style"):
            resolved["style"] = str(global_cfg.get("style"))

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if "fields" in plat_footer:
                    resolved["fields"] = _coerce_fields(plat_footer.get("fields"), resolved["fields"])
                if plat_footer.get("style"):
                    resolved["style"] = str(plat_footer.get("style"))

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
    response_seconds: Optional[float] = None,
    session_started_at: Any = None,
    now: Optional[float] = None,
    style: str = "default",
) -> str:
    """Render the footer line, or return "" if no fields have data.

    Fields are skipped silently when their underlying data is missing — a
    partially-populated footer is better than a line with ``?%`` or empty slots.
    """
    fields = tuple(fields or ())
    if style in {"telegram_status", "status"}:
        if fields == _DEFAULT_FIELDS:
            fields = _TELEGRAM_STATUS_FIELDS
        sep = _STATUS_SEP
    else:
        sep = _SEP

    pct = _context_pct(context_tokens, context_length)
    parts: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if pct is not None:
                parts.append(f"{pct}%")
        elif field == "context_tokens":
            if context_length and context_length > 0 and context_tokens >= 0:
                parts.append(f"{_format_token_count(context_tokens)}/{_format_token_count(context_length)}")
        elif field == "context_bar":
            bar = _context_bar(pct)
            if bar:
                parts.append(bar)
        elif field == "response_elapsed":
            if response_seconds is not None:
                parts.append(f"⏲ {_format_duration(response_seconds)}")
        elif field == "session_elapsed":
            start = _coerce_epoch(session_started_at)
            if start is not None:
                elapsed = (time.time() if now is None else now) - start
                if elapsed >= 0:
                    parts.append(_format_duration(elapsed))
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        # Unknown field names are silently ignored.

    if not parts:
        return ""
    return sep.join(parts)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    response_seconds: Optional[float] = None,
    session_started_at: Any = None,
) -> str:
    """Top-level entry point used by gateway/run.py."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
        response_seconds=response_seconds,
        session_started_at=session_started_at,
        style=cfg.get("style") or "default",
    )
