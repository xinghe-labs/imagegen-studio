"""Offline API tests for the imagegen studio server (no paid endpoints)."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Children of the server must never read HKCU\Environment during tests.
os.environ["IMAGE_GENERATION_DISABLE_USER_ENV"] = "1"

_SPEC = importlib.util.spec_from_file_location(
    "imagegen_server_under_test", TESTS_DIR.parent / "scripts" / "imagegen_server.py"
)
server_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server_module)

from fastapi.testclient import TestClient  # noqa: E402

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeImageHandler(BaseHTTPRequestHandler):
    response_plan: list[tuple[int, dict[str, object] | str]] = []
    model_list: list[str] = ["gpt-image-2"]

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send_json(
                200,
                {"data": [{"id": m, "object": "model"} for m in FakeImageHandler.model_list]},
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        status, payload = FakeImageHandler.response_plan.pop(0)
        self._send_json(status, payload)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeImageServer:
    def __init__(
        self,
        response_plan: list[tuple[int, dict[str, object] | str]],
        model_list: list[str] = ["gpt-image-2"],
    ):
        FakeImageHandler.response_plan = list(response_plan)
        FakeImageHandler.model_list = list(model_list)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeImageServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"


def make_profiles_file(directory: Path, base_url: str, api_key: str = "provider-secret-studio") -> Path:
    profiles = {
        "profiles": [{"name": "test", "base_url": base_url, "api_key": api_key}],
        "active": "test",
    }
    path = directory / "profiles.json"
    path.write_text(json.dumps(profiles), encoding="utf-8")
    return path


class FakePopen:
    """subprocess.Popen 的最小替身：communicate/kill/returncode 足够 JobManager 用。"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    def communicate(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


def poll_job(client: TestClient, job_id: str, timeout: float = 90.0, headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers or {}).json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.4)
    raise AssertionError("job did not finish in time")


class ImagegenServerTest(unittest.TestCase):
    def setUp(self) -> None:
        # resolve(): GitHub windows runners hand out short TEMP paths (RUNNER~1),
        # while the server and CLI resolve() everything internally.
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-studio-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.library = self.tmpdir / "library"
        self.profiles = make_profiles_file(self.tmpdir, "https://gateway.example/v1")
        self.app = server_module.create_app(
            library_root=self.library, profiles_path=self.profiles
        )
        self.client = TestClient(self.app)

    def test_meta_masks_keys_and_lists_presets(self) -> None:
        response = self.client.get("/api/meta")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["app"], "imagegen studio")
        self.assertIn("fast", data["presets"])
        self.assertIn("transparent", data["presets"])
        self.assertNotIn("quality", data["presets"])
        self.assertEqual(len(data["profiles"]), 1)
        self.assertTrue(data["profiles"][0]["has_key"])
        self.assertTrue(data["credentials"])
        self.assertNotIn("provider-secret-studio", response.text)

    def test_meta_credentials_false_without_any_key_source(self) -> None:
        for key in ("IMAGE_GENERATION_API_KEY", "GPT_IMAGE_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(key, None)
        self.profiles.write_text(json.dumps({"profiles": [], "active": None}), encoding="utf-8")
        data = self.client.get("/api/meta").json()
        self.assertFalse(data["credentials"])

    def test_generate_rejects_overlong_prompt(self) -> None:
        response = self.client.post(
            "/api/generate",
            json={"prompt": "x" * 4001},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("4000", response.text)

    def test_spawn_failure_marks_job_error_not_hang(self) -> None:
        # 进程启动抛错（如命令行过长）必须让 job 尽快进入 error，而不是永久 running。
        from unittest import mock

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("command line too long")

        with mock.patch.object(server_module.subprocess, "Popen", side_effect=boom):
            created = self.client.post(
                "/api/generate",
                json={"prompt": "spawn boom", "preset": "fast"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "error")
            self.assertEqual(job["error"]["category"], "spawn")
            self.assertIn("command line too long", job["error"]["summary"])

    def test_gateway_error_is_classified_from_traceback(self) -> None:
        from unittest import mock

        with mock.patch.object(
            server_module.subprocess,
            "Popen",
            return_value=FakePopen(1, stderr='Traceback ... json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
        ) as popen_mock:
            created = self.client.post(
                "/api/generate",
                json={"prompt": "gateway sniff", "preset": "fast"},
            )
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "error")
            self.assertEqual(job["error"]["category"], "gateway")
            self.assertIn("自动重试", job["error"]["summary"])
            # 网关类失败会先把整个命令自动重跑一次，再报错
            self.assertEqual(popen_mock.call_count, 2)
            self.assertEqual(job["attempts"], 2)

    def test_gateway_failure_is_retried_once_then_succeeds(self) -> None:
        from unittest import mock

        good = '{"ok": true, "saved": ["C:/tmp/fake.png"], "sidecars": [], "artifacts": [], "model": "gpt-image-2"}'
        with mock.patch.object(
            server_module.subprocess,
            "Popen",
            side_effect=[
                FakePopen(1, stderr='Traceback ... json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'),
                FakePopen(0, stdout=good),
            ],
        ) as popen_mock:
            created = self.client.post(
                "/api/generate",
                json={"prompt": "gateway retry", "preset": "fast"},
            )
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)
            self.assertEqual(popen_mock.call_count, 2)
            self.assertEqual(job["attempts"], 2)

    def test_cancel_running_job_marks_cancelled(self) -> None:
        import threading as _threading

        from unittest import mock

        started = _threading.Event()

        class SlowPopen(FakePopen):
            def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
                started.set()
                # 等取消方 kill 之后再返回（模拟被终止）
                for _ in range(100):
                    if self.killed:
                        break
                    time.sleep(0.05)
                return "", ""

        proc = SlowPopen(0)
        with mock.patch.object(server_module.subprocess, "Popen", return_value=proc):
            created = self.client.post("/api/generate", json={"prompt": "cancel me", "preset": "fast"})
            job_id = created.json()["job_id"]
            self.assertTrue(started.wait(timeout=5), "job 未开始运行")
            cancelled = self.client.post(f"/api/jobs/{job_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            job = poll_job(self.client, job_id)
            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(job["error"]["category"], "cancelled")

    def test_cancel_unknown_job_is_404(self) -> None:
        self.assertEqual(self.client.post("/api/jobs/nope/cancel").status_code, 404)

    def test_choice_bounds_validated(self) -> None:
        over_choice = self.client.post("/api/generate", json={"prompt": "ok", "choice": 11})
        self.assertEqual(over_choice.status_code, 422)
        under_choice = self.client.post("/api/generate", json={"prompt": "ok", "choice": 0})
        self.assertEqual(under_choice.status_code, 422)

    def test_generate_end_to_end_and_history(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = self.client.post(
                "/api/generate",
                json={"prompt": "studio test cat", "preset": "fast"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)
            saved = Path(job["result"]["saved"][0])
            self.assertTrue(saved.is_file())
            self.assertTrue(saved.is_relative_to(self.library))

            history = self.client.get("/api/history").json()
            self.assertEqual(history["total"], 1)
            record = history["records"][0]
            self.assertEqual(record["prompt"], "studio test cat")
            self.assertEqual(record["model"], "gpt-image-2")
            self.assertNotIn("provider-secret-studio", json.dumps(history))

    def test_rate_updates_sidecar_and_filters_favorites(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            job = poll_job(
                self.client,
                self.client.post("/api/generate", json={"prompt": "rate me", "preset": "fast"}).json()["job_id"],
            )
            self.assertEqual(job["status"], "done", job)
            image = job["result"]["saved"][0]

            rated = self.client.post("/api/rate", json={"image": image, "rating": 5})
            self.assertEqual(rated.status_code, 200, rated.text)
            sidecar = json.loads(Path(str(image) + ".json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["rating"], 5)

            favorites = self.client.get("/api/history?favorites=true").json()
            self.assertEqual(favorites["total"], 1)
            self.assertEqual(favorites["records"][0]["rating"], 5)

    def test_image_endpoint_confined_to_library(self) -> None:
        outside = self.tmpdir / "outside.png"
        outside.write_bytes(b"not an image")
        response = self.client.get(f"/api/image?path={outside}")
        self.assertEqual(response.status_code, 403)

    def test_legacy_record_type_is_listed(self) -> None:
        image = self.library / "legacy.png"
        image.write_bytes(b"fake image bytes")
        sidecar = Path(str(image) + ".json")
        sidecar.write_text(
            json.dumps({
                "record_type": "image-generation-sidecar",
                "model": "gpt-image-2",
                "prompt": "legacy record",
                "created_at": "2026-09-19T12:00:00+08:00",
                "parameters": {},
            }),
            encoding="utf-8",
        )
        history = self.client.get("/api/history").json()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["records"][0]["prompt"], "legacy record")

    def _seed_record(self, name: str, created_at: str = "2026-09-20T12:00:00+08:00") -> Path:
        image = self.library / f"{name}.png"
        image.write_bytes(b"fake-image-bytes")
        Path(str(image) + ".json").write_text(
            json.dumps({
                "record_type": "image-generation-sidecar",
                "model": "gpt-image-2",
                "prompt": name,
                "created_at": created_at,
                "parameters": {},
            }),
            encoding="utf-8",
        )
        return image

    def test_delete_moves_to_trash_and_restores(self) -> None:
        image = self._seed_record("to-delete")
        self.assertEqual(self.client.get("/api/history").json()["total"], 1)

        deleted = self.client.post("/api/delete", json={"images": [str(image)]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        payload = deleted.json()
        self.assertEqual(len(payload["deleted"]), 1)
        self.assertFalse(image.is_file())
        self.assertFalse(Path(str(image) + ".json").is_file())
        # 回收站里留着文件（可撤销），但不出现在图库列表里
        self.assertTrue(any((self.library / ".trash").iterdir()))
        self.assertEqual(self.client.get("/api/history").json()["total"], 0)

        restored = self.client.post("/api/restore", json={"moved": payload["moved"]})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertTrue(image.is_file())
        self.assertTrue(Path(str(image) + ".json").is_file())
        self.assertEqual(self.client.get("/api/history").json()["total"], 1)

        outside = self.tmpdir / "outside2.png"
        outside.write_bytes(b"x")
        blocked = self.client.post("/api/delete", json={"images": [str(outside)]})
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(outside.is_file())

    def test_restore_only_accepts_trash_sources(self) -> None:
        # 不能借 /api/restore 变成「库内任意移动文件」：来源必须在回收站里
        victim = self._seed_record("victim")
        target = self.library / "moved-away.png"
        response = self.client.post(
            "/api/restore",
            json={"moved": [{"from": str(target), "to": str(victim)}]},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertTrue(victim.is_file())
        self.assertFalse(target.exists())

    def test_trash_retention_uses_deletion_time_not_mtime(self) -> None:
        # os.replace 保留原 mtime：老图今天被删，必须仍留在回收站里（可撤销）
        image = self._seed_record("old-image")
        old = time.time() - 30 * 86400
        os.utime(image, (old, old))
        deleted = self.client.post("/api/delete", json={"images": [str(image)]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        trash = self.library / ".trash"
        # 再触发一次删除流程（内部会 prune）——刚删的条目不应被清掉
        other = self._seed_record("trigger-prune")
        self.client.post("/api/delete", json={"images": [str(other)]})
        names = [p.name for p in trash.iterdir()]
        self.assertTrue(any("old-image" in n for n in names), names)

    def test_cancelled_job_removes_partial_image_without_sidecar(self) -> None:
        import threading as _threading

        from unittest import mock

        month = self.library / datetime.now().strftime("%Y-%m")
        month.mkdir(parents=True, exist_ok=True)
        started = _threading.Event()
        stale = month / "20260101-000000-user-kept.png"  # 任务开始前就存在，绝不能删

        class SlowPopen(FakePopen):
            def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
                started.set()
                # 模拟被终止前留下一个半截文件（无账本）
                partial = month / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-half-written.png"
                partial.write_bytes(b"\x89PNG-half")
                time.sleep(2.5)  # 让它超过「2 秒新鲜度」保护（避免误删并发任务的在写文件）
                for _ in range(100):
                    if self.killed:
                        break
                    time.sleep(0.05)
                return "", ""

        # 任务开始前就存在的文件（mtime 更早），清理绝不能碰
        stale.write_bytes(b"keep me")
        old_stamp = time.time() - 3600
        os.utime(stale, (old_stamp, old_stamp))
        proc = SlowPopen(0)
        with mock.patch.object(server_module.subprocess, "Popen", return_value=proc):
            created = self.client.post("/api/generate", json={"prompt": "cancel partial", "preset": "fast"})
            job_id = created.json()["job_id"]
            self.assertTrue(started.wait(timeout=5))
            self.client.post(f"/api/jobs/{job_id}/cancel")
            # cancel() 立刻置状态，清理在工作线程里稍后完成：等 removed_partials 出现再断言
            deadline = time.time() + 10
            removed = None
            while time.time() < deadline:
                job = self.client.get(f"/api/jobs/{job_id}").json()
                removed = (job.get("error") or {}).get("removed_partials")
                if removed is not None:
                    break
                time.sleep(0.2)
            self.assertEqual(job["status"], "cancelled")
            self.assertIsNotNone(removed, "未等到残片清理完成")

        self.assertTrue(stale.is_file(), "任务开始前就有的文件被误删了")
        leftovers = [p.name for p in month.glob("*half-written*")]
        self.assertEqual(leftovers, [], "取消后应清掉无账本的半截图片")

    def test_static_assets_stay_public_under_token_mode(self) -> None:
        # 令牌模式下页面本身必须能打开（否则输错令牌就再也进不了 UI）
        app = server_module.create_app(
            library_root=self.library, profiles_path=self.profiles, token="tok-123"
        )
        client = TestClient(app)
        for path in ("/", "/app.js", "/style.css"):
            self.assertEqual(client.get(path).status_code, 200, path)
        self.assertEqual(client.get("/api/meta").status_code, 401)
        self.assertEqual(client.get("/api/meta", headers={"X-Auth-Token": "tok-123"}).status_code, 200)

    def test_zip_downloads_selected_images(self) -> None:
        first = self._seed_record("zip-one")
        second = self._seed_record("zip-two")
        zip_response = self.client.post("/api/zip", json={"images": [str(first), str(second)]})
        self.assertEqual(zip_response.status_code, 200, zip_response.text)
        self.assertEqual(zip_response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["zip-one.png", "zip-two.png"])

    def test_history_since_filter(self) -> None:
        self._seed_record("old-record", "2026-01-05T10:00:00+08:00")
        self._seed_record("new-record", "2026-09-21T10:00:00+08:00")
        all_records = self.client.get("/api/history").json()
        self.assertEqual(all_records["total"], 2)
        recent = self.client.get("/api/history", params={"since": "2026-09-01"}).json()
        self.assertEqual(recent["total"], 1)
        self.assertEqual(recent["records"][0]["prompt"], "new-record")

    def test_auth_lockout_after_repeated_failures(self) -> None:
        app = server_module.create_app(
            library_root=self.library, profiles_path=self.profiles, token="right-token"
        )
        client = TestClient(app)
        for _ in range(10):
            self.assertEqual(client.get("/api/meta", headers={"X-Auth-Token": "wrong"}).status_code, 401)
        locked = client.get("/api/meta", headers={"X-Auth-Token": "right-token"})
        self.assertEqual(locked.status_code, 429)
        self.assertIn("次数过多", locked.json()["detail"])

    def test_meta_works_without_any_profiles_file(self) -> None:
        # Regression: zero-config machines have no profiles file; /api/meta
        # must still return 200 so the UI initializes.
        app = server_module.create_app(
            library_root=self.library,
            profiles_path=self.tmpdir / "does-not-exist.json",
        )
        client = TestClient(app)
        response = client.get("/api/meta")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["profiles"], [])
        self.assertIsNone(data["active"])

    def _multi_user_app(self, users_file: Path):
        return server_module.create_app(
            library_root=self.tmpdir / "shared-root",
            profiles_path=self.profiles,
            users_path=users_file,
        )

    def test_multi_user_isolation_and_token_boundaries(self) -> None:
        users_file = self.tmpdir / "users.json"
        alice_lib = self.tmpdir / "lib-alice"
        users_file.write_text(
            json.dumps({
                "users": [
                    {"name": "alice", "token": "tok-alice", "library": str(alice_lib)},
                    {"name": "bob", "token": "tok-bob"},
                ]
            }),
            encoding="utf-8",
        )
        client = TestClient(self._multi_user_app(users_file))

        # 未带 token / 错误 token → 401
        self.assertEqual(client.get("/api/meta").status_code, 401)
        self.assertEqual(client.get("/api/meta?token=nope").status_code, 401)

        alice = {"X-Auth-Token": "tok-alice"}
        bob = {"X-Auth-Token": "tok-bob"}

        # 各自的图库根：alice 用显式目录，bob 落在 shared-root/bob
        meta_a = client.get("/api/meta", headers=alice).json()
        meta_b = client.get("/api/meta", headers=bob).json()
        self.assertEqual(meta_a["user"], "alice")
        self.assertEqual(Path(meta_a["library"]), alice_lib.resolve())
        self.assertEqual(Path(meta_b["library"]), (self.tmpdir / "shared-root" / "bob").resolve())
        self.assertNotEqual(meta_a["library"], meta_b["library"])

        # alice 生成一张（fake 网关），bob 的历史里不应出现
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = client.post(
                "/api/generate", json={"prompt": "alice private cat", "preset": "fast"}, headers=alice
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(client, created.json()["job_id"], headers=alice)
            self.assertEqual(job["status"], "done", job)
            alice_image = job["result"]["saved"][0]
            self.assertTrue(Path(alice_image).is_relative_to(alice_lib.resolve()))

        self.assertEqual(client.get("/api/history", headers=alice).json()["total"], 1)
        self.assertEqual(client.get("/api/history", headers=bob).json()["total"], 0)
        self.assertEqual(client.get("/api/stats", headers=bob).json()["total"], 0)

        # bob 不能借 alice 的路径读图或评分（confine 按各自的图库根）
        blocked = client.get("/api/image", params={"path": alice_image}, headers=bob)
        self.assertEqual(blocked.status_code, 403, blocked.text)
        denied = client.post("/api/rate", json={"image": alice_image, "rating": 5}, headers=bob)
        self.assertEqual(denied.status_code, 403, denied.text)
        # alice 自己可以
        ok = client.get("/api/image", params={"path": alice_image}, headers=alice)
        self.assertEqual(ok.status_code, 200)

    def test_token_guard_when_configured(self) -> None:
        app = server_module.create_app(
            library_root=self.library, profiles_path=self.profiles, token="s3cret"
        )
        client = TestClient(app)
        self.assertEqual(client.get("/api/meta").status_code, 401)
        self.assertEqual(
            client.get("/api/meta", headers={"X-Auth-Token": "s3cret"}).status_code, 200
        )
        self.assertEqual(client.get("/api/meta?token=s3cret").status_code, 200)

    def test_profiles_crud_activation_and_masking(self) -> None:
        created = self.client.post(
            "/api/profiles",
            data={"name": "apiclaw", "base_url": "https://gateway.example/v1", "api_key": "sk-key-one"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertNotIn("sk-key-one", created.text)

        updated = self.client.post(
            "/api/profiles",
            data={"name": "apiclaw", "base_url": "https://gateway.example/v1", "api_key": ""},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertNotIn("sk-key-one", updated.text)
        meta = self.client.get("/api/meta").json()
        entry = next(p for p in meta["profiles"] if p["name"] == "apiclaw")
        self.assertTrue(entry["has_key"])

        # 不带 /v1 的地址现在自动补全，而不是被拒
        appended = self.client.post(
            "/api/profiles", data={"name": "no-suffix", "base_url": "https://no-v1.example", "api_key": ""}
        )
        self.assertEqual(appended.status_code, 200, appended.text)
        saved = next(p for p in self.client.get("/api/meta").json()["profiles"] if p["name"] == "no-suffix")
        self.assertEqual(saved["base_url"], "https://no-v1.example/v1")
        bad = self.client.post(
            "/api/profiles", data={"name": "bad", "base_url": "ftp://no-http.example", "api_key": ""}
        )
        self.assertEqual(bad.status_code, 422)

        self.client.post(
            "/api/profiles",
            data={"name": "backup", "base_url": "https://backup.example/v1", "api_key": "sk-two"},
        )
        activated = self.client.post("/api/profiles/activate", json={"name": "backup"})
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(self.client.get("/api/meta").json()["active"], "backup")

        deleted = self.client.delete("/api/profiles/backup")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/meta").json()["active"], "test")
        missing = self.client.delete("/api/profiles/backup")
        self.assertEqual(missing.status_code, 404)

    def test_edit_end_to_end_with_upload(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = self.client.post(
                "/api/edit",
                data={"prompt": "make it dawn", "model": "gpt-image-2"},
                files={"images": ("ref.png", b"\x89PNG-fake-bytes", "image/png")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)
            saved = Path(job["result"]["saved"][0])
            self.assertTrue(saved.is_file())
            self.assertTrue(saved.is_relative_to(self.library))
            refs = list((self.library / "references").glob("*ref.png"))
            self.assertEqual(len(refs), 1)

            history = self.client.get("/api/history").json()
            self.assertEqual(history["total"], 1)
            self.assertEqual(history["records"][0]["model"], "gpt-image-2")

    def test_edit_accepts_size_format_and_rejects_bad_format(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = self.client.post(
                "/api/edit",
                data={"prompt": "recolor to night", "quality": "medium", "output_format": "png"},
                files={"images": ("ref3.png", b"\x89PNG-fake-bytes", "image/png")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)

            bad = self.client.post(
                "/api/edit",
                data={"prompt": "x", "output_format": "gif"},
                files={"images": ("ref4.png", b"\x89PNG-fake-bytes", "image/png")},
            )
            self.assertEqual(bad.status_code, 422)
            self.assertIn("png / jpeg / webp", bad.text)

    def test_edit_skips_choice_when_model_is_explicit(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = self.client.post(
                "/api/edit",
                data={"prompt": "change to night", "model": "gpt-image-2", "choice": "3"},
                files={"images": ("ref2.png", b"\x89PNG-fake-bytes", "image/png")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)

    def test_generate_skips_choice_when_model_is_explicit(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            created = self.client.post(
                "/api/generate",
                json={"prompt": "branchy oak", "model": "gpt-image-2", "choice": 2, "preset": "fast"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "done", job)

    def test_generate_rejects_missing_credentials_before_queuing(self) -> None:
        # AVOID any inherited env key slipping through on the host/CI.
        for key in ("IMAGE_GENERATION_API_KEY", "GPT_IMAGE_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(key, None)
        self.profiles.write_text(json.dumps({"profiles": [], "active": None}), encoding="utf-8")
        r = self.client.post("/api/generate", json={"prompt": "no credit card here"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("还没有配置任何网关凭据", r.json()["detail"])
        self.assertNotIn("job_id", r.json())

    def test_generate_rejects_missing_credentials_on_edit_missing_key(self) -> None:
        self.profiles.write_text(
            json.dumps({"profiles": [{"name": "nokey", "base_url": "http://127.0.0.1:1"}], "active": "nokey"}),
            encoding="utf-8",
        )
        r = self.client.post(
            "/api/edit",
            data={"prompt": "swap the sky"},
            files={"images": ("ref.png", b"\x89PNG-fake-bytes", "image/png")},
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("还没有配置任何网关凭据", r.json()["detail"])

    def test_edit_rejects_paths_outside_library(self) -> None:
        outside = self.tmpdir / "outside.png"
        outside.write_bytes(b"x")
        response = self.client.post(
            "/api/edit",
            data={"prompt": "sneaky", "image_paths": str(outside)},
        )
        self.assertEqual(response.status_code, 403)

    def test_stats_and_project_filtering(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            job = poll_job(
                self.client,
                self.client.post(
                    "/api/generate",
                    json={"prompt": "cover draft", "project": "小说A"},
                ).json()["job_id"],
            )
            self.assertEqual(job["status"], "done", job)

            stats = self.client.get("/api/stats").json()
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["this_month"], 1)
            self.assertEqual(stats["by_model"].get("gpt-image-2"), 1)
            self.assertEqual(stats["by_project"].get("小说A"), 1)

            filtered = self.client.get("/api/history", params={"project": "小说A"}).json()
            self.assertEqual(filtered["total"], 1)
            other = self.client.get("/api/history", params={"project": "别处"}).json()
            self.assertEqual(other["total"], 0)

            image = job["result"]["saved"][0]
            moved = self.client.post(
                "/api/project", json={"image": image, "project": "继承人封面"}
            )
            self.assertEqual(moved.status_code, 200)
            stats = self.client.get("/api/stats").json()
            self.assertEqual(stats["by_project"].get("继承人封面"), 1)
            self.assertNotIn("小说A", stats["by_project"])


class UsersAdminApiTest(unittest.TestCase):
    """管理员令牌的页面管理 API（users 模式）。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-admin-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.library = self.tmpdir / "shared-root"
        self.users_file = self.tmpdir / "users.json"
        self.users_file.write_text(json.dumps({
            "users": [
                {"name": "alice", "token": "tok-alice", "admin": True},
                {"name": "bob", "token": "tok-bob"},
                {"name": "carol", "token": "tok-carol"},
            ]
        }), encoding="utf-8")
        app = server_module.create_app(
            library_root=self.library, profiles_path=self.tmpdir / "profiles-absent.json",
            users_path=self.users_file,
        )
        self.client = TestClient(app)

    def auth(self, token: str) -> dict:
        return {"X-Auth-Token": token}

    def test_meta_reports_mode_and_admin_flag(self) -> None:
        meta = self.client.get("/api/meta", headers=self.auth("tok-alice")).json()
        self.assertEqual(meta["auth_mode"], "users")
        self.assertTrue(meta["is_admin"])
        meta_bob = self.client.get("/api/meta", headers=self.auth("tok-bob")).json()
        self.assertFalse(meta_bob["is_admin"])
        self.assertEqual(self.client.get("/api/meta", headers=self.auth("wrong")).status_code, 401)

    def test_list_requires_admin(self) -> None:
        self.assertEqual(self.client.get("/api/users").status_code, 401)
        self.assertEqual(self.client.get("/api/users", headers=self.auth("tok-bob")).status_code, 403)
        data = self.client.get("/api/users", headers=self.auth("tok-alice")).json()
        self.assertEqual([u["name"] for u in data["users"]], ["alice", "bob", "carol"])
        alice = next(u for u in data["users"] if u["name"] == "alice")
        self.assertTrue(alice["admin"])
        self.assertTrue(alice["is_me"])
        self.assertEqual(alice["token"], "tok-alice")  # 管理员可见完整 token

    def test_users_mode_required(self) -> None:
        empty_users = self.tmpdir / "empty-users.json"
        empty_users.write_text(json.dumps({"users": []}), encoding="utf-8")
        app = server_module.create_app(
            library_root=self.tmpdir / "l2", profiles_path=self.tmpdir / "profiles-absent.json",
            users_path=empty_users,
        )
        client = TestClient(app)
        response = client.get("/api/users")
        self.assertEqual(response.status_code, 403)
        self.assertIn("多用户", response.json()["detail"])

    def test_add_user_roundtrip_and_validation(self) -> None:
        response = self.client.post(
            "/api/users", json={"name": "dave"}, headers=self.auth("tok-alice"))
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        self.assertTrue(token)
        self.assertIn("/?token=", response.json()["link"])
        # 新 token 立即可用（热加载，无需重启）
        meta = self.client.get("/api/meta", headers=self.auth(token)).json()
        self.assertEqual(meta["user"], "dave")
        # users 文件已持久化
        persisted = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertIn("dave", [u["name"] for u in persisted["users"]])
        # 校验：重名 409 / 非法用户名 422 / 非 admin 403
        self.assertEqual(self.client.post("/api/users", json={"name": "dave"},
                                          headers=self.auth("tok-alice")).status_code, 409)
        self.assertEqual(self.client.post("/api/users", json={"name": "坏 名字"},
                                          headers=self.auth("tok-alice")).status_code, 422)
        self.assertEqual(self.client.post("/api/users", json={"name": "eve"},
                                          headers=self.auth("tok-bob")).status_code, 403)

    def test_rotate_invalidates_old_token(self) -> None:
        response = self.client.post("/api/users/bob/rotate", headers=self.auth("tok-alice"))
        self.assertEqual(response.status_code, 200)
        new_token = response.json()["token"]
        self.assertEqual(self.client.get("/api/meta", headers=self.auth("tok-bob")).status_code, 401)
        self.assertEqual(self.client.get("/api/meta", headers=self.auth(new_token)).status_code, 200)
        self.assertEqual(self.client.post("/api/users/nobody/rotate",
                                          headers=self.auth("tok-alice")).status_code, 404)

    def test_remove_user_keeps_library_files(self) -> None:
        carol_dir = self.library / "carol"
        carol_dir.mkdir(parents=True, exist_ok=True)
        (carol_dir / "keep.png").write_bytes(b"png")
        response = self.client.delete("/api/users/carol", headers=self.auth("tok-alice"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/meta", headers=self.auth("tok-carol")).status_code, 401)
        self.assertTrue((carol_dir / "keep.png").is_file(), "移除用户不动图库文件")
        # 不能移除自己
        self.assertEqual(self.client.delete("/api/users/alice", headers=self.auth("tok-alice")).status_code, 400)
        self.assertEqual(self.client.delete("/api/users/nobody", headers=self.auth("tok-alice")).status_code, 404)


class FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SelfUpdateApiTest(unittest.TestCase):
    """服务端自助更新：护栏矩阵 + 成功链路（git/pip/重启全部打桩）。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-selfup-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.restarts: list[bool] = []
        self.git_calls: list[list[str]] = []
        self._originals = {n: getattr(server_module, n) for n in
                           ("git_run", "install_requirements", "perform_restart", "fetch_latest_upstream")}
        self.addCleanup(self._restore)
        server_module.perform_restart = self._fake_restart

    def _restore(self) -> None:
        for name, fn in self._originals.items():
            setattr(server_module, name, fn)

    def _fake_restart(self) -> None:
        self.restarts.append(True)

    def patch_git(self, rev_list: str = "2", dirty: str = "", fail: str | None = None,
                  pip_rc: int = 0) -> None:
        def fake_git(args: list[str], timeout: float = 90.0):
            self.git_calls.append(list(args))
            if args[0] == "rev-parse":
                return FakeProc("true\n")
            if args[0] == "status":
                return FakeProc(dirty)
            if args[0] == "fetch":
                return FakeProc("", "network error") if fail == "fetch" else FakeProc()
            if args[0] == "rev-list":
                return FakeProc(f"{rev_list}\n")
            if args[0] == "pull":
                return FakeProc("", "conflict", 1) if fail == "pull" else FakeProc("Fast-forward\n")
            return FakeProc()

        def fake_pip():
            return FakeProc("", "pip boom", pip_rc)

        async def fake_fetch(force: bool = False):
            return {"ok": True, "latest_upstream": "9.9.9"}

        server_module.git_run = fake_git
        server_module.install_requirements = fake_pip
        server_module.fetch_latest_upstream = fake_fetch

    def make_client(self, token: str | None = None, users_file: Path | None = None):
        app = server_module.create_app(
            library_root=self.tmpdir / "lib", profiles_path=self.tmpdir / "profiles-absent.json",
            token=token, users_path=users_file,
        )
        return TestClient(app)

    def test_open_mode_forbidden(self) -> None:
        client = self.make_client()
        response = client.post("/api/self-update")
        self.assertEqual(response.status_code, 403)
        self.assertIn("未启用认证", response.json()["detail"])

    def test_users_mode_requires_admin(self) -> None:
        users_file = self.tmpdir / "users.json"
        users_file.write_text(json.dumps({
            "users": [{"name": "alice", "token": "tok-a", "admin": True}, {"name": "bob", "token": "tok-b"}],
        }), encoding="utf-8")
        client = self.make_client(users_file=users_file)
        self.assertEqual(client.post("/api/self-update", headers={"X-Auth-Token": "tok-b"}).status_code, 403)
        meta_bob = client.get("/api/meta", headers={"X-Auth-Token": "tok-b"}).json()
        self.assertFalse(meta_bob["self_update_allowed"])
        meta_alice = client.get("/api/meta", headers={"X-Auth-Token": "tok-a"}).json()
        self.assertTrue(meta_alice["self_update_allowed"])

    def test_success_flow_pulls_and_restarts(self) -> None:
        self.patch_git(rev_list="2")
        client = self.make_client(token="tok-admin")
        response = client.post("/api/self-update", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["updating"])
        self.assertTrue(self.restarts, "成功后应触发重启")
        self.assertIn("pull", [c[0] for c in self.git_calls])

    def test_already_latest_no_restart(self) -> None:
        self.patch_git(rev_list="0")
        client = self.make_client(token="tok-admin")
        response = client.post("/api/self-update", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["updating"])
        self.assertEqual(self.restarts, [])

    def test_dirty_tree_refused(self) -> None:
        self.patch_git(dirty=" M something.py\n")
        client = self.make_client(token="tok-admin")
        response = client.post("/api/self-update", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("未提交", response.json()["detail"])
        self.assertEqual(self.restarts, [])

    def test_pull_failure_refused(self) -> None:
        self.patch_git(fail="pull")
        client = self.make_client(token="tok-admin")
        response = client.post("/api/self-update", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.restarts, [])

    def test_pip_failure_aborts_before_restart(self) -> None:
        self.patch_git(pip_rc=1)
        client = self.make_client(token="tok-admin")
        response = client.post("/api/self-update", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("依赖安装失败", response.json()["detail"])
        self.assertEqual(self.restarts, [])

    def test_version_check_endpoint(self) -> None:
        self.patch_git()
        client = self.make_client(token="tok-admin")
        response = client.get("/api/version-check", headers={"X-Auth-Token": "tok-admin"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["latest_upstream"], "9.9.9")
        self.assertEqual(data["current"], server_module.APP_VERSION)


class InvitesApiTest(unittest.TestCase):
    """邀请码分发：管理员生成、朋友自助注册激活。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-invite-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.users_file = self.tmpdir / "users.json"
        self.users_file.write_text(json.dumps({
            "users": [{"name": "alice", "token": "tok-alice", "admin": True}],
        }), encoding="utf-8")
        app = server_module.create_app(
            library_root=self.tmpdir / "lib", profiles_path=self.tmpdir / "profiles-absent.json",
            users_path=self.users_file,
        )
        self.client = TestClient(app)

    def create_invite(self, **payload) -> dict:
        response = self.client.post("/api/invites", json=payload or {},
                                    headers={"X-Auth-Token": "tok-alice"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_create_requires_admin_and_persists(self) -> None:
        self.assertEqual(self.client.post("/api/invites", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/invites", json={},
                                          headers={"X-Auth-Token": "nope"}).status_code, 401)
        invite = self.create_invite()
        self.assertRegex(invite["code"], r"^[A-Z0-9]{8}$")
        self.assertEqual(invite["remaining"], 10)
        persisted = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["invites"][0]["code"], invite["code"])

    def test_register_flow_activates_user(self) -> None:
        invite = self.create_invite()
        response = self.client.post("/api/register", json={"code": invite["code"], "name": "carol"})
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        meta = self.client.get("/api/meta", headers={"X-Auth-Token": token}).json()
        self.assertEqual(meta["user"], "carol")
        self.assertFalse(meta["is_admin"])
        # 次数计数 + 持久化
        listing = self.client.get("/api/invites", headers={"X-Auth-Token": "tok-alice"}).json()
        self.assertEqual(listing["invites"][0]["used"], 1)
        persisted = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertIn("carol", [u["name"] for u in persisted["users"]])
        # 重名 409
        self.assertEqual(self.client.post("/api/register",
                                          json={"code": invite["code"], "name": "carol"}).status_code, 409)

    def test_used_up_and_expired_codes_rejected(self) -> None:
        invite = self.create_invite(max_uses=1)
        self.client.post("/api/register", json={"code": invite["code"], "name": "carol"})
        response = self.client.post("/api/register", json={"code": invite["code"], "name": "dave"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("用完", response.json()["detail"])
        # 手动把有效期改成过去（模拟过期，文件热加载）
        data = json.loads(self.users_file.read_text(encoding="utf-8"))
        data["invites"][0]["expires_at"] = "2020-01-01T00:00:00+08:00"
        self.users_file.write_text(json.dumps(data), encoding="utf-8")
        response = self.client.post("/api/register", json={"code": invite["code"], "name": "eve"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("过期", response.json()["detail"])

    def test_revoke_and_unknown_codes(self) -> None:
        invite = self.create_invite()
        code = invite["code"]
        self.assertEqual(self.client.delete(f"/api/invites/{code}",
                                            headers={"X-Auth-Token": "tok-alice"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/invite/{code}").status_code, 404)
        self.assertEqual(self.client.post("/api/register",
                                          json={"code": code, "name": "carol"}).status_code, 403)
        self.assertEqual(self.client.get("/api/invite/ZZZZZZZZ").status_code, 404)

    def test_register_requires_users_mode(self) -> None:
        app = server_module.create_app(
            library_root=self.tmpdir / "l2", profiles_path=self.tmpdir / "profiles-absent.json",
            token="tok-admin",
        )
        client = TestClient(app)
        response = client.post("/api/register", json={"code": "ABCD1234", "name": "carol"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("多用户", response.json()["detail"])

    def test_invite_info_public_no_auth(self) -> None:
        invite = self.create_invite()
        response = self.client.get(f"/api/invite/{invite['code']}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])


class SetupAdminApiTest(unittest.TestCase):
    """首次引导：全新实例用控制台一次性码创建管理员。"""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-setup-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._original_code = server_module.SETUP_CODE
        self.addCleanup(self._restore_code)
        server_module.SETUP_CODE = None  # 用例间隔离：不让上一个实例的码残留

    def _restore_code(self) -> None:
        server_module.SETUP_CODE = self._original_code

    def make_client(self, token: str | None = None):
        # 必须显式指定 users 文件：setup 端点会写入它，绝不能落到本机真实路径
        users_file = self.tmpdir / "users.json"
        app = server_module.create_app(
            library_root=self.tmpdir / "lib", profiles_path=self.tmpdir / "profiles-absent.json",
            token=token, users_path=users_file,
        )
        return TestClient(app), server_module.SETUP_CODE

    def test_open_mode_gets_setup_code(self) -> None:
        client, code = self.make_client()
        self.assertTrue(code, "全新实例应生成初始化码")
        status = client.get("/api/setup/status").json()
        self.assertTrue(status["needed"])

    def test_wrong_code_rejected_right_code_creates_admin(self) -> None:
        client, code = self.make_client()
        bad = client.post("/api/setup/admin", json={"code": "WRONGWRONG", "name": "admin"})
        self.assertEqual(bad.status_code, 403)
        response = client.post("/api/setup/admin", json={"code": code, "name": "admin"})
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        meta = client.get("/api/meta", headers={"X-Auth-Token": token}).json()
        self.assertTrue(meta["is_admin"])
        self.assertEqual(meta["auth_mode"], "users")
        # 初始化码一次性：用过即作废，再次创建被拒
        self.assertIsNone(server_module.SETUP_CODE)
        again = client.post("/api/setup/admin", json={"code": code, "name": "admin2"})
        self.assertEqual(again.status_code, 403)
        self.assertIn("已初始化", again.json()["detail"])

    def test_initialized_instance_refuses_setup(self) -> None:
        client, code = self.make_client(token="tok-owner")
        self.assertIsNone(code, "已配置 token 的实例不应生成初始化码")
        status = client.get("/api/setup/status").json()
        self.assertFalse(status["needed"])
        response = client.post("/api/setup/admin", json={"code": "whatever123", "name": "admin"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("已初始化", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
