"""工具注册表"""

from collections.abc import Callable, Coroutine
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)

from core.exceptions import ToolAlreadyExistsError, ToolNotFoundError
from core.registry_base import SimpleRegistry
from tools.interfaces import IToolRegistry
from tools.types import Tool, ToolCategory, ToolSource

if TYPE_CHECKING:
    from core.runnable import ToolRunnable
    from services.tool_sync_service import ToolSyncService


# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class ToolRegistry(SimpleRegistry[str, Tool], IToolRegistry):
    """工具注册表"""

    def __init__(self, sync_service: Optional["ToolSyncService"] = None, lazy_load: bool = True) -> None:
        """初始化注册表"""
        # 初始化基类
        SimpleRegistry.__init__(self)

        # 工具特定的数据结构
        self._handlers: dict[str, ToolHandler] = {}
        self._runnables: dict[str, ToolRunnable] = {}
        self._sync_service = sync_service
        self._lazy_load = lazy_load
        # 已知的工具名称（用于按需加载判断）
        self._known_tool_names: set[str] = set()

        # 动态加载的工具名称集合（由 auto_loader 在运行时加载的工具）。
        self._dynamic_tool_names: dict[str, set[str]] = {}

        # Schema 动态丰富器注册表（tool_name -> enricher callable）
        self._schema_enrichers: dict[str, Callable] = {}

        # 使用统计（用于 LRU 卸载策略）
        self._usage_count: dict[str, int] = {}  # 工具使用次数
        self._last_used: dict[str, float] = {}  # 最后使用时间戳

        # 卸载配置
        self._max_tools: int = 100  # 最大工具数量
        self._unload_threshold: int = 20  # 触发卸载时的清理数量

    def set_sync_service(self, sync_service: "ToolSyncService") -> None:
        """设置同步服务（支持延迟注入）"""
        self._sync_service = sync_service

    def configure_unload_policy(self, max_tools: int = 100, unload_threshold: int = 20) -> None:
        """配置工具卸载策略"""
        self._max_tools = max_tools
        self._unload_threshold = unload_threshold

    def mark_dynamic(self, name: str, pipeline_id: str = "") -> None:
        """将工具标记为动态加载的工具。"""
        if not pipeline_id:
            pipeline_id = self._current_pid()
        self._dynamic_tool_names.setdefault(pipeline_id, set()).add(name)

    def get_dynamic_tool_names(self, pipeline_id: str = "") -> set[str]:
        """获取指定管道动态加载的工具名称集合。"""
        if not pipeline_id:
            pipeline_id = self._current_pid()
        return self._dynamic_tool_names.get(pipeline_id, set())

    @staticmethod
    def _current_pid() -> str:
        """从 contextvar 获取当前 pipeline_id（隔离动态工具状态用）。"""
        try:
            from pipeline.engine_state import _current_pipeline_id  # noqa: PLC0415

            return _current_pipeline_id.get() or ""
        except Exception:
            return ""

    def register_schema_enricher(self, tool_name: str, enricher: Callable[[Any, dict[str, Any]], Any]) -> None:
        """注册工具 Schema 动态丰富器。"""
        self._schema_enrichers[tool_name] = enricher

    def get_schema_enricher(self, tool_name: str) -> Callable | None:
        """获取工具的 Schema 动态丰富器。"""
        return self._schema_enrichers.get(tool_name)

    def register(
        self,
        tool: Tool,
        key: str | None = None,
        overwrite: bool = False,
    ) -> str:
        """注册工具"""
        name = key or tool.name

        # 检查是否已存在
        if name in self._items and not overwrite:
            raise ToolAlreadyExistsError(name)

        # 使用基类的注册逻辑
        try:
            super().register(tool, key=name, overwrite=overwrite)
        except KeyError as e:
            # 转换 KeyError 为 ToolAlreadyExistsError
            raise ToolAlreadyExistsError(name) from e

        self._known_tool_names.add(name)
        return name

    def register_with_handler(
        self,
        tool: Tool,
        handler: ToolHandler,
        overwrite: bool = False,
    ) -> str:
        """注册工具并绑定处理函数"""
        name = self.register(tool, overwrite=overwrite)
        self._handlers[name] = handler

        # 同时创建 Runnable
        self._runnables[name] = tool.to_runnable(handler)

        return name

    async def register_with_sync(
        self,
        tool: Tool,
        handler: ToolHandler,
        overwrite: bool = False,
    ) -> str:
        """注册工具并同步到数据库"""
        name = self.register_with_handler(tool, handler, overwrite)

        # 同步到数据库
        if self._sync_service:
            db_id = await self._sync_service.sync_tool_to_db(tool)
            tool.db_id = db_id

        return name

    async def unregister_with_sync(self, name: str) -> None:
        """注销工具并从数据库删除"""
        self.unregister(name)

        if self._sync_service:
            await self._sync_service.remove_tool_from_db(name)

    def register_runnable(
        self,
        runnable: "ToolRunnable",
        overwrite: bool = False,
    ) -> str:
        """直接注册 ToolRunnable"""
        from tools.mcp_adapter import runnable_to_mcp_tool  # noqa: PLC0415

        name = runnable.name
        if not name:
            raise ValueError("ToolRunnable must have a name")

        if name in self._items and not overwrite:
            raise ToolAlreadyExistsError(name)

        # 转换为 Tool 并注册
        tool = runnable_to_mcp_tool(runnable)
        self.register(tool, overwrite=overwrite)
        self._runnables[name] = runnable

        return name

    def bind_handler(self, name: str, handler: ToolHandler) -> None:
        """为已注册的工具绑定处理函数"""
        if name not in self._items:
            raise ToolNotFoundError(name)

        self._handlers[name] = handler

        # 更新 Runnable
        tool = self._items[name]
        self._runnables[name] = tool.to_runnable(handler)

    def get(self, name: str) -> Tool:
        """获取工具定义（支持按需自动加载）"""
        # 如果工具未注册，尝试按需加载
        if name not in self._items:
            self._try_load_tool_on_demand(name)

        if name not in self._items:
            raise ToolNotFoundError(name)

        # 更新使用统计
        self._update_usage_stats(name)

        return self._items[name]

    def get_optional(self, name: str) -> Tool | None:
        """可选获取工具（不抛异常，支持按需自动加载）"""
        # 如果工具未注册，尝试按需加载
        if name not in self._items:
            self._try_load_tool_on_demand(name)

        tool = self._items.get(name)

        # 如果工具存在，更新使用统计
        if tool:
            self._update_usage_stats(name)

        return tool

    def _try_load_tool_on_demand(self, name: str) -> None:
        """按需从内置工具类中加载工具"""
        import logging  # noqa: PLC0415

        logger = logging.getLogger(__name__)

        try:
            from tools.builtin import get_all_builtin_tools  # noqa: PLC0415
            from tools.types import Tool  # noqa: PLC0415

            all_tools = get_all_builtin_tools()

            for tool_item in all_tools:
                if hasattr(tool_item, "get_tool_definition"):
                    tool_def = tool_item.get_tool_definition()
                    if tool_def.name == name:
                        registered_name = self.register_with_handler(
                            tool=tool_def,
                            handler=tool_item.execute,
                        )
                        # 注册 Schema 丰富器（如果有）
                        if hasattr(tool_item, "get_schema_enricher"):
                            enricher = tool_item.get_schema_enricher()
                            if enricher:
                                self.register_schema_enricher(tool_def.name, enricher)
                        logger.info(f"[按需加载] 工具 {registered_name} 已自动注册")
                        return
                elif isinstance(tool_item, Tool):
                    if tool_item.name == name:
                        from tools.builtin.lsp_tools import LSPTools  # noqa: PLC0415

                        lsp_instance = LSPTools()
                        handler_map = {
                            "lsp_definition": lsp_instance._lsp_definition,
                            "lsp_references": lsp_instance._lsp_references,
                            "lsp_diagnostics": lsp_instance._lsp_diagnostics,
                            "file_jump": lsp_instance._file_jump,
                        }
                        handler = handler_map.get(tool_item.name)
                        if handler:
                            registered_name = self.register_with_handler(
                                tool=tool_item,
                                handler=handler,
                            )
                            logger.info(f"[按需加载] LSP 工具 {registered_name} 已自动注册")
                            return

            logger.debug(f"[按需加载] 未找到工具: {name}")

        except Exception as e:
            logger.warning(f"[按需加载] 加载工具 {name} 失败: {e}")

    def _update_usage_stats(self, name: str) -> None:
        """更新工具使用统计"""
        import time  # noqa: PLC0415

        # 更新使用次数
        self._usage_count[name] = self._usage_count.get(name, 0) + 1

        # 更新最后使用时间
        self._last_used[name] = time.time()

        # 检查是否需要卸载工具
        self._check_and_unload_if_needed()

    def _check_and_unload_if_needed(self) -> None:
        """检查工具数量，如果超过限制则卸载最少使用的工具"""
        if len(self._items) <= self._max_tools:
            return

        import logging  # noqa: PLC0415

        logger = logging.getLogger(__name__)

        # 获取核心工具列表（不能卸载）
        try:
            from tools.loader import CORE_SYSTEM_TOOLS  # noqa: PLC0415

            core_tools = set(CORE_SYSTEM_TOOLS)
        except ImportError:
            core_tools = set()

        # 找出可以卸载的工具（非核心工具）
        unloadable_tools = [name for name in self._items if name not in core_tools]

        if not unloadable_tools:
            logger.warning(f"[工具卸载] 所有工具都是核心工具，无法卸载 | 工具数量={len(self._items)}")
            return

        # 按最后使用时间排序（最久未使用的排在前面）
        sorted_tools = sorted(unloadable_tools, key=lambda x: self._last_used.get(x, 0))

        # 计算需要卸载的数量
        num_to_unload = min(self._unload_threshold, len(sorted_tools))

        # 卸载工具
        unloaded = []
        for tool_name in sorted_tools[:num_to_unload]:
            try:
                self.unregister(tool_name)
                unloaded.append(tool_name)
            except Exception as e:
                logger.warning(f"[工具卸载] 卸载工具失败 | tool={tool_name} | error={e}")

        if unloaded:
            logger.info(
                f"[工具卸载] 已卸载 {len(unloaded)} 个工具 | 当前工具数={len(self._items)} | 卸载的工具={unloaded}"
            )

    def get_usage_stats(self) -> dict[str, dict[str, Any]]:
        """获取工具使用统计"""
        return {
            name: {
                "usage_count": self._usage_count.get(name, 0),
                "last_used": self._last_used.get(name, 0),
            }
            for name in self._items
        }

    def get_runnable(self, name: str) -> Optional["ToolRunnable"]:
        """获取 ToolRunnable"""
        return self._runnables.get(name)

    def get_handler(self, name: str) -> ToolHandler | None:
        """获取处理函数（支持按需自动加载）"""
        if name not in self._handlers:
            self._try_load_tool_on_demand(name)
        return self._handlers.get(name)

    def has(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._items

    def has_handler(self, name: str) -> bool:
        """检查工具是否有处理函数"""
        return name in self._handlers

    def has_runnable(self, name: str) -> bool:
        """检查工具是否有 Runnable"""
        return name in self._runnables

    def unregister(self, name: str) -> Tool:
        """注销工具"""
        if name not in self._items:
            raise ToolNotFoundError(name)

        # 调用基类的注销逻辑
        tool = super().unregister(name)

        # 清理相关数据
        self._handlers.pop(name, None)
        self._runnables.pop(name, None)
        # 从所有管道的动态工具集合中移除（dict[pipeline_id, set] 结构）
        for _pid_set in self._dynamic_tool_names.values():
            _pid_set.discard(name)
        self._schema_enrichers.pop(name, None)

        return tool

    def list_all(self) -> list[Tool]:
        """列出所有工具"""
        return list(self._items.values())

    def list_runnables(self) -> list["ToolRunnable"]:
        """列出所有 ToolRunnable"""
        return list(self._runnables.values())

    def list_by_category(self, category: ToolCategory) -> list[Tool]:
        """按分类列出工具"""
        return [t for t in self._items.values() if t.category == category]

    def list_by_source(self, source: ToolSource) -> list[Tool]:
        """按来源列出工具"""
        return [t for t in self._items.values() if t.source == source]

    def search(self, query: str) -> list[Tool]:
        """搜索工具"""
        query_lower = query.lower()
        results = []

        for tool in self._items.values():
            if query_lower in tool.name.lower() or query_lower in tool.description.lower():
                results.append(tool)

        return results

    def get_tools_for_llm(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """获取 LLM 可用的工具描述列表"""
        tools = list(self._items.values()) if names is None else [self._items[n] for n in names if n in self._items]

        return [tool.to_llm_format() for tool in tools]

    def get_tools_for_llm_yaml(self, names: list[str] | None = None) -> str:
        """获取 LLM 可用的 YAML 格式工具描述（节省 token）"""
        import yaml  # noqa: PLC0415

        tools = list(self._items.values()) if names is None else [self._items[n] for n in names if n in self._items]

        # 构建工具列表的简化描述（复用 Tool._simplify_schema）
        tools_desc = []
        for tool in tools:
            tool_simple = {
                "name": tool.name,
                "desc": tool.description,
                "params": tool._simplify_schema(tool.input_schema),
            }
            tools_desc.append(tool_simple)

        return yaml.dump({"tools": tools_desc}, default_flow_style=False, allow_unicode=True)

    def get_tools_for_llm_format(
        self, format_type: str | None = None, names: list[str] | None = None
    ) -> list[dict[str, Any]] | str:
        """获取指定格式的 LLM 工具描述（统一接口）"""
        from tools.format_manager import ToolFormat, get_format_manager  # noqa: PLC0415

        # 获取 JSON 格式工具列表
        json_tools = self.get_tools_for_llm(names)

        if format_type is None:
            # 使用全局格式管理器的当前设置
            manager = get_format_manager()
            return manager.get_tools_for_llm(json_tools, names=names)
        # 使用指定格式
        try:
            target_format = ToolFormat(format_type.lower())
            manager = get_format_manager()
            return manager.get_tools_for_llm(json_tools, target_format, names)
        except ValueError:
            # 无效格式，返回 JSON
            return json_tools

    def get_tools_for_mcp(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """获取 MCP 格式的工具列表"""
        tools = list(self._items.values()) if names is None else [self._items[n] for n in names if n in self._items]

        return [tool.to_mcp_format() for tool in tools]

    def count(self) -> int:
        """获取工具数量"""
        return len(self._items)

    def clear(self) -> None:
        """清空注册表"""
        super().clear()
        self._handlers.clear()
        self._runnables.clear()
        self._dynamic_tool_names.clear()
        self._schema_enrichers.clear()
