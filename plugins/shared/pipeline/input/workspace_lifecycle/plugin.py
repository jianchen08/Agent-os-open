"""工作空间生命周期 Input 插件。

挂载在管道的 init / exit 循环体（多循环体模型），按 `state["current_phase"]`
分发两个阶段。**插件自己持有工作空间服务**（WorkspaceLifecycleManager），
自己创建、给 agent 使用、自己清理——不依赖跨进程 capability：

- **init（bootstrap）**：消费 `state.execution_context.workspace`
  （`{source_path, mode}`，由 task_submit / 会话创建参数解析注入）。有任务
  上下文（state["task.id"]，== pipeline_id 的任务身份权威键）时调
  `on_task_start` 真实创建空间（worktree/plain）；主会话（无任务）直接解析
  source_path 写 state（主会话语义，非降级）。结果写入
  `state.workspace` / `ws_meta`（project_root 不由本插件写——其语义是
  实际项目目录，不是工作区路径）。幂等：state 已有 workspace 则跳过。
  任务管道无降级路径：服务不可用/创建失败/默认根解析失败一律显式报错
  （PluginResult.error，引擎记入 _plugin_errors 可见面），不落假工作空间。
- **exit（finalize）**：有任务且 ws_meta.mode=worktree 时调
  `merge_worktree_before_complete` 合并回源空间；否则 no-op。失败留痕不阻断。

与隔离解耦：本插件只管"在哪个目录执行"（拓扑），执行环境（容器/宿主）由
environment_lifecycle / isolation_guard 决策。

State 命名空间：
    - workspace / ws_meta：init 阶段写入
    - workspace_finalized：exit 阶段写入
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import state_fields
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


def _session_workspace_key(state: dict[str, Any]) -> str:
    """主会话工作区目录键：session_id（thread 权威键）优先，pipeline_id 兜底。

    只保留目录名安全字符（字母数字-_）；清洗后为空（异常形态）回退
    "default"——同基目录下所有会话工作区共用一层隔离目录，不会外溢。
    """
    raw = str(state.get("session_id") or state.get("pipeline_id") or "")
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_")
    return cleaned or "default"


# ── state 聚合读取器（server.py on_load 注入，pipeline-state capability）──
_state_reader: Any = None
# 最近一次聚合行快照（async 上下文刷新，sync 消费端只读缓存——
# 消费链 task_tree.get_task 是同步库接口，直接调用 async reader 会产生
# 永不 await 的协程（RuntimeWarning）且恒降级为空）
_state_rows_cache: list[dict[str, Any]] = []
# task.* 写面（server.py on_load 注入，pipeline-state.update）——init 创建
# 工作空间后把 ws_meta 镜像为 task.ws_meta：update 写直入 state 注册表，
# 运行中即时可见（state_updates 的 ws_meta 键随引擎回写快照有延迟），供
# task_evaluate 合并门控等运行中读面即时消费。
_task_state_writer: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（sidecar on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def set_task_state_writer(writer: Any) -> None:
    """注入 task.* 写面（sidecar on_load 经 pipeline-state.update capability）。"""
    global _task_state_writer  # noqa: PLW0603
    _task_state_writer = writer


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
    except Exception as exc:  # noqa: BLE001
        # 保留旧值而非清空（快照仍可用），但必须留痕——静默吞掉曾致父链
        # 查找失败无从排障（sidecar 重生窗口聚合读失败零日志，2026-08-29）。
        logger.warning(
            "[WorkspaceLifecycle] state 聚合行刷新失败（沿用上次快照）| error=%s",
            exc,
        )


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
        # metadata["ws_meta"]：服务端 restore_ws_meta 从 task.metadata 恢复父
        # 链工作空间坐标——聚合行直接带扁平 ws_meta（state 单一真值，YAML 只读
        # 镜像无 metadata 可读），跨进程父子任务据此共享工作空间。
        # as_dict 兼容跨边界 JSON 字符串形态（契约见 state_fields 模块 docstring）。
        _ws_meta = state_fields.as_dict(row.get("ws_meta"), field="ws_meta")
        return SimpleNamespace(
            id=task_id,
            parent_task_id=parent_id or None,
            metadata={"ws_meta": _ws_meta},
        )

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
        """懒加载 WorkspaceLifecycleManager（实例化失败返回 None，调用方裁决）。

        任务管道：manager None = 工作空间服务不可用 → 显式报错（无降级路径）。
        主会话：manager None 仅跳过 skills 同步（工作区解析不依赖服务）。

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
                "workspace",
            )
            if state.get(k) is not None
        }
        logger.info("[WorkspaceLifecycle] bootstrap state keys=%s", _dbg)
        if state.get("workspace"):
            logger.debug(
                "[WorkspaceLifecycle] workspace 已就位，跳过创建 | workspace=%s",
                state["workspace"],
            )
            await self._mirror_inherited_ws_meta(state)
            return PluginResult()

        ec = state.get("execution_context")
        ws_spec = ec.get("workspace") if isinstance(ec, dict) else None

        # ── 主会话（无任务身份、无显式工作区）工作区 ──
        # 工作区 = 「配置的工作空间根/sessions/{session_id}」——每会话独立
        # 目录（与任务管道 {task_id} 同构）：会话间隔离、不与任务目录互踩、
        # 仍在被 .gitignore 的基目录下（不新增 git 面）；跨 run 稳定（键 =
        # session_id，会话内文件可续用）。skills 快照同步到该目录，agent
        # 配置引用的 skills/... 相对路径在会话工作区内解析。仓库根不得作为
        # 会话工作区（agent 读写面不得触及项目源码树）；缺锚点时文件工具
        # 按 fail-closed 报错。
        _explicit_source = ws_spec.get("source_path") or "" if isinstance(ws_spec, dict) else ""
        if not state.get("task.id") and not _explicit_source:
            try:
                _ensure_isolation_path()
                from isolation.workspace import get_workspace_base_dir  # noqa: PLC0415

                _session_key = _session_workspace_key(state)
                _ws_root = str(Path(get_workspace_base_dir()) / "sessions" / _session_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[WorkspaceLifecycle] 主会话工作区解析失败，本会话文件工具无锚点 | error=%s",
                    exc,
                )
                return PluginResult()
            # skills 源 = 项目根 skills/（manager 的 base_path 决定复制源），
            # 目标 = 会话工作区；服务不可用时降级为纯解析（目录由根解析侧建）。
            manager = self._get_manager(base_path_hint=None)
            if manager is not None:
                try:
                    await ctx_await(manager.on_session_start, _ws_root)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[WorkspaceLifecycle] 主会话 skills 同步失败（工作区仍生效）| error=%s",
                        exc,
                    )
            logger.info(
                "[WorkspaceLifecycle] init 主会话工作区 | session=%s | path=%s",
                _session_key,
                _ws_root,
            )
            # project_root 不写：它语义 = 实际项目目录（提示词 {{project_root}}
            # 与配置注入基准），工作区路径由 workspace 键独立承载（param_inject
            # 工具锚点只认 workspace）；主会话 state 留空该键，防会话目录
            # 伪装成项目目录污染下游（fs 锚点/提示词语义）。
            return PluginResult(
                state_updates={
                    "workspace": _ws_root,
                    "ws_meta": {
                        "mode": "plain",
                        "path": _ws_root,
                        "session_id": _session_key,
                    },
                }
            )

        if not isinstance(ec, dict):
            logger.debug("[WorkspaceLifecycle] 无 execution_context，跳过工作空间创建")
            return PluginResult()
        if not isinstance(ws_spec, dict):
            logger.debug("[WorkspaceLifecycle] execution_context 无 workspace 声明，跳过")
            return PluginResult()

        source_path = ws_spec.get("source_path") or ""
        # 模式未指定 → 默认 plain（直接操作目标目录）；worktree 是显式选择
        # （需先填写工作空间，前端表单联动锁定）。
        mode = ws_spec.get("mode") or "plain"
        # 0.2 统一：任务身份 = pipeline_id，引擎注入 state 的扁平键是 task.id
        # （点号键）。缺 task 上下文 = 主会话纯解析。
        task_id = state.get("task.id") or ""
        # 工作流服务基础路径 = 项目根：sidecar cwd 是插件目录（非项目根），
        # 用 source_path（task 创建时带的项目根）作为 base_path 修正。
        manager = self._get_manager(base_path_hint=source_path)
        if task_id:
            # ── 任务管道：工作空间必须真实创建（无降级——失败显式报错，让
            # 插件错误面可见；不落"源路径/占位目录"假工作空间裸奔）──
            if not source_path:
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
                    logger.error(
                        "[WorkspaceLifecycle] 默认工作空间根解析失败 | task=%s | error=%s",
                        task_id,
                        exc,
                    )
                    return PluginResult(
                        error=RuntimeError(f"任务 {task_id} 默认工作空间根解析失败: {exc}")
                    )
            if manager is None:
                logger.error(
                    "[WorkspaceLifecycle] 工作空间服务不可用，任务工作空间创建失败（无降级）| task=%s",
                    task_id,
                )
                return PluginResult(
                    error=RuntimeError(f"工作空间服务不可用，任务 {task_id} 工作空间创建失败（无降级路径）")
                )
            # 调工作空间服务真实创建（对齐 task_executor 契约：
            # on_task_start(task_id, workspace, task_data) 分发 root/subtask）。
            # task_data 字段形态与 task_submit → task_data 一致。
            # is_root 由 lineage 推导：有父管道（lineage.parent_pipeline_id 非空）
            # 即子任务——共享父工作空间（服务 _start_subtask 按父链 ws_meta 解析），
            # 无父（根任务/独立任务）才按声明拓扑建独立工作区。
            _parent_id = str(state.get("lineage.parent_pipeline_id") or "")
            # 出生契约继承的父链工作空间坐标（lineage.parent_ws_meta，task_submit
            # 提交时随出生 state 写全）：_start_subtask 据此共享父工作空间，
            # 不依赖发起瞬间的聚合读可见性（父管道运行中 registry 行尚未建立，
            # 曾致同会话子任务工作空间漂移）。
            _inherited_ws_meta = state_fields.optional_dict(
                state.get("lineage.parent_ws_meta"),
                field="lineage.parent_ws_meta",
            )
            task_data = {
                "is_root": not _parent_id,
                "workspace_mode": mode,
                "isolation_mode": (ec.get("isolation") or {}).get("level", ""),
                "_has_explicit_workspace": bool(ws_spec.get("explicit")),
                "_inherit_workspace_resolved": False,
                "_inherited_parent_ws_meta": _inherited_ws_meta,
            }
            try:
                ws_meta = await ctx_await(manager.on_task_start, task_id, source_path, task_data)
            except Exception as exc:
                logger.error(
                    "[WorkspaceLifecycle] 工作空间创建失败 | task=%s | error=%s",
                    task_id,
                    exc,
                )
                return PluginResult(
                    error=RuntimeError(f"任务 {task_id} 工作空间创建失败: {exc}")
                )
            if not (isinstance(ws_meta, dict) and ws_meta.get("path")):
                logger.error(
                    "[WorkspaceLifecycle] 工作空间创建未返回有效路径 | task=%s | ws_meta=%s",
                    task_id,
                    ws_meta,
                )
                return PluginResult(
                    error=RuntimeError(f"任务 {task_id} 工作空间创建未返回有效路径: {ws_meta!r}")
                )
            # ws_meta 镜像进任务域键空间（task.ws_meta，经 pipeline-state.update
            # 即时入注册表）：运行中的 task_evaluate 合并门控经 state 读面即时
            # 可见。镜像失败不阻断 init（主创建已成功、ws_meta 仍随 state_updates
            # 入引擎 state），但 ERROR 留痕——门控运行中会读到空值并显式报错。
            writer = _task_state_writer
            if writer is not None:
                try:
                    await writer(task_id, {"task.ws_meta": ws_meta})
                except Exception as mirror_exc:
                    logger.error(
                        "[WorkspaceLifecycle] task.ws_meta 镜像写失败 | task=%s | error=%s",
                        task_id,
                        mirror_exc,
                    )
            logger.info(
                "[WorkspaceLifecycle] init 服务创建工作空间 | task=%s | mode=%s | path=%s",
                task_id,
                ws_meta.get("mode"),
                ws_meta["path"],
            )
            return PluginResult(
                state_updates={
                    "workspace": ws_meta["path"],
                    "ws_meta": ws_meta,
                }
            )

        # ── 主会话（无任务身份）显式源路径：plain 语义解析 ──
        # 这不是降级，是主会话语义（任务管道在上方已全部返回）。
        # project_root 不写：它语义 = 实际项目目录（提示词 {{project_root}}
        # 与配置注入基准），工作区路径由 workspace 键独立承载（param_inject
        # 工具锚点只认 workspace）；主会话 state 留空该键，防会话目录
        # 伪装成项目目录污染下游（fs 锚点/提示词语义）。
        updates = {
            "workspace": source_path,
            "ws_meta": {
                "mode": mode,
                "path": source_path,
            },
        }
        logger.info(
            "[WorkspaceLifecycle] init 解析工作空间 | mode=%s | path=%s",
            mode,
            source_path,
        )
        return PluginResult(state_updates=updates)

    async def _mirror_inherited_ws_meta(self, state: Any) -> None:
        """workspace 已就位（继承/恢复）而跳过创建时，补写 task.ws_meta 即时镜像。

        镜像仅在服务创建路径落笔（on_task_start 成功后）；继承型子任务的
        workspace 由出生契约预置，幂等短路绕过该路径——运行中的 task_evaluate
        合并门控读 task.ws_meta / ws_meta（引擎出口键，run 末才落库）/
        task.metadata（0.1 退役通路）三路皆空，按 fail-closed 判"ws_meta
        读取失败"误伤评估。镜像内容与 _start_subtask 继承分支同形
        （mode=shared：子任务不拥有合并，父任务完成门控负责其 worktree）；
        出生契约无坐标（根任务恢复等）不虚构 plain——虚构会让真实 worktree
        任务跳过合并、产物静默丢失，维持门控 fail-closed。
        """
        task_id = str(state.get("task.id") or "")
        if not task_id:
            return
        if state.get("task.ws_meta") or state.get("ws_meta"):
            return
        inherited = state_fields.optional_dict(
            state.get("lineage.parent_ws_meta"),
            field="lineage.parent_ws_meta",
        )
        inherited_path = str(inherited.get("path") or "")
        if not inherited_path:
            logger.warning(
                "[WorkspaceLifecycle] workspace 已就位但出生契约无父链坐标，"
                "task.ws_meta 镜像不补写（门控将按 ws_meta 读取失败处理）| task=%s",
                task_id,
            )
            return
        mirror = {
            "mode": "shared",
            "path": inherited_path,
            "parent_workspace": str(state.get("workspace") or ""),
            "project_root": str(inherited.get("project_root") or ""),
        }
        writer = _task_state_writer
        if writer is None:
            logger.warning(
                "[WorkspaceLifecycle] task_state_writer 未绑定，task.ws_meta 镜像补写跳过 | task=%s",
                task_id,
            )
            return
        try:
            await writer(task_id, {"task.ws_meta": mirror})
        except Exception as exc:
            logger.error(
                "[WorkspaceLifecycle] task.ws_meta 镜像补写失败 | task=%s | error=%s",
                task_id,
                exc,
            )

    # ── exit：合并/清理（服务自持，真实执行）──────────────────

    async def _finalize(self, ctx: PluginContext) -> PluginResult:
        """工作空间收尾：worktree 合并回源空间。

        有任务且 ws_meta.mode=worktree → 调服务 merge_worktree_before_complete；
        其余（plain/主会话）no-op。失败留痕不阻断（收尾类操作不得让 run 翻车）。
        """
        state = ctx.state
        ws_meta = state_fields.optional_dict(state.get("ws_meta"), field="ws_meta")
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
