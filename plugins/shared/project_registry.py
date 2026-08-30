"""项目登记 — project = 真实文件夹 + 登记行（YAML 持久化，跨插件共享真值源）。

模型契约（docs/decisions/2026-08-27-project-folder-registration.md）：
- project 不是任务系统实体：不占 task_id、无状态机、无管道；
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
from tenant_data import DEFAULT_TENANT, tenant_data_root  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class ProjectModel:
    """项目登记行。

    Attributes:
        id: 项目唯一标识（12hex，与任务 id 同格式）
        path: 项目文件夹宿主绝对路径
        title: 项目标题
        status: active | paused
        auto_execute: 自动执行开关（toggle_auto_execute 端点持久化面）
        created_at / updated_at: ISO 时间戳
        submitted_by: 创建者（用户 sub）
        session_id: 创建时关联的会话（可选）
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: str = ""
    title: str = ""
    status: str = "active"
    auto_execute: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    submitted_by: str = ""
    session_id: str = ""


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
                logger.warning("加载项目登记文件失败: %s — %s", yaml_file, exc)

    def _persist(self, project: ProjectModel) -> None:
        file_path = self._data_dir / f"{project.id}.yaml"
        file_path.write_text(
            yaml.safe_dump(asdict(project), default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

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
        p = Path(env_root) / "isolation" / "isolation_config.yaml"
        if p.exists():
            return p
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "config" / "isolation" / "isolation_config.yaml"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / "config" / "isolation" / "isolation_config.yaml"


def project_root_of_tree() -> Path:
    """仓库根（含 config/ 目录的最近祖先）。

    对齐 isolation/workspace.py find_project_root：AGENTOS_CONFIG_ROOT 优先
    （内核启动时把它发布到进程环境，指向 <project_root>/config——其父目录
    即项目根；e2e/多环境部署布局无关）；回退从本文件向上找 config/ 祖先。
    """
    env_root = os.environ.get("AGENTOS_CONFIG_ROOT")
    if env_root:
        parent = Path(env_root).parent
        if (parent / "config").is_dir():
            return parent
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "config").is_dir():
            return ancestor
    return Path(__file__).resolve().parent.parent


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
    target.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        result = subprocess.run(
            ["git", "init"], cwd=str(target), capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise RuntimeError(f"git init 失败（项目文件夹已建于 {target}）: {result.stderr.strip()}")
    return str(target)


def _norm_path(path: str) -> str:
    """登记路径归一化（Windows 大小写不敏感）。"""
    norm = os.path.normpath(os.path.abspath(path))
    return norm.lower() if os.name == "nt" else norm


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
        want = _norm_path(explicit)
        for project in registry.list():
            if project.path and _norm_path(project.path) == want:
                return project, False
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

    shutil.rmtree(target)
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
