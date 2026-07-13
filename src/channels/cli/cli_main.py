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
import contextlib
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
            with contextlib.suppress(Exception):
                _stream.reconfigure(encoding="utf-8", errors="replace")
    # 启用 Windows CMD 的 ANSI escape code 支持
    try:
        import ctypes

        _kernel32 = ctypes.windll.kernel32
        for _handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            _handle = _kernel32.GetStdHandle(_handle_id)
            _mode = ctypes.c_ulong()
            if _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)):
                _kernel32.SetConsoleMode(
                    _handle,
                    _mode.value | 0x0004,  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
    except Exception:
        pass

from channels.cli.cli_commands import SlashCommandRegistry
from channels.cli.cli_interactive import CLIInteractiveMixin

# 导入拆分后的混入类
from channels.cli.cli_runner import CLIRunnerMixin
from channels.cli.cli_single import CLISingleMixin
from channels.cli.input_adapter import CLIInputAdapter
from channels.cli.output_adapter import CLIOutputAdapter
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
    """初始化统一日志系统（已转发到 src.core.logging）。

    在所有入口点调用一次即可。重复调用不会重复初始化。

    Args:
        debug: 是否启用 DEBUG 级别（终端也会显示管道内部日志）
        log_dir: 日志目录路径，默认为项目根目录下的 logs/
    """
    global _LOGGING_INITIALIZED  # noqa: PLW0603
    if _LOGGING_INITIALIZED:
        return
    _LOGGING_INITIALIZED = True

    _log_dir = _PROJECT_ROOT / "logs" if log_dir is None else Path(log_dir)
    _log_dir.mkdir(parents=True, exist_ok=True)

    from src.core.logging import LoggingConfig, setup_logging as _unified_setup  # noqa: PLC0415

    config = LoggingConfig(
        level=logging.DEBUG if debug else logging.INFO,
        json_output=False,
        output="both",
        file_path=str(_log_dir / "agent_os.log"),
    )
    _unified_setup(config, reset=True)

    logger.info(
        "Logging initialized: console_level=%s, file=agent_os.log (DEBUG)",
        "DEBUG" if debug else "INFO",
    )

    if not debug:
        _SUPPRESSED_NS = (  # noqa: N806
            "pipeline.",
            "httpcore",
            "httpx",
            "LiteLLM",
            "openai",
            "isolation.",
            "infrastructure.",
            "tools.",
            "plugins.",
            "llm.",
            "src.tools.",
            "src.plugins.",
            "src.llm.",
            "evaluation",
            "tasks",
            "memory",
            "human_interaction",
            "channels.cli.",
            "__main__",
            "asyncio",
        )

        def _console_filter(record: logging.LogRecord) -> bool:
            # 外部库（非内部命名空间）→ 全部放行
            if not any(record.name.startswith(_ns) for _ns in _SUPPRESSED_NS):  # noqa: SIM103
                return True
            # 内部命名空间：错误已通过 output adapter 结构化显示，
            # 不再重复输出到终端，避免长 traceback 泄露
            return False

        _console_handler = logging.getLogger().handlers[0]
        _console_handler.addFilter(_console_filter)


# 默认管道配置路径 -- 优先项目根目录的 config/，回退到 src/config/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_PIPELINE_CONFIG = _PROJECT_ROOT / "config" / "pipelines" / "default.yaml"

_SESSION_DIR = _PROJECT_ROOT / "data" / "session"

_DEFAULT_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
# No hard limit on session messages — context compression handles overflow


# ---------------------------------------------------------------------------
# CLI Application（Claude Code 风格）
# ---------------------------------------------------------------------------


class CLIApplication(CLIRunnerMixin, CLISingleMixin, CLIInteractiveMixin):
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
        from config.models import get_model_config_loader  # noqa: PLC0415
        from pipeline.config import build_plugin_registry, load_pipeline_config  # noqa: PLC0415

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

        # 创建 ModelConfigLoader（使用缓存单例避免重复解析 YAML）
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
            pipeline_config,
            model_loader=model_loader,
            router=None,
        )
        logger.info(
            "[STARTUP] build_plugin_registry: %.2fs",
            _time.monotonic() - _t0,
        )

        # 获取共享 Router
        from llm.router_factory import get_or_create_router  # noqa: PLC0415

        router = get_or_create_router(model_loader)

        # 使用配置中的路由表
        self._input_route_table = pipeline_config.input_route_table
        self._output_route_table = pipeline_config.output_route_table

        # 加载 Agent 配置（使用全局单例 registry，热重载统一生效）
        from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

        agent_registry = get_global_agent_registry_sync()

        # 创建共享服务 → 注入 PipelineEngine
        _t1 = _time.monotonic()
        self._services = self._build_services(agent_registry=agent_registry)
        logger.info(
            "[STARTUP] _build_services: %.2fs",
            _time.monotonic() - _t1,
        )

        # 注入 model_loader 和 router 到 services
        self._services["model_loader"] = model_loader
        if router is not None:
            self._services["llm_router"] = router

        # 如果 ToolCore 存在，从 ToolRegistry 注册工具
        self._register_tools_to_core()

        # 加载 Agent 配置
        self._load_agent_config(agent_registry)

        # 创建管道引擎
        _t2 = _time.monotonic()
        checkpoint_mgr = self._services.get("checkpoint_manager")
        self._engine = PipelineEngine(
            input_route_table=self._input_route_table,
            output_route_table=self._output_route_table,
            plugin_registry=self._plugin_registry,
            services=self._services,
            checkpoint_manager=checkpoint_mgr,
        )
        logger.info(
            "PipelineEngine created (direct call, no Worker) %.2fs",
            _time.monotonic() - _t2,
        )

        # 注册 llm_core 服务并注入 LLM 调用到 context_service
        self._register_llm_core_service(router)

        # 初始化任务执行器
        self._init_task_worker(config_path)

        logger.info("Real pipeline setup complete: name=%s", pipeline_config.name)

    def _register_tools_to_core(self) -> None:
        """注册核心工具到 ToolCore 并注入隔离执行器。"""
        tool_core = self._plugin_registry.get_core("tool_execute")
        if tool_core is None:
            return

        tool_registry = self._services.get("tool_registry")
        if tool_registry is not None:
            try:
                from tools.builtin import register_core_tools  # noqa: PLC0415

                registered = register_core_tools(tool_registry, session=None)
                logger.info("ToolCore registered %d core tools", len(registered))
            except Exception as exc:
                logger.warning("register_core_tools failed: %s", exc)
            tool_core.register_tools_from_registry(tool_registry)
            # Docker 容器隔离通过 IsolationManager 统一管理

    def _load_agent_config(self, agent_registry: Any) -> None:
        """从 AgentRegistry 加载 Agent 配置。

        Args:
            agent_registry: Agent 注册表实例
        """
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

    def _register_llm_core_service(self, router: Any) -> None:
        """注册 llm_core 为服务，并将 LLM 调用能力注入到 context_service。

        Args:
            router: LLM Router 实例
        """
        llm_core_plugin = self._plugin_registry.get_core("llm_call")
        if llm_core_plugin is None:
            return

        self._services["llm_core"] = llm_core_plugin
        logger.info("Service registered: llm_core (from plugin registry)")

        context_svc = self._services.get("context_service")
        if context_svc is not None and hasattr(llm_core_plugin, "_adapter"):
            from llm.adapter import LLMResponse  # noqa: PLC0415

            async def _llm_call_fn(prompt: str) -> str:
                if router is not None:
                    response: LLMResponse = await llm_core_plugin._adapter.completion(
                        model=llm_core_plugin._model,
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                    )
                else:
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

    def _init_task_worker(self, config_path: Path) -> None:
        """初始化任务执行器（事件驱动，用于后台任务处理）。

        Args:
            config_path: 管道配置文件路径
        """
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
            from tasks.service import TaskService  # noqa: PLC0415

            _provider = get_service_provider()
            task_service = self._services.get("task_service") or _provider.get_or_create(
                "task_service",
                lambda: TaskService(event_bus=_provider.get("event_bus")),
            )

            import yaml as _yaml  # noqa: PLC0415

            from infrastructure.task_worker import TaskWorker  # noqa: PLC0415

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

            def _eval_pipeline_factory() -> PipelineEngine:
                return PipelineEngine(
                    input_route_table=_prt,
                    output_route_table=_ort,
                    plugin_registry=_pr,
                    services=_svc,
                )

            # 注册 pipeline_factory 到 ServiceProvider
            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

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

    def _build_services(self, agent_registry: Any = None) -> dict[str, Any]:
        """构建共享服务字典（委托 Application 统一构建）。

        Args:
            agent_registry: Agent 注册表实例

        Returns:
            服务字典
        """
        from application import Application  # noqa: PLC0415

        app = Application(project_root=_PROJECT_ROOT)
        services = app.build_services(agent_registry=agent_registry)

        # 保存引用
        self._app = app
        self._event_bus = services.get("event_bus")

        # CLI 渠道特有服务（不属于后端通用服务）
        try:  # noqa: SIM105
            import human_interaction.desktop_notifier  # noqa: F401,PLC0415
        except Exception:
            pass

        try:
            from channels.cli.cli_interaction import CLIInteractionNotifier  # noqa: PLC0415
            from human_interaction import get_human_interaction_service  # noqa: PLC0415

            cli_notifier = CLIInteractionNotifier(console=self._output_adapter.console)
            human_svc = get_human_interaction_service()
            human_svc.set_notifier(cli_notifier)
            services["cli_notifier"] = cli_notifier
            services["human_interaction_service"] = human_svc
            logger.info("Service created: CLIInteractionNotifier -> HumanInteractionService")
        except Exception as exc:
            logger.warning("Failed to create CLIInteractionNotifier: %s", exc)

        return services


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
    parser.add_argument(
        "--no-streaming",
        action="store_true",
        help="禁用流式输出（默认启用）",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="normal",
        choices=["normal", "auto", "plan"],
        help="交互模式 (normal/auto/plan)",
    )
    parser.add_argument(
        "--message",
        "-m",
        type=str,
        default=None,
        help="直接发送消息（非交互模式）",
    )
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
            from llm.adapter import cleanup_litellm_resources_sync  # noqa: PLC0415

            cleanup_litellm_resources_sync()
        except Exception:
            pass


if __name__ == "__main__":
    main()
