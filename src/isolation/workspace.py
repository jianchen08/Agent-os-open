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
_config_path = Path("config/isolation/isolation_config.yaml")


def get_workspace_config_root() -> str:
    """从配置文件读取工作空间根目录，读取失败则返回默认值"""
    try:
        import yaml

        if _config_path.exists():
            with open(_config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            root = config.get("workspace", {}).get("root")
            if root:
                return str(root)
    except Exception as e:
        logger.warning(f"读取工作空间配置失败，使用默认值 | error={e}")
    return _DEFAULT_WORKSPACE_ROOT


def _is_absolute_path(path_str: str) -> bool:
    """判断路径是否为绝对路径（兼容 Windows 和 Unix 风格）"""
    p = Path(path_str)
    if p.is_absolute():
        return True
    if path_str.startswith("/") and not path_str.startswith("//"):
        return True
    return False


def resolve_workspace(
    task_id: str,
    task_workspace: str | None,
    parent_resolved_workspace: str | None = None,
    config_root: str | None = None,
) -> str:
    """统一解析任务的工作空间路径

    规则：
    - 根任务（parent_resolved_workspace 为 None）：
      - 绝对路径：直接使用
      - 相对路径：config_root / task_workspace
      - 默认：config_root / task_id
    - 子任务（parent_resolved_workspace 有值）：
      - 指定空间：parent_resolved_workspace / task_workspace
      - 默认：parent_resolved_workspace / task_id

    Args:
        task_id: 当前任务 ID
        task_workspace: 当前任务 DB 中的 workspace 字段
        parent_resolved_workspace: 父任务已解析的工作空间路径（根任务时为 None）
        config_root: 工作空间根目录配置，默认从配置文件读取

    Returns:
        解析后的工作空间路径字符串
    """
    root = config_root or get_workspace_config_root()

    if parent_resolved_workspace is None:
        if not task_workspace:
            return f"{root}/{task_id}"
        if _is_absolute_path(task_workspace):
            return task_workspace
        if task_workspace.startswith(f"{root}/") or task_workspace == root:
            logger.debug(
                f"[resolve_workspace] task_workspace 已包含 root 前缀，直接返回 | "
                f"task_workspace={task_workspace}"
            )
            return task_workspace
        return f"{root}/{task_workspace}"
    else:
        if task_workspace:
            return f"{parent_resolved_workspace}/{task_workspace}"
        return f"{parent_resolved_workspace}/{task_id}"


async def resolve_workspace_chain(
    task_id: str,
    task_workspace: str | None,
    session,
) -> str:
    """递归解析任务工作空间路径（支持多层嵌套）

    BUG-FIX-fix_20260409_workspace_chain:
    问题根因: resolve_workspace 解析父任务 workspace 时只追溯一层，
             三层及以上嵌套子任务的父任务被当作根任务解析，丢失祖先链信息，
             导致孙任务工作空间与子任务平级而非嵌套。
    修复方案: 沿 parent_task_id 链递归到根任务，逐层构建完整工作空间路径。
    影响范围: 工作空间路径解析系统

    Args:
        task_id: 当前任务 ID
        task_workspace: 当前任务 DB 中的 workspace 字段
        session: 数据库会话（AsyncSession）

    Returns:
        解析后的工作空间路径字符串
    """
    from src.db.models import Task

    task = await session.get(Task, task_id)
    if not task:
        logger.warning(
            f"[resolve_workspace_chain] 任务不存在，使用基础解析 | task_id={task_id}"
        )
        return resolve_workspace(task_id, task_workspace)

    if not task.parent_task_id:
        return resolve_workspace(task_id, task_workspace)

    parent_workspace = await resolve_workspace_chain(
        task_id=task.parent_task_id,
        task_workspace=None,
        session=session,
    )
    return resolve_workspace(
        task_id, task_workspace, parent_resolved_workspace=parent_workspace
    )
