#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import venv
from pathlib import Path

APP_DATA_NAME = "telegram-uploader"
PACKAGE_NAME = "telegram-uploader"
PUBLIC_NAME = "TG Uploader"


def _windows_launcher_content() -> str:
    return '@echo off\r\n"%~dp0..\\runtime\\venv\\Scripts\\telegram.exe" %*\r\n'


def _windows_known_folder(csidl: int) -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows known-folder lookup is only available on Windows.")
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError(f"Windows known-folder lookup failed (CSIDL {csidl}, error {result}).")
    return Path(buffer.value).resolve()


def _trusted_home() -> Path:
    if os.name == "nt":
        return _windows_known_folder(0x0028)  # CSIDL_PROFILE
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def app_data_root() -> Path:
    if sys.platform == "win32":
        base = _windows_known_folder(0x001C)  # CSIDL_LOCAL_APPDATA
    else:
        safe_home = _trusted_home()
        if sys.platform == "darwin":
            base = safe_home / "Library" / "Application Support"
        else:
            # Deliberately ignore HOME/XDG_DATA_HOME environment overrides in the
            # installer. Local environment poisoning must not redirect writes.
            base = safe_home / ".local" / "share"
    return base / APP_DATA_NAME


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_telegram(venv: Path) -> Path:
    return venv / ("Scripts/telegram.exe" if os.name == "nt" else "bin/telegram")


def command_home(root: Path) -> Path:
    if os.name == "nt":
        return root / "bin"
    return _trusted_home() / ".local" / "bin"


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
    except Exception:
        # Do not echo profile-derived absolute paths or exception text. The
        # installer can continue safely; the user only needs to know PATH was
        # not updated automatically.
        print("Warning: could not update your user PATH automatically.", file=sys.stderr)
        return False


def _trusted_shell_name() -> str:
    if os.name == "nt":
        return ""
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_shell or "").name


def _add_posix_user_path(directory: Path) -> bool:
    if os.name == "nt" or str(directory) in os.environ.get("PATH", "").split(os.pathsep):
        return False

    shell = _trusted_shell_name()
    home = _trusted_home()
    if shell == "zsh":
        profile = home / ".zshrc"
    elif shell == "bash":
        profile = home / ".bashrc"
    elif shell in {"sh", "dash", "ksh"}:
        profile = home / ".profile"
    else:
        return False

    if profile.is_symlink():
        raise RuntimeError("Refusing to modify a symlinked shell profile.")

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


def _source_package_version(source_dir: Path) -> str:
    pyproject = source_dir / "pyproject.toml"
    in_project = False
    for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if in_project and line.startswith("["):
            break
        if in_project:
            key, separator, value = line.partition("=")
            if separator and key.strip() == "version":
                version = value.strip().strip('"').strip("'")
                if version:
                    return version
    raise RuntimeError("Could not read the package version from pyproject.toml.")


def _install_launcher(venv: Path, bin_home: Path) -> bool:
    if bin_home.is_symlink():
        raise RuntimeError("Refusing to install into a symlinked command directory.")
    bin_home.mkdir(parents=True, exist_ok=True)
    target = venv_telegram(venv)
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("Installed telegram launcher target is not a trusted regular file.")
    if os.name == "nt":
        launcher = bin_home / "telegram.cmd"
        if launcher.is_symlink():
            raise RuntimeError("Refusing to overwrite a symlinked launcher.")
        # Keep the launcher relocatable and avoid persisting a profile-derived
        # absolute path in clear text. bin/ is a sibling of runtime/.
        launcher.write_text(_windows_launcher_content(), encoding="utf-8")
        return _add_windows_user_path(bin_home)

    launcher = bin_home / "telegram"
    if launcher.exists() or launcher.is_symlink():
        launcher.unlink()
    launcher.symlink_to(target)
    return _add_posix_user_path(bin_home)


def main() -> int:
    source_dir = Path(__file__).resolve().parent
    source_package = (source_dir / "src" / "bulkuploader").resolve()
    expected_version = _source_package_version(source_dir)
    root = app_data_root()
    runtime = root / "runtime"
    venv_dir = runtime / "venv"
    bin_home = command_home(root)

    for candidate, label in ((root, "application-data directory"), (runtime, "runtime directory"), (venv_dir, "virtual environment")):
        if candidate.is_symlink():
            raise RuntimeError(f"Refusing to use a symlinked {label}.")

    root.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            root.chmod(0o700)
        except OSError:
            pass

    print(f"Installing {PUBLIC_NAME} for {sys.platform}...")
    python = venv_python(venv_dir)
    # Upgrades must reuse a healthy existing runtime. Re-running EnvBuilder.create()
    # over a POSIX venv whose python executable is already a symlink to the system
    # interpreter can raise shutil.SameFileError before pip gets a chance to upgrade
    # the package. The runtime venv contains application code/dependencies only;
    # user configuration, Telegram sessions, and job history live outside runtime/.
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = venv_python(venv_dir)
    if not python.is_file():
        raise RuntimeError("Virtual-environment Python is missing or invalid.")

    # Use argv lists with shell=False and an installer-created Python executable.
    # Upgrade pip first so fresh environments don't retain vulnerable ensurepip builds.
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip>=26.2.1,<27"], check=True, shell=False)
    subprocess.run([str(python), "-m", "pip", "uninstall", "-y", PACKAGE_NAME], check=True, shell=False)
    subprocess.run([str(python), "-m", "pip", "install", str(source_dir)], check=True, shell=False)

    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import pathlib, bulkuploader; print(pathlib.Path(bulkuploader.__file__).resolve())",
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    installed_from = Path(probe.stdout.strip()).resolve()
    if installed_from.parent == source_package:
        raise RuntimeError("Install verification failed: telegram still points at the source package.")

    version_probe = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; print(m.version('telegram-uploader'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    installed_version = version_probe.stdout.strip()
    if installed_version != expected_version:
        raise RuntimeError("Install verification failed: installed package version does not match the source package version.")

    path_changed = _install_launcher(venv_dir, bin_home)

    print()
    print("Installed command: telegram")
    print(f"Installed version: {installed_version}")
    print("Installed package verified.")
    print("Application data directory prepared.")
    print("The source/repository folder is no longer required for daily use.")
    if path_changed:
        print("Open a new terminal once so the updated user PATH is visible.")
    elif str(bin_home) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add the TG Uploader user command directory to PATH if your shell does not already include it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
