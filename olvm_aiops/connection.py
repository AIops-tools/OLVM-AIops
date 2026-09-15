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
#: After a failed re-login, further renewals fail fast for this long. Retrying a
#: rotated password on every call (and once per waiting thread) is how an account
#: lockout policy locks the account.
LOGIN_BACKOFF_SECONDS = 60.0


def _seg(value: Any) -> str:
    """Percent-encode one URL *path segment* (agent-supplied ids).

    Prevents path traversal / smuggling when an id like ``../hosts`` is
    interpolated into a REST path. ``quote`` leaves ``.`` and ``..`` intact and
    httpx would normalise them away (``/vms/..`` becomes the API root), so those
    are refused outright — no engine id is a dot segment. Query parameters passed
    via httpx ``params=`` must NOT go through this (httpx encodes those itself).
    """
    text = str(value)
    if text in (".", ".."):
        raise ValueError(f"'{text}' is not a valid engine id.")
    return quote(text, safe="")


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
    reason = str(err.verify_message or err).rstrip(".")
    return (
        f"TLS verification failed for {origin}: {reason}. The engine "
        f"answered, but its certificate could not be trusted — this is not a "
        f"connectivity fault. Use the engine's FQDN in 'url' (its certificate is issued "
        f"to that name, not to an IP), point 'ca_file' at the engine CA "
        f"(/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA), "
        f"or set verify_ssl: false on a throwaway lab engine only."
    )


def _connect_timeout_message(origin: str, timeout: float) -> str:
    return (
        f"Could not connect to {origin} within {timeout:g}s: nothing answered the "
        f"connection attempt. The engine is unreachable — it is down, a firewall drops "
        f"the port, or 'url' names the wrong host. A longer 'timeout' will not help."
    )


def _backoff_error(error: OlvmApiError, remaining: float) -> OlvmApiError:
    return OlvmApiError(
        f"{error} (Not retried: the last login failed "
        f"{LOGIN_BACKOFF_SECONDS - remaining:.0f}s ago; the next attempt is allowed in "
        f"{remaining:.0f}s, so a changed password cannot lock the account.)",
        status_code=error.status_code,
        path=error.path,
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
        self._login_failure: tuple[float, OlvmApiError] | None = None
        # Reads may run concurrently; renewal is serialised so an expired
        # token is replaced once, not once per thread.
        self._auth_lock = threading.Lock()
        try:
            self._login()
        except BaseException:
            if client is None:
                self._client.close()  # a failed login must not leak the client it opened
            raise

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
        except httpx.ConnectTimeout as exc:
            # Before TimeoutException (its parent): no connection was made at all.
            raise OlvmApiError(
                _connect_timeout_message(self._target.origin, self._target.timeout),
                path=SSO_TOKEN_PATH,
                timed_out=True,
            ) from exc
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

    def _parse_success(self, resp: Any, path: str, method: str) -> Any:
        if not resp.content:
            if method == "GET":
                # The engine answers even an empty collection with a JSON body ("{}").
                # An empty GET body is something else answering — reading it as an empty
                # collection would report "no hosts" and call that healthy.
                raise OlvmApiError(
                    f"The engine answered {path} with an empty body. Something other than "
                    f"the engine API responded — check that 'url' is the engine origin and "
                    f"that no proxy is in the way.",
                    status_code=resp.status_code,
                    path=path,
                )
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            # A proxy error page or an SSO login page answering 200 must not read as an
            # empty inventory ("no hosts" -> healthy).
            headers = getattr(resp, "headers", None) or {}
            ctype = headers.get("content-type", "unknown")
            raise OlvmApiError(
                f"The engine answered {path} with a non-JSON body (content-type {ctype}). "
                f"Something other than the engine API responded — check that 'url' is the "
                f"engine origin and that no proxy or login page is in the way.",
                status_code=resp.status_code,
                path=path,
            ) from exc

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
        return self._parse_success(resp, path, method)

    def _refresh_if_expired(self) -> None:
        if self._expires_at is None or time.monotonic() < self._expires_at:
            return
        with self._auth_lock:
            if self._expires_at is not None and time.monotonic() >= self._expires_at:
                self._relogin()

    def _renew(self, seen: str | None) -> None:
        """Log in again unless another thread already replaced the token ``seen``."""
        with self._auth_lock:
            if self._client.headers.get("Authorization") == seen:
                self._relogin()

    def _relogin(self) -> None:
        """Re-authenticate (lock held), failing fast inside the backoff after a failure."""
        if self._login_failure is not None:
            failed_at, error = self._login_failure
            remaining = LOGIN_BACKOFF_SECONDS - (time.monotonic() - failed_at)
            if remaining > 0:
                raise _backoff_error(error, remaining) from error
        try:
            self._login()
        except OlvmApiError as exc:
            self._login_failure = (time.monotonic(), exc)
            raise
        self._login_failure = None

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.ConnectTimeout as exc:
            raise OlvmApiError(
                _connect_timeout_message(self._target.origin, self._target.timeout),
                path=path,
                timed_out=True,
            ) from exc
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

    def probe(self, path: str) -> tuple[int, str]:
        """GET an engine path outside the REST API (e.g. the health servlet).

        Returns the status code and up to 500 characters of text; the body is not
        parsed and a non-2xx status is returned, not raised — the caller judges it.
        """
        resp = self._send("GET", path)
        return resp.status_code, (resp.text or "")[:500]

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
        # A refused first login, per target. Without it a long-running MCP server with a
        # changed password tries a fresh password login on every tool call.
        self._failures: dict[str, tuple[float, OlvmApiError]] = {}
        # Two first calls at once must not each log in (one session would never be revoked).
        self._lock = threading.Lock()
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
        with self._lock:
            cached = self._connections.get(target.name)
            if cached is not None:
                return cached
            failure = self._failures.get(target.name)
            if failure is not None:
                failed_at, error = failure
                remaining = LOGIN_BACKOFF_SECONDS - (time.monotonic() - failed_at)
                if remaining > 0:
                    raise _backoff_error(error, remaining) from error
            try:
                conn = OlvmConnection(target)
            except OlvmApiError as exc:
                # Only a login the engine answered and refused counts; an unreachable
                # engine never sees the password, so it cannot lock the account.
                if exc.status_code is not None:
                    self._failures[target.name] = (time.monotonic(), exc)
                raise
            self._failures.pop(target.name, None)
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


# Managers hold cached clients and live engine sessions; close them at exit. The
# reference is strong on purpose: a manager dropped without disconnect_all() (the
# CLI's was, on every command) would be collected before exit and its engine
# session never revoked.
_MANAGERS: set[ConnectionManager] = set()


def _close_all_managers() -> None:
    for mgr in list(_MANAGERS):
        try:
            mgr.disconnect_all()
        except Exception:  # noqa: BLE001 — exit-time cleanup must never raise
            pass


atexit.register(_close_all_managers)
