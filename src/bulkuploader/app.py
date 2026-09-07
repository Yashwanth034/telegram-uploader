from __future__ import annotations

import asyncio
import getpass
import hashlib
import contextlib
import inspect
import importlib.metadata
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

try:
    from blake3 import blake3
except ImportError:
    def blake3():
        return hashlib.blake2b(digest_size=32)

from bulkuploader.paths import app_data_dir, trusted_home
from rich.console import Console
from rich.live import Live
from rich.table import Table

APP_NAME = "telegram-uploader"
DATA_DIR = app_data_dir()
DB_PATH = DATA_DIR / "state.sqlite3"
TELEGRAM_CONFIG_PATH = DATA_DIR / "telegram-api.json"
TELEGRAM_SESSION_BASE = DATA_DIR / "telegram-mtproto"
TELEGRAM_API_HASH_KEY_PREFIX = "telegram-api-hash"
TELEGRAM_PART_SIZE = 512 * 1024
TELEGRAM_BIG_FILE_THRESHOLD = 10 * 1024 * 1024
TELEGRAM_MIN_PART_WORKERS = 8
TELEGRAM_MAX_PART_WORKERS = 32
TELEGRAM_DEFAULT_PART_WORKERS = 16
TELEGRAM_SINGLE_MIN_PART_WORKERS = 2
TELEGRAM_SINGLE_MAX_PART_WORKERS = 10
TELEGRAM_SINGLE_DEFAULT_PART_WORKERS = 5
TELEGRAM_DOWNLOAD_FILE_WINDOW = 4
TELEGRAM_DEFAULT_FILE_WINDOW = 8
TELEGRAM_DEFAULT_TRANSFER_CONNECTIONS = 8
TELEGRAM_MAX_TRANSFER_CONNECTIONS = 8
TRANSIENT_FILE_SUFFIXES = (".part", ".crdownload", ".download", ".partial", ".aria2", ".!qb")
console = Console()


def package_version() -> str:
    try:
        return importlib.metadata.version(APP_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "development"


def _is_transient_file(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(suffix) for suffix in TRANSIENT_FILE_SUFFIXES)


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


INSTANCE_LOCK_PATH = DATA_DIR / "telegram.lock"


class TelegramInstanceLock:
    """Prevent two CLI processes from sharing Telethon/TDLib session databases."""

    def __init__(self, path: Path = INSTANCE_LOCK_PATH):
        self.path = path
        self._fh = None
        self._lock_kind: str | None = None

    def _busy(self, exc: BaseException) -> RuntimeError:
        holder = ""
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.seek(0)
                holder = self._fh.read().strip()
        detail = f" (PID {holder})" if holder.isdigit() else ""
        return RuntimeError(
            f"Another `telegram` command is already running{detail}. "
            "Wait for it to finish or close that other terminal before starting a second one."
        )

    def __enter__(self):
        _secure_dir(self.path.parent)
        self._fh = self.path.open("a+", encoding="utf-8")
        _secure_file(self.path)

        if os.name == "nt":
            import msvcrt

            # Windows byte-range locking needs at least one byte in the file.
            self._fh.seek(0, os.SEEK_END)
            if self._fh.tell() == 0:
                self._fh.write("0")
                self._fh.flush()
            self._fh.seek(0)
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                busy = self._busy(exc)
                self._fh.close()
                self._fh = None
                raise busy from exc
            self._lock_kind = "msvcrt"
        else:
            try:
                import fcntl
            except ImportError:
                # Unknown platforms still get the application's SQLite safety checks;
                # supported Unix-like systems provide fcntl and Windows uses msvcrt.
                self._lock_kind = None
            else:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    busy = self._busy(exc)
                    self._fh.close()
                    self._fh = None
                    raise busy from exc
                self._lock_kind = "fcntl"

        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is None:
            return False
        try:
            if self._lock_kind == "msvcrt":
                import msvcrt

                with contextlib.suppress(OSError):
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._lock_kind == "fcntl":
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            self._lock_kind = None
        return False


_secure_dir(DATA_DIR)


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int
    digest: str


@dataclass(frozen=True)
class Destination:
    platform: str
    key: str
    title: str


class StateDB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        _secure_file(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS fingerprints (
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    path_hint TEXT NOT NULL,
                    PRIMARY KEY(device, inode, size, mtime_ns)
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    platform TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    remote_id TEXT,
                    uploaded_at INTEGER NOT NULL,
                    PRIMARY KEY(platform, destination_key, digest)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    destination_title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS job_files (
                    job_id INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    PRIMARY KEY(job_id, digest),
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    platform TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    media_key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    downloaded_at INTEGER NOT NULL,
                    PRIMARY KEY(platform, source_key, message_id)
                );
                CREATE INDEX IF NOT EXISTS downloads_media_idx
                    ON downloads(platform, source_key, media_key);
                CREATE INDEX IF NOT EXISTS downloads_digest_idx
                    ON downloads(platform, source_key, digest);
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    output_ref TEXT NOT NULL,
                    media_filter TEXT NOT NULL DEFAULT 'all',
                    status TEXT NOT NULL DEFAULT 'active'
                );
                """
            )
            download_job_columns = {
                str(row[1]) for row in self.conn.execute("PRAGMA table_info(download_jobs)")
            }
            if "media_filter" not in download_job_columns:
                self.conn.execute(
                    "ALTER TABLE download_jobs ADD COLUMN media_filter TEXT NOT NULL DEFAULT 'all'"
                )

    def cached_digest(self, stat: os.stat_result) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT digest FROM fingerprints WHERE device=? AND inode=? AND size=? AND mtime_ns=?",
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns),
            ).fetchone()
            return row[0] if row else None

    def remember_digest(self, path: Path, stat: os.stat_result, digest: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO fingerprints(device,inode,size,mtime_ns,digest,path_hint) VALUES(?,?,?,?,?,?)",
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest, str(path)),
            )

    def uploaded(self, platform: str, destination_key: str, digest: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM uploads WHERE platform=? AND destination_key=? AND digest=?",
                (platform, destination_key, digest),
            ).fetchone()
            return row is not None

    def mark_uploaded(self, platform: str, destination_key: str, rec: FileRecord, remote_id: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO uploads(platform,destination_key,digest,size,remote_id,uploaded_at) VALUES(?,?,?,?,?,?)",
                (platform, destination_key, rec.digest, rec.size, remote_id, int(time.time())),
            )

    def create_job(self, destination: Destination, records: Sequence[FileRecord]) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs(created_at,platform,destination_key,destination_title,status) VALUES(?,?,?,?, 'active')",
                (int(time.time()), destination.platform, destination.key, destination.title),
            )
            jid = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT OR IGNORE INTO job_files(job_id,digest,path,size,status) VALUES(?,?,?,?, 'pending')",
                [(jid, r.digest, str(r.path), r.size) for r in records],
            )
            return jid

    def set_job_file(self, job_id: int, digest: str, status: str, error: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE job_files SET status=?, error=? WHERE job_id=? AND digest=?",
                (status, error, job_id, digest),
            )

    def finish_job(self, job_id: int) -> None:
        with self._lock, self.conn:
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM job_files WHERE job_id=? AND status NOT IN ('done','duplicate','ignored')",
                (job_id,),
            ).fetchone()[0]
            status = "done" if pending == 0 else "interrupted"
            self.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))

    def unfinished_jobs(self, platform: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM jobs WHERE status IN ('active','interrupted')"
        params: list[object] = []
        if platform:
            q += " AND platform=?"
            params.append(platform)
        q += " ORDER BY id DESC"
        with self._lock:
            return list(self.conn.execute(q, params))

    def unresolved_job_rows(self, job_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT path,digest,size,status,error FROM job_files "
                "WHERE job_id=? AND status NOT IN ('done','duplicate','ignored') ORDER BY rowid",
                (job_id,),
            ))

    def pending_job_records(self, job_id: int) -> list[FileRecord]:
        rows = self.unresolved_job_rows(job_id)
        out: list[FileRecord] = []
        for row in rows:
            path = Path(row["path"])
            try:
                st = path.stat()
            except OSError:
                # Keep the unresolved DB row intact. resume() will report the exact
                # missing/unreadable path instead of silently pretending the job has
                # zero remaining files.
                continue
            out.append(FileRecord(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, row["digest"]))
        return out

    def downloaded_message(self, platform: str, source_key: str, message_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT media_key,digest,path,size,downloaded_at FROM downloads WHERE platform=? AND source_key=? AND message_id=?",
                (platform, source_key, int(message_id)),
            ).fetchone()

    def downloaded_media(self, platform: str, source_key: str, media_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT message_id,digest,path,size,downloaded_at FROM downloads WHERE platform=? AND source_key=? AND media_key=?",
                (platform, source_key, media_key),
            ).fetchone()

    def downloaded_digest(self, platform: str, source_key: str, digest: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT message_id,media_key,path,size,downloaded_at FROM downloads WHERE platform=? AND source_key=? AND digest=? LIMIT 1",
                (platform, source_key, digest),
            ).fetchone()

    def mark_downloaded(
        self,
        platform: str,
        source_key: str,
        message_id: int,
        media_key: str,
        digest: str,
        path: Path,
        size: int,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO downloads(platform,source_key,message_id,media_key,digest,path,size,downloaded_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    platform,
                    source_key,
                    int(message_id),
                    media_key,
                    digest,
                    str(path),
                    int(size),
                    int(time.time()),
                ),
            )

    def create_download_job(
        self,
        source_key: str,
        source_title: str,
        source_input: str,
        output_ref: str,
        media_filter: str,
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO download_jobs(created_at,platform,source_key,source_title,source_input,output_ref,media_filter,status) "
                "VALUES(?,?,?,?,?,?,?,'active')",
                (
                    int(time.time()),
                    "telegram",
                    source_key,
                    source_title,
                    source_input,
                    output_ref,
                    media_filter,
                ),
            )
            return int(cur.lastrowid)

    def finish_download_job(self, job_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE download_jobs SET status='done' WHERE id=?", (int(job_id),))

    def unfinished_download_jobs(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM download_jobs WHERE status='active' ORDER BY id DESC"
            ))


def hash_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = blake3()
    with path.open("rb", buffering=0) as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan_paths(inputs: Iterable[Path], db: StateDB) -> list[FileRecord]:
    seen_paths: set[Path] = set()
    files: list[Path] = []
    for raw in inputs:
        p = raw.expanduser()
        # Check the user-supplied path before resolving it. resolve() follows
        # symlinks, which would otherwise turn a symlink into its target and defeat
        # the safety rule below, potentially uploading an unintended target file.
        if p.is_symlink():
            console.print(f"[dim]Skipping symbolic link:[/] {p}")
            continue
        p = p.resolve()
        if not p.exists():
            console.print(f"[yellow]Skipping missing path:[/] {p}")
            continue
        if p.is_file():
            if _is_transient_file(p):
                console.print(f"[dim]Skipping temporary download file:[/] {p.name}")
                continue
            if p not in seen_paths:
                seen_paths.add(p)
                files.append(p)
            continue
        for root, dirs, names in os.walk(p, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in names:
                f = Path(root) / name
                try:
                    if f.is_file() and not f.is_symlink() and not _is_transient_file(f):
                        rf = f.resolve()
                        if rf not in seen_paths:
                            seen_paths.add(rf)
                            files.append(rf)
                except OSError:
                    pass

    records: list[FileRecord] = []
    for idx, path in enumerate(files, 1):
        try:
            st = path.stat()
            digest = db.cached_digest(st)
            if digest is None:
                digest = hash_file(path)
                after = path.stat()
                if (
                    after.st_dev != st.st_dev
                    or after.st_ino != st.st_ino
                    or after.st_size != st.st_size
                    or after.st_mtime_ns != st.st_mtime_ns
                ):
                    console.print(f"[dim]Skipping file still changing:[/] {path.name}")
                    continue
                db.remember_digest(path, st, digest)
            records.append(FileRecord(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, digest))
        except (OSError, PermissionError) as exc:
            console.print(f"[yellow]Skipping unreadable file:[/] {path} ({exc})")
        if idx % 100 == 0 or idx == len(files):
            print(f"\rScanning {idx:,}/{len(files):,}", end="", flush=True)
    if files:
        print()
    return records


def _trusted_gui_picker(name: str) -> str | None:
    if name not in {"zenity", "kdialog"} or os.name == "nt":
        return None
    candidate = Path("/usr/bin") / name
    return str(candidate) if candidate.is_file() else None


def choose_native_paths() -> list[Path]:
    mode = console.input("Select [F]iles or a [D]irectory? [F/d]: ").strip().lower()
    want_dir = mode == "d"
    zenity = _trusted_gui_picker("zenity")
    if zenity:
        cmd = [zenity, "--file-selection"]
        if want_dir:
            cmd.append("--directory")
        else:
            cmd += ["--multiple", "--separator=\n"]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
        if result.returncode == 0:
            return [Path(x) for x in result.stdout.splitlines() if x.strip()]
    kdialog = _trusted_gui_picker("kdialog")
    if kdialog:
        cmd = [kdialog, "--getexistingdirectory", "."] if want_dir else [kdialog, "--getopenfilename", ".", "*", "--multiple"]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
        if result.returncode == 0:
            return [Path(x) for x in result.stdout.splitlines() if x.strip()]
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw()
        picked = filedialog.askdirectory() if want_dir else filedialog.askopenfilenames()
        root.destroy()
        if want_dir:
            return [Path(picked)] if picked else []
        return [Path(x) for x in picked]
    except Exception:
        return []


def parse_path_input(line: str, windows: bool | None = None) -> list[Path]:
    import shlex

    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        return [Path(x) for x in shlex.split(line)]

    # POSIX shlex treats Windows backslashes as escapes. In Windows mode preserve
    # them and only remove a matching outer quote pair added by drag/drop or shells.
    values = shlex.split(line, posix=False)
    cleaned: list[Path] = []
    for value in values:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        cleaned.append(Path(value))
    return cleaned


def _telegram_api_hash_key(api_id: int) -> str:
    return f"{TELEGRAM_API_HASH_KEY_PREFIX}:{int(api_id)}"


def _read_saved_telegram_api_id() -> int | None:
    env_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    if env_id:
        try:
            value = int(env_id)
        except ValueError:
            return None
        return value if value > 0 else None
    if not TELEGRAM_CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(TELEGRAM_CONFIG_PATH.read_text(encoding="utf-8"))
        value = int(data["api_id"])
        return value if value > 0 else None
    except Exception:
        return None


def _write_telegram_api_id(api_id: int) -> None:
    _secure_dir(TELEGRAM_CONFIG_PATH.parent)
    tmp = TELEGRAM_CONFIG_PATH.with_name(TELEGRAM_CONFIG_PATH.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"api_id": int(api_id)}, fh)
            fh.write("\n")
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, TELEGRAM_CONFIG_PATH)
    _secure_file(TELEGRAM_CONFIG_PATH)


def _load_telegram_api_config() -> tuple[int, str] | None:
    from bulkuploader.secure_store import get_secret, set_secret

    api_id = _read_saved_telegram_api_id()
    if api_id is None:
        return None

    # Migrate old v1.0.x config files that stored the Telegram API hash beside
    # the API ID. The legacy value is used only for this process, moved into an
    # OS-backed credential store when available, and removed from disk either way.
    try:
        data = json.loads(TELEGRAM_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    legacy_hash = str(data.get("api_hash", "")).strip()
    if legacy_hash:
        set_secret(_telegram_api_hash_key(api_id), legacy_hash)
        _write_telegram_api_id(api_id)
        return api_id, legacy_hash

    stored_hash = get_secret(_telegram_api_hash_key(api_id))
    return (api_id, stored_hash) if stored_hash else None


def _save_telegram_api_config(api_id: int, api_hash: str) -> bool:
    from bulkuploader.secure_store import set_secret

    _write_telegram_api_id(api_id)
    return set_secret(_telegram_api_hash_key(api_id), api_hash)


def _configure_telegram_api_interactive() -> tuple[int, str]:
    existing = _load_telegram_api_config()
    if existing:
        return existing

    console.print("Telegram MTProto setup (one time)")
    console.print("API ID is saved locally. API hash is stored only in a secure OS credential store when available.")
    saved_id = _read_saved_telegram_api_id()
    if saved_id is not None:
        api_id = saved_id
        console.print("[green]✓[/] API ID already saved")
    else:
        while True:
            raw_id = console.input("API ID: ").strip()
            if raw_id.isdigit() and int(raw_id) > 0:
                api_id = int(raw_id)
                break
            console.print("[yellow]API ID must be a positive number.[/]")

    while True:
        api_hash = getpass.getpass("API hash: ").strip()
        if api_hash:
            break
        console.print("[yellow]API hash cannot be empty.[/]")

    persisted = _save_telegram_api_config(api_id, api_hash)
    if not persisted:
        console.print(
            "[yellow]Secure OS credential storage is unavailable, so the API hash was not saved to disk.[/]"
        )
        console.print(
            "[dim]This run can continue, but a future run will ask for the API hash again.[/]"
        )
    return api_id, api_hash


def _normalize_public_channel_input(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise RuntimeError("Enter a public Telegram channel username or t.me link.")
    raw = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(?:t\.me|telegram\.me)/", "", raw, flags=re.IGNORECASE)
    username = raw.split("/", 1)[0].lstrip("@").strip()
    if username.startswith("+") or username.lower() == "joinchat":
        raise RuntimeError("Private/invite-only Telegram links are not supported. Use a public channel username or link.")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        raise RuntimeError("Invalid public Telegram channel username or link.")
    return username


class TelegramDirect:
    """Browserless Telegram user client using MTProto via Telethon."""

    name = "telegram"

    def __init__(self):
        self.client = None
        self._entities: dict[str, object] = {}
        self._part_workers = self._read_int_setting(
            "TELEGRAM_UPLOAD_WORKERS",
            TELEGRAM_DEFAULT_PART_WORKERS,
            TELEGRAM_MIN_PART_WORKERS,
            TELEGRAM_MAX_PART_WORKERS,
        )
        self._single_part_workers = self._read_int_setting(
            "TELEGRAM_SINGLE_CONNECTION_WORKERS",
            TELEGRAM_SINGLE_DEFAULT_PART_WORKERS,
            TELEGRAM_SINGLE_MIN_PART_WORKERS,
            TELEGRAM_SINGLE_MAX_PART_WORKERS,
        )
        self._file_window = self._read_int_setting(
            "TELEGRAM_FILE_WINDOW", TELEGRAM_DEFAULT_FILE_WINDOW, 1, 12
        )
        self._transfer_connections = self._read_int_setting(
            "TELEGRAM_TRANSFER_CONNECTIONS",
            TELEGRAM_DEFAULT_TRANSFER_CONNECTIONS,
            1,
            TELEGRAM_MAX_TRANSFER_CONNECTIONS,
        )
        self._transfer_senders: list[object] = []
        self._owned_transfer_senders: list[object] = []
        self._transfer_locks: list[asyncio.Lock] = []
        self._transfer_pool_attempted = False
        self._transfer_kind = "auto"
        self._tmp_sessions = 1
        self._transfer_limit = 1
        self._transfer_target = min(4, self._transfer_connections)
        self._transfer_round_robin = 0
        self._last_batch_speed: float | None = None
        self._flood_waits = 0
        self._premium_waits = 0
        self._last_wait_seconds = 0
        self.native_client = None
        self._native_engine = False
        self._native_version: str | None = None
        self._native_error: str | None = None

    @staticmethod
    def _read_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    @property
    def part_workers(self) -> int:
        return self._part_workers

    @property
    def file_window(self) -> int:
        return self._file_window

    @property
    def transfer_connections(self) -> int:
        return self._transfer_connections

    @property
    def transfer_mode(self) -> str:
        if self._transfer_kind == "media":
            return f"media-pool:{len(self._transfer_senders)}/{self._transfer_limit}"
        if self._transfer_kind == "tmp-main":
            return f"tmp-session-pool:{len(self._transfer_senders)}/{self._transfer_limit}"
        if self._transfer_kind == "main-single":
            return "main-single"
        if self._transfer_pool_attempted:
            return "main-single"
        return "auto"

    @property
    def transfer_limit(self) -> int:
        return self._transfer_limit

    @property
    def tmp_sessions(self) -> int:
        return self._tmp_sessions

    @property
    def worker_bounds(self) -> tuple[int, int]:
        if self._transfer_kind == "main-single":
            return TELEGRAM_SINGLE_MIN_PART_WORKERS, TELEGRAM_SINGLE_MAX_PART_WORKERS
        return TELEGRAM_MIN_PART_WORKERS, TELEGRAM_MAX_PART_WORKERS

    def _set_single_connection_mode(self) -> None:
        if self._transfer_kind != "main-single":
            self._part_workers = self._single_part_workers
        self._transfer_kind = "main-single"
        self._transfer_limit = 1

    @property
    def engine_name(self) -> str:
        if self._native_engine:
            return f"TDLib {self._native_version or ''}".strip()
        return "Telethon"

    def _connect_native_transport(self, cfg: tuple[int, str]) -> bool:
        self._native_engine = False
        self._native_error = None
        self._native_version = None
        try:
            from bulkuploader.tdlib_native import TDLibNativeClient, tdlib_runtime_status

            status = tdlib_runtime_status()
            if not status["installed"]:
                self._native_error = "TDLib runtime is not installed"
                return False
            native = TDLibNativeClient(*cfg)
            try:
                if not native.authorize(interactive=False, timeout=15):
                    self._native_error = "TDLib session is not authorized"
                    native.close()
                    return False
                self._native_version = native.version()
                self.native_client = native
                self._native_engine = True
                return True
            except Exception:
                native.close()
                raise
        except Exception as exc:
            self._native_error = str(exc)
            self.native_client = None
            self._native_engine = False
            return False

    def connect(self, require_login: bool = True):
        cfg = _load_telegram_api_config()
        if not cfg:
            raise RuntimeError(
                "Telegram MTProto is not configured yet. Run `telegram login` once and enter your Telegram API ID/hash locally."
            )
        api_id, api_hash = cfg
        from telethon.sync import TelegramClient

        # Telethon's sync facade needs an event loop even though this CLI calls it
        # synchronously. Python 3.12 no longer creates one implicitly; create and set
        # it explicitly without relying on the deprecated get_event_loop fallback.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        self.client = TelegramClient(str(TELEGRAM_SESSION_BASE), api_id, api_hash)
        try:
            self.client.connect()
            session_file = Path(str(TELEGRAM_SESSION_BASE) + ".session")
            if session_file.exists():
                _secure_file(session_file)
            if require_login and not self.client.is_user_authorized():
                raise RuntimeError("Telegram session is not authorized. Run `telegram login` once.")
            # TDLib is the preferred data-plane when its separately authorized native
            # session is available. Telethon remains connected for destination/control
            # operations and is the fallback transport chosen before any native send.
            self._connect_native_transport(cfg)
            return self
        except Exception as exc:
            # Telethon may already have started its send/receive tasks before a
            # SQLite/session failure is raised. Always disconnect that partial client
            # so the event loop is not left with "Task was destroyed" warnings.
            client, self.client = self.client, None
            if client is not None:
                with contextlib.suppress(Exception):
                    client.disconnect()
            if isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower():
                raise RuntimeError(
                    "Telegram session database is busy. Another Telegram process is probably using the same saved session. "
                    "Close/wait for the other `telegram` command, then retry. Your login/session data is not corrupted."
                ) from exc
            raise

    async def _ensure_transfer_pool(self) -> list[object]:
        """Open only Telegram-supported parallel transfer sessions.

        Media-DC sessions may be parallelized freely. On a normal/main DC, Telegram
        permits extra sessions only when help.getConfig advertises tmp_sessions > 1.
        Otherwise uploads stay on the already-authorized main connection and only
        pipeline requests within that single session.
        """
        self._transfer_pool_attempted = True
        if self.client is None:
            self._set_single_connection_mode()
            return self._transfer_senders

        try:
            from telethon import functions
            from telethon.network.mtprotosender import MTProtoSender

            config = await self.client(functions.help.GetConfigRequest())
            self._tmp_sessions = max(1, int(getattr(config, "tmp_sessions", None) or 1))
            dc_id = int(self.client.session.dc_id)
            auth_key = self.client.session.auth_key
            if auth_key is None:
                self._set_single_connection_mode()
                return self._transfer_senders

            # Prefer Telegram's media-only endpoint for the current DC. These
            # dedicated file-transfer sessions are explicitly exempt from the main
            # session duplication restriction.
            media_options = [
                dc for dc in config.dc_options
                if dc.id == dc_id
                and getattr(dc, "media_only", False)
                and not getattr(dc, "cdn", False)
            ]
            if media_options:
                preferred = [
                    dc for dc in media_options
                    if bool(getattr(dc, "ipv6", False)) == bool(self.client._use_ipv6)
                ]
                dc = (preferred or media_options)[0]
                self._transfer_kind = "media"
                self._transfer_limit = self._transfer_connections
                desired = min(self._transfer_target, self._transfer_limit)

                while len(self._transfer_senders) < desired:
                    sender = MTProtoSender(auth_key, loggers=self.client._log, updates_queue=None)
                    await sender.connect(self.client._connection(
                        dc.ip_address,
                        dc.port,
                        dc.id,
                        loggers=self.client._log,
                        proxy=self.client._proxy,
                        local_addr=self.client._local_addr,
                    ))
                    sender.dc_id = dc.id
                    self._transfer_senders.append(sender)
                    self._owned_transfer_senders.append(sender)
                    self._transfer_locks.append(asyncio.Lock())
                return self._transfer_senders

            # No media endpoint: never invent extra main sessions. Only use them if
            # Telegram explicitly allows parallel main sessions through tmp_sessions.
            self._transfer_limit = min(self._transfer_connections, self._tmp_sessions)
            if self._transfer_limit <= 1:
                self._set_single_connection_mode()
                return self._transfer_senders

            self._transfer_kind = "tmp-main"
            if not self._transfer_senders:
                self._transfer_senders.append(self.client._sender)
                self._transfer_locks.append(asyncio.Lock())

            dc = await self.client._get_dc(dc_id)
            desired = min(self._transfer_target, self._transfer_limit)
            while len(self._transfer_senders) < desired:
                sender = MTProtoSender(auth_key, loggers=self.client._log, updates_queue=None)
                await sender.connect(self.client._connection(
                    dc.ip_address,
                    dc.port,
                    dc.id,
                    loggers=self.client._log,
                    proxy=self.client._proxy,
                    local_addr=self.client._local_addr,
                ))
                sender.dc_id = dc.id
                self._transfer_senders.append(sender)
                self._owned_transfer_senders.append(sender)
                self._transfer_locks.append(asyncio.Lock())
        except Exception:
            if not self._transfer_senders:
                self._set_single_connection_mode()
        return self._transfer_senders

    async def _close_transfer_pool(self) -> None:
        owned, self._owned_transfer_senders = self._owned_transfer_senders, []
        self._transfer_senders = []
        self._transfer_locks = []
        if owned:
            await asyncio.gather(*(sender.disconnect() for sender in owned), return_exceptions=True)

    def close(self):
        if self.native_client is not None:
            try:
                self.native_client.close()
            finally:
                self.native_client = None
                self._native_engine = False
        if self.client:
            try:
                loop = self.client.loop
                if self._transfer_senders and loop and not loop.is_running():
                    loop.run_until_complete(self._close_transfer_pool())
            finally:
                self.client.disconnect()

    def is_logged_in(self) -> bool:
        return bool(self.client and self.client.is_user_authorized())

    def login_interactive(self, phone: str | None = None) -> str | None:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        if self.client.is_user_authorized():
            return phone
        console.print("Authorizing Telegram control session...")
        console.print("No browser will be opened. Codes and passwords stay in this terminal.")
        phone = (phone or console.input("Phone number (with country code): ")).strip()
        if not phone:
            raise RuntimeError("Phone number cannot be empty.")
        self.client.start(
            phone=phone,
            code_callback=lambda: console.input("Telegram login code (control session): ").strip(),
            password=lambda: getpass.getpass("Telegram 2-step password (if enabled): "),
        )
        session_file = Path(str(TELEGRAM_SESSION_BASE) + ".session")
        if session_file.exists():
            _secure_file(session_file)
        return phone

    def _load_dialogs(self) -> tuple[list[Destination], list[Destination], list[Destination]]:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        from telethon import utils
        from telethon.tl.types import Channel, Chat, User

        channels: list[Destination] = []
        groups: list[Destination] = []
        chats: list[Destination] = []
        self._entities.clear()
        for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            title = (dialog.name or "").strip()
            if not title:
                continue
            peer_id = utils.get_peer_id(entity)
            key = f"peer:{peer_id}"
            self._entities[key] = entity
            dest = Destination(self.name, key, title)
            if isinstance(entity, Channel):
                if getattr(entity, "broadcast", False):
                    channels.append(dest)
                else:
                    groups.append(dest)
            elif isinstance(entity, Chat):
                groups.append(dest)
            elif isinstance(entity, User):
                chats.append(dest)
        channels.sort(key=lambda d: d.title.casefold())
        groups.sort(key=lambda d: d.title.casefold())
        chats.sort(key=lambda d: d.title.casefold())
        return channels, groups, chats

    def destinations(self) -> list[Destination]:
        channels, groups, chats = self._load_dialogs()
        return channels + groups + chats

    def grouped_destinations(self) -> tuple[list[Destination], list[Destination], list[Destination]]:
        return self._load_dialogs()

    def resolve_username(self, username: str) -> Destination:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        username = username.strip()
        if not username:
            raise RuntimeError("Enter a Telegram username such as @exampleuser.")
        if not username.startswith("@"):
            username = "@" + username
        try:
            entity = self.client.get_entity(username)
        except Exception as exc:
            raise RuntimeError(f"Telegram username was not found or is not accessible: {username}") from exc

        from telethon import utils

        peer_id = utils.get_peer_id(entity)
        key = f"peer:{peer_id}"
        title = utils.get_display_name(entity).strip() or username
        self._entities[key] = entity
        return Destination(self.name, key, title)

    def resolve_public_channel(self, value: str) -> Destination:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        username = _normalize_public_channel_input(value)
        try:
            entity = self.client.get_entity("@" + username)
        except Exception as exc:
            raise RuntimeError(f"Public Telegram channel was not found or is not accessible: @{username}") from exc

        from telethon import utils
        from telethon.tl.types import Channel

        if not isinstance(entity, Channel) or not getattr(entity, "broadcast", False) or not getattr(entity, "username", None):
            raise RuntimeError("Only public Telegram broadcast channels are supported for downloads.")
        peer_id = utils.get_peer_id(entity)
        key = f"peer:{peer_id}"
        title = utils.get_display_name(entity).strip() or ("@" + username)
        self._entities[key] = entity
        return Destination(self.name, key, title)

    def saved_messages_destination(self) -> Destination:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        from telethon import utils

        entity = self.client.get_me()
        if entity is None:
            raise RuntimeError("Telegram Saved Messages could not be resolved for this account.")
        peer_id = utils.get_peer_id(entity)
        key = f"peer:{peer_id}"
        self._entities[key] = entity
        return Destination(self.name, key, "Saved Messages")

    def _entity_for(self, destination: Destination):
        entity = self._entities.get(destination.key)
        if entity is not None:
            return entity

        # Stable peer IDs are stored in jobs/duplicate history. Telethon normally
        # retains the access-hash entity in its session, so resume can recover even
        # a direct @username target that is not present in the dialog list.
        if destination.key.startswith("peer:") and self.client is not None:
            try:
                peer_id = int(destination.key[5:])
                entity = self.client.get_entity(peer_id)
                self._entities[destination.key] = entity
                return entity
            except Exception:
                pass

        self._load_dialogs()
        entity = self._entities.get(destination.key)
        if entity is None:
            raise RuntimeError(f"Telegram destination no longer exists or is no longer accessible: {destination.title}")
        return entity

    def _native_chat_for_entity(self, entity) -> int:
        if self.native_client is None:
            raise RuntimeError("TDLib native transport is not connected.")
        from telethon.tl.types import Channel, Chat, User

        if isinstance(entity, User):
            username = getattr(entity, "username", None)
            if username:
                try:
                    return self.native_client.search_public_chat(str(username))
                except Exception:
                    pass
            return self.native_client.ensure_chat("user", int(entity.id))
        if isinstance(entity, Chat):
            return self.native_client.ensure_chat("basic_group", int(entity.id))
        if isinstance(entity, Channel):
            username = getattr(entity, "username", None)
            if username:
                try:
                    return self.native_client.search_public_chat(str(username))
                except Exception:
                    pass
            return self.native_client.ensure_chat("supergroup", int(entity.id))
        raise RuntimeError(f"Unsupported Telegram destination type for TDLib: {type(entity).__name__}")

    def _upload_batch_native(self, entity, paths: Sequence[Path], on_bytes=None):
        if self.native_client is None:
            raise RuntimeError("TDLib native transport is not connected.")
        # Resolve the TDLib chat before the first send. If this fails, no message has
        # started and the caller may safely choose the Telethon fallback.
        chat_id = self._native_chat_for_entity(entity)
        results: list[str | Exception | None] = []
        for path in paths:
            try:
                results.append(self.native_client.send_document(chat_id, path, on_bytes=on_bytes))
            except Exception as exc:
                # Never silently resend a started native file through Telethon; that
                # could create a duplicate message if Telegram accepted the first send.
                results.append(exc)
        return results

    async def _send_part(self, request, size: int, semaphore: asyncio.Semaphore, senders: Sequence[object] | None = None, on_bytes=None) -> None:
        from telethon.errors import FloodPremiumWaitError, FloodWaitError

        async with semaphore:
            sender = None
            sender_lock = None
            if senders:
                sender_index = self._transfer_round_robin % len(senders)
                self._transfer_round_robin += 1
                sender = senders[sender_index]
                while len(self._transfer_locks) < len(senders):
                    self._transfer_locks.append(asyncio.Lock())
                sender_lock = self._transfer_locks[sender_index]

            for attempt in range(3):
                try:
                    if sender is not None:
                        # FastTelethon/mautrix-style file queues: one in-flight part
                        # per TCP sender. client._call preserves Telethon's RPC/flood
                        # handling while the independent senders provide real network
                        # parallelism.
                        async with sender_lock:
                            if hasattr(self.client, "_call"):
                                result = await self.client._call(sender, request)
                            else:
                                result = await sender.send(request)
                    else:
                        result = await self.client(request)
                    if not result:
                        raise RuntimeError("Telegram rejected an upload file part.")
                    if on_bytes:
                        on_bytes(size)
                    return
                except FloodPremiumWaitError as exc:
                    self._premium_waits += 1
                    self._last_wait_seconds = max(self._last_wait_seconds, int(exc.seconds))
                    floor, _ = self.worker_bounds
                    self._part_workers = max(floor, self._part_workers - 1)
                    if exc.seconds > 30 or attempt == 2:
                        raise
                    await asyncio.sleep(max(1, exc.seconds))
                except FloodWaitError as exc:
                    self._flood_waits += 1
                    self._last_wait_seconds = max(self._last_wait_seconds, int(exc.seconds))
                    floor, _ = self.worker_bounds
                    self._part_workers = max(floor, self._part_workers - 1)
                    if exc.seconds > 30 or attempt == 2:
                        raise
                    await asyncio.sleep(max(1, exc.seconds))
                except (OSError, TimeoutError, ConnectionError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.25 * (2 ** attempt))

    async def _upload_input_async(self, path: Path, semaphore: asyncio.Semaphore, senders: Sequence[object] | None = None, on_bytes=None, per_file_parallelism: int | None = None):
        from telethon import utils
        from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
        from telethon.tl.types import InputFile, InputFileBig

        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"Telegram cannot upload an empty file: {path.name}")

        # Existing single-connection uploaders (telegram-upload and the 2026
        # Telethon concurrent uploader derived from it) use Telethon's adaptive
        # part sizing. Keep 512 KiB for real multi-session transfer pools, but use
        # the proven adaptive size on the server-limited single main connection.
        if self._transfer_kind == "main-single":
            part_size = int(utils.get_appropriated_part_size(size)) * 1024
        else:
            part_size = TELEGRAM_PART_SIZE
        part_count = math.ceil(size / part_size)
        file_id = secrets.randbits(63)
        is_big = size > TELEGRAM_BIG_FILE_THRESHOLD
        # Telegram's small-file MTProto API requires an MD5 checksum as a
        # protocol field; it is not used here for security decisions.
        md5 = hashlib.md5(usedforsecurity=False) if not is_big else None
        part_index = 0

        # Read only a bounded window into memory at a time. Each file may prepare a
        # window concurrently, while the shared semaphore caps total MTProto RPCs.
        with path.open("rb", buffering=0) as fh:
            while part_index < part_count:
                tasks = []
                part_window = per_file_parallelism or self._part_workers
                for _ in range(part_window):
                    chunk = fh.read(part_size)
                    if not chunk:
                        break
                    if md5 is not None:
                        md5.update(chunk)
                    if is_big:
                        request = SaveBigFilePartRequest(file_id, part_index, part_count, chunk)
                    else:
                        request = SaveFilePartRequest(file_id, part_index, chunk)
                    tasks.append(asyncio.create_task(self._send_part(request, len(chunk), semaphore, senders, on_bytes)))
                    part_index += 1
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            raise result

        if is_big:
            return InputFileBig(file_id, part_count, path.name)
        return InputFile(file_id, part_count, path.name, md5.hexdigest())

    def _adapt_upload_workers(self, transferred: int, elapsed: float, had_errors: bool) -> None:
        if elapsed <= 0 or transferred < 4 * 1024 * 1024:
            return
        speed = transferred / elapsed
        pool_size = len(self._transfer_senders)
        floor, ceiling = self.worker_bounds
        step = 1 if self._transfer_kind == "main-single" else max(2, pool_size or 2)
        improving = self._last_batch_speed is None or speed >= self._last_batch_speed * 0.97
        if had_errors:
            self._part_workers = max(floor, self._part_workers - step)
        elif improving:
            self._part_workers = min(ceiling, self._part_workers + step)
            if pool_size and self._transfer_target < self._transfer_limit:
                self._transfer_target += 1
        elif speed < self._last_batch_speed * 0.85:
            self._part_workers = max(floor, self._part_workers - step)
        self._last_batch_speed = speed

    async def _upload_batch_async(self, entity, paths: Sequence[Path], on_bytes=None):
        senders = await self._ensure_transfer_pool()
        semaphore = asyncio.Semaphore(self._part_workers)
        transferred = 0

        def progress(amount: int) -> None:
            nonlocal transferred
            transferred += amount
            if on_bytes:
                on_bytes(amount)

        started = time.monotonic()
        per_file_parallelism = max(2, min(16, math.ceil(self._part_workers / max(1, len(paths)))))
        prepared = await asyncio.gather(
            *(
                self._upload_input_async(
                    path,
                    semaphore,
                    senders,
                    progress,
                    per_file_parallelism,
                )
                for path in paths
            ),
            return_exceptions=True,
        )

        results: list[str | Exception | None] = []
        had_errors = False
        # Parts are prepared concurrently for speed, but messages are finalized in
        # the original file order so a large batch does not arrive scrambled.
        for uploaded in prepared:
            if isinstance(uploaded, Exception):
                had_errors = True
                results.append(uploaded)
                continue
            try:
                message = await self.client.send_file(entity, uploaded, force_document=True)
                results.append(str(getattr(message, "id", "")) or None)
            except Exception as exc:
                had_errors = True
                results.append(exc)

        self._adapt_upload_workers(transferred, time.monotonic() - started, had_errors)
        return results

    def upload_batch(self, destination: Destination, paths: Sequence[Path], on_bytes=None):
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        if not paths:
            return []
        entity = self._entity_for(destination)

        if self._native_engine and self.native_client is not None:
            try:
                return self._upload_batch_native(entity, paths, on_bytes)
            except Exception as exc:
                # Chat resolution happens before any native send, so this fallback is
                # safe. Once _upload_batch_native starts sending, per-file errors are
                # returned in the result list and are never silently resent here.
                self._native_error = str(exc)
                self._native_engine = False

        loop = self.client.loop
        task = loop.create_task(self._upload_batch_async(entity, paths, on_bytes))
        try:
            return loop.run_until_complete(task)
        except KeyboardInterrupt:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            raise

    def upload(self, destination: Destination, path: Path) -> str | None:
        result = self.upload_batch(destination, [path])[0]
        if isinstance(result, Exception):
            raise result
        return result

    def send_text(self, destination: Destination, text: str) -> str | None:
        if self.client is None:
            raise RuntimeError("Telegram client is not connected.")
        if not text.strip():
            raise RuntimeError("Text message cannot be empty.")
        entity = self._entity_for(destination)
        # Preserve the user's text literally. Important notes often contain
        # underscores, asterisks, URLs, code, etc. and should not be reinterpreted
        # as Telethon Markdown formatting.
        message = self.client.send_message(entity, text, parse_mode=None)
        return str(getattr(message, "id", "")) or None


def search_destinations(items: Sequence[Destination], query: str) -> list[Destination]:
    q = query.strip().casefold()
    if not q:
        return list(items)
    return [item for item in items if q in item.title.casefold()]


def _choose_searchable_section(label: str, items: Sequence[Destination]) -> Destination | None:
    query = ""
    while True:
        matches = search_destinations(items, query)
        visible = matches[:25]
        console.print(f"\n[bold]{label}[/] — {len(matches):,} match{'es' if len(matches) != 1 else ''}")
        if visible:
            for i, destination in enumerate(visible, 1):
                console.print(f"{i}. {destination.title}")
            if len(matches) > len(visible):
                console.print(f"[dim]Showing first {len(visible)}. Type a narrower search.[/]")
        else:
            console.print("[yellow]No matches.[/]")

        raw = console.input("Choose number, type search, or B to go back: ").strip()
        if raw.lower() == "b":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(visible):
            return visible[int(raw) - 1]
        query = raw


def choose_telegram_destination(adapter: TelegramDirect) -> Destination:
    channels, groups, chats = adapter.grouped_destinations()

    sections = (
        ("1", "Channels", channels),
        ("2", "Groups", groups),
        ("3", "Chats", chats),
    )
    while True:
        console.print("\n[bold]Telegram destinations[/]")
        for number, label, items in sections:
            console.print(f"{number}. {label} ({len(items):,})")
        console.print("4. Username (@name)")
        console.print("5. Saved Messages")
        raw = console.input("Category [1/2/3/4/5] or @username: ").strip()
        lowered = raw.lower()

        if lowered in {"5", "s", "saved", "saved messages", "me"}:
            try:
                destination = adapter.saved_messages_destination()
            except RuntimeError as exc:
                console.print(f"[yellow]{exc}[/]")
                continue
            console.print("Found: [bold]Saved Messages[/]")
            return destination

        if raw.startswith("@") or lowered in {"4", "u", "user", "username"}:
            username = raw if raw.startswith("@") else console.input("Username: ").strip()
            try:
                destination = adapter.resolve_username(username)
            except RuntimeError as exc:
                console.print(f"[yellow]{exc}[/]")
                continue
            console.print(f"Found: [bold]{destination.title}[/]")
            return destination

        aliases = {"1": 0, "c": 0, "channel": 0, "channels": 0,
                   "2": 1, "g": 1, "group": 1, "groups": 1,
                   "3": 2, "p": 2, "chat": 2, "chats": 2}
        index = aliases.get(lowered)
        if index is None:
            continue
        _, label, items = sections[index]
        if not items:
            console.print(f"[yellow]No {label.lower()} found.[/]")
            continue
        selected = _choose_searchable_section(label, items)
        if selected is not None:
            return selected


def choose_send_mode() -> str:
    while True:
        console.print("\n[bold]Send[/]")
        console.print("1. Files / Folders")
        console.print("2. Text")
        raw = console.input("Choose [1/2]: ").strip().lower()
        if raw in {"", "1", "f", "file", "files", "folder", "folders"}:
            return "files"
        if raw in {"2", "t", "text"}:
            return "text"


@dataclass
class EditableText:
    lines: list[str]
    row: int = 0
    col: int = 0

    @classmethod
    def from_text(cls, text: str = "") -> "EditableText":
        lines = text.split("\n") if text else [""]
        return cls(lines=lines, row=len(lines) - 1, col=len(lines[-1]))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def insert(self, value: str) -> None:
        line = self.lines[self.row]
        self.lines[self.row] = line[: self.col] + value + line[self.col :]
        self.col += len(value)

    def newline(self) -> None:
        line = self.lines[self.row]
        before, after = line[: self.col], line[self.col :]
        self.lines[self.row] = before
        self.lines.insert(self.row + 1, after)
        self.row += 1
        self.col = 0

    def backspace(self) -> None:
        if self.col > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col - 1] + line[self.col :]
            self.col -= 1
            return
        if self.row > 0:
            previous = self.lines[self.row - 1]
            current = self.lines.pop(self.row)
            self.row -= 1
            self.col = len(previous)
            self.lines[self.row] = previous + current

    def delete(self) -> None:
        line = self.lines[self.row]
        if self.col < len(line):
            self.lines[self.row] = line[: self.col] + line[self.col + 1 :]
            return
        if self.row < len(self.lines) - 1:
            self.lines[self.row] = line + self.lines.pop(self.row + 1)

    def move_left(self) -> None:
        if self.col > 0:
            self.col -= 1
        elif self.row > 0:
            self.row -= 1
            self.col = len(self.lines[self.row])

    def move_right(self) -> None:
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0

    def move_up(self) -> None:
        if self.row > 0:
            self.row -= 1
            self.col = min(self.col, len(self.lines[self.row]))

    def move_down(self) -> None:
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = min(self.col, len(self.lines[self.row]))

    def home(self) -> None:
        self.col = 0

    def end(self) -> None:
        self.col = len(self.lines[self.row])

    def clear(self) -> None:
        self.lines = [""]
        self.row = 0
        self.col = 0


def _terminal_text_editor(destination_title: str, initial: str = "") -> str | None:
    import curses
    import unicodedata

    def cell_width(value: str) -> int:
        width = 0
        for char in value:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        return width

    def visible_slice(value: str, start: int, max_cells: int) -> str:
        out: list[str] = []
        used = 0
        for char in value[start:]:
            char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
            if out and used + char_width > max_cells:
                break
            if not out and char_width > max_cells:
                break
            out.append(char)
            used += char_width
        return "".join(out)

    def run(stdscr):
        buffer = EditableText.from_text(initial)
        stdscr.keypad(True)
        curses.noecho()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        top = 0
        notice = ""

        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()

            if height < 7 or width < 32:
                message = "Terminal too small. Resize it to edit text."
                try:
                    stdscr.addnstr(0, 0, message, max(1, width - 1))
                except curses.error:
                    pass
                stdscr.refresh()
                key = stdscr.get_wch()
                if key == "\x1b":
                    return None
                continue

            header = f"Text → {destination_title}"
            help_line = "Enter:new line  Arrows:move  Backspace/Delete:edit  Ctrl+D/F2:send  Ctrl+U:clear  Esc:cancel"
            try:
                stdscr.addnstr(0, 0, header, width - 1, curses.A_BOLD)
                stdscr.addnstr(1, 0, help_line, width - 1)
                stdscr.hline(2, 0, curses.ACS_HLINE, max(1, width - 1))
            except curses.error:
                pass

            content_top = 3
            content_height = max(1, height - 5)
            if buffer.row < top:
                top = buffer.row
            elif buffer.row >= top + content_height:
                top = buffer.row - content_height + 1

            digits = max(2, len(str(len(buffer.lines))))
            prefix_width = digits + 3
            text_width = max(1, width - prefix_width - 1)

            for screen_index in range(content_height):
                line_index = top + screen_index
                if line_index >= len(buffer.lines):
                    break
                line = buffer.lines[line_index]
                start = 0
                if line_index == buffer.row:
                    while start < buffer.col and cell_width(line[start: buffer.col]) >= text_width:
                        start += 1
                shown = visible_slice(line, start, text_width)
                prefix = f"{line_index + 1:>{digits}} │ "
                if start > 0:
                    prefix = prefix[:-2] + "‹ "
                try:
                    stdscr.addnstr(content_top + screen_index, 0, prefix, prefix_width)
                    stdscr.addnstr(content_top + screen_index, prefix_width, shown, text_width)
                except curses.error:
                    pass

            status = f"Line {buffer.row + 1}/{len(buffer.lines)}  Col {buffer.col + 1}  Chars {len(buffer.text)}"
            if notice:
                status += f"  • {notice}"
            try:
                stdscr.hline(height - 2, 0, curses.ACS_HLINE, max(1, width - 1))
                stdscr.addnstr(height - 1, 0, status, width - 1)
            except curses.error:
                pass

            current_line = buffer.lines[buffer.row]
            horizontal_start = 0
            while horizontal_start < buffer.col and cell_width(current_line[horizontal_start: buffer.col]) >= text_width:
                horizontal_start += 1
            cursor_y = content_top + (buffer.row - top)
            cursor_x = prefix_width + cell_width(current_line[horizontal_start: buffer.col])
            cursor_x = max(prefix_width, min(width - 2, cursor_x))
            try:
                stdscr.move(cursor_y, cursor_x)
            except curses.error:
                pass
            stdscr.refresh()
            notice = ""

            key = stdscr.get_wch()
            if key == "\x1b":
                return None
            if key == "\x04" or key == curses.KEY_F2:
                if buffer.text.strip():
                    return buffer.text
                notice = "Type or paste some text first"
                continue
            if key == "\x15":
                buffer.clear()
                notice = "Cleared"
                continue
            if key in {"\n", "\r"} or key == curses.KEY_ENTER:
                buffer.newline()
                continue
            if key in {"\b", "\x7f"} or key == curses.KEY_BACKSPACE:
                buffer.backspace()
                continue
            if key == curses.KEY_DC:
                buffer.delete()
                continue
            if key == curses.KEY_LEFT:
                buffer.move_left()
                continue
            if key == curses.KEY_RIGHT:
                buffer.move_right()
                continue
            if key == curses.KEY_UP:
                buffer.move_up()
                continue
            if key == curses.KEY_DOWN:
                buffer.move_down()
                continue
            if key == curses.KEY_HOME:
                buffer.home()
                continue
            if key == curses.KEY_END:
                buffer.end()
                continue
            if key == curses.KEY_RESIZE:
                continue
            if key == "\t":
                buffer.insert("    ")
                continue
            if isinstance(key, str) and key.isprintable():
                buffer.insert(key)

    return curses.wrapper(run)


def ask_text(destination_title: str = "Telegram") -> str:
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb":
        try:
            text = _terminal_text_editor(destination_title)
        except Exception as exc:
            console.print(f"[dim]Full text editor unavailable ({exc}); using single-line input.[/]")
        else:
            if text is None:
                raise KeyboardInterrupt
            if not text.strip():
                raise RuntimeError("Text message cannot be empty.")
            return text

    console.print("\nEnter text")
    text = console.input("> ")
    if not text.strip():
        raise RuntimeError("Text message cannot be empty.")
    return text


def ask_files(db: StateDB) -> list[FileRecord]:
    console.print("\nDrop files/folders here, paste paths, or press [bold]S[/] to select files")
    line = console.input("> ").strip()
    paths = choose_native_paths() if line.lower() == "s" else parse_path_input(line)
    if not paths:
        raise RuntimeError("No files or folders selected.")
    return scan_paths(paths, db)


class TelegramRateLimitError(RuntimeError):
    def __init__(self, retry_after: int, resume_command: str = "telegram resume"):
        self.retry_after = max(1, int(retry_after))
        self.resume_command = resume_command
        minutes, seconds = divmod(self.retry_after, 60)
        human = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        super().__init__(
            f"Telegram rate limit is active. Wait about {human}, then run `{resume_command}`. "
            "The unfinished work has been preserved and no additional requests will be sent during the cooldown."
        )


def _telegram_retry_after_seconds(exc: BaseException) -> int | None:
    text = str(exc)
    match = re.search(r"retry\s+after\s+(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_download_component(value: str, fallback: str = "media") -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(value)).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned[:160]


def _download_message_filename(message) -> str:
    message_id = int(getattr(message, "id", 0) or 0)
    file_info = getattr(message, "file", None)
    name = getattr(file_info, "name", None) if file_info is not None else None
    ext = str(getattr(file_info, "ext", "") or "") if file_info is not None else ""
    if name:
        base = _safe_download_component(str(name), fallback=f"media{ext}")
    else:
        base = _safe_download_component(f"media{ext}", fallback="media")
    return f"{message_id}_{base}"


def _available_download_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not choose a non-conflicting download filename.")


def _download_media_key(message) -> str:
    document = getattr(message, "document", None)
    if document is None:
        document = getattr(getattr(message, "media", None), "document", None)
    document_id = getattr(document, "id", None)
    if document_id is not None:
        return f"document:{int(document_id)}"

    photo = getattr(message, "photo", None)
    if photo is None:
        photo = getattr(getattr(message, "media", None), "photo", None)
    photo_id = getattr(photo, "id", None)
    if photo_id is not None:
        return f"photo:{int(photo_id)}"

    file_info = getattr(message, "file", None)
    file_id = getattr(file_info, "id", None) if file_info is not None else None
    if file_id is not None:
        return f"file:{int(file_id)}"
    return f"message:{int(getattr(message, 'id', 0) or 0)}"


DOWNLOAD_FILTER_LABELS = {
    "all": "All media/files",
    "videos": "Videos",
    "images": "Images",
    "documents": "Documents",
    "audio": "Audio",
}


@dataclass
class DownloadCandidate:
    message: object
    message_id: int
    media_key: str
    category: str
    size: int
    aliases: list[int] = field(default_factory=list)


def _download_media_category(message) -> str:
    if getattr(message, "photo", None) is not None:
        return "images"
    file_info = getattr(message, "file", None)
    mime_type = str(getattr(file_info, "mime_type", "") or "").lower()
    if mime_type.startswith("video/"):
        return "videos"
    if mime_type.startswith("image/"):
        return "images"
    if mime_type.startswith("audio/"):
        return "audio"
    return "documents"


def _scan_download_candidates(adapter: TelegramDirect, entity) -> tuple[int, list[DownloadCandidate]]:
    message_count = 0
    unique: dict[str, DownloadCandidate] = {}
    for message in adapter.client.iter_messages(entity, reverse=True):
        message_count += 1
        file_info = getattr(message, "file", None)
        if getattr(message, "media", None) is None or file_info is None:
            continue
        message_id = int(message.id)
        media_key = _download_media_key(message)
        size = max(0, int(getattr(file_info, "size", 0) or 0))
        existing = unique.get(media_key)
        if existing is not None:
            existing.aliases.append(message_id)
            continue
        unique[media_key] = DownloadCandidate(
            message=message,
            message_id=message_id,
            media_key=media_key,
            category=_download_media_category(message),
            size=size,
        )
    return message_count, list(unique.values())


def _choose_download_filter(source_title: str, message_count: int, candidates: list[DownloadCandidate]) -> str | None:
    total_media = sum(1 + len(item.aliases) for item in candidates)
    total_size = sum(item.size * (1 + len(item.aliases)) for item in candidates)
    console.print(f"\nChannel: {source_title}")
    console.print(f"Messages found: {message_count:,}")
    console.print(f"Media files: {total_media:,}")
    console.print(f"Total size: {human_bytes(total_size)}\n")
    console.print("Download")
    choices = ["all", "videos", "images", "documents", "audio"]
    for index, key in enumerate(choices, 1):
        count = sum((1 + len(item.aliases)) for item in candidates if key == "all" or item.category == key)
        console.print(f"{index}. {DOWNLOAD_FILTER_LABELS[key]} ({count:,})")
    console.print("B. Back")
    while True:
        raw = console.input("Choose [1/2/3/4/5/B]: ").strip().lower()
        if raw == "b":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        console.print("Choose 1, 2, 3, 4, 5, or B.")


@dataclass
class DownloadCounters:
    scanned: int = 0
    media: int = 0
    total_files: int = 0
    total_bytes: int = 0
    downloaded: int = 0
    duplicates: int = 0
    failed: int = 0
    bytes_received: int = 0
    bytes_done: int = 0
    active: int = 0
    current: str = ""
    last_error: str = ""
    cancelled: bool = False
    started: float = 0.0


def render_download_status(source_title: str, c: DownloadCounters) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2))
    elapsed = max(time.monotonic() - c.started, 0.001)
    remaining = max(0, c.total_files - c.downloaded - c.duplicates)
    table.add_row("Channel", source_title)
    table.add_row("Files", f"{c.total_files:,}")
    table.add_row("Downloaded", f"{c.downloaded:,}")
    table.add_row("Remaining", f"{remaining:,}")
    table.add_row("Duplicates", f"{c.duplicates:,}")
    if c.failed:
        table.add_row("Failed", f"{c.failed:,}")
    table.add_row("Speed", human_bytes(c.bytes_received / elapsed) + "/s")
    table.add_row("Size", f"{human_bytes(c.bytes_done)} / {human_bytes(c.total_bytes)}")
    if c.active:
        table.add_row("Active", f"{c.active:,}")
    if c.current:
        table.add_row("Current", c.current[:80])
    if c.last_error:
        message = c.last_error.replace("\n", " ").strip()
        if len(message) > 120:
            message = message[:117] + "..."
        table.add_row("Last error", message)
    return table


def _default_download_dir(source_title: str) -> Path:
    return trusted_home() / "Downloads" / "TG Uploader" / _safe_download_component(source_title, "Telegram Channel")


def _prepare_download_dir(path: Path) -> Path:
    raw = str(path)
    if raw == "~":
        path = trusted_home()
    elif raw.startswith("~/") or raw.startswith("~\\"):
        path = trusted_home() / raw[2:]
    elif not path.is_absolute():
        path = trusted_home() / path
    target = path.resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise RuntimeError("Download destination is not a directory.")
    _secure_dir(target)
    return target


def _download_public_channel_run(
    source_input: str,
    output_dir: Path | None = None,
    *,
    output_ref: str | None = None,
    media_filter: str | None = "all",
    db: StateDB | None = None,
    job_id: int | None = None,
    display: bool = True,
) -> DownloadCounters:
    db = db or StateDB()
    adapter = TelegramDirect().connect()
    completed_scan = False
    live_ctx: Live | None = None
    c = DownloadCounters(started=time.monotonic())
    try:
        source = adapter.resolve_public_channel(source_input)
        entity = adapter._entity_for(source)
        message_count, candidates = _scan_download_candidates(adapter, entity)
        c.scanned = message_count

        if media_filter is None:
            media_filter = _choose_download_filter(source.title, message_count, candidates)
            if media_filter is None:
                c.cancelled = True
                return c
        if media_filter not in DOWNLOAD_FILTER_LABELS:
            raise RuntimeError("Saved download filter is invalid. Start a new channel download.")

        selected = [
            item for item in candidates
            if media_filter == "all" or item.category == media_filter
        ]
        c.media = sum(1 + len(item.aliases) for item in candidates)
        c.total_files = sum(1 + len(item.aliases) for item in selected)
        c.total_bytes = sum(item.size * (1 + len(item.aliases)) for item in selected)
        if not selected:
            console.print(f"No {DOWNLOAD_FILTER_LABELS[media_filter].lower()} found in this channel.")
            return c

        if output_dir is None:
            default_dir = _default_download_dir(source.title)
            if output_ref is not None:
                output_dir = default_dir if output_ref == "default" else Path(output_ref)
            else:
                raw = console.input("Download folder [press Enter for default]: ").strip()
                if raw:
                    output_ref = raw
                    output_dir = Path(raw)
                else:
                    output_ref = "default"
                    output_dir = default_dir
        elif output_ref is None:
            output_ref = str(output_dir)
        output_dir = _prepare_download_dir(output_dir)
        if job_id is None:
            job_id = db.create_download_job(
                source.key,
                source.title,
                source_input,
                output_ref or "default",
                media_filter,
            )

        c.started = time.monotonic()
        live_ctx = Live(render_download_status(source.title, c), console=console, refresh_per_second=4) if display else None
        if live_ctx:
            live_ctx.start()
        last_refresh = 0.0

        def refresh(force: bool = False) -> None:
            nonlocal last_refresh
            if not live_ctx:
                return
            now = time.monotonic()
            if force or now - last_refresh >= 0.15:
                live_ctx.update(render_download_status(source.title, c))
                last_refresh = now

        def valid_record(row, expected_size: int = 0) -> tuple[Path, int] | None:
            if row is None:
                return None
            saved_path = Path(row["path"])
            try:
                saved_size = int(row["size"])
                valid = saved_path.is_file() and saved_path.stat().st_size == saved_size
                if expected_size > 0:
                    valid = valid and saved_size == expected_size
            except OSError:
                return None
            return (saved_path, saved_size) if valid else None

        def mark_aliases(item: DownloadCandidate, digest: str, saved_path: Path, size: int) -> None:
            for alias_id in item.aliases:
                db.mark_downloaded(
                    "telegram",
                    source.key,
                    alias_id,
                    item.media_key,
                    digest,
                    saved_path,
                    size,
                )

        def count_handled(item: DownloadCandidate, size: int, *, downloaded: bool) -> None:
            logical_count = 1 + len(item.aliases)
            logical_size = (item.size or size) * logical_count
            c.bytes_done += logical_size
            if downloaded:
                c.downloaded += 1
                c.duplicates += len(item.aliases)
            else:
                c.duplicates += logical_count

        def prepare(item: DownloadCandidate):
            previous = db.downloaded_message("telegram", source.key, item.message_id)
            valid_previous = valid_record(previous, item.size)
            if valid_previous is not None:
                saved_path, saved_size = valid_previous
                digest = str(previous["digest"])
                mark_aliases(item, digest, saved_path, saved_size)
                count_handled(item, saved_size, downloaded=False)
                return None

            same_media = db.downloaded_media("telegram", source.key, item.media_key)
            valid_media = valid_record(same_media, item.size)
            if valid_media is not None:
                saved_path, saved_size = valid_media
                digest = str(same_media["digest"])
                db.mark_downloaded(
                    "telegram", source.key, item.message_id, item.media_key, digest, saved_path, saved_size
                )
                mark_aliases(item, digest, saved_path, saved_size)
                count_handled(item, saved_size, downloaded=False)
                return None

            final_path = output_dir / _download_message_filename(item.message)
            preexisting_path: Path | None = None
            preexisting_digest: str | None = None
            preexisting_size = 0
            try:
                if final_path.is_file():
                    preexisting_path = final_path
                    preexisting_size = final_path.stat().st_size
                    preexisting_digest = hash_file(final_path)
                    duplicate = db.downloaded_digest("telegram", source.key, preexisting_digest)
                    valid_duplicate = valid_record(duplicate)
                    if valid_duplicate is not None:
                        saved_path, saved_size = valid_duplicate
                        db.mark_downloaded(
                            "telegram",
                            source.key,
                            item.message_id,
                            item.media_key,
                            preexisting_digest,
                            saved_path,
                            saved_size,
                        )
                        mark_aliases(item, preexisting_digest, saved_path, saved_size)
                        count_handled(item, saved_size, downloaded=False)
                        return None
                    final_path = _available_download_path(final_path)
            except OSError:
                final_path = _available_download_path(final_path)

            part_path = final_path.with_name(final_path.name + ".part")
            with contextlib.suppress(OSError):
                part_path.unlink()
            return (item, final_path, part_path, preexisting_path, preexisting_digest, preexisting_size)

        async def download_one(entry):
            item, final_path, part_path, *_ = entry
            progress_seen = 0
            c.active += 1
            c.current = final_path.name
            refresh(True)

            def progress(current: int, total: int) -> None:
                nonlocal progress_seen
                current_value = max(0, int(current or 0))
                delta = max(0, current_value - progress_seen)
                progress_seen = current_value
                c.bytes_received += delta
                refresh()

            try:
                result = adapter.client.download_media(
                    item.message,
                    file=str(part_path),
                    progress_callback=progress,
                )
                if inspect.isawaitable(result):
                    result = await result
                return result
            finally:
                c.active = max(0, c.active - 1)
                if c.active == 0:
                    c.current = ""
                refresh(True)

        async def download_batch(entries):
            return await asyncio.gather(
                *(download_one(entry) for entry in entries),
                return_exceptions=True,
            )

        loop = getattr(adapter.client, "loop", None)
        own_loop = False
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            own_loop = True

        try:
            for offset in range(0, len(selected), TELEGRAM_DOWNLOAD_FILE_WINDOW):
                batch_items = selected[offset : offset + TELEGRAM_DOWNLOAD_FILE_WINDOW]
                prepared = []
                for item in batch_items:
                    entry = prepare(item)
                    if entry is not None:
                        prepared.append(entry)
                refresh(True)
                if not prepared:
                    continue

                results = loop.run_until_complete(download_batch(prepared))
                rate_limit_seconds = 0
                for entry, result in zip(prepared, results):
                    item, final_path, part_path, preexisting_path, preexisting_digest, preexisting_size = entry
                    if isinstance(result, BaseException):
                        retry_after = getattr(result, "seconds", None) or _telegram_retry_after_seconds(result)
                        if retry_after:
                            rate_limit_seconds = max(rate_limit_seconds, int(retry_after))
                        else:
                            c.failed += 1
                        c.last_error = str(result)
                        with contextlib.suppress(OSError):
                            part_path.unlink()
                        continue

                    try:
                        candidate = Path(str(result)) if result else part_path
                        if not part_path.exists() and candidate.exists() and candidate != part_path:
                            os.replace(candidate, part_path)
                        if not part_path.is_file():
                            raise RuntimeError("Telegram download did not produce the expected temporary file.")
                        size = part_path.stat().st_size
                        if item.size > 0 and size != item.size:
                            raise RuntimeError(
                                f"Telegram download size mismatch: expected {item.size} bytes, received {size} bytes."
                            )
                        digest = hash_file(part_path)

                        if (
                            preexisting_path is not None
                            and preexisting_digest == digest
                            and preexisting_size == size
                            and preexisting_path.is_file()
                        ):
                            with contextlib.suppress(OSError):
                                part_path.unlink()
                            db.mark_downloaded(
                                "telegram", source.key, item.message_id, item.media_key, digest, preexisting_path, size
                            )
                            mark_aliases(item, digest, preexisting_path, size)
                            count_handled(item, size, downloaded=False)
                            c.last_error = ""
                            continue

                        duplicate = db.downloaded_digest("telegram", source.key, digest)
                        valid_duplicate = valid_record(duplicate)
                        if valid_duplicate is not None:
                            duplicate_path, duplicate_size = valid_duplicate
                            with contextlib.suppress(OSError):
                                part_path.unlink()
                            db.mark_downloaded(
                                "telegram",
                                source.key,
                                item.message_id,
                                item.media_key,
                                digest,
                                duplicate_path,
                                duplicate_size,
                            )
                            mark_aliases(item, digest, duplicate_path, duplicate_size)
                            count_handled(item, duplicate_size, downloaded=False)
                            c.last_error = ""
                            continue

                        os.replace(part_path, final_path)
                        db.mark_downloaded(
                            "telegram", source.key, item.message_id, item.media_key, digest, final_path, size
                        )
                        mark_aliases(item, digest, final_path, size)
                        count_handled(item, size, downloaded=True)
                        c.last_error = ""
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        retry_after = getattr(exc, "seconds", None) or _telegram_retry_after_seconds(exc)
                        if retry_after:
                            rate_limit_seconds = max(rate_limit_seconds, int(retry_after))
                        else:
                            c.failed += 1
                        c.last_error = str(exc)
                        with contextlib.suppress(OSError):
                            part_path.unlink()

                refresh(True)
                if rate_limit_seconds:
                    raise TelegramRateLimitError(rate_limit_seconds, "telegram download resume")
        finally:
            if own_loop:
                loop.close()

        completed_scan = True
        if c.failed == 0:
            db.finish_download_job(job_id)
        c.current = ""
        refresh(True)
        return c
    finally:
        if live_ctx:
            live_ctx.stop()
        adapter.close()
        if completed_scan and c.failed:
            console.print(f"[yellow]{c.failed} media file(s) failed and remain resumable with `telegram download resume`.[/]")


def download_public_channel(source_input: str | None = None) -> int:
    source_input = (source_input or console.input("Public channel username or t.me link: ")).strip()
    counters = _download_public_channel_run(source_input, media_filter=None)
    if counters.cancelled:
        console.print("Download cancelled.")
        return 0
    console.print(
        f"Download finished: {counters.downloaded:,} downloaded, "
        f"{counters.duplicates:,} duplicate(s) skipped, {counters.failed:,} failed."
    )
    return 0 if counters.failed == 0 else 1


def download_resume() -> int:
    db = StateDB()
    jobs = db.unfinished_download_jobs()
    if not jobs:
        console.print("No unfinished channel downloads.")
        return 0
    for i, row in enumerate(jobs, 1):
        console.print(f"{i}. {row['source_title']}")
    raw = console.input("Resume which download? ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(jobs)):
        return 1
    row = jobs[int(raw) - 1]
    counters = _download_public_channel_run(
        str(row["source_input"]),
        output_ref=str(row["output_ref"]),
        media_filter=str(row["media_filter"]),
        db=db,
        job_id=int(row["id"]),
    )
    console.print(
        f"Download resume finished: {counters.downloaded:,} downloaded, "
        f"{counters.duplicates:,} duplicate(s) skipped, {counters.failed:,} failed."
    )
    return 0 if counters.failed == 0 else 1


@dataclass
class Counters:
    total: int = 0
    completed: int = 0
    duplicates: int = 0
    failed: int = 0
    retrying: int = 0
    bytes_done: int = 0
    bytes_sent: int = 0
    premium_waits: int = 0
    flood_waits: int = 0
    last_wait_seconds: int = 0
    last_error: str = ""
    started: float = 0.0


def render_status(name: str, c: Counters) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2))
    elapsed = max(time.monotonic() - c.started, 0.001)
    speed = (c.bytes_sent or c.bytes_done) / elapsed
    table.add_row("Platform", name.title())
    table.add_row("Speed", human_bytes(speed) + "/s")
    table.add_row("Completed", f"{c.completed:,}")
    remaining = max(c.total - c.completed - c.duplicates, 0)
    table.add_row("Remaining", f"{remaining:,}")
    if c.duplicates:
        table.add_row("Duplicates", f"{c.duplicates:,}")
    if c.failed:
        table.add_row("Failed", f"{c.failed:,}")
    if c.retrying:
        table.add_row("Retry queued", f"{c.retrying:,}")
    if c.last_error:
        message = c.last_error.replace("\n", " ").strip()
        if len(message) > 120:
            message = message[:117] + "..."
        table.add_row("Last error", message)
    if c.premium_waits:
        table.add_row("Premium throttle", f"{c.premium_waits:,} wait(s), last {c.last_wait_seconds}s")
    elif c.flood_waits:
        table.add_row("Telegram waits", f"{c.flood_waits:,}, last {c.last_wait_seconds}s")
    return table


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(value)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} TB"


def _upload_job_telegram(adapter: TelegramDirect, destination: Destination, records: Sequence[FileRecord], db: StateDB, retries: int = 3, display: bool = True, counters: Counters | None = None, job_id: int | None = None) -> Counters:
    unique: dict[str, FileRecord] = {}
    local_dupes = 0
    for rec in records:
        if rec.digest in unique:
            local_dupes += 1
        else:
            unique[rec.digest] = rec
    todo = list(unique.values())

    c = counters or Counters()
    c.total = len(records)
    c.duplicates = local_dupes
    c.started = c.started or time.monotonic()
    if job_id is None:
        job_id = db.create_job(destination, todo)

    pending: list[tuple[FileRecord, int]] = []
    for rec in todo:
        if db.uploaded(destination.platform, destination.key, rec.digest):
            c.duplicates += 1
            db.set_job_file(job_id, rec.digest, "duplicate")
        else:
            pending.append((rec, 0))

    live_ctx = Live(render_status(adapter.name, c), console=console, refresh_per_second=4) if display else None
    if live_ctx:
        live_ctx.start()
    last_refresh = 0.0

    def refresh(force: bool = False) -> None:
        nonlocal last_refresh
        if not live_ctx:
            return
        now = time.monotonic()
        if force or now - last_refresh >= 0.15:
            live_ctx.update(render_status(adapter.name, c))
            last_refresh = now

    def on_bytes(amount: int) -> None:
        c.bytes_sent += amount
        refresh()

    try:
        refresh(True)
        current_round = [rec for rec, _ in pending]
        for attempt in range(retries + 1):
            if not current_round:
                break

            if attempt:
                c.retrying = len(current_round)
                refresh(True)
                # Back off once per retry round, not once per small file batch.
                # Large jobs can contain thousands of files; sleeping before every
                # 8-file batch made a healthy retry sweep appear frozen for minutes.
                time.sleep(min(2 ** attempt, 8))

            next_round: list[FileRecord] = []
            offset = 0
            while offset < len(current_round):
                batch = current_round[offset : offset + adapter.file_window]
                offset += len(batch)

                try:
                    results = adapter.upload_batch(destination, [rec.path for rec in batch], on_bytes=on_bytes)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    results = [exc] * len(batch)

                c.premium_waits = adapter._premium_waits
                c.flood_waits = adapter._flood_waits
                c.last_wait_seconds = adapter._last_wait_seconds

                rate_limit_seconds = 0
                for rec, result in zip(batch, results):
                    if isinstance(result, Exception):
                        retry_after = getattr(result, "seconds", None) or _telegram_retry_after_seconds(result)
                        if retry_after:
                            rate_limit_seconds = max(rate_limit_seconds, retry_after)
                            next_round.append(rec)
                            db.set_job_file(job_id, rec.digest, "pending", str(result))
                            c.last_error = str(result)
                            continue
                        if attempt < retries:
                            next_round.append(rec)
                        else:
                            c.failed += 1
                            c.last_error = str(result)
                            db.set_job_file(job_id, rec.digest, "failed", str(result))
                        continue

                    db.mark_uploaded(destination.platform, destination.key, rec, result)
                    db.set_job_file(job_id, rec.digest, "done")
                    c.completed += 1
                    c.bytes_done += rec.size

                # "Retry queued" means files that still need another attempt,
                # including the unprocessed portion of the current retry round.
                if attempt:
                    c.retrying = (len(current_round) - offset) + len(next_round)
                else:
                    c.retrying = len(next_round)
                refresh(True)

                if rate_limit_seconds:
                    c.retrying = (len(current_round) - offset) + len(next_round)
                    c.flood_waits += 1
                    c.last_wait_seconds = rate_limit_seconds
                    refresh(True)
                    raise TelegramRateLimitError(rate_limit_seconds)

            current_round = next_round

        c.retrying = 0
        if c.failed == 0:
            c.last_error = ""
        refresh(True)
    finally:
        if live_ctx:
            live_ctx.stop()
        db.finish_job(job_id)
    return c


def upload_job(adapter, destination: Destination, records: Sequence[FileRecord], db: StateDB, retries: int = 3, display: bool = True, counters: Counters | None = None, job_id: int | None = None) -> Counters:
    if isinstance(adapter, TelegramDirect):
        return _upload_job_telegram(adapter, destination, records, db, retries, display, counters, job_id)
    unique: dict[str, FileRecord] = {}
    local_dupes = 0
    for r in records:
        if r.digest in unique:
            local_dupes += 1
        else:
            unique[r.digest] = r
    todo = list(unique.values())
    c = counters or Counters()
    c.total = len(records)
    c.duplicates = local_dupes
    c.started = c.started or time.monotonic()
    if job_id is None:
        job_id = db.create_job(destination, todo)

    def refresh(live: Live | None = None):
        if display and live is not None:
            live.update(render_status(adapter.name, c))

    live_ctx = Live(render_status(adapter.name, c), console=console, refresh_per_second=4) if display else None
    if live_ctx:
        live_ctx.start()
    try:
        for rec in todo:
            if db.uploaded(destination.platform, destination.key, rec.digest):
                c.duplicates += 1
                db.set_job_file(job_id, rec.digest, "duplicate")
                refresh(live_ctx)
                continue
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    if attempt:
                        c.retrying = 1
                        refresh(live_ctx)
                        time.sleep(min(2 ** attempt, 8))
                    remote_id = adapter.upload(destination, rec.path)
                    db.mark_uploaded(destination.platform, destination.key, rec, remote_id)
                    db.set_job_file(job_id, rec.digest, "done")
                    c.completed += 1
                    c.bytes_done += rec.size
                    c.bytes_sent += rec.size
                    if attempt:
                        c.retrying = 0
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == retries:
                        c.failed += 1
                        c.retrying = 0
                        db.set_job_file(job_id, rec.digest, "failed", str(exc))
                finally:
                    refresh(live_ctx)
            if last_error:
                continue
    finally:
        if live_ctx:
            live_ctx.stop()
        db.finish_job(job_id)
    return c


def run_selected(telegram_username: str | None = None) -> int:
    db = StateDB()
    adapter = TelegramDirect().connect()
    try:
        if telegram_username:
            destination = adapter.resolve_username(telegram_username)
            console.print(f"Telegram username → [bold]{destination.title}[/]")
        else:
            destination = choose_telegram_destination(adapter)

        if choose_send_mode() == "text":
            text = ask_text(destination.title)
            adapter.send_text(destination, text)
            console.print(f"Text sent → [bold]{destination.title}[/]")
            return 0

        records = ask_files(db)
        if not records:
            console.print("No readable files found.")
            return 1
        console.print(f"\nFiles      {len(records):,}")
        console.print(f"Size       {human_bytes(sum(x.size for x in records))}")
        upload_job(adapter, destination, records, db)
        return 0
    finally:
        adapter.close()


def _ensure_tdlib_login_interactive(phone: str | None = None) -> bool:
    from bulkuploader.tdlib_native import TDLibNativeClient, install_tdlib_runtime, tdlib_runtime_status

    status = tdlib_runtime_status()
    if not status["supported"]:
        console.print("[dim]Fast TDLib transfer is not available on this platform; Telethon fallback is ready.[/]")
        return False
    if not status["installed"]:
        if status.get("bundled_install_supported"):
            console.print("Installing the optional fast TDLib transfer engine locally...")
            try:
                status = install_tdlib_runtime()
            except Exception as exc:
                console.print(f"[yellow]Fast TDLib setup skipped:[/] {exc}")
                console.print("[dim]Telethon transfer fallback remains ready.[/]")
                return False
        else:
            console.print("[dim]Fast TDLib transfer is optional and not installed.[/]")
            console.print(f"[dim]{status['setup_hint']}[/]")
            return False
    cfg = _configure_telegram_api_interactive()
    try:
        native = TDLibNativeClient(*cfg)
    except Exception as exc:
        console.print(f"[yellow]Fast TDLib engine unavailable:[/] {exc}")
        console.print("[dim]Telethon transfer fallback remains ready.[/]")
        return False
    try:
        if native.authorize(interactive=False, timeout=15):
            return True
        console.print("Authorizing Telegram fast-transfer session...")
        console.print("Telegram uses a separate TDLib session, but the same API ID/hash and phone number are reused.")
        try:
            native.authorize(
                interactive=True,
                phone_provider=(lambda: phone) if phone else (lambda: console.input("Phone number (with country code): ")),
                code_provider=lambda: console.input("Telegram login code (fast-transfer session): "),
                password_provider=lambda: getpass.getpass("Telegram 2-step password: "),
                timeout=300,
            )
        except Exception as exc:
            console.print(f"[yellow]Fast-transfer authorization incomplete:[/] {exc}")
            console.print("[dim]Telethon transfer fallback remains ready. Run `telegram native login` later to retry TDLib.[/]")
            return False
        return True
    finally:
        native.close()


def login() -> int:
    console.print("[bold]TG Uploader setup[/]")
    console.print("[dim]Independent third-party client for Telegram; not affiliated with or endorsed by Telegram.[/]")
    _configure_telegram_api_interactive()
    console.print("[green]✓[/] API configuration ready")

    adapter = TelegramDirect().connect(require_login=False)
    native_was_ready = adapter._native_engine
    phone: str | None = None
    try:
        if adapter.is_logged_in():
            console.print("[green]✓[/] Control session authorized")
        else:
            phone = adapter.login_interactive()
            console.print("[green]✓[/] Control session authorized")
    finally:
        adapter.close()

    native_ready = native_was_ready or _ensure_tdlib_login_interactive(phone)
    if native_ready:
        console.print("[green]✓[/] Fast-transfer session authorized")
    else:
        console.print("[green]✓[/] Telethon transfer fallback ready")
    console.print("[bold green]Setup complete.[/] Future normal use does not require another login unless a saved session is revoked or deleted.")
    return 0


def doctor() -> int:
    console.print("[bold]Telegram doctor[/]")
    console.print(f"Version        {package_version()}")
    config = _load_telegram_api_config()
    session_file = Path(str(TELEGRAM_SESSION_BASE) + ".session")
    console.print(f"API configured {'yes' if config else 'no'}")
    console.print(f"Session file   {'yes' if session_file.exists() else 'no'}")
    try:
        import cryptg  # noqa: F401
        cryptg_ready = True
    except ImportError:
        cryptg_ready = False
    console.print(f"Cryptg         {'yes' if cryptg_ready else 'no'}")
    if not config:
        console.print("Authorized     no")
        return 0
    adapter = TelegramDirect().connect(require_login=False)
    try:
        authorized = adapter.is_logged_in()
        console.print(f"Authorized     {'yes' if authorized else 'no'}")
        if authorized:
            channels, groups, chats = adapter.grouped_destinations()
            me = adapter.client.get_me()
            console.print(f"Engine         {adapter.engine_name}")
            console.print(f"Premium        {'yes' if getattr(me, 'premium', False) else 'no'}")
            console.print(f"Channels       {len(channels):,}")
            console.print(f"Groups         {len(groups):,}")
            console.print(f"Chats          {len(chats):,}")
            if adapter._native_engine:
                console.print("Transfer mode  TDLib/C++")
                console.print("Fallback       Telethon ready")
            else:
                adapter.client.loop.run_until_complete(adapter._ensure_transfer_pool())
                part_mode = "adaptive 128/256/512 KB" if adapter.transfer_mode == "main-single" else "512 KB throughput mode"
                worker_floor, worker_ceiling = adapter.worker_bounds
                console.print(f"Part sizing    {part_mode}")
                console.print(f"Part workers   {adapter.part_workers} (adaptive {worker_floor}-{worker_ceiling})")
                console.print(f"Requested conns {adapter.transfer_connections}")
                console.print(f"tmp_sessions   {adapter.tmp_sessions}")
                console.print(f"Effective conns {adapter.transfer_limit}")
                console.print(f"Transfer mode  {adapter.transfer_mode}")
                console.print(f"File window    {adapter.file_window}")
                if adapter._native_error:
                    from bulkuploader.tdlib_native import tdlib_runtime_status

                    native_status = tdlib_runtime_status()
                    console.print("Fast engine    unavailable (Telethon fallback active)")
                    if not native_status["installed"]:
                        console.print(f"Fast setup     {native_status['setup_hint']}")
        return 0
    finally:
        adapter.close()


def resume() -> int:
    db = StateDB()
    jobs = db.unfinished_jobs("telegram")
    if not jobs:
        console.print("No unfinished uploads.")
        return 0
    for i, row in enumerate(jobs, 1):
        console.print(f"{i}. {row['platform'].title()} → {row['destination_title']}")
    raw = console.input("Resume which job? ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(jobs)):
        return 1
    row = jobs[int(raw) - 1]
    job_id = int(row["id"])
    unresolved = db.unresolved_job_rows(job_id)

    # Partial-download artifacts are not real finished user files. Older jobs may
    # contain them from scans made before transient-file filtering was added. Never
    # retry them; resolve those stale rows as ignored and let a fresh scan pick up
    # the completed replacement file (for example .m4a instead of .m4a.part).
    transient_rows = [item for item in unresolved if _is_transient_file(Path(item["path"]))]
    if transient_rows:
        for item in transient_rows:
            db.set_job_file(job_id, item["digest"], "ignored", "temporary/incomplete download file")
        console.print(f"Ignored {len(transient_rows)} temporary/incomplete download file(s) from the old job.")
        unresolved = db.unresolved_job_rows(job_id)

    records = db.pending_job_records(job_id)

    if not unresolved:
        db.finish_job(job_id)
        if transient_rows:
            console.print("The old job now has no unfinished real files.")
            console.print("Run `telegram` with the same destination and folder to pick up any completed replacement files; duplicate protection will skip files already uploaded.")
        else:
            console.print("No unfinished files remain in this job.")
        return 0

    resumable_paths = {str(record.path) for record in records}
    unavailable = [item for item in unresolved if item["path"] not in resumable_paths]
    if unavailable:
        console.print(f"[yellow]Resume warning:[/] {len(unavailable)} unresolved file(s) cannot currently be reopened.")
        for item in unavailable[:10]:
            reason = item["error"] or "saved path is missing or unreadable"
            console.print(f"- {item['path']}")
            console.print(f"  [dim]{reason}[/]")
        if len(unavailable) > 10:
            console.print(f"[dim]...and {len(unavailable) - 10} more[/]")

    if not records:
        console.print("[yellow]Nothing was retried.[/] The job is still unfinished; the previous 0/0 display was misleading.")
        console.print("Re-select the same destination and folder with `telegram`; content dedupe will skip files already uploaded and retry only files that are still missing from that destination.")
        return 1

    adapter = TelegramDirect().connect()
    try:
        dest = Destination("telegram", row["destination_key"], row["destination_title"])
        upload_job(adapter, dest, records, db, job_id=job_id)
    finally:
        adapter.close()

    remaining = db.unresolved_job_rows(job_id)
    if remaining:
        console.print(f"[yellow]{len(remaining)} file(s) are still unresolved in this job.[/]")
        for item in remaining[:10]:
            reason = item["error"] or "pending"
            console.print(f"- {item['path']}")
            console.print(f"  [dim]{reason}[/]")
    return 0


def _argv_username(argv: Sequence[str] | None = None) -> str | None:
    args = list(sys.argv[1:] if argv is None else argv)
    return next((arg for arg in args if arg.startswith("@") and len(arg) > 1), None)


def _argv_download(argv: Sequence[str] | None = None) -> tuple[str, str | None] | None:
    args = list(sys.argv[1:] if argv is None else argv)
    if "download" not in args:
        return None
    marker = args.index("download")
    rest = args[marker + 1 :]
    if not rest:
        return "download", None
    if rest[0].lower() == "resume":
        if len(rest) != 1:
            raise RuntimeError("Use `telegram download resume` without extra arguments.")
        return "resume", None
    if len(rest) != 1:
        raise RuntimeError("Use `telegram download @channel` or `telegram download https://t.me/channel`.")
    return "download", rest[0]


def _argv_native(argv: Sequence[str] | None = None) -> tuple[str, str | None] | None:
    args = list(sys.argv[1:] if argv is None else argv)
    marker = next((i for i, value in enumerate(args) if value in {"native", "tdlib"}), None)
    if marker is None:
        return None
    action = args[marker + 1].lower() if marker + 1 < len(args) else "doctor"
    aliases = {
        "status": "doctor",
        "doctor": "doctor",
        "setup": "setup",
        "install": "setup",
        "login": "login",
        "auth": "login",
        "bench": "bench",
        "benchmark": "bench",
    }
    action = aliases.get(action, action)
    value = args[marker + 2] if marker + 2 < len(args) else None
    return action, value


def native_tdlib(action: str, value: str | None = None) -> int:
    from bulkuploader.tdlib_native import (
        TDJson,
        TDLibNativeClient,
        install_tdlib_runtime,
        tdlib_runtime_status,
    )

    if action == "setup":
        console.print("Setting up the optional TDLib native runtime for this platform...")
        status = install_tdlib_runtime()
        console.print(f"TDLib native runtime [bold]{'ready' if status['installed'] else 'not ready'}[/].")
        if status.get("source"):
            console.print(f"Runtime source  {status['source']}")
        return 0 if status["installed"] else 1

    status = tdlib_runtime_status()
    if action == "doctor":
        console.print("[bold]Telegram native doctor[/]")
        console.print(f"Platform       {'supported' if status['supported'] else 'unsupported'}")
        console.print(f"TDLib runtime  {'yes' if status['installed'] else 'no'}")
        if not status["installed"]:
            console.print(f"Setup          {status['setup_hint']}")
            return 0
        if status.get("source"):
            console.print(f"Runtime source {status['source']}")
        cfg = _load_telegram_api_config()
        console.print(f"API configured {'yes' if cfg else 'no'}")
        try:
            if cfg:
                # Use one native client for both version and authorization-state
                # inspection. td_receive() is process-global across client IDs.
                native = TDLibNativeClient(*cfg)
                try:
                    version = native.version() or "unknown"
                    console.print(f"TDLib version  {version}")
                    try:
                        ready = native.authorize(interactive=False, timeout=15)
                        console.print(f"Authorized     {'yes' if ready else 'no'}")
                        console.print(f"Auth state     {native.authorization_state}")
                    except TimeoutError:
                        console.print("Authorized     unknown")
                        console.print(f"Auth state     {native.authorization_state}")
                finally:
                    native.close()
            else:
                td = TDJson()
                version_obj = td.request({"@type": "getOption", "name": "version"}, timeout=5)
                console.print(f"TDLib version  {version_obj.get('value', 'unknown')}")
        except Exception as exc:
            console.print(f"TDLib load     failed ({exc})")
            return 1
        return 0

    if action == "login":
        if not status["installed"]:
            install_tdlib_runtime()
        cfg = _configure_telegram_api_interactive()
        native = TDLibNativeClient(*cfg)
        try:
            if native.authorize(interactive=False, timeout=15):
                console.print("Telegram native TDLib session is already authorized.")
                return 0
            console.print("Telegram native TDLib one-time authorization")
            console.print("No browser is used. Your phone/code/password stay in this terminal.")
            # Continue the same native client from the wait-state consumed by the
            # non-interactive probe; don't create a second process-global TDLib client.
            native.authorize(
                interactive=True,
                phone_provider=lambda: console.input("Phone number (with country code): "),
                code_provider=lambda: console.input("Telegram login code: "),
                password_provider=lambda: getpass.getpass("Telegram 2-step password: "),
                timeout=300,
            )
            console.print("Telegram native TDLib session saved locally.")
            return 0
        finally:
            native.close()

    if action == "bench":
        if not status["installed"]:
            raise RuntimeError("TDLib runtime is not installed. Run `telegram native setup` first.")
        cfg = _load_telegram_api_config()
        if not cfg:
            raise RuntimeError("Telegram API configuration is missing. Run `telegram login` first.")
        native = TDLibNativeClient(*cfg)
        try:
            if not native.authorize(interactive=False, timeout=15):
                raise RuntimeError("TDLib native session is not authorized. Run `telegram native login` once.")
            raw = value or console.input("Benchmark file path: ").strip()
            path = Path(raw).expanduser()
            last_update = [0.0]

            def progress(uploaded: int, elapsed: float) -> None:
                now = time.monotonic()
                if now - last_update[0] < 0.25:
                    return
                last_update[0] = now
                speed = uploaded / max(elapsed, 0.001)
                print(
                    f"\rTDLib upload  {human_bytes(uploaded)}  {human_bytes(speed)}/s",
                    end="",
                    flush=True,
                )

            console.print("Benchmark target: Saved Messages (benchmark message is deleted automatically)")
            result = native.benchmark_saved_messages(path, on_progress=progress, delete_after=True)
            print()
            console.print(f"Native average  [bold]{human_bytes(result.average_bytes_per_second)}/s[/]")
            console.print(f"Elapsed         {result.elapsed_seconds:.2f}s")
            return 0
        finally:
            native.close()

    raise RuntimeError("Unknown native action. Use: telegram native setup|login|doctor|bench [file]")


def _argv_action(argv: Sequence[str] | None = None) -> str | None:
    args = list(sys.argv[1:] if argv is None else argv)
    for action in ("doctor", "login", "resume"):
        if action in args:
            return action
    return None


def _run_cli(callable_):
    try:
        with TelegramInstanceLock():
            return callable_()
    except KeyboardInterrupt:
        console.print("\nCancelled.")
        return 130
    except Exception as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 1


def telegram_entry():
    try:
        download = _argv_download()
    except RuntimeError as exc:
        raise SystemExit(_run_cli(lambda exc=exc: (_ for _ in ()).throw(exc)))
    if download is not None:
        action, source = download
        if action == "resume":
            raise SystemExit(_run_cli(download_resume))
        raise SystemExit(_run_cli(lambda: download_public_channel(source)))

    native = _argv_native()
    if native is not None:
        action, value = native
        raise SystemExit(_run_cli(lambda: native_tdlib(action, value)))

    args = list(sys.argv[1:])
    if args in (["version"], ["--version"], ["-V"]):
        console.print(package_version())
        raise SystemExit(0)
    action = _argv_action(args)
    username = _argv_username(args)
    allowed = {"doctor", "login", "resume"}
    unknown = [arg for arg in args if arg not in allowed and not (arg.startswith("@") and len(arg) > 1)]
    if unknown:
        raise SystemExit(_run_cli(lambda: (_ for _ in ()).throw(
            RuntimeError(
                f"Unknown command: {unknown[0]}. Use telegram, telegram login, telegram doctor, "
                "telegram resume, telegram download @channel, telegram download resume, or telegram @username."
            )
        )))
    if action == "doctor":
        raise SystemExit(_run_cli(doctor))
    if action == "login":
        raise SystemExit(_run_cli(login))
    if action == "resume":
        raise SystemExit(_run_cli(resume))
    raise SystemExit(_run_cli(lambda: run_selected(username)))


def main(argv: Sequence[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    old_argv = sys.argv
    try:
        sys.argv = ["telegram", *args]
        telegram_entry()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()
