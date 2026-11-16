"""acp_quota_pool 技能载荷播种 —— on_load 镜像面（裸名 acp_seed 独名防碰撞）。

播种链实证（2026-10-15，照实记台账 §七）：技能到达会话工作空间的机制只有
三类源——仓根 skills/ ∪ mode_* 包 skills/ ∪ 用户根 skills/
（workspace_lifecycle._skill_sources；内核零技能概念）。本插件目录不在源内
且 mode_* 前缀被模式包体系占用——故 on_load 把载荷镜像到
``<USER_ROOT>/skills/acp-quota-pool/``（装机形态根技能权威源，用户根 skills/
源接链进工作空间），镜像为真值同步（覆盖写，插件载荷 = 唯一书写处）。

播种失败显式降级为告警不阻断加载：技能实体仍在插件目录内（调用方可直接
引用插件内路径），播种只是可达性增强——这是明示的降级策略，非吞异常。
"""

from __future__ import annotations

import shutil
from pathlib import Path

SKILL_ID = "acp-quota-pool"
PAYLOAD_FILES = (
    "SKILL.md",
    "scripts/providers.toml",
    "scripts/acp_pool.py",
    "scripts/acp_registry.py",
    "scripts/acp_ledger.py",
    "scripts/acp_engine.py",
)

__all__ = ["PAYLOAD_FILES", "SKILL_ID", "seed_skill_into", "seed_with_degradation"]


def seed_skill_into(user_root: Path, plugin_dir: Path) -> list[str]:
    """把技能载荷镜像到 ``<user_root>/skills/<SKILL_ID>/``（覆盖写，真值同步）。

    Returns:
        实际写入的相对路径清单（对调用方可观察；测试断言行级）。

    Raises:
        OSError: 载荷缺失或写失败（fail-closed；调用方裁决降级语义）。
    """
    target_dir = user_root / "skills" / SKILL_ID
    written: list[str] = []
    for rel in PAYLOAD_FILES:
        source = plugin_dir / rel
        if not source.is_file():
            raise OSError(f"技能载荷缺失: {source}")
        target = target_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(rel)
    return written


def seed_with_degradation(user_root: Path | None, plugin_dir: Path) -> str:
    """带明示降级的播种入口（on_load 用）：返回可观察结果行，失败返回降级说明。

    - user_root 不可得 → "skipped: 用户空间不可得"；
    - OSError → "degraded: <原因>"；
    - 成功 → "seeded: <n> files"。
    不抛（降级策略明示为可达性增强，主流程失败面隔离）。
    """
    if user_root is None:
        return "skipped: 用户空间不可得（载荷留在插件目录内）"
    try:
        written = seed_skill_into(user_root, plugin_dir)
    except OSError as exc:
        return f"degraded: {exc}"
    return f"seeded: {len(written)} files"
