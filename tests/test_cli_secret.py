"""CLI ``secret`` sub-commands — set / list / rm / migrate / rotate-password.

Drives each command through the real encrypted store redirected to a tmp dir,
so the command bodies (which the smoke test only reaches via --help) execute
end to end. The master password is supplied via the env var so nothing prompts.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import olvm_aiops.secretstore as ss

runner = CliRunner()


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Redirect the store to tmp and provide the master password via env."""
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", tmp_path / "secrets.enc")
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ss, "_cached", None)
    # cli/secret.py imports these names into its own namespace at import time.
    import olvm_aiops.cli.secret as secret_cli

    monkeypatch.setattr(secret_cli, "SECRETS_FILE", tmp_path / "secrets.enc")
    monkeypatch.setenv("OLVM_AIOPS_MASTER_PASSWORD", "test-master-pw")
    return tmp_path


@pytest.mark.unit
def test_secret_set_with_value_then_list(store_env):
    from olvm_aiops.cli import app

    r = runner.invoke(app, ["secret", "set", "engine1", "--value", "tok-123"])
    assert r.exit_code == 0, r.output
    assert "Stored encrypted" in r.output

    r = runner.invoke(app, ["secret", "list"])
    assert r.exit_code == 0, r.output
    assert "engine1" in r.output
    # value is never printed
    assert "tok-123" not in r.output


@pytest.mark.unit
def test_secret_set_prompts_when_value_omitted(store_env, monkeypatch):
    import olvm_aiops.cli.secret as secret_cli
    from olvm_aiops.cli import app

    monkeypatch.setattr(secret_cli.getpass, "getpass", lambda prompt="": "prompted-pw")
    r = runner.invoke(app, ["secret", "set", "engine2"])
    assert r.exit_code == 0, r.output
    assert ss.SecretStore.unlock("test-master-pw").get("engine2") == "prompted-pw"


@pytest.mark.unit
def test_secret_list_empty_hints_to_add_one(store_env):
    from olvm_aiops.cli import app

    r = runner.invoke(app, ["secret", "list"])
    assert r.exit_code == 0, r.output
    assert "No secrets stored yet" in r.output


@pytest.mark.unit
def test_secret_rm(store_env):
    from olvm_aiops.cli import app

    runner.invoke(app, ["secret", "set", "engine1", "--value", "v"])
    r = runner.invoke(app, ["secret", "rm", "engine1"])
    assert r.exit_code == 0, r.output
    assert "Deleted" in r.output
    assert ss.SecretStore.unlock("test-master-pw").names() == ()


@pytest.mark.unit
def test_secret_migrate_imports_legacy_env(store_env):
    from olvm_aiops.cli import app

    (store_env / ".env").write_text("OLVM_ENGINE1_PASSWORD=legacy-pw\n")
    r = runner.invoke(app, ["secret", "migrate"])
    assert r.exit_code == 0, r.output
    assert "Imported 1 secret" in r.output
    assert ss.SecretStore.unlock("test-master-pw").get("engine1") == "legacy-pw"


@pytest.mark.unit
def test_secret_migrate_nothing_to_do(store_env):
    from olvm_aiops.cli import app

    r = runner.invoke(app, ["secret", "migrate"])
    assert r.exit_code == 0, r.output
    assert "Nothing to migrate" in r.output


@pytest.mark.unit
def test_secret_rotate_password_success(store_env, monkeypatch):
    import olvm_aiops.cli.secret as secret_cli
    from olvm_aiops.cli import app

    runner.invoke(app, ["secret", "set", "engine1", "--value", "v"])
    monkeypatch.setattr(secret_cli.getpass, "getpass", lambda prompt="": "new-pw")
    r = runner.invoke(app, ["secret", "rotate-password"])
    assert r.exit_code == 0, r.output
    assert "rotated" in r.output.lower()
    assert ss.SecretStore.unlock("new-pw").get("engine1") == "v"


@pytest.mark.unit
def test_secret_rotate_password_mismatch_aborts(store_env, monkeypatch):
    import olvm_aiops.cli.secret as secret_cli
    from olvm_aiops.cli import app

    runner.invoke(app, ["secret", "set", "engine1", "--value", "v"])
    answers = iter(["new-pw", "different-pw"])
    monkeypatch.setattr(
        secret_cli.getpass, "getpass", lambda prompt="": next(answers)
    )
    r = runner.invoke(app, ["secret", "rotate-password"])
    assert r.exit_code == 1
    assert "did not match" in r.output
