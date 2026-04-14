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
    """获取所有内置工具实例（不需要依赖注入的工具）
    
    导入失败的模块自动跳过并记录警告。
    """
    import logging
    _logger = logging.getLogger(__name__)
    
    tools: list[Any] = []
    
    # 可用工具列表（延迟导入，导入失败的自动跳过）
    _tool_modules = [
        (".file_read", "FileReadTool"),
        (".file_write", "FileWriteTool"),
        (".bash", "BashTool"),
        (".enhanced_search", "EnhancedSearchTool"),
        (".web", "WebTool"),
        (".web_search_mcp", "WebSearchMCPTool"),
        (".evaluate", "EvaluateTool"),
        (".resource_search", "ResourceSearchTool"),
        (".yaml_validate", "YamlValidateTool"),
        (".evaluators", "SchemaEvaluator"),
        (".evaluators", "ResourceEvaluator"),
        (".compatibility_checker", "CompatibilityCheckerTool"),
        (".rollback", "RollbackTool"),
        (".state_update", "StateUpdateTool"),
        (".todo_manage", "TodoManageTool"),
        (".trigger_setup", "TriggerSetupTool"),
    ]
    
    for module_path, class_name in _tool_modules:
        try:
            mod = __import__(module_path, fromlist=[class_name], level=1)
            cls = getattr(mod, class_name)
            if class_name == "WebTool":
                tools.append(cls.from_config())
            else:
                tools.append(cls())
        except Exception as e:
            _logger.debug(f"[内置工具] 跳过 {class_name}: {e}")
    
    # LSP 工具（特殊处理）
    try:
        from .lsp_tools import LSPTools
        tools.extend(LSPTools.get_tools())
    except Exception as e:
        _logger.debug(f"[内置工具] 跳过 LSPTools: {e}")

    return tools


def get_all_builtin_tools_with_session() -> list[Any]:
    """获取需要数据库会话的内置工具类（不实例化）"""
    # 延迟导入需要数据库会话的工具
    from .memory import MemoryTool
    from .task import TaskTool
    from .task_evaluate import TaskEvaluateTool
    from .task_submit import TaskSubmitTool

    return [
        MemoryTool,
        TaskSubmitTool,
        TaskTool,
        TaskEvaluateTool,
    ]


def register_all_builtin_tools(
    registry: Any,
    session: Any | None = None,
    evaluator_callback: Callable | None = None,
    skip_existing: bool = True,
) -> list:
    """注册所有内置工具到注册表"""
    import logging

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
                from tools.types import Tool

                if isinstance(tool_item, Tool):
                    tool_name = tool_item.name

                    # 检查工具是否已存在
                    if skip_existing and registry.has(tool_name):
                        skipped.append(tool_name)
                        logger.debug(f"[内置工具注册] 工具已存在，跳过: {tool_name}")
                        continue

                    from .lsp_tools import LSPTools

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
        from .memory import MemoryTool
        from .task import TaskTool
        from .task_evaluate import TaskEvaluateTool
        from .task_submit import TaskSubmitTool

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


def register_core_tools(
    registry: Any,
    session: Any | None = None,
    evaluator_callback: Callable | None = None,
    skip_existing: bool = True,
) -> list:
    """只注册核心系统工具到注册表（用于应用启动时的预热）"""
    import logging

    from tools.loader import CORE_SYSTEM_TOOLS

    logger = logging.getLogger(__name__)
    names = []
    skipped = []
    failed = []

    # 延迟导入核心工具类
    from .bash import BashTool
    from .enhanced_search import EnhancedSearchTool
    from .file_read import FileReadTool
    from .file_write import FileWriteTool
    from .lsp_tools import LSPTools
    from .resource_search import ResourceSearchTool
    from .web import WebTool
    from .web_search_mcp import WebSearchMCPTool

    # 工具映射表（不需要依赖注入的工具）
    core_tool_map = {
        "bash_execute": BashTool,
        "file_read": FileReadTool,
        "file_write": FileWriteTool,
        "enhanced_search": EnhancedSearchTool,
        "web_search": WebSearchMCPTool,
        "fetch": WebTool,
        "resource_search": ResourceSearchTool,
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
        # BUG-FIX-fix_20260316_task_manage_not_found
        # 问题根因: 原代码只注册工具定义，没有注册 handler，导致执行时找不到处理函数
        # 修复方案: 当 session 存在时，实例化工具并注册 handler
        # 注意：工具名称必须与 Tool.get_tool_definition() 中的 name 字段一致
        # - task_submit -> TaskSubmitTool (name="task_submit")
        # - task_manage -> TaskTool (name="task_manage")
        # - task_evaluate -> TaskEvaluateTool (name="task_evaluate")
        # - memory -> MemoryTool (name="memory")
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
                import importlib
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

            if skip_existing and registry.has(tool_name):
                skipped.append(tool_name)
                logger.debug(f"[核心工具注册] 工具已存在，跳过: {tool_name}")
                continue

            try:
                tool_instance = tool_class()
                tool_def = tool_instance.get_tool_definition()
                name = registry.register_with_handler(
                    tool=tool_def,
                    handler=tool_instance.execute,
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
            tool_instance = tool_class()
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

    logger.info(
        f"[核心工具注册] 完成 | 成功: {len(names)} | 跳过: {len(skipped)} | 失败: {len(failed)}"
    )

    return names
