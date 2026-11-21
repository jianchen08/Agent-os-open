"""工作空间生命周期 Input 插件。

挂载在管道的 init / exit 循环体（多循环体模型），按 `state["current_phase"]`
分发两个阶段。**插件自己持有工作空间服务**（WorkspaceLifecycleManager），
自己创建、给 agent 使用、自己清理——不依赖跨进程 capability：

- **init（bootstrap）**：消费 `state.execution_context.workspace`
  （`{source_path, mode}`，由 task_submit / 会话创建参数解析注入）。有任务
  上下文（state["task.id"]，== pipeline_id 的任务身份权威键）时调
  `on_task_start` 真实创建空间（worktree/plain）；主会话（无任务）直接解析
  source_path 写 state（主会话语义，非降级）。结果写入 `state.workspace`；
  工作空间坐标单键：任务管道 = `task.ws_meta`（出生/创建时一次写全，
  引擎出口键 `ws_meta` 已退役——state 单一真相源收敛 2026-10-08），主会话
  = `ws_meta`（会话域坐标键，与任务域分立）。project_root 不由本插件写
  ——其语义是实际项目目录，不是工作区路径。幂等：state 已有 workspace
  则跳过。任务管道无降级路径：服务不可用/创建失败/默认根解析失败一律
  显式报错（PluginResult.error，引擎记入 _plugin_errors 可见面），不落假
  工作空间。
- **exit（finalize）**：**零操作**（2026-09-04 用户裁定）。合并/清理 worktree 的
  唯一合法触发点 = 评估通过（task_evaluate 完成门控 `_try_merge_before_complete`，
  合并+清理一体执行）；run 结束 ≠ 任务终态——exit 若在此合并/拆工作区，续跑轮
  会踩在被拆的空壳上（598b4ad4 实锤）。失败即失败的语义由完成门控承载，
  exit 不再做任何合并/删除动作。

与隔离解耦：本插件只管"在哪个目录执行"（拓扑），执行环境（容器/宿主）由
environment_lifecycle / isolation_guard 决策。

State 命名空间：
    - workspace：init 阶段写入
    - task.ws_meta：任务工作空间坐标单键（出生 task_submit 预写 / 创建本
      插件即写），主会话域坐标键 = ws_meta（与任务域分立，不互写）
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _root_api import state_fields

logger = logging.getLogger(__name__)


from _root_api import ensure_isolation_path as _ensure_isolation_path

# 会话目录键长度：对齐任务侧 id_utils 短 id 约定（12 位，48 bit 熵；同值
# 不跨包 import，插件自包含）。
_SESSION_KEY_SHORT_LEN = 12

_DEVICE_ID_ENV = "AGENTOS_DEVICE_ID"


def _tree_device_id() -> str:
    """统一树编址的本端设备 id（D9' 设备前缀；解析序与 sync_agent/router 同式）。"""
    return os.environ.get(_DEVICE_ID_ENV, "").strip() or platform.node()


def _active_mode(state: Any) -> str:
    """当前生效模式 id：execution_context.mode 标签集主标签 → agent 键 → 空。

    语义单点在 mode_keys.resolve_active_mode（与 mode_material_inject 激活
    判定同式：标签集末元素优先、字符串旧形态兼容、agent 键派生需包存在）。
    """
    from _root_api import resolve_active_mode  # noqa: PLC0415

    return resolve_active_mode(state)


def _mode_package_dirs(state: Any) -> list[Path] | None:
    """当前模式包目录（技能源收窄面）；无模式/包未命中 → None（全量回退）。

    模式 id 先过 MODE_ID_RE 形态闸再拼包目录（杜绝 `../` 穿越出厂根）；
    find_mode_package_dir 是出厂/用户双根单源解析（用户副本优先）。
    """
    mode = _active_mode(state)
    if not mode:
        return None
    try:
        _ensure_isolation_path()
        from _root_api import MODE_ID_RE, find_mode_package_dir  # noqa: PLC0415

        if MODE_ID_RE.match(mode) is None:
            logger.warning(
                "[WorkspaceLifecycle] 模式 id 形态非法，按无模式处理 | mode=%r", mode
            )
            return None
        package = find_mode_package_dir(mode)
    except Exception as exc:  # 包解析不可得 → 全量回退（技能可访问性优先）
        logger.warning(
            "[WorkspaceLifecycle] 模式包解析失败（全量回退）| mode=%s | err=%s", mode, exc
        )
        return None
    return [package] if package is not None else None


def _resolve_ws_anchor_base(workspace_path: str) -> Path | None:
    """锚定子树根 = 工作空间基目录（会话/任务工作空间共同父目录，D9' 子树约定）。"""
    _ensure_isolation_path()
    from isolation.workspace import (  # noqa: PLC0415
        get_workspace_base_dir,
        resolve_workspace_base,
    )

    return resolve_workspace_base(Path(get_workspace_base_dir()), Path(workspace_path))


def _anchor_ws_meta(meta: dict[str, Any], workspace_path: str, *, base: Path | None = None) -> None:
    """ws_meta 坐标补统一树路径（D9' 编址；锚定是增强不是依赖）。

    子树根缺省按工作空间基目录解析；解析/编址失败 = tree_path 省略并留痕，
    绝不阻断工作空间创建（创建语义优先，树地址是跨端协调元数据）。
    """
    from tree_anchor import workspace_tree_path  # noqa: PLC0415 —— 惰性：本模块
    # 零插件内模块级依赖（按路径装载面契约，tests 按文件加载本插件不挑 sys.path）

    try:
        anchor_base = base if base is not None else _resolve_ws_anchor_base(workspace_path)
        tree = (
            workspace_tree_path(
                workspace_path, device_id=_tree_device_id(), subtree_root=str(anchor_base)
            )
            if anchor_base is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001 —— 增强失败省略（错误留痕不阻断）
        logger.warning("[WorkspaceLifecycle] 统一树锚定失败，tree_path 省略 | error=%s", exc)
        return
    if tree:
        meta["tree_path"] = tree


def _session_workspace_key(state: dict[str, Any]) -> str:
    """主会话工作区目录键：session_id（thread 权威键）优先，pipeline_id 兜底；
    短 id 形态 = 剥 thread- 前缀与连字符后取前 12 位（与 pipeline_id 的
    12hex 同形；全量 thread-<uuid> 目录名过长）。

    只保留目录名安全字符（字母数字-_）；清洗后为空（异常形态）回退
    "default"——同基目录下所有会话工作区共用一层隔离目录，不会外溢。
    """
    raw = str(state.get("session_id") or state.get("pipeline_id") or "")
    if raw.startswith("thread-"):
        raw = raw[len("thread-"):]
    cleaned = "".join(c for c in raw.replace("-", "") if c.isalnum() or c in "-_")
    return cleaned[:_SESSION_KEY_SHORT_LEN] or "default"


# ── state 聚合读取器（server.py on_load 注入，pipeline-state capability）──
_state_reader: Any = None
# 当前 execute 的管道 state（sync 消费端 task_tree 的唯一 state 来源）。
# ContextVar 而非实例字段：同 sidecar 实例被多管道并发复用，实例字段会在
# await 处被其他管道的 execute 覆盖（每条 asyncio 任务各持独立 context 副本）。
_current_pipeline_state: ContextVar[dict[str, Any] | None] = ContextVar(
    "workspace_lifecycle_pipeline_state", default=None
)
# 最近一次聚合行快照（async 上下文刷新，sync 消费端只读缓存——
# 消费链 task_tree.get_task 是同步库接口，直接调用 async reader 会产生
# 永不 await 的协程（RuntimeWarning）且恒降级为空）
_state_rows_cache: list[dict[str, Any]] = []
# task.* 写面（server.py on_load 注入，pipeline-state.update）——init 创建
# 工作空间后把坐标写进单键 task.ws_meta：update 写直入 state 注册表，运行中
# 即时可见（state_updates 的 task.ws_meta 随引擎 step 投影有步界延迟），供
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
        # 保留旧值而非清空（快照仍可用），但必须留痕——静默吞掉会让父链
        # 查找失败无从排障。
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

    def __init__(self, plugin: WorkspaceLifecyclePlugin) -> None:
        self._plugin = plugin

    def _read_rows(self) -> list[dict[str, Any]]:
        # sync 消费端只读缓存：聚合行由 refresh_state_rows() 在 async 上下文
        # （execute 入口）刷新——不在此调用 reader（async reader 会产生永不
        # await 的协程，见 refresh_state_rows 注释）
        return list(_state_rows_cache)

    def get_task(self, task_id: str):
        # 本管道 state 走 ContextVar（execute 绑定）：实例字段会被并发管道的
        # execute 覆盖，父链兜底会读到别的任务身份。
        state = _current_pipeline_state.get() or {}
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
        # 任务工作空间坐标单键 task.ws_meta：restore_ws_meta 据此恢复父链坐标，
        # 跨进程父子任务据此共享工作空间。父行坐标键域分派：任务父行 =
        # task.ws_meta（任务域），主会话父行 = ws_meta（会话域，任务管道行
        # 不写会话域键，两域互斥——or-链只在任务域缺席时认会话域）。
        # as_dict 兼容跨边界 JSON 字符串形态（契约见 state_fields 模块 docstring）。
        _ws_meta = state_fields.as_dict(
            row.get("task.ws_meta") or row.get("ws_meta"),
            field="task.ws_meta",
        )
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
                task_tree=_ExecutionContextTaskTree(self),
                ws_meta_store={},
                base_path=base_path,
            )
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
        token = _current_pipeline_state.set(ctx.state)
        try:
            # 刷新聚合行快照：init/exit 体内 task_tree.get_task 走 sync 缓存读
            await refresh_state_rows()
            phase = ctx.state.get("current_phase", "")
            if phase == "init":
                return await self._bootstrap(ctx)
            if phase == "exit":
                return await self._finalize(ctx)
            # 其它循环体（main）：生命周期插件不参与，零产出
            return PluginResult()
        finally:
            _current_pipeline_state.reset(token)

    async def prepare_task_workspace(
        self,
        *,
        pipeline_id: str,
        state: dict[str, Any],
        execution_context: dict[str, Any] | None = None,
    ) -> PluginResult:
        """出生期同步准备任务工作空间（ADR 2026-10-17 四段式出生第三段）。

        与 init 体**同一段代码**（`_bootstrap`）：提交期工作空间目录名需 task_id
        （身份登记之后才可算），且须在派发之前完成，才能兑现「派发时工作空间
        一定就绪」。四形态（worktree / plain+显式 / plain 缺省 / 非 git 降级）
        决策零重复——本插件持有服务，提交层只调不动实现。

        Args:
            pipeline_id: 已登记的任务管道 id（= task.id）。
            state: 阶段二之后的管道 state（`task.id` 已在位）。
            execution_context: 工作区/隔离声明（管道 init 时才随 dispatch 注入，
                此处由提交方按同形传入）。

        Returns:
            PluginResult：成功带 ``workspace`` + ``task.ws_meta`` 坐标；
            失败带 error（无降级路径，调用方据其回传失败）。
        """
        ctx = PluginContext(
            state={
                **state,
                "pipeline_id": pipeline_id,
                "current_phase": "init",
                "execution_context": dict(execution_context or {}),
            },
            config=self._config,
        )
        token = _current_pipeline_state.set(ctx.state)
        try:
            await refresh_state_rows()
            return await self._bootstrap(ctx)
        finally:
            _current_pipeline_state.reset(token)

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
        ws_state_path = str(state.get("workspace") or "")
        if ws_state_path and Path(ws_state_path).is_dir():
            logger.debug(
                "[WorkspaceLifecycle] workspace 已就位，跳过创建 | workspace=%s",
                state["workspace"],
            )
            await self._mirror_inherited_ws_meta(state)
            return PluginResult(state_updates=self._session_ws_meta_selfheal(state, ws_state_path))
        if ws_state_path:
            # state 里的 workspace 指向不存在的目录（worktree 被外部清理/
            # 目录被删）——禁止静默跳过：续跑轮会踩在被拆的空壳上（598b4ad4
            # 实锤）。落空即重建，走下方创建段按 execution_context 重新分叉。
            logger.warning(
                "[WorkspaceLifecycle] state.workspace 指向不存在的目录，重建 | path=%s",
                ws_state_path,
            )
        else:
            await self._mirror_inherited_ws_meta(state)

        ec = state.get("execution_context")
        ws_spec = ec.get("workspace") if isinstance(ec, dict) else None
        _explicit_source = ws_spec.get("source_path") or "" if isinstance(ws_spec, dict) else ""

        # 主会话（无任务身份、无显式工作区）工作区 = 会话目录
        if not state.get("task.id") and not _explicit_source:
            return await self._bootstrap_session_workspace(ctx, state)

        _birth_fallback = False
        if not isinstance(ec, dict):
            # 出生契约回退：root 派发路径出生只写 task.ws_meta（plain 显式拓扑
            # 提交期写全），execution_context 未入管道 state——此前在此跳过
            # 创建，state.workspace 永不落键，fs 工具无锚（2026-10-15 装机
            # 哨兵任务实锤）。出生坐标带 path 时按同形 ws_spec 照常创建
            # （与 execution_context 在场同一条任务路径）；无坐标维持跳过。
            ws_spec = self._fallback_ws_spec_from_birth(state)
            if ws_spec is None:
                logger.debug(
                    "[WorkspaceLifecycle] 无 execution_context/出生坐标，跳过工作空间创建"
                )
                return PluginResult()
            ec = {"workspace": ws_spec}
            _birth_fallback = True
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
        if task_id:
            result = await self._bootstrap_task_workspace(ctx, state, ec, ws_spec, task_id, mode, source_path)
            if _birth_fallback and result.error is None and result.state_updates:
                # 回退路径补 fs 锚单键 project_root（plain 显式拓扑 path=
                # project_root=source_path，与出生契约同形）；有
                # execution_context 的原路径行为不变（不补写）。
                _created = result.state_updates.get("task.ws_meta")
                _project_root = (
                    str(_created.get("project_root") or "") if isinstance(_created, dict) else ""
                )
                result.state_updates["project_root"] = _project_root or source_path
            return result

        # ── 主会话（无任务身份）显式源路径：plain 语义解析 ──
        # 这不是降级，是主会话语义（任务管道在上方已全部返回）。
        # 坐标键 = ws_meta（主会话域坐标键，与任务域 task.ws_meta 分立）。
        # project_root 不写：它语义 = 实际项目目录（提示词 {{project_root}}
        # 与配置注入基准），工作区路径由 workspace 键独立承载（param_inject
        # 工具锚点只认 workspace）；主会话 state 留空该键，防会话目录
        # 伪装成项目目录污染下游（fs 锚点/提示词语义）。
        explicit_ws_meta = {
            "mode": mode,
            "path": source_path,
        }
        _anchor_ws_meta(explicit_ws_meta, source_path)
        updates = {
            "workspace": source_path,
            "ws_meta": explicit_ws_meta,
        }
        logger.info(
            "[WorkspaceLifecycle] init 解析工作空间 | mode=%s | path=%s",
            mode,
            source_path,
        )
        return PluginResult(state_updates=updates)

    async def _bootstrap_session_workspace(self, ctx: PluginContext, state: Any) -> PluginResult:
        """主会话（无任务身份、无显式工作区）工作区。

        工作区 = 「配置的工作空间根/sessions/{短id}」——每会话独立
        目录（与任务管道 {task_id} 同构）：会话间隔离、不与任务目录互踩、
        仍在被 .gitignore 的基目录下（不新增 git 面）；跨 run 稳定（键 =
        session_id 确定性派生的短 id，见 _session_workspace_key，会话内
        文件可续用）。skills 快照同步到该目录，agent
        配置引用的 skills/... 相对路径在会话工作区内解析。仓库根不得作为
        会话工作区（agent 读写面不得触及项目源码树）；缺锚点时文件工具
        按 fail-closed 报错。

        基目录经 resolve_workspace_base 与任务工作空间同口径收口（跨盘退化
        到项目所在盘），否则会话目录会与任务 worktree 分盘、两套坐标。
        """
        try:
            _ensure_isolation_path()
            from isolation.workspace import (  # noqa: PLC0415
                find_project_root,
                get_workspace_base_dir,
                resolve_workspace_base,
            )

            _session_key = _session_workspace_key(state)
            # 项目根与 _get_manager 的 base_path 推导同式（配置 base_path 优先，
            # 缺省项目根），确保退化判定用同一个盘。
            _project_root = Path(self._config.get("base_path") or find_project_root())
            _ws_base = resolve_workspace_base(Path(get_workspace_base_dir()), _project_root)
            _ws_root = str(_ws_base / "sessions" / _session_key)
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
                await ctx_await(
                    manager.on_session_start, _ws_root, mode_packages=_mode_package_dirs(state)
                )
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
        session_ws_meta = {
            "mode": "plain",
            "path": _ws_root,
            "session_id": _session_key,
        }
        _anchor_ws_meta(session_ws_meta, _ws_root, base=_ws_base)
        return PluginResult(
            state_updates={
                "workspace": _ws_root,
                "ws_meta": session_ws_meta,
            }
        )

    async def _bootstrap_task_workspace(
        self,
        ctx: PluginContext,
        state: Any,
        ec: dict[str, Any],
        ws_spec: dict[str, Any],
        task_id: str,
        mode: str,
        source_path: str,
    ) -> PluginResult:
        """任务管道段：工作空间必须真实创建（绑定 → 创建 → 镜像）。

        无降级——失败显式报错，让插件错误面可见；不落"源路径/占位目录"
        假工作空间裸奔。is_root 由 lineage 推导：有父管道
        （lineage.parent_pipeline_id 非空）即子任务——共享父工作空间（服务
        _start_subtask 按父链 ws_meta 解析），无父（根任务/独立任务）才按
        声明拓扑建独立工作区。
        """
        if not source_path:
            resolved_source, default_error = self._resolve_default_source(task_id, mode)
            if resolved_source is None:
                return PluginResult(
                    error=RuntimeError(f"任务 {task_id} 默认工作空间根解析失败: {default_error}")
                )
            source_path = resolved_source
        # 工作流服务基础路径 = 项目根：sidecar cwd 是插件目录（非项目根），
        # 用 source_path（task 创建时带的项目根）作为 base_path 修正。
        manager = self._get_manager(base_path_hint=source_path)
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
        task_data = self._build_task_data(state, ec, ws_spec, mode)
        try:
            ws_meta = await ctx_await(
                manager.on_task_start,
                task_id,
                source_path,
                task_data,
                mode_packages=_mode_package_dirs(state),
            )
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
        _anchor_ws_meta(ws_meta, str(ws_meta["path"]))
        await self._write_task_ws_meta(task_id, ws_meta)
        logger.info(
            "[WorkspaceLifecycle] init 服务创建工作空间 | task=%s | mode=%s | path=%s",
            task_id,
            ws_meta.get("mode"),
            ws_meta["path"],
        )
        return PluginResult(
            state_updates={
                "workspace": ws_meta["path"],
                "task.ws_meta": ws_meta,
            }
        )

    def _fallback_ws_spec_from_birth(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """出生契约回退：task.ws_meta → execution_context.workspace 同形声明。

        仅消费出生期即确定的 plain 显式坐标（task_submit 出生只对
        source_path+plain 写 task.ws_meta，见其出生写面），explicit=True 与
        来源一致（有坐标必有调用方显式 workspace）。无任务身份或坐标无
        path（残缺/JSON 非法形态按缺失处理）返回 None，调用方维持跳过。
        """
        if not state.get("task.id"):
            return None
        ws_meta = state_fields.as_dict(state.get("task.ws_meta"), field="task.ws_meta")
        path = str((ws_meta or {}).get("path") or "")
        if not path:
            return None
        return {
            "mode": str(ws_meta.get("mode") or "plain"),
            "source_path": path,
            "explicit": True,
        }

    def _resolve_default_source(self, task_id: str, mode: str) -> tuple[str | None, str | None]:
        """无显式 workspace 声明时按默认根解析源路径。

        默认工作空间语义：无显式 workspace 声明的任务在
        「工作空间根/{task_id}」下创建目录——默认隔离执行的工作空间
        （容器挂载与 bash 执行均以此为锚）。

        Returns:
            (source_path, None)；解析失败返回 (None, 错误说明)
        """
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
            return source_path, None
        except Exception as exc:
            logger.error(
                "[WorkspaceLifecycle] 默认工作空间根解析失败 | task=%s | error=%s",
                task_id,
                exc,
            )
            return None, str(exc)

    def _build_task_data(
        self,
        state: Any,
        ec: dict[str, Any],
        ws_spec: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        """构造 on_task_start 的 task_data（字段形态与 task_submit → task_data 一致）。"""
        _parent_id = str(state.get("lineage.parent_pipeline_id") or "")
        # 出生契约继承的父链工作空间坐标（lineage.parent_ws_meta，task_submit
        # 提交时随出生 state 写全）：_start_subtask 据此共享父工作空间，
        # 不依赖发起瞬间的聚合读可见性（父管道运行中 registry 行可能尚未建立）。
        _inherited_ws_meta = state_fields.optional_dict(
            state.get("lineage.parent_ws_meta"),
            field="lineage.parent_ws_meta",
        )
        return {
            "is_root": not _parent_id,
            "workspace_mode": mode,
            "isolation_mode": (ec.get("isolation") or {}).get("level", ""),
            # 显式坐标 = 调用方显式声明（execution_context.workspace.explicit，
            # task_submit 只在调用方显式指定 workspace 或显式挂靠项目时置位；
            # L2/L3 沿父链继承的项目挂靠不置位）。继承型子任务的落位由出生
            # 契约的父链坐标（lineage.parent_ws_meta）决定——继承直接上级的
            # 工作空间；父无任务工作空间（主会话派的根任务显式挂靠等）才按
            # 声明拓扑在项目文件夹上建自己的工作区（2026-09-03 用户裁定，
            # 收窄 2026-08-30「挂靠即显式锚」至显式挂靠）。
            "_has_explicit_workspace": bool(ws_spec.get("explicit")),
            "_inherit_workspace_resolved": False,
            "_inherited_parent_ws_meta": _inherited_ws_meta,
        }

    async def _write_task_ws_meta(self, task_id: str, ws_meta: dict[str, Any]) -> None:
        """创建即写工作空间坐标单键（task.ws_meta，经 pipeline-state.update
        即时入注册表）：worktree/默认拓扑的最终坐标创建期才可知（提交期预写
        即谎报），本写是这些拓扑的坐标权威落笔；运行中的 task_evaluate 合并
        门控经 state 读面即时可见。写失败不阻断 init（主创建已成功、
        task.ws_meta 仍随 state_updates 入引擎 state 并按 step 投影落表），
        但 ERROR 留痕——门控运行中会读到空值并显式报错。
        """
        writer = _task_state_writer
        if writer is None:
            return
        try:
            await writer(task_id, {"task.ws_meta": ws_meta})
        except Exception as exc:
            logger.error(
                "[WorkspaceLifecycle] task.ws_meta 创建写失败 | task=%s | error=%s",
                task_id,
                exc,
            )

    def _session_ws_meta_selfheal(self, state: dict[str, Any], workspace: str) -> dict[str, Any]:
        """主会话域坐标键补全（workspace 已就位的幂等跳过分支）。

        会话域坐标键只在创建路径落笔（_bootstrap_session_workspace /
        显式源解析分支），历史会话该键未入投影落表白名单时跨 run 冷恢复
        读不到——第二轮起 task_submit 出生契约缺父链坐标，子任务工作空间
        创建 fail-closed。workspace 在而坐标缺（含无 path 残缺形态，与
        注入侧有效性判定同口径）时按现行 workspace 据实补写（不改变落点）；
        坐标已在 = 运行时权威，零产出。任务管道不写会话域键（两域互斥，
        任务域补全归 _mirror_inherited_ws_meta）。
        """
        if state.get("task.id"):
            return {}
        current = state_fields.as_dict(state.get("ws_meta"), field="ws_meta")
        if current and current.get("path"):
            return {}
        ec = state.get("execution_context")
        ws_spec = ec.get("workspace") if isinstance(ec, dict) else None
        mode = (ws_spec.get("mode") or "plain") if isinstance(ws_spec, dict) else "plain"
        session_key = _session_workspace_key(state)
        logger.info(
            "[WorkspaceLifecycle] 主会话坐标键缺失，按现行工作区补全 | session=%s | path=%s",
            session_key,
            workspace,
        )
        healed = {
            "mode": mode,
            "path": workspace,
            "session_id": session_key,
        }
        _anchor_ws_meta(healed, workspace)
        return {"ws_meta": healed}

    async def _mirror_inherited_ws_meta(self, state: Any) -> None:
        """workspace 已就位（继承/恢复）而跳过创建时，补全坐标单键 task.ws_meta。

        单键仅在服务创建路径落笔（on_task_start 成功后 _write_task_ws_meta）；
        继承型子任务的 workspace 由出生契约预置，幂等短路绕过该路径——运行中
        的 task_evaluate 合并门控读 task.ws_meta 为空，按 fail-closed 判
        "ws_meta 读取失败"误伤评估。补全内容与 _start_subtask 继承分支同形
        （mode=shared：子任务不拥有合并，父任务完成门控负责其 worktree）；
        出生契约无坐标（根任务恢复等）不虚构 plain——虚构会让真实 worktree
        任务跳过合并、产物静默丢失，维持门控 fail-closed。
        """
        task_id = str(state.get("task.id") or "")
        if not task_id:
            return
        if state.get("task.ws_meta"):
            return
        inherited = state_fields.optional_dict(
            state.get("lineage.parent_ws_meta"),
            field="lineage.parent_ws_meta",
        )
        inherited_path = str(inherited.get("path") or "")
        if not inherited_path:
            logger.warning(
                "[WorkspaceLifecycle] workspace 已就位但出生契约无父链坐标，"
                "task.ws_meta 补全不落笔（门控将按 ws_meta 读取失败处理）| task=%s",
                task_id,
            )
            return
        mirror = {
            "mode": "shared",
            "path": inherited_path,
            "parent_workspace": str(state.get("workspace") or ""),
            "project_root": str(inherited.get("project_root") or ""),
        }
        _anchor_ws_meta(mirror, inherited_path)
        writer = _task_state_writer
        if writer is None:
            logger.warning(
                "[WorkspaceLifecycle] task_state_writer 未绑定，task.ws_meta 补全跳过 | task=%s",
                task_id,
            )
            return
        try:
            await writer(task_id, {"task.ws_meta": mirror})
        except Exception as exc:
            logger.error(
                "[WorkspaceLifecycle] task.ws_meta 补全写失败 | task=%s | error=%s",
                task_id,
                exc,
            )

    # ── exit：合并/清理（服务自持，真实执行）──────────────────

    async def _finalize(self, ctx: PluginContext) -> PluginResult:
        """工作空间收尾：**零操作**（2026-09-04 用户裁定）。

        合并/清理 worktree 的唯一合法触发点 = 评估通过（task_evaluate 完成门控
        `_try_merge_before_complete`，合并+清理一体）。run 结束 ≠ 任务终态：
        exit 在此合并/拆工作区，同任务续跑轮会重新 bootstrap 到被拆的空壳路径
        （598b4ad4 实锤：23:51 拆除、23:51:18 续跑踩壳、产物链全断）。故 exit
        不做任何合并/删除动作。
        """
        logger.debug(
            "[WorkspaceLifecycle] exit 零操作（合并归评估通过门控）| task=%s",
            ctx.state.get("task.id") or "-",
        )
        return PluginResult()


async def ctx_await(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """同步/异步统一调用（服务方法是同步的，跑在线程池避免阻塞事件循环）。"""
    import asyncio  # noqa: PLC0415

    result = await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))
    return result
