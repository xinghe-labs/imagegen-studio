#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多用户管理：加用户 / 列用户 / 删用户（users 文件见 README「多用户」）。

用法：
    python scripts/add_user.py add alice                # 生成随机 token 并写入
    python scripts/add_user.py add alice --library /data/library/alice
    python scripts/add_user.py list                     # 列出用户（token 打码）
    python scripts/add_user.py remove alice             # 删用户（不动图库文件）

默认文件 ~/.codex/imagegen-users.json（可用 IMAGE_GEN_USERS 或 --file 指定）。
加用户后把打印出的访问链接发给对方即可；base URL 用 --base-url 指定（默认按 host:port 拼）。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def default_users_path() -> Path:
    env = os.environ.get("IMAGE_GEN_USERS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex" / "imagegen-users.json"


def load_users(path: Path) -> dict:
    if not path.is_file():
        return {"users": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"读取失败 {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        print(f"{path} 结构不对：应为 {{\"users\": [...]}}", file=sys.stderr)
        raise SystemExit(2)
    return data


def save_users(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def mask(token: str) -> str:
    return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "…"


def cmd_add(args: argparse.Namespace, path: Path) -> int:
    data = load_users(path)
    users = data["users"]
    if any(u.get("name") == args.name for u in users):
        print(f"用户已存在: {args.name}（先 remove 再 add 可换 token）", file=sys.stderr)
        return 1
    token = args.token or secrets.token_urlsafe(24)
    entry: dict[str, Any] = {"name": args.name, "token": token}
    if args.admin:
        entry["admin"] = True
    if args.library:
        entry["library"] = args.library
    users.append(entry)
    save_users(path, data)
    base = args.base_url.rstrip("/") if args.base_url else "http://127.0.0.1:8642"
    print(f"已添加 {args.name}{'（管理员）' if args.admin else ''} → {path}")
    print(f"  访问链接: {base}/?token={token}")
    if args.library:
        print(f"  图库: {args.library}")
    else:
        print("  图库: <IMAGE_GEN_LIBRARY>/%s（未指定 library）" % args.name)
    return 0


def cmd_list(args: argparse.Namespace, path: Path) -> int:
    data = load_users(path)
    users = data["users"]
    if not users:
        print(f"{path} 里还没有用户")
        return 0
    for user in users:
        print(f"{user.get('name')}\ttoken={mask(str(user.get('token', '')))}\tlibrary={user.get('library') or '<默认>/' + str(user.get('name'))}")
    return 0


def cmd_invite(args: argparse.Namespace, path: Path) -> int:
    data = load_users(path)
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8))
    expires = (datetime.now().astimezone() + timedelta(hours=args.hours)).isoformat(timespec="seconds")
    data.setdefault("invites", []).append(
        {"code": code, "expires_at": expires, "max_uses": args.max_uses, "used": 0}
    )
    save_users(path, data)
    base = args.base_url.rstrip("/") if args.base_url else "http://127.0.0.1:8642"
    print(f"已生成邀请码: {code}")
    print(f"  有效期: {args.hours} 小时 / 限 {args.max_uses} 次（过期或用完自动失效）")
    print(f"  注册链接: {base}/?invite={code}")
    return 0


def cmd_remove(args: argparse.Namespace, path: Path) -> int:
    data = load_users(path)
    users = data["users"]
    remaining = [u for u in users if u.get("name") != args.name]
    if len(remaining) == len(users):
        print(f"用户不存在: {args.name}", file=sys.stderr)
        return 1
    data["users"] = remaining
    save_users(path, data)
    print(f"已移除 {args.name}（其图库文件未动）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="imagegen studio 多用户管理")
    parser.add_argument("--file", help="users 文件，默认 ~/.codex/imagegen-users.json 或 IMAGE_GEN_USERS")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="添加用户并生成 token")
    add.add_argument("name")
    add.add_argument("--library", help="该用户的图库目录（默认 <IMAGE_GEN_LIBRARY>/<name>）")
    add.add_argument("--token", help="指定 token（默认随机生成）")
    add.add_argument("--admin", action="store_true", help="设为管理员（可在页面管理用户与令牌）")
    add.add_argument("--base-url", help="打印访问链接用的地址，如 https://img.example.com")
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="列出用户")
    lst.set_defaults(func=cmd_list)

    inv = sub.add_parser("invite", help="生成邀请码（对方自助激活注册）")
    inv.add_argument("--max-uses", type=int, default=10, help="最多激活次数（默认 10）")
    inv.add_argument("--hours", type=int, default=168, help="有效小时数（默认 168 = 7 天）")
    inv.add_argument("--base-url", help="打印注册链接用的地址")
    inv.set_defaults(func=cmd_invite)

    rm = sub.add_parser("remove", help="移除用户")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    path = Path(args.file).expanduser() if args.file else default_users_path()
    return args.func(args, path)


if __name__ == "__main__":
    raise SystemExit(main())