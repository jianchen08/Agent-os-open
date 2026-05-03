"""运行时热加载模块。

负责将生成的代码动态加载到运行时环境：
- 将代码写入文件系统
- 使用 importlib 动态导入
- 注册到工具注册中心
- 支持卸载和清理

整合已有的热加载设施（src/tools/builtin/hot_swap.py 等）。

暴露接口：
- load_plugin(artifact) -> bool
- unload_plugin(plugin_name) -> bool
- is_loaded(plugin_name) -> bool
- HotLoader: 热加载器类
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from evolution.types import GeneratedArtifact, GenerationType

logger = logging.getLogger(__name__)


class HotLoader:
    """运行时热加载器。

    将生成的代码动态加载到运行时环境，支持：
    - 写入文件系统
    - 动态导入模块
    - 注册到工具注册中心
    - 卸载和清理

    Attributes:
        _tool_registry: 工具注册中心实例
        _loaded_modules: 已加载的模块映射 {plugin_name: module}
        _loaded_paths: 已加载的文件路径映射 {plugin_name: file_path}
    """

    def __init__(
        self,
        tool_registry: Any | None = None,
        base_path: str = ".",
    ) -> None:
        """初始化热加载器。

        Args:
            tool_registry: 工具注册中心实例（需实现 register_with_handler 方法）
            base_path: 基础路径（用于写入文件）
        """
        self._tool_registry = tool_registry
        self._base_path = Path(base_path)
        self._loaded_modules: dict[str, Any] = {}
        self._loaded_paths: dict[str, str] = {}

    def load_plugin(self, artifact: GeneratedArtifact) -> bool:
        """加载生成的代码到运行时环境。

        流程：
        1. 将代码写入文件系统
        2. 使用 importlib 动态导入
        3. 提取工具类实例
        4. 注册到工具注册中心

        Args:
            artifact: 生成的代码产物

        Returns:
            是否加载成功
        """
        plugin_name = self._extract_plugin_name(artifact)
        logger.info(
            "[HotLoader] 开始加载插件: name='%s', type=%s",
            plugin_name,
            artifact.generation_type.value,
        )

        try:
            # Step 1: 写入文件
            file_path = self._write_artifact(artifact)
            if file_path is None:
                logger.error("[HotLoader] 写入文件失败: %s", artifact.file_path)
                return False

            # Step 2: 动态导入
            module = self._dynamic_import(plugin_name, file_path)
            if module is None:
                logger.error("[HotLoader] 动态导入失败: %s", file_path)
                self._cleanup_file(file_path)
                return False

            # Step 3: 提取并注册工具
            if artifact.generation_type == GenerationType.BUILTIN_TOOL:
                success = self._register_builtin_tool(plugin_name, module)
                if not success:
                    self._cleanup_file(file_path)
            else:
                # MCP Server 暂时只做导入验证
                success = True
                logger.info(
                    "[HotLoader] MCP Server 导入成功: %s", plugin_name
                )

            if success:
                self._loaded_modules[plugin_name] = module
                self._loaded_paths[plugin_name] = str(file_path)
                logger.info(
                    "[HotLoader] 插件加载成功: name='%s', path='%s'",
                    plugin_name,
                    file_path,
                )

            return success

        except Exception as exc:
            logger.error(
                "[HotLoader] 插件加载异常: name='%s', error=%s",
                plugin_name,
                exc,
            )
            return False

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载已加载的插件。

        从注册中心移除并清理模块缓存。

        Args:
            plugin_name: 插件名称

        Returns:
            是否卸载成功
        """
        logger.info("[HotLoader] 开始卸载插件: name='%s'", plugin_name)

        try:
            # Step 1: 从注册中心移除
            if self._tool_registry is not None:
                try:
                    if hasattr(self._tool_registry, "has") and self._tool_registry.has(plugin_name):
                        self._tool_registry.unregister(plugin_name)
                except Exception as exc:
                    logger.warning(
                        "[HotLoader] 从注册中心移除失败: %s", exc
                    )

            # Step 2: 清理模块缓存
            if plugin_name in self._loaded_modules:
                module = self._loaded_modules.pop(plugin_name)

                # 从 sys.modules 中移除
                module_name = getattr(module, "__name__", "")
                if module_name and module_name in sys.modules:
                    del sys.modules[module_name]

            # Step 3: 清理路径记录
            self._loaded_paths.pop(plugin_name, None)

            logger.info("[HotLoader] 插件卸载成功: name='%s'", plugin_name)
            return True

        except Exception as exc:
            logger.error(
                "[HotLoader] 插件卸载异常: name='%s', error=%s",
                plugin_name,
                exc,
            )
            return False

    def is_loaded(self, plugin_name: str) -> bool:
        """检查插件是否已加载。

        Args:
            plugin_name: 插件名称

        Returns:
            是否已加载
        """
        # 检查本地记录
        if plugin_name in self._loaded_modules:
            return True

        # 检查工具注册中心
        if self._tool_registry is not None:
            try:
                if hasattr(self._tool_registry, "has"):
                    return self._tool_registry.has(plugin_name)
            except Exception:
                pass

        return False

    def get_loaded_plugins(self) -> list[str]:
        """获取所有已加载的插件名称列表。

        Returns:
            插件名称列表
        """
        return list(self._loaded_modules.keys())

    def _cleanup_file(self, file_path: Path) -> None:
        """清理加载失败时已写入的文件。

        Args:
            file_path: 需要删除的文件路径
        """
        try:
            if file_path.exists():
                file_path.unlink()
                logger.info("[HotLoader] 已清理文件: %s", file_path)
        except Exception as exc:
            logger.warning("[HotLoader] 清理文件失败: %s, error=%s", file_path, exc)

    def _write_artifact(self, artifact: GeneratedArtifact) -> Path | None:
        """将代码产物写入文件系统。

        Args:
            artifact: 代码产物

        Returns:
            写入的文件路径，失败返回 None
        """
        try:
            file_path = (self._base_path / artifact.file_path).resolve()
            # 路径遍历防护：确保解析后的路径在 base_path 内
            if not str(file_path).startswith(str(self._base_path.resolve())):
                raise ValueError(f"非法文件路径: {artifact.file_path}")
            file_path.parent.mkdir(parents=True, exist_ok=True)

            file_path.write_text(artifact.code, encoding="utf-8")
            logger.info("[HotLoader] 写入文件: %s", file_path)
            return file_path

        except Exception as exc:
            logger.error("[HotLoader] 写入文件失败: %s", exc)
            return None

    def _dynamic_import(
        self,
        plugin_name: str,
        file_path: Path,
    ) -> Any | None:
        """使用 importlib 动态导入模块。

        Args:
            plugin_name: 插件名称（作为模块标识）
            file_path: 文件路径

        Returns:
            导入的模块对象，失败返回 None
        """
        try:
            module_name = f"evolution.plugins.{plugin_name}"

            # 创建模块规格
            spec = importlib.util.spec_from_file_location(
                module_name,
                str(file_path),
            )
            if spec is None or spec.loader is None:
                logger.error(
                    "[HotLoader] 无法创建模块规格: %s", file_path
                )
                return None

            # 创建并执行模块
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            return module

        except Exception as exc:
            logger.error(
                "[HotLoader] 动态导入失败: file=%s, error=%s",
                file_path,
                exc,
            )
            return None

    def _register_builtin_tool(self, plugin_name: str, module: Any) -> bool:
        """注册 BuiltinTool 到工具注册中心。

        从模块中提取工具类并注册。

        Args:
            plugin_name: 插件名称
            module: 已导入的模块

        Returns:
            是否注册成功
        """
        if self._tool_registry is None:
            logger.info(
                "[HotLoader] 无注册中心，跳过注册: %s", plugin_name
            )
            return True  # 无注册中心时视为成功（仅导入验证）

        try:
            # 查找工具类（继承 BuiltinTool 的类）
            tool_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and hasattr(attr, "get_tool_definition")
                    and hasattr(attr, "execute")
                    and attr_name != "BuiltinTool"
                ):
                    tool_class = attr
                    break

            if tool_class is None:
                logger.error(
                    "[HotLoader] 未找到工具类: module=%s", module
                )
                return False

            # 实例化并注册
            tool_instance = tool_class()
            tool_def = tool_instance.get_tool_definition()

            if hasattr(self._tool_registry, "register_with_handler"):
                self._tool_registry.register_with_handler(
                    tool=tool_def,
                    handler=tool_instance.execute,
                    overwrite=True,
                )
            elif hasattr(self._tool_registry, "register"):
                self._tool_registry.register(tool=tool_def, overwrite=True)

            logger.info(
                "[HotLoader] 工具注册成功: name='%s'", tool_def.name
            )
            return True

        except Exception as exc:
            logger.error(
                "[HotLoader] 工具注册失败: name='%s', error=%s",
                plugin_name,
                exc,
            )
            return False

    @staticmethod
    def _extract_plugin_name(artifact: GeneratedArtifact) -> str:
        """从产物中提取插件名称。

        Args:
            artifact: 代码产物

        Returns:
            插件名称
        """
        path = Path(artifact.file_path)
        return path.stem
