import hashlib
import tempfile
import unittest
from pathlib import Path

import mikan_gopeed as service
import webui as service_panel


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
        self.deleted = []

    def call(self, method, path, payload=None, *, timeout=30):
        if method == "DELETE":
            self.deleted.append(path)
            return None
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

    def test_error_task_deleted_and_state_popped_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record = root / "p.json", root / "r.md"
            target = root / "目标番剧"
            target.mkdir()
            entry = self._entry(target)
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {"g1": "created", "X|1": "created"}}
            client = _FakeClient([{"id": "t1", "status": "error"}])
            service.check_pending_downloads(
                client, pending, record, state, max_attempts=3,
            )
            # 失败任务必须被删除，否则 btih 占据去重名单、重试永远不发生
            self.assertEqual(client.deleted, ["/api/v1/tasks/t1?force=true"])
            self.assertNotIn("g1", state["seen"])  # 重试：状态已摘除
            self.assertNotIn("X|1", state["seen"])
            self.assertEqual(state["download_attempts"]["X|1"], 1)
            self.assertEqual(service.load_pending(pending), {})

    def test_error_task_gives_up_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record = root / "p.json", root / "r.md"
            target = root / "目标番剧"
            target.mkdir()
            entry = self._entry(target)
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {"g1": "created", "X|1": "created"},
                     "download_attempts": {"X|1": 2}}
            service.check_pending_downloads(
                _FakeClient([{"id": "t1", "status": "error"}]),
                pending, record, state, max_attempts=3,
            )
            self.assertEqual(state["seen"]["g1"], "failed")  # 放弃并标记
            self.assertEqual(state["seen"]["X|1"], "failed")
            self.assertEqual(service.load_pending(pending), {})


class WaitRecordsTests(unittest.TestCase):
    def test_wait_returns_true_once_pending_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record, backup = root / "p.json", root / "r.md", root / "b.md"
            target = root / "目标番剧"
            target.mkdir()
            (target / "x.mp4").write_bytes(b"x")
            entry = service.make_pending_entry(
                {"title": "[ANi] X - 01", "guid": "g1"}, target, 1, [{"name": "x.mp4"}]
            )
            service.save_pending(pending, {"t1": entry})
            state = {"seen": {}}
            client = _FakeClient([{"id": "t1", "status": "done"}])
            ok = service.wait_for_records(
                client, pending, record, state, backup,
                max_wait=5, poll_interval=0, verbose=False,
            )
            self.assertTrue(ok)
            self.assertIn("第 1 集", record.read_text(encoding="utf-8"))
            self.assertEqual(service.load_pending(pending), {})

    def test_wait_times_out_and_leaves_pending(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending, record, backup = root / "p.json", root / "r.md", root / "b.md"
            target = root / "目标番剧"
            target.mkdir()
            entry = service.make_pending_entry(
                {"title": "[ANi] X - 02", "guid": "g2"}, target, 2, []
            )
            service.save_pending(pending, {"t2": entry})
            state = {"seen": {}}
            # 任务一直处于 running 状态 → 超时返回 False，pending 保留给下轮兜底
            ok = service.wait_for_records(
                _FakeClient([{"id": "t2", "status": "running"}]),
                pending, record, state, backup,
                max_wait=0, poll_interval=0, verbose=False,
            )
            self.assertFalse(ok)
            self.assertIn("t2", service.load_pending(pending))


class PanelTests(unittest.TestCase):
    def test_parse_record_md(self):
        sample = (
            "# 番剧更新记录\n\n头部说明\n"
            "\n## 2026-01-02 09:00\n- **B 第 2 集**\n"
            "  - RSS 标题：[ANi] B - 02\n  - 文件：`b.mp4`\n"
            "\n## 2026-01-01 08:00\n- **A 第 1 集**\n  - 文件：`a.mp4`\n"
        )
        entries = service_panel.parse_record_md(sample)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["time"], "2026-01-02 09:00")
        self.assertEqual(entries[0]["title"], "B 第 2 集")
        self.assertEqual(entries[0]["rss"], "[ANi] B - 02")
        self.assertEqual(entries[0]["files"], ["b.mp4"])
        self.assertEqual(entries[1]["files"], ["a.mp4"])

    def test_parse_record_md_tolerates_freeform_edit(self):
        sample = (
            "# 番剧更新记录\n"
            "\n## 2026-01-03 10:00\n- 手动加的一行随便写\n- **C 第 3 集**\n"
        )
        entries = service_panel.parse_record_md(sample)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "C 第 3 集")

    def test_parse_log_status(self):
        lines = [
            "SKIP existing file: x",
            "ERROR RSS fetch/parse failed (will retry next cycle): 网络原因",
            'SUMMARY {"items": 2, "skipped": 1, "created": 1, "would_create": 0, "errors": 0}',
        ]
        st = service_panel.parse_log_status(lines)
        self.assertEqual(st["summary"]["items"], 2)
        self.assertEqual(len(st["errors"]), 1)
        self.assertIn("RSS", st["errors"][0])

    def test_placeholder_png(self):
        data = service_panel.generate_placeholder_png(180)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 500)


    def test_series_title_fullwidth_group_brackets(self):
        # 【字幕组】用全角括号、番名在 [] 里且含多语言变体——曾是解析盲区
        title = ("【今晚月色真美】[青之箱 第二季 / アオのハコ Season 2 / "
                 "Ao no Hako (2026)][26][WebRip][1080P][AVC-8bit][AAC][简日内嵌]")
        self.assertEqual(service.series_title(title), "青之箱 第二季")
        self.assertEqual(service.new_folder_name(title), "青之箱 第二季")

    def test_series_title_multilang_outside_brackets(self):
        title = ("[ANi] Seihantai na Kimi to Boku S02 /  相反的你和我 第二季 - 25 "
                 "[1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]")
        self.assertEqual(service.series_title(title), "相反的你和我 第二季")


if __name__ == "__main__":
    unittest.main()
