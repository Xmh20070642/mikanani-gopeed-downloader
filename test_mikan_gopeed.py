import hashlib
import tempfile
import unittest
from pathlib import Path

import mikan_gopeed as service


class MikanGopeedTests(unittest.TestCase):
    def test_extract_episode(self):
        self.assertEqual(service.extract_episode("[ANi] GRAND BLUE 碧蓝之海 3 - 12 [1080P]"), 12)
        self.assertEqual(
            service.extract_episode("[ANi] 关于我转生变成史莱姆这档事 第四季 - 95 [1080P]"),
            95,
        )
        self.assertEqual(service.extract_episode("[SubGroup] 某某番剧 第08话 [720P]"), 8)
        self.assertEqual(service.extract_episode("[SubGroup] 某某番剧 EP09 [720P]"), 9)
        self.assertEqual(service.extract_episode("[SubGroup] 某某番剧 [07][1080P]"), 7)
        self.assertIsNone(service.extract_episode("[SubGroup] 某某番剧 [1080P][MP4]"))

    def test_classify_match_ambiguous(self):
        folders = ["魔法星空物语", "魔法星空物语2"]
        status, _, _ = service.classify_match("[ANi] 魔法星空 - 01", folders, {})
        self.assertEqual(status, "ambiguous")

    def test_classify_match_exact_beats_containment(self):
        folders = ["碧蓝之海 第三季", "碧蓝之海 第三季 特别篇"]
        status, folder, _ = service.classify_match(
            "GRAND BLUE 碧蓝之海 3 - 12", folders, {}
        )
        self.assertEqual(status, "matched")
        self.assertEqual(folder, "碧蓝之海 第三季")

    def test_classify_match_new(self):
        status, folder, _ = service.classify_match("[ANi] 完全未知的新番 - 01", ["猫与龙"], {})
        self.assertEqual(status, "new")
        self.assertEqual(folder, "")

    def test_existing_aliases(self):
        folders = [
            "碧蓝之海 第三季",
            "转生史莱姆 第四季",
            "与奔驰于透明之夜的你，谈一场看不见的恋爱",
        ]
        aliases = {
            "碧蓝之海 第三季": ["GRAND BLUE 碧蓝之海 3"],
            "转生史莱姆 第四季": ["关于我转生变成史莱姆这档事 第四季"],
            "与奔驰于透明之夜的你，谈一场看不见的恋爱": [
                "與奔馳於透明之夜的你，談一場看不見的戀愛。"
            ],
        }
        status, folder, _ = service.classify_match(
            "GRAND BLUE 碧蓝之海 3 - 12", folders, aliases
        )
        self.assertEqual((status, folder), ("matched", "碧蓝之海 第三季"))
        status, folder, _ = service.classify_match(
            "关于我转生变成史莱姆这档事 第四季 - 95", folders, aliases
        )
        self.assertEqual((status, folder), ("matched", "转生史莱姆 第四季"))
        status, folder, _ = service.classify_match(
            "與奔馳於透明之夜的你，談一場看不見的戀愛。 - 12", folders, aliases
        )
        self.assertEqual(
            (status, folder), ("matched", "与奔驰于透明之夜的你，谈一场看不见的恋爱")
        )

    def test_new_folder_name(self):
        self.assertEqual(
            service.new_folder_name(
                "[ANi] Grand Blue Dreaming / GRAND BLUE 碧蓝之海 3 - 12 [1080P]"
            ),
            "碧蓝之海 第三季",
        )

    def test_episode_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "[ANi] show - 12 [1080P].mp4").write_bytes(b"x")
            self.assertIsNotNone(service.episode_file(folder, 12))
            self.assertIsNone(service.episode_file(folder, 1))

    def test_magnet_extraction(self):
        links = service.extract_magnet_links(
            'href="magnet:?xt=urn:btih:2de54d12635409e08e1296704bc4f133bbd86723&amp;tr=https%3A%2F%2Fexample.test"'
        )
        self.assertEqual(len(links), 1)
        # 磁力里的 "https" 含字母 s，曾被旧正则的字符类误当分隔符截断成 tr=http
        self.assertIn("tr=https%3A%2F%2Fexample.test", links[0])

    def test_torrent_infohash(self):
        info = b"d4:name4:teste"
        raw = b"d4:info" + info + b"e"
        infohash, trackers = service.torrent_info(raw)
        self.assertEqual(infohash, hashlib.sha1(info).hexdigest())
        self.assertEqual(trackers, [])

    def test_prepend_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "番剧更新记录.md"
            service.prepend_record(path, ["## 2026-01-01 08:00", "- **A 第 1 集**"])
            service.prepend_record(path, ["## 2026-01-02 09:00", "- **B 第 2 集**"])
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("# 番剧更新记录"), 1)
            self.assertLess(text.index("2026-01-02"), text.index("2026-01-01"))
            self.assertIn("- **A 第 1 集**", text)

    def test_pending_entry_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record-pending.json"
            entry = service.make_pending_entry(
                {"title": "[ANi] X - 01", "guid": "g1"}, Path("/tmp/x"), 1, []
            )
            service.save_pending(path, {"task-1": entry})
            loaded = service.load_pending(path)
            self.assertEqual(loaded["task-1"]["series"], "x")
            self.assertEqual(loaded["task-1"]["episode"], 1)


class _FakeClient:
    def __init__(self, tasks):
        self._tasks = tasks

    def call(self, method, path, payload=None, *, timeout=30):
        return self._tasks


class CheckPendingTests(unittest.TestCase):
    def _entry(self, root):
        return service.make_pending_entry(
            {"title": "[ANi] X - 01", "guid": "g1"}, root, 1, [{"name": "x.mp4"}]
        )

    def test_done_task_recorded_and_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record, backup = root / "p.json", root / "r.md", root / "b.md"
            target = root / "目标番剧"
            target.mkdir()
            (target / "x.mp4").write_bytes(b"x")
            entry = self._entry(target)
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {"g1": "created"}}
            service.check_pending_downloads(
                _FakeClient([{"id": "t1", "status": "done"}]),
                pending, record, state, backup_path=backup,
            )
            self.assertEqual(service.load_pending(pending), {})
            self.assertIn("第 1 集", record.read_text(encoding="utf-8"))
            self.assertIn("第 1 集", backup.read_text(encoding="utf-8"))

    def test_error_task_pops_state_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record = root / "p.json", root / "r.md"
            target = root / "目标番剧"
            target.mkdir()
            entry = self._entry(target)
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {"g1": "created"}}
            service.check_pending_downloads(
                _FakeClient([{"id": "t1", "status": "error"}]),
                pending, record, state, max_attempts=3,
            )
            self.assertNotIn("g1", state["seen"])  # 重试：状态已摘除
            self.assertEqual(service.load_pending(pending)["t1"]["attempts"], 1)

    def test_error_task_gives_up_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record = root / "p.json", root / "r.md"
            target = root / "目标番剧"
            target.mkdir()
            entry = self._entry(target)
            entry["attempts"] = 2
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {"g1": "created"}}
            service.check_pending_downloads(
                _FakeClient([{"id": "t1", "status": "error"}]),
                pending, record, state, max_attempts=3,
            )
            self.assertEqual(state["seen"]["g1"], "failed")  # 放弃并标记
            self.assertEqual(service.load_pending(pending), {})


if __name__ == "__main__":
    unittest.main()
