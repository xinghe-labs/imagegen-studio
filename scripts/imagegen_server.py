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
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
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


def default_characters_path() -> Path:
    env = os.environ.get("IMAGE_GEN_CHARACTERS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex" / "imagegen-characters.json"


def load_json_file(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


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
    for profile in profiles.get("profiles", []):
        if profile.get("name") == wanted:
            return profile
    return profiles.get("profiles", [None])[0]


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


class JobManager:
    def __init__(self, max_concurrent: int = 2) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
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
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job_id, args, child_env, cwd, annotate), daemon=True
        )
        thread.start()
        return job_id

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
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=900,
                    cwd=str(cwd),
                    env=child_env,
                )
            except subprocess.TimeoutExpired:
                job["status"] = "error"
                job["error"] = {"category": "timeout", "summary": "生成超过 15 分钟被终止。"}
                return
            finally:
                job["elapsed"] = round(time.time() - started, 1)
        if proc.returncode == 0:
            try:
                job["result"] = json.loads(proc.stdout)
                job["status"] = "done"
                if annotate:
                    for sidecar_text in job["result"].get("sidecars", []):
                        try:
                            sc_path = Path(sidecar_text)
                            record = json.loads(sc_path.read_text(encoding="utf-8"))
                            record.update(annotate)
                            sc_path.write_text(
                                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                        except (json.JSONDecodeError, OSError):
                            continue
            except json.JSONDecodeError:
                job["status"] = "error"
                job["error"] = {"category": "parse", "summary": proc.stdout[-400:] or "空输出"}
        else:
            try:
                job["error"] = json.loads(proc.stderr)
            except json.JSONDecodeError:
                job["error"] = {"category": "unknown", "summary": (proc.stderr or proc.stdout)[-400:]}
            job["status"] = "error"

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


def scan_history(library: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not library.is_dir():
        return records
    for sidecar_path in library.rglob("*.json"):
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
        records.append(
            {
                "image": str(image_path),
                "sidecar": str(sidecar_path),
                "model": record.get("model"),
                "prompt": record.get("prompt"),
                "created_at": created,
                "rating": int(record.get("rating") or 0),
                "bytes": stat.st_size,
                "parameters": record.get("parameters", {}),
                "choice": record.get("choice"),
                "project": record.get("project"),
                "character": record.get("character"),
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
    if payload.get("choice") is not None:
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


def build_reproduce_command(record: dict[str, Any]) -> str:
    parts = ["python scripts/image_gen.py generate"]
    prompt = (record.get("prompt") or "").replace('"', '\\"')
    parts.append(f'--prompt "{prompt}"')
    if record.get("model"):
        parts.append(f"--model {record['model']}")
    params = record.get("parameters") or {}
    for key, flag in (
        ("preset", "--preset"), ("size", "--size"), ("quality", "--quality"),
        ("output_format", "--format"), ("aspect_ratio", "--aspect-ratio"),
        ("resolution", "--resolution"),
    ):
        if params.get(key):
            parts.append(f"{flag} {params[key]}")
    return " ".join(parts)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    choice: int | None = None
    model: str | None = None
    preset: str | None = None
    size: str | None = None
    quality: str | None = None
    format: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    profile: str | None = None
    character: str | None = None
    project: str | None = None


class RateRequest(BaseModel):
    image: str
    rating: int = Field(ge=0, le=5)


class PathRequest(BaseModel):
    image: str


class ActivateRequest(BaseModel):
    name: str


class CharacterRequest(BaseModel):
    name: str
    from_image: str | None = None
    identity_block: str | None = None


class ProjectRequest(BaseModel):
    image: str
    project: str = Field(max_length=60)


def create_app(
    library_root: Path | None = None,
    profiles_path: Path | None = None,
    token: str | None = None,
    characters_path: Path | None = None,
) -> FastAPI:
    library = (library_root or default_library()).expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)
    profiles_file = (profiles_path or default_profiles_path()).expanduser().resolve()
    auth_token = token or os.environ.get("IMAGE_GEN_TOKEN") or ""
    jobs = JobManager(max_concurrent=2)
    app = FastAPI(title="imagegen studio", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if auth_token:
            provided = request.headers.get("X-Auth-Token") or request.query_params.get("token")
            if provided != auth_token:
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

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def style_css() -> FileResponse:
        return FileResponse(WEB_DIR / "style.css", media_type="text/css")

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        profiles = load_profiles(profiles_file)
        profile = active_profile(profiles)
        base_url = profile.get("base_url") if profile else None
        return {
            "app": "imagegen studio",
            "library": str(library),
            "presets": ["fast", "standard", "quality", "final", "square-2k",
                        "landscape-2k", "portrait-2k", "landscape-4k", "portrait-4k", "transparent"],
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
            "models": load_catalog_models(base_url),
        }

    @app.post("/api/generate")
    async def generate(payload: GenerateRequest) -> dict[str, Any]:
        profile = profile_for(payload.profile)
        child_env = os.environ.copy()
        if profile:
            if profile.get("api_key"):
                child_env["IMAGE_GENERATION_API_KEY"] = str(profile["api_key"])
            if profile.get("base_url"):
                child_env["IMAGE_GENERATION_BASE_URL"] = str(profile["base_url"])
        payload_dict = payload.model_dump(exclude={"profile"})
        annotate: dict[str, Any] = {}
        if payload_dict.get("character"):
            characters = load_json_file(characters_file, {"characters": []})
            entry = next(
                (c for c in characters.get("characters", []) if c.get("name") == payload_dict["character"]),
                None,
            )
            if entry is None:
                raise HTTPException(404, f"角色不存在: {payload_dict['character']}")
            identity = (entry.get("identity_block") or "").strip()
            if identity:
                payload_dict["prompt"] = f"{identity}, {payload_dict['prompt']}"
            annotate["character"] = payload_dict["character"]
        if payload_dict.get("project"):
            annotate["project"] = payload_dict["project"]
        args = build_generate_args(payload_dict, library)
        job_id = jobs.create(args, child_env, library, annotate=annotate or None)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job 不存在")
        return job

    characters_file = (characters_path or default_characters_path()).expanduser().resolve()

    @app.get("/api/history")
    async def history(
        model: str | None = None,
        q: str | None = None,
        favorites: bool = False,
        project: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        records = scan_history(library)
        if model:
            records = [r for r in records if r["model"] == model]
        if q:
            needle = q.lower()
            records = [r for r in records if needle in (r["prompt"] or "").lower()]
        if project:
            records = [r for r in records if r.get("project") == project]
        if favorites:
            records = [r for r in records if r["rating"] > 0]
        return {"total": len(records), "records": records[:limit]}

    def confine(image: str) -> Path:
        candidate = Path(image).expanduser().resolve()
        if not candidate.is_relative_to(library):
            raise HTTPException(403, "路径不在图库内")
        return candidate

    @app.get("/api/image")
    async def image(path: str) -> FileResponse:
        image_path = confine(path)
        if not image_path.is_file():
            raise HTTPException(404, "图片不存在")
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        return FileResponse(image_path, media_type=media.get(image_path.suffix.lower(), "application/octet-stream"))

    @app.post("/api/rate")
    async def rate(payload: RateRequest) -> dict[str, Any]:
        image_path = confine(payload.image)
        sidecar_path = Path(str(image_path) + ".json")
        if not sidecar_path.is_file():
            raise HTTPException(404, "找不到该图的 sidecar 记录")
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        record["rating"] = payload.rating
        sidecar_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"image": str(image_path), "rating": payload.rating}

    @app.post("/api/reproduce")
    async def reproduce(payload: PathRequest) -> dict[str, str]:
        image_path = confine(payload.image)
        sidecar_path = Path(str(image_path) + ".json")
        if not sidecar_path.is_file():
            raise HTTPException(404, "找不到 sidecar")
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return {"command": build_reproduce_command(record)}

    @app.post("/api/open")
    async def open_folder(payload: PathRequest) -> dict[str, str]:
        image_path = confine(payload.image)
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
        if not (base_url.startswith(("http://", "https://")) and base_url.endswith("/v1")):
            raise HTTPException(422, "base_url 必须是 http(s) 且以 /v1 结尾")
        profiles = load_profiles(profiles_file)
        entries = profiles.setdefault("profiles", [])
        existing = next((p for p in entries if p.get("name") == name), None)
        if existing:
            if api_key:
                existing["api_key"] = api_key
            existing["base_url"] = base_url + "/v1" if not base_url.endswith("/v1") else base_url
            if model:
                existing["model"] = model
        else:
            entry: dict[str, Any] = {"name": name, "base_url": base_url if base_url.endswith("/v1") else base_url + "/v1"}
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

    @app.post("/api/edit")
    async def edit_images(
        prompt: str = Form(...),
        profile: str = Form(""),
        model: str = Form(""),
        choice: str = Form(""),
        preset: str = Form(""),
        n: str = Form("1"),
        project: str = Form(""),
        image_paths: list[str] = Form([]),
        images: list[UploadFile] = File(default=[]),
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise HTTPException(422, "先写修改指令")
        refs: list[Path] = []
        for path_text in image_paths:
            ref = confine(path_text)
            if not ref.is_file():
                raise HTTPException(404, f"参考图不存在: {path_text}")
            refs.append(ref)
        if len(refs) + len(images) > 4:
            raise HTTPException(422, "参考图最多 4 张")
        refs_dir = library / "references"
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
        out_dir = library / now.strftime("%Y-%m")
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify(prompt)}.png"
        args = [sys.executable, str(resolve_cli()), "edit", "--prompt", prompt, "--output", str(output)]
        for ref in refs:
            args += ["--image", str(ref)]
        if choice:
            args += ["--choice", choice]
        if model:
            args += ["--model", model]
        if preset:
            args += ["--preset", preset]
        if int(n) > 1:
            args += ["--n", n]

        profile_entry = profile_for(profile or None)
        child_env = os.environ.copy()
        if profile_entry:
            if profile_entry.get("api_key"):
                child_env["IMAGE_GENERATION_API_KEY"] = str(profile_entry["api_key"])
            if profile_entry.get("base_url"):
                child_env["IMAGE_GENERATION_BASE_URL"] = str(profile_entry["base_url"])
        job_id = jobs.create(args, child_env, library, annotate={"project": project} if project.strip() else None)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/characters")
    async def list_characters() -> dict[str, Any]:
        data = load_json_file(characters_file, {"characters": []})
        entries = []
        for c in data.get("characters", []):
            entries.append(
                {
                    "name": c.get("name"),
                    "identity_block": c.get("identity_block"),
                    "reference_image": c.get("reference_image"),
                    "has_reference": bool(c.get("reference_image") and Path(c["reference_image"]).is_file()),
                    "created_at": c.get("created_at"),
                }
            )
        return {"characters": entries}

    @app.post("/api/characters")
    async def upsert_character(payload: CharacterRequest) -> dict[str, Any]:
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "角色名不能为空")
        identity = (payload.identity_block or "").strip()
        reference: str | None = None
        if payload.from_image:
            image_path = confine(payload.from_image)
            sidecar_path = Path(str(image_path) + ".json")
            if not sidecar_path.is_file():
                raise HTTPException(404, "找不到该图的 sidecar，无法提取身份块")
            record = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if not identity:
                identity = (record.get("prompt") or "").strip()
            reference = str(image_path)
        if not identity:
            raise HTTPException(422, "身份块为空：填 identity_block 或提供 from_image 提取")
        characters = load_json_file(characters_file, {"characters": []})
        entries = characters.setdefault("characters", [])
        existing = next((c for c in entries if c.get("name") == name), None)
        if existing:
            existing["identity_block"] = identity
            if reference:
                existing["reference_image"] = reference
        else:
            entry: dict[str, Any] = {
                "name": name,
                "identity_block": identity,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            if reference:
                entry["reference_image"] = reference
            entries.append(entry)
        save_json_file(characters_file, characters)
        return {"saved": name}

    @app.delete("/api/characters/{name}")
    async def delete_character(name: str) -> dict[str, Any]:
        characters = load_json_file(characters_file, {"characters": []})
        entries = characters.get("characters", [])
        if name not in [c.get("name") for c in entries]:
            raise HTTPException(404, f"角色不存在: {name}")
        characters["characters"] = [c for c in entries if c.get("name") != name]
        save_json_file(characters_file, characters)
        return {"deleted": name}

    @app.post("/api/project")
    async def set_project(payload: ProjectRequest) -> dict[str, Any]:
        image_path = confine(payload.image)
        sidecar_path = Path(str(image_path) + ".json")
        if not sidecar_path.is_file():
            raise HTTPException(404, "找不到该图的 sidecar 记录")
        record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        record["project"] = payload.project.strip()
        sidecar_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"image": str(image_path), "project": record["project"]}

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        records = scan_history(library)
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
    args = parser.parse_args()

    import uvicorn

    app = create_app(
        library_root=Path(args.library) if args.library else None,
        profiles_path=Path(args.profiles) if args.profiles else None,
        token=args.token,
    )
    library = (Path(args.library) if args.library else default_library()).expanduser()
    library.mkdir(parents=True, exist_ok=True)
    print(f"imagegen studio -> http://{args.host}:{args.port}  (图库: {library})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
