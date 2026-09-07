import importlib.util
from pathlib import Path


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "install.py"
    spec = importlib.util.spec_from_file_location("telegram_uploader_installer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_uses_clean_macos_data_directory(tmp_path):
    installer = _load_installer()
    assert installer.app_data_root(platform_name="darwin", home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "telegram-uploader"
    )


def test_installer_uses_clean_windows_data_directory(tmp_path, monkeypatch):
    installer = _load_installer()
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert installer.app_data_root(platform_name="win32", home=tmp_path) == local / "telegram-uploader"


def test_posix_path_setup_is_idempotent(tmp_path, monkeypatch):
    installer = _load_installer()
    target = tmp_path / ".local" / "bin"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert installer._add_posix_user_path(target, host_os_name="posix", home=tmp_path) is True
    assert installer._add_posix_user_path(target, host_os_name="posix", home=tmp_path) is False
    profile = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert profile.count("# telegram-uploader: user command path") == 1
    assert 'export PATH="$HOME/.local/bin:$PATH"' in profile
