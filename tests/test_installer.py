import importlib.util
from pathlib import Path


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
    monkeypatch.setenv("HOME", str(tmp_path))
    assert installer.app_data_root() == tmp_path / "Library" / "Application Support" / "telegram-uploader"


def test_installer_uses_clean_windows_data_directory(tmp_path, monkeypatch):
    installer = _load_installer()
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert installer.app_data_root() == local / "telegram-uploader"


def test_posix_path_setup_is_idempotent(tmp_path, monkeypatch):
    installer = _load_installer()
    target = tmp_path / ".local" / "bin"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert installer._add_posix_user_path(target) is True
    assert installer._add_posix_user_path(target) is False
    profile = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert profile.count("# telegram-uploader: user command path") == 1
    assert 'export PATH="$HOME/.local/bin:$PATH"' in profile
