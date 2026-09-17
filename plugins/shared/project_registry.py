"""项目登记 — project = 真实文件夹 + 登记行（YAML 持久化，跨插件共享真值源）。

模型契约（docs/decisions/2026-08-27-project-folder-registration.md）：
- project 不是任务系统实体：不占 task_id、无任务状态机、无管道
  （方案工作流状态 workflow_state 是独立的生命周期能轴，ADR 2026-09-17）；
- 登记行是 id ↔ 文件夹路径的最薄账本（任务树分组/工作空间定位/生命周期键）；
- 子任务挂靠键 = 任务行 ``metadata.project_id``（state 面
  ``task.parent_project_id`` 同值，task_submit 双写）；
- 项目文件夹是 git 主工作树，子任务 worktree（branch=task/{task_id}）从它分叉。

共享面：tasks（登记读写/文件夹创建）、isolation / pipeline / workspace（只读
解析 project_id → path）三方插件进程隔离，本模块是登记文件的唯一访问实现
（sys.path 自举引用，与 tenant_data.py 同模式）。

存储：``{tasks 数据根}/projects/{project_id}.yaml``（与 TaskStorage 同根）。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# 多租户数据根咽喉点（plugins/shared/tenant_data.py）。本文件位于
# plugins/shared/project_registry.py，上溯 1 级即 plugins/shared/。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from tenant_data import (  # noqa: E402
    DEFAULT_TENANT,
    tenant_config_dir,
    tenant_data_root,
)

logger = logging.getLogger(__name__)


@dataclass
class ProjectModel:
    """项目登记行。

    Attributes:
        id: 项目唯一标识（12hex，与任务 id 同格式）
        path: 项目文件夹宿主绝对路径
        title: 项目标题
        status: active | paused（文件夹生命周期）
        workflow_state: 方案工作流状态 plan | running | done（方案生命周期轴，
            与任务状态机无关；迁移合法边见 WORKFLOW_TRANSITIONS，
            ADR 2026-09-17-plan-mode-project-state-gate）
        auto_execute: 自动执行开关（toggle_auto_execute 端点持久化面）
        created_at / updated_at: ISO 时间戳
        submitted_by: 创建者（用户 sub）
        session_id: 创建时关联的会话（可选）
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: str = ""
    title: str = ""
    status: str = "active"
    workflow_state: str = "plan"
    auto_execute: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    submitted_by: str = ""
    session_id: str = ""


# 方案工作流状态机（ADR 2026-09-17）：plan→running / running→plan 为门控迁移
# （审批在工具层强制，登记面只认边）；running→done 为用户界面管理面（收尾）；
# done 为终态（重新规划 = 用户另起登记）。
WORKFLOW_TRANSITIONS: dict[str, set[str]] = {
    "plan": {"running"},
    "running": {"plan", "done"},
    "done": set(),
}

# plan 态派发白名单（ADR 2026-09-17 决策 2）：方案讨论段允许的外包执行者
# （调研 / 环境准备）。打样验证不在白名单内——先过门（用户审批）再派，从紧口径。
PLAN_DISPATCH_WHITELIST = frozenset({"research_agent", "environment_setup_agent"})


def transition_workflow_state(project: ProjectModel, target: str) -> ProjectModel:
    """方案工作流状态迁移（合法边校验，fail-closed）。

    只校验登记面的合法边；plan↔running 的审批门由工具层（状态迁移入口）强制，
    本函数不感知调用方身份。
    """
    allowed = WORKFLOW_TRANSITIONS.get(project.workflow_state, set())
    if target not in allowed:
        raise ValueError(
            f"非法状态迁移: {project.workflow_state} → {target}"
            f"（合法目标: {', '.join(sorted(allowed)) or '无（终态）'}）"
        )
    project.workflow_state = target
    project.updated_at = datetime.now().isoformat()
    return project


def registry_data_dir(data_dir: str | Path | None = None, tenant_id: str | None = None) -> Path:
    """登记文件目录（与 TaskStorage 同根的 projects/ 子目录，解析优先级一致）。

    显式 data_dir > ``TASKS_STORAGE_DIR`` env > 多租户根 ``data/{tenant_id}/tasks``。
    """
    if data_dir is not None:
        resolved = data_dir
    else:
        env_dir = os.environ.get("TASKS_STORAGE_DIR")
        if env_dir:
            resolved = env_dir
        else:
            resolved = tenant_data_root(tenant_id or DEFAULT_TENANT, "tasks")
    return Path(resolved) / "projects"


class ProjectRegistry:
    """项目登记簿 — 内存缓存 + YAML 文件持久化。

    Attributes:
        _projects: 内存中的登记行缓存（project_id → ProjectModel）
        _data_dir: 登记文件目录
    """

    def __init__(self, data_dir: str | Path | None = None, tenant_id: str | None = None) -> None:
        self._projects: dict[str, ProjectModel] = {}
        self._data_dir = registry_data_dir(data_dir, tenant_id)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _load_all(self) -> None:
        for yaml_file in sorted(self._data_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                project = ProjectModel(**data)
                self._projects[project.id] = project
            except Exception as exc:  # noqa: BLE001 — 单文件损坏不阻断其余登记加载
                # error 级留痕（非 warning）：损坏登记行静默消失 = 项目脱离账本，
                # 必须可被日志告警捕获（写入面已原子化，新损坏属异常态）
                logger.error("加载项目登记文件失败（该行跳过，账本缺行可见）: %s — %s", yaml_file, exc)

    def _persist(self, project: ProjectModel) -> None:
        # 原子写：先写临时文件再 os.replace——项目登记是唯一持久化账本，
        # 写中途崩溃留下的截断 YAML 会在下次启动被跳过 = 项目行静默消失。
        file_path = self._data_dir / f"{project.id}.yaml"
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp_path.write_text(
            yaml.safe_dump(asdict(project), default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp_path.replace(file_path)

    def save(self, project: ProjectModel) -> ProjectModel:
        """保存登记行（新建与更新统一入口）。"""
        self._projects[project.id] = project
        self._persist(project)
        return project

    def get(self, project_id: str) -> ProjectModel | None:
        return self._projects.get(project_id)

    def list(self) -> list[ProjectModel]:
        """全部登记行（创建时间倒序，新项目在前）。"""
        return sorted(self._projects.values(), key=lambda p: p.created_at, reverse=True)

    def delete(self, project_id: str) -> bool:
        """删除登记行（不动文件夹——文件夹清理由调用方决定）。"""
        if project_id not in self._projects:
            return False
        del self._projects[project_id]
        file_path = self._data_dir / f"{project_id}.yaml"
        if file_path.exists():
            file_path.unlink()
        return True


def load_project_paths() -> dict[str, str]:
    """轻量只读解析：project_id → 文件夹路径（isolation/pipeline/workspace 侧用）。

    每次直读登记目录（文件量小）；目录不可达/损坏文件跳过并留痕。
    """
    paths: dict[str, str] = {}
    data_dir = registry_data_dir()
    if not data_dir.is_dir():
        return paths
    for yaml_file in data_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            pid = str(data.get("id") or yaml_file.stem)
            path = str(data.get("path") or "")
            if path:
                paths[pid] = path
        except Exception as exc:  # noqa: BLE001 — 单文件损坏不阻断其余登记解析
            logger.warning("解析项目登记失败: %s — %s", yaml_file, exc)
    return paths


# ════════════════════════════════════════════════════════════
# 项目文件夹解析与创建
# ════════════════════════════════════════════════════════════


def _isolation_config_path() -> Path:
    """定位 isolation_config.yaml（配置文件是共享真值源；workspace.root 同源）。"""
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        p = Path(env_root) / "plugins" / "isolation" / "isolation_config.yaml"
        if p.exists():
            return p
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "config" / "plugins" / "isolation" / "isolation_config.yaml"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / "config" / "plugins" / "isolation" / "isolation_config.yaml"


def project_root_of_tree() -> Path:
    """仓库根（含 config/ 目录的最近祖先）。

    对齐 isolation/workspace.py find_project_root：AGENTOS_CONFIG_ROOT 优先
    （内核启动时把它发布到进程环境，指向 <project_root>/config——其父目录
    即项目根；e2e/多环境部署布局无关）；回退从本文件向上找 config/ 祖先。

    Raises:
        RuntimeError: 探针全失（无 AGENTOS_CONFIG_ROOT 且祖先链无 config/
            目录）——fail-closed：静默兜底会把工作空间基目录落进插件树，
            产生插件树内 .ai_workspaces 残留。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        parent = Path(env_root).parent
        if (parent / "config").is_dir():
            return parent
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "config").is_dir():
            return ancestor
    raise RuntimeError(
        "无法定位项目根（含 config/ 目录）：请设置 AGENTOS_CONFIG_ROOT"
        "（指向 <project_root>/config）或检查部署布局完整性"
    )


def workspace_base_dir() -> Path:
    """工作空间基目录（isolation workspace.root 同源解析：绝对原样、相对拼仓库根）。

    项目默认根 = ``{workspace_base}/projects/``。
    """
    root = ".ai_workspaces"
    try:
        config = yaml.safe_load(_isolation_config_path().read_text(encoding="utf-8")) or {}
        configured = (config.get("workspace") or {}).get("root")
        if isinstance(configured, str) and configured.strip():
            root = configured.strip()
    except Exception as exc:  # noqa: BLE001 — 配置缺失走缺省值
        logger.warning("[projects] 读取 workspace.root 失败，使用缺省 .ai_workspaces | err=%s", exc)
    p = Path(root)
    return p if p.is_absolute() else project_root_of_tree() / p


def _slugify(title: str) -> str:
    """标题 → 安全文件夹名（非法字符折叠为 _，限长 50）。"""
    slug = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("._")
    return slug[:50] or "project"


def ensure_project_folder(title: str, explicit_path: str = "") -> str:
    """解析并创建项目文件夹，返回宿主绝对路径。

    - 显式路径优先（已存在目录直接复用，非 git 自动 ``git init``——幂等
      不删改现有文件；worktree 分叉前提）；
    - 缺省 ``{workspace_base}/projects/<slug>``，重名递增后缀 ``-2/-3...``；
    - 非 git 仓库时 ``git init``（worktree 前提；失败抛错，创建整体失败）。
    """
    base = workspace_base_dir() / "projects"
    if explicit_path:
        target = Path(explicit_path).resolve()
    else:
        target = base / _slugify(title)
        suffix = 2
        while target.exists() and any(target.iterdir()):
            target = base / f"{_slugify(title)}-{suffix}"
            suffix += 1
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"项目文件夹创建失败（{target}）: {e}") from e
    if not (target / ".git").exists():
        result = subprocess.run(
            ["git", "init"],
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git init 失败（项目文件夹已建于 {target}）: {result.stderr.strip()}")
    return str(target)


def _norm_path(path: str) -> str:
    """登记路径归一化（Windows 大小写不敏感）。"""
    norm = os.path.normpath(os.path.abspath(path))
    return norm.lower() if os.name == "nt" else norm


# ════════════════════════════════════════════════════════════
# 登记白名单（ADR 2026-09-17：locked 用户配置，前缀授权）
# ════════════════════════════════════════════════════════════


def load_registration_whitelist(
    tenant_id: str = DEFAULT_TENANT, base: str | Path | None = None
) -> list[str]:
    """项目登记白名单（``config/users/{tenant}/project_whitelist.yaml``）。

    前缀授权：条目授权自身及任意层级后代（含登记时新建的目录，授权看路径
    不要求先存在）。文件缺失/损坏 = 空白名单（范围收缩，安全侧）。写面
    locked 分级（agent 只读 + 提案制）见 ADR 2026-09-17-plan-mode-project-state-gate。
    """
    config_path = tenant_config_dir(tenant_id, base=base) / "project_whitelist.yaml"
    if not config_path.is_file():
        return []
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("项目登记白名单解析失败（按空白名单处理）: %s — %s", config_path, exc)
        return []
    entries = data.get("entries") or []
    return [str(e) for e in entries if str(e or "").strip()]


def _canonical_path(path: str | Path) -> str:
    """登记比较用同源 canonicalize（白名单条目/既有登记/新目标三边共用）。

    目标可能尚不存在（白名单授权新建目录）：对最近存在祖先 realpath 后拼接
    剩余段——单边 canonicalize 在 Windows \\?\\ 前缀/大小写上有跨边误判前科
    （ADR 2026-09-17 白名单校验节），所有比较边必须过同一函数。
    """
    p = Path(path)
    anchor: Path = p
    remainder: list[str] = []
    while not anchor.exists():
        if anchor.parent == anchor:
            break
        remainder.insert(0, anchor.name)
        anchor = anchor.parent
    real = Path(os.path.realpath(anchor))
    for seg in remainder:
        real = real / seg
    norm = os.path.normpath(str(real))
    return norm.lower() if os.name == "nt" else norm


def match_registration_scope(target_canon: str, entries: list[str]) -> str | None:
    """返回命中授权范围（canonical 形态）：白名单条目优先，工作空间根为缺省成员。

    白名单未配置/未命中时仅工作空间根（含其下任意层级）可登记——存量兼容默认。
    """
    for entry in entries:
        canon = _canonical_path(entry)
        if target_canon == canon or target_canon.startswith(canon + os.sep):
            return canon
    ws = _canonical_path(workspace_base_dir())
    if target_canon == ws or target_canon.startswith(ws + os.sep):
        return ws
    return None


def _assert_no_project_overlap(target_canon: str, registry: ProjectRegistry) -> None:
    """项目根重叠拒绝（嵌套任一方向）——重叠会使状态闸/挂载/追溯范围歧义。"""
    for project in registry.list():
        if not project.path:
            continue
        existing = _canonical_path(project.path)
        if target_canon.startswith(existing + os.sep) or existing.startswith(target_canon + os.sep):
            raise ValueError(
                f"登记路径与已登记项目 {project.id}（{project.path}）重叠"
                "——项目根禁止嵌套，状态闸与挂载范围要求一根一项目"
            )


def ensure_project_registered(
    title: str,
    explicit_path: str = "",
    session_id: str = "",
    submitted_by: str = "",
    auto_execute: bool = False,
    registry: ProjectRegistry | None = None,
) -> tuple[ProjectModel, bool]:
    """按路径幂等登记项目：同路径已登记 → 复用（created=False），否则建文件夹 + 登记。

    多入口共用（projects 域 API / task_submit 创建挂靠 / 会话目录登记）：
    登记簿是 id ↔ 路径最薄账本，同一文件夹不应产生多条登记——重复提交
    返回既有登记行，调用方按 created 区分新建与复用。
    """
    if registry is None:
        registry = ProjectRegistry()
    explicit = explicit_path or ""
    if explicit:
        want = _canonical_path(explicit)
        for project in registry.list():
            if project.path and _canonical_path(project.path) == want:
                return project, False
        # 新登记才过闸（幂等复用先于白名单：存量登记不受新闸影响）
        if match_registration_scope(want, load_registration_whitelist()) is None:
            raise ValueError(
                f"登记路径不在白名单内: {explicit}"
                "（白名单 = config/users/{tenant}/project_whitelist.yaml，"
                "locked 用户配置；工作空间根为缺省成员）"
            )
        _assert_no_project_overlap(want, registry)
    folder = ensure_project_folder(title, explicit)
    project = ProjectModel(
        path=folder,
        title=title,
        auto_execute=auto_execute,
        submitted_by=submitted_by,
        session_id=session_id,
    )
    registry.save(project)
    return project, True


def _rmtree_onexc(func: Any, path: str, exc: BaseException) -> None:
    """rmtree 遇 Windows 只读文件（git 对象恒只读）解锁重试，其余异常原样抛出。"""
    if isinstance(exc, PermissionError):
        os.chmod(path, stat.S_IWRITE)
        func(path)
        return
    raise exc


def remove_project_folder(path: str) -> bool:
    """删除项目文件夹（破坏性，调用方负责确认；带路径安全校验）。

    校验：不得为盘符根/仓库根/工作空间基本身——命中拒绝删除返回 False。
    """
    target = Path(path).resolve()
    guarded = {str(workspace_base_dir().resolve()).lower(), str(project_root_of_tree().resolve()).lower()}
    target_s = str(target).lower()
    if target_s in guarded or (os.name == "nt" and re.fullmatch(r"[a-z]:\\", target_s)):
        logger.warning("[projects] 拒绝删除受保护路径: %s", path)
        return False
    if not target.is_dir():
        return False
    import shutil

    # 只读解锁回调双签名：onexc(func, path, exc)=3.12+ 增补 API；
    # onerror(func, path, excinfo)=3.11 及以下（未删除，仅弃用）。
    # CI（3.11）与本地（3.12/3.14）并存，按签名可用性分发（不猜版本号）。
    import inspect

    if "onexc" in inspect.signature(shutil.rmtree).parameters:
        shutil.rmtree(target, onexc=_rmtree_onexc)  # type: ignore[call-arg]
    else:

        def _onerror(func: Any, path: str, excinfo: Any) -> None:
            # onerror 的 excinfo 是 (type, value, tb) 元组
            _rmtree_onexc(func, path, excinfo[1])

        shutil.rmtree(target, onerror=_onerror)
    return True


# ════════════════════════════════════════════════════════════
# 容器任务实体遗留数据清除（一次性语义，幂等执行）
# ════════════════════════════════════════════════════════════


def purge_legacy_container_data(task_storage: Any) -> dict[str, int]:
    """清除容器任务实体的遗留数据（tasks 插件启动时调用，幂等）。

    范围（ADR 2026-08-27：不迁移直接清除）：
    1. TaskStorage 中 ``metadata.task_scope == "container"`` 的任务行；
    2. 子任务 ``parent_task_id`` 指向被删容器 → 置 None（退化为独立任务）；
    3. 工作空间基目录下 ``container_*`` 隔离副本目录。

    Returns:
        统计字典 {removed_containers, detached_children, removed_dirs}。
    """
    import shutil

    container_ids = {
        t.id for t in task_storage.list_all() if (t.metadata or {}).get("task_scope") == "container"
    }
    removed_containers = 0
    for cid in container_ids:
        if task_storage.delete(cid):
            removed_containers += 1

    detached_children = 0
    for t in task_storage.list_all():
        if t.parent_task_id in container_ids:
            task_storage.update(t.id, parent_task_id=None)
            detached_children += 1

    removed_dirs = 0
    ws_base = workspace_base_dir()
    if ws_base.is_dir():
        for d in ws_base.glob("container_*"):
            if not d.is_dir():
                continue
            try:
                shutil.rmtree(d)
                removed_dirs += 1
            except OSError as exc:
                logger.warning("[projects] 容器空间目录删除失败（下次启动重试）: %s | err=%s", d, exc)

    if removed_containers or detached_children or removed_dirs:
        logger.info(
            "[projects] 容器任务遗留数据清除 | containers=%s detached_children=%s dirs=%s",
            removed_containers,
            detached_children,
            removed_dirs,
        )
    return {
        "removed_containers": removed_containers,
        "detached_children": detached_children,
        "removed_dirs": removed_dirs,
    }
