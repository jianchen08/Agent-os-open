#!/usr/bin/env python
"""!.env 完整性守卫（D4）：启动前校验 .env 健康，可自愈时自动从备份恢复。

背景（2026-09-13 事故）：.env 被误写成 5 字节"K=v"，OLLAMA-OLINE_API_KEY 丢失；
内核重启后 sidecar 重读被毁 .env，所有 LLM 调用 0 token 空返回，全部任务
20 秒内假绿终局（批次损失约 6000 万 token）。本守卫在每次内核启动前：

1. 解析 .env（KEY=VALUE）；解析失败 = 存在性损坏，--fix 模式直接用备份整体还原。
2. 从 config/models/*.yaml 提取全部 ``${VAR}`` 占位符作为"配置声明的键"集合。
3. 快照：.env 健康时把其中的键合并进 .env.backup（只增不删，跨事故存活）。
4. 自愈（--fix）：备份里有而 .env 缺的键 → 回写 .env 并告警。
5. 退出码：0 = 健康或已自愈；1 = .env 存在但不可解析且无可用药备份（启动器
   据此阻断）。全新环境（无 .env 无备份）只提示不阻断——首配是合法状态。

用法：
    python scripts/check_env_health.py            # 只检查
    python scripts/check_env_health.py --fix      # 检查 + 快照 + 自愈（启动器用）
    python scripts/check_env_health.py --env PATH # 指定 env 文件（默认仓库根 .env）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKUP_NAME = ".env.backup"
MIN_HEALTHY_ENTRIES = 2  # 少于 2 个键视为可疑（事故现场是 1 个假键 K=v）


def _parse_env(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            entries[key] = value.strip()
    return entries


def _declared_placeholders(config_glob: str) -> set[str]:
    """config/models/*.yaml 里声明的 ${VAR} 占位符集合（api_key 类真值来源）。"""
    import re

    declared: set[str] = set()
    for cfg in sorted(REPO.glob(config_glob)):
        text = cfg.read_text(encoding="utf-8", errors="replace")
        declared.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text))
    return declared


def _write_env(path: Path, entries: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in entries.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=".env 完整性守卫")
    parser.add_argument("--fix", action="store_true", help="快照健康键到备份，并从备份自愈缺失键")
    parser.add_argument("--env", default=str(REPO / ".env"), help="env 文件路径")
    parser.add_argument("--config-glob", default="config/models/*.yaml", help="占位符声明来源 glob")
    args = parser.parse_args()

    env_path = Path(args.env)
    backup_path = env_path.parent / BACKUP_NAME
    backup_entries = _parse_env(backup_path.read_text(encoding="utf-8")) if backup_path.exists() else {}

    if not env_path.exists():
        print(f"[env-guard] {env_path} 不存在：全新环境合法状态，跳过（建议配置后重跑以建立备份）")
        return 0

    raw = env_path.read_text(encoding="utf-8", errors="replace")
    entries = _parse_env(raw)
    if not entries:
        # 存在但解析不出任何键：存在性损坏。
        if args.fix and backup_entries:
            _write_env(env_path, backup_entries)
            print(f"[env-guard][FIXED] .env 不可解析，已用备份整体还原（{len(backup_entries)} 键）")
            return 0
        print("[env-guard][ERROR] .env 不可解析且无可用药备份——拒绝以此启动（所有 LLM 调用将空返回）")
        return 1

    declared = _declared_placeholders(args.config_glob)
    missing_declared = sorted(k for k in declared if k not in entries)
    restored = 0
    if args.fix and backup_entries:
        missing_from_backup = sorted(k for k in backup_entries if k not in entries)
        if missing_from_backup:
            merged = dict(entries)
            for key in missing_from_backup:
                merged[key] = backup_entries[key]
            _write_env(env_path, merged)
            entries = merged
            restored = len(missing_from_backup)
            print(f"[env-guard][FIXED] 自愈恢复缺失键：{', '.join(missing_from_backup)}")

    # 快照：.env 健康且与备份有差异时合并（只增不删——备份跨事故存活）
    backup_size = len(backup_entries)
    if args.fix:
        new_in_env = {k: v for k, v in entries.items() if backup_entries.get(k) != v}
        if new_in_env:
            merged_backup = dict(backup_entries)
            merged_backup.update(new_in_env)
            _write_env(backup_path, merged_backup)
            backup_size = len(merged_backup)
            print(f"[env-guard] 备份快照已更新（{backup_size} 键）")

    if missing_declared and restored == 0:
        # 配置声明了但两边都没有的键：合法（未用的 provider），提示不阻断；
        # 但若核心 LLM 键缺失会由运行时快速失败兜底，此处只做可见性。
        preview = ", ".join(missing_declared[:6]) + ("…" if len(missing_declared) > 6 else "")
        print(f"[env-guard] 配置声明但未配置的键（如未用对应 provider 可忽略）: {preview}")

    if entries and len(entries) < MIN_HEALTHY_ENTRIES and not backup_entries:
        print(f"[env-guard][WARN] .env 仅 {len(entries)} 键且无备份——若属误写请从备份/凭据源恢复后重跑")

    state = "已自愈" if restored else "健康"
    print(f"[env-guard] .env {state}（{len(entries)} 键，备份 {backup_size} 键）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
