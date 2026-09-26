"""Playwright 冒烟测试：真实浏览器驱动 UI，全程离线（假网关 + 假 CLI）。

覆盖四条主链路：
1. 页面加载 — 版本号、模型下拉、profile 面板
2. 草稿记忆 — 输入 → 刷新 → 原样恢复
3. 文生图端到端 — 点击生成 → 假 CLI 出图 → 图库出现新卡片 → sidecar 落库
4. 图库整理 — 收藏写回账本、多选全选、批量打包 zip 下载

另有一条安全断言：provider API key 永不出现在页面或 localStorage。

跑法与主套件一致（未安装 playwright 或浏览器时自动跳过，CI 里显式安装）：
    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# 与主套件相同的隔离：服务进程不得读取 HKCU\Environment 里的真实凭据。
os.environ["IMAGE_GENERATION_DISABLE_USER_ENV"] = "1"

_SPEC = importlib.util.spec_from_file_location(
    "imagegen_server_under_test", TESTS_DIR.parent / "scripts" / "imagegen_server.py"
)
server_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server_module)

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # 本地未安装 playwright 时跳过 UI 套件
    sync_playwright = None

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
SECRET_KEY = "ui-smoke-provider-secret"

# 假 image-gen CLI：立即在 --output 写出 1x1 PNG + sidecar，stdout 打一份
# 与真 CLI 同构的结果 JSON（image + sidecars），服务端按原流程收集入账。
FAKE_CLI_SOURCE = f"""\
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


output = Path(opt("--output"))
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(base64.b64decode("{ONE_PIXEL_PNG_B64}"))
record = {{
    "schema_version": 1,
    "record_type": "image-generation-sidecar",
    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "command": argv[0] if argv else "generate",
    "model": opt("--model") or "gpt-image-2",
    "prompt": opt("--prompt"),
    "rating": 0,
    "parameters": {{"preset": opt("--preset"), "size": opt("--size")}},
    "output": {{"final_size": [1, 1]}},
}}
sidecar = Path(str(output) + ".json")
sidecar.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
print(json.dumps({{"image": str(output), "sidecars": [str(sidecar)]}}))
"""


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容网关的最小替身：只应答模型列表；真正的生成走假 CLI。"""

    model_ids: list[str] = ["gpt-image-2"]

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send_json(
                200, {"data": [{"id": m, "object": "model"} for m in self.model_ids]}
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        self._send_json(
            500, {"error": {"message": "fake gateway: 生成必须走假 CLI，不应触网"}}
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornThread:
    """后台线程里跑真正的 HTTP 服务（Playwright 需要真实端口，TestClient 不够）。"""

    def __init__(self, app: object) -> None:
        import uvicorn

        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> "_UvicornThread":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn 15 秒内未能启动")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _restore_cli_env() -> None:
    """把 IMAGE_GEN_CLI 恢复原值（CI 里它指向真实引擎，不能被假 CLI 顶掉）。"""
    original = ImagegenUISmokeTest._original_cli_env
    if original is None:
        os.environ.pop("IMAGE_GEN_CLI", None)
    else:
        os.environ["IMAGE_GEN_CLI"] = original


@unittest.skipUnless(
    sync_playwright is not None,
    "未安装 playwright：pip install playwright && python -m playwright install chromium",
)
class ImagegenUISmokeTest(unittest.TestCase):
    """全离线 UI 冒烟：假网关 + 假 CLI + 真浏览器。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls._pw = sync_playwright().start()
            cls.browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:  # 浏览器二进制缺失等情况按跳过处理
            raise unittest.SkipTest(
                f"playwright 浏览器不可用（python -m playwright install chromium）：{exc}"
            )
        # 类清理按 LIFO 执行：先注册 pw.stop（最后执行），browser.close 才能先跑
        cls.addClassCleanup(cls._pw.stop)
        cls.addClassCleanup(cls.browser.close)

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-ui-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmpdir, ignore_errors=True)
        cls.library = cls.tmpdir / "library"
        cls.library.mkdir(parents=True)

        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
        threading.Thread(target=cls.gateway.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.gateway.shutdown)
        gateway_url = f"http://127.0.0.1:{cls.gateway.server_address[1]}/v1"

        profiles = {
            "profiles": [{"name": "test", "base_url": gateway_url, "api_key": SECRET_KEY}],
            "active": "test",
        }
        profiles_path = cls.tmpdir / "profiles.json"
        profiles_path.write_text(json.dumps(profiles), encoding="utf-8")

        cli_path = cls.tmpdir / "fake_image_gen.py"
        cli_path.write_text(FAKE_CLI_SOURCE, encoding="utf-8")
        cls._original_cli_env = os.environ.get("IMAGE_GEN_CLI")
        os.environ["IMAGE_GEN_CLI"] = str(cli_path)
        cls.addClassCleanup(_restore_cli_env)

        app = server_module.create_app(library_root=cls.library, profiles_path=profiles_path)
        cls.http = _UvicornThread(app)
        cls.http.__enter__()
        cls.addClassCleanup(cls.http.__exit__, None, None, None)

    def setUp(self) -> None:
        for child in self.library.iterdir():  # 每条用例从空图库开始
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        self._js_errors: list[str] = []
        context = self.browser.new_context(accept_downloads=True)
        context.on("pageerror", lambda exc: self._js_errors.append(str(exc)))
        context.on("dialog", lambda dialog: dialog.accept())  # 删除确认等 window.confirm
        self.addCleanup(self._fail_on_js_errors)
        self.addCleanup(context.close)
        self.page = context.new_page()

    def _fail_on_js_errors(self) -> None:
        if self._js_errors:
            self.fail("页面出现未捕获的 JS 错误：" + "; ".join(self._js_errors))

    def open_app(self) -> None:
        self.page.goto(self.http.base_url)
        self.page.wait_for_load_state("networkidle")

    def seed_image(self, prompt: str) -> Path:
        month = self.library / datetime.now().strftime("%Y-%m")
        month.mkdir(parents=True, exist_ok=True)
        image_path = month / f"seed-{uuid.uuid4().hex[:8]}.png"
        image_path.write_bytes(base64.b64decode(ONE_PIXEL_PNG_B64))
        record = {
            "schema_version": 1,
            "record_type": "image-generation-sidecar",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "command": "generate",
            "model": "gpt-image-2",
            "prompt": prompt,
            "rating": 0,
            "parameters": {},
            "output": {"final_size": [1, 1]},
        }
        Path(str(image_path) + ".json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        return image_path

    def test_page_renders_with_version_and_profiles(self) -> None:
        self.open_app()
        self.assertIn("imagegen studio", self.page.title())
        # 版本号来自 /api/meta（APP_VERSION），页面头部原样展示
        self.assertEqual(
            self.page.locator("#app-version").inner_text(), f"v{server_module.APP_VERSION}"
        )
        # 模型下拉永远有默认项；编号目录内容随机器而异，不做强断言
        self.assertIn("gpt-image-2", self.page.locator("#model").inner_text())
        profile_text = self.page.locator("#profile-select").inner_text()
        self.assertIn("test", profile_text)
        self.assertIn("✓key", profile_text)
        self.assertTrue(self.page.locator("#prompt").is_visible())
        self.assertTrue(self.page.locator("#generate-btn").is_enabled())

    def test_draft_survives_reload(self) -> None:
        self.open_app()
        self.page.fill("#prompt", "雨夜便利店的一盏灯")
        self.page.select_option("#n", "3")
        time.sleep(0.7)  # 提示词草稿走 400ms 防抖
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.page.input_value("#prompt"), "雨夜便利店的一盏灯")
        self.assertEqual(self.page.input_value("#n"), "3")

    def test_generate_end_to_end_lands_in_gallery(self) -> None:
        self.open_app()
        self.page.fill("#prompt", "深夜食堂的霓虹招牌")
        self.page.click("#generate-btn")
        self.page.wait_for_selector("#gallery .card", timeout=30_000)
        self.assertEqual(self.page.locator("#gallery .card").count(), 1)
        caption = self.page.locator("#gallery .card figcaption").inner_text()
        self.assertIn("深夜食堂", caption)
        # sidecar 已落库：假 CLI 写出 → 服务端补注（提示词/模型）→ 账本扫描
        sidecars = list(self.library.rglob("*.json"))
        self.assertEqual(len(sidecars), 1)
        record = json.loads(sidecars[0].read_text(encoding="utf-8"))
        self.assertEqual(record["prompt"], "深夜食堂的霓虹招牌")
        self.assertEqual(record["model"], "gpt-image-2")

    def test_preset_chip_selects_and_reaches_cli(self) -> None:
        """回归 v1.1.1：预设 chip 必须能点击选中，并把 preset 传到引擎。"""
        self.open_app()
        self.page.click('#preset button[data-v="fast"]')
        self.assertEqual(
            self.page.evaluate("document.querySelector('#preset .on')?.dataset.v"), "fast"
        )
        self.page.fill("#prompt", "预设传递测试")
        self.page.click("#generate-btn")
        self.page.wait_for_selector("#gallery .card", timeout=30_000)
        sidecar = list(self.library.rglob("*.json"))[0]
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(record["parameters"]["preset"], "fast")

    def test_undo_toast_restores_deleted_image(self) -> None:
        """回归 v1.1.1：删除后 toast 里的「撤销」按钮必须可点，且能真正恢复。"""
        self.seed_image("待删除后撤销的图")
        self.open_app()
        self.page.wait_for_selector("#gallery .card")
        self.page.locator("#gallery .card").first.click()
        self.page.wait_for_selector("#detail:not(.hidden)")
        self.page.click("#delete-img")  # confirm 对话框由 handler 自动接受
        self.page.wait_for_selector('.toast:has-text("撤销")', timeout=8_000)
        self.assertEqual(self.page.locator("#gallery .card").count(), 0)
        self.assertTrue(list(self.library.rglob(".trash/**/*.png")), "删除后应进回收站")
        self.page.locator(".toast-action", has_text="撤销").click()
        self.page.wait_for_selector("#gallery .card", timeout=8_000)
        self.assertEqual(self.page.locator("#gallery .card").count(), 1)
        self.assertFalse(list(self.library.rglob(".trash/**/*.png")), "撤销后回收站应清空")

    def test_favorite_batch_select_and_zip(self) -> None:
        self.seed_image("第一张：雨夜便利店")
        self.seed_image("第二张：雪夜电话亭")
        self.open_app()
        self.page.wait_for_selector("#gallery .card")
        self.assertEqual(self.page.locator("#gallery .card").count(), 2)

        # 收藏：hover 出快捷栏 → ☆ 变 ★，评分写回 sidecar
        first_card = self.page.locator("#gallery .card").first
        first_card.hover()
        first_card.locator('[data-quick="fav"]').click()
        self.page.wait_for_selector("#gallery .star-badge")
        ratings = sorted(
            json.loads(p.read_text(encoding="utf-8")).get("rating")
            for p in self.library.rglob("*.json")
        )
        self.assertEqual(ratings, [0, 5])  # 收藏 = 五分制满分

        # 多选：进入选择模式 → 点选一张（批量栏此时才出现）→ 全选 → 计数 2 → 打包 zip
        self.page.click("#select-mode")
        self.page.locator("#gallery .card").first.click(position={"x": 20, "y": 20})
        self.page.wait_for_selector("#batch-bar:not(.hidden)")
        self.page.click("#batch-all")
        self.assertEqual(self.page.locator("#batch-count").inner_text(), "2")
        with self.page.expect_download() as download_info:
            self.page.click("#batch-zip")
        self.assertTrue(download_info.value.suggested_filename.endswith(".zip"))

    def test_provider_key_never_reaches_the_page(self) -> None:
        self.open_app()
        self.page.wait_for_load_state("networkidle")
        self.assertNotIn(SECRET_KEY, self.page.content())
        self.assertNotIn(SECRET_KEY, self.page.evaluate("JSON.stringify(localStorage)"))


TOKEN_VALUE = "e2e-smoke-token-123"


@unittest.skipUnless(
    sync_playwright is not None,
    "未安装 playwright：pip install playwright && python -m playwright install chromium",
)
class TokenModeUITest(unittest.TestCase):
    """令牌模式首访：认证提示引导的「面板保存令牌」路径必须走通（v1.1.1 回归）。

    回归背景：init() 过去先 await loadMeta()，无令牌时 401 中断，令牌面板按钮
    的事件从未绑定——提示引导用户走的正是一条死路。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls._pw = sync_playwright().start()
            cls.browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:
            raise unittest.SkipTest(f"playwright 浏览器不可用：{exc}")
        # 类清理按 LIFO 执行：先注册 pw.stop（最后执行），browser.close 才能先跑
        cls.addClassCleanup(cls._pw.stop)
        cls.addClassCleanup(cls.browser.close)

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-ui-token-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmpdir, ignore_errors=True)
        profiles_path = cls.tmpdir / "profiles.json"
        profiles_path.write_text(
            json.dumps({
                "profiles": [{"name": "test", "base_url": "https://gateway.example/v1", "api_key": SECRET_KEY}],
                "active": "test",
            }),
            encoding="utf-8",
        )
        app = server_module.create_app(
            library_root=cls.tmpdir / "library", profiles_path=profiles_path, token=TOKEN_VALUE
        )
        cls.http = _UvicornThread(app)
        cls.http.__enter__()
        cls.addClassCleanup(cls.http.__exit__, None, None, None)

    def setUp(self) -> None:
        context = self.browser.new_context()
        context.on("dialog", lambda dialog: dialog.accept())
        self.addCleanup(context.close)
        self.page = context.new_page()

    def test_token_save_and_clear_roundtrip_on_first_visit(self) -> None:
        page = self.page
        page.goto(self.http.base_url)
        page.wait_for_load_state("networkidle")
        self.assertTrue(page.locator("#auth-hint").is_visible(), "无令牌应显示认证提示")
        page.locator("summary", has_text="网关配置").click()
        page.fill("#token-input", TOKEN_VALUE)
        page.click("#token-save")  # 保存并重载
        page.wait_for_load_state("networkidle")
        self.assertEqual(page.evaluate("localStorage.getItem('imagegen-token')"), TOKEN_VALUE)
        self.assertTrue(page.locator("#auth-hint").is_hidden(), "带令牌后认证提示应消失")
        self.assertIn("✓key", page.locator("#profile-select").inner_text())
        # 清除令牌：此时 init 已成功、按钮事件已绑定
        page.locator("summary", has_text="网关配置").click()
        page.click("#token-clear")
        page.wait_for_load_state("networkidle")
        self.assertIsNone(page.evaluate("localStorage.getItem('imagegen-token')"))

    def test_selfupdate_button_flow(self) -> None:
        """回归 v1.4.1：面板检测到 GitHub 新版 → 一键更新服务端（git/pip/重启打桩）。"""
        self._originals = {
            n: getattr(server_module, n)
            for n in ("fetch_latest_upstream", "git_run", "install_requirements", "perform_restart")
        }
        self.addCleanup(self._restore_patches)
        self.restarts: list[bool] = []

        async def fake_fetch(force: bool = False):
            return {"ok": True, "latest_upstream": "9.9.9"}

        def fake_git(args, timeout: float = 90.0):
            if args[0] == "rev-parse":
                return type("P", (), {"returncode": 0, "stdout": "true\n", "stderr": ""})()
            if args[0] == "status":
                return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if args[0] == "rev-list":
                return type("P", (), {"returncode": 0, "stdout": "3\n", "stderr": ""})()
            return type("P", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

        server_module.fetch_latest_upstream = fake_fetch
        server_module.git_run = fake_git
        server_module.install_requirements = lambda: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        server_module.perform_restart = lambda: self.restarts.append(True)

        page = self.page
        page.goto(f"{self.http.base_url}?token={TOKEN_VALUE}")  # 先登录，meta 才有 self_update_allowed
        page.wait_for_load_state("networkidle")
        page.click("#app-version")
        page.wait_for_selector("#update-panel:not(.hidden)")
        page.wait_for_selector("#update-selfupdate:not(.hidden)", timeout=8_000)
        self.assertIn("GitHub 最新 v9.9.9", page.locator("#update-upstream").inner_text())
        page.click("#update-selfupdate")  # confirm 对话框自动接受
        page.wait_for_selector('.toast:has-text("服务端更新中")', timeout=8_000)
        self.assertTrue(self.restarts, "应触发服务端重启")

    def _restore_patches(self) -> None:
        for name, fn in self._originals.items():
            setattr(server_module, name, fn)


class UpdatePromptUITest(unittest.TestCase):
    """版本轮询：服务端升级后，开着的页面弹「刷新」提示，点击后换到新版（v1.2.0 回归）。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls._pw = sync_playwright().start()
            cls.browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:
            raise unittest.SkipTest(f"playwright 浏览器不可用：{exc}")
        # 类清理按 LIFO 执行：先注册 pw.stop（最后执行），browser.close 才能先跑
        cls.addClassCleanup(cls._pw.stop)
        cls.addClassCleanup(cls.browser.close)

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-ui-update-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmpdir, ignore_errors=True)
        profiles_path = cls.tmpdir / "profiles.json"
        profiles_path.write_text(
            json.dumps({
                "profiles": [{"name": "test", "base_url": "https://gateway.example/v1", "api_key": SECRET_KEY}],
                "active": "test",
            }),
            encoding="utf-8",
        )
        cls._original_version = server_module.APP_VERSION
        cls.addClassCleanup(cls._restore_version)
        # 更新面板打开会触发上游检查：打桩离线，避免测试真实请求 GitHub
        async def _offline_fetch(force: bool = False):
            return {"ok": False, "latest_upstream": None}
        cls._original_fetch = server_module.fetch_latest_upstream
        server_module.fetch_latest_upstream = _offline_fetch
        cls.addClassCleanup(cls._restore_fetch)
        app = server_module.create_app(
            library_root=cls.tmpdir / "library", profiles_path=profiles_path
        )
        cls.http = _UvicornThread(app)
        cls.http.__enter__()
        cls.addClassCleanup(cls.http.__exit__, None, None, None)

    @classmethod
    def _restore_version(cls) -> None:
        server_module.APP_VERSION = cls._original_version

    @classmethod
    def _restore_fetch(cls) -> None:
        server_module.fetch_latest_upstream = cls._original_fetch

    def setUp(self) -> None:
        context = self.browser.new_context()
        self.addCleanup(context.close)
        self.page = context.new_page()

    def test_update_panel_flow(self) -> None:
        """回归 v1.3.0：版本徽章下拉面板——已是最新/外点关闭/橙点/刷新加载新版。"""
        page = self.page
        page.goto(self.http.base_url)
        page.wait_for_load_state("networkidle")
        # 同版本：面板显示已是最新，无刷新按钮
        page.click("#app-version")
        page.wait_for_selector("#update-panel:not(.hidden)")
        self.assertIn(
            f"v{server_module.APP_VERSION}", page.locator("#update-current").inner_text()
        )
        self.assertIn("已是最新", page.locator("#update-status").inner_text())
        self.assertTrue(page.locator("#update-status").is_visible())
        self.assertFalse(page.locator("#update-apply").is_visible())
        self.assertFalse(page.locator("#update-dot").is_visible())
        # 点面板外部 → 关闭
        page.mouse.click(640, 400)
        page.wait_for_timeout(250)
        self.assertIn("hidden", page.locator("#update-panel").get_attribute("class"))
        # 模拟服务端升级 → ↻ 手动检查 → 橙点 + 有新版提示 + 刷新按钮
        server_module.APP_VERSION = "9.9.9"
        try:
            page.click("#app-version")
            page.click("#update-check")
            page.wait_for_selector("#update-dot:not(.hidden)", timeout=8_000)
            self.assertIn("服务端有新版", page.locator("#update-status").inner_text())
            self.assertTrue(page.locator("#update-status").is_visible())
            self.assertIn("v9.9.9", page.locator("#update-latest").inner_text())
            self.assertTrue(page.locator("#update-apply").is_visible())
            # 刷新加载新版
            page.click("#update-apply")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("#app-version")
            self.assertEqual(page.locator("#app-version").inner_text(), "v9.9.9")
        finally:
            server_module.APP_VERSION = self._original_version

    def test_update_prompt_and_reload_picks_new_version(self) -> None:
        page = self.page
        page.goto(self.http.base_url)
        page.wait_for_load_state("networkidle")
        self.assertEqual(
            page.locator("#app-version").inner_text(), f"v{server_module.APP_VERSION}"
        )
        # 模拟服务端升级：运行中的服务把版本号换成 9.9.9
        server_module.APP_VERSION = "9.9.9"
        try:
            page.evaluate("window.dispatchEvent(new Event('focus'))")  # 触发一次立即检查
            page.wait_for_selector('.toast:has-text("服务端已更新")', timeout=8_000)
            page.locator(".toast-action", has_text="刷新").click()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("#app-version")
            self.assertEqual(page.locator("#app-version").inner_text(), "v9.9.9")
        finally:
            server_module.APP_VERSION = self._original_version


class UsersAdminUITest(unittest.TestCase):
    """管理员在页面上管理用户与令牌（users 模式，v1.4.0）。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls._pw = sync_playwright().start()
            cls.browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:
            raise unittest.SkipTest(f"playwright 浏览器不可用：{exc}")
        # 类清理按 LIFO 执行：先注册 pw.stop（最后执行），browser.close 才能先跑
        cls.addClassCleanup(cls._pw.stop)
        cls.addClassCleanup(cls.browser.close)

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="imagegen-ui-users-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmpdir, ignore_errors=True)
        profiles_path = cls.tmpdir / "profiles.json"
        profiles_path.write_text(
            json.dumps({
                "profiles": [{"name": "test", "base_url": "https://gateway.example/v1", "api_key": SECRET_KEY}],
                "active": "test",
            }),
            encoding="utf-8",
        )
        cls.users_file = cls.tmpdir / "users.json"
        cls.users_file.write_text(
            json.dumps({"users": [{"name": "alice", "token": "tok-alice-admin", "admin": True}]}),
            encoding="utf-8",
        )
        app = server_module.create_app(
            library_root=cls.tmpdir / "library",
            profiles_path=profiles_path,
            users_path=cls.users_file,
        )
        cls.http = _UvicornThread(app)
        cls.http.__enter__()
        cls.addClassCleanup(cls.http.__exit__, None, None, None)

    def setUp(self) -> None:
        context = self.browser.new_context()
        context.on("dialog", lambda dialog: dialog.accept())
        self.addCleanup(context.close)
        self.page = context.new_page()

    def test_admin_manages_users_in_page(self) -> None:
        page = self.page
        page.goto(f"{self.http.base_url}?token=tok-alice-admin")
        page.wait_for_load_state("networkidle")
        page.locator("summary", has_text="网关配置").click()
        admin_section = page.locator("#users-admin")
        page.wait_for_selector("#users-admin:not(.hidden)")
        # 添加用户 bob → 出现邀请链接，列表多一行
        page.fill("#user-name", "bob")
        page.click('#user-form button[type="submit"]')
        page.wait_for_selector("#user-result:not(.hidden)")
        link = page.locator(".user-result-link").inner_text()
        self.assertIn("/?token=", link)
        page.wait_for_selector('#users-list .user-row[data-name="bob"]')
        # 非 admin 用户看不到管理区
        other = self.browser.new_context().new_page()
        other.goto(f"{self.http.base_url}?token={link.split('token=')[1]}")
        other.wait_for_load_state("networkidle")
        other.locator("summary", has_text="网关配置").click()
        self.assertTrue(other.locator("#users-admin").get_attribute("class").find("hidden") >= 0)
        # bob 的令牌确实可用（meta 正常加载且非管理员）
        self.assertEqual(other.locator("#profile-select").inner_text().find("✓key") >= 0, True)
        other.close()
        # 移除 bob（confirm 自动接受）→ 行消失
        page.locator('#users-list .user-row[data-name="bob"]').locator('button[data-act="remove"]').click()
        page.wait_for_timeout(600)
        self.assertEqual(page.locator('#users-list .user-row[data-name="bob"]').count(), 0)
        # users 文件同步持久化
        persisted = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertEqual([u["name"] for u in persisted["users"]], ["alice"])


if __name__ == "__main__":
    unittest.main()
