"""项目登记 — project = 真实文件夹 + 登记行（YAML 持久化）。

模型契约（docs/decisions/2026-08-27-project-folder-registration.md）：
- project 不是任务系统实体：不占 task_id、无任务状态机、无管道
  （方案工作流状态 workflow_state 是独立的生命周期能轴，ADR 2026-09-17）；
- 登记行是 id ↔ 文件夹路径的最薄账本（任务树分组/工作空间定位/生命周期键）；
- 子任务挂靠键 = state 行 ``task.parent_project_id``（task_submit 出生写全，
  项目挂靠唯一真值）；
- 项目文件夹是 git 主工作树，子任务 worktree（branch=task/{task_id}）从它分叉。

归属：本模块自 T6a（资源注册收敛）起为 project_manager 插件私有实现
（原共享根 project_registry.py 整体吸收，共享根过渡件已随消费方迁移删除）；
插件外消费经本插件 project.* 服务（requires_services + capability 直连），
不跨插件 import 本模块。

存储：``{tasks 数据根}/projects/{project_id}.yaml``（与 TaskStorage 同根）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# 多租户数据根咽喉点与模式键解析（plugins/shared/ 共享根薄工具，经 _root_api
# 出口消费）。本文件位于 plugins/shared/project_manager/ 内，上溯 1 级即共享根。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _libs.paths import canon_path  # noqa: E402 — 路径大小写 canon 单源（_libs 共享件）
from _root_api import (  # noqa: E402
    DEFAULT_TENANT,
    MODE_ID_RE,
    find_mode_package_dir,
    legacy_config_users_base,
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
        mode_tags: 项目登记的模式标签集合（开放标签，非枚举——ADR
            2026-10-09 批 B8；空 = 未登记，task_submit 集合闸跳过）
        default_mode: 主域标注（mode_tags 子集，空 = 未标注；仅说明面消费——
            AGENTS.md 主域标注 / 骨架第零步主域确定，不做派发回退）
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
    mode_tags: list[str] = field(default_factory=list)
    default_mode: str = ""


# 方案工作流状态机（ADR 2026-09-17）：plan→running / running→plan 为门控迁移
# （审批在工具层强制，登记面只认边）；running→done 为用户界面管理面（收尾）；
# done 为终态（重新规划 = 用户另起登记）。
WORKFLOW_TRANSITIONS: dict[str, set[str]] = {
    "plan": {"running"},
    "running": {"plan", "done"},
    "done": set(),
}


def validate_mode_registration(mode_tags: list[str], default_mode: str) -> None:
    """项目登记模式标签校验（批 B8，登记/修改写入面 fail-closed）。

    三条（全部可二值）：
    1. 标签形态 = MODE_ID_RE（mode_keys.MODE_ID_RE，与模式 id 全仓同口径）；
    2. 每个标签须在模式注册表可解析（出厂 + 用户双根存在 mode_<tag> 包，
       经 mode_keys.find_mode_package_dir，禁 agent_manager）；
    3. default_mode 非空时必须 ∈ mode_tags（主域是已登记标签之一）。

    Raises:
        ValueError: 任一条不满足，错误信息指明哪个标签不合格。
    """
    seen: list[str] = []
    for tag in mode_tags:
        if not isinstance(tag, str) or not MODE_ID_RE.match(tag):
            raise ValueError(
                f"非法模式标签 {tag!r}（须为小写字母开头的 [a-z0-9_]，≤64 字符，"
                "与模式键形态同口径）"
            )
        if tag in seen:
            continue
        if find_mode_package_dir(tag) is None:
            raise ValueError(
                f"模式标签 {tag!r} 在模式注册表不可解析"
                f"（出厂/用户双根均无 mode_{tag} 包——检查拼写或先安装该模式包）"
            )
        seen.append(tag)
    if default_mode and default_mode not in mode_tags:
        raise ValueError(
            f"default_mode {default_mode!r} 不在 mode_tags 集合内"
            f"（登记主域必须是已登记标签之一，当前集合: {mode_tags or '空'}）"
        )


def seed_mode_skills(project: ProjectModel) -> list[str]:
    """把 mode_tags 各包 skills/ 播种到项目根 skills/（批 B8，用户裁定）。

    播种语义（与 workspace_lifecycle._copy_skills_to_workspace 同款）：增量
    补缺不覆盖——目标已存在的同名技能保持原样（用户从此拥有并直接改这份），
    缺的补齐；包侧技能升级后的对账补缺 = 每次写入 mode_tags 时幂等重跑即得。
    复制源 = 双根中优先者的包内 skills/ 全部内容（find_mode_package_dir
    用户副本赢，不是全六包）；未登记 tags / 无项目路径 = 零操作。

    失败不阻断登记：单技能复制失败 warning + 计入返回的 warnings（错误可见
    不静默）；包不可解析（登记后卸载的陈旧标签）同样 warning 跳过。

    Returns:
        warnings 字符串列表（调用方回传工具输出 warning 字段）。
    """
    warnings: list[str] = []
    if not project.mode_tags or not project.path:
        return warnings
    skills_dst = Path(project.path) / "skills"
    for tag in project.mode_tags:
        pkg_dir = find_mode_package_dir(tag)
        if pkg_dir is None:
            message = (
                f"模式标签 {tag!r} 在模式注册表不可解析（包可能已卸载），"
                "本轮播种跳过该包"
            )
            logger.warning("[projects] 技能播种跳过 | project=%s | %s", project.id, message)
            warnings.append(message)
            continue
        skills_src = pkg_dir / "skills"
        if not skills_src.is_dir():
            continue
        for skill_src in sorted(skills_src.iterdir()):
            if not skill_src.is_dir():
                continue
            skill_dst = skills_dst / skill_src.name
            if skill_dst.exists():
                continue  # 已有技能保持原样（用户改动不覆盖），仅补缺
            try:
                shutil.copytree(skill_src, skill_dst, symlinks=True)
            except Exception as exc:  # noqa: BLE001 — 单技能失败不阻断登记
                message = f"技能播种失败: {skill_src} → {skill_dst}: {exc}"
                logger.warning("[projects] %s | project=%s", message, project.id)
                warnings.append(message)
    return warnings


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
        # mode 字段上次保存值影子（批 B8 写面变更检测）：与 _projects 分离——
        # 调用方惯例是取行原地改后 save 同一对象（动作端点），按 _projects 比
        # 对会被同一引用别名化而永远"未变"；影子在 save 成功时才更新。
        self._mode_state: dict[str, tuple[list[str], str]] = {}
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
                self._mode_state[project.id] = (list(project.mode_tags), project.default_mode)
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
        """保存登记行（新建与更新统一入口；mode 字段变更时校验，批 B8）。

        校验只在 mode_tags/default_mode 相对上次保存值发生变化（或首存）时
        执行——写面统一执法（任意修改入口经此保存），且不让无关写面
        （auto_execute 翻转等动作端点）被模式包卸载后的陈旧标签卡死；修正
        陈旧标签 = 改写标签的写面，照常被校验拦截或放行。校验失败不更新影子。
        """
        previous = self._mode_state.get(project.id)
        if (
            previous is None
            or previous[0] != list(project.mode_tags)
            or previous[1] != project.default_mode
        ):
            validate_mode_registration(project.mode_tags, project.default_mode)
        self._projects[project.id] = project
        self._mode_state[project.id] = (list(project.mode_tags), project.default_mode)
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
        self._mode_state.pop(project_id, None)
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
    """登记路径归一化（Windows 大小写不敏感；canon 尾单源见 ``_libs.paths``）。"""
    return canon_path(os.path.abspath(path))


# ════════════════════════════════════════════════════════════
# 登记白名单（ADR 2026-09-17：locked 用户配置，前缀授权）
# ════════════════════════════════════════════════════════════


def _load_zone_policy(tenant_id: str, base: str | Path | None) -> dict[str, Any]:
    """授权名单 YAML 单点解析（``entries``/``read_deny`` 两节共用）。

    名单文件：``config/users/{tenant}/project_whitelist.yaml``（locked 用户
    配置，ADR 2026-09-24-read-deny-write-zones 决策4）。真值在用户空间；
    ``base=None``（默认解析）且用户空间文件缺失时按 legacy base（仓库根
    ``config/users``，装机布局落包内 resources）回退读——真值迁移期的覆盖层
    "无覆盖 → 回退全局"形态，写面（授权卡永久落盘）恒写用户空间。显式传
    ``base``（测试/部署钉桩）不回退：调用方明确指定即所见即所得。
    """
    config_path = tenant_config_dir(tenant_id, base=base) / "project_whitelist.yaml"
    if not config_path.is_file() and base is None:
        config_path = legacy_config_users_base() / tenant_id / "project_whitelist.yaml"
    if not config_path.is_file():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("项目授权名单解析失败（按空名单处理）: %s — %s", config_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def remove_zone(
    path: str,
    section: str = "entries",
    tenant_id: str = DEFAULT_TENANT,
    base: str | Path | None = None,
) -> list[str]:
    """移除名单条目（设置页管理面；幂等——条目不存在时无变化直接返回）。

    Args:
        section: ``"entries"``（写区）、``"read_deny"``（读排除）或
            ``"read_allow"``（读授权）。

    Returns:
        移除后的该节完整条目列表（真值文件缺失 = 空列表，无操作）。
    """
    if section not in ("entries", "read_deny", "read_allow"):
        raise ValueError(f"非法名单节: {section!r}")
    return _mutate_zone_section(section, path, add=False, tenant_id=tenant_id, base=base)


# 授权名单三节（project.load_zones / project.add_zone 服务面的 section 值域）
ZONE_SECTIONS = ("entries", "read_deny", "read_allow")


def load_zone_sections(
    tenant_id: str = DEFAULT_TENANT, base: str | Path | None = None
) -> dict[str, list[str]]:
    """授权名单三节一次读全（写区/读排除/读授权；单文件单次解析）。

    服务面 project.load_zones 消费：一次 RPC 取全三节（真值同一份 YAML，
    判定链读/写两链各取所需）。空节 = 空列表，与三个单节加载器同语义。
    """
    data = _load_zone_policy(tenant_id, base)
    return {
        section: [str(e) for e in (data.get(section) or []) if str(e or "").strip()]
        for section in ZONE_SECTIONS
    }


def add_zone_entry(
    path: str,
    section: str = "entries",
    tenant_id: str = DEFAULT_TENANT,
    base: str | Path | None = None,
) -> list[str]:
    """按节追加名单条目（写区/读排除/读授权统一写通道；project.add_zone 消费）。

    语义同三个单节 adder（授权卡永久落盘 / 设置页），section 分发到同一
    ``_mutate_zone_section``；非法节 fail-closed。

    Returns:
        落盘后的该节完整条目列表。
    """
    if section not in ZONE_SECTIONS:
        raise ValueError(f"非法名单节: {section!r}")
    return _mutate_zone_section(section, path, add=True, tenant_id=tenant_id, base=base)


def _mutate_zone_section(
    section: str,
    path: str,
    *,
    add: bool,
    tenant_id: str,
    base: str | Path | None,
) -> list[str]:
    """名单节读改写单点（增/删共用；用户空间真值 + legacy 播种）。

    - 归一化：去重比较按 normcase/normpath 同规；落盘条目保原大小写
      （normpath 归一，展示友好）。
    - 播种：add 且真值文件缺失且 base=None（默认解析）时，从 legacy base
      整体接管后修改（remove 不播种——没有可移除的对象就无操作）。
    - 整盘根（空路径/盘符根/UNC 根/POSIX 根）与相对路径拒绝：不给整盘
      授权也不给歧义锚点。POSIX 上 splitdrive 恒无盘符，盘符存在性不可
      作判据，按「绝对 + 非根」语义判。
    """
    normalized = os.path.normcase(os.path.normpath(str(path or "").strip()))
    _, rest = os.path.splitdrive(normalized)
    if not normalized or not Path(normalized).is_absolute() or rest in ("", "\\", "/", "//"):
        raise ValueError(f"非法名单条目（空路径或整盘根）: {path!r}")
    stored = os.path.normpath(str(path).strip())

    target_dir = tenant_config_dir(tenant_id, base=base)
    target_file = target_dir / "project_whitelist.yaml"
    if (
        add
        and not target_file.is_file()
        and base is None
        and legacy_config_users_base().joinpath(tenant_id, "project_whitelist.yaml").is_file()
    ):
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_config_users_base() / tenant_id / "project_whitelist.yaml", target_file)

    data: dict[str, Any] = {}
    if target_file.is_file():
        try:
            loaded = yaml.safe_load(target_file.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError as exc:
            logger.error("授权名单解析失败（重建文件，legacy 播种已保留条目语义）: %s — %s", target_file, exc)

    entries = [str(e) for e in (data.get(section) or []) if str(e or "").strip()]
    if add:
        if normalized not in [os.path.normcase(os.path.normpath(e)) for e in entries]:
            entries.append(stored)
    else:
        entries = [e for e in entries if os.path.normcase(os.path.normpath(e)) != normalized]
    data[section] = entries

    target_dir.mkdir(parents=True, exist_ok=True)
    target_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.info("[zone_policy] 名单节变更 | section=%s | add=%s | entry=%s", section, add, stored)
    return entries


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
    return canon_path(str(real))


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
    mode_tags: list[str] | None = None,
    default_mode: str | None = None,
) -> tuple[ProjectModel, bool]:
    """按路径幂等登记项目：同路径已登记 → 复用（created=False），否则建文件夹 + 登记。

    多入口共用（projects 域 API / task_submit 创建挂靠 / 会话目录登记）：
    登记簿是 id ↔ 路径最薄账本，同一文件夹不应产生多条登记——重复提交
    返回既有登记行，调用方按 created 区分新建与复用。

    mode_tags/default_mode（批 B8）：mode_tags=None 表示调用方未声明（复用
    既有行不动其登记）；显式传入（含空列表 = 清空）时新建即登记、复用即补挂
    （存量项目经本幂等复用通道补登记，ADR 2026-10-09 批 B8）——校验在
    save 写面统一执法。技能播种由调用方在登记成功后调 seed_mode_skills。
    """
    if registry is None:
        registry = ProjectRegistry()
    tags_arg = [str(t) for t in mode_tags] if mode_tags is not None else None
    default_arg = str(default_mode) if default_mode is not None else None
    if tags_arg is not None or default_arg is not None:
        # 声明了 mode 维度 → 建文件夹/改登记行之前先校验（失败原子，不留孤儿）
        validate_mode_registration(tags_arg or [], default_arg or "")
    explicit = explicit_path or ""
    if explicit:
        want = _canonical_path(explicit)
        for project in registry.list():
            if project.path and _canonical_path(project.path) == want:
                if tags_arg is not None:
                    project.mode_tags = tags_arg
                if default_arg is not None:
                    project.default_mode = default_arg
                if tags_arg is not None or default_arg is not None:
                    project.updated_at = datetime.now().isoformat()
                    registry.save(project)
                return project, False
        # 新登记才过闸（幂等复用先于白名单：存量登记不受新闸影响）
        if match_registration_scope(want, load_zone_sections()["entries"]) is None:
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
        mode_tags=tags_arg or [],
        default_mode=default_arg or "",
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
    # 只读解锁回调双签名：onexc(func, path, exc)=3.12+ 增补 API；
    # onerror(func, path, excinfo)=3.11 及以下（未删除，仅弃用）。
    # CI（3.11）与本地（3.12/3.14）并存，按签名可用性分发（不猜版本号）。
    import inspect
    import shutil

    if "onexc" in inspect.signature(shutil.rmtree).parameters:
        shutil.rmtree(target, onexc=_rmtree_onexc)  # type: ignore[call-arg]
    else:

        def _onerror(func: Any, path: str, excinfo: Any) -> None:
            # onerror 的 excinfo 是 (type, value, tb) 元组
            _rmtree_onexc(func, path, excinfo[1])

        shutil.rmtree(target, onerror=_onerror)
    return True


# ════════════════════════════════════════════════════════════
# 容器任务实体遗留数据清除·目录半（一次性语义，幂等执行）
# ════════════════════════════════════════════════════════════


def purge_legacy_workspace_dirs() -> int:
    """清除工作空间基目录下 ``container_*`` 隔离副本目录（幂等）。

    T6a 拆分说明：原 purge_legacy_container_data 的任务行半（container 行删除
    + 子任务挂靠脱钩）是纯 TaskStorage 操作，已内联回 tasks 插件启动序列；
    本函数只保留目录半——工作空间基目录解析（workspace_base_dir）随登记簿
    归本插件，经 project.purge_legacy_dirs 服务供 tasks 消费。

    Returns:
        本次删除的目录数（删除失败的目录 warning 留痕，下次启动重试）。
    """
    import shutil

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
    if removed_dirs:
        logger.info("[projects] 容器任务遗留目录清除 | dirs=%s", removed_dirs)
    return removed_dirs
