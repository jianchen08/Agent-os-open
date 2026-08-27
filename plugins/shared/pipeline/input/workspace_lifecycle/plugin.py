"""工作空间生命周期 Input 插件。

挂载在管道的 init / exit 循环体（多循环体模型），按 `state["current_phase"]`
分发两个阶段。**插件自己持有工作空间服务**（WorkspaceLifecycleManager），
自己创建、给 agent 使用、自己清理——不依赖跨进程 capability：

- **init（bootstrap）**：消费 `state.execution_context.workspace`
  （`{source_path, mode}`，由 task_submit / 会话创建参数解析注入）。有任务
  上下文（state["task.id"]，== pipeline_id 的任务身份权威键）时调
  `on_task_start` 真实创建空间（worktree/plain）；
  主会话（无任务）直接解析 source_path 写 state。结果写入
  `state.workspace` / `project_root` / `ws_meta`。幂等：state 已有 workspace
  则跳过。服务不可用时降级为纯解析（不阻断管道）。
- **exit（finalize）**：有任务且 ws_meta.mode=worktree 时调
  `merge_worktree_before_complete` 合并回源空间；否则 no-op。失败留痕不阻断。

与隔离解耦：本插件只管"在哪个目录执行"（拓扑），执行环境（容器/宿主）由
environment_lifecycle / isolation_guard 决策。

State 命名空间：
    - workspace / project_root / ws_meta：init 阶段写入
    - workspace_finalized：exit 阶段写入
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


def _ensure_isolation_path() -> None:
    """把 isolation 插件目录加入 sys.path（workspace_lifecycle 服务所在）。"""
    _here = Path(__file__).resolve().parent
    _system_dir = _here.parents[2] / "system"
    _iso_dir = _system_dir / "isolation"
    for _p in (str(_system_dir), str(_iso_dir)):
        if _p not in sys.path:
            sys.path.insert(0, _p)


# ── state 聚合读取器（server.py on_load 注入，pipeline-state capability）──
_state_reader: Any = None
# 最近一次聚合行快照（async 上下文刷新，sync 消费端只读缓存——
# 消费链 task_tree.get_task 是同步库接口，直接调用 async reader 会产生
# 永不 await 的协程（RuntimeWarning）且恒降级为空）
_state_rows_cache: list[dict[str, Any]] = []


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（sidecar on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


async def refresh_state_rows() -> None:
    """在 async 上下文刷新聚合行快照（execute 入口调用，供 sync 消费端读取）。"""
    global _state_rows_cache  # noqa: PLW0603
    reader = _state_reader
    if reader is None:
        return
    try:
        rows = reader()
        if asyncio.iscoroutine(rows):
            rows = await rows
        if isinstance(rows, list):
            _state_rows_cache = [r for r in rows if isinstance(r, dict)]
    except Exception:
        pass


class _ExecutionContextTaskTree:
    """task_tree 接口的 state 直读实现（任务 = 管道 state 单一真值）。

    服务内部 `_start_subtask` 依赖 `task_tree.get_task(task_id)` 返回带
    `parent_task_id` / `metadata` 的对象；本实现直接读管道 state 聚合行
    （lineage.parent_pipeline_id = 父链），不仿真库对象：

    - `get_task(当前任务)` → `parent_task_id`（state 顶层扁平键
      `lineage.parent_pipeline_id`，引擎出生写入）；
    - 聚合不可用 → None（服务内部已有 try/except 兜底）。
    """

    def __init__(self, plugin: WorkspaceLifecyclePlugin, manager: Any) -> None:
        self._plugin = plugin
        self._manager = manager

    def _read_rows(self) -> list[dict[str, Any]]:
        # sync 消费端只读缓存：聚合行由 refresh_state_rows() 在 async 上下文
        # （execute 入口）刷新——不在此调用 reader（async reader 会产生永不
        # await 的协程，见 refresh_state_rows 注释）
        return list(_state_rows_cache)

    def get_task(self, task_id: str):
        state = self._plugin._last_state or {}
        # 0.2 统一：任务身份 = pipeline_id，引擎注入 state 的扁平键是 task.id（点号键）
        current_id = state.get("task.id")
        rows = self._read_rows()
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None and task_id != current_id:
            return None
        if task_id == current_id and row is None:
            # 当前管道行缺失时用本 state 的 lineage 扁平键兜底
            parent_id = str(state.get("lineage.parent_pipeline_id") or "")
            return SimpleNamespace(id=task_id, parent_task_id=parent_id or None, metadata={})
        # 上方两分支已保证：row 为 None 时（无论 task_id 是否等于 current_id）均已
        # return——此处 row 必非 None，assert 仅供类型收窄。
        assert row is not None
        parent_id = str(row.get("lineage.parent_pipeline_id") or "")
        return SimpleNamespace(id=task_id, parent_task_id=parent_id or None, metadata={})

    def save_task(self, task: Any) -> Any:
        """持久化 no-op（ws_meta 由 state 承载——YAML 只读镜像，统一后不写）。"""
        return task


class WorkspaceLifecyclePlugin(IInputPlugin):
    """工作空间生命周期插件：init 创建空间，exit 合并清理（自持服务）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化。

        Args:
            config: 插件配置（base_path / workspace 配置等）
        """
        self._config = config or {}
        self._manager: Any = None
        self._last_state: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "workspace_lifecycle"

    @property
    def priority(self) -> int:
        """插件执行优先级（init/exit 体单次执行，置于链首）。"""
        return self._config.get("priority", 5)

    # ── 服务对象（懒加载，插件进程内自持）──────────────────────

    def _get_manager(self, base_path_hint: str | None = None) -> Any | None:
        """懒加载 WorkspaceLifecycleManager（服务不可用时返回 None，降级纯解析）。

        task_tree 用 execution_context 适配器（0.2 sidecar 无 task_service）：
        容器直接子任务经 parent_task_id 重建父链，定位容器空间。

        base_path_hint：工作流服务的基础路径（=项目根）。sidecar 的 cwd 是插件
        目录（invoker with_working_dir），绝不能回退 cwd——0.2 下从
        execution_context.workspace.source_path 传入（task 创建时带项目根）。
        首次传入后缓存；空 hint 且未缓存时回退安全值（不误用插件目录）。
        """
        if self._manager is not None:
            return self._manager
        try:
            _ensure_isolation_path()
            from isolation.workspace import find_project_root  # noqa: PLC0415
            from isolation.workspace_lifecycle import WorkspaceLifecycleManager  # noqa: PLC0415

            base_path = (
                base_path_hint
                or self._config.get("base_path")
                or str(find_project_root())
            )
            manager = WorkspaceLifecycleManager(
                resource_merge=None,
                config=self._config.get("workspace_config", {}),
                task_tree=_ExecutionContextTaskTree(self, None),  # type: ignore[arg-type]
                ws_meta_store={},
                base_path=base_path,
            )
            # 适配器需要 manager 推导容器空间路径，构造后回填
            manager._task_tree._manager = manager  # type: ignore[attr-defined]
            self._manager = manager
            logger.info("[WorkspaceLifecycle] 工作空间服务已实例化 | base_path=%s", base_path)
        except Exception as exc:
            logger.warning(
                "[WorkspaceLifecycle] 工作空间服务实例化失败，降级为纯解析 | error=%s",
                exc,
            )
            self._manager = None
        return self._manager

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """按 current_phase 分发 init/exit 阶段。"""
        self._last_state = ctx.state
        # 刷新聚合行快照：init/exit 体内 task_tree.get_task 走 sync 缓存读
        await refresh_state_rows()
        phase = ctx.state.get("current_phase", "")
        if phase == "init":
            return await self._bootstrap(ctx)
        if phase == "exit":
            return await self._finalize(ctx)
        # 其它循环体（main）：生命周期插件不参与，零产出
        return PluginResult()

    # ── init：创建/解析工作空间 ────────────────────────────────

    async def _bootstrap(self, ctx: PluginContext) -> PluginResult:
        """创建或解析工作空间。

        有任务上下文 → 服务真实创建（worktree/plain）；主会话 → 纯解析。
        幂等：state 已有 workspace（恢复/复用）时跳过。
        """
        state = ctx.state
        _dbg = {
            k: str(state.get(k))[:120]
            for k in (
                "task_id", "task.id", "pipeline_id",
                "execution_context", "execution_context.workspace",
                "workspace", "project_root",
            )
            if state.get(k) is not None
        }
        logger.info("[WorkspaceLifecycle] bootstrap state keys=%s", _dbg)
        if state.get("workspace"):
            logger.debug(
                "[WorkspaceLifecycle] workspace 已就位，跳过创建 | workspace=%s",
                state["workspace"],
            )
            return PluginResult()

        ec = state.get("execution_context")
        if not isinstance(ec, dict):
            logger.debug("[WorkspaceLifecycle] 无 execution_context，跳过工作空间创建")
            return PluginResult()
        ws_spec = ec.get("workspace")
        if not isinstance(ws_spec, dict):
            logger.debug("[WorkspaceLifecycle] execution_context 无 workspace 声明，跳过")
            return PluginResult()

        source_path = ws_spec.get("source_path") or ""
        # 模式未指定 → 默认 worktree（不是 plain）。
        mode = ws_spec.get("mode") or "worktree"
        # 0.2 统一：任务身份 = pipeline_id，引擎注入 state 的扁平键是 task.id
        # （点号键）。缺 task 上下文 = 主会话纯解析。
        task_id = state.get("task.id") or ""
        # 工作流服务基础路径 = 项目根：sidecar cwd 是插件目录（非项目根），
        # 用 source_path（task 创建时带的项目根）作为 base_path 修正。
        manager = self._get_manager(base_path_hint=source_path)
        if not source_path and task_id:
            # 默认工作空间语义：无显式 workspace 声明的任务在
            # 「工作空间根/{task_id}」下创建目录——默认隔离执行的工作空间
            # （容器挂载与 bash 执行均以此为锚）。
            try:
                _ensure_isolation_path()
                from isolation.workspace import (  # noqa: PLC0415
                    find_project_root,
                    get_workspace_base_dir,
                )

                if mode == "worktree":
                    # worktree 拓扑：workspace 参数是**源项目**（服务自动在
                    # 工作区根下建隔离副本）；以项目根为源。
                    source_path = str(find_project_root())
                else:
                    # plain 拓扑：直接在「工作区根/{task_id}」目录操作（默认隔离）
                    source_path = str(get_workspace_base_dir() / task_id)
                logger.info(
                    "[WorkspaceLifecycle] 无显式 workspace，按默认根创建 | task=%s | mode=%s | path=%s",
                    task_id,
                    mode,
                    source_path,
                )
            except Exception as exc:
                logger.warning(
                    "[WorkspaceLifecycle] 默认工作空间根解析失败 | error=%s", exc
                )
        if not source_path:
            logger.debug("[WorkspaceLifecycle] workspace source_path 为空，跳过")
            return PluginResult()

        if manager is not None and task_id:
            # 任务管道：调工作空间服务真实创建（对齐 task_executor 契约：
            # on_task_start(task_id, workspace, task_data) 分发 root/subtask）。
            # task_data 字段形态与 task_submit → task_data 一致。
            task_data = {
                "is_root": True,
                "workspace_mode": mode,
                "isolation_mode": (ec.get("isolation") or {}).get("level", ""),
                "_has_explicit_workspace": bool(ws_spec.get("explicit")),
                "_inherit_workspace_resolved": False,
            }
            try:
                ws_meta = await ctx_await(manager.on_task_start, task_id, source_path, task_data)
                if isinstance(ws_meta, dict) and ws_meta.get("path"):
                    updates = {
                        "workspace": ws_meta["path"],
                        "project_root": ws_meta.get("project_root") or ws_meta["path"],
                        "ws_meta": ws_meta,
                    }
                    logger.info(
                        "[WorkspaceLifecycle] init 服务创建工作空间 | task=%s | mode=%s | path=%s",
                        task_id,
                        ws_meta.get("mode"),
                        ws_meta["path"],
                    )
                    return PluginResult(state_updates=updates)
            except Exception as exc:
                logger.warning(
                    "[WorkspaceLifecycle] 工作空间创建失败，降级为源路径 | task=%s | error=%s",
                    task_id,
                    exc,
                )
        # 降级/主会话：直接使用源路径（plain 语义）。
        # 无显式 workspace 时 mode 统一按 plain 落 ws_meta——服务不可用时没有
        # worktree 被创建，声明 worktree 会造成"无 workspace 却 worktree"的
        # 虚假标记（exit 会据此尝试 merge）。对齐服务层矫正
        # （WorkspaceLifecycleManager._start_root_task：无显式 workspace → 强制
        # plain 目录）。
        # 无显式 workspace + worktree 模式时 source_path 已被解析为项目根
        # （worktree 的源）——服务不可用降级时**不能把项目根直接当 workspace**
        # （任务会在项目根上直接读写），回退「工作区根/{task_id}」占位目录
        # （同 plain 默认位置，`.ai_workspaces/` 下）。
        _effective_mode = mode
        _effective_path = source_path
        if not ws_spec.get("explicit"):
            if mode == "worktree":
                try:
                    _ensure_isolation_path()
                    from isolation.workspace import get_workspace_base_dir  # noqa: PLC0415

                    # 统一基目录解析（配置驱动：绝对路径原样，相对路径相对项目根）
                    _effective_path = str(get_workspace_base_dir() / task_id)
                except Exception as _exc:  # noqa: BLE001
                    # 配置读取失败：沿用 source_path（项目根）已是最坏兜底
                    logger.warning(
                        "[WorkspaceLifecycle] 降级路径配置根解析失败，沿用项目根 | error=%s",
                        _exc,
                    )
            _effective_mode = "plain"
        updates = {
            "workspace": _effective_path,
            "project_root": _effective_path,
            "ws_meta": {
                "mode": _effective_mode,
                "path": _effective_path,
                "project_root": _effective_path,
            },
        }
        logger.info(
            "[WorkspaceLifecycle] init 解析工作空间 | mode=%s | path=%s",
            mode,
            _effective_path,
        )
        return PluginResult(state_updates=updates)

    # ── exit：合并/清理（服务自持，真实执行）──────────────────

    async def _finalize(self, ctx: PluginContext) -> PluginResult:
        """工作空间收尾：worktree 合并回源空间。

        有任务且 ws_meta.mode=worktree → 调服务 merge_worktree_before_complete；
        其余（plain/主会话）no-op。失败留痕不阻断（收尾类操作不得让 run 翻车）。
        """
        state = ctx.state
        ws_meta = state.get("ws_meta") if isinstance(state.get("ws_meta"), dict) else {}
        if ws_meta.get("mode") != "worktree":
            return PluginResult()
        task_id = state.get("task.id") or ""
        if not task_id:
            return PluginResult()
        manager = self._get_manager()
        if manager is None:
            return PluginResult(state_updates={"workspace_finalized": False})
        try:
            merged = await ctx_await(manager.merge_worktree_before_complete, task_id)
            logger.info(
                "[WorkspaceLifecycle] exit 合并 worktree | task=%s | merged=%s",
                task_id,
                merged,
            )
            return PluginResult(state_updates={"workspace_finalized": True})
        except Exception as exc:
            logger.warning(
                "[WorkspaceLifecycle] exit 合并失败（留痕不阻断）| task=%s | error=%s",
                task_id,
                exc,
            )
            return PluginResult(state_updates={"workspace_finalized": False})


async def ctx_await(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """同步/异步统一调用（服务方法是同步的，跑在线程池避免阻塞事件循环）。"""
    import asyncio  # noqa: PLC0415

    result = await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))
    return result
