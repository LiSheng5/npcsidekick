#!/usr/bin/env python
"""NPCSidekick — git 全量单文件备份（`git bundle`）。

## 为什么需要它

本机 `.git` 有过**整块损坏史**：`refs/` 目录消失 + `objects/pack/*.pack` 被删
（**只删 `.pack` 大文件、留下 `.idx`**；2026-09-17 发生，三个 `.git` 备份全是同一病征）。
`.idx` 单独存在**毫无用处** —— 恢复只能靠远端。所以留一份**本地**、**可核验**的全量备份。

## 用法

    python scripts/backup_git_bundle.py                  # 默认备份到 <仓库父目录>/_git_bundles
    python scripts/backup_git_bundle.py --dest D:/x      # 指定目录
    python scripts/backup_git_bundle.py --keep 5         # 保留最近 N 份（默认 3）

## 产物

| 文件 | 说明 |
|---|---|
| `npcsidekick_<时间戳>.bundle` | `git bundle create --all`（含全部分支）+ 随后自动 `git bundle verify` |
| `LATEST.txt` | **纯文本**清单：HEAD sha / 远端 sha / 分支 / 提交数 / 时间 |

⚠ `bundle` 是**大二进制**，理论上也可能被"清理大文件"的工具干掉 ——
所以同时写一份 **纯文本** `LATEST.txt` 当兜底线索（小文件，不易被盯上）。

## 退出码

`0` = 成功；非 `0` = 失败（可被计划任务 / CI 直接卡住）。**失败会打印明确原因**。

## 纪律

- 只读仓库、只在 `--dest` 下写文件；**不碰 `.git/`**（除了只读的 `git bundle`）。
- 路径一律用 `pathlib` 算成**绝对 Windows 路径**再传给 `git` ——
  本机 `git` **认不了 `/d/...` 这种 MSYS 路径**（2026-09-17 实测，踩过两次）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 仓库根 = 本脚本所在目录的上一级（scripts/ 在仓库根下）
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEEP = 3
BUNDLE_PREFIX = "npcsidekick_"
MANIFEST_NAME = "LATEST.txt"


def _git(*args: str) -> subprocess.CompletedProcess:
    """跑一条 git 命令（工作目录 = 仓库根，文本模式）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _out(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "").strip()


def _err(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or "").strip()


def default_dest() -> Path:
    """默认落点：仓库**父目录**下的 `_git_bundles`（放在仓库外面，少受仓库操作影响）。"""
    return REPO_ROOT.parent / "_git_bundles"


def collect_manifest(head: str, bundle_name: str) -> str:
    """生成纯文本清单（内容全是可核对的客观事实，不做任何推断）。"""
    branches = _out(_git("branch", "--format=%(refname:short)")) or "(无)"
    remote = _out(_git("rev-parse", "origin/master")) or "(取不到)"
    count = _out(_git("rev-list", "--count", "HEAD")) or "?"
    last = _out(_git("log", "-1", "--format=%h %ad %s", "--date=short")) or "(无)"
    lines = [
        "NPCSidekick — git 备份清单（纯文本兜底）",
        f"备份时间   : {datetime.now().isoformat(timespec='seconds')}",
        f"bundle 文件: {bundle_name}",
        f"HEAD       : {head}",
        f"origin/master: {remote}",
        f"提交总数   : {count}",
        f"最新提交   : {last}",
        "",
        "分支:",
        branches,
        "",
        "恢复方式:",
        f"  git clone {bundle_name} <新目录>",
        "（若只想补对象: git fetch <bundle 文件路径> 'refs/heads/*:refs/remotes/bundle/*'）",
        "",
    ]
    return "\n".join(lines)


def rotate(dest: Path, keep: int, keep_name: str) -> list[str]:
    """只保留最近 `keep` 份 bundle（按文件名里的时间戳排序），返回被删掉的名字。

    `keep_name` 是本次刚生成的文件名 —— 永不删它。
    """
    bundles = sorted(dest.glob(f"{BUNDLE_PREFIX}*.bundle"))
    removed: list[str] = []
    excess = len(bundles) - keep
    for old in bundles:
        if excess <= 0:
            break
        if old.name == keep_name:
            continue
        try:
            old.unlink()
            removed.append(old.name)
            excess -= 1
        except OSError as exc:                       # 删不掉就跳过，不影响本次备份
            print(f"[warn] 轮转删除失败（已跳过）: {old.name} — {exc}")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NPCSidekick git 全量单文件备份（git bundle）")
    ap.add_argument("--dest", default="", help="备份目录（默认 <仓库父目录>/_git_bundles）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"保留最近 N 份（默认 {DEFAULT_KEEP}；<=0 表示不轮转）")
    args = ap.parse_args(argv)

    # ① 确认这是个可用仓库（.git 损坏时这里就会拦下，给出人话）
    head = _out(_git("rev-parse", "HEAD"))
    if not head:
        print("[FAIL] 取不到 HEAD —— 仓库可能已损坏（本机有过此病史）。", file=sys.stderr)
        print("       排查: git show-ref / 看 .git/refs 与 .git/objects/pack/*.pack 是否还在。",
              file=sys.stderr)
        print(f"       git 说: {_err(_git('rev-parse', 'HEAD'))}", file=sys.stderr)
        return 2

    dest = Path(args.dest).expanduser().resolve() if args.dest else default_dest()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[FAIL] 备份目录建不出来: {dest} — {exc}", file=sys.stderr)
        return 3

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{BUNDLE_PREFIX}{stamp}.bundle"
    final = dest / name
    partial = dest / f"{name}.partial"        # 先写临时名，verify 过了再改名（防半截文件冒充好备份）

    # ② 打包全部分支 + 标签
    proc = _git("bundle", "create", str(partial), "--all")
    if proc.returncode != 0:
        print(f"[FAIL] git bundle create 失败:\n{_err(proc)}", file=sys.stderr)
        partial.unlink(missing_ok=True)
        return 4

    # ③ 立刻核验 —— 没验过的备份不算备份
    verify = _git("bundle", "verify", str(partial))
    if verify.returncode != 0:
        print(f"[FAIL] bundle 自校验不通过，已丢弃:\n{_err(verify)}", file=sys.stderr)
        partial.unlink(missing_ok=True)
        return 5

    try:
        shutil.move(str(partial), str(final))
    except OSError as exc:
        print(f"[FAIL] 改名失败: {exc}", file=sys.stderr)
        return 6

    # ④ 纯文本兜底清单
    try:
        (dest / MANIFEST_NAME).write_text(
            collect_manifest(head, name), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] 清单写入失败（bundle 本身是好的）: {exc}")

    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"[OK] {final}")
    print(f"     HEAD={head[:12]}  大小={size_mb:.1f}MB  校验=通过")

    # ⑤ 轮转
    if args.keep > 0:
        for old in rotate(dest, args.keep, name):
            print(f"     [rotate] 移除旧备份 {old}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
