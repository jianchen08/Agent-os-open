"""
内置工具模块

暴露接口：
- get_all_builtin_tools() -> list[Any]：get_all_builtin_tools功能
- get_all_builtin_tools_with_session() -> list[Any]：get_all_builtin_tools_with_session功能
- register_all_builtin_tools(registry: Any, session: Any | None, evaluator_callback: Callable | None, skip_existing: bool) -> list：register_all_builtin_tools功能
- register_core_tools(registry: Any, session: Any | None, evaluator_callback: Callable | None, skip_existing: bool) -> list：register_core_tools功能
"""

from collections.abc import Callable
from typing import Any

# 只导入基类，延迟导入具体工具类
from .base import BuiltinTool, register_builtin_tool

__all__ = [
    # 基类
    "BuiltinTool",
    "register_builtin_tool",
    # 注册函数
    "register_all_builtin_tools",
    "register_core_tools",
    "get_all_builtin_tools_with_session",
]


def get_all_builtin_tools() -> list[Any]:
    """获取所有内置工具实例（使用自动发现机制）

    通过 DynamicToolLoader 自动扫描 builtin 目录发现工具类。
    需要依赖注入的工具（如 session）自动跳过，由 register_core_tools 处理。
    """
    import importlib  # noqa: PLC0415
    import logging  # noqa: PLC0415

    _logger = logging.getLogger(__name__)

    from tools.loader import get_dynamic_tool_loader, init_dynamic_tool_loader  # noqa: PLC0415
    from tools.registry import ToolRegistry  # noqa: PLC0415

    loader = get_dynamic_tool_loader()
    if loader is None:
        registry = ToolRegistry()
        loader = init_dynamic_tool_loader(registry)

    if not loader._discovered:
        loader._discover_tools()

    tools: list[Any] = []

    for tool_name, (module_path, class_name) in loader._tool_classes.items():
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)

            import inspect  # noqa: PLC0415

            sig = inspect.signature(cls.__init__)
            required_params = [
                p
                for p in sig.parameters.values()
                if p.name != "self"
                and p.default is inspect.Parameter.empty
                and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            ]
            if required_params:
                _logger.debug(f"[内置工具] 跳过 {tool_name}（需要参数注入，由 register_core_tools 处理）")
                continue

            if class_name == "WebTool":
                tools.append(cls.from_config())
            else:
                tools.append(cls())

            _logger.debug(f"[内置工具] 已加载 {tool_name}")
        except Exception as e:
            _logger.debug(f"[内置工具] 跳过 {tool_name}: {e}")

    try:
        from .lsp_tools.tool import LSPTools  # noqa: PLC0415

        tools.extend(LSPTools.get_tools())
    except Exception as e:
        _logger.debug(f"[内置工具] 跳过 LSPTools: {e}")

    return tools


def get_all_builtin_tools_with_session() -> list[Any]:
    """获取需要数据库会话的内置工具类（不实例化）"""
    # 延迟导入需要数据库会话的工具
    from .memory.tool import MemoryTool  # noqa: PLC0415
    from .task.tool import TaskTool  # noqa: PLC0415
    from .task_evaluate.tool import TaskEvaluateTool  # noqa: PLC0415
    from .task_submit.tool import TaskSubmitTool  # noqa: PLC0415

    return [
        MemoryTool,
        TaskSubmitTool,
        TaskTool,
        TaskEvaluateTool,
    ]


def register_all_builtin_tools(  # noqa: PLR0912,PLR0915
    registry: Any,
    session: Any | None = None,
    evaluator_callback: Callable | None = None,
    skip_existing: bool = True,
) -> list:
    """注册所有内置工具到注册表"""
    import logging  # noqa: PLC0415

    logger = logging.getLogger(__name__)
    names = []
    skipped = []
    failed = []

    # 1. 注册不需要会话的工具
    for tool_item in get_all_builtin_tools():
        try:
            if hasattr(tool_item, "get_tool_definition"):
                tool = tool_item.get_tool_definition()
                tool_name = tool.name

                # 检查工具是否已存在
                if skip_existing and registry.has(tool_name):
                    skipped.append(tool_name)
                    logger.debug(f"[内置工具注册] 工具已存在，跳过: {tool_name}")
                    continue

                name = registry.register_with_handler(
                    tool=tool,
                    handler=tool_item.execute,
                )
            else:
                from tools.types import Tool  # noqa: PLC0415

                if isinstance(tool_item, Tool):
                    tool_name = tool_item.name

                    # 检查工具是否已存在
                    if skip_existing and registry.has(tool_name):
                        skipped.append(tool_name)
                        logger.debug(f"[内置工具注册] 工具已存在，跳过: {tool_name}")
                        continue

                    from .lsp_tools.tool import LSPTools  # noqa: PLC0415

                    lsp_instance = LSPTools()
                    handler_map = {
                        "lsp_definition": lsp_instance._lsp_definition,
                        "lsp_references": lsp_instance._lsp_references,
                        "lsp_diagnostics": lsp_instance._lsp_diagnostics,
                        "file_jump": lsp_instance._file_jump,
                    }
                    handler = handler_map.get(tool_item.name)
                    if handler:
                        name = registry.register_with_handler(
                            tool=tool_item,
                            handler=handler,
                        )
                    else:
                        logger.warning(f"LSP 工具未找到处理器: {tool_item.name}")
                        continue
                else:
                    logger.warning(f"未知工具类型: {type(tool_item)}")
                    continue
            names.append(name)
        except Exception as e:
            tool_name = getattr(tool_item, "__class__", type(tool_item)).__name__
            if hasattr(tool_item, "name"):
                tool_name = tool_item.name
            failed.append((tool_name, str(e)))
            logger.warning(f"内置工具注册失败: {tool_name}, 错误: {e}")

    # 2. 如果提供了会话，注册需要会话的工具
    if session is not None:
        from .memory.tool import MemoryTool  # noqa: PLC0415
        from .task.tool import TaskTool  # noqa: PLC0415
        from .task_evaluate.tool import TaskEvaluateTool  # noqa: PLC0415
        from .task_submit.tool import TaskSubmitTool  # noqa: PLC0415

        # 2.1 注册 TaskSubmitTool
        if skip_existing and registry.has("task_submit") and registry.get_handler("task_submit") is not None:
            skipped.append("task_submit")
            logger.debug("[内置工具注册] 工具已存在且含handler，跳过: task_submit")
        else:
            try:
                submit_tool_instance = TaskSubmitTool()
                submit_tool_def = submit_tool_instance.get_tool_definition()
                submit_tool_id = registry.register_with_handler(
                    tool=submit_tool_def, handler=submit_tool_instance.execute, overwrite=True
                )
                names.append(submit_tool_id)
                logger.debug(f"[内置工具注册] task_submit 已注册，ID: {submit_tool_id}")
            except Exception as e:
                tool_name = "TaskSubmitTool"
                failed.append((tool_name, str(e)))
                logger.warning(f"内置工具注册失败: {tool_name}, 错误: {e}")

        # 2.2 注册 TaskTool (只需要 session)
        if skip_existing and registry.has("task_manage") and registry.get_handler("task_manage") is not None:
            skipped.append("task_manage")
            logger.debug("[内置工具注册] 工具已存在且含handler，跳过: task_manage")
        else:
            try:
                task_tool_instance = TaskTool(session=session)
                task_tool_def = task_tool_instance.get_tool_definition()
                task_tool_id = registry.register_with_handler(
                    tool=task_tool_def, handler=task_tool_instance.execute, overwrite=True
                )
                names.append(task_tool_id)
                logger.debug(f"[内置工具注册] task_manage 已注册，ID: {task_tool_id}")
            except Exception as e:
                tool_name = "TaskTool"
                failed.append((tool_name, str(e)))
                logger.warning(f"内置工具注册失败: {tool_name}, 错误: {e}")

        # 2.3 注册 TaskEvaluateTool (需要 session)
        if skip_existing and registry.has("task_evaluate") and registry.get_handler("task_evaluate") is not None:
            skipped.append("task_evaluate")
            logger.debug("[内置工具注册] 工具已存在且含handler，跳过: task_evaluate")
        else:
            try:
                eval_tool_instance = TaskEvaluateTool(session=session)
                eval_tool_def = eval_tool_instance.get_tool_definition()
                eval_tool_id = registry.register_with_handler(
                    tool=eval_tool_def, handler=eval_tool_instance.execute, overwrite=True
                )
                names.append(eval_tool_id)
                logger.debug(f"[内置工具注册] task_evaluate 已注册，ID: {eval_tool_id}")
            except Exception as e:
                tool_name = "TaskEvaluateTool"
                failed.append((tool_name, str(e)))
                logger.warning(f"内置工具注册失败: {tool_name}, 错误: {e}")

        # 2.4 注册 MemoryTool (只需要 session)
        # 注意：MemoryTool.get_tool_definition() 返回的 name 是 "memory"
        if skip_existing and registry.has("memory") and registry.get_handler("memory") is not None:
            skipped.append("memory")
            logger.debug("[内置工具注册] 工具已存在且含handler，跳过: memory")
        else:
            try:
                memory_tool_instance = MemoryTool(session=session)
                memory_tool_def = memory_tool_instance.get_tool_definition()
                memory_tool_id = registry.register_with_handler(
                    tool=memory_tool_def, handler=memory_tool_instance.execute, overwrite=True
                )
                names.append(memory_tool_id)
                logger.debug(f"[内置工具注册] memory 已注册，ID: {memory_tool_id}")
            except Exception as e:
                tool_name = "MemoryTool"
                failed.append((tool_name, str(e)))
                logger.warning(f"内置工具注册失败: {tool_name}, 错误: {e}")

    if skipped:
        logger.debug(f"[内置工具注册] 共跳过 {len(skipped)} 个已存在的工具: {skipped}")
    if failed:
        logger.warning(f"共有 {len(failed)} 个工具注册失败")

    return names


def register_core_tools(  # noqa: PLR0912,PLR0915
    registry: Any,
    session: Any | None = None,
    evaluator_callback: Callable | None = None,
    skip_existing: bool = True,
) -> list:
    """只注册核心系统工具到注册表（用于应用启动时的预热）"""
    import logging  # noqa: PLC0415

    from tools.loader import CORE_SYSTEM_TOOLS  # noqa: PLC0415

    logger = logging.getLogger(__name__)
    names = []
    skipped = []
    failed = []

    # 延迟导入核心工具类
    from .bash import BashTool  # noqa: PLC0415
    from .enhanced_search.tool import EnhancedSearchTool  # noqa: PLC0415
    from .file_read.tool import FileReadTool  # noqa: PLC0415
    from .file_write.tool import FileWriteTool  # noqa: PLC0415
    from .human_interaction.tool import HumanInteractionTool  # noqa: PLC0415
    from .lsp_tools.tool import LSPTools  # noqa: PLC0415
    from .resource_merge.tool import ResourceMergeTool  # noqa: PLC0415
    from .resource_search.tool import ResourceSearchTool  # noqa: PLC0415
    from .web.tool import WebTool  # noqa: PLC0415
    from .web_search_mcp.tool import WebSearchMCPTool  # noqa: PLC0415

    core_tool_map = {
        "bash_execute": BashTool,
        "file_read": FileReadTool,
        "file_write": FileWriteTool,
        "enhanced_search": EnhancedSearchTool,
        "web_search": WebSearchMCPTool,
        "fetch": WebTool,
        "resource_search": ResourceSearchTool,
        "resource_merge": ResourceMergeTool,
        "human_interaction": HumanInteractionTool,
    }

    # LSP 工具实例（共享）
    lsp_instance = LSPTools()
    lsp_tools = LSPTools.get_tool_definitions()  # 获取正确的工具定义
    lsp_handler_map = {
        "lsp_definition": lsp_instance._lsp_definition,
        "lsp_references": lsp_instance._lsp_references,
        "lsp_diagnostics": lsp_instance._lsp_diagnostics,
        "file_jump": lsp_instance._file_jump,
    }

    # 注册所有核心工具
    for tool_name in CORE_SYSTEM_TOOLS:
        # 处理 LSP 工具
        if tool_name in lsp_handler_map:
            if skip_existing and registry.has(tool_name):
                skipped.append(tool_name)
                logger.debug(f"[核心工具注册] 工具已存在，跳过: {tool_name}")
                continue
            try:
                lsp_tool = lsp_tools[tool_name]
                name = registry.register_with_handler(
                    tool=lsp_tool,
                    handler=lsp_handler_map[tool_name],
                )
                names.append(name)
                logger.debug(f"[核心工具注册] {tool_name} 已注册，ID: {name}")
            except Exception as e:
                failed.append((tool_name, str(e)))
                logger.warning(f"核心工具注册失败: {tool_name}, 错误: {e}")
            continue

        # 处理需要 session 的工具
        if tool_name in ["task_submit", "task_manage", "task_evaluate", "memory"]:
            tool_import_map = {
                "task_submit": ("tools.builtin.task_submit", "TaskSubmitTool"),
                "task_manage": ("tools.builtin.task", "TaskTool"),
                "task_evaluate": ("tools.builtin.task_evaluate", "TaskEvaluateTool"),
                "memory": ("tools.builtin.memory", "MemoryTool"),
            }
            module_path, class_name = tool_import_map[tool_name]
            tool_class = None
            try:
                import importlib  # noqa: PLC0415

                mod = importlib.import_module(module_path)
                tool_class = getattr(mod, class_name)
            except ImportError as _import_err:
                logger.warning("Session-dependent tool %s skipped (import failed: %s)", tool_name, _import_err)
                continue
            except Exception as _other_err:
                logger.warning("Session-dependent tool %s skipped (error: %s)", tool_name, _other_err)
                continue

            tool_class_map = {
                "task_submit": tool_class if tool_name == "task_submit" else None,
                "task_manage": tool_class if tool_name == "task_manage" else None,
                "task_evaluate": tool_class if tool_name == "task_evaluate" else None,
                "memory": tool_class if tool_name == "memory" else None,
            }

            tool_class = tool_class_map.get(tool_name)
            if not tool_class:
                continue

            # 双重检查：定义存在且 handler 也存在才跳过。
            # 若只查 has()，处于"有定义无 handler"状态（动态加载器会注册无 handler 的定义）
            # 的工具会被错误跳过，导致执行时 "Tool not found"。
            if skip_existing and registry.has(tool_name) and registry.get_handler(tool_name) is not None:
                skipped.append(tool_name)
                logger.debug(f"[核心工具注册] 工具已存在且含handler，跳过: {tool_name}")
                continue

            try:
                tool_instance = tool_class()
                tool_def = tool_instance.get_tool_definition()
                # overwrite=True：工具可能已存在"无 handler 的定义"（动态加载器注册），
                # 需覆盖以补上 handler，否则 register_with_handler 会因定义已存在而抛错。
                name = registry.register_with_handler(
                    tool=tool_def,
                    handler=tool_instance.execute,
                    overwrite=True,
                )
                names.append(name)
                logger.info(f"[核心工具注册] {tool_name} 已注册（含handler），ID: {name}")
            except Exception as e:
                failed.append((tool_name, str(e)))
                logger.error(f"核心工具注册失败: {tool_name}, 错误: {e}")
            continue

        # 处理普通工具
        tool_class = core_tool_map.get(tool_name)
        if not tool_class:
            logger.warning(f"核心工具未找到: {tool_name}")
            continue

        # 检查工具是否已存在
        if skip_existing and registry.has(tool_name):
            skipped.append(tool_name)
            logger.debug(f"[核心工具注册] 工具已存在，跳过: {tool_name}")
            continue

        try:
            # resource_search 需要 tool_registry 参数以支持动态工具加载
            tool_instance = tool_class(tool_registry=registry) if tool_name == "resource_search" else tool_class()
            tool = tool_instance.get_tool_definition()
            name = registry.register_with_handler(
                tool=tool,
                handler=tool_instance.execute,
            )
            names.append(name)
            logger.debug(f"[核心工具注册] {tool_name} 已注册，ID: {name}")
        except Exception as e:
            failed.append((tool_name, str(e)))
            logger.warning(f"核心工具注册失败: {tool_name}, 错误: {e}")

    # 所有核心工具已注册完成
    # 运行时依赖（如 session）将在执行时注入

    if skipped:
        logger.debug(f"[核心工具注册] 共跳过 {len(skipped)} 个已存在的工具: {skipped}")
    if failed:
        logger.warning(f"共有 {len(failed)} 个核心工具注册失败")

    logger.info(f"[核心工具注册] 完成 | 成功: {len(names)} | 跳过: {len(skipped)} | 失败: {len(failed)}")

    return names
