from pathlib import Path

import pytest


def test_secure_store_rejects_non_os_backend(monkeypatch):
    import keyring.backend
    import bulkuploader.secure_store as secure_store

    class PlainBackend:
        priority = 10

    PlainBackend.__module__ = "keyrings.alt.file"
    monkeypatch.setattr(keyring.backend, "get_all_keyring", lambda: [PlainBackend()])
    assert secure_store.secure_backend() is None


def test_secure_store_accepts_secret_service_backend(monkeypatch):
    import keyring.backend
    import bulkuploader.secure_store as secure_store

    class SecretServiceBackend:
        priority = 5

    SecretServiceBackend.__module__ = "keyring.backends.secretservice"
    backend = SecretServiceBackend()
    monkeypatch.setattr(keyring.backend, "get_all_keyring", lambda: [backend])
    assert secure_store.secure_backend() is backend


def test_api_hash_is_never_written_to_config_without_keyring(tmp_path: Path, monkeypatch):
    import json
    import bulkuploader.app as app
    import bulkuploader.secure_store as secure_store

    config = tmp_path / "telegram-api.json"
    monkeypatch.setattr(app, "TELEGRAM_CONFIG_PATH", config)
    monkeypatch.setattr(secure_store, "set_secret", lambda name, value: False)

    assert app._save_telegram_api_config(12345, "not-on-disk") is False
    assert json.loads(config.read_text(encoding="utf-8")) == {"api_id": 12345}
    assert "not-on-disk" not in config.read_text(encoding="utf-8")


def test_legacy_api_hash_is_removed_even_when_keyring_is_unavailable(tmp_path: Path, monkeypatch):
    import json
    import bulkuploader.app as app
    import bulkuploader.secure_store as secure_store

    config = tmp_path / "telegram-api.json"
    config.write_text(json.dumps({"api_id": 24680, "api_hash": "legacy-only-in-memory"}), encoding="utf-8")
    monkeypatch.setattr(app, "TELEGRAM_CONFIG_PATH", config)
    monkeypatch.setattr(secure_store, "set_secret", lambda name, value: False)
    monkeypatch.setattr(secure_store, "get_secret", lambda name: None)

    assert app._load_telegram_api_config() == (24680, "legacy-only-in-memory")
    assert json.loads(config.read_text(encoding="utf-8")) == {"api_id": 24680}
    assert "legacy-only-in-memory" not in config.read_text(encoding="utf-8")


def test_tdlib_database_key_is_random_32_bytes():
    import base64
    import bulkuploader.tdlib_native as tdlib

    first = tdlib._new_database_key()
    second = tdlib._new_database_key()
    assert first != second
    assert len(base64.b64decode(first)) == 32
    assert len(base64.b64decode(second)) == 32


def test_application_data_path_ignores_home_and_xdg_environment(tmp_path: Path, monkeypatch):
    import bulkuploader.paths as paths

    trusted = tmp_path / "trusted-home"
    redirected = tmp_path / "redirected-home"
    monkeypatch.setenv("HOME", str(redirected))
    monkeypatch.setenv("XDG_DATA_HOME", str(redirected / "xdg"))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(paths, "trusted_home", lambda: trusted.resolve())

    assert paths.app_data_dir() == trusted.resolve() / ".local" / "share" / "telegram-uploader"


def test_application_data_path_uses_windows_known_folder(tmp_path: Path, monkeypatch):
    import bulkuploader.paths as paths

    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(paths, "_windows_known_folder", lambda csidl: local.resolve())

    assert paths.app_data_dir() == local.resolve() / "telegram-uploader"


def test_tdlib_download_rejects_non_https_or_wrong_host(tmp_path: Path):
    import bulkuploader.tdlib_native as tdlib

    destination = tmp_path / "runtime.deb"
    with pytest.raises(RuntimeError, match="restricted"):
        tdlib._download_checked("http://archive.ubuntu.com/file.deb", "0" * 64, destination)
    with pytest.raises(RuntimeError, match="restricted"):
        tdlib._download_checked("https://example.com/file.deb", "0" * 64, destination)
    assert not destination.exists()
