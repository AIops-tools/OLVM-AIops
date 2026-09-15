"""Storage domains against payloads from a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.connection import OlvmApiError
from olvm_aiops.ops import storage as st

pytestmark = pytest.mark.unit

RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


def _routes(extra: dict | None = None) -> MagicMock:
    glob = run("storagedomains_global")
    dc_id = next(s for s in glob["storage_domain"] if s["name"] == "lab-nfs-data")[
        "data_centers"]["data_center"][0]["id"]
    routes = {"/storagedomains": glob,
              f"/datacenters/{dc_id}/storagedomains": run("datacenter_storagedomains"),
              **(extra or {})}
    conn = MagicMock()

    def get(path, params=None):
        value = routes.get(path, {})
        if isinstance(value, Exception):
            raise value
        return value
    conn.get.side_effect = get
    conn.routes = routes
    conn.dc_id = dc_id
    return conn


def test_attached_domain_status_comes_from_its_data_center():
    """Live: the global row has NO status; reading only it would say 'unknown'."""
    out = st.list_storage_domains(_routes())
    nfs = next(d for d in out["storageDomains"] if d["name"] == "lab-nfs-data")
    assert nfs["status"] == "active" and nfs["statusSource"] == "dataCenter"
    assert nfs["master"] is True
    image = next(d for d in out["storageDomains"] if d["name"] == "ovirt-image-repository")
    assert image["status"] == "unattached" and image["statusSource"] == "global"
    assert image["dataCenterIds"] == [] and out["statusErrors"] == []


def test_capacity_is_computed_from_string_bytes():
    nfs = next(d for d in st.list_storage_domains(_routes())["storageDomains"]
               if d["name"] == "lab-nfs-data")
    assert nfs["availableBytes"] == 48318382080 and type(nfs["availableBytes"]) is int
    assert nfs["totalBytes"] == 48318382080 + 7516192768
    assert nfs["usedPct"] == 13.5 and nfs["freePct"] == 86.5
    assert nfs["committedBytes"] == 1073741824
    assert nfs["warningLowSpacePct"] == 10 and nfs["criticalSpaceBlockerGiB"] == 5


def test_unreadable_data_center_leaves_status_null_and_says_why():
    conn = _routes()
    conn.routes[f"/datacenters/{conn.dc_id}/storagedomains"] = OlvmApiError(
        "Not authorized (403)", status_code=403)
    out = st.list_storage_domains(conn)
    nfs = next(d for d in out["storageDomains"] if d["name"] == "lab-nfs-data")
    assert nfs["status"] is None and nfs["statusSource"] is None
    assert out["statusErrors"][0]["dataCenterId"] == conn.dc_id


def test_each_data_center_is_read_once():
    conn = _routes()
    st.list_storage_domains(conn)
    dc_calls = [c for c in conn.get.call_args_list if c.args[0].startswith("/datacenters/")]
    assert len(dc_calls) == 1


def test_get_storage_domain_joins_the_data_center_view():
    single = run("storagedomain_get")
    conn = _routes({f"/storagedomains/{single['id']}": single})
    row = st.get_storage_domain(conn, single["id"])
    assert row["status"] == "active" and row["name"] == "lab-nfs-data"
    with pytest.raises(ValueError, match="no storage domain"):
        st.get_storage_domain(_routes(), "missing")
