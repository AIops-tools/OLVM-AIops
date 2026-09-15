"""Configuration management for OLVM AIops.

Loads Oracle Linux Virtualization Manager (OLVM) engine targets from a YAML
config file. OLVM is Oracle's build of oVirt; the REST API at
``/ovirt-engine/api`` is the same, so an upstream oVirt 4.5 engine works too.

The password is NEVER stored in the config file and never on disk in
plaintext: it lives in the encrypted store ``~/.olvm-aiops/secrets.enc`` (see
:mod:`olvm_aiops.secretstore`). A legacy plaintext env var
(``OLVM_<TARGET>_PASSWORD``) is still honoured as a fallback, with a warning.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from olvm_aiops.governance.paths import ops_home
from olvm_aiops.secretstore import (
    MasterPasswordError,
    SecretStoreError,
    get_secret,
    has_store,
)

CONFIG_DIR = ops_home()
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"

API_PATH = "/ovirt-engine/api"
SSO_TOKEN_PATH = "/ovirt-engine/sso/oauth/token"  # nosec B105 — URL path, not a secret
SSO_LOGOUT_PATH = "/ovirt-engine/services/sso-logout"

DEFAULT_TIMEOUT = 30.0
"""Seconds per HTTP request when a target does not set ``timeout``."""

# Legacy env-var prefix/suffix; also used by the migration helper.
SECRET_ENV_PREFIX = "OLVM_"  # nosec B105 — env-var name, not a secret
SECRET_ENV_SUFFIX = "_PASSWORD"  # nosec B105 — env-var name, not a secret

_log = logging.getLogger("olvm-aiops.config")


def _secret_env_key(name: str) -> str:
    """Legacy per-target password env var name, e.g. OLVM_ENGINE1_PASSWORD."""
    return f"{SECRET_ENV_PREFIX}{name.upper().replace('-', '_')}{SECRET_ENV_SUFFIX}"


def _resolve_secret(name: str) -> str:
    """Return a target's password: encrypted store first, then legacy env var."""
    if has_store():
        try:
            return get_secret(name)
        except MasterPasswordError:
            # A wrong or missing master password is NOT "this target has no
            # secret" — falling through would send the operator to add a
            # credential that is already stored.
            raise
        except SecretStoreError:
            pass  # no secret stored for this target — try the legacy env var
    legacy = os.environ.get(_secret_env_key(name))
    if legacy:
        _log.warning(
            "Using plaintext env var %s. Migrate to the encrypted store with "
            "'olvm-aiops secret migrate'.",
            _secret_env_key(name),
        )
        return legacy
    raise OSError(
        f"No password for target '{name}'. Add one with "
        f"'olvm-aiops secret set {name}' (stored encrypted), or run "
        f"'olvm-aiops init'."
    )


@dataclass(frozen=True)
class TargetConfig:
    """An OLVM / oVirt engine REST API connection target.

    ``url`` is the engine's web origin (``https://engine.example.com``), the
    same address the Administration Portal is served from. ``username``
    includes the authorization profile: ``admin@internal`` on an engine without
    Keycloak, ``admin@ovirt@internalsso`` on one where engine-setup enabled
    Keycloak (the default since 4.5.1). ``ca_file`` points at the engine CA
    (``https://<engine>/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA``)
    so TLS can be verified without disabling verification.
    """

    name: str
    url: str
    username: str
    verify_ssl: bool = True
    ca_file: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError(
                f"Target '{self.name}': url must start with https:// (got '{self.url}'). "
                f"Login sends the account password in the request body, so a plain-http "
                f"url would put it on the wire unencrypted; the engine serves its API over "
                f"HTTPS."
            )
        if "@" not in self.username:
            raise ValueError(
                f"Target '{self.name}': username must include its profile, e.g. "
                f"'admin@internal' or 'admin@ovirt@internalsso' (got '{self.username}')."
            )
        if self.timeout <= 0:
            raise ValueError(f"Target '{self.name}': timeout must be positive.")

    @property
    def password(self) -> str:
        return _resolve_secret(self.name)

    @property
    def origin(self) -> str:
        return self.url.rstrip("/")

    @property
    def verify(self) -> bool | str:
        """What httpx should verify against: the engine CA file when given."""
        if not self.verify_ssl:
            return False
        return self.ca_file or True


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config."""

    targets: tuple[TargetConfig, ...] = ()

    def get_target(self, name: str) -> TargetConfig:
        for t in self.targets:
            if t.name == name:
                return t
        available = ", ".join(t.name for t in self.targets) or "(none)"
        raise KeyError(f"Target '{name}' not found. Available: {available}")

    @property
    def default_target(self) -> TargetConfig:
        if not self.targets:
            raise ValueError("No targets configured. Check config.yaml")
        return self.targets[0]


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config from YAML; the password comes from the encrypted store."""
    path = config_path or CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run 'olvm-aiops init' to set up an OLVM engine target and store "
            f"its password encrypted, or create {CONFIG_FILE} with a 'targets' list."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    targets = tuple(
        TargetConfig(
            name=t["name"],
            url=t["url"],
            username=t["username"],
            verify_ssl=bool(t.get("verify_ssl", True)),
            ca_file=(t.get("ca_file") or None),
            timeout=float(t.get("timeout", DEFAULT_TIMEOUT)),
        )
        for t in raw.get("targets", [])
    )

    return AppConfig(targets=targets)
