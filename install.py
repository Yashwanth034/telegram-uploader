#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

APP_DATA_NAME = "telegram-uploader"
PACKAGE_NAME = "telegram-uploader"
PUBLIC_NAME = "TG Uploader"


def app_data_root() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    # Keep the historical application-data directory stable across the public
    # branding/package rename so existing sessions and upload history keep working.
    return base / APP_DATA_NAME


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_telegram(venv: Path) -> Path:
    return venv / ("Scripts/telegram.exe" if os.name == "nt" else "bin/telegram")


def command_home(root: Path) -> Path:
    if os.name == "nt":
        return root / "bin"
    return Path.home() / ".local" / "bin"


def _run(args: list[str | os.PathLike[str]]) -> None:
    subprocess.run([str(x) for x in args], check=True)


def _add_windows_user_path(directory: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment")
        try:
            try:
                current, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, value_type = "", winreg.REG_EXPAND_SZ
            parts = [part for part in str(current).split(";") if part]
            normalized = {os.path.normcase(os.path.normpath(os.path.expandvars(part))) for part in parts}
            target = os.path.normcase(os.path.normpath(str(directory)))
            if target in normalized:
                return False
            updated = ";".join([*parts, str(directory)])
            winreg.SetValueEx(key, "Path", 0, value_type, updated)
        finally:
            winreg.CloseKey(key)

        # Tell Explorer/new terminals that the per-user environment changed.
        with_context = getattr(ctypes, "windll", None)
        if with_context is not None:
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.c_ulong()
            with_context.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(result),
            )
        return True
    except Exception as exc:
        print(f"Warning: could not add {directory} to your user PATH automatically: {exc}", file=sys.stderr)
        return False


def _add_posix_user_path(directory: Path) -> bool:
    if os.name == "nt" or str(directory) in os.environ.get("PATH", "").split(os.pathsep):
        return False

    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "zsh":
        profile = home / ".zshrc"
    elif shell == "bash":
        profile = home / ".bashrc"
    elif shell in {"sh", "dash", "ksh"}:
        profile = home / ".profile"
    else:
        return False

    marker = "# telegram-uploader: user command path"
    export_line = 'export PATH="$HOME/.local/bin:$PATH"'
    existing = profile.read_text(encoding="utf-8", errors="ignore") if profile.exists() else ""
    if marker in existing or export_line in existing:
        return False
    with profile.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"\n{marker}\n{export_line}\n")
    return True


def _install_launcher(venv: Path, bin_home: Path) -> bool:
    bin_home.mkdir(parents=True, exist_ok=True)
    target = venv_telegram(venv)
    if os.name == "nt":
        launcher = bin_home / "telegram.cmd"
        launcher.write_text(
            "@echo off\r\n"
            f'"{target}" %*\r\n',
            encoding="utf-8",
        )
        return _add_windows_user_path(bin_home)

    launcher = bin_home / "telegram"
    if launcher.exists() or launcher.is_symlink():
        launcher.unlink()
    launcher.symlink_to(target)
    return _add_posix_user_path(bin_home)


def main() -> int:
    source_dir = Path(__file__).resolve().parent
    source_package = (source_dir / "src" / "bulkuploader").resolve()
    root = app_data_root()
    runtime = root / "runtime"
    venv = runtime / "venv"
    bin_home = command_home(root)

    root.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            root.chmod(0o700)
        except OSError:
            pass

    print(f"Installing {PUBLIC_NAME} for {sys.platform}...")
    _run([sys.executable, "-m", "venv", venv])
    python = venv_python(venv)
    _run([python, "-m", "pip", "uninstall", "-y", PACKAGE_NAME])
    _run([python, "-m", "pip", "install", source_dir])

    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import pathlib, bulkuploader; print(pathlib.Path(bulkuploader.__file__).resolve())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    installed_from = Path(probe.stdout.strip()).resolve()
    if installed_from.parent == source_package:
        raise RuntimeError("Install verification failed: telegram still points at the source package.")

    path_changed = _install_launcher(venv, bin_home)

    print()
    print("Installed command: telegram")
    print(f"Installed package: {installed_from}")
    print(f"Application data: {root}")
    print("The source/repository folder is no longer required for daily use.")
    if path_changed:
        print("Open a new terminal once so the updated user PATH is visible.")
    elif str(bin_home) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {bin_home} to PATH if your shell does not already include it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
