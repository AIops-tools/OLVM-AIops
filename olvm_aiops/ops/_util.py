"""Shared helpers for OLVM AIops ops modules.

Shapes verified against a live OLVM 4.5.5 engine (JSON representation):

  * a collection is ``{"<singular>": [ ... ]}`` — ``{"host": [...]}``,
    ``{"storage_domain": [...]}`` — and an EMPTY collection may omit the key;
  * counts, sizes and flags are JSON **strings** (``"memory": "0"``,
    ``"update_available": "false"``), but timestamps are JSON **numbers** in
    epoch milliseconds (``"time": 1789438559456``) — never assume one type;
  * a field the engine has no value for is absent, not zero — an unattached
    storage domain carries no ``available`` / ``used`` at all.

So counts and sizes go through :func:`as_int`, flags through :func:`as_bool`,
and both return ``None`` for "not reported" rather than inventing a 0 or False.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from olvm_aiops.governance import opt_str

#: Default cap on listings that can grow without bound (VMs, events, jobs).
DEFAULT_LIST_LIMIT = 100
#: Cap applied to the inputs of diagnose analyses and the overview.
ANALYSIS_LIST_LIMIT = 1000
MAX_LIST_LIMIT = 1000


def as_int(value: Any) -> int | None:
    """An engine number (sent as a string) as ``int``; ``None`` when not reported.

    ``bool`` is rejected before anything else — it is an ``int`` subclass and a
    flag must never pass for a count. Ints are returned as-is, never
    round-tripped through float (int64 byte counts would lose precision).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def as_bool(value: Any) -> bool | None:
    """An engine flag (``"true"`` / ``"false"``) as ``bool``; ``None`` when not reported."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def ms_to_iso(value: Any) -> str | None:
    """An engine timestamp (epoch milliseconds, number or numeric string) as ISO-8601 UTC.

    ``None`` when absent, a flag, non-numeric, or outside the representable range —
    one absurd value must not take down a whole listing.
    """
    ms = as_int(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def text(value: Any, limit: int = 256) -> str | None:
    """Sanitised engine text; ``None`` when absent (missing is not empty)."""
    return opt_str(value, limit)


def items(body: Any, key: str) -> list[dict]:
    """The objects of a collection response ``{"<key>": [...]}``.

    A missing key is an empty collection (the engine omits it when there is
    nothing); anything that is not a dict or list is a malformed answer.
    """
    if not isinstance(body, dict):
        raise ValueError(f"Engine returned a non-object collection body for '{key}'.")
    rows = body.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"Engine collection '{key}' is not a list.")
    return [r for r in rows if isinstance(r, dict)]


def ref_id(value: Any) -> str | None:
    """The id of a link object ``{"href": ..., "id": ...}``."""
    return text(value.get("id"), 64) if isinstance(value, dict) else None


def bounded_limit(limit: Any, name: str = "limit") -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"{name} must be an integer between 1 and {MAX_LIST_LIMIT}.")
    return limit


def fetch_page(conn: Any, path: str, key: str, limit: int,
               search: str | None = None) -> tuple[list[dict], bool]:
    """Up to ``limit`` objects plus whether more exist — measured, not guessed.

    The engine has no skip/offset; ``max`` caps a listing and a listing without
    ``max`` returns everything. Asking for ``limit + 1`` and seeing the extra
    row is the only honest way to say "truncated".
    """
    limit = bounded_limit(limit)
    params: dict[str, str] = {"max": str(limit + 1)}
    if search:
        params["search"] = search
    rows = items(conn.get(path, params=params), key)
    return rows[:limit], len(rows) > limit


def envelope(key: str, rows: list[dict], limit: int, truncated: bool) -> dict:
    """``{<key>: [...], "returned": N, "limit": L, "truncated": bool}``."""
    return {key: rows, "returned": len(rows), "limit": limit, "truncated": truncated}


def pct(part: int | None, whole: int | None) -> float | None:
    """``part / whole`` as a percentage to 1 decimal; ``None`` when not computable."""
    if part is None or whole is None or whole <= 0:
        return None
    return round(part / whole * 100, 1)
