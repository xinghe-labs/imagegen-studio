#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图库备份：把整个图库（图片 + sidecar 账本 + references）拷贝为一份带时间戳的快照。

用法：
    python scripts/backup_library.py --dest D:/backups            # 备份到 D:/backups/imagegen-backup-<时间戳>
    python scripts/backup_library.py --dest D:/backups --days 30  # 最多保留 30 份，旧快照自动清理

计划任务示例（每天 03:30，Windows）：
    schtasks /create /tn imagegen-backup /sc daily /st 03:30 ^
      /tr "python D:\\code\\imagegen-studio\\scripts\\backup_library.py --dest D:\\backups"

图库默认 ~/Pictures/imagegen，可用 IMAGE_GEN_LIBRARY 环境变量覆盖。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path


def default_library() -> Path:
    env = __import__("os").environ.get("IMAGE_GEN_LIBRARY")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Pictures" / "imagegen"


def prune_snapshots(dest: Path, keep: int) -> None:
    snapshots = sorted(dest.glob("imagegen-backup-*"))
    for old in snapshots[:-keep] if keep > 0 else snapshots:
        shutil.rmtree(old, ignore_errors=True)
        print(f"pruned {old.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="imagegen 图库备份")
    parser.add_argument("--dest", required=True, help="备份目标目录（快照会放在 <dest>/imagegen-backup-<时间戳>）")
    parser.add_argument("--days", type=int, default=14, help="保留最近 N 天 (每天一份，默认 14)")
    parser.add_argument("--source", default=None, help="图库目录（默认 ~/Pictures/imagegen 或 IMAGE_GEN_LIBRARY）")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve() if args.source else default_library().resolve()
    dest = Path(args.dest).expanduser().resolve()
    if not source.is_dir():
        print(f"图库目录不存在：{source}", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = dest / f"imagegen-backup-{stamp}"
    # 跳过回收站（.trash）：那是待清理的删除件，备份它只会让快照虚胖
    shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns(".trash"))
    prune_snapshots(dest, args.days)

    files = sum(1 for _ in snapshot.rglob("*") if _.is_file())
    size = sum(_.stat().st_size for _ in snapshot.rglob("*") if _.is_file())
    print(f"备份完成：{snapshot}（{files} 个文件，{size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())