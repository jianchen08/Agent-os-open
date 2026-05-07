"""CLI 入口模块（Claude Code 风格交互体验）。

提供命令行交互式管道应用，包含：
- CLIApplication: Claude Code 风格 CLI 应用主类

特性：
- 斜杠命令系统（/help, /compact, /clear, /model 等）
- 底部状态栏（Agent 名称、模型、轮次、上下文占用）
- 多行输入（\\ 续行）
- 工具调用可视化
- 交互模式切换（Normal/Auto/Plan）
- 行内快捷语法（@path, !cmd, #memo）

启动方式::

    # 默认启动
    python -m channels.cli.cli_main

    # 指定管道配置
    python -m channels.cli.cli_main --config path/to/pipeline.yaml

    # 通过入口点
    agent-os
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys as _sys
import time as _time
from pathlib import Path
from typing import Any

# Windows 终端修复：
# 1. 强制 stdout/stderr 使用 UTF-8，防止 GBK 编码错误
#    （LLM 返回的 emoji 等 Unicode 字符在 GBK 下无法编码，导致流式输出失败）
# 2. 为 CMD 启用 ANSI/VT100 虚拟终端处理，让 Rich 能正确渲染颜色和定位
if _sys.platform == "win32":
    for _stream in (_sys.stdout, _sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # 启用 Windows CMD 的 ANSI escape code 支持
    try:
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        for _handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            _handle = _kernel32.GetStdHandle(_handle_id)
            _mode = ctypes.c_ulong()
            if _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)):
                _kernel32.SetConsoleMode(
                    _handle, _mode.value | 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
    except Exception:
        pass

from channels.cli.cli_commands import CommandResult, SlashCommandRegistry
from channels.cli.input_adapter import CLIInputAdapter
from channels.cli.output_adapter import CLIOutputAdapter, sanitize_for_terminal
from pipeline.engine import PipelineEngine
from pipeline.registry import PluginRegistry
from pipeline.route import (
    InputRouteTable,
    OutputRouteTable,
)

logger = logging.getLogger(__name__)

_LOGGING_INITIALIZED = False

def setup_logging(
    debug: bool = False,
    log_dir: Path | str | None = None,
) -> None:
    """初始化统一日志系统（终端 + 文件）。

    在所有入口点调用一次即可。重复调用不会重复初始化。

    Args:
        debug: 是否启用 DEBUG 级别（终端也会显示管道内部日志）
        log_dir: 日志目录路径，默认为项目根目录下的 logs/
    """
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return
    _LOGGING_INITIALIZED = True

    if log_dir is None:
        log_dir = _PROJECT_ROOT / "logs"
    else:
        log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = logging.DEBUG if debug else logging.INFO
    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt="%H:%M:%S",
    )

    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        filename=log_dir / "agent_os.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(file_handler)

    logger.info(
        "Logging initialized: console_level=%s, file=agent_os.log (DEBUG)",
        "DEBUG" if debug else "INFO",
    )

    if not debug:
        _SUPPRESSED_NS = (
            "pipeline.", "httpcore", "httpx", "LiteLLM", "openai",
            "isolation.", "infrastructure.",
            "tools.", "plugins.", "llm.",
            "src.tools.", "src.plugins.", "src.llm.",
            "evaluation", "tasks", "memory",
            "human_interaction", "channels.cli.",
            "__main__", "asyncio",
        )

        def _console_filter(record: logging.LogRecord) -> bool:
            # 外部库（非内部命名空间）→ 全部放行
            if not any(record.name.startswith(_ns) for _ns in _SUPPRESSED_NS):
                return True
            # 内部命名空间：错误已通过 output adapter 结构化显示，
            # 不再重复输出到终端，避免长 traceback 泄露
            return False

        _console_handler = logging.getLogger().handlers[0]
        _console_handler.addFilter(_console_filter)

# 默认管道配置路径（相对于包目录）
# 默认管道配置路径 -- 优先项目根目录的 config/，回退到 src/config/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_PIPELINE_CONFIG = _PROJECT_ROOT / "config" / "pipelines" / "default.yaml"

# Session directory for CLI session metadata (absolute path)
_SESSION_DIR = _PROJECT_ROOT / "data" / "session"

_DEFAULT_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
# No hard limit on session messages — context compression handles overflow


# ---------------------------------------------------------------------------
# CLI Application（Claude Code 风格）
# ---------------------------------------------------------------------------


class CLIApplication:
    """CLI 交互式管道应用（Claude Code 风格）。

    组装输入适配器、输出适配器、管道引擎及路由表，
    提供增强的交互式命令行循环。

    从 YAML 配置加载 LLMCore + ToolCore + Output 插件。

    交互模式：
    - Normal（默认）：正常对话，工具调用需确认
    - Auto：自动执行，不需确认
    - Plan：只读模式，只规划不执行

    Example::

        app = CLIApplication()
        app.setup_pipeline()
        asyncio.run(app.run())
    """

    def __init__(self, streaming: bool = True) -> None:
        """初始化 CLI 应用，创建各核心组件实例。

        Args:
            streaming: 是否启用流式输出，默认 True。
        """
        # 斜杠命令注册表
        self._command_registry = SlashCommandRegistry()

        # 输入/输出适配器
        self._input_adapter = CLIInputAdapter(
            prompt_str="> ",
            command_registry=self._command_registry,
        )
        self._output_adapter = CLIOutputAdapter()

        # 管道引擎
        self._engine: PipelineEngine | None = None
        self._plugin_registry = PluginRegistry()
        self._input_route_table = InputRouteTable()
        self._output_route_table = OutputRouteTable()
        self._streaming = streaming
        self._agent_config: Any | None = None
        self._services: dict[str, Any] = {}

        # 子对话期间的管道输出抑制
        self._suppress_streaming: bool = False
        self._streaming_buffer: list[str] = []

        # 交互状态
        self._interaction_mode: str = "normal"  # normal / auto / plan
        self._show_thinking: bool = False
        self._turn_count: int = 0

        # 后台管道状态
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_initial_state: dict[str, Any] | None = None

        # 并发执行状态
        self._bg_tasks: set[asyncio.Task] = set()

    def setup_pipeline(self, config_path: str | Path | None = None) -> None:
        """设置真实管道配置（从 YAML 加载 LLMCore + ToolCore + Output 插件）。

        启动流程（Agent 只注入参数，插件自主读取）：
        1. 加载 YAML → build_plugin_registry() 实例化插件
        2. 创建共享服务（ToolRegistry、JsonMemoryStore）→ 注入 PipelineEngine
        3. 加载 Agent 配置 → 参数写入 state
        4. 插件运行时从 ctx.state / ctx.get_service() 自主获取

        Args:
            config_path: 管道配置 YAML 文件路径。
                默认使用 ``config/pipelines/default.yaml``。
        """
        from config.models import get_model_config_loader
        from pipeline.config import build_plugin_registry, load_pipeline_config

        # 确定配置路径
        if config_path is None:
            config_path = _DEFAULT_PIPELINE_CONFIG

        config_path = Path(config_path)
        if not config_path.exists():
            # 回退到 src/ 下的 config/pipelines/
            project_root = Path(__file__).resolve().parent.parent.parent / "config" / "pipelines" / "default.yaml"
            if project_root.exists():
                config_path = project_root
            else:
                logger.error("Pipeline config not found at %s", config_path)
                raise FileNotFoundError(f"Pipeline config not found: {config_path}")

        logger.info("Loading pipeline config from: %s", config_path)

        # 创建 ModelConfigLoader 用于环境变量回退（使用缓存单例避免重复解析 YAML）
        model_loader = get_model_config_loader()

        # 加载管道配置
        try:
            pipeline_config = load_pipeline_config(config_path, model_loader=model_loader)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Failed to load pipeline config: %s", exc)
            raise

        # 构建插件注册表（通过 model_loader 自动创建共享 Router）
        _t0 = _time.monotonic()
        self._plugin_registry = build_plugin_registry(
            pipeline_config, model_loader=model_loader, router=None,
        )
        logger.info("[STARTUP] build_plugin_registry: %.2fs", _time.monotonic() - _t0)

        # 获取由 build_plugin_registry → get_or_create_adapter 创建的共享 Router
        from llm.router_factory import get_or_create_router
        router = get_or_create_router(model_loader)

        # 使用配置中的路由表
        self._input_route_table = pipeline_config.input_route_table
        self._output_route_table = pipeline_config.output_route_table

        # 加载 Agent 配置（默认灵汐 lingxi）— 直接从 AgentRegistry 加载
        from agents.registry import AgentRegistry
        agent_registry = AgentRegistry()
        agent_config_dir = _PROJECT_ROOT / "config" / "agents"
        if agent_config_dir.exists():
            agent_registry.load_directory(agent_config_dir)

        # 创建共享服务 → 注入 PipelineEngine（agent_registry 需要在 _build_services 前创建）
        _t1 = _time.monotonic()
        self._services = self._build_services(agent_registry=agent_registry)
        logger.info("[STARTUP] _build_services: %.2fs", _time.monotonic() - _t1)

        # 注入 model_loader 和 router 到 services（供 engine 模型覆盖使用）
        self._services["model_loader"] = model_loader
        if router is not None:
            self._services["llm_router"] = router

        # 如果 ToolCore 存在，从 ToolRegistry 注册工具
        tool_core = self._plugin_registry.get_core("tool_execute")
        if tool_core is not None:
            tool_registry = self._services.get("tool_registry")
            if tool_registry is not None:
                try:
                    from tools.builtin import register_core_tools
                    registered = register_core_tools(tool_registry, session=None)
                    logger.info("ToolCore registered %d core tools", len(registered))
                except Exception as exc:
                    logger.warning("register_core_tools failed: %s", exc)
                tool_core.register_tools_from_registry(tool_registry)

            # BUG-FIX-fix_20260419_isolation_executor_not_injected:
            # 初始化隔离执行器并注入到 ToolCore，使工具能够在 Docker 容器中执行
            # 之前 IsolationExecutor 从未被实例化，导致 IsolationGuard 的容器隔离决策无法生效
            try:
                from isolation.executor import IsolationExecutor
                isolation_executor = IsolationExecutor()
                tool_core.set_isolation_executor(isolation_executor)
                logger.info("IsolationExecutor 已注入到 ToolCore")
            except Exception as exc:
                logger.warning("IsolationExecutor 初始化失败: %s，工具将在宿主机执行", exc)

        for candidate in ["default", "lingxi"]:
            self._agent_config = agent_registry.get(candidate)
            if self._agent_config:
                break
        if self._agent_config:
            logger.info(
                "Agent config loaded: %s (%s), level=%s",
                self._agent_config.config_id,
                self._agent_config.display_name,
                self._agent_config.level.value,
            )
        else:
            logger.info("No agent config loaded, using raw LLM without system prompt")

        # 创建管道引擎（直接调用，不需要 Worker 中间层）
        _t2 = _time.monotonic()
        checkpoint_mgr = self._services.get("checkpoint_manager")
        self._engine = PipelineEngine(
            input_route_table=self._input_route_table,
            output_route_table=self._output_route_table,
            plugin_registry=self._plugin_registry,
            services=self._services,
            checkpoint_manager=checkpoint_mgr,
        )
        logger.info("PipelineEngine created (direct call, no Worker) %.2fs", _time.monotonic() - _t2)

        # Register llm_core as a service for context_window_guard
        llm_core_plugin = self._plugin_registry.get_core("llm_call")
        if llm_core_plugin is not None:
            self._services["llm_core"] = llm_core_plugin
            logger.info("Service registered: llm_core (from plugin registry)")

            # 将 LLM 调用能力注入到 context_service（延迟注入）
            context_svc = self._services.get("context_service")
            if context_svc is not None and hasattr(llm_core_plugin, "_adapter"):
                from llm.adapter import LLMResponse

                async def _llm_call_fn(prompt: str) -> str:
                    if router is not None:
                        # Router 模式：用路由别名，凭证由 Router 管理
                        response: LLMResponse = await llm_core_plugin._adapter.completion(
                            model=llm_core_plugin._model,
                            messages=[{"role": "user", "content": prompt}],
                            stream=False,
                        )
                    else:
                        # 直连模式：透传凭证
                        call_kwargs: dict[str, Any] = {}
                        if llm_core_plugin._api_base:
                            call_kwargs["api_base"] = llm_core_plugin._api_base
                        if llm_core_plugin._api_key:
                            call_kwargs["api_key"] = llm_core_plugin._api_key
                        response = await llm_core_plugin._adapter.completion(
                            model=llm_core_plugin._get_model_string(),
                            messages=[{"role": "user", "content": prompt}],
                            stream=False,
                            **call_kwargs,
                        )
                    return response.text or ""

                context_svc.set_llm_call_fn(_llm_call_fn)
                logger.info("Service injected: context_service <- llm_core adapter")

        # 初始化任务执行器（事件驱动，用于后台任务处理）
        try:
            from tasks.service import TaskService
            task_service = self._services.get("task_service") or TaskService()

            from infrastructure.task_worker import TaskWorker
            import yaml as _yaml
            _tw_config: dict[str, Any] = {}
            try:
                with open(config_path, encoding="utf-8") as _f:
                    _raw_cfg = _yaml.safe_load(_f) or {}
                _tw_config = _raw_cfg.get("task_worker", {})
            except Exception:
                pass
            self._task_worker = TaskWorker(
                task_service=task_service,
                plugin_registry=self._plugin_registry,
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                services=self._services,
                event_bus=self._event_bus,
                config=_tw_config,
            )
            logger.info("Task worker initialized")

            _prt = self._input_route_table
            _ort = self._output_route_table
            _pr = self._plugin_registry
            _svc = self._services

            def _eval_pipeline_factory():
                return PipelineEngine(
                    input_route_table=_prt,
                    output_route_table=_ort,
                    plugin_registry=_pr,
                    services=_svc,
                )

            # 注册 pipeline_factory 到 ServiceProvider，替代 sys._agent_os_* 全局变量
            try:
                from infrastructure.service_provider import get_service_provider
                get_service_provider().register("pipeline_factory", _eval_pipeline_factory)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Failed to initialize task worker: %s", exc)
            self._task_worker = None
            console = self._output_adapter.console
            console.print(
                f"[bold red]⚠ 任务执行器初始化失败: {exc}[/bold red]\n"
                "[dim]任务提交功能将不可用，请检查日志排查原因[/dim]"
            )

        logger.info("Real pipeline setup complete: name=%s", pipeline_config.name)



    def _build_services(self, agent_registry: Any = None) -> dict[str, Any]:
        """构建共享服务字典（委托 Application 统一构建）。"""
        from application import Application

        app = Application(project_root=_PROJECT_ROOT)
        services = app.build_services(agent_registry=agent_registry)

        # 保存引用
        self._app = app
        self._event_bus = services.get("event_bus")

        # CLI 渠道特有服务（不属于后端通用服务）
        try:
            import human_interaction.desktop_notifier  # noqa: F401
        except Exception:
            pass

        try:
            from channels.cli.cli_interaction import CLIInteractionNotifier
            from human_interaction import get_human_interaction_service

            cli_notifier = CLIInteractionNotifier(console=self._output_adapter.console)
            human_svc = get_human_interaction_service()
            human_svc.set_notifier(cli_notifier)
            services["cli_notifier"] = cli_notifier
            services["human_interaction_service"] = human_svc
            logger.info("Service created: CLIInteractionNotifier -> HumanInteractionService")
        except Exception as exc:
            logger.warning("Failed to create CLIInteractionNotifier: %s", exc)

        return services

    # -----------------------------------------------------------------------
    # 非交互模式：发送单条消息并等待任务闭环
    # -----------------------------------------------------------------------

    async def run_single(self, message: str) -> None:
        """非交互模式：发送单条消息，等待后台任务闭环后退出。"""
        import time as _time

        t0 = _time.time()
        console = self._output_adapter.console

        console.print(f"\n[bold green]User:[/bold green] {message}\n")

        tw = getattr(self, "_task_worker", None)
        if tw and hasattr(tw, "start"):
            await tw.start()
            logger.info("TaskWorker started (single-message mode)")

        try:
            try:
                result = await self._engine.run(
                    user_input=message,
                    agent_config=self._agent_config,
                    conversation_history=None,
                    streaming=False,
                    auto_approve=True,
                    interaction_mode="auto",
                )
            except Exception as exc:
                console.print(f"\n[red]Engine error: {exc}[/red]")
                return

            elapsed_l1 = _time.time() - t0
            iters = result.get("iteration", 0)
            pipeline_id = result.get("pipeline_id", "")
            raw = result.get("raw_result", "")

            ts = self._services.get("task_service")
            task_ids = []
            if ts:
                try:
                    storage = getattr(ts, "_storage", None)
                    if storage is not None:
                        all_tasks = getattr(storage, "_tasks", {})
                        running_tasks = [
                            (tid, t) for tid, t in all_tasks.items()
                            if hasattr(t, "status") and t.status.value in ("running", "pending")
                        ]
                        if running_tasks:
                            running_tasks.sort(key=lambda x: getattr(x[1], "created_at", ""), reverse=True)
                            task_ids = [tid for tid, _ in running_tasks]
                except Exception:
                    pass

            console.print(f"\n[dim]L1 done: {elapsed_l1:.1f}s, {iters} iterations, pipeline={pipeline_id}[/dim]")
            if task_ids:
                console.print(f"[dim]Tasks submitted: {task_ids}, waiting for completion...[/dim]\n")

                final_statuses = {}
                for _ in range(120):
                    await asyncio.sleep(5)
                    if ts:
                        try:
                            remaining = []
                            for tid in task_ids:
                                task = ts.get_task(tid)
                                if task:
                                    status = task.status if hasattr(task, "status") else task.get("status", "?")
                                    status_val = status.value if hasattr(status, "value") else str(status)
                                    if status_val in ("completed", "failed", "cancelled"):
                                        final_statuses[tid] = status_val
                                    else:
                                        remaining.append(tid)
                            if not remaining:
                                break
                        except Exception:
                            pass

                if final_statuses and self._engine:
                    try:
                        summary_lines = []
                        for tid, st in final_statuses.items():
                            task_obj = ts.get_task(tid) if ts else None
                            title = getattr(task_obj, "title", tid) if task_obj else tid
                            summary_lines.append(f"- 任务 [{title}](id={tid}): {st}")
                        summary_text = "\n".join(summary_lines)
                        followup = (
                            f"[系统通知] 以下子任务已到达终态，请向用户汇报最终结果：\n{summary_text}\n\n"
                            "请用简洁的方式向用户汇报任务执行结果。"
                        )
                        followup_result = await self._engine.run(
                            user_input=followup,
                            agent_config=self._agent_config,
                            conversation_history=None,
                            streaming=False,
                            auto_approve=True,
                            interaction_mode="auto",
                        )
                        followup_raw = followup_result.get("raw_result", "")
                        if followup_raw:
                            console.print(f"\n[bold green]Agent:[/bold green] {followup_raw}")
                    except Exception as exc:
                        logger.warning("run_single followup failed: %s", exc)

                elapsed_total = _time.time() - t0
                for tid in task_ids:
                    status = final_statuses.get(tid, "timeout")
                    console.print(f"\n[bold]Task {tid}: {status}[/bold]")
                console.print(f"[dim]Total: {elapsed_total:.1f}s[/dim]")
            else:
                console.print(f"\n[dim]No task submitted. Response: {str(raw)[:300]}[/dim]")

        finally:
            # 确保 TaskWorker 和 LiteLLM 资源始终被清理
            if tw and hasattr(tw, "stop"):
                await tw.stop()
            try:
                from llm.adapter import cleanup_litellm_resources
                await cleanup_litellm_resources()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Claude Code 风格 REPL 循环
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """运行 Claude Code 风格 CLI 交互主循环。

        特性：
        - 斜杠命令处理（/help, /clear, /mode 等）
        - 底部状态栏渲染
        - 流式输出 + <think/> 过滤
        - 交互模式切换（Normal/Auto/Plan）
        - 工具调用可视化
        - 行内快捷语法
        """
        from rich.console import Console

        _run_t0 = _time.monotonic()
        console = self._output_adapter.console
        agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"
        model_name = self._get_model_name()

        # 显示启动横幅
        self._output_adapter.show_startup_banner(agent_name, self._interaction_mode)

        # 异步初始化 TagNetworkRetriever（从 PG 加载 Tag 向量和共现关系）
        _run_t1 = _time.monotonic()
        tag_network_retriever = self._services.get("tag_network_retriever")
        vector_retriever = self._services.get("vector_retriever")
        if tag_network_retriever is not None and vector_retriever is not None:
            try:
                await tag_network_retriever.init_from_pg(vector_retriever)
            except Exception as exc:
                logger.warning("TagNetworkRetriever async init failed: %s", exc)
        logger.info("[STARTUP] TagNetworkRetriever init: %.2fs", _time.monotonic() - _run_t1)

        # 启动任务执行器（如果可用）
        _run_t2 = _time.monotonic()
        if hasattr(self, '_task_worker') and self._task_worker:
            if hasattr(self._task_worker, 'start'):
                await self._task_worker.start()
                logger.info("Task worker started")
        logger.info("[STARTUP] TaskWorker start: %.2fs", _time.monotonic() - _run_t2)

        # 初始化状态栏
        self._output_adapter.update_status_bar(
            agent_name=agent_name,
            model_name=model_name,
            turn_count=0,
            context_pct=0.0,
            mode=self._interaction_mode,
            is_processing=False,
        )

        # 确定 Agent ID（从已加载的 agent_config 获取）
        agent_id = self._agent_config.config_id if self._agent_config else "lingxi"

        # 通过 SessionService 管理会话（只管 session_id 和 pipeline_id）
        _run_t3 = _time.monotonic()
        session_svc = self._services.get("session_service")
        if session_svc is None:
            from infrastructure.session import SessionService
            session_svc = SessionService(session_dir=_SESSION_DIR)
        session = await session_svc.create_or_restore(
            channel_type="cli",
        )
        logger.info("[STARTUP] session restore: %.2fs", _time.monotonic() - _run_t3)

        # 引擎 pipeline_id 跟随 session：如果 session 有已保存的 active_pipeline_id，
        # 用它覆盖引擎的随机 ID，保证跨 CLI 重启管道 ID 不变
        if session.active_pipeline_id and self._engine is not None:
            logger.info(
                "Syncing engine pipeline_id to session: %s → %s",
                self._engine.pipeline_id, session.active_pipeline_id,
            )
            self._engine._pipeline_id = session.active_pipeline_id

        # 跨轮次对话历史：从执行记录恢复（绑定 pipeline_id）
        _run_t4 = _time.monotonic()
        conversation_history: list[dict[str, Any]] = []
        restored = False

        if session.active_pipeline_id:
            exec_storage = self._services.get("execution_record_storage")
            if exec_storage:
                try:
                    prev_records = exec_storage.list_by_pipeline(session.active_pipeline_id)
                    if prev_records:
                        conversation_history = []
                        for r in prev_records:
                            msg: dict[str, Any] = {"role": r.role, "content": r.content}
                            if r.name:
                                msg["name"] = r.name
                            if r.tool_call_id:
                                msg["tool_call_id"] = r.tool_call_id
                            if r.tool_input:
                                msg["tool_input"] = r.tool_input
                            if r.tool_calls_json:
                                try:
                                    import json as _json
                                    msg["tool_calls"] = _json.loads(r.tool_calls_json)
                                except (_json.JSONDecodeError, TypeError):
                                    pass
                            conversation_history.append(msg)

                        # 旧记录没有 tool_calls_json，需要从 tool 记录反向重建
                        from infrastructure.task_worker import _reconstruct_tool_calls
                        _reconstruct_tool_calls(conversation_history)

                        restored = True
                        logger.info(
                            "Restored %d messages from pipeline records (pipeline=%s)",
                            len(conversation_history), session.active_pipeline_id,
                        )
                except Exception as exc:
                    logger.debug("Failed to restore from pipeline records: %s", exc)
        logger.info("[STARTUP] conversation restore (%d msgs): %.2fs", len(conversation_history), _time.monotonic() - _run_t4)
        logger.info("[STARTUP] === run() total: %.2fs ===", _time.monotonic() - _run_t0)

        if restored:
            console.print(
                f"[dim]已恢复上次会话 ({len(conversation_history)} 条消息)，"
                f"使用 /clear 开启新会话[/dim]"
            )

        # REPL 主循环（事件驱动：后台管道 + 即时提示符）
        _repl_iteration = 0
        _exit_reason = ""
        while True:
            _repl_iteration += 1
            # --- 后台管道完成检查 ---
            if (
                self._pipeline_task is not None
                and self._pipeline_task.done()
            ):
                try:
                    final_state = self._pipeline_task.result()
                except asyncio.CancelledError:
                    logger.info(
                        "Pipeline task cancelled (user Ctrl+C"
                        " or external cancel)"
                    )
                    final_state = {
                        "error": "Pipeline cancelled",
                    }
                except Exception as exc:
                    logger.warning("Pipeline task failed: %s", exc)
                    final_state = {"error": str(exc)}

                initial_state = self._pipeline_initial_state or {}
                self._pipeline_task = None
                self._pipeline_initial_state = None

                # 结束残留的文本行（用 sys.stdout 避免 rich markup 干扰）
                if getattr(self, "_last_was_text", False):
                    _sys.stdout.write("\n")
                    _sys.stdout.flush()
                    self._last_was_text = False

                try:
                    conversation_history = await self._handle_pipeline_result(
                        final_state, initial_state,
                        conversation_history, console,
                    )
                except Exception:
                    logger.exception(
                        "Error handling pipeline result, "
                        "continuing REPL loop"
                    )

                # → 不 continue，直接落到下方显示提示符，避免多一轮循环延迟

            # 渲染状态栏提示符
            status_text = self._output_adapter.status_bar.render_simple()
            self._input_adapter._prompt_str = f"{status_text} > "

            # --- 子 Agent 交互请求处理（管道运行中或空闲时） ---
            cli_notifier = self._services.get("cli_notifier")
            if cli_notifier and cli_notifier.has_pending():
                human_svc = self._services.get(
                    "human_interaction_service"
                )
                from channels.cli.cli_interaction import (
                    run_sub_conversation,
                )

                # 子对话期间抑制管道流式输出
                self._suppress_streaming = True
                try:
                    await run_sub_conversation(
                        console=console,
                        input_adapter=self._input_adapter,
                        notifier=cli_notifier,
                        interaction_service=human_svc,
                        idle_timeout=60,
                    )
                except Exception as _sub_conv_exc:
                    logger.warning(
                        "[REPL] run_sub_conversation (top) error: %s",
                        _sub_conv_exc, exc_info=True,
                    )
                finally:
                    self._suppress_streaming = False
                    if self._streaming_buffer:
                        safe = sanitize_for_terminal(
                            "".join(self._streaming_buffer)
                        )
                        console.print(safe, end="", highlight=False)
                        self._last_was_text = True
                        self._streaming_buffer.clear()
                self._input_adapter.drain_stdin()
                # 不 continue，直接落到下方显示提示符

            # === 等待输出结束 ===
            # 只要有活跃输出、或管道刚启动还没出东西，就等待
            # 不显示提示符，避免输出覆盖提示符后光标位置错乱
            _pipeline_was_running = (
                self._pipeline_task is not None
                and not self._pipeline_task.done()
            )
            if _pipeline_was_running:
                _output_wait_start = _time.monotonic()
                while True:
                    # 管道已完成 → 退出等待，回到循环顶部处理结果
                    if (
                        self._pipeline_task is None
                        or self._pipeline_task.done()
                    ):
                        break
                    # 管道挂起 → 退出等待，显示提示符
                    if (
                        self._engine is not None
                        and self._engine.is_suspended
                    ):
                        break
                    # 输出已停止 300ms → 退出等待，显示提示符
                    _last_t = getattr(self, "_last_chunk_time", 0)
                    if _last_t > 0 and (_time.monotonic() - _last_t) >= 0.3:
                        break
                    # 兜底：最多等 2 秒后显示提示符，避免 LLM 首响应
                    # 慢或无 chunk 时 CLI 假死
                    if (_time.monotonic() - _output_wait_start) >= 2.0:
                        break
                    # 还在输出或刚启动 → 等待 0.3s 后重新检查
                    await asyncio.wait(
                        {self._pipeline_task},
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=0.3,
                    )
                    # 检查是否有待处理的交互请求（管道可能正在
                    # 执行 human_interaction 工具）
                    if cli_notifier and cli_notifier.has_pending():
                        break

                # 管道在等待期间完成了 → 回到循环顶部处理结果
                if (
                    self._pipeline_task is not None
                    and self._pipeline_task.done()
                ):
                    continue

            # === 显示提示符（输出结束/管道挂起/空闲） ===
            if getattr(self, "_last_was_text", False):
                _sys.stdout.write("\n")
                _sys.stdout.flush()
                self._last_was_text = False
            # 直接写 stdout，绕开 rich markup 解析 [NORMAL] 被吞的问题
            _sys.stdout.write(self._input_adapter.prompt_text())
            _sys.stdout.flush()

            # === 等待事件：用户输入 / 交互请求 / 管道完成 ===
            try:
                initial_state, pipeline_done = (
                    await self._wait_for_next_event(
                        cli_notifier, console
                    )
                )
            except (EOFError, KeyboardInterrupt):
                logger.warning(
                    "[REPL] EOF/KeyboardInterrupt at iter=%d, "
                    "pipeline_running=%s — exiting REPL",
                    _repl_iteration,
                    self._pipeline_task is not None
                    and not self._pipeline_task.done(),
                )
                self._output_adapter.show_system_message(
                    "感谢使用 Agent OS，再见！",
                    "bold blue",
                )
                _exit_reason = "EOF/KeyboardInterrupt"
                break
            except asyncio.CancelledError:
                # CancelledError 不是 Exception 的子类，
                # 必须显式捕获，否则会穿透导致 app.run() 退出
                logger.warning(
                    "[REPL] CancelledError at iter=%d — "
                    "suppressing, continuing loop",
                    _repl_iteration,
                )
                continue
            except Exception as _wait_exc:
                # 保护：_wait_for_next_event 不应抛出异常，
                # 但如果发生了，记录日志并继续循环（而非退出）。
                logger.warning(
                    "[REPL] _wait_for_next_event unexpected error "
                    "(iter=%d, pipeline_running=%s): %s",
                    _repl_iteration,
                    self._pipeline_task is not None
                    and not self._pipeline_task.done(),
                    _wait_exc,
                    exc_info=True,
                )
                continue

            # 管道完成了 → 回到循环顶部处理结果
            if pipeline_done:
                continue

            # 交互请求中断了输入等待
            if initial_state is None:
                continue

            # 多行粘贴反馈：打印分隔线和行数提示
            if self._input_adapter.was_paste():
                total = self._input_adapter.paste_line_count() + 1
                console.print(
                    f"\n[dim green]  ▲ 已接收 {total} 行"
                    "粘贴内容，正在处理...[/dim green]"
                )

            # 退出信号 — 但如果管道仍在运行，阻止退出。
            # 输入适配器的意外异常（如 stdin pipe 问题）不应导致
            # 管道被取消。只有用户主动的 Ctrl+C 才能中断运行中的管道。
            if initial_state.get("should_stop"):
                _pipeline_still_running = (
                    self._pipeline_task is not None
                    and not self._pipeline_task.done()
                )
                if _pipeline_still_running:
                    logger.warning(
                        "[REPL] should_stop while pipeline running "
                        "(iter=%d) — ignoring, pipeline continues",
                        _repl_iteration,
                    )
                    continue
                if (
                    hasattr(self, "_task_worker")
                    and self._task_worker
                    and hasattr(self._task_worker, "stop")
                ):
                    await self._task_worker.stop()

                ts = self._services.get("task_service")
                if ts and hasattr(ts, "list_by_status"):
                    try:
                        from tasks.types import TaskStatus

                        all_tasks = []
                        for st in TaskStatus:
                            all_tasks.extend(
                                ts.list_by_status(st)
                            )
                        if all_tasks:
                            console.print(
                                "\n[bold]任务状态汇总:[/bold]"
                            )
                            for t in all_tasks:
                                tid = (
                                    t.id
                                    if hasattr(t, "id")
                                    else str(t.get("id", "?"))
                                )
                                tstatus = (
                                    t.status
                                    if hasattr(t, "status")
                                    else t.get("status", "?")
                                )
                                tstatus_str = (
                                    tstatus.value
                                    if hasattr(tstatus, "value")
                                    else str(tstatus)
                                )
                                ttitle = (
                                    t.title
                                    if hasattr(t, "title")
                                    else t.get("title", "")
                                )
                                icon = (
                                    "✅"
                                    if tstatus_str == "completed"
                                    else "❌"
                                    if tstatus_str == "failed"
                                    else "🔄"
                                )
                                console.print(
                                    f"  {icon} {tid[:12]} |"
                                    f" {tstatus_str} | {ttitle}"
                                )
                    except Exception as exc:
                        logger.debug(
                            "任务状态汇总失败: %s", exc
                        )

                self._output_adapter.show_system_message(
                    "感谢使用 Agent OS，再见！", "bold blue"
                )
                _exit_reason = "should_stop (no pipeline)"
                break

            # 空输入 — 检查是否有待处理的交互请求
            if initial_state.get("_is_empty"):
                if cli_notifier and cli_notifier.has_pending():
                    human_svc = self._services.get(
                        "human_interaction_service"
                    )
                    from channels.cli.cli_interaction import (
                        run_sub_conversation,
                    )

                    try:
                        await run_sub_conversation(
                            console=console,
                            input_adapter=self._input_adapter,
                            notifier=cli_notifier,
                            interaction_service=human_svc,
                            idle_timeout=60,
                        )
                    except Exception as _sub_exc:
                        logger.warning(
                            "[REPL] run_sub_conversation (empty) "
                            "error: %s", _sub_exc, exc_info=True,
                        )
                    self._input_adapter.drain_stdin()
                continue

            # 斜杠命令处理
            slash_result = initial_state.get("slash_command")
            if slash_result and hasattr(slash_result, "output"):
                if slash_result.output:
                    console.print(slash_result.output)
                if slash_result.should_exit:
                    console.print(
                        "[bold blue]Goodbye![/bold blue]"
                    )
                    _exit_reason = "slash_result.should_exit"
                    break
                continue

            # 检查是否为斜杠命令（旧版兼容）
            if (
                hasattr(
                    self._input_adapter, "is_slash_command"
                )
                and self._input_adapter.is_slash_command(
                    initial_state
                )
            ):
                cmd_result = await self._handle_slash_command(
                    initial_state
                )
                if cmd_result is None:
                    continue
                if cmd_result.should_stop:
                    self._output_adapter.show_system_message(
                        "感谢使用 Agent OS，再见！",
                        "bold blue",
                    )
                    _exit_reason = "cmd_result.should_stop"
                    break
                if cmd_result.should_clear_history:
                    conversation_history.clear()
                    session_svc.clear(session)
                    self._turn_count = 0
                    self._output_adapter.update_status_bar(
                        turn_count=0, context_pct=0.0
                    )
                if cmd_result.state_updates:
                    self._apply_command_updates(
                        cmd_result.state_updates
                    )
                continue

            # Plan 模式：只显示规划，不实际执行
            if self._interaction_mode == "plan":
                self._output_adapter.show_system_message(
                    "[PLAN 模式] 不会执行任何操作，仅显示规划。"
                    "使用 /mode normal 切换回正常模式。",
                    "yellow",
                )
                user_input = initial_state.get("user_input", "")
                console.print(
                    f"\n[dim][规划模式] 收到输入:"
                    f" {user_input}[/dim]"
                )
                console.print(
                    "[dim]使用 /mode normal 或 /mode auto"
                    " 切换模式后执行[/dim]\n"
                )
                continue

            # === 启动管道（后台运行，不阻塞提示符） ===

            # 如果已有管道在运行
            if (
                self._pipeline_task is not None
                and not self._pipeline_task.done()
            ):
                # 管道挂起 → 注入用户输入并唤醒管道
                if (
                    self._engine is not None
                    and self._engine.is_suspended
                ):
                    user_input = initial_state.get("user_input", "")
                    if user_input:
                        self._engine.inject_and_wake(user_input)
                        console.print(
                            "[dim cyan]→ 已将输入注入挂起的管道"
                            "并唤醒[/dim cyan]"
                        )
                    else:
                        self._engine.wake()
                    continue
                # 管道真正在运行 → 检查是否有待处理的交互请求
                if cli_notifier and cli_notifier.has_pending():
                    human_svc = self._services.get(
                        "human_interaction_service"
                    )
                    from channels.cli.cli_interaction import (
                        run_sub_conversation,
                    )
                    self._suppress_streaming = True
                    try:
                        await run_sub_conversation(
                            console=console,
                            input_adapter=self._input_adapter,
                            notifier=cli_notifier,
                            interaction_service=human_svc,
                            idle_timeout=60,
                        )
                    except Exception as _busy_exc:
                        logger.warning(
                            "[REPL] run_sub_conversation (busy) "
                            "error: %s", _busy_exc, exc_info=True,
                        )
                    finally:
                        self._suppress_streaming = False
                        if self._streaming_buffer:
                            safe = sanitize_for_terminal(
                                "".join(self._streaming_buffer)
                            )
                            console.print(
                                safe, end="", highlight=False,
                            )
                            self._last_was_text = True
                            self._streaming_buffer.clear()
                    self._input_adapter.drain_stdin()
                    continue
                # 管道仍在运行但没有挂起也没有交互请求
                # → 将用户输入推入消息队列，管道下一轮迭代会读取
                user_input = initial_state.get("user_input", "")
                if user_input.strip():
                    msg_queue = self._services.get("message_queue")
                    _pid = (
                        session.active_pipeline_id or ""
                    )
                    if msg_queue and _pid:
                        from infrastructure.message_queue import (
                            MessageQueue,
                            Message,
                            create_message_id,
                        )
                        if isinstance(msg_queue, MessageQueue):
                            await msg_queue.push(Message(
                                id=create_message_id(),
                                pipeline_id=_pid,
                                target_id="",
                                content=user_input,
                            ))
                            console.print(
                                "[dim cyan]→ 消息已发送给运行中的管道"
                                "[/dim cyan]"
                            )
                continue

            user_input = initial_state.get("user_input", "")

            on_chunk = None
            if self._streaming:
                on_chunk = self._build_on_chunk_callback(console)

            task_stats = self._get_task_stats()
            self._output_adapter.update_status_bar(
                is_processing=True,
                pipeline_running=True,
                pipeline_iteration=0,
                pipeline_max_iterations=(
                    self._engine.max_iterations
                    if self._engine
                    else 0
                ),
                running_task_count=task_stats["running"],
                pending_task_count=task_stats["pending"],
                completed_task_count=task_stats["completed"],
                failed_task_count=task_stats["failed"],
            )

            pipeline_id = session_svc.prepare_run(session)

            # 引擎 pipeline_id 跟随 session（session 是权威来源）
            if pipeline_id != self._engine.pipeline_id:
                logger.info(
                    "Syncing engine pipeline_id to session on run: %s → %s",
                    self._engine.pipeline_id, pipeline_id,
                )
                self._engine._pipeline_id = pipeline_id

            # BUG-FIX-fix_pipeline_thread_id_missing:
            # 将 session_id 作为 thread_id 注入管道 state，
            # 供 TrackPlugin 在保存 PipelineRunSummary 时写入 thread_id 字段。
            # 这确保了服务器重启后 _try_recover_pipeline_ids 能通过 summary.thread_id 找到管道记录。
            _thread_id = session.session_id if session else ""

            self._pipeline_task = asyncio.create_task(
                self._engine.run(
                    user_input=user_input,
                    agent_config=self._agent_config,
                    conversation_history=(
                        conversation_history
                        if conversation_history
                        else None
                    ),
                    streaming=self._streaming,
                    on_chunk=on_chunk,
                    auto_approve=(
                        self._interaction_mode == "auto"
                    ),
                    interaction_mode=self._interaction_mode,
                    thread_id=_thread_id,
                )
            )

            self._pipeline_initial_state = initial_state
            # 管道在后台运行 → 立即回到提示符
            continue

        # while 循环已退出（正常 break）
        logger.warning(
            "[REPL] Loop exited! reason=%s | "
            "pipeline_running=%s | _repl_iteration=%d",
            _exit_reason or "UNKNOWN (exception?)",
            self._pipeline_task is not None
            and not self._pipeline_task.done(),
            _repl_iteration,
        )

        # 清理后台任务
        for _t in list(self._bg_tasks):
            if not _t.done():
                _t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        # 清理 LiteLLM 资源（后台任务 + HTTP 会话）
        try:
            from llm.adapter import cleanup_litellm_resources
            await cleanup_litellm_resources()
        except Exception:
            pass

    async def _handle_pipeline_result(
        self,
        final_state: dict[str, Any],
        initial_state: dict[str, Any],
        conversation_history: list[dict[str, Any]],
        console: Console,
    ) -> list[dict[str, Any]]:
        """处理管道执行结果：更新历史、绑定任务、刷新状态栏。"""
        # 错误结果直接显示
        if "error" in final_state and final_state.get("error"):
            await self._output_adapter.send(
                {"error": final_state["error"]}
            )
            self._update_status_bar_idle()
            return conversation_history

        # 回填 pipeline_run_id 到关联的任务
        pipeline_run_id = final_state.get("pipeline_id", "")
        if pipeline_run_id:
            submitted_task_id = final_state.get(
                "submitted_task_id"
            )
            if submitted_task_id:
                task_service = self._services.get("task_service")
                if (
                    task_service
                    and hasattr(task_service, "bind_pipeline_run")
                ):
                    try:
                        task_service.bind_pipeline_run(
                            submitted_task_id, pipeline_run_id
                        )
                        logger.info(
                            "Bound task %s to pipeline_run %s",
                            submitted_task_id,
                            pipeline_run_id,
                        )
                        exec_storage = self._services.get(
                            "execution_record_storage"
                        )
                        if exec_storage:
                            root_id = (
                                task_service.get_root_task_id(
                                    submitted_task_id
                                )
                            )
                            if root_id:
                                exec_storage.register_pipeline(
                                    pipeline_run_id, root_id
                                )
                    except Exception as exc:
                        logger.warning(
                            "Failed to bind pipeline_run_id: %s",
                            exc,
                        )
            logger.info(
                "Pipeline run completed: pipeline_id=%s",
                pipeline_run_id,
            )

        await self._output_adapter.send(
            final_state, streamed=self._streaming
        )

        # 显示管道产生的工具调用信息
        self._display_tool_calls_from_state(final_state)

        # 更新对话轮次
        self._turn_count += 1

        # 更新对话历史
        final_messages = final_state.get("messages", [])
        if final_messages:
            conversation_history = list(final_messages)
        else:
            user_input = initial_state.get("user_input", "")
            raw_result = final_state.get("raw_result", "")
            if user_input:
                conversation_history.append(
                    {"role": "user", "content": user_input}
                )
            if raw_result:
                conversation_history.append(
                    {"role": "assistant", "content": raw_result}
                )

        # 更新状态栏
        ctx_pct = self._estimate_context_pct(conversation_history)
        task_stats = self._get_task_stats()
        iteration = final_state.get("iteration", 0)
        max_iterations = final_state.get("max_iterations", 0)
        self._output_adapter.update_status_bar(
            turn_count=self._turn_count,
            context_pct=ctx_pct,
            is_processing=False,
            pipeline_running=False,
            pipeline_iteration=iteration,
            pipeline_max_iterations=max_iterations,
            running_task_count=task_stats["running"],
            pending_task_count=task_stats["pending"],
            completed_task_count=task_stats["completed"],
            failed_task_count=task_stats["failed"],
        )

        return conversation_history

    def _update_status_bar_idle(self) -> None:
        """将状态栏更新为空闲状态。"""
        task_stats = self._get_task_stats()
        self._output_adapter.update_status_bar(
            is_processing=False,
            pipeline_running=False,
            running_task_count=task_stats["running"],
            pending_task_count=task_stats["pending"],
            completed_task_count=task_stats["completed"],
            failed_task_count=task_stats["failed"],
        )

    async def _wait_input_or_interaction(
        self,
        cli_notifier: Any,
        console: Console,
    ) -> dict[str, Any] | None:
        """等待用户输入或交互请求，先到先处理。

        Returns:
            用户输入的 state 字典，或 None 表示交互已处理。
        """
        receive_task = asyncio.create_task(
            self._input_adapter.receive()
        )

        if cli_notifier is None:
            try:
                return await receive_task, False
            except (EOFError, KeyboardInterrupt):
                return {"should_stop": True}, False
            except Exception as _recv_exc:
                logger.warning(
                    "[_wait_for_next_event] receive error "
                    "(no cli_notifier): %s",
                    _recv_exc, exc_info=True,
                )
                return None, False

        async def _poll_interaction() -> None:
            while not cli_notifier.has_pending():
                await asyncio.sleep(0.3)

        interaction_task = asyncio.create_task(_poll_interaction())

        done, pending = await asyncio.wait(
            {receive_task, interaction_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()

        if receive_task in done:
            return receive_task.result()

        # 交互请求到达 → 中断 stdin 读取
        self._input_adapter.interrupt_stdin()
        try:
            await receive_task
        except (asyncio.CancelledError, Exception):
            pass
        self._input_adapter.drain_stdin()

        # 子对话期间抑制管道流式输出
        self._suppress_streaming = True
        try:
            human_svc = self._services.get(
                "human_interaction_service"
            )
            from channels.cli.cli_interaction import (
                run_sub_conversation,
            )
            await run_sub_conversation(
                console=console,
                input_adapter=self._input_adapter,
                notifier=cli_notifier,
                interaction_service=human_svc,
                idle_timeout=60,
            )
        finally:
            self._suppress_streaming = False
            # 回放缓冲的管道输出
            if self._streaming_buffer:
                safe = sanitize_for_terminal(
                    "".join(self._streaming_buffer)
                )
                console.print(safe, end="", highlight=False)
                self._last_was_text = True
                self._streaming_buffer.clear()

        self._input_adapter.drain_stdin()
        return None

    async def _wait_for_next_event(
        self,
        cli_notifier: Any,
        console: Console,
    ) -> tuple[dict[str, Any] | None, bool]:
        """等待下一个事件：用户输入、交互请求或管道完成。

        提示符已显示，在此等待任一事件发生。

        Returns:
            (initial_state, pipeline_done)
            - initial_state: 用户输入的 state，或 None（交互已处理）
            - pipeline_done: 管道是否已完成（需要回到循环顶部处理）
        """
        receive_task = asyncio.create_task(
            self._input_adapter.receive()
        )

        tasks: dict[asyncio.Task, str] = {receive_task: "input"}

        if cli_notifier is not None:
            async def _poll_interaction() -> None:
                while not cli_notifier.has_pending():
                    await asyncio.sleep(0.3)
            interaction_task = asyncio.create_task(
                _poll_interaction()
            )
            tasks[interaction_task] = "interaction"

        if (
            self._pipeline_task is not None
            and not self._pipeline_task.done()
        ):
            tasks[self._pipeline_task] = "pipeline"

        done, pending = await asyncio.wait(
            set(tasks.keys()),
            return_when=asyncio.FIRST_COMPLETED,
        )

        done_tags = [tasks.get(t) for t in done]
        pending_tags = [tasks.get(t) for t in pending]
        logger.info(
            "[_wait_for_next_event] done=%s pending=%s",
            done_tags, pending_tags,
        )

        # 取消非管道的 pending 任务
        for t in pending:
            if t != self._pipeline_task:
                t.cancel()

        # 判断哪个事件先完成（优先级：input > pipeline > interaction）
        for t in done:
            tag = tasks.get(t)
            if tag == "input":
                try:
                    result = t.result()
                    logger.info(
                        "[_wait_for_next_event] input result: "
                        "stop=%s empty=%s interrupted=%s",
                        result.get("should_stop"),
                        result.get("_is_empty"),
                        result.get("_interrupted"),
                    )
                    return result, False
                except (EOFError, KeyboardInterrupt):
                    logger.warning(
                        "[_wait_for_next_event] input EOFError"
                    )
                    return {"should_stop": True}, False
                except Exception as _input_exc:
                    # 输入适配器异常不应导致 CLI 退出。
                    # 记录日志并返回 None 让主循环继续。
                    logger.warning(
                        "[_wait_for_next_event] Input adapter error: %s",
                        _input_exc, exc_info=True,
                    )
                    return None, False
            elif tag == "pipeline":
                # 管道完成 → 中断 stdin，回到循环顶部处理
                if receive_task not in done:
                    self._input_adapter.interrupt_stdin()
                    try:
                        await receive_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._input_adapter.drain_stdin()
                return None, True
            elif tag == "interaction":
                # 交互请求 → 中断 stdin，处理子对话
                self._input_adapter.interrupt_stdin()
                try:
                    await receive_task
                except (asyncio.CancelledError, Exception):
                    pass
                # 清除残留的 interrupt 信号，防止
                # run_sub_conversation 中的 stdin 读取
                # 立即返回 None（假 EOF）
                self._input_adapter.drain_stdin()

                self._suppress_streaming = True
                try:
                    human_svc = self._services.get(
                        "human_interaction_service"
                    )
                    from channels.cli.cli_interaction import (
                        run_sub_conversation,
                    )
                    await run_sub_conversation(
                        console=console,
                        input_adapter=self._input_adapter,
                        notifier=cli_notifier,
                        interaction_service=human_svc,
                        idle_timeout=60,
                    )
                except Exception as _sub_conv_exc:
                    logger.warning(
                        "[_wait_for_next_event] run_sub_conversation "
                        "error: %s", _sub_conv_exc, exc_info=True,
                    )
                finally:
                    self._suppress_streaming = False
                    if self._streaming_buffer:
                        safe = sanitize_for_terminal(
                            "".join(self._streaming_buffer)
                        )
                        console.print(
                            safe, end="", highlight=False
                        )
                        self._last_was_text = True
                        self._streaming_buffer.clear()
                self._input_adapter.drain_stdin()
                return None, False

        # 兜底（不应到达）
        return None, False

    async def _handle_slash_command(self, state: dict[str, Any]) -> CommandResult | None:
        """处理斜杠命令。

        Args:
            state: 包含 _is_slash_command 标记的 state

        Returns:
            命令执行结果，None 表示跳过
        """
        user_input = state.get("user_input", "")

        # 构建命令执行上下文
        cmd_context = self._build_command_context()

        # 执行命令
        result = await self._command_registry.execute(user_input, cmd_context)
        return result

    def _build_command_context(self) -> dict[str, Any]:
        """构建斜杠命令执行上下文。

        Returns:
            包含 services/config/state 等引用的上下文字典
        """
        return {
            "services": self._services,
            "agent_config": self._agent_config,
            "mode": self._interaction_mode,
            "show_thinking": self._show_thinking,
            "turn_count": self._turn_count,
            "conversation_history": [],
            "last_state": {},
        }

    def _apply_command_updates(self, updates: dict[str, Any]) -> None:
        """应用斜杠命令产生的状态更新。

        Args:
            updates: 命令返回的 state_updates 字典
        """
        # 交互模式切换
        if "interaction_mode" in updates:
            new_mode = updates["interaction_mode"]
            if new_mode in ("normal", "auto", "plan"):
                self._interaction_mode = new_mode
                self._output_adapter.update_status_bar(mode=new_mode)
                # 更新输入提示符
                mode_label = new_mode.upper()
                agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"
                self._input_adapter._prompt_str = f"[{mode_label}] {agent_name} > "

        # 思考过程显示切换
        if "show_thinking" in updates:
            self._show_thinking = updates["show_thinking"]
            self._output_adapter.show_thinking = self._show_thinking

        # 模型切换
        if "model_override" in updates:
            model_name = updates["model_override"]
            self._output_adapter.update_status_bar(model_name=model_name)

    def _build_on_chunk_callback(self, console: Console) -> Any:
        """构建流式输出的 on_chunk 回调。

        处理五种 chunk 类型：
        - type='text': 正常回复内容，逐 token 输出
        - type='thinking': 思考过程内容，根据 show_thinking 决定是否显示
        - type='tool_call': LLM 返回工具调用，实时显示工具名称
        - type='tool_result': 工具执行完成，显示执行结果
        - type='tool_start': 工具开始执行，显示执行中指示
        - type='iteration': 管道迭代进度，更新状态栏

        Args:
            console: rich Console 实例

        Returns:
            on_chunk 回调函数
        """
        _displayed_tool_indices: set[int] = set()
        self._last_was_text = False
        self._text_output_received = False
        self._last_chunk_time = 0

        def on_chunk(chunk: dict[str, Any]) -> None:
            """流式回调：将管道事件实时输出到终端。"""
            # 子对话期间抑制管道输出，缓冲到 _streaming_buffer
            if self._suppress_streaming:
                chunk_type = chunk.get("type", "text")
                _content = chunk.get("content", "")
                if chunk_type == "text" and _content:
                    self._streaming_buffer.append(_content)
                return

            chunk_type = chunk.get("type", "text")
            content = chunk.get("content", "")
            self._last_chunk_time = _time.monotonic()

            if chunk_type == "thinking":
                if self._show_thinking and content:
                    safe = sanitize_for_terminal(content)
                    console.print(safe, end="", highlight=False)
                    self._last_was_text = True
                return

            if chunk_type == "text":
                if content:
                    safe = sanitize_for_terminal(content)
                    console.print(safe, end="", highlight=False)
                    self._last_was_text = True
                    self._text_output_received = True
                return

            # 非文本 chunk：先结束之前的文本行
            if self._last_was_text:
                print()
                self._last_was_text = False

            if chunk_type == "tool_call":
                tool_calls_data = chunk.get("tool_calls", [])
                for tc in tool_calls_data:
                    tc_idx = getattr(tc, "index", 0)
                    if tc_idx in _displayed_tool_indices:
                        continue
                    func = getattr(tc, "function", None)
                    if func:
                        name = getattr(func, "name", "")
                        if name:
                            _displayed_tool_indices.add(tc_idx)
                            args_str = getattr(func, "arguments", "")
                            try:
                                import json as _json
                                args = _json.loads(args_str) if args_str else {}
                            except Exception:
                                args = {}
                            self._output_adapter.show_tool_call(name, args)
                return

            if chunk_type == "tool_start":
                tool_name = chunk.get("tool_name", "unknown")
                console.print(
                    f"  [dim yellow]>> 执行 {tool_name}...[/dim yellow]"
                )
                return

            if chunk_type == "tool_result":
                tool_name = chunk.get("tool_name", "unknown")
                result = chunk.get("result", "")
                success = chunk.get("success", True)
                duration_ms = chunk.get("duration_ms", 0)
                self._output_adapter.show_tool_result(
                    tool_name, result, success=success, duration_ms=duration_ms
                )
                return

            if chunk_type == "iteration":
                iteration = chunk.get("iteration", 0)
                max_iterations = chunk.get("max_iterations", 0)
                self._output_adapter.update_status_bar(
                    pipeline_iteration=iteration,
                    pipeline_max_iterations=max_iterations,
                    pipeline_running=True,
                )
                self._output_adapter.render_status_bar()
                return

        return on_chunk

    def _display_tool_calls_from_state(self, state: dict[str, Any]) -> None:
        """从管道最终 state 中显示工具调用信息（非流式模式的兜底显示）。

        流式模式下工具调用已通过 on_chunk 实时显示，此方法仅显示
        迭代信息等补充内容，避免重复显示。

        Args:
            state: 管道引擎的最终 state 字典
        """
        if not self._streaming:
            tool_results = state.get("tool_results")
            if tool_results and isinstance(tool_results, list):
                for tr in tool_results:
                    if isinstance(tr, dict):
                        tool_name = tr.get("tool_name", "unknown")
                        data = tr.get("data", tr.get("error", ""))
                        success = tr.get("success", True)
                        duration_ms = tr.get("duration_ms", 0)
                        self._output_adapter.show_tool_call(tool_name)
                        self._output_adapter.show_tool_result(
                            tool_name, str(data), success=success,
                            duration_ms=duration_ms,
                        )

            raw_tool_calls = state.get("raw_tool_calls")
            if raw_tool_calls and isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        name = func.get("name", tc.get("name", "unknown"))
                        args_str = func.get("arguments", tc.get("args", ""))
                        try:
                            import json
                            args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        self._output_adapter.show_tool_call(name, args)

        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 0)
        if iteration and max_iterations and iteration > 1:
            self._output_adapter.show_iteration(iteration, max_iterations)

    def _get_model_name(self) -> str:
        """获取当前模型名称。

        Returns:
            模型名称字符串
        """
        if self._agent_config:
            if hasattr(self._agent_config, "model"):
                return self._agent_config.model
            if hasattr(self._agent_config, "config_id"):
                return self._agent_config.config_id
        return "unknown"

    def _get_task_stats(self) -> dict[str, int]:
        """收集任务状态统计。

        从 TaskService 中获取各状态的任务数量。

        Returns:
            包含 running/pending/completed/failed 计数的字典
        """
        stats = {"running": 0, "pending": 0, "completed": 0, "failed": 0}
        task_service = self._services.get("task_service")
        if task_service is None:
            return stats
        try:
            from tasks.types import TaskStatus
            stats["running"] = len(task_service.list_by_status(TaskStatus.RUNNING))
            stats["pending"] = len(task_service.list_by_status(TaskStatus.PENDING))
            stats["completed"] = len(task_service.list_by_status(TaskStatus.COMPLETED))
            stats["failed"] = len(task_service.list_by_status(TaskStatus.FAILED))
        except Exception as exc:
            logger.debug("Failed to collect task stats: %s", exc)
        return stats

    def _estimate_context_pct(self, history: list[dict[str, Any]]) -> float:
        """估算上下文占用百分比。

        Args:
            history: 对话历史消息列表

        Returns:
            占用百分比 (0-100)
        """
        if not history:
            return 0.0

        char_count = sum(
            len(m.get("content", "")) if isinstance(m, dict) else len(str(m))
            for m in history
        )
        estimated_tokens = char_count // 3
        max_context = 128000
        return min(100.0, estimated_tokens / max_context * 100)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI 入口函数，启动交互式管道应用。

    支持命令行参数：
    - ``--config PATH``: 指定管道配置 YAML 路径
    - ``--debug``: 启用调试日志
    - ``--no-streaming``: 禁用流式输出
    - ``--mode MODE``: 交互模式 (normal/auto/plan)
    """
    parser = argparse.ArgumentParser(description="Agent OS CLI — Claude Code 风格插件化管道交互式命令行")
    parser.add_argument("--config", type=str, default=None, help="管道配置 YAML 路径")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument("--no-streaming", action="store_true", help="禁用流式输出（默认启用）")
    parser.add_argument(
        "--mode", type=str, default="normal",
        choices=["normal", "auto", "plan"],
        help="交互模式 (normal/auto/plan)",
    )
    parser.add_argument("--message", "-m", type=str, default=None, help="直接发送消息（非交互模式）")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    streaming = not args.no_streaming
    app = CLIApplication(streaming=streaming)
    app._interaction_mode = args.mode
    app.setup_pipeline(config_path=args.config)

    try:
        if args.message:
            asyncio.run(app.run_single(args.message))
        else:
            asyncio.run(app.run())
    finally:
        try:
            from llm.adapter import cleanup_litellm_resources_sync
            cleanup_litellm_resources_sync()
        except Exception:
            pass


if __name__ == "__main__":
    main()
