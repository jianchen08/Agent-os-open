"""仓库源码区读取锚 — 文件读面（file_read / enhanced_search）的第三锚点。

0.2 文件工具锚定链原为 workspace/project_root 单根（A1 收口后 fail-closed），
任务管道的 project_root 由 workspace_lifecycle 写为任务工作空间本身
（param_inject 注入注释原裁定：不做仓库根回退——不得把项目源码树变成
agent 读写面）。2026-09-12 用户裁定反转该限制的**读侧**：纯读操作
（read / search）额外允许仓库源码区（Agent OS 仓库根内、运行时/产物目录
之外），write / delete / move / copy 维持单根不变。凭据黑名单
（fs_tools._sensitive_file_reason）与本模块目录拒绝集正交且恒先行。

拒绝集：仓库根内一等目录中的运行时面与产物面——config（安全规则/LLM
密钥配置）、data（多租户任务树）、logs、.ai_workspaces（跨会话工作区，
自身工作区经 workspace 锚仍可读）、.git（历史对象库），以及无可读价值的
噪声目录（.venv / venv / node_modules / __pycache__ / target）。

解析规则（与 param_inject 原实现同源，统一到本模块单点）：
AGENTOS_CONFIG_ROOT（内核启动发布，指向 <repo>/config）优先；回退自本
文件向上找含 config/kernel 的祖先目录。两者皆不可得 → 无仓库锚
（行为退回单根，不报错）。

[来源: docs/decisions/2026-09-12-read-anchor-repo-source.md]
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根内禁止读取面覆盖的一等目录（运行时面 + 产物/噪声面）。
REPO_READ_DENIED_DIRS: frozenset[str] = frozenset(
    {
        "data",
        "config",
        "logs",
        ".ai_workspaces",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "target",
    }
)

_resolved: Path | None = None
_resolved_done = False


def resolve_repo_root() -> Path | None:
    """解析 Agent OS 仓库根（结果缓存；测试用 :func:`reset_cache` 重置）。"""
    global _resolved, _resolved_done
    if _resolved_done:
        return _resolved

    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root)
        if (p / "kernel").is_dir():
            _resolved = p.parent
            _resolved_done = True
            return _resolved
        if (p / "config" / "kernel").is_dir():
            _resolved = p
            _resolved_done = True
            return _resolved
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "kernel").is_dir():
            _resolved = parent
            _resolved_done = True
            return _resolved

    _resolved = None
    _resolved_done = True
    return None


def reset_cache() -> None:
    """清空解析缓存（测试在改 AGENTOS_CONFIG_ROOT 后调用）。"""
    global _resolved, _resolved_done
    _resolved = None
    _resolved_done = False


def repo_read_verdict(resolved: Path) -> tuple[bool, str | None]:
    """判定绝对路径是否落在本仓库可读源码区内。

    Returns:
        (是否命中仓库锚, 拒绝原因)
        - (False, None)：不在仓库根内（调用方继续走原单根拒绝路径）；
        - (True, None)：在仓库根内且不在拒绝目录（允许读取）；
        - (True, 原因)：在仓库根内但位于运行时/产物目录（拒绝读取）。
    """
    root = resolve_repo_root()
    if root is None:
        return (False, None)
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return (False, None)
    first = rel.parts[0] if rel.parts else ""
    if first in REPO_READ_DENIED_DIRS:
        return (
            True,
            f"路径位于仓库 {first}/ 目录（运行时/产物区，不可读），"
            "读取被拒绝；仓库内可读面=源码与文档目录（kernel/plugins/frontend/docs/scripts/tests 等）",
        )
    return (True, None)


def repo_walk_prune(resolved_root: Path) -> set[str]:
    """搜索根若在仓库根内，返回遍历需剪枝的拒绝目录绝对路径集合。

    搜索根指向仓库根本身时会顺路遍历一等目录，剪枝集交给
    search_tool 的 walk 循环剔除；根在仓库外（普通工作区）时返回空集。
    条目经 normcase 归一（Windows 文件系统大小写不敏感，os.walk 产出的
    路径大小写跟随传入串，字符串集比较必须同规）。
    """
    root = resolve_repo_root()
    if root is None:
        return set()
    try:
        resolved_root.relative_to(root)
    except ValueError:
        return set()
    return {os.path.normcase(str(root / d)) for d in REPO_READ_DENIED_DIRS}
