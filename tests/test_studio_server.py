"""Offline API tests for the imagegen studio server (no paid endpoints)."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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


def poll_job(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
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

    def test_reproduce_command_uses_record(self) -> None:
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
                self.client.post("/api/generate", json={"prompt": "reproduce me", "preset": "quality"}).json()["job_id"],
            )
            self.assertEqual(job["status"], "done", job)
            image = job["result"]["saved"][0]
            command = self.client.post("/api/reproduce", json={"image": image}).json()["command"]
            self.assertIn("--model gpt-image-2", command)
            self.assertIn('--prompt "reproduce me"', command)
            self.assertIn("--preset quality", command)

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
