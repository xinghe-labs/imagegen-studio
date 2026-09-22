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


def poll_job(client: TestClient, job_id: str, timeout: float = 90.0, headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers or {}).json()
        if job["status"] in ("done", "error"):
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

        with mock.patch.object(server_module.subprocess, "run", side_effect=boom):
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

        class Completed:
            returncode = 1
            stdout = ""
            stderr = 'Traceback ... json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'

        with mock.patch.object(
            server_module.subprocess, "run", return_value=Completed()  # type: ignore[arg-type]
        ):
            created = self.client.post(
                "/api/generate",
                json={"prompt": "gateway sniff", "preset": "fast"},
            )
            job = poll_job(self.client, created.json()["job_id"])
            self.assertEqual(job["status"], "error")
            self.assertEqual(job["error"]["category"], "gateway")
            self.assertIn("请稍后重试", job["error"]["summary"])

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

    def test_delete_removes_image_and_sidecar(self) -> None:
        image = self._seed_record("to-delete")
        self.assertEqual(self.client.get("/api/history").json()["total"], 1)

        deleted = self.client.post("/api/delete", json={"images": [str(image)]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["count"], 1)
        self.assertFalse(image.is_file())
        self.assertFalse(Path(str(image) + ".json").is_file())
        self.assertEqual(self.client.get("/api/history").json()["total"], 0)

        outside = self.tmpdir / "outside2.png"
        outside.write_bytes(b"x")
        blocked = self.client.post("/api/delete", json={"images": [str(outside)]})
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(outside.is_file())

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



if __name__ == "__main__":
    unittest.main()
