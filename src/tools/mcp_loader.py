"""
MCP 工具加载器

暴露接口：
- parse_config(self, config_path: str, include_disabled: bool) -> list[MCPServerConfig]：parse_config功能
- tool_to_runnable(self, tool: Tool, handler: ToolHandler) -> 'ToolRunnable'：tool_to_runnable功能
- get_server_status(self) -> dict[str, str]：get_server_status功能
- MCPServerConfig：MCPServerConfig类
- MCPToolLoader：MCPToolLoader类
- MCPClient：从 mcp_client 模块重导出
"""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from core.exceptions import MCPConfigError, MCPConnectionError
from tools.mcp_client import MCPClient  # noqa: F401 — 重导出，保持向后兼容
from tools.types import Tool, ToolSource

if TYPE_CHECKING:
    from core.runnable import ToolRunnable


# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class MCPServerConfig(BaseModel):
    """MCP 服务器配置"""

    name: str = Field(..., description="服务器名称")
    command: str = Field(..., description="启动命令")
    args: list[str] = Field(default_factory=list, description="命令参数")
    env: dict[str, str] = Field(default_factory=dict, description="环境变量")
    disabled: bool = Field(False, description="是否禁用")


class MCPToolLoader:
    """
    MCP 工具加载器

    负责从 MCP 服务器加载工具定义
    """

    def __init__(self):
        """初始化加载器"""
        self._connections: dict[str, Any] = {}

    def parse_config(
        self,
        config_path: str | Path,
        include_disabled: bool = True,
    ) -> list[MCPServerConfig]:
        """解析 MCP 配置文件（支持 JSON 和 YAML 格式）"""
        config_path = Path(config_path)
        if not config_path.exists():
            raise MCPConfigError(f"配置文件不存在: {config_path}")

        servers = {}

        if config_path.suffix in [".yaml", ".yml"]:
            import yaml  # noqa: PLC0415

            try:
                with open(config_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise MCPConfigError(f"YAML 解析失败: {e}") from e

            servers = data.get("mcp_servers", {})

        elif config_path.suffix == ".json":
            try:
                content = config_path.read_text(encoding="utf-8")
                data = json.loads(content)
            except json.JSONDecodeError as e:
                raise MCPConfigError(f"JSON 解析失败: {e}") from e

            servers = data.get("mcpServers", {})

        else:
            raise MCPConfigError(f"不支持的配置文件格式: {config_path.suffix}")

        configs = []
        for name, server_config in servers.items():
            if isinstance(server_config, dict):
                config = MCPServerConfig(
                    name=name,
                    command=server_config.get("command", ""),
                    args=server_config.get("args", []),
                    env=server_config.get("env", {}),
                    disabled=server_config.get("disabled", False),
                )
            else:
                continue

            if include_disabled or not config.disabled:
                configs.append(config)

        return configs

    async def load_from_server(self, config: MCPServerConfig) -> list[Tool]:
        """从 MCP 服务器加载工具"""
        try:
            client = await self._connect_server(config)
            tools_response = await client.list_tools()

            tools = []
            for mcp_tool in tools_response.tools:
                tool = self._convert_mcp_tool(mcp_tool, config.name)
                tools.append(tool)

            return tools

        except Exception as e:
            raise MCPConnectionError(
                message=f"MCP 服务器 '{config.name}' 加载工具失败",
                details={"server": config.name, "error": str(e)},
                cause=e,
            ) from e

    async def load_from_config(
        self,
        config_path: Path,
        include_disabled: bool = False,
    ) -> list[Tool]:
        """从配置文件加载所有工具"""
        configs = self.parse_config(config_path, include_disabled)
        all_tools = []

        for config in configs:
            if not config.disabled:
                try:
                    tools = await self.load_from_server(config)
                    all_tools.extend(tools)
                except MCPConnectionError:
                    pass

        return all_tools

    async def load_from_directory(
        self,
        directory: str | Path,
        include_disabled: bool = False,
    ) -> list[Tool]:
        """
        自动扫描目录下所有 mcp.json 并加载工具

        目录结构：
          mcp-servers/
            server-a/
              mcp.json
              src/ ...
            server-b/
              mcp.json
              src/ ...
        """
        import logging  # noqa: PLC0415

        logger = logging.getLogger(__name__)
        directory = Path(directory)
        if not directory.exists():
            logger.warning("[MCP] MCP 服务器目录不存在: %s", directory)
            return []

        all_tools = []
        mcp_json_files = sorted(directory.glob("*/mcp.json"))

        if not mcp_json_files:
            logger.info("[MCP] 未找到 MCP 服务器配置（mcp.json）")
            return []

        logger.info("[MCP] 发现 %d 个 MCP 服务器配置", len(mcp_json_files))

        for mcp_json_path in mcp_json_files:
            server_name = mcp_json_path.parent.name
            try:
                configs = self.parse_config(mcp_json_path, include_disabled)
                if not configs:
                    continue

                for config in configs:
                    if config.name == "unnamed":
                        config.name = server_name

                    # 将相对路径的 args 转换为相对于项目根目录的绝对路径
                    config = self._resolve_args_paths(config, directory)  # noqa: PLW2901

                    if not config.disabled:
                        try:
                            tools = await self.load_from_server(config)
                            all_tools.extend(tools)
                            logger.info(
                                "[MCP] 服务器加载成功 | server=%s | tools=%d",
                                config.name,
                                len(tools),
                            )
                        except MCPConnectionError as e:
                            logger.warning(
                                "[MCP] 服务器连接失败 | server=%s | error=%s",
                                config.name,
                                e,
                            )

            except MCPConfigError as e:
                logger.warning(
                    "[MCP] 配置解析失败 | file=%s | error=%s",
                    mcp_json_path,
                    e,
                )

        return all_tools

    def _resolve_args_paths(self, config: MCPServerConfig, base_dir: Path) -> MCPServerConfig:
        """
        将配置中相对路径的 args 转换为相对于 base_dir 的绝对路径

        只处理看起来像文件路径的参数（如 .js .py .ts 后缀）
        """
        resolved_args = []
        for arg in config.args:
            arg_path = Path(arg)
            if arg_path.suffix in (".js", ".ts", ".py", ".mjs") and not arg_path.is_absolute():
                absolute_path = (base_dir / config.name / arg).resolve()
                if absolute_path.exists():
                    resolved_args.append(str(absolute_path))
                else:
                    resolved_args.append(arg)
            else:
                resolved_args.append(arg)

        if resolved_args != config.args:
            return MCPServerConfig(
                name=config.name,
                command=config.command,
                args=resolved_args,
                env=config.env,
                disabled=config.disabled,
            )
        return config

    async def load_as_runnables(
        self,
        config_path: Path,
        handlers: dict[str, ToolHandler],
        include_disabled: bool = False,
    ) -> list["ToolRunnable"]:
        """从配置文件加载工具并转换为 Runnable"""
        from tools.mcp_adapter import mcp_tool_to_runnable  # noqa: PLC0415

        tools = await self.load_from_config(config_path, include_disabled)
        runnables = []

        for tool in tools:
            handler = handlers.get(tool.name)
            if handler:
                runnable = mcp_tool_to_runnable(tool, handler)
                runnables.append(runnable)

        return runnables

    def tool_to_runnable(
        self,
        tool: Tool,
        handler: ToolHandler,
    ) -> "ToolRunnable":
        """将单个 MCP 工具转换为 Runnable"""
        from tools.mcp_adapter import mcp_tool_to_runnable  # noqa: PLC0415

        return mcp_tool_to_runnable(tool, handler)

    def _convert_mcp_tool(
        self,
        mcp_tool: Any,
        server_name: str,
    ) -> Tool:
        """转换 MCP 工具格式为内部格式"""
        # 支持字典和对象两种格式
        if isinstance(mcp_tool, dict):
            name = mcp_tool.get("name", "")
            description = mcp_tool.get("description", "")
            input_schema = mcp_tool.get("inputSchema", {"type": "object"})
        else:
            name = getattr(mcp_tool, "name", "")
            description = getattr(mcp_tool, "description", "")
            input_schema = getattr(mcp_tool, "inputSchema", {"type": "object"})

        return Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema={"type": "object"},
            category=self._infer_tool_category(name, description),
            level="user",
            version="1.0.0",
            status="active",
            source=ToolSource.MCP,
            metadata={"server": server_name},
        )

    def _infer_tool_category(self, name: str, description: str) -> str:  # noqa: PLR0911
        """根据工具名称和描述推断工具类别"""
        name_lower = name.lower()
        desc_lower = description.lower() if description else ""

        # 文件操作
        file_keywords = ["file", "read", "write", "edit", "create", "delete", "save"]
        if any(keyword in name_lower or keyword in desc_lower for keyword in file_keywords):
            return "file"

        # 搜索
        if any(keyword in name_lower or keyword in desc_lower for keyword in ["search", "find", "lookup", "query"]):
            return "search"

        # Web 操作
        if any(keyword in name_lower or keyword in desc_lower for keyword in ["web", "http", "url", "fetch", "browse"]):
            return "web"

        # 记忆
        if any(keyword in name_lower or keyword in desc_lower for keyword in ["memory", "remember", "recall", "store"]):
            return "memory"

        # 任务
        if any(keyword in name_lower or keyword in desc_lower for keyword in ["task", "execute", "run", "complete"]):
            return "task"

        # 分析
        if any(
            keyword in name_lower or keyword in desc_lower for keyword in ["analyze", "evaluate", "assess", "check"]
        ):
            return "analysis"

        # 默认为系统工具
        return "system"

    def _start_stderr_consumer(self, process, server_name: str):
        """启动后台任务消费子进程 stderr，防止管道缓冲区满导致进程挂起

        当 MCP 子进程向 stderr 输出大量日志时，管道缓冲区（通常 64KB）填满后
        操作系统会阻塞写操作，导致子进程挂起。此方法通过后台线程或异步任务
        持续消费 stderr 输出来避免该问题。

        Args:
            process: 子进程对象（subprocess.Popen 或 asyncio.subprocess.Process）
            server_name: MCP 服务器名称，用于日志前缀
        """
        if isinstance(process, subprocess.Popen):
            # sync 模式：使用守护线程消费 stderr
            def _consume():
                try:
                    for line in process.stderr:
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").strip()
                        if text:
                            logger.debug("[MCP:%s stderr] %s", server_name, text)  # noqa: F821
                except Exception:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        process.stderr.close()

            thread = threading.Thread(target=_consume, daemon=True)
            thread.start()
        else:
            # async 模式：使用 asyncio 任务消费 stderr
            async def _consume_async():
                try:
                    while True:
                        line = await process.stderr.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").strip()
                        if text:
                            logger.debug("[MCP:%s stderr] %s", server_name, text)  # noqa: F821
                except Exception:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        process.stderr.close()

            with contextlib.suppress(RuntimeError):
                asyncio.ensure_future(_consume_async())

    async def _connect_server(self, config: MCPServerConfig) -> Any:
        """连接 MCP 服务器

        用 subprocess.Popen 创建子进程并 asyncio.to_thread 包装 I/O（Windows 上
        uvicorn --reload 的 SelectorEventLoop 不支持 asyncio.create_subprocess_exec）。
        返回缓存连接前先检测子进程存活状态，已退出则清理缓存并重建连接，避免写入
        已关闭的 stdin 或等待超时。
        """
        import logging  # noqa: PLC0415
        import sys  # noqa: PLC0415

        logger = logging.getLogger(__name__)

        if config.name in self._connections:
            cached_client = self._connections[config.name]
            if cached_client.is_alive():
                return cached_client
            logger.warning("MCP 子进程已退出，清理并重连 | server=%s", config.name)
            with contextlib.suppress(Exception):
                await cached_client.close()
            del self._connections[config.name]

        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                env = {**os.environ, **config.env}

                use_sync = False
                if sys.platform == "win32":
                    try:
                        loop = asyncio.get_running_loop()
                        if not isinstance(loop, asyncio.ProactorEventLoop):
                            use_sync = True
                            logger.info(
                                "当前事件循环不支持子进程，使用 subprocess.Popen | server=%s | loop=%s",
                                config.name,
                                type(loop).__name__,
                            )
                    except RuntimeError:
                        pass

                if use_sync:
                    process = subprocess.Popen(
                        [config.command, *config.args],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    self._start_stderr_consumer(process, config.name)
                    client = MCPClient(process, config.name, use_sync=True)
                else:
                    process = await asyncio.create_subprocess_exec(
                        config.command,
                        *config.args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        limit=1024 * 1024,
                    )
                    self._start_stderr_consumer(process, config.name)
                    client = MCPClient(process, config.name, use_sync=False)

                await client.initialize()

                self._connections[config.name] = client

                logger.info(
                    "成功连接到 MCP 服务器: %s (sync=%s, attempt=%d/%d)",
                    config.name,
                    use_sync,
                    attempt + 1,
                    max_retries,
                )
                return client

            except Exception as e:
                last_error = e
                logger.warning(
                    "连接 MCP 服务器失败 %s: %s (attempt=%d/%d)",
                    config.name,
                    e,
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                else:
                    logger.error("连接 MCP 服务器最终失败 %s: %s", config.name, e)
                    raise MCPConnectionError(
                        message=f"连接 MCP 服务器 '{config.name}' 失败（重试 {max_retries} 次后）",
                        details={"server": config.name, "error": str(e)},
                        cause=last_error,
                    ) from last_error
        return None

    def get_server_status(self) -> dict[str, str]:
        """获取所有服务器状态"""
        return {name: "connected" if client else "disconnected" for name, client in self._connections.items()}

    async def call_tool(
        self,
        server_config: "MCPServerConfig",
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 60.0,
        overall_timeout: float | None = None,
    ) -> Any:
        """统一工具调用入口（自动检测连接状态 + 失败重连重试）

        所有 MCP 工具调用应通过此方法，而非直接获取 client 后调用。
        此方法保证：
        1. 调用前检测子进程存活状态
        2. 调用失败时自动清理死连接并重建
        3. 最多重试 1 次（连接恢复后）
        4. overall_timeout 保护整个重试流程不被无限挂起

        Args:
            server_config: MCP 服务器配置
            tool_name: 工具名称
            arguments: 工具参数
            timeout: 单次调用超时时间（秒）
            overall_timeout: 整体超时时间（秒），None 表示不限制

        Returns:
            工具调用结果
        """
        logger = logging.getLogger(__name__)

        async def _call_with_retry() -> Any:
            max_attempts = 2
            for attempt in range(max_attempts):
                client = await self._connect_server(server_config)
                try:
                    return await client.call_tool(tool_name, arguments, timeout=timeout)
                except MCPConnectionError:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "MCP 工具调用失败，清理连接并重试 | server=%s | tool=%s",
                            server_config.name,
                            tool_name,
                        )
                        await self.disconnect_server(server_config.name)
                    else:
                        raise
            return None

        if overall_timeout is not None:
            try:
                return await asyncio.wait_for(_call_with_retry(), timeout=overall_timeout)
            except asyncio.TimeoutError:
                raise MCPConnectionError(  # noqa: B904
                    message=(
                        f"MCP 整体调用超时（{overall_timeout}s），"
                        f"含重试仍未完成 | server={server_config.name} | tool={tool_name}"
                    ),
                    details={
                        "server": server_config.name,
                        "tool": tool_name,
                        "overall_timeout": overall_timeout,
                    },
                )
        return await _call_with_retry()

    async def disconnect_server(self, server_name: str) -> None:
        """断开服务器连接"""
        if server_name in self._connections:
            client = self._connections[server_name]
            if hasattr(client, "close"):
                await client.close()
            del self._connections[server_name]

    async def disconnect_all(self) -> None:
        """断开所有连接"""
        server_names = list(self._connections.keys())
        for name in server_names:
            await self.disconnect_server(name)
