from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from bulkuploader.app import Counters, Destination, StateDB, scan_paths, upload_job


class MockPlatform:
    name = "telegram"

    def __init__(self):
        self.calls = []
        self.fail_once = set()

    def upload(self, destination, path):
        if path.name in self.fail_once:
            self.fail_once.remove(path.name)
            raise RuntimeError("transient")
        self.calls.append(path.name)
        return "ok-" + path.name


def run():
    with TemporaryDirectory() as td:
        root = Path(td)
        db = StateDB(root / "state.sqlite3")
        data = root / "data"
        data.mkdir()

        for i in range(10_000):
            (data / f"f{i:05d}.bin").write_bytes(f"payload-{i}".encode())
        for i in range(100):
            (data / f"dup{i:03d}.bin").write_bytes(f"payload-{i}".encode())

        records = scan_paths([data], db)
        assert len(records) == 10_100

        first = MockPlatform()
        first.fail_once = {"f00001.bin"}
        dest = Destination("telegram", "channel-1", "Mock Channel")
        c1 = upload_job(first, dest, records, db, retries=2, display=False, counters=Counters())
        assert c1.total == 10_100
        assert c1.completed == 10_000
        assert c1.duplicates == 100
        assert c1.failed == 0
        assert len(first.calls) == 10_000

        second = MockPlatform()
        c2 = upload_job(second, dest, records, db, retries=0, display=False, counters=Counters())
        assert c2.completed == 0
        assert c2.duplicates == 10_100
        assert len(second.calls) == 0

        third = MockPlatform()
        dest2 = Destination("telegram", "channel-2", "Second Channel")
        c3 = upload_job(third, dest2, records, db, retries=0, display=False, counters=Counters())
        assert c3.completed == 10_000
        assert c3.duplicates == 100

        # Exercise the SQLite/thread-safety path with two independent Telegram
        # destinations at the same time; platform isolation is no longer needed.
        left = MockPlatform()
        right = MockPlatform()
        left_dest = Destination("telegram", "sim-left", "Left")
        right_dest = Destination("telegram", "sim-right", "Right")
        with ThreadPoolExecutor(max_workers=2) as pool:
            fl = pool.submit(upload_job, left, left_dest, records[:500], db, 0, False, Counters())
            fr = pool.submit(upload_job, right, right_dest, records[:500], db, 0, False, Counters())
            cl = fl.result()
            cr = fr.result()
        assert cl.completed + cl.duplicates == 500
        assert cr.completed + cr.duplicates == 500
        assert cl.failed == 0 and cr.failed == 0

    print("TORTURE PASS: 10,100 inputs; duplicates, retry, destination isolation, and simultaneous workers verified")


if __name__ == "__main__":
    run()
