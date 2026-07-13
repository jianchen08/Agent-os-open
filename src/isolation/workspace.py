"""
工作空间路径解析

暴露接口：
- resolve_workspace()：统一解析任务的工作空间路径
- resolve_workspace_chain()：递归解析任务工作空间（支持多层嵌套）
- get_workspace_config_root()：从配置文件读取工作空间根目录
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = ".ai_workspaces"


def _load_isolation_config() -> dict:
    """通过 ConfigCenter 读取 isolation 配置（统一缓存）。"""
    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        return get_config_center().get("isolation/isolation_config.yaml") or {}
    except Exception as e:
        logger.warning(f"读取 isolation 配置失败 | error={e}")
        return {}


def get_workspace_config_root() -> str:
    """从配置文件读取工作空间根目录，读取失败则返回默认值"""
    config = _load_isolation_config()
    root = config.get("workspace", {}).get("root")
    if root:
        return str(root)
    return _DEFAULT_WORKSPACE_ROOT


def get_isolation_level() -> str:
    """从配置文件读取隔离级别，读取失败则返回默认值 isolated"""
    config = _load_isolation_config()
    level = config.get("coordinator", {}).get("default_level")
    if level:
        return str(level)
    return "isolated"


def resolve_container_workspace_path(workspace: str | None, task_id: str, isolation_mode: str | None = None) -> str:
    """纯路径计算：返回容器任务应使用的工作空间路径。

    规则：
    - 有 workspace + non_isolated 模式 → 返回 workspace（原空间）
    - 其余所有情况 → 返回 ws_root/container_{task_id}（配置空间）
    """
    if workspace and (isolation_mode or get_isolation_level()) == "non_isolated":
        return workspace
    ws_root = get_workspace_config_root()
    return f"{ws_root}/container_{task_id}"


def _is_absolute_path(path_str: str) -> bool:
    """判断路径是否为绝对路径（兼容 Windows 和 Unix 风格）"""
    p = Path(path_str)
    if p.is_absolute():
        return True
    return bool(path_str.startswith("/") and not path_str.startswith("//"))


def resolve_workspace(  # noqa: PLR0911
    task_id: str,
    task_workspace: str | None,
    parent_resolved_workspace: str | None = None,
    config_root: str | None = None,
    nesting_mode: str = "nested",
) -> str:
    """统一解析任务的工作空间路径

    规则：
    - 根任务（parent_resolved_workspace 为 None）：
      - 绝对路径：直接使用
      - 相对路径：config_root / task_workspace
      - 默认：config_root / task_id
    - 子任务（parent_resolved_workspace 有值）：
      - nesting_mode="nested"（默认）：在父路径下创建独立子目录
        - 指定空间：parent_resolved_workspace / task_workspace
        - 默认：parent_resolved_workspace / task_id
      - nesting_mode="shared"：子任务直接使用父 workspace 路径，不创建子目录

    Args:
        task_id: 当前任务 ID
        task_workspace: 当前任务 DB 中的 workspace 字段
        parent_resolved_workspace: 父任务已解析的工作空间路径（根任务时为 None）
        config_root: 工作空间根目录配置，默认从配置文件读取
        nesting_mode: 子任务嵌套模式，"nested" 创建独立子目录，"shared" 共享父目录

    Returns:
        解析后的工作空间路径字符串
    """
    root = config_root or get_workspace_config_root()

    root = root.replace("\\", "/")
    if task_workspace:
        task_workspace = task_workspace.replace("\\", "/")
    if parent_resolved_workspace:
        parent_resolved_workspace = parent_resolved_workspace.replace("\\", "/")

    if parent_resolved_workspace is None:
        if not task_workspace:
            return f"{root}/{task_id}"
        if _is_absolute_path(task_workspace):
            return task_workspace
        if task_workspace.startswith(f"{root}/") or task_workspace == root:
            logger.debug(
                f"[resolve_workspace] task_workspace 已包含 root 前缀，直接返回 | task_workspace={task_workspace}"
            )
            return task_workspace
        return f"{root}/{task_workspace}"
    # shared 模式：子任务直接复用父 workspace，不创建独立子目录
    if nesting_mode == "shared":
        logger.debug(
            f"[resolve_workspace] shared 模式，子任务复用父工作空间 | "
            f"task_id={task_id}, parent_workspace={parent_resolved_workspace}"
        )
        return parent_resolved_workspace

    if task_workspace:
        if _is_absolute_path(task_workspace):
            logger.debug(
                f"[resolve_workspace] 子任务 task_workspace 是绝对路径，直接返回 | task_workspace={task_workspace}"
            )
            return task_workspace
        if task_workspace.startswith(f"{parent_resolved_workspace}/") or task_workspace == parent_resolved_workspace:
            logger.debug(
                f"[resolve_workspace] 子任务 task_workspace 已包含父路径前缀，直接返回 | "
                f"task_workspace={task_workspace}"
            )
            return task_workspace
        if task_workspace.startswith(f"{root}/"):
            logger.debug(
                f"[resolve_workspace] 子任务 task_workspace 已包含 root 前缀，直接返回 | "
                f"task_workspace={task_workspace}"
            )
            return task_workspace
        return f"{parent_resolved_workspace}/{task_workspace}"
    return f"{parent_resolved_workspace}/{task_id}"


async def resolve_workspace_chain(
    task_id: str,
    task_workspace: str | None,
    session,
    nesting_mode: str = "nested",
) -> str:
    """递归解析任务工作空间路径（支持多层嵌套）

    沿 parent_task_id 链递归到根任务，逐层构建完整工作空间路径，避免只追溯一层时
    三层及以上嵌套子任务的父任务被当作根任务解析、丢失祖先链信息，导致孙任务
    工作空间与子任务平级而非嵌套。

    Args:
        task_id: 当前任务 ID
        task_workspace: 当前任务 DB 中的 workspace 字段
        session: 数据库会话（AsyncSession）
        nesting_mode: 子任务嵌套模式，"nested" 创建独立子目录，"shared" 共享父目录

    Returns:
        解析后的工作空间路径字符串
    """
    from src.db.models import Task  # noqa: PLC0415

    task = await session.get(Task, task_id)
    if not task:
        logger.warning(f"[resolve_workspace_chain] 任务不存在，使用基础解析 | task_id={task_id}")
        return resolve_workspace(task_id, task_workspace)

    if not task.parent_task_id:
        return resolve_workspace(task_id, task_workspace)

    parent_workspace = await resolve_workspace_chain(
        task_id=task.parent_task_id,
        task_workspace=None,
        session=session,
        nesting_mode=nesting_mode,
    )
    return resolve_workspace(
        task_id,
        task_workspace,
        parent_resolved_workspace=parent_workspace,
        nesting_mode=nesting_mode,
    )
