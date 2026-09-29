"""区域读写判定单源（ADR 2026-09-24-read-deny-write-zones 及其修订）。

位置闸的判定链单源：security_check（管道层统一位置闸 + 授权卡）与 fs_tools
（工具层 fail-closed 兜底）共用本模块，杜绝双处判定漂移。

判定链（判定序：内容黑名单 → 位置闸 → 档位）：
- 读 = 黑名单制全放：凭据黑名单（消费方自持，紧贴文件操作）→ 仓库锚拒绝集
  → read_deny 前缀 → 其余放行；授权前缀（管道级 grants ∪ 内建工作空间
  地界 ∪ 名单 read_allow）先行命中即放行（白名单先行，用户裁定
  2026-09-29：工作空间根必须在白名单内，不能在黑名单内）；
- 写 = 写区白名单：仓库自保目录恒拒 → 根锚（workspace/project_root）∪ 名单
  entries ∪ 管道级授权前缀命中放行 → 区外（管道层弹授权卡，工具层兜底拒绝）。
  内建地界**不进写链**（跨会话写污染防线）。

名单真值与解析链见 project_registry/tenant_data（ADR 决策5：用户空间真值，
legacy 读取回退）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repo_anchor import repo_read_verdict, repo_write_denied

logger = logging.getLogger(__name__)

# 管道级授权写区的 state 键（pipeline-state.update 仅允许 task.* 前缀）。
STATE_KEY = "task.authorized_write_zones"

# 管道级授权读取的 state 键（读授权卡「仅本管道」落盘；与 param_inject
# 注入读键同名，杜绝写侧 authorized_write_zones 的前缀漂移形态）。
STATE_KEY_READ = "task.authorized_read_zones"

# 工作空间地界目录名（任务工作空间常驻其下会话子目录，项目工作树是
# sessions 的兄弟目录）。
AI_WORKSPACES_DIR = ".ai_workspaces"


def builtin_read_allow_prefixes(workspace: str | None, project_root: str | None) -> list[str]:
    """内建 read_allow 派生前缀：工作空间地界（用户裁定 2026-09-29）。

    任务工作空间常驻仓库 ``.ai_workspaces`` 下的会话子目录，项目工作树是
    sessions 的**兄弟目录**——workspace/project_root 锚覆盖不到，读兄弟
    工作树撞仓库拒绝集弹读授权卡（事故 2026-09-29：无人值守停泊）。用户
    裁定："工作空间根必须在白名单内，不能在黑名单内"——从两锚向上找最近
    的 ``.ai_workspaces`` 祖先（大小写不敏感），命中即作为内建白名单前缀
    参与放行链，与显式 read_allow 同位同权；两锚均不在 .ai_workspaces 下
    则不追加（dev 仓库工作区在别处时零影响）。

    仅读链消费：写链若同扩，agent 可写其他会话的工作树（跨会话污染）；
    黑名单保护的运行时/产物区（config/data/logs）均不在 .ai_workspaces 下，
    读面扩展不触碰它们。
    """
    prefixes: list[str] = []
    for root_str in (project_root, workspace):
        if not root_str:
            continue
        cur = Path(root_str).resolve()
        while True:
            if cur.name.lower() == AI_WORKSPACES_DIR:
                prefixes.append(str(cur))
                break
            if cur.parent == cur:
                break
            cur = cur.parent
    return list(dict.fromkeys(prefixes))


def parse_zones_raw(raw: Any) -> list[str]:
    """授权前缀解析（state 落盘形态 = JSON 数组串）；非法形态安全侧收缩为空。"""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("[zone_policy] authorized_zones 非法 JSON，按无授权处理: %.80s", raw)
        return []
    if not isinstance(data, list):
        return []
    return [str(z) for z in data if str(z or "").strip()]


def load_session_zones(state: Mapping[str, Any]) -> list[str]:
    """从管道 state 读管道级授权写区（zone 授权卡「仅本管道」落盘键）。"""
    return parse_zones_raw(state.get(STATE_KEY, ""))


def load_session_read_grants(state: Mapping[str, Any]) -> list[str]:
    """从管道 state 读管道级授权读取（读授权卡「仅本管道」落盘键）。"""
    return parse_zones_raw(state.get(STATE_KEY_READ, ""))


def resolve_anchored(path: str, workspace: str | None, project_root: str | None) -> Path:
    """把工具路径参数解析为绝对路径（与 fs_tools 同一套锚定语义）。

    - /workspace 前缀重映射（isolation_guard 容器挂载约定 → 宿主工作空间）；
    - 相对路径以 project_root（优先）/workspace 为锚；
    - 无锚 fail-closed（ValueError）——相对路径禁止落到进程 cwd。
    """
    root_str = project_root or workspace
    if not root_str:
        raise ValueError("workspace/project_root 未注入，无法锚定路径（相对路径禁止以进程 cwd 解析）")
    root = Path(root_str).resolve()
    if path == "/workspace" or path.startswith("/workspace/"):
        path = str(root) + path[len("/workspace") :]
    target = Path(path)
    if target.is_absolute():
        return target.resolve()
    return (root / target).resolve()


def grant_dir_of(resolved: Path) -> str:
    """授权目录归一：文件取其父目录（授权粒度 = 目录）。"""
    dir_path = resolved if resolved.is_dir() else resolved.parent
    return os.path.normpath(str(dir_path))


def within_root_anchors(resolved: Path, workspace: str | None, project_root: str | None) -> bool:
    """路径是否落在根锚（project_root 优先语义同 fs_tools：project_root ∪ workspace）内。"""
    for root_str in (project_root, workspace):
        if not root_str:
            continue
        root = Path(root_str).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _load_registration_whitelist() -> list[str]:
    """写区名单加载（project_registry 惰性导入；降级 = 空名单，范围收缩）。"""
    try:
        from project_registry import load_registration_whitelist as _load
    except Exception as exc:  # noqa: BLE001 — 共享根缺失降级可见（error 级留痕）
        logger.error("[zone_policy] 写区名单模块不可用，按空名单处理: %s", exc)
        return []
    return _load()


def _load_read_deny() -> list[str]:
    """读排除前缀加载（同名单文件 read_deny 节；降级语义同写区名单）。"""
    try:
        from project_registry import load_read_deny as _load
    except Exception as exc:  # noqa: BLE001 — 共享根缺失降级可见（error 级留痕）
        logger.error("[zone_policy] 读排除名单模块不可用，按空排除处理: %s", exc)
        return []
    return _load()


def _load_read_allow() -> list[str]:
    """读授权前缀加载（同名单文件 read_allow 节；降级 = 空名单，范围收缩）。"""
    try:
        from project_registry import load_read_allow as _load
    except Exception as exc:  # noqa: BLE001 — 共享根缺失降级可见（error 级留痕）
        logger.error("[zone_policy] 读授权名单模块不可用，按空名单处理: %s", exc)
        return []
    return _load()


def _path_under_prefix(path: Path, prefix: str) -> bool:
    """前缀判定（normcase/normpath 归一，Windows 大小写不敏感同规）。"""
    p = os.path.normcase(os.path.normpath(str(path)))
    pre = os.path.normcase(os.path.normpath(prefix))
    return p == pre or p.startswith(pre + os.sep)


def read_verdict(
    resolved: Path,
    extra_grants: list[str] | None = None,
    workspace: str | None = None,
    project_root: str | None = None,
) -> tuple[bool, str | None]:
    """根外纯读判定链：授权前缀（用户裁定 2026-09-28）→ 仓库锚拒绝集 →
    read_deny 前缀 → 放行。

    放行链 = 管道级 ``extra_grants`` ∪ 内建工作空间地界（``builtin_read_
    allow_prefixes``，用户裁定 2026-09-29：工作空间根必须在白名单内）∪
    名单 ``read_allow`` 节——三者同位同权，任一命中即放行；白名单先行为
    既有语义（地界内 read_deny 条目被白名单压过，与显式 read_allow 同效）。
    未授权的黑名单路径由管道层弹读授权卡交用户裁定（工具层兜底维持拒绝）。

    Args:
        resolved: 已解析的绝对路径。
        extra_grants: 管道级读授权前缀（读授权卡「仅本管道」落盘）。
        workspace: 工作空间锚（内建地界派生输入；不传 = 不派生，行为同前）。
        project_root: 项目根锚（同上）。

    Returns:
        (是否允许, 拒绝原因)；黑名单制无"无锚覆盖"态，拒绝恒带原因。
    """
    for entry in [
        *(extra_grants or []),
        *builtin_read_allow_prefixes(workspace, project_root),
        *_load_read_allow(),
    ]:
        if _path_under_prefix(resolved, entry):
            return True, None
    matched, deny_reason = repo_read_verdict(resolved)
    if matched:
        return deny_reason is None, deny_reason
    for entry in _load_read_deny():
        if _path_under_prefix(resolved, entry):
            return False, f"路径位于用户只读排除区（read_deny 前缀 {entry}），读取被拒绝"
    return True, None


def write_verdict(resolved: Path, extra_zones: list[str] | None = None) -> tuple[bool, str | None]:
    """写区判定链：仓库自保目录恒拒 → 名单 entries ∪ 管道级授权前缀放行。

    Returns:
        (是否放行, 拒绝原因)；(False, None) = 不在任何写区（调用方决定弹授权卡
        还是拒绝）。根锚内路径由调用方先行判定（:func:`within_root_anchors`），
        不经本链——任务工作区常驻仓库 .ai_workspaces，锚内不套自保拒绝。
    """
    deny = repo_write_denied(resolved)
    if deny is not None:
        return False, deny
    for entry in [*(extra_zones or []), *_load_registration_whitelist()]:
        if _path_under_prefix(resolved, entry):
            return True, None
    return False, None
