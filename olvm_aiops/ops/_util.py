"""Shared helpers for XCP-ng AIops ops modules.

Xen Orchestra REST ``/rest/v0`` collection endpoints return a bare JSON array
(of objects when ``fields=`` is passed, of href strings otherwise); a few wrap
results in ``{"data": [...]}``. ``as_list`` normalises all of these to a list
of dicts. All API-returned text reaches the caller only after ``sanitize()``
(output hygiene).
"""

from __future__ import annotations

from typing import Any

from olvm_aiops.governance import sanitize

# The field sets ops modules request from XO — asking for fields makes the
# collection endpoints return objects instead of href strings.

#: Default cap on any listing that can grow without bound (VMs, VDIs, tasks,
#: backup logs). Chosen to keep a single tool result inside a small model's
#: context window; every capped listing says so via ``truncated``.
DEFAULT_LIST_LIMIT = 200

#: Cap the RCA / overview fan-outs apply to their input listings. Higher than
#: the interactive default because a correlation is only as good as the set it
#: ran over — and the analyses report when even this was not enough.
ANALYSIS_LIST_LIMIT = 1000


def as_list(data: Any) -> list[dict]:
    """Normalise a collection endpoint's payload to a list of dicts."""
    if isinstance(data, dict):
        items = data.get("data", [])
    else:
        items = data
    return [i for i in (items or []) if isinstance(i, dict)]


def s(value: Any, limit: int = 128) -> str:
    """Sanitize an arbitrary value to a bounded, injection-safe string."""
    return sanitize(str(value if value is not None else ""), limit)


def paged(key: str, rows: list[dict], limit: int) -> dict:
    """Wrap a client-side-sliced listing in a truncation-announcing envelope.

    Xen Orchestra's ``/rest/v0`` collection endpoints return the whole
    collection, so the cap is applied here. ``rows`` is therefore the COMPLETE
    list and ``truncated`` is *measured* against its real length — never
    guessed from ``len(items) == limit``, which is a coincidence, not a fact.

    Returns ``{<key>: [...], "returned": N, "limit": L, "truncated": bool}``
    so a consumer (and a smaller local model especially) can tell "that is
    everything" apart from "that is the first N of more".
    """
    requested = max(1, int(limit))
    items = rows[:requested]
    return {
        key: items,
        "returned": len(items),
        "limit": requested,
        "truncated": len(rows) > requested,
    }


def pct(used: Any, total: Any) -> float | None:
    """Percentage used/total rounded to 1 decimal, or None when not computable."""
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total:
        return round(used / total * 100, 1)
    return None
