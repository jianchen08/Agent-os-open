"""区域读写判定单源（ADR 2026-09-24-read-deny-write-zones 及其修订）。

位置闸的判定链单源：security_check（管道层统一位置闸 + 授权卡）与 fs_tools
（工具层 fail-closed 兜底）共用本模块，杜绝双处判定漂移。

判定链（判定序：内容黑名单 → 位置闸 → 档位）：
- 读 = 黑名单制全放：凭据黑名单（消费方自持，紧贴文件操作）→ 仓库锚拒绝集
  → read_deny 前缀 → 其余放行；
- 写 = 写区白名单：仓库自保目录恒拒 → 根锚（workspace/project_root）∪ 名单
  entries ∪ 管道级授权前缀命中放行 → 区外（管道层弹授权卡，工具层兜底拒绝）。

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
        path = str(root) + path[len("/workspace"):]
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


def _path_under_prefix(path: Path, prefix: str) -> bool:
    """前缀判定（normcase/normpath 归一，Windows 大小写不敏感同规）。"""
    p = os.path.normcase(os.path.normpath(str(path)))
    pre = os.path.normcase(os.path.normpath(prefix))
    return p == pre or p.startswith(pre + os.sep)


def read_verdict(resolved: Path) -> tuple[bool, str | None]:
    """根外纯读判定链（读黑名单制）：仓库锚拒绝集 → read_deny 前缀 → 放行。

    Returns:
        (是否允许, 拒绝原因)；黑名单制无"无锚覆盖"态，拒绝恒带原因。
    """
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
