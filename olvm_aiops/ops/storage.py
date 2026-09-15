"""Storage domains: inventory with the status that actually applies, and capacity.

Verified on a live OLVM 4.5.5 engine: a storage domain attached to a data center
has NO ``status`` in ``/storagedomains`` (list or single GET) — its status lives
under ``/datacenters/{id}/storagedomains``. Reading only the global collection
reports every attached domain as status-unknown. An unattached domain (e.g. the
default Glance ``ovirt-image-repository``) carries ``status: "unattached"``
globally and belongs to no data center; that is not a fault.

Capacity fields are strings: ``available``, ``used``, ``committed`` (bytes),
``warning_low_space_indicator`` (% free that triggers a warning) and
``critical_space_action_blocker`` (GB free below which the engine refuses to
create disks).
"""

from __future__ import annotations

from typing import Any

from olvm_aiops.connection import OlvmApiError, _seg
from olvm_aiops.ops import _util as u

GIB = 1024**3


def _dc_ids(sd: dict) -> list[str]:
    block = sd.get("data_centers") if isinstance(sd.get("data_centers"), dict) else {}
    refs = block.get("data_center") if isinstance(block.get("data_center"), list) else []
    return [i for i in (u.ref_id(r) for r in refs) if i]


def domain_row(sd: dict, dc_view: dict | None = None) -> dict:
    """One domain; ``dc_view`` is its row from a data center's storagedomains, if attached."""
    storage = sd.get("storage") if isinstance(sd.get("storage"), dict) else {}
    available, used = u.as_int(sd.get("available")), u.as_int(sd.get("used"))
    committed = u.as_int(sd.get("committed"))
    total = available + used if available is not None and used is not None else None
    if dc_view is not None and dc_view.get("status"):
        status, source = u.text(dc_view.get("status"), 32), "dataCenter"
    elif sd.get("status"):
        status, source = u.text(sd.get("status"), 32), "global"
    else:
        status, source = None, None
    master = dc_view.get("master") if dc_view is not None else sd.get("master")
    return {
        "id": u.text(sd.get("id"), 64),
        "name": u.text(sd.get("name")),
        "type": u.text(sd.get("type"), 32),
        "storageType": u.text(storage.get("type"), 32),
        "status": status,
        "statusSource": source,
        "master": u.as_bool(master),
        "dataCenterIds": _dc_ids(sd),
        "availableBytes": available,
        "usedBytes": used,
        "committedBytes": committed,
        "totalBytes": total,
        "usedPct": u.pct(used, total),
        "freePct": u.pct(available, total),
        "committedPctOfTotal": u.pct(committed, total),
        "warningLowSpacePct": u.as_int(sd.get("warning_low_space_indicator")),
        "criticalSpaceBlockerGiB": u.as_int(sd.get("critical_space_action_blocker")),
        "externalStatus": u.text(sd.get("external_status"), 32),
    }


def _dc_views(conn: Any, dc_ids: set[str]) -> tuple[dict[str, dict], list[dict]]:
    """Domain rows keyed by domain id, from each data center's collection."""
    views: dict[str, dict] = {}
    errors: list[dict] = []
    for dc_id in sorted(dc_ids):
        try:
            rows = u.items(conn.get(f"/datacenters/{_seg(dc_id)}/storagedomains"),
                           "storage_domain")
        except (OlvmApiError, ValueError) as exc:
            errors.append({"dataCenterId": dc_id, "error": str(exc)[:300]})
            continue
        for row in rows:
            if row.get("id"):
                views[str(row["id"])] = row
    return views, errors


def list_storage_domains(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT) -> dict:
    """[READ] Storage domains with data-center-scoped status and capacity.

    ``statusSource`` says where a status came from; ``status`` is null (never
    guessed) when an attached domain's data center could not be read, and that
    failure is listed in ``statusErrors``.
    """
    rows, truncated = u.fetch_page(conn, "/storagedomains", "storage_domain", limit)
    views, errors = _dc_views(conn, {i for r in rows for i in _dc_ids(r)})
    domains = [domain_row(r, views.get(str(r.get("id")))) for r in rows]
    return {**u.envelope("storageDomains", domains, limit, truncated), "statusErrors": errors}


def get_storage_domain(conn: Any, domain_id: str) -> dict:
    """[READ] One storage domain by id, with its data-center-scoped status."""
    if not str(domain_id or "").strip():
        raise ValueError("domain_id is required.")
    sd = conn.get(f"/storagedomains/{_seg(domain_id)}")
    if not isinstance(sd, dict) or not sd.get("id"):
        raise ValueError(f"The engine returned no storage domain for id '{domain_id}'.")
    views, errors = _dc_views(conn, set(_dc_ids(sd)))
    return {**domain_row(sd, views.get(str(sd["id"]))), "statusErrors": errors}
