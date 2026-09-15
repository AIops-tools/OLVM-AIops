"""Tests for ``olvm_aiops.doctor.run_doctor``.

All filesystem paths are redirected to a tmp dir and the connection layer is
mocked at the ConnectionManager boundary — no test touches a real engine or
the real ``~/.olvm-aiops``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

import olvm_aiops.config as config_mod
import olvm_aiops.doctor as doctor_mod
import olvm_aiops.secretstore as ss
from olvm_aiops.doctor import run_doctor

pytestmark = pytest.mark.unit

MASTER_PW = "test-master-pw"
API_ROOT = {"product_info": {"name": "oVirt Engine",
                             "version": {"full_version": "4.5.5-1.73.el9"}}}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect every config/secret path constant at a throwaway directory."""
    config_file = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    secrets_file = tmp_path / "secrets.enc"

    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, MASTER_PW)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "ENV_FILE", env_file)
    monkeypatch.setattr(doctor_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(doctor_mod, "ENV_FILE", env_file)
    monkeypatch.setattr(doctor_mod, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", env_file)
    monkeypatch.setattr(ss, "_cached", None)
    return tmp_path


def _write_config(home, targets: list[dict]) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump({"targets": targets}), "utf-8")


def _target(name: str = "engine1", **extra) -> dict:
    return {"name": name, "url": "https://engine.example.com",
            "username": "admin@internal", **extra}


def _store_secret(name: str = "engine1", value: str = "engine-pw") -> None:
    ss.SecretStore.unlock(MASTER_PW).set(name, value)


@pytest.fixture
def ok_connection(monkeypatch):
    """A ConnectionManager whose connections answer the API root."""
    mgr = MagicMock(name="ConnectionManager")
    mgr.return_value.connect.return_value.get.return_value = API_ROOT
    monkeypatch.setattr("olvm_aiops.connection.ConnectionManager", mgr)
    return mgr


def _out(capsys) -> str:
    return " ".join(capsys.readouterr().out.split())


def test_missing_config_file(isolated_home, capsys):
    assert run_doctor() == 1
    assert "Config file missing" in capsys.readouterr().out


def test_config_load_failure_reported_not_raised(isolated_home, capsys):
    # No username: load_config raises, the doctor reports it as a check.
    _write_config(isolated_home, [{"name": "engine1", "url": "https://engine.example.com"}])
    assert run_doctor() == 1
    assert "Config load failed" in capsys.readouterr().out


def test_no_targets_configured(isolated_home, capsys):
    _write_config(isolated_home, [])
    assert run_doctor() == 1
    assert "No targets configured" in capsys.readouterr().out


def test_all_healthy_reports_user_and_engine_version(isolated_home, ok_connection, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()
    assert run_doctor() == 0
    out = _out(capsys)
    assert "Password present for 'engine1'" in out
    assert ("Connected to 'engine1' (https://engine.example.com) as admin@internal — "
            "oVirt Engine 4.5.5-1.73.el9") in out
    ok_connection.return_value.connect.assert_called_once_with("engine1")
    ok_connection.return_value.connect.return_value.get.assert_called_once_with("")
    ok_connection.return_value.disconnect_all.assert_called_once()


def test_missing_version_is_said_not_invented(isolated_home, ok_connection, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()
    ok_connection.return_value.connect.return_value.get.return_value = {}
    assert run_doctor() == 0
    assert "version not reported" in _out(capsys)


def test_failed_login_is_a_problem(isolated_home, ok_connection, capsys):
    from olvm_aiops.connection import OlvmApiError

    _write_config(isolated_home, [_target()])
    _store_secret()
    ok_connection.return_value.connect.side_effect = OlvmApiError(
        "Engine SSO login failed (400) for 'admin@internal'", status_code=400)
    assert run_doctor() == 1
    out = _out(capsys)
    assert "Connect to 'engine1' failed" in out and "400" in out


def test_skip_auth_never_touches_connection_layer(isolated_home, monkeypatch, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()

    def _boom(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("ConnectionManager must not be constructed with --skip-auth")

    monkeypatch.setattr("olvm_aiops.connection.ConnectionManager", _boom)
    assert run_doctor(skip_auth=True) == 0
    assert "Skipping connectivity check" in capsys.readouterr().out


def test_missing_secret_is_a_problem(isolated_home, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret("other-target")
    assert run_doctor(skip_auth=True) == 1
    assert "No password for target 'engine1'" in _out(capsys)


def test_no_secret_store_yet_warns_and_fails(isolated_home, capsys):
    _write_config(isolated_home, [_target()])
    assert run_doctor(skip_auth=True) == 1
    assert "No secret store yet" in capsys.readouterr().out


def test_legacy_env_file_warns_but_env_secret_passes(isolated_home, monkeypatch, capsys):
    _write_config(isolated_home, [_target()])
    (isolated_home / ".env").write_text("OLVM_ENGINE1_PASSWORD=legacy\n")
    monkeypatch.setenv("OLVM_ENGINE1_PASSWORD", "legacy")
    assert run_doctor(skip_auth=True) == 0
    out = _out(capsys)
    assert "legacy plaintext .env" in out and "Password present for 'engine1'" in out


def test_disabled_tls_verification_is_flagged(isolated_home, capsys):
    _write_config(isolated_home, [_target(verify_ssl=False)])
    _store_secret()
    assert run_doctor(skip_auth=True) == 0
    assert "verify_ssl: false" in _out(capsys)


def test_connect_failure_reported_per_target(isolated_home, ok_connection, capsys):
    _write_config(isolated_home, [_target("engine-a"), _target("engine-b")])
    _store_secret("engine-a")
    _store_secret("engine-b")

    def _connect(name):
        if name == "engine-b":
            raise ConnectionError("connection refused")
        conn = MagicMock()
        conn.get.return_value = API_ROOT
        return conn

    ok_connection.return_value.connect.side_effect = _connect
    assert run_doctor() == 1
    out = _out(capsys)
    assert "Connected to 'engine-a'" in out
    assert "Connect to 'engine-b' failed: connection refused" in out


def test_permission_warning_surfaced(isolated_home, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()
    (isolated_home / "secrets.enc").chmod(0o644)
    assert run_doctor(skip_auth=True) == 0
    assert "should be 600" in _out(capsys)


def test_wrong_master_password_is_reported_not_raised(isolated_home, monkeypatch, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, "not-the-master-password")
    monkeypatch.setattr(ss, "_cached", None)
    assert run_doctor(skip_auth=True) == 1
    assert "Wrong master password" in _out(capsys)


def test_engine_text_with_markup_does_not_break_the_report(isolated_home, ok_connection, capsys):
    _write_config(isolated_home, [_target()])
    _store_secret()
    ok_connection.return_value.connect.return_value.get.return_value = {
        "product_info": {"name": "[link=https://evil.example]Engine[/link]"}}
    assert run_doctor() == 0
    assert "[link=https://evil.example]Engine[/link]" in _out(capsys)
