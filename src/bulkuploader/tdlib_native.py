from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from platformdirs import user_data_dir

APP_NAME = "telegram-uploader"
APP_VERSION = "0.2.0"
DATA_DIR = Path(user_data_dir(APP_NAME, appauthor=False))
TDLIB_ROOT = DATA_DIR / "tdlib"
TDLIB_DB_DIR = DATA_DIR / "tdlib-native-db"
TDLIB_FILES_DIR = DATA_DIR / "tdlib-native-files"

SQLCIPHER_URL = "https://archive.ubuntu.com/ubuntu/pool/universe/s/sqlcipher/libsqlcipher1_4.5.6-1build2_amd64.deb"
SQLCIPHER_SHA256 = "30ffc3589facffbd72fc8720fb5ab7448b4dfb6e99929f0f41b4b3321f45a611"
TDJSON_URL = "https://archive.ubuntu.com/ubuntu/pool/universe/t/td/libtdjson1.8.38_1.8.38~git20241021.d321984+dfsg-4_amd64.deb"
TDJSON_SHA256 = "250f2f51b4fae813ab166a0c12b1845e1ac5752cc66d74e43bdcd3e185e6401b"


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _platform_name() -> str:
    return platform.system().lower()


def _machine_name() -> str:
    return platform.machine().lower()


def _platform_supported() -> bool:
    return _platform_name() in {"linux", "darwin", "windows"}


def _bundled_runtime_supported() -> bool:
    return _platform_name() == "linux" and _machine_name() in {"x86_64", "amd64"}


def _homebrew_prefix() -> Path | None:
    brew = shutil.which("brew")
    if not brew:
        return None
    try:
        result = subprocess.run(
            [brew, "--prefix", "tdlib"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _vcpkg_root() -> Path | None:
    raw = os.environ.get("VCPKG_ROOT", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.exists():
            return p
    executable = shutil.which("vcpkg")
    if executable:
        p = Path(executable).resolve().parent
        if (p / "installed").exists():
            return p
    return None


def _candidate_tdlib_paths(root: Path) -> list[Path]:
    system = _platform_name()
    candidates: list[Path] = []
    if system == "linux":
        candidates.extend(sorted(root.glob("usr/lib/x86_64-linux-gnu/TDLib*/libtdjson.so*")))
        candidates.extend(sorted(root.glob("lib*/libtdjson.so*")))
        brew_prefix = _homebrew_prefix()
        if brew_prefix:
            candidates.extend(sorted((brew_prefix / "lib").glob("libtdjson.so*")))
        candidates.extend(sorted(Path("/usr/lib/x86_64-linux-gnu").glob("TDLib*/libtdjson.so*")))
        candidates.extend(sorted(Path("/usr/lib/aarch64-linux-gnu").glob("TDLib*/libtdjson.so*")))
        candidates.extend(
            Path(p)
            for p in (
                "/usr/lib/libtdjson.so",
                "/usr/local/lib/libtdjson.so",
                "/usr/lib/x86_64-linux-gnu/libtdjson.so",
                "/usr/lib/aarch64-linux-gnu/libtdjson.so",
            )
        )
    elif system == "darwin":
        candidates.extend(sorted(root.glob("lib*/libtdjson*.dylib")))
        brew_prefix = _homebrew_prefix()
        if brew_prefix:
            candidates.extend(sorted((brew_prefix / "lib").glob("libtdjson*.dylib")))
        candidates.extend(
            Path(p)
            for p in (
                "/opt/homebrew/lib/libtdjson.dylib",
                "/usr/local/lib/libtdjson.dylib",
            )
        )
    elif system == "windows":
        candidates.extend(sorted(root.rglob("tdjson.dll")))
        vcpkg_root = _vcpkg_root()
        if vcpkg_root:
            machine = "arm64-windows" if _machine_name() in {"arm64", "aarch64"} else "x64-windows"
            candidates.extend(
                [
                    vcpkg_root / "installed" / machine / "bin" / "tdjson.dll",
                    vcpkg_root / "installed" / machine / "debug" / "bin" / "tdjson.dll",
                ]
            )
        path_hit = shutil.which("tdjson.dll")
        if path_hit:
            candidates.append(Path(path_hit))
    return candidates


def tdlib_library_path(root: Path = TDLIB_ROOT) -> Path | None:
    override = os.environ.get("TDLIB_LIBRARY", "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    return next((p for p in _candidate_tdlib_paths(root) if p.is_file()), None)


def tdlib_sqlcipher_path(root: Path = TDLIB_ROOT) -> Path | None:
    candidates = sorted(root.glob("usr/lib/x86_64-linux-gnu/libsqlcipher.so.1*"))
    return next((p for p in candidates if p.is_file()), None)


def tdlib_setup_hint() -> str:
    system = _platform_name()
    if _bundled_runtime_supported():
        return "Run `telegram native setup` to install the bundled TDLib runtime locally."
    if system == "darwin":
        return "Install Homebrew TDLib with `brew install tdlib`, then run `telegram login` again."
    if system == "windows":
        return "Install TDLib with vcpkg (`vcpkg install tdlib`) or set TDLIB_LIBRARY to tdjson.dll, then run `telegram login` again."
    if system == "linux":
        return "Install system TDLib or Homebrew TDLib, or set TDLIB_LIBRARY to libtdjson.so."
    return "TDLib is optional on this platform; Telethon remains available as the transfer engine."


def tdlib_runtime_status(root: Path = TDLIB_ROOT) -> dict[str, object]:
    lib = tdlib_library_path(root)
    sqlcipher = tdlib_sqlcipher_path(root)
    bundled = False
    if lib:
        try:
            bundled = lib.relative_to(root).parts[:1] == ("usr",)
        except ValueError:
            bundled = False
    # The Ubuntu-local runtime needs its unpacked SQLCipher dependency. System,
    # Homebrew and vcpkg TDLib installations resolve their own dependencies.
    installed = bool(lib and (not bundled or sqlcipher))
    source = None
    system = _platform_name()
    brew_prefix = _homebrew_prefix() if system in {"darwin", "linux"} else None
    vcpkg_root = _vcpkg_root() if system == "windows" else None
    if installed:
        if bundled:
            source = "bundled-linux"
        elif brew_prefix and brew_prefix in lib.parents:
            source = "homebrew"
        elif vcpkg_root and vcpkg_root in lib.parents:
            source = "vcpkg"
        else:
            source = "system"
    return {
        "supported": _platform_supported(),
        "bundled_install_supported": _bundled_runtime_supported(),
        "auto_install_supported": _bundled_runtime_supported() or (_platform_name() == "darwin" and shutil.which("brew") is not None),
        "installed": installed,
        "library": str(lib) if lib else None,
        "sqlcipher": str(sqlcipher) if sqlcipher else None,
        "source": source,
        "setup_hint": tdlib_setup_hint(),
    }


def _download_checked(url: str, expected_sha256: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"tg-uploader/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    actual = _sha256(destination)
    if actual != expected_sha256:
        raise RuntimeError(
            f"TDLib runtime package checksum mismatch for {destination.name}: expected {expected_sha256}, got {actual}"
        )


def install_tdlib_runtime(root: Path = TDLIB_ROOT, force: bool = False) -> dict[str, object]:
    status = tdlib_runtime_status(root)
    if status["installed"] and not force:
        return status

    system = _platform_name()
    if _bundled_runtime_supported():
        if shutil.which("dpkg-deb") is None:
            raise RuntimeError("dpkg-deb is required to unpack the local TDLib runtime on Linux.")
        _secure_dir(root.parent)
        with tempfile.TemporaryDirectory(prefix="tdlib-install-", dir=str(root.parent)) as tmp_name:
            tmp = Path(tmp_name)
            sql_deb = tmp / "libsqlcipher1.deb"
            td_deb = tmp / "libtdjson.deb"
            staged = tmp / "root"
            staged.mkdir()
            _download_checked(SQLCIPHER_URL, SQLCIPHER_SHA256, sql_deb)
            _download_checked(TDJSON_URL, TDJSON_SHA256, td_deb)
            subprocess.run(["dpkg-deb", "-x", str(sql_deb), str(staged)], check=True)
            subprocess.run(["dpkg-deb", "-x", str(td_deb), str(staged)], check=True)
            if not tdlib_library_path(staged) or not tdlib_sqlcipher_path(staged):
                raise RuntimeError("Downloaded TDLib packages did not contain the expected runtime libraries.")
            if root.exists():
                shutil.rmtree(root)
            shutil.move(str(staged), str(root))
            _secure_dir(root)
        return tdlib_runtime_status(root)

    if system in {"darwin", "linux"}:
        brew = shutil.which("brew")
        if brew:
            subprocess.run([brew, "install", "tdlib"], check=True)
            status = tdlib_runtime_status(root)
            if status["installed"]:
                return status
            raise RuntimeError("Homebrew finished, but libtdjson could not be located. Set TDLIB_LIBRARY to the installed libtdjson path.")

    if system == "windows":
        vcpkg = shutil.which("vcpkg")
        if not vcpkg:
            candidate = _vcpkg_root()
            if candidate:
                exe = candidate / "vcpkg.exe"
                if exe.is_file():
                    vcpkg = str(exe)
        if vcpkg:
            triplet = "arm64-windows" if _machine_name() in {"arm64", "aarch64"} else "x64-windows"
            subprocess.run([vcpkg, "install", f"tdlib:{triplet}"], check=True)
            status = tdlib_runtime_status(root)
            if status["installed"]:
                return status
            raise RuntimeError("vcpkg finished, but tdjson.dll could not be located. Set TDLIB_LIBRARY to the installed tdjson.dll path.")

    raise RuntimeError(str(status["setup_hint"]))


class TDJson:
    """Thin ctypes binding to TDLib's current process-wide JSON C interface."""

    # td_receive() is process-global across every TDLib client ID. Keep one
    # process-wide receive lock and route foreign-client events into per-client
    # mailboxes so one wrapper cannot steal another client's authorization/update.
    _receive_lock = threading.Lock()
    _mailbox_lock = threading.Lock()
    _mailboxes: dict[int, deque[dict]] = {}

    def __init__(self, root: Path = TDLIB_ROOT):
        status = tdlib_runtime_status(root)
        if not status["installed"]:
            raise RuntimeError(f"TDLib native runtime is not installed. {status['setup_hint']}")
        library = Path(str(status["library"]))
        self._dependency_handles: list[object] = []
        self._dll_directory = None

        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            self._dll_directory = os.add_dll_directory(str(library.parent))

        sqlcipher_value = status.get("sqlcipher")
        if sqlcipher_value:
            mode = getattr(ctypes, "RTLD_GLOBAL", 0)
            self._dependency_handles.append(ctypes.CDLL(str(sqlcipher_value), mode=mode))

        if os.name == "nt":
            self.lib = ctypes.CDLL(str(library))
        else:
            self.lib = ctypes.CDLL(str(library), mode=getattr(ctypes, "RTLD_GLOBAL", 0))

        self.lib.td_create_client_id.argtypes = []
        self.lib.td_create_client_id.restype = ctypes.c_int
        self.lib.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
        self.lib.td_send.restype = None
        self.lib.td_receive.argtypes = [ctypes.c_double]
        self.lib.td_receive.restype = ctypes.c_char_p
        self.lib.td_execute.argtypes = [ctypes.c_char_p]
        self.lib.td_execute.restype = ctypes.c_char_p
        if hasattr(self.lib, "td_set_log_verbosity_level"):
            self.lib.td_set_log_verbosity_level.argtypes = [ctypes.c_int]
            self.lib.td_set_log_verbosity_level.restype = None
            self.lib.td_set_log_verbosity_level(0)

        self.client_id = int(self.lib.td_create_client_id())
        self._extra = 0
        self._updates: deque[dict] = deque()
        with self._mailbox_lock:
            self._mailboxes.setdefault(self.client_id, deque())

    def send(self, request: dict) -> None:
        self.lib.td_send(self.client_id, json.dumps(request, separators=(",", ":")).encode("utf-8"))

    def _pop_mailbox(self) -> dict | None:
        with self._mailbox_lock:
            mailbox = self._mailboxes.setdefault(self.client_id, deque())
            return mailbox.popleft() if mailbox else None

    @classmethod
    def _route_foreign(cls, obj: dict) -> None:
        client_id = obj.get("@client_id")
        if client_id is None:
            return
        with cls._mailbox_lock:
            cls._mailboxes.setdefault(int(client_id), deque()).append(obj)

    def receive(self, timeout: float = 1.0) -> dict | None:
        queued = self._pop_mailbox()
        if queued is not None:
            return queued

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # TDLib explicitly requires that td_receive isn't called concurrently.
            # Use short bounded waits so another client can acquire the global lock.
            with self._receive_lock:
                raw = self.lib.td_receive(min(remaining, 0.25))
            if not raw:
                queued = self._pop_mailbox()
                if queued is not None:
                    return queued
                continue
            obj = json.loads(raw.decode("utf-8"))
            client_id = obj.get("@client_id")
            if client_id is None or int(client_id) == self.client_id:
                return obj
            self._route_foreign(obj)
            queued = self._pop_mailbox()
            if queued is not None:
                return queued

    def request(self, request: dict, timeout: float = 30.0) -> dict:
        self._extra += 1
        token = f"native-{self.client_id}-{self._extra}"
        payload = dict(request)
        payload["@extra"] = token
        self.send(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.receive(min(1.0, max(0.01, deadline - time.monotonic())))
            if obj is None:
                continue
            if obj.get("@extra") == token:
                if obj.get("@type") == "error":
                    raise RuntimeError(f"TDLib error {obj.get('code')}: {obj.get('message')}")
                return obj
            self._updates.append(obj)
        raise TimeoutError(f"Timed out waiting for TDLib response to {request.get('@type')}")

    def pop_update(self) -> dict | None:
        return self._updates.popleft() if self._updates else None

    def execute(self, request: dict) -> dict | None:
        raw = self.lib.td_execute(json.dumps(request, separators=(",", ":")).encode("utf-8"))
        return json.loads(raw.decode("utf-8")) if raw else None


@dataclass
class NativeBenchmarkResult:
    bytes_uploaded: int
    elapsed_seconds: float
    peak_bytes_per_second: float
    average_bytes_per_second: float
    message_id: int | None


class TDLibNativeClient:
    """Persistent TDLib user client used for the native upload benchmark/transport."""

    def __init__(self, api_id: int, api_hash: str, root: Path = TDLIB_ROOT):
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.root = root
        self.td = TDJson(root)
        self.ready = False
        self._authorization_state = "unknown"
        _secure_dir(TDLIB_DB_DIR)
        _secure_dir(TDLIB_FILES_DIR)

    @property
    def authorization_state(self) -> str:
        return self._authorization_state

    def _set_parameters(self) -> None:
        self.td.request(
            {
                "@type": "setTdlibParameters",
                "use_test_dc": False,
                "database_directory": str(TDLIB_DB_DIR),
                "files_directory": str(TDLIB_FILES_DIR),
                "database_encryption_key": "",
                "use_file_database": True,
                "use_chat_info_database": True,
                "use_message_database": False,
                "use_secret_chats": False,
                "api_id": self.api_id,
                "api_hash": self.api_hash,
                "system_language_code": "en",
                "device_model": "TG Uploader",
                "system_version": platform.platform(),
                "application_version": APP_VERSION,
            },
            timeout=30,
        )

    def _handle_authorization_state(
        self,
        state_type: str,
        interactive: bool,
        phone_provider: Callable[[], str] | None,
        code_provider: Callable[[], str] | None,
        password_provider: Callable[[], str] | None,
    ) -> bool | None:
        self._authorization_state = state_type
        if state_type == "authorizationStateWaitTdlibParameters":
            self._set_parameters()
            return None
        if state_type == "authorizationStateWaitEncryptionKey":
            self.td.request({"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
            return None
        if state_type == "authorizationStateWaitPhoneNumber":
            if not interactive:
                return False
            phone = (phone_provider or (lambda: input("Phone number (with country code): ")))().strip()
            self.td.request({"@type": "setAuthenticationPhoneNumber", "phone_number": phone, "settings": None})
            return None
        if state_type == "authorizationStateWaitCode":
            if not interactive:
                return False
            code = (code_provider or (lambda: input("Telegram login code: ")))().strip()
            self.td.request({"@type": "checkAuthenticationCode", "code": code})
            return None
        if state_type == "authorizationStateWaitPassword":
            if not interactive:
                return False
            password = (password_provider or (lambda: getpass.getpass("Telegram 2-step password: ")))()
            self.td.request({"@type": "checkAuthenticationPassword", "password": password})
            return None
        if state_type == "authorizationStateWaitRegistration":
            raise RuntimeError("This TDLib benchmark supports existing Telegram accounts only, not new-account registration.")
        if state_type in {"authorizationStateWaitEmailAddress", "authorizationStateWaitEmailCode", "authorizationStateWaitPremiumPurchase"}:
            raise RuntimeError(f"TDLib requires an unsupported authorization step: {state_type}")
        if state_type == "authorizationStateReady":
            self.ready = True
            return True
        if state_type in {"authorizationStateClosing", "authorizationStateClosed", "authorizationStateLoggingOut"}:
            return False
        return None

    def authorize(
        self,
        interactive: bool = False,
        phone_provider: Callable[[], str] | None = None,
        code_provider: Callable[[], str] | None = None,
        password_provider: Callable[[], str] | None = None,
        timeout: float = 120.0,
    ) -> bool:
        if self.ready:
            return True

        # TDLib instances don't emit authorization updates until their first
        # request. Start the client explicitly; the version response itself is
        # harmless and is ignored by the auth-state loop below.
        if self._authorization_state == "unknown":
            self.td.send({"@type": "getOption", "name": "version"})

        # A prior non-interactive probe may already have consumed the current
        # wait-state update. Continue that same client instead of creating another.
        if self._authorization_state != "unknown":
            handled = self._handle_authorization_state(
                self._authorization_state,
                interactive,
                phone_provider,
                code_provider,
                password_provider,
            )
            if handled is not None:
                return handled

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.td.pop_update() or self.td.receive(min(1.0, max(0.01, deadline - time.monotonic())))
            if obj is None or obj.get("@type") != "updateAuthorizationState":
                continue
            state = obj.get("authorization_state", {})
            state_type = state.get("@type", "unknown")
            handled = self._handle_authorization_state(
                state_type,
                interactive,
                phone_provider,
                code_provider,
                password_provider,
            )
            if handled is not None:
                return handled
        raise TimeoutError("Timed out waiting for TDLib authorization state.")

    def close(self, timeout: float = 5.0) -> None:
        """Close this TDLib client without logging the Telegram account out."""
        if self._authorization_state == "authorizationStateClosed":
            return
        try:
            self.td.request({"@type": "close"}, timeout=min(timeout, 5.0))
        except Exception:
            # A closing client may race the request response; still drain the
            # authorization-state update below so TDLib can flush its database.
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.td.pop_update() or self.td.receive(min(0.25, max(0.01, deadline - time.monotonic())))
            if not obj or obj.get("@type") != "updateAuthorizationState":
                continue
            state_type = obj.get("authorization_state", {}).get("@type", "unknown")
            self._authorization_state = state_type
            if state_type == "authorizationStateClosed":
                break
        self.ready = False
        with TDJson._mailbox_lock:
            TDJson._mailboxes.pop(self.td.client_id, None)

    def version(self) -> str | None:
        obj = self.td.request({"@type": "getOption", "name": "version"}, timeout=5)
        return str(obj.get("value")) if obj.get("@type") == "optionValueString" else None

    def get_self_chat_id(self) -> int:
        me = self.td.request({"@type": "getMe"})
        user_id = int(me["id"])
        chat = self.td.request({"@type": "createPrivateChat", "user_id": user_id, "force": False})
        return int(chat["id"])

    def search_public_chat(self, username: str) -> int:
        username = username.strip().lstrip("@")
        if not username:
            raise RuntimeError("Telegram username is empty.")
        chat = self.td.request({"@type": "searchPublicChat", "username": username})
        return int(chat["id"])

    def ensure_chat(self, kind: str, raw_id: int) -> int:
        """Make a Telethon-resolved peer available to TDLib and return its chat ID."""
        raw_id = int(raw_id)
        if kind == "user":
            chat = self.td.request({"@type": "createPrivateChat", "user_id": raw_id, "force": False})
        elif kind == "basic_group":
            chat = self.td.request({"@type": "createBasicGroupChat", "basic_group_id": raw_id, "force": False})
        elif kind == "supergroup":
            chat = self.td.request({"@type": "createSupergroupChat", "supergroup_id": raw_id, "force": False})
        else:
            raise RuntimeError(f"Unsupported Telegram peer type for TDLib upload: {kind}")
        return int(chat["id"])

    def send_document(
        self,
        chat_id: int,
        path: Path,
        on_bytes: Callable[[int], None] | None = None,
        timeout: float = 7200.0,
    ) -> str | None:
        """Send one general file through TDLib and report real upload-byte deltas."""
        path = path.expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Telegram file does not exist: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"Telegram cannot upload an empty file: {path.name}")
        if not self.ready:
            raise RuntimeError("TDLib session is not authorized.")

        sent = self.td.request(
            {
                "@type": "sendMessage",
                "chat_id": int(chat_id),
                "message_thread_id": 0,
                "reply_to": None,
                "options": None,
                "reply_markup": None,
                "input_message_content": {
                    "@type": "inputMessageDocument",
                    "document": {"@type": "inputFileLocal", "path": str(path)},
                    "thumbnail": None,
                    "disable_content_type_detection": True,
                    "caption": {"@type": "formattedText", "text": "", "entities": []},
                },
            },
            timeout=30,
        )
        old_message_id = int(sent.get("id", 0))
        content = sent.get("content", {})
        document = content.get("document", {}) if isinstance(content, dict) else {}
        file_obj = document.get("document", {}) if isinstance(document, dict) else {}
        file_id = int(file_obj["id"]) if isinstance(file_obj, dict) and file_obj.get("id") is not None else None

        last_uploaded = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.td.pop_update() or self.td.receive(min(0.5, max(0.01, deadline - time.monotonic())))
            if not obj:
                continue
            typ = obj.get("@type")
            if typ == "updateFile":
                current = obj.get("file", {})
                if file_id is not None and int(current.get("id", -1)) != file_id:
                    continue
                remote = current.get("remote", {})
                uploaded = max(0, int(remote.get("uploaded_size", 0) or 0))
                if uploaded > last_uploaded:
                    delta = min(uploaded, size) - min(last_uploaded, size)
                    last_uploaded = uploaded
                    if delta > 0 and on_bytes:
                        on_bytes(delta)
                continue
            if typ == "updateMessageSendSucceeded":
                old = obj.get("old_message_id")
                if old is None or int(old) == old_message_id:
                    # TDLib may coalesce the final updateFile with message success.
                    # Count only any unreported tail, never more than the file size.
                    if on_bytes and last_uploaded < size:
                        on_bytes(size - last_uploaded)
                    message = obj.get("message", {})
                    remote_id = int(message.get("id", 0))
                    return str(remote_id) if remote_id else None
                continue
            if typ == "updateMessageSendFailed":
                old = obj.get("old_message_id")
                if old is None or int(old) == old_message_id:
                    error = obj.get("error", {})
                    raise RuntimeError(f"TDLib send failed: {error.get('message', 'unknown error')}")
        raise TimeoutError(f"TDLib upload timed out: {path.name}")

    def _wait_for_sent_message(self, old_message_id: int, timeout: float = 120.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.td.pop_update() or self.td.receive(1.0)
            if not obj:
                continue
            typ = obj.get("@type")
            if typ == "updateMessageSendSucceeded":
                old = obj.get("old_message_id")
                message = obj.get("message", {})
                if old is None or int(old) == old_message_id:
                    return int(message.get("id", 0)) or None
            if typ == "updateMessageSendFailed":
                old = obj.get("old_message_id")
                if old is None or int(old) == old_message_id:
                    error = obj.get("error", {})
                    raise RuntimeError(f"TDLib send failed: {error.get('message', 'unknown error')}")
        raise TimeoutError("Timed out waiting for TDLib message upload to finish.")

    def benchmark_saved_messages(
        self,
        path: Path,
        on_progress: Callable[[int, float], None] | None = None,
        delete_after: bool = True,
        timeout: float = 7200.0,
    ) -> NativeBenchmarkResult:
        path = path.expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Benchmark file does not exist: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError("Benchmark file must not be empty.")
        if not self.ready:
            raise RuntimeError("TDLib native session is not authorized. Run `telegram native login` first.")

        chat_id = self.get_self_chat_id()
        started = time.monotonic()
        send_result = self.td.request(
            {
                "@type": "sendMessage",
                "chat_id": chat_id,
                "message_thread_id": 0,
                "reply_to": None,
                "options": None,
                "reply_markup": None,
                "input_message_content": {
                    "@type": "inputMessageDocument",
                    "document": {"@type": "inputFileLocal", "path": str(path)},
                    "thumbnail": None,
                    "disable_content_type_detection": True,
                    "caption": {"@type": "formattedText", "text": "", "entities": []},
                },
            },
            timeout=30,
        )
        old_message_id = int(send_result.get("id", 0))
        file_id = None
        content = send_result.get("content", {})
        document = content.get("document", {})
        if isinstance(document, dict):
            file_obj = document.get("document", {})
            if isinstance(file_obj, dict) and file_obj.get("id") is not None:
                file_id = int(file_obj["id"])

        last_uploaded = 0
        last_time = started
        peak = 0.0
        final_message_id: int | None = None
        deadline = started + timeout
        while time.monotonic() < deadline:
            obj = self.td.pop_update() or self.td.receive(0.5)
            now = time.monotonic()
            if not obj:
                continue
            typ = obj.get("@type")
            if typ == "updateFile":
                file_obj = obj.get("file", {})
                if file_id is not None and int(file_obj.get("id", -1)) != file_id:
                    continue
                remote = file_obj.get("remote", {})
                uploaded = int(remote.get("uploaded_size", 0) or 0)
                if uploaded >= last_uploaded:
                    dt = max(now - last_time, 1e-6)
                    instant = (uploaded - last_uploaded) / dt
                    if uploaded > last_uploaded:
                        peak = max(peak, instant)
                        last_uploaded = uploaded
                        last_time = now
                        if on_progress:
                            on_progress(min(uploaded, size), max(now - started, 1e-6))
            elif typ == "updateMessageSendSucceeded":
                old = obj.get("old_message_id")
                if old is None or int(old) == old_message_id:
                    message = obj.get("message", {})
                    final_message_id = int(message.get("id", 0)) or None
                    break
            elif typ == "updateMessageSendFailed":
                old = obj.get("old_message_id")
                if old is None or int(old) == old_message_id:
                    error = obj.get("error", {})
                    raise RuntimeError(f"TDLib send failed: {error.get('message', 'unknown error')}")
        else:
            raise TimeoutError("TDLib native upload benchmark timed out.")

        elapsed = max(time.monotonic() - started, 1e-6)
        if delete_after and final_message_id:
            try:
                self.td.request(
                    {"@type": "deleteMessages", "chat_id": chat_id, "message_ids": [final_message_id], "revoke": True},
                    timeout=30,
                )
            except Exception:
                pass
        return NativeBenchmarkResult(
            bytes_uploaded=size,
            elapsed_seconds=elapsed,
            peak_bytes_per_second=peak,
            average_bytes_per_second=size / elapsed,
            message_id=final_message_id,
        )
