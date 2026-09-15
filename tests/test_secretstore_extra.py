"""Edge paths of the encrypted secret store not covered by the roundtrip suite:
master-password resolution, corrupt/old-version blobs, guard rails, permission
warnings, and the no-op migration branch.
"""

from __future__ import annotations

import base64
import json

import pytest

import olvm_aiops.secretstore as ss


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", tmp_path / "secrets.enc")
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ss, "_cached", None)
    return tmp_path


@pytest.mark.unit
def test_resolve_master_password_from_env(monkeypatch):
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, "from-env")
    assert ss.resolve_master_password() == "from-env"


@pytest.mark.unit
def test_resolve_master_password_non_tty_raises(monkeypatch):
    monkeypatch.delenv(ss.MASTER_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ss.MasterPasswordError, match="non-interactively"):
        ss.resolve_master_password()


@pytest.mark.unit
def test_resolve_master_password_prompts_on_tty(store_dir, monkeypatch):
    monkeypatch.delenv(ss.MASTER_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ss.getpass, "getpass", lambda prompt="": "typed-pw")
    assert ss.resolve_master_password() == "typed-pw"


@pytest.mark.unit
def test_resolve_master_password_empty_rejected(store_dir, monkeypatch):
    monkeypatch.delenv(ss.MASTER_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ss.getpass, "getpass", lambda prompt="": "")
    with pytest.raises(ss.MasterPasswordError, match="Empty"):
        ss.resolve_master_password()


@pytest.mark.unit
def test_resolve_master_password_confirm_mismatch(store_dir, monkeypatch):
    monkeypatch.delenv(ss.MASTER_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: True)
    answers = iter(["pw", "different"])
    monkeypatch.setattr(ss.getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(ss.MasterPasswordError, match="did not match"):
        ss.resolve_master_password(confirm_if_new=True)


@pytest.mark.unit
def test_corrupt_json_blob_raises_teaching(store_dir):
    (store_dir / "secrets.enc").write_text("{ not json")
    with pytest.raises(ss.SecretStoreError, match="Could not read"):
        ss.SecretStore.unlock("pw")


@pytest.mark.unit
def test_unsupported_version_rejected(store_dir):
    (store_dir / "secrets.enc").write_text(
        json.dumps(
            {
                "version": 999,
                "salt": base64.b64encode(b"x" * 16).decode(),
                "ciphertext": "zzz",
            }
        )
    )
    with pytest.raises(ss.SecretStoreError, match="Unsupported secret store version"):
        ss.SecretStore.unlock("pw")


@pytest.mark.unit
def test_contains_operator(store_dir):
    store = ss.SecretStore.unlock("pw").set("engine1", "v")
    assert "engine1" in store
    assert "missing" not in store


@pytest.mark.unit
def test_empty_name_rejected(store_dir):
    with pytest.raises(ss.SecretStoreError, match="name must not be empty"):
        ss.SecretStore.unlock("pw").set("", "v")


@pytest.mark.unit
def test_delete_missing_rejected(store_dir):
    with pytest.raises(ss.SecretStoreError, match="to delete"):
        ss.SecretStore.unlock("pw").delete("nope")


@pytest.mark.unit
def test_with_password_empty_rejected(store_dir):
    with pytest.raises(ss.SecretStoreError, match="must not be empty"):
        ss.SecretStore.unlock("pw").with_password("")


@pytest.mark.unit
def test_check_permissions_none_when_no_file(store_dir):
    assert ss.check_permissions() is None


@pytest.mark.unit
def test_check_permissions_warns_when_group_readable(store_dir):
    ss.SecretStore.unlock("pw").set("a", "1")
    (store_dir / "secrets.enc").chmod(0o644)
    warning = ss.check_permissions()
    assert warning is not None
    assert "chmod 600" in warning


@pytest.mark.unit
def test_migrate_no_legacy_file_returns_empty(store_dir):
    assert ss.migrate_legacy_env("OLVM_", "_PASSWORD", "pw") == []


@pytest.mark.unit
def test_open_store_caches_and_get_secret(store_dir, monkeypatch):
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, "pw")
    ss.SecretStore.unlock("pw").set("engine1", "tok")
    monkeypatch.setattr(ss, "_cached", None)
    first = ss.open_store()
    assert ss.open_store() is first  # cached
    assert ss.get_secret("engine1") == "tok"
    assert ss.has_store() is True
