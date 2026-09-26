#!/usr/bin/env python3
"""imagegen studio: local web workbench for the image-gen CLI (standalone repo).

FastAPI backend serving a single-page Chinese UI (web/) on 127.0.0.1.
Generation runs the existing image_gen.py CLI as a subprocess so the agent
channel, catalog, and sidecar ledger stay the single source of truth.

    python scripts/imagegen_server.py [--host H] [--port P] [--library DIR]
                                      [--profiles FILE] [--token TOKEN]

Set IMAGE_GEN_TOKEN to require an X-Auth-Token (or ?token=) on every request
before exposing the server beyond localhost. API keys live only in the
profiles file and are never returned to the frontend.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx
from pydantic import BaseModel, Field

def resolve_cli() -> Path:
    """Locate the image-gen CLI: IMAGE_GEN_CLI env, repo sibling, or installed skill."""
    env = os.environ.get("IMAGE_GEN_CLI")
    if env:
        return Path(env).expanduser().resolve()
    candidates = [
        Path(__file__).resolve().parents[2] / "image-gen" / "scripts" / "image_gen.py",
    ]
    for root in (
        Path.home() / ".agents" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".claude" / "skills",
    ):
        candidates.append(root / "image-gen" / "scripts" / "image_gen.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "未找到 image_gen.py：请先安装 image-gen skill，或用 IMAGE_GEN_CLI 指向其 scripts/image_gen.py"
    )
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SIDECAR_RECORD_TYPES = {"image-gen-sidecar", "image-generation-sidecar"}
DEFAULT_PORT = 8642
APP_VERSION = "1.4.3"


def default_library() -> Path:
    env = os.environ.get("IMAGE_GEN_LIBRARY")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Pictures" / "imagegen"


def default_profiles_path() -> Path:
    env = os.environ.get("IMAGE_GEN_PROFILES")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex" / "imagegen-profiles.json"


def default_users_path() -> Path:
    env = os.environ.get("IMAGE_GEN_USERS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex" / "imagegen-users.json"


def load_json_file(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


_CREDENTIAL_ENV_KEYS = ("IMAGE_GENERATION_API_KEY", "GPT_IMAGE_API_KEY", "OPENAI_API_KEY")


def windows_user_env() -> dict[str, str]:
    if os.name != "nt":
        return {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            return {n: str(winreg.QueryValueEx(key, n)[0]) for n in _CREDENTIAL_ENV_KEYS}
    except OSError:
        return {}


def has_gateway_credentials(profile: dict[str, Any] | None) -> bool:
    if profile and profile.get("api_key"):
        return True
    if any(os.environ.get(k) for k in _CREDENTIAL_ENV_KEYS):
        return True
    user_env = windows_user_env()
    return any(user_env.get(k) for k in _CREDENTIAL_ENV_KEYS)


def require_gateway_credentials(profile: dict[str, Any] | None) -> None:
    if has_gateway_credentials(profile):
        return
    raise HTTPException(
        400,
        "还没有配置任何网关凭据：请在上方栏添加一个 profile（名称 + API key + base URL），"
        "或设置环境变量 IMAGE_GENERATION_API_KEY / IMAGE_GENERATION_BASE_URL 后再生成。",
    )


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_profiles(profiles_path: Path) -> dict[str, Any]:
    if not profiles_path.is_file():
        return {"profiles": [], "active": None}
    try:
        data = json.loads(profiles_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"profiles": [], "active": None}
    if isinstance(data, list):
        return {"profiles": data, "active": data[0]["name"] if data else None}
    return data if isinstance(data, dict) else {"profiles": [], "active": None}


def save_profiles(profiles_path: Path, data: dict[str, Any]) -> None:
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = profiles_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(profiles_path)


def masked_profiles(profiles: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": p.get("name"),
            "base_url": p.get("base_url"),
            "has_key": bool(p.get("api_key")),
            "model": p.get("model"),
        }
        for p in profiles.get("profiles", [])
    ]


def active_profile(profiles: dict[str, Any]) -> dict[str, Any] | None:
    wanted = profiles.get("active")
    entries = profiles.get("profiles") or []
    for profile in entries:
        if profile.get("name") == wanted:
            return profile
    return entries[0] if entries else None


def load_catalog_models(base_url: str | None) -> list[dict[str, Any]]:
    catalog_path = Path.home() / ".codex" / "image-gen-model-catalog.json"
    if not catalog_path.is_file():
        return []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if base_url and catalog.get("base_url") and catalog["base_url"] != base_url:
        return []
    return catalog.get("choices", [])


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()[:48]).strip("-")
    return slug or "image"


class _ProcResult:
    """subprocess.run 的轻量替身：Popen + communicate 之后仍保留三个常用字段。"""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str | None, stderr: str | None) -> None:
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _with_brief(error: dict[str, Any] | None) -> dict[str, Any]:
    """给错误补一句人话短句（前端 toast 用它，完整内容留给「详情」）。"""
    if not isinstance(error, dict):
        return {"category": "unknown", "brief": "生成失败", "summary": str(error)}
    if not error.get("brief"):
        text = str(error.get("summary") or error.get("category") or "生成失败").strip()
        error["brief"] = (text.splitlines()[0] if text else "生成失败")[:80]
    return error


TRASH_DIRNAME = ".trash"
TRASH_KEEP_DAYS = 7
TRASH_STAMP_RE = re.compile(r"^(\d{8})-(\d{6})-")


def trash_deleted_at(entry: Path) -> float:
    """回收站条目的删除时刻：取自文件名的 <YYYYmmdd>-<HHMMSS>- 前缀。

    不能用 mtime——os.replace 会保留原文件的修改时间，那样「今天删除一张老图」
    会被立刻当成过期清掉，撤销窗口形同虚设。文件名对不上的（外来文件）退回 mtime。
    """
    match = TRASH_STAMP_RE.match(entry.name)
    if match:
        try:
            stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
            return stamp.timestamp()
        except ValueError:
            pass
    try:
        return entry.stat().st_mtime
    except OSError:
        return time.time()


def prune_trash(trash: Path, keep_days: int = TRASH_KEEP_DAYS) -> None:
    """回收站按「删除时间」保留最近 N 天，避免无限增长。"""
    if not trash.is_dir():
        return
    cutoff = time.time() - keep_days * 86400
    try:
        entries = list(trash.iterdir())
    except OSError:
        return
    for entry in entries:
        if trash_deleted_at(entry) < cutoff:
            try:
                entry.unlink()
            except OSError:
                continue


PARTIAL_IMAGE_RE = re.compile(r"^\d{8}-\d{6}-.*\.(png|jpe?g|webp)$", re.IGNORECASE)


def _cleanup_partials(cwd: Path, started: float) -> list[str]:
    """取消任务后清掉「本次任务新产生、但没有账本」的图片残片。

    被终止的 CLI 可能刚好写了一半图片（账本还没写），这种文件图库扫不到、
    却会一直躺在目录里。只删同时满足：图片后缀、命名符合 CLI 产物格式、
    没有同名 .json 账本、修改时间不早于任务开始——避免误删用户自己的文件。
    """
    removed: list[str] = []
    if not cwd.is_dir():
        return removed
    try:
        candidates = [p for p in cwd.rglob("*") if p.is_file() and PARTIAL_IMAGE_RE.match(p.name)]
    except OSError:
        return removed
    for path in candidates:
        if TRASH_DIRNAME in path.parts:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < started - 1:
            continue  # 任务开始前就存在的文件：不是本次残片
        if time.time() - mtime < 2:
            continue  # 太新：可能是另一个并发任务正在写入（账本还没落），别抢
        if Path(str(path) + ".json").is_file():
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            continue
    return removed


class JobManager:
    def __init__(self, max_concurrent: int = 2) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, Any] = {}  # job_id -> Popen（供取消时终止）
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max_concurrent)

    def create(
        self,
        args: list[str],
        child_env: dict[str, str],
        cwd: Path,
        annotate: dict[str, Any] | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "result": None,
            "error": None,
            "attempts": 1,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job_id, args, child_env, cwd, annotate), daemon=True
        )
        thread.start()
        return job_id

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """取消排队中或正在跑的任务：排队中的直接标记，运行中的终止子进程。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in ("done", "error", "cancelled"):
                return dict(job)
            job["status"] = "cancelled"
            job["error"] = {"category": "cancelled", "brief": "已取消", "summary": "任务已取消。"}
            proc = self._procs.get(job_id)
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 进程可能刚好退出
                pass
        return dict(job)

    def _run(
        self,
        job_id: str,
        args: list[str],
        child_env: dict[str, str],
        cwd: Path,
        annotate: dict[str, Any] | None = None,
    ) -> None:
        job = self._jobs[job_id]
        started = time.time()
        with self._slots:
            if job["status"] == "cancelled":
                return
            job["status"] = "running"
            gateway_retry_left = 1  # 网关偶发空响应：允许整个命令再跑一次
            while True:
                outcome = self._run_subprocess(job_id, args, child_env, cwd)
                if job["status"] == "cancelled":  # 取消后不再改写状态
                    job["error"]["removed_partials"] = _cleanup_partials(cwd, started)
                    job["elapsed"] = round(time.time() - started, 1)
                    return
                if outcome[0] == "error":
                    job["status"] = "error"
                    job["error"] = outcome[1]
                    job["elapsed"] = round(time.time() - started, 1)
                    return
                proc = outcome[1]
                if proc.returncode == 0:
                    error = self._collect_success(job, proc, annotate)
                else:
                    error = self._classify_failure(proc)
                if error is None:
                    job["elapsed"] = round(time.time() - started, 1)
                    return
                # 只有「可重试的瞬时故障」（网关/网络类）值得重跑；
                # 其余错误（参数、认证、CLI 用法）重跑没意义
                if error.get("retryable") is True and gateway_retry_left > 0:
                    gateway_retry_left -= 1
                    job["attempts"] = int(job.get("attempts") or 1) + 1
                    continue
                job["status"] = "error"
                job["error"] = error
                job["elapsed"] = round(time.time() - started, 1)
                return

    def _run_subprocess(
        self, job_id: str, args: list[str], child_env: dict[str, str], cwd: Path
    ) -> tuple[str, Any]:
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd),
                env=child_env,
            )
        except Exception as exc:  # noqa: BLE001 启动失败（命令行过长/CLI 缺失等）不得让任务悬挂
            return ("error", _with_brief({
                "category": "spawn",
                "brief": "生成进程启动失败",
                "summary": f"生成进程启动失败：{exc}",
            }))
        with self._lock:
            self._procs[job_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 收尾失败无所谓
                pass
            return ("error", _with_brief({
                "category": "timeout",
                "brief": "超时终止（超过 15 分钟）",
                "summary": "生成超过 15 分钟被终止。",
            }))
        finally:
            with self._lock:
                self._procs.pop(job_id, None)
        return ("proc", _ProcResult(proc.returncode, stdout, stderr))

    @staticmethod
    def _collect_success(
        job: dict[str, Any], proc: Any, annotate: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """成功时填充结果并回写账本；返回 None 表示任务完成。"""
        try:
            job["result"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _with_brief({
                "category": "parse",
                "brief": "生成结果解析失败",
                "summary": proc.stdout[-400:] or "空输出",
            })
        job["status"] = "done"
        if annotate:
            for sidecar_text in job["result"].get("sidecars", []):
                try:
                    sc_path = Path(sidecar_text)
                    record = json.loads(sc_path.read_text(encoding="utf-8"))
                    record.update(annotate)
                    save_json_file(sc_path, record)
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    # CLI 报错里出现这些痕迹 = 瞬时网络/网关故障（掐断连接、读响应中断、超时、空响应）
    TRANSIENT_MARKERS = (
        "JSONDecodeError", "Expecting value", "RemoteDisconnected", "ConnectionResetError",
        "ConnectionRefusedError", "IncompleteRead", "BadStatusLine", "timed out",
    )

    @staticmethod
    def _classify_failure(proc: Any) -> dict[str, Any]:
        tail = (proc.stderr or proc.stdout)[-400:]
        try:
            parsed = _with_brief(json.loads(proc.stderr))
            # CLI 自己的分类里带 retryable 标记的（network_error / gateway_timeout /
            # gateway_unparseable_response 等）都是瞬时故障
            if parsed.get("retryable"):
                parsed["category"] = "gateway"
                parsed["brief"] = "网关/网络瞬时故障（已自动重试仍失败）"
            return parsed
        except (json.JSONDecodeError, TypeError):
            if any(marker in tail for marker in JobManager.TRANSIENT_MARKERS):
                return _with_brief({
                    "category": "gateway",
                    "retryable": True,
                    "brief": "网关/网络瞬时故障（已自动重试仍失败）",
                    "summary": "网关或网络出现瞬时故障（连接被掐断/响应不完整），已自动重试仍失败，请稍后再试。\n" + tail,
                })
            return _with_brief({"category": "cli", "brief": "生成失败（CLI 报错）", "summary": tail})

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


def scan_history(library: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not library.is_dir():
        return records
    for sidecar_path in library.rglob("*.json"):
        if TRASH_DIRNAME in sidecar_path.parts:  # 回收站内容不出现在图库
            continue
        try:
            record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict) or record.get("record_type") not in SIDECAR_RECORD_TYPES:
            continue
        image_path = Path(str(sidecar_path)[:-5])
        if not image_path.is_file():
            continue
        created = record.get("created_at")
        if not created:
            created = datetime.fromtimestamp(image_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        try:
            stat = image_path.stat()
        except OSError:
            continue
        output_meta = record.get("output") if isinstance(record.get("output"), dict) else {}
        final_size = output_meta.get("final_size") or [None, None]
        records.append(
            {
                "image": str(image_path),
                "sidecar": str(sidecar_path),
                "model": record.get("model"),
                "prompt": record.get("prompt"),
                "created_at": created,
                "rating": int(record.get("rating") or 0),
                "bytes": stat.st_size,
                "width": final_size[0],
                "height": final_size[1],
                "parameters": record.get("parameters", {}),
                "choice": record.get("choice"),
                "project": record.get("project"),
                "record_type": record.get("record_type"),
            }
        )
    records.sort(key=lambda item: item["created_at"], reverse=True)
    return records


def build_generate_args(payload: dict[str, Any], library: Path) -> list[str]:
    now = datetime.now()
    dated_dir = library / now.strftime("%Y-%m")
    dated_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify(payload.get('prompt') or '')}.png"
    output = dated_dir / filename
    args = [
        sys.executable, str(resolve_cli()), "generate",
        "--prompt", payload["prompt"],
        "--output", str(output),
    ]
    if payload.get("choice") is not None and not payload.get("model"):
        args += ["--choice", str(payload["choice"])]
    if payload.get("preset"):
        args += ["--preset", payload["preset"]]
    if payload.get("size"):
        args += ["--size", payload["size"]]
    if payload.get("quality"):
        args += ["--quality", payload["quality"]]
    if payload.get("format"):
        args += ["--format", payload["format"]]
    if payload.get("aspect_ratio"):
        args += ["--aspect-ratio", payload["aspect_ratio"]]
    if payload.get("resolution"):
        args += ["--resolution", payload["resolution"]]
    if payload.get("model"):
        args += ["--model", payload["model"]]
    if (payload.get("n") or 1) > 1:
        args += ["--n", str(payload["n"])]
    return args


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    choice: int | None = Field(default=None, ge=1, le=10)
    model: str | None = None
    preset: str | None = None
    size: str | None = None
    quality: str | None = None
    format: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    profile: str | None = None
    project: str | None = None


class RateRequest(BaseModel):
    image: str
    rating: int = Field(ge=0, le=5)


class UserCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    library: str | None = None


class PathRequest(BaseModel):
    image: str


class ActivateRequest(BaseModel):
    name: str


class ProjectRequest(BaseModel):
    image: str
    project: str = Field(max_length=60)


class PathsRequest(BaseModel):
    images: list[str] = Field(min_length=1, max_length=200)


class RestoreRequest(BaseModel):
    moved: list[dict[str, str]] = Field(min_length=1, max_length=600)


# ---------- 上游版本检查：服务端代理查 GitHub tag（离线容错，结果缓存 1 小时） ----------

UPSTREAM_REPO = "xinghe-labs/imagegen-studio"
_upstream_cache: dict[str, Any] = {"at": 0.0, "data": None}


async def fetch_latest_upstream(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _upstream_cache["data"] is not None and now - _upstream_cache["at"] < 3600:
        return _upstream_cache["data"]
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "imagegen-studio"}) as client:
            resp = await client.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/tags?per_page=1")
            resp.raise_for_status()
            tags = resp.json()
        latest = str(tags[0]["name"]).lstrip("v") if tags else None
        data = {"ok": latest is not None, "latest_upstream": latest}
    except Exception as exc:  # noqa: BLE001 无外网/GitHub 不可达时静默降级
        data = {"ok": False, "latest_upstream": None, "detail": str(exc)[:160]}
    _upstream_cache["data"] = data
    _upstream_cache["at"] = now
    return data


REPO_ROOT = Path(__file__).resolve().parent.parent
BRANCH = "main"


def git_run(args: list[str], timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def install_requirements() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(REPO_ROOT / "requirements.txt")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )


def perform_restart() -> None:
    """原地替换进程：监听端口随 exec 释放后由新进程重新绑定。"""
    threading.Timer(1.5, lambda: os.execv(sys.executable, [sys.executable, *sys.argv])).start()


def create_app(
    library_root: Path | None = None,
    profiles_path: Path | None = None,
    token: str | None = None,
    users_path: Path | None = None,
) -> FastAPI:
    library = (library_root or default_library()).expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)
    profiles_file = (profiles_path or default_profiles_path()).expanduser().resolve()
    users_file = (users_path or default_users_path()).expanduser().resolve()
    # users 文件运行时可变（页面管理 / CLI 都会改），按 mtime 变化热加载
    users_state = {"mtime": None, "by_token": {}}

    def reload_users_if_changed() -> None:
        try:
            mtime = users_file.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != users_state["mtime"]:
            users = load_json_file(users_file, {"users": []}).get("users", [])
            users_state["by_token"] = {
                str(u["token"]): u
                for u in users
                if isinstance(u, dict) and u.get("token") and u.get("name")
            }
            users_state["mtime"] = mtime

    reload_users_if_changed()
    auth_token = token or os.environ.get("IMAGE_GEN_TOKEN") or ""
    jobs = JobManager(max_concurrent=2)
    app = FastAPI(title="imagegen studio", docs_url=None, redoc_url=None)

    def library_for_user(user: dict[str, Any]) -> Path:
        custom = user.get("library")
        base = Path(str(custom)).expanduser().resolve() if custom else library / str(user["name"])
        base.mkdir(parents=True, exist_ok=True)
        return base

    def req_library(request: Request) -> Path:
        return getattr(request.state, "library", None) or library

    # 认证失败限流：同一来源连续失败达阈值后短暂锁定（防 token 爆破）
    auth_failures: dict[str, list[float]] = {}
    AUTH_MAX_FAILURES = 10
    AUTH_WINDOW_SECONDS = 300.0

    def auth_locked(ip: str) -> bool:
        now = time.time()
        recent = [t for t in auth_failures.get(ip, []) if now - t < AUTH_WINDOW_SECONDS]
        if recent:
            auth_failures[ip] = recent
        else:
            auth_failures.pop(ip, None)
        return len(recent) >= AUTH_MAX_FAILURES

    def auth_failed(ip: str) -> None:
        auth_failures.setdefault(ip, []).append(time.time())

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        # 只保护 /api/*：静态页面（index/app.js/style.css）不含任何密钥，
        # 让页面永远能打开，令牌没配/配错时由界面提示并可当场修改——
        # 否则输错令牌重载后只会看到一页 401，UI 再也进不去（还得跟限流赛跑）。
        if (users_state["by_token"] or auth_token) and request.url.path.startswith("/api/"):
            ip = request.client.host if request.client else "?"
            provided = request.headers.get("X-Auth-Token") or request.query_params.get("token")
            if auth_locked(ip):
                return JSONResponse({"detail": "认证失败次数过多，请稍后再试"}, status_code=429)
            if users_state["by_token"]:
                reload_users_if_changed()
                user = users_state["by_token"].get(provided or "")
                if user is None:
                    auth_failed(ip)
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
                request.state.library = library_for_user(user)
                request.state.user = str(user["name"])
            elif provided != auth_token:
                auth_failed(ip)
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    def profile_for(payload_profile: str | None) -> dict[str, Any] | None:
        profiles = load_profiles(profiles_file)
        if payload_profile:
            for profile in profiles.get("profiles", []):
                if profile.get("name") == payload_profile:
                    return profile
            raise HTTPException(404, f"profile 不存在: {payload_profile}")
        return active_profile(profiles)

    no_store = {"Cache-Control": "no-cache"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers=no_store)

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript", headers=no_store)

    @app.get("/style.css")
    async def style_css() -> FileResponse:
        return FileResponse(WEB_DIR / "style.css", media_type="text/css", headers=no_store)

    @app.get("/api/meta")
    async def meta(request: Request) -> dict[str, Any]:
        profiles = load_profiles(profiles_file)
        profile = active_profile(profiles)
        base_url = profile.get("base_url") if profile else None
        users = load_json_file(users_file, {"users": []}).get("users", [])
        provided = request.headers.get("X-Auth-Token") or request.query_params.get("token") or ""
        me = next((u for u in users if str(u.get("token")) == provided), None)
        auth_mode = "users" if users else ("token" if auth_token else "open")
        is_admin = bool(me and me.get("admin"))
        return {
            "app": "imagegen studio",
            "version": APP_VERSION,
            "auth_mode": auth_mode,
            "is_admin": is_admin,
            "self_update_allowed": (auth_mode == "users" and is_admin) or auth_mode == "token",
            "library": str(req_library(request)),
            "user": getattr(request.state, "user", None),
            "presets": ["fast", "standard", "transparent"],
            "profiles": [
                {
                    "name": p.get("name"),
                    "base_url": p.get("base_url"),
                    "has_key": bool(p.get("api_key")),
                    "model": p.get("model"),
                }
                for p in profiles.get("profiles", [])
            ],
            "active": profile.get("name") if profile else None,
            "credentials": has_gateway_credentials(profile),
            "models": load_catalog_models(base_url),
        }

    @app.post("/api/generate")
    async def generate(request: Request, payload: GenerateRequest) -> dict[str, Any]:
        profile = profile_for(payload.profile)
        require_gateway_credentials(profile)
        child_env = os.environ.copy()
        if profile:
            if profile.get("api_key"):
                child_env["IMAGE_GENERATION_API_KEY"] = str(profile["api_key"])
            if profile.get("base_url"):
                child_env["IMAGE_GENERATION_BASE_URL"] = str(profile["base_url"])
        payload_dict = payload.model_dump(exclude={"profile"})
        annotate: dict[str, Any] = {}
        if payload_dict.get("project"):
            annotate["project"] = payload_dict["project"]
        lib = req_library(request)
        args = build_generate_args(payload_dict, lib)
        job_id = jobs.create(args, child_env, lib, annotate=annotate or None)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job 不存在")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, "job 不存在")
        return {"id": job_id, "status": job["status"]}


    @app.get("/api/history")
    async def history(
        request: Request,
        model: str | None = None,
        q: str | None = None,
        favorites: bool = False,
        project: str | None = None,
        since: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        records = scan_history(req_library(request))
        if model:
            records = [r for r in records if r["model"] == model]
        if q:
            needle = q.lower()
            records = [r for r in records if needle in (r["prompt"] or "").lower()]
        if project:
            records = [r for r in records if r.get("project") == project]
        if since:
            records = [r for r in records if (r["created_at"] or "") >= since]
        if favorites:
            records = [r for r in records if r["rating"] > 0]
        return {"total": len(records), "records": records[:limit]}

    def confine(image: str, lib: Path) -> Path:
        candidate = Path(image).expanduser().resolve()
        if not candidate.is_relative_to(lib):
            raise HTTPException(403, "路径不在图库内")
        return candidate

    @app.get("/api/image")
    async def image(request: Request, path: str, download: bool = False) -> FileResponse:
        image_path = confine(path, req_library(request))
        if not image_path.is_file():
            raise HTTPException(404, "图片不存在")
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        if download:
            return FileResponse(image_path, media_type=media.get(image_path.suffix.lower(), "application/octet-stream"), filename=image_path.name)
        return FileResponse(image_path, media_type=media.get(image_path.suffix.lower(), "application/octet-stream"))

    @app.post("/api/delete")
    async def delete_images(request: Request, payload: PathsRequest) -> dict[str, Any]:
        """删除 = 移入 <library>/.trash/（可撤销），并清理过期的回收站内容。"""
        lib = req_library(request)
        trash = lib / TRASH_DIRNAME
        trash.mkdir(parents=True, exist_ok=True)
        prune_trash(trash)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved: list[dict[str, str]] = []
        for index, item in enumerate(payload.images):
            image_path = confine(item, lib)
            if not image_path.is_file():
                continue
            for path in (image_path, Path(str(image_path) + ".json")):
                if path.is_file():
                    dest = trash / f"{stamp}-{index}-{path.name}"
                    try:
                        path.replace(dest)
                    except OSError:
                        continue
                    moved.append({"from": str(path), "to": str(dest)})
        if not moved:
            raise HTTPException(404, "没有可删除的图片")
        return {"deleted": [m["from"] for m in moved if not m["from"].endswith(".json")], "moved": moved, "count": len(moved)}

    @app.post("/api/restore")
    async def restore_images(request: Request, payload: RestoreRequest) -> dict[str, Any]:
        """撤销删除：把回收站里的文件按原路径放回。"""
        lib = req_library(request)
        restored: list[str] = []
        for pair in payload.moved:
            src = Path(pair.get("to", "")).expanduser().resolve()
            dest = Path(pair.get("from", "")).expanduser().resolve()
            # 只接受「确实来自本用户回收站」的来源，避免变成库内任意移动文件
            if not src.is_file() or TRASH_DIRNAME not in src.parts:
                continue
            if not src.is_relative_to(lib / TRASH_DIRNAME) or not dest.is_relative_to(lib):
                continue
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                src.replace(dest)
            except OSError:
                continue
            restored.append(str(dest))
        if not restored:
            raise HTTPException(404, "没有可恢复的文件")
        return {"restored": restored, "count": len(restored)}

    @app.post("/api/zip")
    async def zip_images(request: Request, payload: PathsRequest) -> Response:
        lib = req_library(request)
        buffer = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in payload.images:
                image_path = confine(item, lib)
                if image_path.is_file():
                    archive.write(image_path, arcname=image_path.name)
                    added += 1
        if not added:
            raise HTTPException(404, "没有可打包的图片")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="imagegen-{stamp}.zip"'},
        )

    @app.post("/api/rate")
    async def rate(request: Request, payload: RateRequest) -> dict[str, Any]:
        image_path = confine(payload.image, req_library(request))
        sidecar_path = Path(str(image_path) + ".json")
        if not sidecar_path.is_file():
            raise HTTPException(404, "找不到该图的 sidecar 记录")
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        record["rating"] = payload.rating
        save_json_file(sidecar_path, record)
        return {"image": str(image_path), "rating": payload.rating}

    @app.post("/api/open")
    async def open_folder(request: Request, payload: PathRequest) -> dict[str, str]:
        image_path = confine(payload.image, req_library(request))
        folder = image_path.parent
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"opened": str(folder)}

    @app.post("/api/profiles")
    async def upsert_profile(
        name: str = Form(...),
        base_url: str = Form(...),
        api_key: str = Form(""),
        model: str = Form(""),
    ) -> dict[str, Any]:
        name, base_url = name.strip(), base_url.strip().rstrip("/")
        if not name:
            raise HTTPException(422, "profile 名不能为空")
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "base_url 必须是 http(s) 地址")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        profiles = load_profiles(profiles_file)
        entries = profiles.setdefault("profiles", [])
        existing = next((p for p in entries if p.get("name") == name), None)
        if existing:
            if api_key:
                existing["api_key"] = api_key
            existing["base_url"] = base_url
            if model:
                existing["model"] = model
        else:
            entry: dict[str, Any] = {"name": name, "base_url": base_url}
            if api_key:
                entry["api_key"] = api_key
            if model:
                entry["model"] = model
            entries.append(entry)
            if not profiles.get("active"):
                profiles["active"] = name
        save_profiles(profiles_file, profiles)
        return {"saved": name, "profiles": masked_profiles(profiles)}

    @app.post("/api/profiles/activate")
    async def activate_profile(payload: ActivateRequest) -> dict[str, Any]:
        profiles = load_profiles(profiles_file)
        if payload.name not in [p.get("name") for p in profiles.get("profiles", [])]:
            raise HTTPException(404, f"profile 不存在: {payload.name}")
        profiles["active"] = payload.name
        save_profiles(profiles_file, profiles)
        return {"active": payload.name}

    @app.delete("/api/profiles/{name}")
    async def delete_profile(name: str) -> dict[str, Any]:
        profiles = load_profiles(profiles_file)
        entries = profiles.get("profiles", [])
        if name not in [p.get("name") for p in entries]:
            raise HTTPException(404, f"profile 不存在: {name}")
        profiles["profiles"] = [p for p in entries if p.get("name") != name]
        if profiles.get("active") == name:
            profiles["active"] = profiles["profiles"][0]["name"] if profiles["profiles"] else None
        save_profiles(profiles_file, profiles)
        return {"deleted": name, "profiles": masked_profiles(profiles)}

    # ---------- 用户与令牌管理（users 模式 + 管理员令牌） ----------

    USER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

    def users_admin_guard(request: Request) -> list[dict[str, Any]]:
        reload_users_if_changed()
        users = load_json_file(users_file, {"users": []}).get("users", [])
        if not users:
            raise HTTPException(403, "未启用多用户模式（users 文件为空）")
        provided = request.headers.get("X-Auth-Token") or request.query_params.get("token") or ""
        me = next((u for u in users if str(u.get("token")) == provided), None)
        if me is None:
            raise HTTPException(401, "unauthorized")
        if not me.get("admin"):
            raise HTTPException(403, "需要管理员令牌")
        return users

    def save_users_file(users: list[dict[str, Any]]) -> None:
        save_json_file(users_file, {"users": users})
        reload_users_if_changed()

    def invite_link(request: Request, token: str) -> str:
        host = request.headers.get("host") or request.url.netloc
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{scheme}://{host}/?token={token}"

    @app.get("/api/users")
    async def list_users(request: Request) -> dict[str, Any]:
        users = users_admin_guard(request)
        me = request.headers.get("X-Auth-Token") or request.query_params.get("token") or ""
        return {
            "users": [
                {
                    "name": u.get("name"),
                    "library": u.get("library"),
                    "admin": bool(u.get("admin")),
                    "token": str(u.get("token") or ""),
                    "is_me": str(u.get("token")) == me,
                }
                for u in users
            ]
        }

    @app.post("/api/users")
    async def create_user(request: Request, payload: UserCreateRequest) -> dict[str, Any]:
        users = users_admin_guard(request)
        if not USER_NAME_RE.fullmatch(payload.name):
            raise HTTPException(422, "用户名限 1-32 位字母/数字/下划线/连字符")
        if any(u.get("name") == payload.name for u in users):
            raise HTTPException(409, f"用户已存在: {payload.name}")
        token = secrets.token_urlsafe(24)
        entry: dict[str, Any] = {"name": payload.name, "token": token}
        if payload.library:
            entry["library"] = payload.library
        users.append(entry)
        save_users_file(users)
        return {"name": payload.name, "token": token, "link": invite_link(request, token)}

    @app.post("/api/users/{name}/rotate")
    async def rotate_user_token(name: str, request: Request) -> dict[str, Any]:
        users = users_admin_guard(request)
        target = next((u for u in users if u.get("name") == name), None)
        if target is None:
            raise HTTPException(404, f"用户不存在: {name}")
        token = secrets.token_urlsafe(24)
        target["token"] = token
        save_users_file(users)
        return {"name": name, "token": token, "link": invite_link(request, token)}

    @app.delete("/api/users/{name}")
    async def remove_user(name: str, request: Request) -> dict[str, Any]:
        users = users_admin_guard(request)
        provided = request.headers.get("X-Auth-Token") or request.query_params.get("token") or ""
        me = next((u for u in users if str(u.get("token")) == provided), None)
        if me and me.get("name") == name:
            raise HTTPException(400, "不能移除自己")
        remaining = [u for u in users if u.get("name") != name]
        if len(remaining) == len(users):
            raise HTTPException(404, f"用户不存在: {name}")
        save_users_file(remaining)
        return {"removed": name}

    # ---------- 服务端自助更新（git 部署；Docker 请用定时重建） ----------

    def self_update_guard(request: Request) -> None:
        users = load_json_file(users_file, {"users": []}).get("users", [])
        if users:
            users_admin_guard(request)  # 多用户模式：仅管理员
            return
        if not auth_token:
            raise HTTPException(403, "未启用认证：请先配置 IMAGE_GEN_TOKEN 或 users 再开放自助更新")
        provided = request.headers.get("X-Auth-Token") or request.query_params.get("token") or ""
        if provided != auth_token:
            raise HTTPException(401, "unauthorized")

    @app.get("/api/version-check")
    async def version_check(force: bool = False) -> dict[str, Any]:
        data = await fetch_latest_upstream(force)
        return {**data, "current": APP_VERSION}

    @app.post("/api/self-update")
    async def self_update(request: Request) -> dict[str, Any]:
        self_update_guard(request)
        if Path("/.dockerenv").exists():
            raise HTTPException(409, "Docker 部署不支持页内更新：请用定时任务 docker compose pull && up -d（见 DEPLOY.md §7）")
        check = git_run(["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0 or check.stdout.strip() != "true":
            raise HTTPException(409, "服务端不是 git 仓库，无法自助更新（参考 DEPLOY.md §7 的部署方式）")
        dirty = git_run(["status", "--porcelain"])
        if dirty.returncode != 0:
            raise HTTPException(409, f"git status 失败：{(dirty.stderr or '')[:160]}")
        if dirty.stdout.strip():
            raise HTTPException(409, "服务端代码有未提交的本地改动，为避免覆盖请先处理（git stash / commit）")
        fetch = git_run(["fetch", "origin", BRANCH])
        if fetch.returncode != 0:
            raise HTTPException(409, f"git fetch 失败：{(fetch.stderr or '')[:160]}")
        behind = git_run(["rev-list", "--count", f"HEAD..origin/{BRANCH}"])
        if behind.returncode != 0:
            raise HTTPException(409, f"git rev-list 失败：{(behind.stderr or '')[:160]}")
        if behind.stdout.strip() == "0":
            return {"updating": False, "reason": "服务端已是上游最新"}
        pull = git_run(["pull", "--ff-only", "origin", BRANCH])
        if pull.returncode != 0:
            raise HTTPException(409, f"git pull 失败：{(pull.stderr or pull.stdout or '')[:200]}")
        pip = install_requirements()
        if pip.returncode != 0:
            raise HTTPException(409, f"依赖安装失败（已停止更新，服务端仍运行旧版）：{(pip.stderr or pip.stdout or '')[:200]}")
        perform_restart()
        return {"updating": True, "detail": (pull.stdout or "").strip()[-200:]}

    @app.post("/api/edit")
    async def edit_images(
        request: Request,
        prompt: str = Form(...),
        profile: str = Form(""),
        model: str = Form(""),
        choice: str = Form(""),
        preset: str = Form(""),
        quality: str = Form(""),
        size: str = Form(""),
        output_format: str = Form(""),
        n: str = Form("1"),
        project: str = Form(""),
        image_paths: list[str] = Form([]),
        images: list[UploadFile] = File(default=[]),
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise HTTPException(422, "先写修改指令")
        if len(prompt) > 4000:
            raise HTTPException(422, "修改指令过长（上限 4000 字符）")
        refs: list[Path] = []
        lib = req_library(request)
        for path_text in image_paths:
            ref = confine(path_text, lib)
            if not ref.is_file():
                raise HTTPException(404, f"参考图不存在: {path_text}")
            refs.append(ref)
        if len(refs) + len(images) > 4:
            raise HTTPException(422, "参考图最多 4 张")
        refs_dir = lib / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for index, upload in enumerate(images):
            safe = (re.sub(r"[^A-Za-z0-9._-]+", "_", upload.filename or "")[-60:]) or f"ref{index}.png"
            dest = refs_dir / f"{stamp}-{index}-{safe}"
            dest.write_bytes(await upload.read())
            refs.append(dest)
        if not refs:
            raise HTTPException(422, "图生图至少需要一张参考图")

        now = datetime.now()
        out_dir = lib / now.strftime("%Y-%m")
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify(prompt)}.png"
        args = [sys.executable, str(resolve_cli()), "edit", "--prompt", prompt, "--output", str(output)]
        for ref in refs:
            args += ["--image", str(ref)]
        if choice and not model:
            args += ["--choice", choice]
        if model:
            args += ["--model", model]
        if preset:
            args += ["--preset", preset]
        if quality:
            if quality not in ("low", "medium", "high"):
                raise HTTPException(422, "质量仅支持 low / medium / high")
            args += ["--quality", quality]
        if size:
            args += ["--size", size]
        if output_format:
            if output_format not in ("png", "jpeg", "webp"):
                raise HTTPException(422, "格式仅支持 png / jpeg / webp")
            args += ["--format", output_format]
        if int(n) > 1:
            args += ["--n", n]

        profile_entry = profile_for(profile or None)
        require_gateway_credentials(profile_entry)
        child_env = os.environ.copy()
        if profile_entry:
            if profile_entry.get("api_key"):
                child_env["IMAGE_GENERATION_API_KEY"] = str(profile_entry["api_key"])
            if profile_entry.get("base_url"):
                child_env["IMAGE_GENERATION_BASE_URL"] = str(profile_entry["base_url"])
        job_id = jobs.create(args, child_env, lib, annotate={"project": project} if project.strip() else None)
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/project")
    async def set_project(request: Request, payload: ProjectRequest) -> dict[str, Any]:
        image_path = confine(payload.image, req_library(request))
        sidecar_path = Path(str(image_path) + ".json")
        if not sidecar_path.is_file():
            raise HTTPException(404, "找不到该图的 sidecar 记录")
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        record["project"] = payload.project.strip()
        save_json_file(sidecar_path, record)
        return {"image": str(image_path), "project": record["project"]}

    @app.get("/api/stats")
    async def stats(request: Request) -> dict[str, Any]:
        records = scan_history(req_library(request))
        month_prefix = datetime.now().astimezone().strftime("%Y-%m")
        by_model: dict[str, int] = {}
        by_project: dict[str, int] = {}
        total_bytes = 0
        month_count = 0
        favorites = 0
        for r in records:
            key = r["model"] or "unknown"
            by_model[key] = by_model.get(key, 0) + 1
            if r.get("project"):
                by_project[r["project"]] = by_project.get(r["project"], 0) + 1
            total_bytes += r["bytes"]
            if (r["created_at"] or "").startswith(month_prefix):
                month_count += 1
            if r["rating"]:
                favorites += 1
        return {
            "total": len(records),
            "this_month": month_count,
            "favorites": favorites,
            "bytes_total": total_bytes,
            "by_model": by_model,
            "by_project": by_project,
        }

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="imagegen studio 本地服务。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--library", help="图库根目录，默认 ~/Pictures/imagegen")
    parser.add_argument("--profiles", help="profiles 文件，默认 ~/.codex/imagegen-profiles.json")
    parser.add_argument("--token", help="访问令牌；也可用 IMAGE_GEN_TOKEN 环境变量")
    parser.add_argument("--users", help="多用户文件（每 token 一个用户/图库），默认 ~/.codex/imagegen-users.json")
    args = parser.parse_args()

    import uvicorn

    app = create_app(
        library_root=Path(args.library) if args.library else None,
        profiles_path=Path(args.profiles) if args.profiles else None,
        token=args.token,
        users_path=Path(args.users) if args.users else None,
    )
    library = (Path(args.library) if args.library else default_library()).expanduser()
    library.mkdir(parents=True, exist_ok=True)
    print(f"imagegen studio -> http://{args.host}:{args.port}  (图库: {library})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
