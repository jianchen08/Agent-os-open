"""
动态工具加载器

暴露接口：
- get_dynamic_tool_loader() -> DynamicToolLoader | None：get_dynamic_tool_loader功能
- set_dynamic_tool_loader(loader: DynamicToolLoader) -> None：set_dynamic_tool_loader功能
- init_dynamic_tool_loader(registry: ToolRegistry) -> DynamicToolLoader：init_dynamic_tool_loader功能
- is_core_tool(self, tool_name: str) -> bool：is_core_tool功能
- is_loaded(self, tool_name: str) -> bool：is_loaded功能
- is_available(self, tool_name: str) -> bool：is_available功能
- get_loaded_tools(self) -> set：get_loaded_tools功能
- get_available_tools(self) -> list[str]：get_available_tools功能
- get_discovered_tools(self) -> dict：get_discovered_tools功能
- DynamicToolLoader：DynamicToolLoader类
"""

import importlib
import inspect
import logging
import os
from pathlib import Path
from typing import Any

from core.exceptions import ToolNotFoundError
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# 核心系统工具（预注册列表）
# 这些工具在应用启动时注册，其他工具按需动态加载
CORE_SYSTEM_TOOLS = [
    # 基础文件操作（必需，高频）
    "file_read",
    "file_write",
    "bash_execute",
    # 搜索工具（高频）
    "enhanced_search",
    "web_search",
    "fetch",
    # LSP 工具（代码分析高频）
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
    "file_jump",
    # 任务管理（核心）
    "task_submit",
    "task_manage",
    "task_evaluate",
    # 记忆工具（核心）
    "memory",
    # 资源搜索（核心）
    "resource_search",
    # 人类交互（核心）
    "human_interaction",
]


class DynamicToolLoader:
    """
    动态工具加载器

    支持自动发现和按需加载工具。

    自动发现机制：
    1. 扫描 src/tools/builtin/ 目录
    2. 查找所有 BuiltinTool 子类
    3. 通过 get_tool_definition() 获取工具名
    4. 建立工具名 -> 模块路径 的映射
    """

    def __init__(self, registry: ToolRegistry):
        """初始化动态工具加载器"""
        self._registry = registry
        self._loading: dict[str, Any] = {}  # 正在加载的工具
        self._loaded: set = set()  # 已加载的工具
        self._tool_modules: dict[str, str] = {}  # 工具名 -> 模块路径
        self._tool_classes: dict[str, tuple[str, str]] = {}  # 工具名 -> (模块路径, 类名)
        self._discovered = False  # 是否已完成发现

    def _discover_tools(self) -> None:
        """
        自动发现所有内置工具

        扫描 src/tools/builtin/ 目录，查找所有 BuiltinTool 子类。
        """
        if self._discovered:
            return

        builtin_dir = Path(__file__).parent / "builtin"
        if not builtin_dir.exists():
            logger.warning(f"[动态加载] builtin 目录不存在: {builtin_dir}")
            return

        logger.info("[动态加载] 开始自动发现工具...")

        src_root = Path(__file__).parent.parent

        # 扫描所有 Python 文件
        for py_file in builtin_dir.rglob("*.py"):
            if py_file.name.startswith("_"):
                continue

            # 构建模块路径（相对于 src 根目录）
            relative_path = py_file.relative_to(src_root)
            module_path = str(relative_path.with_suffix("")).replace(os.sep, ".")

            # 尝试导入模块并发现工具类
            self._discover_tools_in_module(module_path)

        self._discovered = True
        logger.info(f"[动态加载] 工具发现完成 | 发现 {len(self._tool_modules)} 个工具")

    def _discover_tools_in_module(self, module_path: str) -> None:
        """在指定模块中发现工具类"""
        try:
            module = importlib.import_module(module_path)

            # 查找所有 BuiltinTool 子类
            for attr_name in dir(module):
                attr = getattr(module, attr_name)

                # 检查是否是类且是 BuiltinTool 的子类
                if not isinstance(attr, type):
                    continue

                # 检查是否是 BuiltinTool 子类（但不是 BuiltinTool 本身或抽象基类）
                try:
                    from tools.builtin.base import BuiltinTool  # noqa: PLC0415

                    if not issubclass(attr, BuiltinTool):
                        continue
                    if attr is BuiltinTool:
                        continue
                    if inspect.isabstract(attr):
                        continue
                except (ImportError, TypeError):
                    continue

                # 尝试获取工具定义
                try:
                    # 尝试无参数实例化
                    try:
                        instance = attr()
                    except TypeError as e:
                        error_msg = str(e)
                        # 检查是否是"缺少必需参数"的错误
                        if "missing" in error_msg and "required positional argument" in error_msg:
                            # 如果需要参数，尝试使用静态方法 get_tool_definition 获取定义
                            # 工具定义在注册阶段只需要，不需要运行时依赖
                            tool_def = attr.get_tool_definition()
                            tool_name = tool_def.name
                            self._tool_modules[tool_name] = module_path
                            self._tool_classes[tool_name] = (module_path, attr_name)
                            logger.debug(
                                f"[动态加载] 发现工具（通过静态方法）| "
                                f"name={tool_name} | class={attr_name} | module={module_path}"
                            )
                            continue
                        # 其他 TypeError 重新抛出
                        raise

                    tool_def = instance.get_tool_definition()
                    tool_name = tool_def.name

                    self._tool_modules[tool_name] = module_path
                    self._tool_classes[tool_name] = (module_path, attr_name)

                    logger.debug(f"[动态加载] 发现工具 | name={tool_name} | class={attr_name} | module={module_path}")

                except Exception as e:
                    logger.warning(
                        f"[动态加载] 获取工具定义失败 | class={attr_name} | module={module_path} | error={e}"
                    )

        except ImportError as e:
            logger.debug(f"[动态加载] 导入模块失败 | module={module_path} | error={e}")
        except Exception as e:
            logger.warning(f"[动态加载] 发现工具失败 | module={module_path} | error={e}")

    def is_core_tool(self, tool_name: str) -> bool:
        """检查是否是核心系统工具"""
        return tool_name in CORE_SYSTEM_TOOLS

    def is_loaded(self, tool_name: str) -> bool:
        """检查工具是否已加载"""
        return tool_name in self._loaded

    def is_available(self, tool_name: str) -> bool:
        """检查工具是否可用（已发现）"""
        # 确保已完成发现
        if not self._discovered:
            self._discover_tools()

        return tool_name in self._tool_modules

    async def load_tool(self, tool_name: str) -> str:  # noqa: PLR0912
        """动态加载工具"""
        # 确保已完成发现
        if not self._discovered:
            self._discover_tools()

        # 检查是否已加载
        if self.is_loaded(tool_name):
            logger.debug(f"[动态加载] 工具已加载 | tool_name={tool_name}")
            return tool_name

        # 检查是否正在加载
        if tool_name in self._loading:
            logger.debug(f"[动态加载] 工具正在加载中 | tool_name={tool_name}")
            return tool_name

        # 检查工具是否可用
        if tool_name not in self._tool_modules:
            raise ToolNotFoundError(tool_name)

        logger.debug(f"[动态加载] 开始加载工具 | tool_name={tool_name}")

        # 标记正在加载
        self._loading[tool_name] = True

        try:
            # 获取模块路径和类名
            module_path, class_name = self._tool_classes[tool_name]

            # 动态导入模块
            module = importlib.import_module(module_path)

            # 获取工具类
            tool_class = getattr(module, class_name)

            # 实例化工具
            try:
                tool_instance = tool_class()
            except TypeError as e:
                if "missing" in str(e) and "required positional argument" in str(e):
                    # 工具需要依赖注入，记录警告但继续尝试加载
                    # 运行时依赖在执行时通过其他方式注入（如通过 session factory）
                    logger.warning(f"[动态加载] 工具需要依赖注入，将延迟实例化 | tool_name={tool_name} | hint={e}")
                    tool_definition = tool_class.get_tool_definition()
                    tool_instance = None
                else:
                    raise
            else:
                tool_definition = tool_instance.get_tool_definition()
                tool_instance = tool_instance  # noqa: PLW0127

            # 注册工具
            if tool_instance is not None:
                registered_name = self._registry.register_with_handler(
                    tool=tool_definition,
                    handler=tool_instance.execute,
                )
            else:
                # 工具需要依赖注入，注册定义但不注册 handler
                # 通过工具名直接注册
                registered_name = self._registry.register(tool_definition)
                logger.info(f"[动态加载] 工具定义已注册（无handler），执行时需要注入依赖 | tool_name={tool_name}")

            # 标记已加载
            self._loaded.add(tool_name)

            logger.debug(f"[动态加载] 工具加载成功 | tool_name={tool_name} | registered_name={registered_name}")

            return registered_name

        except ImportError as e:
            logger.error(
                f"[动态加载] 导入模块失败 | "
                f"tool_name={tool_name} | module={self._tool_modules.get(tool_name)} | "
                f"error={e}"
            )
            raise ToolNotFoundError(tool_name) from e

        except AttributeError as e:
            logger.error(
                f"[动态加载] 找不到工具类 | "
                f"tool_name={tool_name} | class={self._tool_classes.get(tool_name)} | "
                f"error={e}"
            )
            raise ToolNotFoundError(tool_name) from e

        except ToolNotFoundError:
            raise

        except Exception as e:
            logger.error(
                f"[动态加载] 加载工具失败 | tool_name={tool_name} | error={e}",
                exc_info=True,
            )
            raise ToolNotFoundError(tool_name) from e

        finally:
            # 清除加载状态
            if tool_name in self._loading:
                del self._loading[tool_name]

    async def ensure_loaded(self, tool_names: list[str]) -> None:
        """确保指定的工具都已加载"""
        for tool_name in tool_names:
            # 检查是否已加载或已在注册表中
            if self.is_loaded(tool_name) or self._registry.has(tool_name):
                continue

            if self.is_available(tool_name):
                try:
                    await self.load_tool(tool_name)
                except ToolNotFoundError:
                    logger.warning(f"[动态加载] 无法加载工具 | tool_name={tool_name}")

    def load_tool_sync(self, tool_name: str) -> str:  # noqa: PLR0912
        """同步动态加载工具（从 load_tool 提取的纯同步路径）"""
        if not self._discovered:
            self._discover_tools()

        if self.is_loaded(tool_name):
            logger.debug(f"[动态加载-同步] 工具已加载 | tool_name={tool_name}")
            return tool_name

        if tool_name in self._loading:
            logger.debug(f"[动态加载-同步] 工具正在加载中 | tool_name={tool_name}")
            return tool_name

        if tool_name not in self._tool_modules:
            raise ToolNotFoundError(tool_name)

        logger.debug(f"[动态加载-同步] 开始加载工具 | tool_name={tool_name}")

        self._loading[tool_name] = True

        try:
            module_path, class_name = self._tool_classes[tool_name]
            module = importlib.import_module(module_path)
            tool_class = getattr(module, class_name)

            try:
                tool_instance = tool_class()
            except TypeError as e:
                if "missing" in str(e) and "required positional argument" in str(e):
                    logger.warning(f"[动态加载-同步] 工具需要依赖注入，将延迟实例化 | tool_name={tool_name} | hint={e}")
                    tool_definition = tool_class.get_tool_definition()
                    tool_instance = None
                else:
                    raise
            else:
                tool_definition = tool_instance.get_tool_definition()

            if tool_instance is not None:
                registered_name = self._registry.register_with_handler(
                    tool=tool_definition,
                    handler=tool_instance.execute,
                )
            else:
                registered_name = self._registry.register(tool_definition)
                logger.info(f"[动态加载-同步] 工具定义已注册（无handler），执行时需要注入依赖 | tool_name={tool_name}")

            self._loaded.add(tool_name)

            logger.debug(f"[动态加载-同步] 工具加载成功 | tool_name={tool_name} | registered_name={registered_name}")

            return registered_name

        except ImportError as e:
            logger.error(
                f"[动态加载-同步] 导入模块失败 | "
                f"tool_name={tool_name} | module={self._tool_modules.get(tool_name)} | "
                f"error={e}"
            )
            raise ToolNotFoundError(tool_name) from e

        except AttributeError as e:
            logger.error(
                f"[动态加载-同步] 找不到工具类 | "
                f"tool_name={tool_name} | class={self._tool_classes.get(tool_name)} | "
                f"error={e}"
            )
            raise ToolNotFoundError(tool_name) from e

        except ToolNotFoundError:
            raise

        except Exception as e:
            logger.error(
                f"[动态加载-同步] 加载工具失败 | tool_name={tool_name} | error={e}",
                exc_info=True,
            )
            raise ToolNotFoundError(tool_name) from e

        finally:
            if tool_name in self._loading:
                del self._loading[tool_name]

    def ensure_loaded_sync(self, tool_names: list[str]) -> None:
        """同步确保指定的工具都已加载"""
        for tool_name in tool_names:
            if self.is_loaded(tool_name) or self._registry.has(tool_name):
                continue

            if self.is_available(tool_name):
                try:
                    self.load_tool_sync(tool_name)
                except ToolNotFoundError:
                    logger.warning(f"[动态加载-同步] 无法加载工具 | tool_name={tool_name}")

    def get_loaded_tools(self) -> set:
        """获取已加载的工具集合"""
        return self._loaded.copy()

    def get_available_tools(self) -> list[str]:
        """获取所有可用的工具名称（包括未加载的）"""
        # 确保已完成发现
        if not self._discovered:
            self._discover_tools()

        return list(self._tool_modules.keys())

    def get_discovered_tools(self) -> dict[str, tuple[str, str]]:
        """获取所有已发现的工具映射

        Returns:
            dict: {工具名: (模块路径, 类名)}
        """
        if not self._discovered:
            self._discover_tools()
        return self._tool_classes.copy()


# 全局动态加载器实例
_global_loader: DynamicToolLoader | None = None


def get_dynamic_tool_loader() -> DynamicToolLoader | None:
    """获取全局动态工具加载器"""
    return _global_loader


def set_dynamic_tool_loader(loader: DynamicToolLoader) -> None:
    """设置全局动态工具加载器"""
    global _global_loader  # noqa: PLW0603
    _global_loader = loader


def init_dynamic_tool_loader(registry: ToolRegistry) -> DynamicToolLoader:
    """初始化全局动态工具加载器"""
    global _global_loader  # noqa: PLW0603
    _global_loader = DynamicToolLoader(registry)
    return _global_loader
