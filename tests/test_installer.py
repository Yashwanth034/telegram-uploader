import importlib.util
import os
from pathlib import Path

import pytest


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "install.py"
    spec = importlib.util.spec_from_file_location("telegram_uploader_installer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_uses_clean_macos_data_directory(tmp_path, monkeypatch):
    installer = _load_installer()
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer, "_trusted_home", lambda: tmp_path.resolve())
    assert installer.app_data_root() == (
        tmp_path.resolve() / "Library" / "Application Support" / "telegram-uploader"
    )


def test_installer_uses_clean_windows_data_directory(tmp_path, monkeypatch):
    installer = _load_installer()
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "_windows_known_folder", lambda csidl: local.resolve())
    assert installer.app_data_root() == local.resolve() / "telegram-uploader"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell profile behavior")
def test_posix_path_setup_is_idempotent(tmp_path, monkeypatch):
    installer = _load_installer()
    target = tmp_path / ".local" / "bin"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(installer, "_trusted_home", lambda: tmp_path.resolve())
    monkeypatch.setattr(installer, "_trusted_shell_name", lambda: "zsh")

    assert installer._add_posix_user_path(target) is True
    assert installer._add_posix_user_path(target) is False
    profile = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert profile.count("# telegram-uploader: user command path") == 1
    assert 'export PATH="$HOME/.local/bin:$PATH"' in profile


def test_installer_ignores_poisoned_home_and_xdg_environment(tmp_path, monkeypatch):
    installer = _load_installer()
    trusted = tmp_path / "trusted-home"
    poisoned = tmp_path / "redirected-home"
    monkeypatch.setenv("HOME", str(poisoned))
    monkeypatch.setenv("XDG_DATA_HOME", str(poisoned / "xdg"))
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer, "_trusted_home", lambda: trusted.resolve())

    assert installer.app_data_root() == (
        trusted.resolve() / ".local" / "share" / "telegram-uploader"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell profile behavior")
def test_posix_path_setup_refuses_symlinked_profile(tmp_path, monkeypatch):
    installer = _load_installer()
    target = tmp_path / ".local" / "bin"
    real_profile = tmp_path / "real-profile"
    real_profile.write_text("safe\n", encoding="utf-8")
    profile = tmp_path / ".zshrc"
    try:
        profile.symlink_to(real_profile)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(installer, "_trusted_home", lambda: tmp_path.resolve())
    monkeypatch.setattr(installer, "_trusted_shell_name", lambda: "zsh")

    with pytest.raises(RuntimeError, match="symlinked shell profile"):
        installer._add_posix_user_path(target)
    assert real_profile.read_text(encoding="utf-8") == "safe\n"
