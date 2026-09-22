#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 imagegen studio 加入 Windows 登录自启（启动文件夹快捷方式，免管理员权限）。

    python scripts/install_autostart.py            # 安装
    python scripts/install_autostart.py --remove   # 卸载
    python scripts/install_autostart.py --port 8642

在「启动」文件夹写一个 .lnk 指向 pythonw.exe（后台无窗口）。
非 Windows 平台请用 systemd/Docker（见 DEPLOY.md）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

LNK_NAME = "imagegen-studio.lnk"
LEGACY_VBS = "imagegen-studio.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("找不到 %APPDATA%，仅支持 Windows", file=sys.stderr)
        raise SystemExit(2)
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def main() -> int:
    parser = argparse.ArgumentParser(description="imagegen studio 登录自启")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--remove", action="store_true", help="移除自启")
    args = parser.parse_args()

    if os.name != "nt":
        print("非 Windows：请用 systemd/Docker 常驻（见 DEPLOY.md）", file=sys.stderr)
        return 2

    folder = startup_dir()
    lnk = folder / LNK_NAME
    legacy = folder / LEGACY_VBS

    if args.remove:
        removed = [p.name for p in (lnk, legacy) if p.is_file() and (p.unlink() or True)]
        print(f"已移除自启：{', '.join(removed)}" if removed else "自启未安装")
        return 0

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = Path(sys.executable)
    server = Path(__file__).resolve().parent / "imagegen_server.py"
    repo = server.parent.parent
    folder.mkdir(parents=True, exist_ok=True)

    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; "
        "$s.Arguments = '\"%s\" --port %d'; "
        "$s.WorkingDirectory = '%s'; "
        "$s.Description = 'imagegen studio local server'; "
        "$s.Save()"
    ) % (lnk, pythonw, server, args.port, repo)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0 or not lnk.is_file():
        print((result.stderr or result.stdout or "创建快捷方式失败").strip(), file=sys.stderr)
        return result.returncode or 1
    if legacy.is_file():
        legacy.unlink()

    print(f"已安装登录自启：{lnk}")
    print(f"  目标：{pythonw} \"{server}\" --port {args.port}（后台无窗口）")
    print("  下次登录自动启动；想立刻生效可双击 start.bat，或运行上面那条命令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())