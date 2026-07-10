"""
工具模块接口定义

暴露接口：
- session_id(self) -> str：session_id功能
- task_id(self) -> str：task_id功能
- user_id(self) -> str | None：user_id功能
- metadata(self) -> dict[str, Any]：metadata功能
- register_handler(self, tool_name: str, handler: ToolHandler) -> None：register_handler功能
- unregister_handler(self, tool_name: str) -> None：unregister_handler功能
- has_handler(self, tool_name: str) -> bool：has_handler功能
- set_progress_callback(self, callback: ProgressCallback | None) -> None：set_progress_callback功能
- get_cache_stats(self) -> dict[str, Any]：get_cache_stats功能
- set_runnable_first(self, enabled: bool) -> None：set_runnable_first功能
- register(self, tool: Tool, overwrite: bool) -> str：register功能
- register_with_handler(self, tool: Tool, handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]], overwrite: bool) -> str：register_with_handler功能
- register_runnable(self, runnable: Any, overwrite: bool) -> str：register_runnable功能
- bind_handler(self, name: str, handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]) -> None：bind_handler功能
- get(self, name: str) -> Tool：get功能
- get_optional(self, name: str) -> Tool | None：get_optional功能
- get_runnable(self, name: str) -> Any | None：get_runnable功能
- get_handler(self, name: str) -> Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]] | None：get_handler功能
- has(self, name: str) -> bool：has功能
- has_handler(self, name: str) -> bool：has_handler功能
- has_runnable(self, name: str) -> bool：has_runnable功能
- unregister(self, name: str) -> None：unregister功能
- list_all(self) -> list[Tool]：list_all功能
- list_runnables(self) -> list[Any]：list_runnables功能
- list_by_category(self, category: Any) -> list[Tool]：list_by_category功能
- list_by_source(self, source: Any) -> list[Tool]：list_by_source功能
- search(self, query: str) -> list[Tool]：search功能
- get_tools_for_llm(self, names: list[str] | None) -> list[dict[str, Any]]：get_tools_for_llm功能
- get_tools_for_llm_yaml(self, names: list[str] | None) -> str：get_tools_for_llm_yaml功能
- get_tools_for_llm_format(self, format_type: str | None, names: list[str] | None) -> list[dict[str, Any]] | str：get_tools_for_llm_format功能
- get_tools_for_mcp(self, names: list[str] | None) -> list[dict[str, Any]]：get_tools_for_mcp功能
- count(self) -> int：count功能
- clear(self) -> None：clear功能
- set_sync_service(self, sync_service: Any) -> None：set_sync_service功能
- configure_unload_policy(self, max_tools: int, unload_threshold: int) -> None：configure_unload_policy功能
- get_usage_stats(self) -> dict[str, dict[str, Any]]：get_usage_stats功能
- IExecutionContext：IExecutionContext类
- IToolExecutor：IToolExecutor类
- IToolRegistry：IToolRegistry类
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any

from core.results import ToolExecutionResult
from tools.types import Tool

# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, ToolExecutionResult]]

# 进度回调函数类型
ProgressCallback = Callable[[str, float, str | None], Coroutine[Any, Any, None]]


class IExecutionContext(ABC):
    """执行上下文接口"""

    @property
    @abstractmethod
    def session_id(self) -> str:
        """会话 ID"""

    @property
    @abstractmethod
    def task_id(self) -> str:
        """任务 ID"""

    @property
    @abstractmethod
    def user_id(self) -> str | None:
        """用户 ID"""

    @property
    @abstractmethod
    def metadata(self) -> dict[str, Any]:
        """元数据"""


class IToolExecutor(ABC):
    """
    工具执行器接口

    定义工具执行器的标准接口，支持：
    - 工具执行
    - 批量执行
    - 管道执行
    - 进度回调
    - 缓存管理
    """

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: Any,
        approved: bool = False,
        timeout: float | None = None,
        use_runnable: bool | None = None,
        tool_call_id: str | None = None,
        use_cache: bool = True,
    ) -> ToolExecutionResult:
        """执行工具"""

    @abstractmethod
    async def execute_runnable(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: Any,
        approved: bool = False,
        timeout: float | None = None,
    ) -> ToolExecutionResult:
        """强制使用 Runnable 模式执行"""

    @abstractmethod
    async def batch_execute(
        self,
        calls: list[dict[str, Any]],
        context: Any,
    ) -> list[ToolExecutionResult]:
        """批量执行工具"""

    @abstractmethod
    async def execute_pipeline(
        self,
        tool_names: list[str],
        initial_input: dict[str, Any],
        context: Any,
    ) -> ToolExecutionResult:
        """执行工具管道（顺序执行，前一个输出作为后一个输入）"""

    @abstractmethod
    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        """注册工具处理函数"""

    @abstractmethod
    def unregister_handler(self, tool_name: str) -> None:
        """注销工具处理函数"""

    @abstractmethod
    def has_handler(self, tool_name: str) -> bool:
        """检查是否有处理函数"""

    @abstractmethod
    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """设置进度回调函数"""

    @abstractmethod
    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计"""

    @abstractmethod
    async def clear_tool_cache(self, tool_name: str | None = None) -> int:
        """清除工具缓存"""

    @abstractmethod
    def set_runnable_first(self, enabled: bool) -> None:
        """设置是否优先使用 Runnable 模式"""


class IToolRegistry(ABC):
    """
    工具注册表接口

    定义工具注册表的标准接口，支持：
    - 工具注册和注销
    - 工具查询和检索
    - Runnable 管理
    - 处理函数绑定
    """

    @abstractmethod
    def register(self, tool: Tool, overwrite: bool = False) -> str:
        """注册工具"""

    @abstractmethod
    def register_with_handler(
        self,
        tool: Tool,
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
        overwrite: bool = False,
    ) -> str:
        """注册工具并绑定处理函数"""

    @abstractmethod
    async def register_with_sync(
        self,
        tool: Tool,
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
        overwrite: bool = False,
    ) -> str:
        """注册工具并同步到数据库"""

    @abstractmethod
    async def unregister_with_sync(self, name: str) -> None:
        """注销工具并从数据库删除"""

    @abstractmethod
    def register_runnable(
        self,
        runnable: Any,
        overwrite: bool = False,
    ) -> str:
        """直接注册 ToolRunnable"""

    @abstractmethod
    def bind_handler(self, name: str, handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]) -> None:
        """为已注册的工具绑定处理函数"""

    @abstractmethod
    def get(self, name: str) -> Tool:
        """获取工具定义"""

    @abstractmethod
    def get_optional(self, name: str) -> Tool | None:
        """可选获取工具（不抛异常）"""

    @abstractmethod
    def get_runnable(self, name: str) -> Any | None:
        """获取 ToolRunnable"""

    @abstractmethod
    def get_handler(self, name: str) -> Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]] | None:
        """获取处理函数"""

    @abstractmethod
    def has(self, name: str) -> bool:
        """检查工具是否存在"""

    @abstractmethod
    def has_handler(self, name: str) -> bool:
        """检查工具是否有处理函数"""

    @abstractmethod
    def has_runnable(self, name: str) -> bool:
        """检查工具是否有 Runnable"""

    @abstractmethod
    def unregister(self, name: str) -> None:
        """注销工具"""

    @abstractmethod
    def list_all(self) -> list[Tool]:
        """列出所有工具"""

    @abstractmethod
    def list_runnables(self) -> list[Any]:
        """列出所有 ToolRunnable"""

    @abstractmethod
    def list_by_category(self, category: Any) -> list[Tool]:
        """按分类列出工具"""

    @abstractmethod
    def list_by_source(self, source: Any) -> list[Tool]:
        """按来源列出工具"""

    @abstractmethod
    def search(self, query: str) -> list[Tool]:
        """搜索工具"""

    @abstractmethod
    def get_tools_for_llm(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """获取 LLM 可用的工具描述列表"""

    @abstractmethod
    def get_tools_for_llm_yaml(self, names: list[str] | None = None) -> str:
        """获取 LLM 可用的 YAML 格式工具描述"""

    @abstractmethod
    def get_tools_for_llm_format(
        self, format_type: str | None = None, names: list[str] | None = None
    ) -> list[dict[str, Any]] | str:
        """获取指定格式的 LLM 工具描述"""

    @abstractmethod
    def get_tools_for_mcp(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """获取 MCP 格式的工具列表"""

    @abstractmethod
    def count(self) -> int:
        """获取工具数量"""

    @abstractmethod
    def clear(self) -> None:
        """清空注册表"""

    @abstractmethod
    def set_sync_service(self, sync_service: Any) -> None:
        """设置同步服务"""

    @abstractmethod
    def configure_unload_policy(self, max_tools: int = 100, unload_threshold: int = 20) -> None:
        """配置工具卸载策略"""

    @abstractmethod
    def get_usage_stats(self) -> dict[str, dict[str, Any]]:
        """获取工具使用统计"""
