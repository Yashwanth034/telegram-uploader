from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

APP_DATA_NAME = "telegram-uploader"


def _windows_known_folder(csidl: int) -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows known-folder lookup is only available on Windows.")
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError(f"Windows known-folder lookup failed (CSIDL {csidl}, error {result}).")
    return Path(buffer.value).resolve()


def trusted_home() -> Path:
    if os.name == "nt":
        return _windows_known_folder(0x0028)  # CSIDL_PROFILE
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except KeyError:
        # Some containers/sandboxes intentionally omit the current UID from
        # /etc/passwd. Fall back only for that compatibility case.
        return Path.home().resolve()


def app_data_dir() -> Path:
    """Return a stable per-user data directory without environment redirection."""
    if sys.platform == "win32":
        base = _windows_known_folder(0x001C)  # CSIDL_LOCAL_APPDATA
    elif sys.platform == "darwin":
        base = trusted_home() / "Library" / "Application Support"
    else:
        base = trusted_home() / ".local" / "share"
    return base / APP_DATA_NAME
