#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册图库每日备份计划任务（Windows: schtasks；其他平台打印 cron 行）。

    python scripts/install_backup_task.py --dest D:\\backups --days 14
    python scripts/install_backup_task.py --dest D:\\backups --time 04:15
    python scripts/install_backup_task.py --remove
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "imagegen-studio-backup"


def main() -> int:
    parser = argparse.ArgumentParser(description="注册图库每日备份任务")
    parser.add_argument("--dest", default="D:\\backups", help="备份目标目录")
    parser.add_argument("--days", type=int, default=14, help="保留最近 N 份")
    parser.add_argument("--time", default="03:30", help="每天执行时间 HH:MM")
    parser.add_argument("--remove", action="store_true", help="移除任务")
    args = parser.parse_args()

    script = Path(__file__).resolve().parent / "backup_library.py"

    if os.name != "nt":
        print("非 Windows：请加一条 cron（例如每天 03:30）：")
        print(f"  30 3 * * * {sys.executable} {script} --dest {args.dest} --days {args.days}")
        return 0

    if args.remove:
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], check=False)
        print(f"已移除任务：{TASK_NAME}")
        return 0

    command = f'"{sys.executable}" "{script}" --dest "{args.dest}" --days {args.days}'
    result = subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/sc", "daily", "/st", args.time,
         "/tr", command, "/f"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout.strip() or result.stderr.strip(), file=sys.stderr)
        print("提示：如果提示拒绝访问，请用「以管理员身份运行」的终端重试。", file=sys.stderr)
        return result.returncode
    print(f"已注册任务：{TASK_NAME}（每天 {args.time} → {args.dest}，保留最近 {args.days} 份）")
    print(f"  命令：{command}")
    print(f"  立即验证：schtasks /run /tn {TASK_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())