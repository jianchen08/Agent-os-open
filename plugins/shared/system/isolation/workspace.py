"""
工作空间路径解析

暴露接口：
- resolve_workspace()：统一解析任务的工作空间路径
- resolve_workspace_chain()：递归解析任务工作空间（支持多层嵌套）
- get_workspace_config_root()：从配置文件读取工作空间根目录
- get_workspace_base_dir()：统一解析工作空间基目录（配置驱动，绝对路径）
- find_project_root()：定位仓库根（不硬编码父目录层数）
- validate_workspace_path()：工作空间路径安全性校验（任务/会话共用）
"""

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = ".ai_workspaces"

# Windows 盘符绝对路径（Linux 上 Path.is_absolute() 不认盘符，需正则兜底）
_WIN_ABS_PATH = re.compile(r"^[a-zA-Z]:[/\\]")

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


def _isolation_config_path() -> Path:
    """定位 isolation_config.yaml（仓库根 config/isolation/ 下）。

    优先 AGENTOS_CONFIG_ROOT（内核启动时写入 <project_root>/config 并发布到进程
    环境，sidecar 继承，部署布局无关）；回退从本文件向上查找含
    config/isolation/isolation_config.yaml 的祖先目录（不硬编码父目录层数）。
    旧链 config.config_center 在 0.2 sidecar venv 不存在（P1-7 延后 P6，
    见 docs/working/p1_7_config_center_migration_checklist.md #7）——直读永远
    失败、get_workspace_config_root() 恒返回缺省 .ai_workspaces，配置
    workspace.root 完全不生效（2026-08-24 工作空间基目录偏差根因）。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root) / "isolation" / "isolation_config.yaml"
        if p.exists():
            return p
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "config" / "isolation" / "isolation_config.yaml"
        if candidate.exists():
            return candidate
    # 找不到时保持旧行为：返回推导路径（加载失败走缺省 .ai_workspaces，不 panic）
    return Path(__file__).resolve().parent.parent.parent / "config" / "isolation" / "isolation_config.yaml"


def _load_isolation_config() -> dict:
    """读取 isolation 配置。

    优先 ConfigCenter（统一缓存）；config_center 不可用时（P1-7 迁移前，
    sidecar venv 无 config 包）文件回退直读 isolation_config.yaml，避免
    配置空载导致 workspace.root 恒走缺省。
    """
    try:
        from config.config_center import get_config_center  # noqa: PLC0415
        # P1-7 DEBT(task_11): 🔴 高危——workspace 隔离配置直读，迁移前提同 manager #2。
        # 见 docs/working/p1_7_config_center_migration_checklist.md #7，延后 P6。

        config = get_config_center().get("isolation/isolation_config.yaml") or {}
        if config:
            return config
    except Exception as e:
        logger.warning(f"读取 isolation 配置失败（config_center）| error={e}")
    # 文件回退：config_center 不可用或返回空时直读磁盘真身配置
    try:
        path = _isolation_config_path()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if data:
            logger.warning(f"隔离配置经文件回退加载: {path}")
            return data
    except Exception as e:
        logger.warning(f"读取 isolation 配置失败（文件回退）| error={e}")
    return {}


def get_workspace_config_root() -> str:
    """从配置文件读取工作空间根目录，读取失败则返回默认值

    语义（对齐 _workspace_git_ops._get_workspace_root 与 0.1 契约）：返回的
    workspace.root 是**基目录**——支持绝对路径（如 D:/myproject）与相对路径
    （**相对项目根**，如 .ai_workspaces），不是项目根下的子目录名本身。
    调用方（resolve_workspace / validate_workspace_path / _task_cleanup）以
    字符串使用：绝对路径原样使用，相对路径需自行拼项目根（服务层经
    get_workspace_base_dir() 统一完成）。读取失败返回缺省 ".ai_workspaces"
    （配置缺失时的默认**相对**值，与历史行为一致）。
    """
    config = _load_isolation_config()
    root = config.get("workspace", {}).get("root")
    if root:
        return str(root)
    return _DEFAULT_WORKSPACE_ROOT


def find_project_root() -> Path:
    """定位仓库根（config/isolation/ 所在祖先目录），不硬编码父目录层数。

    对齐 policy._default_policy_path 的祖先查找模式：AGENTOS_CONFIG_ROOT 优先
    （内核启动时把它发布到进程环境，指向 <project_root>/config——其父目录即
    项目根）；回退从本文件向上找含 config/isolation/ 的祖先目录。找不到时
    回退从本文件按旧 parents[3] 推导（调用方缺省兜底，不 panic）。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root)
        if (p / "isolation" / "isolation_config.yaml").is_file():
            return p.parent
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "config" / "isolation").is_dir():
            return ancestor
    return Path(__file__).resolve().parents[3]


def ensure_workspace_git_ignored(base: Path) -> bool:
    """确保工作空间基目录不被所在仓库的 git 追踪（.git/info/exclude 本地排除）。

    工作空间根是配置项（workspace.root）——.gitignore 的静态条目只在特定
    配置值下成立，根改配到仓库内其他位置即失效。exclude 不入库、不产生
    diff，且 git 还原（reset/checkout/clean）不触碰 .git 内部，对受管还原
    环境是可靠的本地防线。

    base 在项目根之外（或项目根无 .git）→ 无 git 追踪面，直接放行；
    base 即项目根本身 → 排除等于瘫痪 git，拒绝并告警。
    幂等：exclude 已含该条目时不重复写。

    Returns:
        True = 无追踪面或已确保排除；False = 排除未落地（含病态配置）。
    """
    project_root = find_project_root()
    try:
        rel = base.resolve().relative_to(project_root.resolve())
    except ValueError:
        return True
    if rel == Path():
        logger.warning(
            "[workspace] 工作空间基目录即项目根，git 排除被拒绝（排除整个仓库等于瘫痪 git）: %s",
            base,
        )
        return False
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return True
    pattern = f"/{rel.as_posix()}/"
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text("utf-8", errors="replace") if exclude.exists() else ""
        if any(line.strip() == pattern for line in existing.splitlines()):
            return True
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"# agentos workspace root (auto-managed, do not commit workspace here)\n{pattern}\n")
        logger.info("[workspace] 工作空间基目录已写入 git 本地排除 | %s", exclude)
        return True
    except OSError as exc:
        logger.warning("[workspace] git 本地排除写入失败（工作区可能被 git 追踪）| error=%s", exc)
        return False


def get_workspace_base_dir() -> Path:
    """统一解析工作空间基目录（配置驱动，返回绝对路径）。

    所有工作空间（worktree/container/plain 占位）的父目录。语义：
    - workspace.root 为**绝对路径** → 原样使用（如 "D:/myproject"）；
    - 相对路径 → 相对**项目根**（find_project_root()，不是 cwd——sidecar 的
      cwd 是插件目录，拼 cwd 会把工作空间建错位置）解析，缺省
      ".ai_workspaces" 即项目根下隐藏目录。

    服务层（_workspace_git_ops._get_workspace_root）与插件降级路径
    （workspace_lifecycle/plugin.py）统一走本函数，杜绝各自硬编码推导。
    每次解析附带 git 本地排除保障：基目录是配置项，静态 .gitignore 只在
    特定配置值下成立，exclude 随解析出的实际根动态落地。
    """
    config = _load_isolation_config()
    raw = config.get("workspace", {}).get("root") or _DEFAULT_WORKSPACE_ROOT
    raw_str = str(raw).strip()
    if not raw_str:
        raw_str = _DEFAULT_WORKSPACE_ROOT
    if _WIN_ABS_PATH.match(raw_str) or Path(raw_str).is_absolute():
        base = Path(os.path.normpath(raw_str))
    else:
        base = Path(os.path.normpath(str(find_project_root() / raw_str)))
    try:
        ensure_workspace_git_ignored(base)
    except Exception:  # noqa: BLE001
        logger.warning("[workspace] git 本地排除保障异常（继续返回基目录）", exc_info=True)
    return base


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
