"""Connection + config against a scripted engine (httpx.MockTransport).

Never touches a real engine. Pins: the SSO login form (scope is mandatory), the
Version/Accept/Bearer headers, paths rooted at /ovirt-engine/api, fault
reason/detail in errors, re-login-once on 401, timeout vs transport errors, and
TargetConfig validation.
"""

from __future__ import annotations

import threading
from urllib.parse import parse_qs

import httpx
import pytest

from olvm_aiops.config import AppConfig, TargetConfig, load_config
from olvm_aiops.connection import ConnectionManager, OlvmApiError, OlvmConnection, _seg


def _target(**kw) -> TargetConfig:
    base = {"name": "engine1", "url": "https://engine.example.com",
            "username": "admin@internal", "verify_ssl": False}
    base.update(kw)
    return TargetConfig(**base)


class Engine:
    """A scripted engine: counts logins, serves the API, can expire tokens."""

    def __init__(self, api=None, token_status: int = 200, expires_in=None):
        self.logins = 0
        self.forms: list[dict] = []
        self.api_calls: list[httpx.Request] = []
        self.api = api or (lambda req: httpx.Response(200, json={"ok": True}))
        self.token_status = token_status
        self.expires_in = expires_in
        self.revoked: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ovirt-engine/sso/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            with self.lock:
                self.logins += 1
                self.forms.append(form)
                n = self.logins
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={
                    "error": "access_denied", "error_description": "Cannot authenticate user"})
            body = {"access_token": f"TOK{n}", "token_type": "bearer"}
            if self.expires_in is not None:
                body["expires_in"] = self.expires_in
            return httpx.Response(200, json=body)
        if path == "/ovirt-engine/services/sso-logout":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200, json={})
        with self.lock:
            self.api_calls.append(request)
        return self.api(request)


def _conn(engine: Engine, **kw) -> OlvmConnection:
    target = _target(**kw)
    client = httpx.Client(base_url=target.origin, transport=httpx.MockTransport(engine),
                          headers={"Accept": "application/json", "Version": "4"})
    return OlvmConnection(target, client=client)


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    monkeypatch.setenv("OLVM_ENGINE1_PASSWORD", "pw")
    monkeypatch.setenv("OLVM_ENGINE2_PASSWORD", "pw2")


# ─── login and headers ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_login_sends_the_mandatory_scope_and_the_full_username():
    engine = Engine()
    _conn(engine, username="admin@ovirt@internalsso")
    assert engine.forms[0] == {"grant_type": "password", "scope": "ovirt-app-api",
                               "username": "admin@ovirt@internalsso", "password": "pw"}


@pytest.mark.unit
def test_api_requests_are_rooted_and_carry_version_accept_and_bearer():
    engine = Engine()
    conn = _conn(engine)
    assert conn.get("/hosts") == {"ok": True}
    req = engine.api_calls[0]
    assert req.url.path == "/ovirt-engine/api/hosts"
    assert req.headers["authorization"] == "Bearer TOK1"
    assert req.headers["version"] == "4"
    assert req.headers["accept"] == "application/json"


@pytest.mark.unit
def test_empty_path_reads_the_api_root():
    engine = Engine()
    _conn(engine).get("")
    assert engine.api_calls[0].url.path == "/ovirt-engine/api"


@pytest.mark.unit
def test_failed_login_names_the_profile_form_of_the_username():
    engine = Engine(token_status=400)
    with pytest.raises(OlvmApiError) as ei:
        _conn(engine)
    msg = str(ei.value)
    assert "Cannot authenticate user" in msg
    assert "admin@ovirt@internalsso" in msg and ei.value.status_code == 400


# ─── errors ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(("status", "needle"), [
    (400, "rejected the request"), (403, "Not authorized"), (404, "Not found"),
    (409, "rejected the request"), (503, "Engine error"), (418, "Engine API error")])
def test_status_classes_map_to_teaching_messages(status, needle):
    engine = Engine(api=lambda req: httpx.Response(status, json={
        "reason": "Operation Failed", "detail": "[Cannot remove VM. VM is running.]"}))
    with pytest.raises(OlvmApiError) as ei:
        _conn(engine).get("/vms/1")
    assert needle in str(ei.value) and ei.value.status_code == status
    assert "VM is running" in str(ei.value), "the engine's own fault detail must survive"


@pytest.mark.unit
def test_fault_nested_under_a_fault_key_is_read_too():
    engine = Engine(api=lambda req: httpx.Response(400, json={
        "fault": {"reason": "Incomplete parameters", "detail": "Vm [name] required for add"}}))
    with pytest.raises(OlvmApiError) as ei:
        _conn(engine).post("/vms", json={})
    assert "Vm [name] required for add" in str(ei.value)


@pytest.mark.unit
def test_empty_success_body_is_an_empty_dict():
    engine = Engine(api=lambda req: httpx.Response(200, content=b""))
    assert _conn(engine).get("/x") == {}


@pytest.mark.unit
def test_timeout_is_reported_as_a_timeout_not_a_connectivity_fault():
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(OlvmApiError) as ei:
        _conn(Engine(api=slow)).get("/events")
    assert ei.value.timed_out is True
    assert "timed out" in str(ei.value) and "not a connectivity fault" in str(ei.value)


@pytest.mark.unit
def test_transport_error_is_not_flagged_as_a_timeout():
    def refused(req):
        raise httpx.ConnectError("refused", request=req)
    with pytest.raises(OlvmApiError) as ei:
        _conn(Engine(api=refused)).get("/events")
    assert ei.value.timed_out is False and "Transport error" in str(ei.value)


# ─── token renewal ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_401_renews_the_token_and_retries_once_with_the_new_one():
    """The engine issues no refresh token; an expired one answers 401."""
    seen: list[str] = []

    def api(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(401 if req.headers["authorization"] == "Bearer TOK1" else 200,
                              json={"ok": True})
    engine = Engine(api=api)
    assert _conn(engine).get("/vms") == {"ok": True}
    assert seen == ["Bearer TOK1", "Bearer TOK2"] and engine.logins == 2


@pytest.mark.unit
def test_a_persistent_401_is_retried_once_then_reported():
    engine = Engine(api=lambda req: httpx.Response(401, json={}))
    with pytest.raises(OlvmApiError) as ei:
        _conn(engine).get("/vms")
    assert ei.value.status_code == 401 and engine.logins == 2 and len(engine.api_calls) == 2


@pytest.mark.unit
def test_a_success_does_not_re_authenticate():
    engine = Engine()
    conn = _conn(engine)
    conn.get("/vms")
    conn.get("/hosts")
    assert engine.logins == 1


@pytest.mark.unit
def test_concurrent_401s_renew_once():
    stale = threading.Barrier(4, timeout=5)

    def api(req):
        if req.headers["authorization"] == "Bearer TOK1":
            stale.wait()
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"ok": True})
    engine = Engine(api=api)
    conn = _conn(engine)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(conn.get("/vms")))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [{"ok": True}] * 4 and engine.logins == 2


@pytest.mark.unit
def test_reported_lifetime_triggers_proactive_renewal():
    engine = Engine(expires_in=1)
    conn = _conn(engine)
    conn._expires_at = 0.0
    conn.get("/vms")
    assert engine.logins == 2


@pytest.mark.unit
def test_close_revokes_the_token():
    engine = Engine()
    conn = _conn(engine)
    conn.close()
    assert engine.revoked == ["TOK1"]


# ─── config ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_seg_encodes_traversal():
    assert _seg("../hosts") == "..%2Fhosts"


@pytest.mark.unit
@pytest.mark.parametrize(("kw", "needle"), [
    ({"url": "engine.example.com"}, "https://"),
    ({"username": "admin"}, "profile"),
    ({"timeout": 0}, "timeout")])
def test_target_validation(kw, needle):
    with pytest.raises(ValueError, match=needle):
        _target(**kw)


@pytest.mark.unit
def test_verify_prefers_the_ca_file_and_honours_disabling():
    assert _target(verify_ssl=True, ca_file="/etc/ca.pem").verify == "/etc/ca.pem"
    assert _target(verify_ssl=True).verify is True
    assert _target(verify_ssl=False, ca_file="/etc/ca.pem").verify is False


@pytest.mark.unit
def test_load_config_reads_every_field(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "targets:\n"
        "  - name: engine1\n"
        "    url: https://engine.example.com\n"
        "    username: admin@ovirt@internalsso\n"
        "    ca_file: /etc/pki/engine-ca.pem\n"
        "    timeout: 120\n")
    t = load_config(cfg).targets[0]
    assert (t.username, t.ca_file, t.timeout, t.verify_ssl) == (
        "admin@ovirt@internalsso", "/etc/pki/engine-ca.pem", 120.0, True)


@pytest.mark.unit
def test_manager_caches_per_target(monkeypatch):
    made: list[str] = []

    class FakeConn:
        def __init__(self, target):
            made.append(target.name)
            self.target = target

        def close(self):
            pass

    monkeypatch.setattr("olvm_aiops.connection.OlvmConnection", FakeConn)
    mgr = ConnectionManager(AppConfig(targets=(_target(), _target(name="engine2"))))
    assert mgr.connect() is mgr.connect("engine1")
    mgr.connect("engine2")
    assert made == ["engine1", "engine2"]
    mgr.disconnect_all()
    assert mgr.list_connected() == []
