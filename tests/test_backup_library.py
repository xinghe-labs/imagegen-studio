"""backup_library.py 脚本测试：快照完整性、.trash 排除、同秒重跑、保留策略。

回归 v1.1.1：同一秒内重复运行曾因快照目录名冲突崩溃（FileExistsError）。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "scripts" / "backup_library.py"


class BackupLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-backup-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.library = self.tmpdir / "library"
        (self.library / "references").mkdir(parents=True)
        (self.library / ".trash").mkdir()
        (self.library / "shot.png").write_bytes(b"\x89PNG-fake")
        (self.library / "shot.png.json").write_text(
            json.dumps({"record_type": "image-generation-sidecar", "prompt": "备份测试"}),
            encoding="utf-8",
        )
        (self.library / "references" / "ref.png").write_bytes(b"\x89PNG-ref")
        (self.library / ".trash" / "junk.png").write_bytes(b"\x89PNG-junk")

    def run_backup(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(self.library),
             "--dest", str(self.tmpdir / "backups"), *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def snapshots(self) -> list[Path]:
        return sorted((self.tmpdir / "backups").glob("imagegen-backup-*"))

    def test_snapshot_copies_images_ledger_and_references_but_not_trash(self) -> None:
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        snap = self.snapshots()[0]
        self.assertTrue((snap / "shot.png").is_file())
        self.assertTrue((snap / "shot.png.json").is_file(), "sidecar 账本必须一起备份")
        self.assertTrue((snap / "references" / "ref.png").is_file())
        self.assertFalse((snap / ".trash").exists(), "回收站内容不应进入快照")

    def test_same_second_rerun_does_not_crash(self) -> None:
        first = self.run_backup()
        second = self.run_backup()  # 紧接着再跑：可能落在同一秒（也可能跨秒，断言都成立）
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len({snap.name for snap in self.snapshots()}), 2, "两次快照目录名不得冲突")

    def test_days_retention_prunes_old_snapshots(self) -> None:
        self.run_backup()
        old = self.tmpdir / "backups" / "imagegen-backup-20260101-000000"
        old.mkdir()
        (old / "keep.txt").write_text("old", encoding="utf-8")
        result = self.run_backup("--days", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists(), "超过保留份数的旧快照应被清理")
        self.assertEqual(len(self.snapshots()), 1)


if __name__ == "__main__":
    unittest.main()
