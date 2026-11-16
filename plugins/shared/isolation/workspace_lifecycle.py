"""工作空间统一生命周期管理"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

# git ops 以平铺混入模块承载（与本文件同目录，导入即生效）：
# WorkspaceLifecycleManager = _GitOpsMixin + 本文件的启动/持久化/清理。
from _workspace_git_ops import _GitOpsMixin, _safe_ws_name  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspaceLifecycleManager",
    "_safe_ws_name",
    "skill_source_dirs",
]


def skill_source_dirs(
    base_path: Path,
    injected: list[Path] | None = None,
) -> list[tuple[Path, str]]:
    """技能源目录四源并集（升优先序，附来源标注；快照复制与 skill 注册表共用单点）。

    源从「项目根 skills/ 单源」扩为「根 ∪ 模式包 skills/」（批 E §3.1 用户
    裁定），来源标注供消费方区分出厂/模式包/用户技能（X11-F1）。升优先序
    排列供调用方按技能名去重（后源同名覆盖前源 = 用户副本赢）：

    1. ``repo_root`` —— ``<base_path>/skills``（仓根/项目根技能）；
    2. ``mode:<plugin_id>`` —— 出厂包 ``<共享根>/mode_*/skills/``（共享根由
       本文件位置绝对推导，与 mode_keys 出厂根同式，不依赖 cwd）；
    3. ``mode:<plugin_id>`` —— 用户根 ``<USER_ROOT>/plugins/modes/mode_*/
       skills/``（经 user_space 解析，不可得 → 无用户源，与 mode_keys 同口径）；
    4. ``user_root`` —— ``<USER_ROOT>/skills/``（装机形态的根技能权威源：
       装机包 resources/ 不含仓根 skills/（R300 实证：agentos.yaml 引用
       skill-container-task-flow 而包内无实体 → 模型到用户根拼绝对路径
       File not found），sync_installed_user_space --skills 把仓根技能镜像
       到该目录，会话工作区经此源拿到全量技能）。

    injected 非 None = 测试/隔离环境的注入面（替换两处包根推导与用户根
    推导，不叠加；注入语义是"替换推导"；条目标注 ``injected``，无来源语义）。

    消费方：WorkspaceLifecycleManager._skill_sources（快照复制，只取目录）
    与 skill_manager 注册表（skill.list / skill.get，目录+标注同源消费）
    ——两侧禁止另写目录推导，防再漂移（X11-F1）。
    """
    sources: list[tuple[Path, str]] = [(base_path / "skills", "repo_root")]
    if injected is not None:
        return sources + [(p, "injected") for p in injected]
    factory_shared = Path(__file__).resolve().parents[1]
    sources.extend(
        (pkg / "skills", f"mode:{pkg.name}")
        for pkg in sorted(factory_shared.glob("mode_*"))
        if (pkg / "skills").is_dir()
    )
    user_modes = None
    try:
        from _root_api import user_space  # noqa: PLC0415

        user_plugins = user_space.user_plugins_dir()
        user_modes = Path(user_plugins) / "modes" if user_plugins else None
    except Exception as exc:  # user_space 不可得 → 无用户源（与 mode_keys 同口径）
        logger.debug("[WorkspaceLifecycle] user_space 不可得（仅仓根+出厂包源）| err=%s", exc)
    if user_modes is not None:
        sources.extend(
            (pkg / "skills", f"mode:{pkg.name}")
            for pkg in sorted(user_modes.glob("mode_*"))
            if (pkg / "skills").is_dir()
        )
    try:
        from _root_api import user_space  # noqa: PLC0415

        user_root = user_space.user_root()
        if user_root is not None:
            sources.append((Path(user_root) / "skills", "user_root"))
    except Exception as exc:  # 与上方用户根推导同口径：不可得即无此源
        logger.debug("[WorkspaceLifecycle] 用户根 skills/ 源解析失败（跳过）| err=%s", exc)
    return sources


class WorkspaceLifecycleManager(_GitOpsMixin):
    """工作空间统一生命周期管理器"""

    def __init__(
        self,
        resource_merge: Any,
        config: dict[str, Any],
        task_tree: Any,
        ws_meta_store: Any,
        base_path: str,
        mode_skill_sources: list[Path] | None = None,
    ):
        """初始化工作空间生命周期管理器。

        mode_skill_sources：模式包技能源目录清单（每项为一个含技能子目录的
        `skills/` 目录），None = 按仓内布局推导（出厂 `plugins/shared/mode_*/
        skills/`，拍平布局 ADR 2026-10-13 + 用户根 `<USER_ROOT>/plugins/modes/
        mode_*/skills/` 装机布局不动）；
        显式传入 = 测试/隔离环境的注入面（替换推导，不叠加）。
        """
        self._resource_merge = resource_merge
        self._config = config
        self._task_tree = task_tree
        self._ws_meta_store = ws_meta_store
        self._base_path = Path(base_path)
        self._mode_skill_sources = mode_skill_sources
        # 按 project_root 粒度的并发锁
        self._merge_locks: dict[str, Any] = {}
        self._global_lock = __import__("threading").Lock()
        # 记录项目根目录的主分支，用于守卫 auto-save 不写入错误分支
        self._main_branch: str = ""
        try:
            self._record_main_branch()
        except Exception:
            logger.warning("[WorkspaceLifecycle] __init__ 中记录主分支失败", exc_info=True)

    # ── 任务启动 ──────────────────────────────────────────────

    def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """任务启动时的生命周期钩子，根据 is_root 分发到子任务或根任务处理"""
        self._record_main_branch()
        self.restore_ws_meta(task_id)
        existing = self._ws_meta_store.get(task_id)
        if existing and existing.get("mode"):
            ws_path = existing.get("path", "")
            if ws_path and Path(ws_path).exists():
                logger.debug(
                    "[WorkspaceLifecycle] 复用已有工作空间: task_id=%s, mode=%s, path=%s",
                    task_id,
                    existing.get("mode"),
                    ws_path,
                )
                self._copy_skills_to_workspace(ws_path)
                return existing
            logger.debug(
                "[WorkspaceLifecycle] 已有 ws_meta 但路径不存在，重新创建: task_id=%s, path=%s",
                task_id,
                ws_path,
            )
        if not task_data.get("is_root", True):
            meta = self._start_subtask(task_id, workspace, task_data)
        else:
            meta = self._start_root_task(task_id, workspace, task_data)
        self._copy_skills_to_workspace(meta["path"])
        return meta

    def on_session_start(self, workspace: str) -> None:
        """主会话（无任务身份）工作区就绪：确保目录存在并同步 skills 快照。

        主会话工作区 = 配置的工作空间根（workspace.root，任务工作区的父目录），
        仓库根不得作为会话工作区（agent 读写面不得触及项目源码树）。
        skills 同源同机制：文件工具与提示词引用的 skills/... 相对路径在此
        目录内解析（与任务管道 _copy_skills_to_workspace 同一复制例程）。
        """
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._copy_skills_to_workspace(workspace)

    def _start_subtask(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """子任务启动：共享父工作空间。

        拓扑由 workspace_mode 显式声明决定（与隔离解耦）：
        - 显式 plain（_has_explicit_workspace 标记，agent/调用方指定）→
          直接共享宿主目录（挂项目任务的 workspace 已解析为项目文件夹）；
        - 其余（缺省 plain / worktree / 未声明）→ 共享父任务工作空间，
          父链坐标为权威。解析顺序：① 出生契约继承（lineage.parent_ws_meta，
          task_submit 提交时随出生 state 写全——无时序窗口）；② 父链聚合
          查找（task_tree ws_meta，补继承缺失的场景）。两者皆缺 = 坐标
          不可知，显式报错，不回退入参目录（静默回退会让同会话子任务
          工作空间漂移）。
        """
        if (
            task_data.get("_has_explicit_workspace")
            and task_data.get("workspace_mode", "") == "plain"
            and workspace
        ):
            meta = {"mode": "shared", "path": workspace}
            self._ws_meta_store[task_id] = meta
            logger.debug(
                "[WorkspaceLifecycle] plain 拓扑(子任务): 共享目录 task_id=%s, path=%s",
                task_id,
                workspace,
            )
            return meta

        # 显式 workspace（挂项目/调用方指定源目录）的 worktree 子任务：在源
        # 目录（项目文件夹）上建隔离 worktree（与根任务 worktree 同构）——
        # 项目挂靠语义 = 任务工作空间锚定项目文件夹；父链继承只是"无显式
        # 坐标"的缺省，不得覆盖显式项目锚（2026-08-30 诊断：挂 project 的
        # 子任务 worktree 被继承父工作空间吞掉，任务跑进会话目录）。
        if (
            task_data.get("_has_explicit_workspace")
            and task_data.get("workspace_mode", "") == "worktree"
            and workspace
        ):
            ws_base = self._get_workspace_root()
            root_path = Path(workspace)
            if not root_path.exists():
                root_path.mkdir(parents=True, exist_ok=True)
            self._prepare_root_repo(root_path, task_id)
            meta = self._create_worktree_root(ws_base, root_path, task_id)
            self._ws_meta_store[task_id] = meta
            logger.info(
                "[WorkspaceLifecycle] worktree 拓扑(子任务): 源目录建隔离副本 "
                "task_id=%s, source=%s, path=%s",
                task_id,
                root_path,
                meta["path"],
            )
            return meta

        inherited = task_data.get("_inherited_parent_ws_meta")
        inherited_path = ""
        inherited_root = ""
        if isinstance(inherited, dict):
            inherited_path = str(inherited.get("path") or "")
            inherited_root = str(inherited.get("project_root") or "")
        if inherited_path:
            meta = {
                "mode": "shared",
                "path": inherited_path,
                "parent_workspace": workspace,
                "project_root": inherited_root,
            }
            self._ws_meta_store[task_id] = meta
            logger.info(
                "[WorkspaceLifecycle] plain 拓扑(子任务): 出生契约继承父工作空间 "
                "task_id=%s, path=%s",
                task_id,
                inherited_path,
            )
            return meta

        parent_path = ""
        parent_meta: dict = {}
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.parent_task_id:
                parent_id = task.parent_task_id
                self.restore_ws_meta(parent_id)
                parent_meta = self._ws_meta_store.get(parent_id, {})
                parent_path = str(parent_meta.get("path") or "")
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] _start_subtask 查找父任务失败: task_id=%s, error=%s", task_id, e)

        if not parent_path:
            raise RuntimeError(
                f"子任务 {task_id} 父链工作空间解析失败：出生契约无 "
                f"lineage.parent_ws_meta，父链聚合查找亦无 ws_meta"
                f"（无降级——拒绝静默回退独立目录）"
            )

        meta = {
            "mode": "shared",
            "path": parent_path,
            "parent_workspace": workspace,
            "project_root": parent_meta.get("project_root", ""),
        }
        self._ws_meta_store[task_id] = meta
        return meta

    # ── 技能文件复制 ──────────────────────────────────────────────

    def _skill_sources(self) -> list[Path]:
        """技能同步源目录清单（升优先序：仓根 → 出厂模式包 → 用户根模式包 → 用户根 skills/）。

        目录解析抽公共函数 skill_source_dirs（快照复制与 skill_manager 注册
        表同源消费，X11-F1），本方法只做快照面适配（丢弃来源标注）；
        mode_skill_sources 注入与用户根推导语义见其 docstring。
        """
        return [
            path for path, _source in skill_source_dirs(self._base_path, self._mode_skill_sources)
        ]

    def _copy_skills_to_workspace(self, ws_path: str) -> None:
        """将技能快照同步到工作空间（按技能粒度增量）。

        源 = 项目根 skills/ ∪ 模式包 skills/（出厂 + 用户根）∪ 用户根 skills/
        （见 _skill_sources）；
        同名优先序与双根解析一致：用户根包技能 > 出厂包技能 > 仓根。任务/
        会话启动时调用一次，让 Agent 在 host / worktree / Docker 容器所有
        模式下都能通过 skills/<技能名>/… 访问技能；已有技能保持原样仅补缺。
        """
        skills_dst = Path(ws_path) / "skills"
        base_skills = self._base_path / "skills"
        # 身份比较（工作空间即项目目录 → 跳过复制）统一走 resolve_path_identity：
        # 裸 Path.resolve() 对 WinFsp 等用户态挂载卷抛 WinError 1005，阻断任务
        # 派发（卷本身可用，stat/mkdir 正常——2026-10-19 真机实锤）；降级语义
        # 见 workspace.resolve_path_identity（两侧同函数，保比较对称性）。
        from isolation.workspace import resolve_path_identity  # noqa: PLC0415

        if base_skills.is_dir() and resolve_path_identity(base_skills) == resolve_path_identity(
            skills_dst
        ):
            logger.debug(
                "[WorkspaceLifecycle] 工作空间即为项目目录，skills/ 已在原位，跳过复制: %s",
                skills_dst,
            )
            return
        # 名 → 源目录：升优先序遍历，后源同名覆盖前源（用户副本赢）
        picked: dict[str, Path] = {}
        dst_identity = resolve_path_identity(skills_dst)
        for source in self._skill_sources():
            if not source.is_dir() or resolve_path_identity(source) == dst_identity:
                continue
            for skill_src in source.iterdir():
                if skill_src.is_dir():
                    picked[skill_src.name] = skill_src
        if not picked:
            logger.debug(
                "[WorkspaceLifecycle] 无任何技能源（项目根与模式包均无 skills/），跳过复制"
            )
            return
        skills_dst.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name, skill_src in sorted(picked.items()):
            skill_dst = skills_dst / name
            if skill_dst.exists():
                continue  # 已有技能保持原样，仅补齐缺失项
            try:
                shutil.copytree(skill_src, skill_dst, symlinks=True)
                copied.append(name)
            except Exception as exc:
                logger.warning(
                    "[WorkspaceLifecycle] 技能复制失败: %s → %s | error=%s",
                    skill_src,
                    skill_dst,
                    exc,
                )
        if copied:
            logger.debug(
                "[WorkspaceLifecycle] 技能已增量同步 → %s | new=%s",
                skills_dst,
                copied,
            )

    def _resolve_ws_mode(self, task_data: dict) -> Any:
        """workspace_mode 拓扑决策：显式声明优先；缺省按显式性分叉——
        显式 workspace → worktree（从源项目分叉隔离副本）；无显式 → plain
        （「工作空间根/{task_id}」目录，上游 plugin 已解析路径）。两者均与隔离解耦。
        """
        explicit_mode = task_data.get("workspace_mode", "")
        if explicit_mode:
            return explicit_mode
        if task_data.get("_has_explicit_workspace", False):
            return self._config.get("workspace", {}).get("default_mode", "worktree")
        return "plain"

    def _inherit_root_workspace(self, task_id: str, workspace: str, task_data: dict) -> dict | None:
        """inherit_workspace_from 解析命中：复用旧任务工作空间并继承其 ws_meta。

        继承 mode/branch/project_root，保持 worktree 生命周期；未命中返回 None。
        """
        if not task_data.get("_inherit_workspace_resolved"):
            return None
        source_ws_meta = task_data.get("_source_ws_meta") or {}
        source_mode = source_ws_meta.get("mode", "shared")
        meta = {
            "mode": source_mode,
            "path": workspace,
            "branch": source_ws_meta.get("branch", ""),
            "project_root": source_ws_meta.get("project_root", ""),
        }
        logger.debug(
            "[WorkspaceLifecycle] inherit: 复用旧工作空间 task_id=%s, workspace=%s, mode=%s, branch=%s",
            task_id,
            workspace,
            source_mode,
            meta.get("branch"),
        )
        self._ws_meta_store[task_id] = meta
        return meta

    def _start_plain_root(self, task_id: str, workspace: str, task_data: dict) -> dict | None:
        """plain 拓扑根任务启动：显式目录 / 场景检测目录 / 缺省空目录。

        未命中 plain（含缺省空目录分支的条件组合）返回 None，交由 worktree 路径。
        """
        _ws_mode = self._resolve_ws_mode(task_data)

        # 显式 workspace 的 plain：直接操作该目录（无 git worktree/branch）
        if _ws_mode == "plain" and workspace:
            ws_path = Path(workspace)
            if not ws_path.exists():
                # 上游解析的「工作区根/{task_id}」路径此时可能未建——工作空间
                # 由本服务负责创建（worktree 路径由 git worktree add 建目录）
                ws_path.mkdir(parents=True, exist_ok=True)
            meta = {
                "mode": "plain",
                "path": workspace,
                "task_id": task_id,
                "project_root": workspace,
            }
            self._ws_meta_store[task_id] = meta
            logger.debug(
                "[WorkspaceLifecycle] plain 拓扑: 直接操作目录 "
                "task_id=%s, path=%s（无 git worktree/branch）",
                task_id,
                workspace,
            )
            return meta

        # 空白 workspace 的 plain：由场景检测结果落位，直接操作项目目录，
        # 保留 task_id + project_root 供 on_before_evaluate 用准确 commit message
        if _ws_mode == "plain":
            _scenario, project_root = self._detect_scenario(workspace, task_data)
            root_path = Path(project_root)
            if not root_path.exists():
                root_path.mkdir(parents=True, exist_ok=True)
            meta = {
                "mode": "shared",
                "path": str(root_path),
                "project_root": str(root_path),
                "task_id": task_id,
            }
            self._ws_meta_store[task_id] = meta
            logger.debug(
                "[WorkspaceLifecycle] PLAIN拓扑: task_id=%s, 直接操作项目目录: %s",
                task_id,
                root_path,
            )
            return meta

        # 非 plain 的无显式任务走 worktree 路径不经过此处
        return None

    def _downgrade_plain_if_not_git_repo(
        self, task_id: str, root_path: Path, project_root: str
    ) -> dict | None:
        """无显式 workspace（源=项目根）的 worktree 前置守卫。

        项目根必须是已提交的 git 仓库——不能在项目根上自动 git init /
        initial commit（会把普通目录改写成仓库，污染用户态）。非 git 仓库
        或无提交 → warn 降级 plain 空目录（fail-honest 不 panic）；否则返回 None。
        """
        rc_wt, out_wt, _ = self._run_git("rev-parse", "--is-inside-work-tree", cwd=root_path)
        rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
        if rc_wt == 0 and out_wt.strip().lower() == "true" and rc_head == 0:
            return None
        ws_base = self._get_workspace_root()
        plain_path = ws_base / task_id
        plain_path.mkdir(parents=True, exist_ok=True)
        meta = {"mode": "plain", "path": str(plain_path)}
        self._ws_meta_store[task_id] = meta
        logger.warning(
            "[WorkspaceLifecycle] 项目根非有效 git 仓库，worktree 无法建立，"
            "降级 plain 模式: task_id=%s, root=%s, rc_worktree=%d, rc_head=%d, path=%s",
            task_id,
            project_root,
            rc_wt,
            rc_head,
            plain_path,
        )
        return meta

    def _prepare_root_repo(self, root_path: Path, task_id: str) -> None:
        """把项目根整理成可建 worktree 的就绪仓库（git init / 补提交 / auto-save 守卫）。

        并发互斥：多个子任务同时派发到同一项目仓时，init/add/commit 会互相
        竞争（index.lock / rev-parse 撞上 HEAD 中间态）——初始化段用进程级
        锁文件串行化（临时目录按项目路径哈希命名，不污染项目内容）；竞争
        等待方在拿锁前先复检 HEAD，其他任务已完成初始化则直接跳过。
        """
        deadline = time.monotonic() + 30.0
        digest = hashlib.sha1(str(root_path).lower().encode("utf-8")).hexdigest()[:16]
        lock_path = Path(tempfile.gettempdir()) / f"agentos_init_{digest}.lock"
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(task_id).encode("utf-8"))
                os.close(fd)
                break
            except FileExistsError:
                if self._root_repo_ready(root_path):
                    logger.info(
                        "[WorkspaceLifecycle] 项目仓已由并发任务完成初始化，跳过: task_id=%s, path=%s",
                        task_id,
                        root_path,
                    )
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"项目空间初始化锁等待超时(30s): task_id={task_id}, path={root_path}"
                    ) from None
                time.sleep(0.5)
        try:
            self._prepare_root_repo_locked(root_path, task_id)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def _root_repo_ready(self, root_path: Path) -> bool:
        """项目仓是否已就绪（.git 存在且 HEAD 可解析）。"""
        if not (root_path / ".git").exists():
            return False
        rc, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
        return rc == 0

    def _prepare_root_repo_locked(self, root_path: Path, task_id: str) -> None:
        """初始化段本体（调用方持有 _prepare_root_repo 的互斥锁）。"""
        if not (root_path / ".git").exists():
            if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                raise RuntimeError(f"项目空间初始化失败（git init）: task_id={task_id}, path={root_path}")
            return

        self._ensure_git_user(root_path)
        rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
        if rc_head != 0:
            logger.debug(
                "[WorkspaceLifecycle] .git 存在但无提交，执行 initial commit: task_id=%s, path=%s",
                task_id,
                root_path,
            )
            if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                raise RuntimeError(
                    f"项目空间初始化失败（已有 .git 但无提交记录）: task_id={task_id}, path={root_path}"
                )
        elif self._guard_root_branch(root_path):
            # 此 auto-save 提交的是项目根目录上残留的脏改动，
            # 来源可能是用户、其他 host 任务、上一次中断的执行——不是当前任务。
            # 用中性 message，避免给本任务"贴上"不属于它的改动。
            self._autosave_before_worktree(
                root_path, "chore: auto-save dirty working tree before worktree creation", task_id
            )
        else:
            logger.warning("[WorkspaceLifecycle] 跳过项目根目录 auto-save: 分支守卫检测到变更, task_id=%s", task_id)

    def _create_worktree_root(self, ws_base: Path, root_path: Path, task_id: str) -> dict:
        """在就绪仓库上建任务 worktree，返回 worktree 模式的 ws_meta。

        全量 checkout 由 git 自管（共享对象库，只展开 tracked 文件）——
        不预扫项目大小做 sparse 决策。

        落地校验（2026-09-14 用户裁定：建了什么目录就把什么目录写进 state，
        使用时只查 state 不重算）：worktree add 之后必须确认目录真实存在，
        未落地即抛错让任务在出生期失败——否则任务拿着幻影路径起跑，直到
        收尾合并才炸 267（实测 repo 根锚定场景：执行降级仓库根 + 合并门控
        找不到分化目录，任务被误判 failed）。

        基目录在拼装前经 resolve_workspace_base 收口：与源仓库跨盘时退化到
        项目所在盘的存放点（git worktree 的同文件系统硬约束，见
        isolation/workspace.py 与 ADR 草稿 2026-10-17-workspace-crossdrive-fallback）。
        """
        from isolation.workspace import resolve_workspace_base  # noqa: PLC0415

        branch = f"task/{task_id}"
        ws_base = resolve_workspace_base(ws_base, root_path)
        ws_dir = ws_base / _safe_ws_name(root_path.name, task_id)
        self._worktree_add_with_repair(root_path, branch, ws_dir, task_id)
        if not ws_dir.is_dir():
            raise RuntimeError(
                f"worktree 分化未落地: task_id={task_id}, 期望目录不存在: {ws_dir} "
                f"（源仓库: {root_path}）。拒绝以幻影工作空间起跑任务。"
            )
        self._ensure_git_user(ws_dir)
        return {"mode": "worktree", "path": str(ws_dir), "branch": branch, "project_root": str(root_path)}

    def _start_root_task(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """根任务启动：场景A(新项目) / 场景B(无.git) / 场景C(有.git)。"""
        inherited = self._inherit_root_workspace(task_id, workspace, task_data)
        if inherited is not None:
            return inherited

        plain = self._start_plain_root(task_id, workspace, task_data)
        if plain is not None:
            return plain

        scenario, project_root = self._detect_scenario(workspace, task_data)
        root_path = Path(project_root)
        has_explicit_workspace = task_data.get("_has_explicit_workspace", False)

        # 无显式 workspace（源=项目根）时才做 worktree 前置守卫；显式 workspace
        # 的场景A（非 git 目录）由 _prepare_root_repo 走 git init。
        if not has_explicit_workspace:
            downgraded = self._downgrade_plain_if_not_git_repo(task_id, root_path, project_root)
            if downgraded is not None:
                return downgraded

        logger.debug(
            "[WorkspaceLifecycle] _start_root_task: task_id=%s, scenario=%s, workspace=%s, root_path=%s",
            task_id,
            scenario,
            workspace,
            root_path,
        )

        ws_base = self._get_workspace_root()
        if not root_path.exists():
            root_path.mkdir(parents=True, exist_ok=True)
        self._prepare_root_repo(root_path, task_id)
        meta = self._create_worktree_root(ws_base, root_path, task_id)
        self._ws_meta_store[task_id] = meta
        return meta

    # ── ws_meta 恢复（只读契约）────────────────────────────────

    def restore_ws_meta(self, task_id: str):
        """从任务侧恢复 ws_meta 进内存 store（只读契约，2026-10-08 写路径退役）。

        数据源 = task_tree 侧的任务视图（生产实现为 pipeline 插件的 state 直读
        适配器，读聚合行单键 task.ws_meta）；持久化权威在 state 单键，本进程
        不再有任何写通道（原 _persist_ws_meta 经 task.metadata 的 YAML 写面已
        删除——双写歧路，state 单一真相源收敛 F13）。此处只做本进程内的
        「恢复→复用」（同任务重新 init 时按已存坐标复用工作空间，不重建）。
        """
        if task_id in self._ws_meta_store:
            return
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.metadata:
                saved = task.metadata.get("ws_meta")
                if saved:
                    self._ws_meta_store[task_id] = saved
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] restore_ws_meta 失败: task_id=%s, error=%s", task_id, e)
