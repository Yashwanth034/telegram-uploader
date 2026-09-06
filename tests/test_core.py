import asyncio
import sqlite3
from pathlib import Path

import pytest

from bulkuploader.app import (
    Destination,
    FileRecord,
    StateDB,
    _argv_action,
    _argv_native,
    _argv_username,
    human_bytes,
    parse_path_input,
    scan_paths,
    search_destinations,
)


def test_parse_drag_drop_paths_with_spaces(tmp_path: Path):
    a = tmp_path / "hello world.txt"
    b = tmp_path / "x.txt"
    assert parse_path_input(f"'{a}' {b}") == [a, b]


def test_parse_windows_drag_drop_paths_preserves_backslashes():
    values = parse_path_input(r'"C:\Users\Alice\My Video.mp4" D:\clip.bin', windows=True)
    assert [str(value) for value in values] == [r"C:\Users\Alice\My Video.mp4", r"D:\clip.bin"]


def test_scan_hashes_identical_content_same_digest(tmp_path: Path):
    db = StateDB(tmp_path / "state.sqlite3")
    a = tmp_path / "a.bin"
    b = tmp_path / "renamed.bin"
    a.write_bytes(b"same-content")
    b.write_bytes(b"same-content")
    records = scan_paths([tmp_path], db)
    content = [r for r in records if r.path.name in {"a.bin", "renamed.bin"}]
    assert len(content) == 2
    assert content[0].digest == content[1].digest


def test_destination_specific_duplicate_history(tmp_path: Path):
    db = StateDB(tmp_path / "state.sqlite3")
    f = tmp_path / "video.mp4"
    f.write_bytes(b"video")
    rec = scan_paths([f], db)[0]
    db.mark_uploaded("telegram", "channel-a", rec)
    assert db.uploaded("telegram", "channel-a", rec.digest)
    assert not db.uploaded("telegram", "channel-b", rec.digest)


def test_modified_file_gets_new_digest(tmp_path: Path):
    db = StateDB(tmp_path / "state.sqlite3")
    f = tmp_path / "same-name.dat"
    f.write_bytes(b"one")
    first = scan_paths([f], db)[0]
    f.write_bytes(b"two-two")
    second = scan_paths([f], db)[0]
    assert first.digest != second.digest


def test_job_resume_returns_only_pending(tmp_path: Path):
    db = StateDB(tmp_path / "state.sqlite3")
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    records = scan_paths([a, b], db)
    dest = Destination("telegram", "1", "Channel")
    jid = db.create_job(dest, records)
    db.set_job_file(jid, records[0].digest, "done")
    pending = db.pending_job_records(jid)
    assert [x.digest for x in pending] == [records[1].digest]


def test_human_bytes():
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(1024 * 1024) == "1.0 MB"


def test_command_action_parsing():
    assert _argv_action(["login"]) == "login"
    assert _argv_action(["doctor"]) == "doctor"
    assert _argv_action(["resume"]) == "resume"
    assert _argv_action([]) is None


def test_direct_username_argument_parsing():
    assert _argv_username(["@exampleuser"]) == "@exampleuser"
    assert _argv_username(["resume"]) is None
    assert _argv_username(["@alice"]) == "@alice"



def test_search_destinations_is_case_insensitive_and_partial():
    items = [
        Destination("telegram", "1", "Indian News"),
        Destination("telegram", "2", "Family Group"),
        Destination("telegram", "3", "Work Updates"),
    ]
    assert [x.title for x in search_destinations(items, "news")] == ["Indian News"]
    assert [x.title for x in search_destinations(items, "FAM")] == ["Family Group"]
    assert search_destinations(items, "missing") == []
    assert search_destinations(items, "") == items


def test_telegram_direct_is_browserless_by_design():
    from bulkuploader.app import TelegramDirect
    tg = TelegramDirect()
    assert not hasattr(tg, "page")
    assert not hasattr(tg, "context")


def test_login_reuses_phone_for_optional_tdlib_session(monkeypatch):
    import bulkuploader.app as app

    events = []

    class Adapter:
        _native_engine = False

        def connect(self, require_login=True):
            assert require_login is False
            events.append("connect")
            return self

        def is_logged_in(self):
            return False

        def login_interactive(self, phone=None):
            assert phone is None
            events.append("control-login")
            return "+10000000000"

        def close(self):
            events.append("close")

    configured = []
    monkeypatch.setattr(app, "_load_telegram_api_config", lambda: None)
    monkeypatch.setattr(app, "_configure_telegram_api_interactive", lambda: configured.append(True) or (12345, "hash"))
    monkeypatch.setattr(app, "TelegramDirect", Adapter)
    monkeypatch.setattr(
        app,
        "_ensure_tdlib_login_interactive",
        lambda phone=None: events.append(("tdlib-login", phone)) or True,
    )

    assert app.login() == 0
    assert configured == [True]
    assert events == ["connect", "control-login", "close", ("tdlib-login", "+10000000000")]


def test_optional_tdlib_setup_failure_keeps_telethon_login_usable(monkeypatch):
    import bulkuploader.app as app
    import bulkuploader.tdlib_native as tdlib

    monkeypatch.setattr(
        tdlib,
        "tdlib_runtime_status",
        lambda: {
            "supported": True,
            "installed": False,
            "bundled_install_supported": True,
            "setup_hint": "optional setup",
        },
    )

    def fail_install():
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(tdlib, "install_tdlib_runtime", fail_install)
    assert app._ensure_tdlib_login_interactive("+10000000000") is False


def test_telegram_connect_cleans_up_partial_client_on_locked_session(tmp_path: Path, monkeypatch):
    import bulkuploader.app as app
    import telethon.sync as telethon_sync

    events = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            events.append("connect")
            raise sqlite3.OperationalError("database is locked")

        def disconnect(self):
            events.append("disconnect")

    monkeypatch.setattr(app, "_load_telegram_api_config", lambda: (12345, "hash"))
    monkeypatch.setattr(app, "TELEGRAM_SESSION_BASE", tmp_path / "telegram-mtproto")
    monkeypatch.setattr(telethon_sync, "TelegramClient", Client)

    tg = app.TelegramDirect()
    with pytest.raises(RuntimeError, match="session database is busy"):
        tg.connect(require_login=False)
    assert events == ["connect", "disconnect"]
    assert tg.client is None


def test_single_instance_lock_rejects_second_telegram_process(tmp_path: Path):
    from bulkuploader.app import TelegramInstanceLock

    lock_path = tmp_path / "telegram.lock"
    with TelegramInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="Another `telegram` command is already running"):
            with TelegramInstanceLock(lock_path):
                pass


def test_telegram_direct_separates_channels_groups_and_chats():
    from bulkuploader.app import TelegramDirect
    from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, User

    class Dialog:
        def __init__(self, name, entity):
            self.name = name
            self.entity = entity

    class Client:
        def iter_dialogs(self):
            return iter([
                Dialog("News Channel", Channel(1, "News Channel", ChatPhotoEmpty(), None, broadcast=True)),
                Dialog("Super Group", Channel(2, "Super Group", ChatPhotoEmpty(), None, megagroup=True)),
                Dialog("Basic Group", Chat(3, "Basic Group", ChatPhotoEmpty(), 2, None, 1)),
                Dialog("Alice", User(4, first_name="Alice")),
                Dialog("Helper Bot", User(5, bot=True, first_name="Helper Bot")),
            ])

    tg = TelegramDirect()
    tg.client = Client()
    channels, groups, chats = tg.grouped_destinations()
    assert [x.title for x in channels] == ["News Channel"]
    assert [x.title for x in groups] == ["Basic Group", "Super Group"]
    assert [x.title for x in chats] == ["Alice", "Helper Bot"]


def test_telegram_direct_resolves_username_without_dialog_membership():
    from bulkuploader.app import TelegramDirect
    from telethon.tl.types import User

    class Client:
        def get_entity(self, username):
            assert username == "@exampleuser"
            return User(42, first_name="Example", username="exampleuser")

    tg = TelegramDirect()
    tg.client = Client()
    destination = tg.resolve_username("exampleuser")
    assert destination.platform == "telegram"
    assert destination.key == "peer:42"
    assert destination.title == "Example"
    assert destination.key in tg._entities


def test_telegram_resume_recovers_direct_username_peer_from_session_cache():
    from bulkuploader.app import TelegramDirect
    from telethon.tl.types import User

    class Client:
        def get_entity(self, peer_id):
            assert peer_id == 42
            return User(42, first_name="Example", username="exampleuser")

    tg = TelegramDirect()
    tg.client = Client()
    destination = Destination("telegram", "peer:42", "Example")
    entity = tg._entity_for(destination)
    assert entity.id == 42
    assert tg._entities["peer:42"] is entity


def test_telegram_upload_parts_are_actually_concurrent(tmp_path: Path):
    from bulkuploader.app import TELEGRAM_PART_SIZE, TelegramDirect
    from telethon.tl.functions.upload import SaveFilePartRequest

    payload = tmp_path / "parallel.bin"
    payload.write_bytes(b"x" * (TELEGRAM_PART_SIZE * 4 + 123))

    class Client:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.requests = []

        async def __call__(self, request):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.005)
            self.requests.append(request)
            self.active -= 1
            return True

    async def run_test():
        tg = TelegramDirect()
        tg._part_workers = 4
        tg.client = Client()
        progress = []
        uploaded = await tg._upload_input_async(payload, asyncio.Semaphore(4), on_bytes=progress.append)
        return tg, uploaded, progress

    tg, uploaded, progress = asyncio.run(run_test())
    expected_parts = (payload.stat().st_size + TELEGRAM_PART_SIZE - 1) // TELEGRAM_PART_SIZE
    assert uploaded.parts == expected_parts
    assert tg.client.max_active >= 2
    assert len(tg.client.requests) == expected_parts
    assert all(isinstance(request, SaveFilePartRequest) for request in tg.client.requests)
    assert sum(progress) == payload.stat().st_size


def test_telegram_main_single_uses_telethon_adaptive_part_size(tmp_path: Path):
    from bulkuploader.app import TelegramDirect
    from telethon import utils

    payload = tmp_path / "single-connection.bin"
    payload.write_bytes(b"x" * (5 * 1024 * 1024 + 123))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return True

    async def run_test():
        tg = TelegramDirect()
        tg.client = Client()
        tg._set_single_connection_mode()
        uploaded = await tg._upload_input_async(payload, asyncio.Semaphore(tg.part_workers))
        return tg, uploaded

    tg, uploaded = asyncio.run(run_test())
    expected_part_size = utils.get_appropriated_part_size(payload.stat().st_size) * 1024
    expected_parts = (payload.stat().st_size + expected_part_size - 1) // expected_part_size
    assert expected_part_size == 128 * 1024
    assert uploaded.parts == expected_parts
    assert tg.part_workers == 5


def test_telegram_large_file_uses_big_file_parts(tmp_path: Path):
    from bulkuploader.app import TELEGRAM_BIG_FILE_THRESHOLD, TelegramDirect
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig

    payload = tmp_path / "large.bin"
    payload.write_bytes(b"z" * (TELEGRAM_BIG_FILE_THRESHOLD + 1))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return True

    async def run_test():
        tg = TelegramDirect()
        tg._part_workers = 8
        tg.client = Client()
        uploaded = await tg._upload_input_async(payload, asyncio.Semaphore(8))
        return tg, uploaded

    tg, uploaded = asyncio.run(run_test())
    assert isinstance(uploaded, InputFileBig)
    assert tg.client.requests
    assert all(isinstance(request, SaveBigFilePartRequest) for request in tg.client.requests)


def test_telegram_transfer_pool_stays_single_main_without_server_permission():
    from bulkuploader.app import TelegramDirect

    class Config:
        tmp_sessions = None
        dc_options = []

    class Session:
        dc_id = 2
        auth_key = object()

    class Client:
        session = Session()

        async def __call__(self, request):
            return Config()

    tg = TelegramDirect()
    tg.client = Client()
    tg._transfer_connections = 8
    senders = asyncio.run(tg._ensure_transfer_pool())
    assert senders == []
    assert tg.tmp_sessions == 1
    assert tg.transfer_limit == 1
    assert tg.transfer_mode == "main-single"
    assert tg.part_workers == 5
    assert tg.worker_bounds == (2, 10)


def test_telegram_transfer_pool_prefers_media_endpoint(monkeypatch):
    from bulkuploader.app import TelegramDirect
    import telethon.network.mtprotosender as mtproto_module

    class MediaDc:
        id = 2
        media_only = True
        cdn = False
        ipv6 = False
        ip_address = "149.154.167.220"
        port = 443

    class Config:
        tmp_sessions = None
        dc_options = [MediaDc()]

    class Session:
        dc_id = 2
        auth_key = object()

    class Client:
        session = Session()
        _use_ipv6 = False
        _log = {}
        _proxy = None
        _local_addr = None

        async def __call__(self, request):
            return Config()

        def _connection(self, ip, port, dc_id, **kwargs):
            return (ip, port, dc_id)

    class Sender:
        def __init__(self, auth_key, **kwargs):
            self.connected = False

        async def connect(self, connection):
            self.connected = True

        async def disconnect(self):
            self.connected = False

    monkeypatch.setattr(mtproto_module, "MTProtoSender", Sender)
    tg = TelegramDirect()
    tg.client = Client()
    tg._transfer_connections = 8
    tg._transfer_target = 4
    senders = asyncio.run(tg._ensure_transfer_pool())
    assert len(senders) == 4
    assert tg.transfer_limit == 8
    assert tg.transfer_mode == "media-pool:4/8"
    asyncio.run(tg._close_transfer_pool())


def test_telegram_transfer_pool_respects_tmp_sessions(monkeypatch):
    from bulkuploader.app import TelegramDirect
    import telethon.network.mtprotosender as mtproto_module

    class Dc:
        id = 2
        ip_address = "149.154.167.50"
        port = 443

    class Config:
        tmp_sessions = 3
        dc_options = []

    class Session:
        dc_id = 2
        auth_key = object()

    class Client:
        session = Session()
        _sender = object()
        _log = {}
        _proxy = None
        _local_addr = None

        async def __call__(self, request):
            return Config()

        async def _get_dc(self, dc_id):
            return Dc()

        def _connection(self, ip, port, dc_id, **kwargs):
            return (ip, port, dc_id)

    class Sender:
        def __init__(self, auth_key, **kwargs):
            self.connected = False

        async def connect(self, connection):
            self.connected = True

        async def disconnect(self):
            self.connected = False

    monkeypatch.setattr(mtproto_module, "MTProtoSender", Sender)
    tg = TelegramDirect()
    tg.client = Client()
    tg._transfer_connections = 8
    tg._transfer_target = 4
    senders = asyncio.run(tg._ensure_transfer_pool())
    assert len(senders) == 3
    assert senders[0] is tg.client._sender
    assert len(tg._owned_transfer_senders) == 2
    assert tg.tmp_sessions == 3
    assert tg.transfer_limit == 3
    assert tg.transfer_mode == "tmp-session-pool:3/3"
    asyncio.run(tg._close_transfer_pool())


def test_telegram_transfer_pool_scales_up_when_throughput_holds():
    from bulkuploader.app import TelegramDirect

    tg = TelegramDirect()
    tg._transfer_connections = 8
    tg._transfer_limit = 8
    tg._transfer_target = 4
    tg._transfer_senders = [object(), object(), object(), object()]
    tg._part_workers = 16
    tg._last_batch_speed = None
    tg._adapt_upload_workers(16 * 1024 * 1024, 1.0, False)
    assert tg._transfer_target == 5
    assert tg._part_workers == 20


def test_telegram_parts_are_distributed_across_transfer_senders_via_client_call():
    from bulkuploader.app import TelegramDirect
    from telethon.tl.functions.upload import SaveFilePartRequest

    class MainClient:
        def __init__(self):
            self.calls = []

        async def __call__(self, request):
            raise AssertionError("main client should not carry upload parts when transfer senders exist")

        async def _call(self, sender, request):
            sender.active += 1
            sender.max_active = max(sender.max_active, sender.active)
            await asyncio.sleep(0.001)
            sender.calls += 1
            sender.active -= 1
            self.calls.append((sender, request.file_part))
            return True

    class Sender:
        def __init__(self):
            self.calls = 0
            self.active = 0
            self.max_active = 0

        async def send(self, request):
            raise AssertionError("client._call(sender, request) should be used")

    async def run_test():
        tg = TelegramDirect()
        tg.client = MainClient()
        senders = [Sender() for _ in range(4)]
        sem = asyncio.Semaphore(12)
        tasks = []
        for part in range(12):
            request = SaveFilePartRequest(123, part, b"x")
            tasks.append(asyncio.create_task(tg._send_part(request, 1, sem, senders)))
        await asyncio.gather(*tasks)
        return senders, tg.client.calls

    senders, calls = asyncio.run(run_test())
    assert [sender.calls for sender in senders] == [3, 3, 3, 3]
    assert all(sender.max_active == 1 for sender in senders)
    assert len(calls) == 12


def test_telegram_job_uses_multi_file_windows(tmp_path: Path):
    from bulkuploader.app import TelegramDirect, _upload_job_telegram

    db = StateDB(tmp_path / "state.sqlite3")
    paths = []
    for index in range(7):
        path = tmp_path / f"file-{index}.bin"
        path.write_bytes(f"payload-{index}".encode())
        paths.append(path)
    records = scan_paths(paths, db)
    destination = Destination("telegram", "peer:99", "Speed Test")

    class FastTelegram(TelegramDirect):
        def __init__(self):
            super().__init__()
            self._file_window = 3
            self.batch_sizes = []

        def upload_batch(self, destination, batch_paths, on_bytes=None):
            self.batch_sizes.append(len(batch_paths))
            results = []
            for index, path in enumerate(batch_paths):
                if on_bytes:
                    on_bytes(path.stat().st_size)
                results.append(str(index + 1))
            return results

    tg = FastTelegram()
    counters = _upload_job_telegram(tg, destination, records, db, display=False)
    assert tg.batch_sizes == [3, 3, 1]
    assert counters.completed == 7
    assert counters.failed == 0
    assert counters.bytes_sent == sum(record.size for record in records)


def test_native_tdlib_argument_parsing():
    assert _argv_native(["native"]) == ("doctor", None)
    assert _argv_native(["native", "status"]) == ("doctor", None)
    assert _argv_native(["tdlib", "setup"]) == ("setup", None)
    assert _argv_native(["native", "login"]) == ("login", None)
    assert _argv_native(["native", "benchmark", "test.bin"]) == ("bench", "test.bin")
    assert _argv_native(["resume"]) is None


def test_tdlib_runtime_status_detects_local_runtime(tmp_path: Path, monkeypatch):
    import bulkuploader.tdlib_native as tdlib

    monkeypatch.setattr(tdlib, "_platform_name", lambda: "linux")
    monkeypatch.setattr(tdlib, "_machine_name", lambda: "x86_64")
    lib_dir = tmp_path / "usr/lib/x86_64-linux-gnu/TDLib1.8.38"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libtdjson.so.1.8.38").write_bytes(b"x")
    sql_dir = tmp_path / "usr/lib/x86_64-linux-gnu"
    (sql_dir / "libsqlcipher.so.1.1.0").write_bytes(b"x")
    status = tdlib.tdlib_runtime_status(tmp_path)
    assert status["installed"] is True
    assert str(status["library"]).endswith("libtdjson.so.1.8.38")
    assert str(status["sqlcipher"]).endswith("libsqlcipher.so.1.1.0")


def test_tdlib_external_library_does_not_require_bundled_sqlcipher(tmp_path: Path, monkeypatch):
    import bulkuploader.tdlib_native as tdlib

    library = tmp_path / "tdjson.dll"
    library.write_bytes(b"x")
    monkeypatch.setenv("TDLIB_LIBRARY", str(library))
    monkeypatch.setattr(tdlib, "_platform_name", lambda: "windows")
    monkeypatch.setattr(tdlib, "_machine_name", lambda: "amd64")
    status = tdlib.tdlib_runtime_status(tmp_path / "local-runtime")
    assert status["supported"] is True
    assert status["installed"] is True
    assert status["library"] == str(library)
    assert status["sqlcipher"] is None
    assert "vcpkg" in str(status["setup_hint"]).lower()


def test_tdlib_macos_hint_uses_homebrew(monkeypatch):
    import bulkuploader.tdlib_native as tdlib

    monkeypatch.setattr(tdlib, "_platform_name", lambda: "darwin")
    assert "brew install tdlib" in tdlib.tdlib_setup_hint()


def test_tdlib_single_session_benchmark_result_math():
    from bulkuploader.tdlib_native import NativeBenchmarkResult

    result = NativeBenchmarkResult(
        bytes_uploaded=20 * 1024 * 1024,
        elapsed_seconds=2.0,
        peak_bytes_per_second=15 * 1024 * 1024,
        average_bytes_per_second=10 * 1024 * 1024,
        message_id=123,
    )
    assert result.average_bytes_per_second == 10 * 1024 * 1024
    assert result.peak_bytes_per_second == 15 * 1024 * 1024


def test_tdjson_routes_process_global_events_by_client_id():
    import json
    from collections import deque
    from bulkuploader.tdlib_native import TDJson

    class FakeLib:
        def __init__(self):
            self.events = deque([
                {"@client_id": 2, "@type": "updateOption", "name": "foreign"},
                {"@client_id": 1, "@type": "updateOption", "name": "own"},
            ])

        def td_receive(self, timeout):
            if not self.events:
                return None
            return json.dumps(self.events.popleft()).encode()

    TDJson._mailboxes.clear()
    lib = FakeLib()
    first = TDJson.__new__(TDJson)
    first.lib = lib
    first.client_id = 1
    first._extra = 0
    first._updates = deque()
    second = TDJson.__new__(TDJson)
    second.lib = lib
    second.client_id = 2
    second._extra = 0
    second._updates = deque()
    with TDJson._mailbox_lock:
        TDJson._mailboxes[1] = deque()
        TDJson._mailboxes[2] = deque()

    assert first.receive(1)["name"] == "own"
    assert second.receive(0.1)["name"] == "foreign"


def test_tdlib_authorize_continues_same_client_from_consumed_wait_state():
    from collections import deque
    from bulkuploader.tdlib_native import TDLibNativeClient

    class FakeTD:
        def __init__(self):
            self.requests = []
            self.events = deque([
                {"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateWaitCode"}},
                {"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateReady"}},
            ])

        def request(self, request, timeout=30):
            self.requests.append(request)
            return {"@type": "ok"}

        def pop_update(self):
            return None

        def receive(self, timeout=1):
            return self.events.popleft() if self.events else None

    native = TDLibNativeClient.__new__(TDLibNativeClient)
    native.td = FakeTD()
    native.ready = False
    native._authorization_state = "authorizationStateWaitPhoneNumber"
    ready = native.authorize(
        interactive=True,
        phone_provider=lambda: "+10000000000",
        code_provider=lambda: "12345",
        password_provider=lambda: "unused",
        timeout=2,
    )
    assert ready is True
    assert native.ready is True
    assert [request["@type"] for request in native.td.requests] == [
        "setAuthenticationPhoneNumber",
        "checkAuthenticationCode",
    ]


def test_tdlib_ensure_chat_maps_peer_kinds():
    from bulkuploader.tdlib_native import TDLibNativeClient

    class FakeTD:
        def __init__(self):
            self.requests = []

        def request(self, request, timeout=30):
            self.requests.append(request)
            return {"@type": "chat", "id": len(self.requests) * 100}

    native = TDLibNativeClient.__new__(TDLibNativeClient)
    native.td = FakeTD()
    assert native.ensure_chat("user", 11) == 100
    assert native.ensure_chat("basic_group", 22) == 200
    assert native.ensure_chat("supergroup", 33) == 300
    assert native.td.requests == [
        {"@type": "createPrivateChat", "user_id": 11, "force": False},
        {"@type": "createBasicGroupChat", "basic_group_id": 22, "force": False},
        {"@type": "createSupergroupChat", "supergroup_id": 33, "force": False},
    ]


def test_tdlib_send_document_reports_exact_bytes_and_message_id(tmp_path: Path):
    from collections import deque
    from bulkuploader.tdlib_native import TDLibNativeClient

    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 1024)

    class FakeTD:
        def __init__(self):
            self.events = deque([
                {"@type": "updateFile", "file": {"id": 7, "remote": {"uploaded_size": 400}}},
                {"@type": "updateFile", "file": {"id": 7, "remote": {"uploaded_size": 800}}},
                {"@type": "updateMessageSendSucceeded", "old_message_id": 55, "message": {"id": 99}},
            ])
            self.requests = []

        def request(self, request, timeout=30):
            self.requests.append(request)
            return {
                "@type": "message",
                "id": 55,
                "content": {"document": {"document": {"id": 7}}},
            }

        def pop_update(self):
            return None

        def receive(self, timeout=1):
            return self.events.popleft() if self.events else None

    native = TDLibNativeClient.__new__(TDLibNativeClient)
    native.td = FakeTD()
    native.ready = True
    chunks = []
    remote_id = native.send_document(123, payload, on_bytes=chunks.append, timeout=2)
    assert remote_id == "99"
    assert chunks == [400, 400, 224]
    assert sum(chunks) == 1024
    request = native.td.requests[0]
    assert request["@type"] == "sendMessage"
    assert request["chat_id"] == 123
    assert request["input_message_content"]["@type"] == "inputMessageDocument"
    assert request["input_message_content"]["disable_content_type_detection"] is True


def test_telegram_native_batch_keeps_errors_for_existing_retry_path(tmp_path: Path):
    from bulkuploader.app import TelegramDirect

    good = tmp_path / "good.bin"
    bad = tmp_path / "bad.bin"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")

    class Native:
        def __init__(self):
            self.calls = []

        def send_document(self, chat_id, path, on_bytes=None):
            self.calls.append((chat_id, path.name))
            if path.name == "bad.bin":
                raise RuntimeError("native send failed")
            if on_bytes:
                on_bytes(path.stat().st_size)
            return "42"

    tg = TelegramDirect()
    tg.native_client = Native()
    tg._native_engine = True
    tg._native_chat_for_entity = lambda entity: 777
    counted = []
    results = tg._upload_batch_native(object(), [good, bad], on_bytes=counted.append)
    assert results[0] == "42"
    assert isinstance(results[1], RuntimeError)
    assert tg.native_client.calls == [(777, "good.bin"), (777, "bad.bin")]
    assert sum(counted) == good.stat().st_size


def test_telegram_saved_messages_uses_stable_self_peer_key():
    from bulkuploader.app import TelegramDirect
    from telethon.tl.types import User

    me = User(123456, is_self=True, first_name="Me")

    class Client:
        def get_me(self):
            return me

    tg = TelegramDirect()
    tg.client = Client()
    destination = tg.saved_messages_destination()
    assert destination.platform == "telegram"
    assert destination.key == "peer:123456"
    assert destination.title == "Saved Messages"
    assert tg._entities[destination.key] is me


def test_telegram_saved_messages_menu_option(monkeypatch):
    import bulkuploader.app as app

    expected = Destination("telegram", "peer:77", "Saved Messages")

    class Adapter:
        def grouped_destinations(self):
            return [], [], []

        def saved_messages_destination(self):
            return expected

    monkeypatch.setattr(app.console, "input", lambda prompt="": "5")
    assert app.choose_telegram_destination(Adapter()) == expected


def test_tdlib_search_public_chat_strips_at_prefix():
    from bulkuploader.tdlib_native import TDLibNativeClient

    class FakeTD:
        def __init__(self):
            self.request_value = None

        def request(self, request, timeout=30):
            self.request_value = request
            return {"@type": "chat", "id": 987}

    native = TDLibNativeClient.__new__(TDLibNativeClient)
    native.td = FakeTD()
    assert native.search_public_chat("@example") == 987
    assert native.td.request_value == {"@type": "searchPublicChat", "username": "example"}


def test_failed_job_row_remains_unresolved_when_saved_path_is_missing(tmp_path: Path):
    db = StateDB(tmp_path / "state.sqlite3")
    path = tmp_path / "failed.bin"
    path.write_bytes(b"failed")
    rec = scan_paths([path], db)[0]
    dest = Destination("telegram", "peer:7", "Channel")
    jid = db.create_job(dest, [rec])
    db.set_job_file(jid, rec.digest, "failed", "native send failed")
    db.finish_job(jid)
    path.unlink()

    rows = db.unresolved_job_rows(jid)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "native send failed"
    assert db.pending_job_records(jid) == []
    assert [row["id"] for row in db.unfinished_jobs("telegram")] == [jid]


def test_resume_does_not_fake_zero_zero_when_unresolved_paths_are_missing(tmp_path: Path, monkeypatch, capsys):
    import bulkuploader.app as app

    db = StateDB(tmp_path / "state.sqlite3")
    path = tmp_path / "gone.bin"
    path.write_bytes(b"gone")
    rec = scan_paths([path], db)[0]
    dest = Destination("telegram", "peer:9", "Channel")
    jid = db.create_job(dest, [rec])
    db.set_job_file(jid, rec.digest, "failed", "TDLib send failed")
    db.finish_job(jid)
    path.unlink()

    monkeypatch.setattr(app, "StateDB", lambda: db)
    monkeypatch.setattr(app.console, "input", lambda prompt="": "1")

    assert app.resume() == 1
    out = capsys.readouterr().out
    assert "Resume warning" in out
    assert "Nothing was retried" in out
    assert "job is still unfinished" in out
    assert db.unfinished_jobs("telegram")


def test_scan_skips_transient_download_files(tmp_path: Path):
    db = StateDB(tmp_path / "db" / "state.sqlite3")
    media = tmp_path / "media"
    media.mkdir()
    final = media / "song.m4a"
    partial = media / "song2.m4a.part"
    chrome = media / "video.mp4.crdownload"
    final.write_bytes(b"done")
    partial.write_bytes(b"partial")
    chrome.write_bytes(b"partial")

    records = scan_paths([media], db)
    assert [record.path.name for record in records] == ["song.m4a"]


def test_scan_skips_file_that_changes_while_hashing(tmp_path: Path, monkeypatch):
    import bulkuploader.app as app

    db = StateDB(tmp_path / "state.sqlite3")
    path = tmp_path / "growing.m4a"
    path.write_bytes(b"first")
    real_hash = app.hash_file

    def changing_hash(target):
        digest = real_hash(target)
        target.write_bytes(target.read_bytes() + b"more")
        return digest

    monkeypatch.setattr(app, "hash_file", changing_hash)
    assert scan_paths([path], db) == []


def test_resume_ignores_stale_transient_rows_and_closes_old_job(tmp_path: Path, monkeypatch, capsys):
    import bulkuploader.app as app

    db = StateDB(tmp_path / "state.sqlite3")
    path = tmp_path / "song.m4a.part"
    path.write_bytes(b"partial")
    st = path.stat()
    rec = FileRecord(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, "deadbeef")
    dest = Destination("telegram", "peer:10", "Channel")
    jid = db.create_job(dest, [rec])
    db.set_job_file(jid, rec.digest, "failed", "file disappeared")
    db.finish_job(jid)
    path.unlink()

    monkeypatch.setattr(app, "StateDB", lambda: db)
    monkeypatch.setattr(app.console, "input", lambda prompt="": "1")

    assert app.resume() == 0
    out = capsys.readouterr().out
    assert "Ignored 1 temporary/incomplete download file" in out
    assert "no unfinished real files" in out
    assert db.unfinished_jobs("telegram") == []


def test_telegram_send_text_preserves_literal_content():
    from bulkuploader.app import TelegramDirect

    destination = Destination("telegram", "peer:88", "Important Notes")
    entity = object()

    class Message:
        id = 321

    class Client:
        def __init__(self):
            self.call = None

        def send_message(self, target, text, parse_mode=None):
            self.call = (target, text, parse_mode)
            return Message()

    tg = TelegramDirect()
    tg.client = Client()
    tg._entities[destination.key] = entity
    text = "Important *literal* note_with_underscores\nhttps://example.com/a_b"
    remote_id = tg.send_text(destination, text)

    assert remote_id == "321"
    assert tg.client.call == (entity, text, None)


def test_choose_send_mode_supports_files_and_text(monkeypatch):
    import bulkuploader.app as app

    monkeypatch.setattr(app.console, "input", lambda prompt="": "2")
    assert app.choose_send_mode() == "text"

    monkeypatch.setattr(app.console, "input", lambda prompt="": "")
    assert app.choose_send_mode() == "files"


def test_editable_text_moves_back_and_edits_previous_lines():
    from bulkuploader.app import EditableText

    text = EditableText.from_text("first line\nsecond line\nthird line")
    text.move_up()
    text.home()
    text.insert("UPDATED ")
    assert text.text == "first line\nUPDATED second line\nthird line"


def test_editable_text_backspace_and_delete_cross_line_boundaries():
    from bulkuploader.app import EditableText

    text = EditableText.from_text("first\nsecond")
    text.home()
    text.backspace()
    assert text.text == "firstsecond"
    assert (text.row, text.col) == (0, 5)

    text = EditableText.from_text("first\nsecond")
    text.move_up()
    text.end()
    text.delete()
    assert text.text == "firstsecond"


def test_ask_text_uses_full_editor_on_tty(monkeypatch):
    import bulkuploader.app as app

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(app.sys, "stdin", TTY())
    monkeypatch.setattr(app.sys, "stdout", TTY())
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(app, "_terminal_text_editor", lambda title: "first line\nsecond line")
    assert app.ask_text("Saved Messages") == "first line\nsecond line"
