"""Connection management for the OLVM / oVirt engine REST API.

Thin httpx wrapper with per-target session reuse and OAuth bearer auth:

  * ``POST /ovirt-engine/sso/oauth/token`` with ``grant_type=password``,
    ``scope=ovirt-app-api`` (mandatory — the engine rejects a token request
    without it), ``username`` and ``password`` yields an ``access_token``.
  * Every API request carries ``Authorization: Bearer <token>``,
    ``Version: 4`` and ``Accept: application/json``. Paths passed to
    :meth:`OlvmConnection.request` are relative to ``/ovirt-engine/api``.
  * The engine issues no refresh token: an expired token answers 401 and the
    only remedy is a new login. A 401 is refused before the handler runs, so
    the request is retried exactly once after re-authenticating.

The official Python SDK is deliberately not used: it depends on ``pycurl`` and
ships as a source distribution only, so installing it needs a C toolchain and
libcurl headers — an ``uvx`` install would fail on machines without them.

All non-2xx responses are translated centrally into ``OlvmApiError`` carrying
the engine's own ``fault`` reason and detail.
"""

from __future__ import annotations

import atexit
import ssl
import threading
import time
import weakref
from typing import Any
from urllib.parse import quote

import httpx

from olvm_aiops.config import (
    API_PATH,
    SSO_LOGOUT_PATH,
    SSO_TOKEN_PATH,
    AppConfig,
    TargetConfig,
    load_config,
)

API_VERSION = "4"


def _seg(value: Any) -> str:
    """Percent-encode one URL *path segment* (agent-supplied ids).

    Prevents path traversal / smuggling when an id like ``../hosts`` is
    interpolated into a REST path. Query parameters passed via httpx
    ``params=`` must NOT go through this (httpx encodes those itself).
    """
    return quote(str(value), safe="")


def _tls_verify_failure(exc: BaseException) -> ssl.SSLCertVerificationError | None:
    """The certificate-verification error behind an httpx transport error, if any."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


def _tls_message(origin: str, err: ssl.SSLCertVerificationError) -> str:
    return (
        f"TLS verification failed for {origin}: {err.verify_message or err}. The engine "
        f"answered, but its certificate could not be trusted — this is not a "
        f"connectivity fault. Use the engine's FQDN in 'url' (its certificate is issued "
        f"to that name, not to an IP), point 'ca_file' at the engine CA "
        f"(/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA), "
        f"or set verify_ssl: false on a throwaway lab engine only."
    )


class OlvmApiError(Exception):
    """An engine REST call failed; carries a teaching message + status code."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str = "",
        timed_out: bool = False,
    ) -> None:
        self.status_code = status_code
        self.path = path
        self.timed_out = timed_out
        super().__init__(message)


def _fault_text(resp: Any) -> str:
    """The engine's own explanation: ``fault.reason`` + ``fault.detail`` when present."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300].strip()
    if isinstance(body, dict):
        fault = body.get("fault") if isinstance(body.get("fault"), dict) else body
        parts = [str(fault.get(k)) for k in ("reason", "detail") if fault.get(k)]
        if parts:
            return " — ".join(parts)[:500]
    return (resp.text or "")[:300].strip()


def _teaching_message(status: int, path: str, detail: str) -> str:
    """Map a non-2xx status to an actionable, teaching error message."""
    if status == 401:
        return (
            f"Authentication failed (401) on {path}. Check the username (it must include "
            f"the profile: 'admin@internal', or 'admin@ovirt@internalsso' when Keycloak "
            f"is enabled) and the stored password. {detail}"
        )
    if status == 403:
        return (
            f"Not authorized (403) on {path}. The account is valid but its role does not "
            f"permit this — grant a role on the object in the Administration Portal. {detail}"
        )
    if status == 404:
        return (
            f"Not found (404) on {path}. The id may be stale — list the parent collection "
            f"first to get a current id. {detail}"
        )
    if status in (400, 409):
        return f"The engine rejected the request ({status}) on {path}: {detail}"
    if status in (500, 502, 503, 504):
        return (
            f"Engine error ({status}) on {path}. The engine may be starting or busy; "
            f"retry shortly. {detail}"
        )
    return f"Engine API error ({status}) on {path}. {detail}"


class OlvmConnection:
    """A single authenticated session against one OLVM / oVirt engine."""

    def __init__(self, target: TargetConfig, client: Any | None = None) -> None:
        self._target = target
        self._client = client if client is not None else httpx.Client(
            base_url=target.origin,
            verify=target.verify,
            timeout=target.timeout,
            headers={"Accept": "application/json", "Version": API_VERSION},
        )
        self._token: str | None = None
        self._expires_at: float | None = None
        # Reads may run concurrently; renewal is serialised so an expired
        # token is replaced once, not once per thread.
        self._auth_lock = threading.Lock()
        self._login()

    @property
    def target(self) -> TargetConfig:
        return self._target

    def _login(self) -> None:
        """Obtain a bearer token from the engine's SSO service."""
        data = {
            "grant_type": "password",
            "scope": "ovirt-app-api",
            "username": self._target.username,
            "password": self._target.password,
        }
        try:
            resp = self._client.post(
                SSO_TOKEN_PATH,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise OlvmApiError(
                f"Login to {self._target.origin} timed out after "
                f"{self._target.timeout:g}s; the engine accepted the connection and did "
                f"not answer. Raise 'timeout' for this target if the engine is busy.",
                path=SSO_TOKEN_PATH,
                timed_out=True,
            ) from exc
        except httpx.HTTPError as exc:
            tls = _tls_verify_failure(exc)
            if tls is not None:
                raise OlvmApiError(_tls_message(self._target.origin, tls),
                                   path=SSO_TOKEN_PATH) from exc
            raise OlvmApiError(
                f"Could not reach the engine at {self._target.origin}: {exc}. Check the "
                f"url and that ovirt-engine is running.",
                path=SSO_TOKEN_PATH,
            ) from exc
        body = self._json(resp)
        token = body.get("access_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            reason = ""
            if isinstance(body, dict):
                reason = str(body.get("error_description") or body.get("error") or "")
            raise OlvmApiError(
                f"Engine SSO login failed ({resp.status_code}) for "
                f"'{self._target.username}': {reason or 'no access_token returned'}. "
                f"The username must include its profile ('admin@internal', or "
                f"'admin@ovirt@internalsso' when Keycloak is enabled).",
                status_code=resp.status_code,
                path=SSO_TOKEN_PATH,
            )
        self._token = str(token)
        self._client.headers["Authorization"] = f"Bearer {self._token}"
        # Absent is NOT zero: without a reported lifetime, renewal is reactive.
        lifetime = body.get("expires_in")
        if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool) and lifetime > 0:
            self._expires_at = time.monotonic() + float(lifetime) - min(60.0, lifetime / 2)
        else:
            self._expires_at = None

    @staticmethod
    def _json(resp: Any) -> Any:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue an API request (path relative to /ovirt-engine/api) and return JSON."""
        self._refresh_if_expired()
        seen = self._client.headers.get("Authorization")
        full = f"{API_PATH}{path}"
        resp = self._send(method, full, **kwargs)
        if resp.status_code == 401:
            # Refused before the handler ran: retrying cannot apply a write
            # twice. Exactly once — a revoked account must surface, not loop.
            self._renew(seen)
            resp = self._send(method, full, **kwargs)
        if not (200 <= resp.status_code < 300):
            raise OlvmApiError(
                _teaching_message(resp.status_code, path, _fault_text(resp)),
                status_code=resp.status_code,
                path=path,
            )
        return self._json(resp)

    def _refresh_if_expired(self) -> None:
        if self._expires_at is None or time.monotonic() < self._expires_at:
            return
        with self._auth_lock:
            if self._expires_at is not None and time.monotonic() >= self._expires_at:
                self._login()

    def _renew(self, seen: str | None) -> None:
        """Log in again unless another thread already replaced the token ``seen``."""
        with self._auth_lock:
            if self._client.headers.get("Authorization") == seen:
                self._login()

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            # Before the generic branch: TimeoutException subclasses HTTPError,
            # and "check connectivity" would misdirect the operator.
            raise OlvmApiError(
                f"{method} {path} timed out after {self._target.timeout:g}s. The engine "
                f"accepted the connection and did not answer in time — not a "
                f"connectivity fault. Raise 'timeout' for this target in config.yaml.",
                path=path,
                timed_out=True,
            ) from exc
        except httpx.HTTPError as exc:
            tls = _tls_verify_failure(exc)
            if tls is not None:
                raise OlvmApiError(_tls_message(self._target.origin, tls), path=path) from exc
            raise OlvmApiError(
                f"Transport error on {method} {path}: {exc}. Check connectivity to "
                f"{self._target.origin}.",
                path=path,
            ) from exc

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        """Revoke the SSO token (best effort) and close the client.

        Each login opens an engine session; leaving them to expire piles up
        sessions on the engine for every CLI invocation.
        """
        if self._token:
            try:
                self._client.post(
                    SSO_LOGOUT_PATH,
                    data={"scope": "ovirt-app-api", "token": self._token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError:
                pass  # revocation is hygiene; the session expires on its own
            self._token = None
        self._client.close()


class ConnectionManager:
    """Manages connections to multiple engine targets with session reuse."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, OlvmConnection] = {}
        _MANAGERS.add(self)

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        return cls(config or load_config())

    def connect(self, target_name: str | None = None) -> OlvmConnection:
        """Connect to a target by name, or the default target."""
        target = (
            self._config.get_target(target_name)
            if target_name
            else self._config.default_target
        )
        cached = self._connections.get(target.name)
        if cached is not None:
            return cached
        conn = OlvmConnection(target)
        self._connections[target.name] = conn
        return conn

    def disconnect(self, target_name: str) -> None:
        conn = self._connections.pop(target_name, None)
        if conn is not None:
            conn.close()

    def disconnect_all(self) -> None:
        for name in list(self._connections):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        return [t.name for t in self._config.targets]

    def list_connected(self) -> list[str]:
        return list(self._connections.keys())


# Managers hold cached clients and live engine sessions; close them at exit.
_MANAGERS: weakref.WeakSet[ConnectionManager] = weakref.WeakSet()


def _close_all_managers() -> None:
    for mgr in list(_MANAGERS):
        try:
            mgr.disconnect_all()
        except Exception:  # noqa: BLE001 — exit-time cleanup must never raise
            pass


atexit.register(_close_all_managers)
