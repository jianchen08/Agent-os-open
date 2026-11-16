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
文件向上找含 config/kernel 的祖先目录。候选含用户空间根一律不采纳
（user_space 契约：用户可写资产整体位于仓库外；装机形态其 config/kernel
由出厂树播种，目录指纹不可辨，按范畴排除——用户根成锚会把用户运行时面
拖进仓库恒拒集）。两者皆不可得 → 无仓库锚（行为退回单根，不报错）。

[来源: docs/decisions/2026-09-12-read-anchor-repo-source.md]
"""

from __future__ import annotations

import os
from pathlib import Path

from user_space import user_root

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


def _is_repo_root_candidate(candidate: Path) -> bool:
    """候选目录是否可作仓库根：config/kernel 指纹命中且非用户空间根。

    用户空间根是运行时面（user_space 契约：用户可写资产整体位于仓库**之外**），
    其 ``config/kernel`` 由装机链播种（kernel-manager 钉
    ``AGENTOS_CONFIG_ROOT=<user_root>/config``，出厂树经 seedConfigTree 补齐），
    目录指纹无法区分仓库根与用户根，按范畴排除。不排除的后果：用户根被采纳
    为仓库根后，用户运行时面（``.ai_workspaces`` 等）整体落进仓库恒拒集，
    写区白名单链在其下全部失效（锚外白名单写被误判系统自保硬拒）。
    """
    if not (candidate / "config" / "kernel").is_dir():
        return False
    user_dir = user_root()
    if user_dir is None:
        return True
    return os.path.normcase(os.path.normpath(str(candidate))) != os.path.normcase(
        os.path.normpath(str(user_dir))
    )


def resolve_repo_root() -> Path | None:
    """解析 Agent OS 仓库根（结果缓存；测试用 :func:`reset_cache` 重置）。"""
    global _resolved, _resolved_done
    if _resolved_done:
        return _resolved

    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root)
        if (p / "kernel").is_dir() and _is_repo_root_candidate(p.parent):
            _resolved = p.parent
            _resolved_done = True
            return _resolved
        if (p / "config" / "kernel").is_dir() and _is_repo_root_candidate(p):
            _resolved = p
            _resolved_done = True
            return _resolved
    for parent in Path(__file__).resolve().parents:
        if _is_repo_root_candidate(parent):
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


def repo_write_denied(resolved: Path) -> str | None:
    """写面系统自保拒绝（ADR 2026-09-24-read-deny-write-zones 决策2）。

    仓库根内拒绝目录对**写**恒拒——写区名单覆盖仓库源码/文档目录后，运行时
    面与产物面（data/config/logs/.git 等）不随写区放行，否则 agent 可经名单
    前缀改写内核运行时资产（登记簿/日志/规则配置）。仅约束根锚之外的写区
    路径：workspace/project_root 锚内写不经本判定（任务工作区常驻仓库
    ``.ai_workspaces``，锚内行为保持现状，不新开也不收口）。

    ``.ai_workspaces/sessions/`` 会话子树豁免：会话登记项目工作树常驻其下
    （sessions/thread-*/projects/*），对根锚不在同树的任务管道而言是"自己
    的项目"，不豁免则撞硬拒且不可授权（无卡路径）。豁免后落入写区链正常
    判定（名单/根锚/授权卡），跨会话污染防线不变：无授权依旧写不进其他
    会话工作树。任务运行时面（.ai_workspaces/<task_id>）不在豁免内，恒拒。

    Returns:
        拒绝原因；None = 不拒绝（不在仓库根内，或在一等源码/文档区）。
    """
    root = resolve_repo_root()
    if root is None:
        return None
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    first = rel.parts[0] if rel.parts else ""
    if first in REPO_READ_DENIED_DIRS:
        if first == ".ai_workspaces" and len(rel.parts) >= 2 and rel.parts[1] == "sessions":
            return None
        return (
            f"路径位于仓库 {first}/ 目录（运行时/产物区，不可写），"
            "写操作被拒绝；写区覆盖仓库源码与文档目录"
        )
    return None


def factory_skills_write_denied(resolved: Path) -> str | None:
    """出厂 ``skills/`` 写区收敛判定（ADR 2026-10-09 批 C-a 红线①，原则 7）。

    出厂 skills/（仓库根一级 ``skills/`` 目录）运行期唯一写路径 = 自进化提案
    管道（proposal_submit/proposal_apply，管道自持 IO 不经工具闸）——任何
    agent 经工具的直写恒拒且不可授权（授权卡会开出第二条写路径，破坏唯一
    写路径红线）。仅锚定仓库根一级 skills 目录：项目根 skills/ 播种（用户域
    资产）与会话工作空间 skills/ 快照（workspace_lifecycle 写面）不经本判定，
    照常按根锚放行。

    消费方须在**根锚放行之前**调用（dev 场景 project_root 即仓库根，锚内
    快捷放行会遮蔽本判定）；读面不收敛（skills/ 是可读源码区）。

    Returns:
        拒绝原因（指向提案管道）；None = 不拒绝。
    """
    root = resolve_repo_root()
    if root is None:
        return None
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    if rel.parts[:1] == ("skills",):
        return (
            "路径位于出厂 skills/ 目录（运行期唯一写路径 = 自进化提案管道："
            "mode_evolution 的 proposal_submit/proposal_apply），工具直写被拒绝；"
            "技能增改请走进化提案"
        )
    return None


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
