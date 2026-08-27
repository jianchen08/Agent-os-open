"""工具缓存写入 Output 插件。

在管道 output 阶段（工具执行完成之后）读取 tool_core 的调用快照
（``_executed_tool_calls``）+ 执行结果（``tool_results``），
将成功的结果写入 tool_cache（input 阶段）共享的模块级单例缓存。

接通 tool_cache 的写入断路：tool_cache.put() 原本无调用方，
本插件是它的生产调用方。下一轮 LLM 若再次产出相同工具调用，
tool_cache 命中缓存直接返回，跳过实际执行。

数据源说明：raw_tool_calls 由 llm_core 产出、tool_core 执行后**清空**
（对齐 tool_core Rust 实现 lib.rs:106）；post 链读 raw_tool_calls 恒为空。
tool_core 执行时把执行前的调用列表快照写入 ``_executed_tool_calls``，
本插件以它为写入源（与结果按下标配对）。

依赖关系：
    - 与 tool_cache（input）共享 _GLOBAL_CACHE（模块级单例字典）
    - exclude_tools 中的有副作用工具（bash/file_write 等）不写缓存
    - 失败的工具调用（result 含 error）不写缓存

State 命名空间：
    - 只读 _executed_tool_calls / tool_results，不写 state（纯副作用：写缓存）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class ToolCacheWriter(IOutputPlugin):
    """工具缓存写入 Output 插件。

    读取工具执行结果，把成功的（tool_name, args)→result 写入 tool_cache
    共享的模块级单例缓存。exclude_tools 中的有副作用工具不写。

    配置项：
    - enabled: 是否启用缓存写入（默认 True）
    - default_ttl: 默认缓存过期时间，单位秒（默认 300）
    - max_size: 最大缓存条目数（默认 100）
    - exclude_tools: 不缓存的工具名列表（默认含 bash_execute 等有副作用的工具）

    优先级：25（尽早写缓存）
    缓存写入异常不阻塞管道。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具缓存写入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用缓存写入（默认 True）
                - default_ttl: 默认缓存过期时间，单位秒（默认 300）
                - max_size: 最大缓存条目数（默认 100）
                - exclude_tools: 不缓存的工具名列表
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_cache_writer"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 25)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行缓存写入。

        读取 _executed_tool_calls + tool_results，把成功的工具调用结果写入
        tool_cache 共享的模块级单例缓存。

        Args:
            ctx: 插件执行上下文

        Returns:
            空结果（纯副作用：写缓存，不修改 state）
        """
        if not self._enabled:
            return OutputResult()

        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        # 工具调用快照：tool_core（Rust）执行后清空 raw_tool_calls，
        # 执行前的调用列表在 _executed_tool_calls（lib.rs:107）。
        executed_calls = ctx.state.get("_executed_tool_calls", [])

        if not tool_results or not executed_calls:
            return OutputResult()

        # 延迟导入 tool_cache（input 端），复用其模块级单例缓存 + put 逻辑
        # 注意：input/tool_cache/plugin.py 和本文件都叫 plugin.py，会冲突，
        # 用 importlib 从绝对路径加载 input 端模块，避免 sys.path 名字污染
        import importlib.util
        import os

        _tool_cache_plugin_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "input", "tool_cache", "plugin.py",
        )
        _spec = importlib.util.spec_from_file_location(
            "tool_cache_input_plugin", _tool_cache_plugin_path
        )
        if _spec is None or _spec.loader is None:
            logger.warning("[%s] cannot load tool_cache plugin, skip write", self.name)
            return OutputResult()
        _tool_cache_mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_tool_cache_mod)
        ToolCache = _tool_cache_mod.ToolCache

        # 用本插件配置构造一个 ToolCache 实例，复用其 put 逻辑（含 exclude 判断）
        cache = ToolCache(config=self._config)

        written = 0
        skipped_exclude = 0
        skipped_error = 0
        call_count = len(executed_calls)
        result_count = len(tool_results)

        for i in range(min(call_count, result_count)):
            tool_call = executed_calls[i]
            result = tool_results[i]

            # 失败的工具调用不缓存（result 含 error 字段）
            if isinstance(result, dict) and "error" in result:
                skipped_error += 1
                continue

            tool_name = tool_call.get("name", "")
            if cache._is_excluded(tool_name):
                skipped_exclude += 1
                continue

            cache.put(tool_call, result)
            written += 1

        if written or skipped_exclude or skipped_error:
            logger.debug(
                "[%s] cache write | written=%d excluded=%d error=%d",
                self.name,
                written,
                skipped_exclude,
                skipped_error,
            )

        return OutputResult()
