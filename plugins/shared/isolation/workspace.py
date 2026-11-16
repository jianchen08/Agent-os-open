"""
工作空间路径解析

暴露接口：
- resolve_workspace()：统一解析任务的工作空间路径
- get_workspace_config_root()：从配置文件读取工作空间根目录
- get_workspace_base_dir()：统一解析工作空间基目录（配置驱动，绝对路径）
- resolve_workspace_base()：按源目录所在盘收口基目录（跨盘退化点）
- resolve_path_identity()：身份比较用路径解析（final-path 查询，卷查询不可用降级单点）
- find_project_root()：定位仓库根（不硬编码父目录层数）
- validate_workspace_path()：工作空间路径安全性校验（任务/会话共用）
"""

# config-write-surface-exempt: 隔离配置只读探测;无配置写面
import logging
import os
import re
import sys
from pathlib import Path

import yaml

# 危险目标空间校验单源（清单+装配+形态驱动盘符根判定，_libs 共享件）
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _libs.dangerous_targets import check_root_and_system_dirs  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = ".ai_workspaces"

# 跨盘 worktree 退化点（项目所在盘符根下的存放点根名，配置可覆盖）
_DEFAULT_CROSSDRIVE_ROOT_NAME = ".agentos-workspaces"

# Windows 盘符绝对路径（Linux 上 Path.is_absolute() 不认盘符，需正则兜底）
_WIN_ABS_PATH = re.compile(r"^[a-zA-Z]:[/\\]")

# 盘符前缀（D:/proj、D:\proj；仅盘符字母形态，UNC 与 POSIX 绝对路径不匹配）
_WIN_DRIVE_PREFIX = re.compile(r"^([a-zA-Z]):")


def validate_workspace_path(workspace: str) -> str | None:
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

    # ── 1+2. 磁盘根目录 / 系统危险目录检查（单源：_libs.dangerous_targets，
    # 盘符根判定=路径形态而非运行平台）──
    root_or_system_err = check_root_and_system_dirs(normalized, workspace)
    if root_or_system_err:
        return root_or_system_err

    # ── 3. 配置文件工作空间根目录检查 ──
    normalized_lower = normalized.lower()
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
    """定位 isolation_config.yaml（仓库根 config/plugins/isolation/ 下）。

    优先 AGENTOS_CONFIG_ROOT（内核启动时写入 <project_root>/config 并发布到进程
    环境，sidecar 继承，部署布局无关）；回退从本文件向上查找含
    config/plugins/isolation/isolation_config.yaml 的祖先目录（不硬编码父目录层数）。
    旧链 config.config_center 在 0.2 sidecar venv 不存在（P1-7 延后 P6，
    见 docs/working/p1_7_config_center_migration_checklist.md #7）——直读永远
    失败、get_workspace_config_root() 恒返回缺省 .ai_workspaces，配置
    workspace.root 完全不生效（2026-08-24 工作空间基目录偏差根因）。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root) / "plugins" / "isolation" / "isolation_config.yaml"
        if p.exists():
            return p
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "config" / "plugins" / "isolation" / "isolation_config.yaml"
        if candidate.exists():
            return candidate
    # 找不到时保持旧行为：返回推导路径（加载失败走缺省 .ai_workspaces，不 panic）
    return Path(__file__).resolve().parent.parent.parent / "config" / "plugins" / "isolation" / "isolation_config.yaml"


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

        config = get_config_center().get("plugins/isolation/isolation_config.yaml") or {}
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

    语义：返回的 workspace.root 是**基目录**——支持绝对路径（如 D:/myproject）
    与相对路径（**相对项目根**，如 .ai_workspaces），不是项目根下的子目录名本身。
    调用方（resolve_workspace / validate_workspace_path / _task_cleanup）以
    字符串使用：绝对路径原样使用，相对路径需自行拼项目根（服务层经
    get_workspace_base_dir() 统一完成）。读取失败返回缺省 ".ai_workspaces"
    （配置缺失时的默认**相对**值）。
    """
    config = _load_isolation_config()
    root = config.get("workspace", {}).get("root")
    if root:
        return str(root)
    return _DEFAULT_WORKSPACE_ROOT


def find_project_root() -> Path:
    """定位仓库根（config/plugins/isolation/ 所在祖先目录），不硬编码父目录层数。

    对齐 policy._default_policy_path 的祖先查找模式：AGENTOS_CONFIG_ROOT 优先
    （内核启动时把它发布到进程环境，指向 <project_root>/config——其父目录即
    项目根）；回退从本文件向上找含 config/plugins/isolation/ 的祖先目录。找不到时
    回退从本文件按旧 parents[2] 推导（调用方缺省兜底，不 panic）。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root)
        if (p / "plugins" / "isolation" / "isolation_config.yaml").is_file():
            return p.parent
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "config" / "plugins" / "isolation").is_dir():
            return ancestor
    return Path(__file__).resolve().parents[2]


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


def _is_installed_chain() -> bool:
    """装机链判定：装机链（electron buildKernelEnv）独有注入
    ``AGENTOS_PLUGIN_SOURCE_PRIORITY=builtin``（与 agentos-kernel.rs 的装机链
    口径注释同源；dev launcher 从不设它，sidecar 继承内核进程环境）。
    """
    return os.environ.get("AGENTOS_PLUGIN_SOURCE_PRIORITY", "").strip() == "builtin"


def _relative_root_anchor() -> Path:
    """相对 workspace.root 的锚点目录（形态感知）。

    - 装机形态 → **用户根**（``user_space.user_root()``——与 AGENTOS_USER_ROOT /
      用户插件根 / 用户空间 .env 同一解析源，跨 OS 目录推导由该机制保证，本处
      不拼任何 OS 特定路径）：安装目录随卸载整体删除（BUG-85 同款动因），相对
      root 锚项目根会把工作空间拖进卸载抹除面。
    - dev 形态 → **项目根**（find_project_root()，既有语义不变）。
    - 用户根不可用（无 OS 数据目录）→ 回落项目根（缺省兜底，不 panic）。
    """
    if _is_installed_chain():
        try:
            from _root_api import user_root  # noqa: PLC0415

            resolved = user_root()
            if resolved is not None:
                return Path(resolved)
        except ImportError:
            logger.warning("[workspace] user_space 不可用，相对 root 回落项目根锚定")
    return find_project_root()


def get_workspace_base_dir() -> Path:
    """统一解析工作空间基目录（配置驱动，返回绝对路径）。

    所有工作空间（worktree/container/plain 占位）的父目录。语义：
    - workspace.root 为**绝对路径** → 原样使用（如 "D:/myproject"）；
    - 相对路径 → 锚点按形态（_relative_root_anchor）：dev 相对**项目根**
      （find_project_root()，不是 cwd——sidecar 的 cwd 是插件目录，拼 cwd 会
      把工作空间建错位置）；装机形态相对**用户根**。缺省 ".ai_workspaces"
      即锚点下隐藏目录。

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
        base = Path(os.path.normpath(str(_relative_root_anchor() / raw_str)))
    try:
        ensure_workspace_git_ignored(base)
    except Exception:  # noqa: BLE001
        logger.warning("[workspace] git 本地排除保障异常（继续返回基目录）", exc_info=True)
    return base


def resolve_path_identity(path: Path | str) -> Path:
    """工作空间身份比较用的路径解析（final-path 查询唯一入口，卷查询降级单点）。

    语义 = ``Path.resolve()``：返回可用于相等比较的规范化绝对路径（跟随
    符号链接/挂载别名）。WinFsp 等用户态挂载卷不支持 final-path 查询
    （``nt._getfinalpathname`` → WinError 1005 此卷不包含可识别的文件系统，
    CPython ≤3.13 的 non-strict resolve 对该错误码 re-raise）；该错误只表示
    「卷身份查询不可用」，不代表目录不可用（stat/mkdir 读写均正常，真机
    实证 2026-10-19）——此时降级为规范化路径（normcase+abspath+normpath，
    Windows 上保持大小写不敏感比较语义）。

    契约：仅用于身份相等比较面，且比较两侧一律经本函数（单侧降级会因
    normcase 大小写差异恒不等）。降级丢失别名识别能力，比较结果偏向
    「不相等」——调用方语义必须是「误判不相等只是多做防御性工作，不产生
    破坏」。需要真实 final-path 的场景（git 面/安全边界）不得用本函数。
    """
    try:
        return Path(path).resolve()
    except OSError as exc:
        logger.warning(
            "[workspace] final-path 解析不可用（用户态挂载卷），身份比较降级为规范化路径"
            " | path=%s | error=%s",
            path,
            exc,
        )
        return Path(os.path.normcase(os.path.normpath(os.path.abspath(str(path)))))


def _drive_of(path: Path) -> str:
    """路径的盘符（小写 "d:" 形态）；无盘符形态返回空串。

    判定基准 = 路径形态而非运行平台（同 validate_workspace_path 第 1 项
    口径）：POSIX 绝对路径、相对路径与 UNC 路径恒为空串，即「不构成跨盘」
    ——git 的跨文件系统边界不按盘符表达，非盘符形态的退化不在判定面内。
    """
    m = _WIN_DRIVE_PREFIX.match(str(path))
    return f"{m.group(1).lower()}:" if m else ""


def crossdrive_root_name() -> str:
    """跨盘退化点根目录名（配置 workspace.crossdrive_root_name）。

    缺省 .agentos-workspaces；空串/缺配置同走缺省（盘符根不可写的机器
    可配到别处的同名目录）。
    """
    raw = str(_load_isolation_config().get("workspace", {}).get("crossdrive_root_name") or "").strip()
    return raw or _DEFAULT_CROSSDRIVE_ROOT_NAME


def resolve_workspace_base(base: Path, root_path: Path) -> Path:
    """按源目录所在盘收口工作空间基目录：同盘原样返回，跨盘退化。

    git worktree 要求 worktree 目录与源仓库同一文件系统，跨盘是 git 二进制
    自身的 fatal（`path is on mount 'D:', start on mount 'C:'`），无配置可
    适配；支持跨盘的改法（clone --shared / 链接 .git）会让共享对象库跨
    文件系统断链，已否决（ADR 草稿 Alternatives Considered 1），故此处
    确定性退化到「源目录所在盘符根/<crossdrive_root_name>」。

    本函数是跨盘判定的唯一收口：worktree 任务（isolation/workspace_lifecycle
    ._create_worktree_root）与主会话工作区（workspace_lifecycle/plugin.py）
    共用，其余链路一律按 ws_meta 里的绝对路径透传。配置 root 的绝对/相对
    两级解析在 get_workspace_base_dir 内完成，行为不变——显式配置的绝对
    root 会经此函数退化（绝对路径只表示"用户显式表达"，不代表跨盘可用）。

    告警而非报错是有意偏离仓内 fail-closed 不降级口径：跨盘是常见布局差异
    （装机工作空间根在用户盘、项目在数据盘），退化成报错会把可跑任务变成
    阻断性故障；显式告警让用户知情路径已偏离默认。
    """
    base_drive = _drive_of(base)
    root_drive = _drive_of(root_path)
    if not (base_drive and root_drive and base_drive != root_drive):
        return base
    fallback = Path(f"{root_drive}{os.sep}") / crossdrive_root_name()
    logger.warning(
        "[workspace] 工作空间基目录与项目源目录跨盘，已退化到项目所在盘的存放点"
        "（隔离语义与工作空间路径均偏离默认，请知情）: base=%s, project=%s, fallback=%s",
        base,
        root_path,
        fallback,
    )
    return fallback


def _is_absolute_path(path_str: str) -> bool:
    """判断路径是否为绝对路径（兼容 Windows 和 Unix 风格）

    Linux 上 ``Path.is_absolute()`` 不认 Windows 盘符（``D:/x`` 返回 False），
    需盘符正则兜底——任务 workspace 里的 Windows 盘符路径（任务在 Windows
    项目上建立、sidecar 在 Linux 消费）必须原样使用，不能被拼接进 root。
    """
    if _WIN_ABS_PATH.match(path_str):
        return True
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
