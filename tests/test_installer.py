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


def test_windows_launcher_content_uses_relative_runtime_path():
    installer = _load_installer()
    content = installer._windows_launcher_content()
    assert "%~dp0" in content
    assert "runtime\\venv\\Scripts\\telegram.exe" in content
    assert ":\\" not in content


def test_installer_main_reuses_existing_valid_venv_on_upgrade(tmp_path, monkeypatch):
    installer = _load_installer()
    root = tmp_path / "data"
    venv_dir = root / "runtime" / "venv"
    python = installer.venv_python(venv_dir)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    created = []

    class FakeBuilder:
        def create(self, target):
            created.append(target)
            raise AssertionError("existing valid venv must be reused")

    def fake_run(args, **kwargs):
        class Result:
            stdout = ""

        if "-c" in args:
            code = args[args.index("-c") + 1]
            if "importlib.metadata" in code:
                Result.stdout = installer._source_package_version(Path(__file__).resolve().parents[1]) + "\n"
            else:
                installed = tmp_path / "installed" / "bulkuploader" / "__init__.py"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text("", encoding="utf-8")
                Result.stdout = f"{installed}\n"
        return Result()

    monkeypatch.setattr(installer, "app_data_root", lambda: root)
    monkeypatch.setattr(installer, "command_home", lambda _root: tmp_path / "bin")
    monkeypatch.setattr(installer.venv, "EnvBuilder", lambda **_kwargs: FakeBuilder())
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer, "_install_launcher", lambda venv_dir, bin_home: False)

    assert installer.main() == 0
    assert created == []


def test_installer_main_uses_venv_module_without_name_collision(tmp_path, monkeypatch, capsys):
    installer = _load_installer()
    root = tmp_path / "data"
    created = []

    class FakeBuilder:
        def create(self, target):
            created.append(target)
            python = installer.venv_python(target)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")

    def fake_run(args, **kwargs):
        class Result:
            stdout = ""

        if "-c" in args:
            code = args[args.index("-c") + 1]
            if "importlib.metadata" in code:
                Result.stdout = installer._source_package_version(Path(__file__).resolve().parents[1]) + "\n"
            else:
                installed = tmp_path / "installed" / "bulkuploader" / "__init__.py"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text("", encoding="utf-8")
                Result.stdout = f"{installed}\n"
        return Result()

    monkeypatch.setattr(installer, "app_data_root", lambda: root)
    monkeypatch.setattr(installer, "command_home", lambda _root: tmp_path / "bin")
    monkeypatch.setattr(installer.venv, "EnvBuilder", lambda **_kwargs: FakeBuilder())
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer, "_install_launcher", lambda venv_dir, bin_home: False)

    assert installer.main() == 0
    assert created == [root / "runtime" / "venv"]
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err
