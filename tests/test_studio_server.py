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
        self.assertIn("quality", data["presets"])
        self.assertEqual(len(data["profiles"]), 1)
        self.assertTrue(data["profiles"][0]["has_key"])
        self.assertNotIn("provider-secret-studio", response.text)

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

        bad = self.client.post(
            "/api/profiles", data={"name": "bad", "base_url": "https://no-v1.example", "api_key": ""}
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

    def test_edit_rejects_paths_outside_library(self) -> None:
        outside = self.tmpdir / "outside.png"
        outside.write_bytes(b"x")
        response = self.client.post(
            "/api/edit",
            data={"prompt": "sneaky", "image_paths": str(outside)},
        )
        self.assertEqual(response.status_code, 403)

    def test_character_lifecycle_and_generation_annotation(self) -> None:
        response = {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        with FakeImageServer([(200, response), (200, response)]) as gateway:
            self.profiles.write_text(
                json.dumps({
                    "profiles": [{"name": "test", "base_url": gateway.base_url, "api_key": "provider-secret-studio"}],
                    "active": "test",
                }),
                encoding="utf-8",
            )
            first = poll_job(
                self.client,
                self.client.post("/api/generate", json={"prompt": "studio test cat"}).json()["job_id"],
            )
            self.assertEqual(first["status"], "done", first)
            anchor = first["result"]["saved"][0]

            saved = self.client.post(
                "/api/characters",
                json={"name": "主角", "from_image": anchor},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            characters = self.client.get("/api/characters").json()["characters"]
            self.assertEqual(len(characters), 1)
            self.assertEqual(characters[0]["identity_block"], "studio test cat")
            self.assertTrue(characters[0]["has_reference"])

            second = poll_job(
                self.client,
                self.client.post(
                    "/api/generate",
                    json={"prompt": "in the rain", "character": "主角", "project": "小说A"},
                ).json()["job_id"],
            )
            self.assertEqual(second["status"], "done", second)
            history = self.client.get("/api/history").json()
            record = next(r for r in history["records"] if r["character"] == "主角")
            self.assertTrue((record["prompt"] or "").startswith("studio test cat"))
            self.assertIn("in the rain", record["prompt"])
            self.assertEqual(record["project"], "小说A")

            deleted = self.client.delete("/api/characters/主角")
            self.assertEqual(deleted.status_code, 200)
            missing = self.client.post(
                "/api/generate", json={"prompt": "x", "character": "主角"}
            )
            self.assertEqual(missing.status_code, 404)

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



class _PromptFixtureHandler(BaseHTTPRequestHandler):
    content = ""

    def do_GET(self) -> None:
        data = _PromptFixtureHandler.content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return


class PromptLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-prompts-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.app = server_module.create_app(
            library_root=self.tmpdir / "library",
            profiles_path=self.tmpdir / "profiles.json",
            prompts_path=self.tmpdir / "prompts-store.json",
        )
        self.client = TestClient(self.app)

    def test_builtin_library_listed_with_categories(self) -> None:
        data = self.client.get("/api/prompts").json()
        self.assertGreaterEqual(data["total"], 72)
        self.assertIn("人像写真", data["categories"])
        self.assertIn("水彩手绘", data["categories"])
        first = data["prompts"][0]
        self.assertEqual(first["source"], "builtin")
        self.assertGreater(len(first["prompt"]), 40)

    def test_custom_prompt_add_search_delete(self) -> None:
        added = self.client.post(
            "/api/prompts",
            json={"title_zh": "我的测试收藏", "prompt": "A custom test prompt about lighthouse at dusk, dramatic waves", "tags": ["测试"]},
        )
        self.assertEqual(added.status_code, 200, added.text)
        duplicate = self.client.post(
            "/api/prompts",
            json={"title_zh": "重复", "prompt": "A custom test prompt about lighthouse at dusk, dramatic waves"},
        )
        self.assertEqual(duplicate.status_code, 409)

        found = self.client.get("/api/prompts", params={"q": "lighthouse"}).json()
        self.assertEqual(found["total"], 1)
        entry = next(p for p in found["prompts"] if p["source"] == "custom")

        # builtin entries cannot be deleted
        self.assertEqual(self.client.delete("/api/prompts/builtin-001").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/prompts/{entry['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/prompts", params={"q": "lighthouse"}).json()["total"], 0)

    def test_sync_pulls_sources_and_deduplicates(self) -> None:
        markdown = chr(10).join([
            "# 别人的提示词清单",
            "- A misty lighthouse on a cliff at dawn, sea fog, cold blue palette",
            "- [a link line](https://example.com) should be ignored",
            "* Golden wheat field under storm light, dramatic clouds, wind motion",
        ])
        _PromptFixtureHandler.content = markdown
        fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), _PromptFixtureHandler)
        threading.Thread(target=fixture_server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (fixture_server.shutdown(), fixture_server.server_close()))
        base = f"http://127.0.0.1:{fixture_server.server_address[1]}"

        added = self.client.post(
            "/api/prompts/sources",
            data={"name": "testsrc", "url": f"{base}/prompts.md", "format": "markdown"},
        )
        self.assertEqual(added.status_code, 200, added.text)

        synced = self.client.post("/api/prompts/sync").json()
        self.assertEqual(synced["synced"]["testsrc"], "+2", synced)

        data = self.client.get("/api/prompts", params={"category": "拉取"}).json()
        self.assertEqual(data["total"], 2)
        pulled = data["prompts"][0]
        self.assertEqual(pulled["source"], "github:testsrc")

        # second sync: all deduplicated
        synced2 = self.client.post("/api/prompts/sync").json()
        self.assertEqual(synced2["synced"]["testsrc"], "+0")

    def test_markdown_and_json_parsers(self) -> None:
        md = server_module.parse_prompt_source(
            chr(10).join([
                "- A cyberpunk street at night with neon rain and wet reflections, cinematic",
                "short line",
            ]),
            "markdown",
        )
        self.assertEqual(len(md), 1)
        js = server_module.parse_prompt_source(
            '[{"prompt": "A glass bird sculpture, studio light", "title": "玻璃鸟"}]',
            "json",
        )
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0]["title_zh"], "玻璃鸟")

if __name__ == "__main__":
    unittest.main()
