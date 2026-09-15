"""Tests for the ``olvm-aiops init`` onboarding wizard.

Driven end-to-end through Typer's CliRunner with config.yaml and secrets.enc
isolated under tmp_path. The master password comes from
OLVM_AIOPS_MASTER_PASSWORD and the hidden password prompt is patched at the
getpass boundary.
"""

from __future__ import annotations

import getpass as getpass_mod

import pytest
import yaml
from typer.testing import CliRunner

import olvm_aiops.cli.init as init_mod
import olvm_aiops.config as config_mod
import olvm_aiops.doctor as doctor_mod
import olvm_aiops.secretstore as ss

pytestmark = pytest.mark.unit

MASTER_PW = "init-master-pw"
ENGINE_PW = "engine-account-password"  # nosec B105 — test fixture value

# Prompts in order: name, URL, username (blank → default), CA file (blank),
# TLS confirm (blank → True), add another (n), run doctor (n).
WIZARD_INPUT = "engine1\nhttps://engine.example.com\n\n\n\nn\nn\n"


@pytest.fixture
def init_home(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    secrets_file = tmp_path / "secrets.enc"
    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, MASTER_PW)
    monkeypatch.setattr(init_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ss, "_cached", None)
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": ENGINE_PW)
    return tmp_path


def _run_init(input_text: str = WIZARD_INPUT):
    from olvm_aiops.cli import app

    return CliRunner().invoke(app, ["init"], input=input_text)


def _targets(home) -> list[dict]:
    return yaml.safe_load((home / "config.yaml").read_text("utf-8"))["targets"]


def test_init_writes_config_with_keycloak_default_username(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert _targets(init_home) == [{
        "name": "engine1",
        "url": "https://engine.example.com",
        "username": "admin@ovirt@internalsso",
        "verify_ssl": True,
    }]


def test_init_accepts_an_internal_profile_username_and_ca_file(init_home):
    result = _run_init("engine1\nhttps://engine.example.com\nadmin@internal\n"
                       "/etc/pki/engine-ca.pem\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    t = _targets(init_home)[0]
    assert t["username"] == "admin@internal" and t["ca_file"] == "/etc/pki/engine-ca.pem"


def test_init_strips_trailing_slash_from_url(init_home):
    result = _run_init("engine1\nhttps://engine.example.com/\n\n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert _targets(init_home)[0]["url"] == "https://engine.example.com"


def test_init_tls_decline_writes_verify_ssl_false(init_home):
    result = _run_init("engine1\nhttps://engine.example.com\n\n\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert _targets(init_home)[0]["verify_ssl"] is False


def test_init_stores_password_encrypted_not_in_config(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert ss.SecretStore.unlock(MASTER_PW).get("engine1") == ENGINE_PW
    assert ENGINE_PW not in (init_home / "config.yaml").read_text("utf-8")
    assert ENGINE_PW not in (init_home / "secrets.enc").read_text("utf-8")


def test_init_writes_no_policy_rules(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert not (init_home / "rules.yaml").exists()


def test_init_declining_doctor_confirm_skips_doctor(init_home, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: calls.append(True) or 0)
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert calls == []


def test_init_accepting_doctor_confirm_runs_doctor(init_home, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: calls.append(True) or 0)
    result = _run_init("engine1\nhttps://engine.example.com\n\n\n\nn\n\n")
    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_init_overwrite_existing_target(init_home):
    assert _run_init().exit_code == 0
    result = _run_init("engine1\ny\nhttps://engine2.example.com\n\n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert [t["url"] for t in _targets(init_home)] == ["https://engine2.example.com"]
