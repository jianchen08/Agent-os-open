"""
工作空间路径解析

暴露接口：
- resolve_workspace()：统一解析任务的工作空间路径
- resolve_workspace_chain()：递归解析任务工作空间（支持多层嵌套）
- get_workspace_config_root()：从配置文件读取工作空间根目录
- validate_workspace_path()：工作空间路径安全性校验（任务/会话共用）
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = ".ai_workspaces"

# ── 危险目标空间目录列表 ──
# 这些目录是操作系统关键目录，绝不允许作为任务/会话的目标工作空间。
_DANGEROUS_DIRS: set[str] = set()

_DANGEROUS_WINDOWS_DIRS = [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
]

_DANGEROUS_UNIX_DIRS = [
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/tmp",
    "/home",
    "/opt",
]

for _d in _DANGEROUS_WINDOWS_DIRS + _DANGEROUS_UNIX_DIRS:
    _DANGEROUS_DIRS.add(os.path.normpath(_d).lower())


def validate_workspace_path(workspace: str) -> str | None:  # noqa: PLR0911
    """验证目标空间路径的安全性（任务/会话工作空间共用）。

    Args:
        workspace: 待校验的工作空间路径

    Returns:
        校验通过返回 None；不通过返回错误信息字符串
    """
    if not workspace:
        return None

    # 规范化路径用于比较
    try:
        normalized = os.path.normpath(workspace)
    except (ValueError, TypeError):
        return f"目标空间路径无效: {workspace}"

    Path(normalized)

    # ── 1. 磁盘根目录检查 ──
    if os.name == "nt":
        # Windows: 检查是否为盘符根目录，如 C:\ D:\
        if len(normalized) == 3 and normalized[1] == ":" and normalized[2] == "\\":
            return f"目标空间不能设置为磁盘根目录: {workspace}。请指定具体的项目子目录。"
    # Unix: 检查是否为 /
    elif normalized == "/":
        return f"目标空间不能设置为根目录: {workspace}。请指定具体的项目子目录。"

    # ── 2. 系统危险目录检查 ──
    normalized_lower = normalized.lower()
    if normalized_lower in _DANGEROUS_DIRS:
        return f"目标空间不能设置为系统目录: {workspace}。系统关键目录不允许作为任务的工作空间。"

    # ── 3. 配置文件工作空间根目录检查 ──
    try:
        ws_root = get_workspace_config_root()
        ws_root_normalized = os.path.normpath(ws_root)
        if normalized_lower == ws_root_normalized.lower():
            return (
                f"目标空间不能设置为当前配置的工作空间根目录: {workspace}。"
                f"该目录是系统管理工作空间的根目录，不允许作为任务目标操作。"
            )
    except Exception as e:
        logger.warning("[validate_workspace_path] 读取工作空间配置根目录失败，跳过该检查 | error=%s", e)

    return None


def _load_isolation_config() -> dict:
    """通过 ConfigCenter 读取 isolation 配置（统一缓存）。"""
    try:
        from config.config_center import get_config_center  # noqa: PLC0415
        # P1-7 DEBT(task_11): 🔴 高危——workspace 隔离配置直读，迁移前提同 manager #2。
        # 见 docs/working/p1_7_config_center_migration_checklist.md #7，延后 P6。

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
    try:
        from db.models import Task  # noqa: PLC0415
    except ImportError:
        # 0.2 架构下 Task ORM 模型不在 src.db（kernel 用 SQLite 四表），
        # 此函数当前无外部调用者；保留接口，降级为基础解析。
        logger.debug(
            "[resolve_workspace_chain] db.models.Task 不可用，降级基础解析 | task_id=%s",
            task_id,
        )
        return resolve_workspace(task_id, task_workspace)

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
