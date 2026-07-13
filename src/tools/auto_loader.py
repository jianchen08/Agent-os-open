"""
工具自动加载器

暴露接口：
- get_tool_auto_loader() -> ToolAutoLoader | None：get_tool_auto_loader功能
- init_tool_auto_loader(registry: ToolRegistry, db_session: Any | None) -> ToolAutoLoader：init_tool_auto_loader功能
- reset_tool_auto_loader() -> None：reset_tool_auto_loader功能
- set_db_session(self, session: Any) -> None：set_db_session功能
- get_tool_guide(self, tool_name: str) -> str | None：get_tool_guide功能
- ToolAutoLoader：ToolAutoLoader类
"""

import importlib
import importlib.util
import logging
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from tools.registry import ToolRegistry
from tools.types import Tool, ToolCategory, ToolExample, ToolLevel, ToolSource

logger = logging.getLogger(__name__)

# 工具处理函数类型（与 registry.py 保持一致）
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class ToolAutoLoader:
    """
    工具自动加载器

    当 Agent 调用未注册的工具时，自动：
    1. 从数据库加载工具定义（唯一的配置来源）
    2. 加载工具的使用指南
    3. 动态注册工具
    4. 返回工具供执行
    """

    # Python 代码目录（用于加载执行实现）
    TOOL_CODE_DIR = Path("src/tools/builtin")

    def __init__(
        self,
        registry: ToolRegistry,
        db_session: Any | None = None,
    ):
        """初始化自动加载器"""
        self._registry = registry
        self._db_session = db_session
        self._loading: set[str] = set()  # 正在加载的工具（防止循环）
        self._tool_guides: dict[str, str] = {}  # 工具使用指南缓存
        self._file_index: dict[str, Path] | None = None  # 工具名→文件路径索引（惰性构建）

    def set_db_session(self, session: Any) -> None:
        """设置数据库会话（支持延迟注入）"""
        self._db_session = session

    async def auto_load_tool(self, tool_name: str) -> Tool | None:
        """自动加载工具"""
        # 检查是否已注册
        if self._registry.has(tool_name):
            logger.debug("[自动加载] 工具已注册: %s", tool_name)
            # 已注册但未标记为动态工具时，补充标记
            # 这样 tool_schema 才能将其 schema 注入给 LLM
            if tool_name not in self._registry.get_dynamic_tool_names():
                self._registry.mark_dynamic(tool_name)
                logger.info("[自动加载] 已注册工具标记为动态: %s", tool_name)
            return self._registry.get(tool_name)

        # 防止循环加载
        if tool_name in self._loading:
            logger.warning("[自动加载] 检测到循环加载: %s", tool_name)
            return None

        self._loading.add(tool_name)

        try:
            logger.info("[自动加载] 开始加载工具: %s", tool_name)

            # 1. 从数据库加载工具定义（唯一的配置来源）
            tool = await self._load_from_database(tool_name)
            if tool:
                logger.info("[自动加载] 从数据库加载成功: %s", tool_name)
                return tool

            # 2. 尝试从 Python 代码加载（内置工具的实现）
            tool = await self._load_from_python_code(tool_name)
            if tool:
                logger.info("[自动加载] 从 Python 代码加载成功: %s", tool_name)
                return tool

            logger.warning("[自动加载] 未找到工具: %s", tool_name)
            return None

        finally:
            self._loading.discard(tool_name)

    async def _load_from_database(self, tool_name: str) -> Tool | None:
        """从数据库加载工具。

        加载成功后注册到 ToolRegistry 并标记为动态工具，
        以便 ToolSchemaPlugin 在下一轮将其 schema 注入给 LLM。
        """
        if not self._db_session:
            return None

        try:
            from db.models import ToolLibrary  # noqa: PLC0415
            from sqlalchemy import select  # noqa: PLC0415

            result = await self._db_session.execute(select(ToolLibrary).where(ToolLibrary.name == tool_name))
            db_tool = result.scalar_one_or_none()

            if not db_tool:
                return None

            # 转换为 Tool 对象
            tool = self._db_tool_to_tool(db_tool)

            # 创建通用执行器
            handler = self._create_db_handler(tool_name, db_tool)

            # 检查是否为首次加载（非已注册工具）
            is_new = not self._registry.has(tool_name)

            # 注册工具
            self._registry.register_with_handler(tool, handler)

            # 首次加载时标记为动态工具
            if is_new:
                self._registry.mark_dynamic(tool_name)

            # 缓存使用指南
            self._cache_tool_guide(tool_name, db_tool)

            return tool

        except Exception as e:
            logger.warning("[自动加载] 数据库加载失败: %s, 错误: %s", tool_name, e)
            return None

    def _db_tool_to_tool(self, db_tool: Any) -> Tool:
        """将数据库模型转换为 Tool 对象"""
        # 获取数据库 ID
        db_id = str(db_tool.id) if hasattr(db_tool, "id") else None

        # 构建输入 schema
        input_schema = db_tool.input_schema or {"type": "object", "properties": {}}

        # 解析分类
        category: ToolCategory | None = None
        category = ToolCategory(db_tool.category) if db_tool.category else ToolCategory.SYSTEM

        # 解析级别
        level = ToolLevel.USER
        if db_tool.level:
            level = ToolLevel(db_tool.level)

        return Tool(
            name=db_tool.name,
            description=db_tool.description or "",
            when_to_use=db_tool.when_to_use or [],
            when_not_to_use=db_tool.when_not_to_use or [],
            examples=[ToolExample(**ex) for ex in (db_tool.examples or [])],
            caveats=db_tool.caveats or [],
            input_schema=input_schema,
            output_schema=db_tool.output_schema,
            source=ToolSource.DATABASE,
            category=category,
            level=level,
            version=db_tool.version or "1.0.0",
            tags=db_tool.tags or [],
            db_id=db_id,
        )

    def _cache_tool_guide(self, tool_name: str, db_tool: Any) -> None:
        """缓存工具使用指南"""
        guide_parts: list[str] = []

        if db_tool.description:
            guide_parts.append(f"## 工具描述\n{db_tool.description}")

        if db_tool.when_to_use:
            items = "\n".join(f"- {item}" for item in db_tool.when_to_use)
            guide_parts.append(f"## 适用场景\n{items}")

        if db_tool.when_not_to_use:
            items = "\n".join(f"- {item}" for item in db_tool.when_not_to_use)
            guide_parts.append(f"## 不适用场景\n{items}")

        if db_tool.examples:
            guide_parts.append("## 使用示例")
            for i, ex in enumerate(db_tool.examples, 1):
                guide_parts.append(f"### 示例 {i}")
                if ex.get("description"):
                    guide_parts.append(f"说明: {ex['description']}")
                if ex.get("input"):
                    guide_parts.append(f"输入: ```json\n{ex['input']}\n```")
                if ex.get("output"):
                    guide_parts.append(f"输出: ```json\n{ex['output']}\n```")

        if db_tool.caveats:
            items = "\n".join(f"- {item}" for item in db_tool.caveats)
            guide_parts.append(f"## 注意事项\n{items}")

        self._tool_guides[tool_name] = "\n\n".join(guide_parts)

    async def _load_from_python_code(self, tool_name: str) -> Tool | None:  # noqa: PLR0912
        """从 Python 代码动态加载工具（使用文件索引加速查找）。

        加载成功后注册到 ToolRegistry 并标记为动态工具，
        以便 ToolSchemaPlugin 在下一轮将其 schema 注入给 LLM。
        """
        if not self.TOOL_CODE_DIR.exists():
            return None

        # 惰性构建文件索引（只扫描一次）
        if self._file_index is None:
            self._file_index = self._build_file_index()

        # 先尝试精确匹配索引
        indexed_path = self._file_index.get(tool_name)
        if indexed_path is not None:
            try:
                tool_instance = self._load_tool_from_file(indexed_path, tool_name)
                if tool_instance:
                    tool_def: Tool = tool_instance.get_tool_definition()

                    # 检查是否为首次加载
                    is_new = not self._registry.has(tool_name)

                    self._registry.register_with_handler(
                        tool=tool_def,
                        handler=tool_instance.execute,
                    )

                    # 首次加载时标记为动态工具
                    if is_new:
                        self._registry.mark_dynamic(tool_name)

                    # 注册 Schema 丰富器（如果工具实例支持）
                    if hasattr(tool_instance, "get_schema_enricher"):
                        enricher = tool_instance.get_schema_enricher()
                        if enricher:
                            self._registry.register_schema_enricher(tool_def.name, enricher)

                    self._cache_python_guide(tool_name, tool_def)
                    return tool_def
            except Exception as e:
                logger.debug("[自动加载] 索引文件加载失败: %s, 错误: %s", indexed_path, e)

        # 索引未命中，退回全扫描（首次或索引不完整时）
        for entry in self.TOOL_CODE_DIR.iterdir():
            if entry.is_dir() and not entry.name.startswith("_"):
                tool_file = entry / "tool.py"
                if tool_file.exists():
                    py_file = tool_file
                else:
                    continue
            elif entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
                py_file = entry
            else:
                continue
            if py_file.name.startswith("_"):
                continue

            try:
                tool_instance = self._load_tool_from_file(py_file, tool_name)
                if tool_instance:
                    tool_def = tool_instance.get_tool_definition()

                    # 检查是否为首次加载
                    is_new = not self._registry.has(tool_name)

                    self._registry.register_with_handler(
                        tool=tool_def,
                        handler=tool_instance.execute,
                    )

                    # 首次加载时标记为动态工具
                    if is_new:
                        self._registry.mark_dynamic(tool_name)

                    # 注册 Schema 丰富器（如果工具实例支持）
                    if hasattr(tool_instance, "get_schema_enricher"):
                        enricher = tool_instance.get_schema_enricher()
                        if enricher:
                            self._registry.register_schema_enricher(tool_def.name, enricher)

                    self._cache_python_guide(tool_name, tool_def)

                    # 更新索引
                    self._file_index[tool_name] = py_file

                    return tool_def

            except Exception as e:
                logger.debug("[自动加载] 加载 Python 失败: %s, 错误: %s", py_file, e)
                continue

        return None

    def _build_file_index(self) -> dict[str, Path]:
        """构建工具名→文件路径的索引映射（扫描一次，后续直接命中）。

        通过文件名启发式匹配：如 task.py → task, task_manage 等。
        """
        index: dict[str, Path] = {}
        if not self.TOOL_CODE_DIR.exists():
            return index

        for py_file in self.TOOL_CODE_DIR.rglob("*.py"):
            if py_file.name.startswith("_"):
                continue
            stem = py_file.stem
            # 将文件名中的常见分隔符转为可能的工具名
            candidates = {stem, stem.replace("_", ""), stem.replace("_", "-")}
            for name in candidates:
                if name not in index:
                    index[name] = py_file

        return index

    def _load_tool_from_file(
        self,
        file_path: Path,
        target_tool_name: str,
    ) -> Any | None:
        """从 Python 文件加载工具实例"""
        try:
            # 生成唯一模块名
            module_name = f"dynamic_tools.{file_path.stem}_{id(file_path)}"

            # 加载模块
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if not spec or not spec.loader:
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # 查找工具类
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue

                attr = getattr(module, attr_name)

                # 检查是否是工具类
                if isinstance(attr, type) and hasattr(attr, "get_tool_definition") and hasattr(attr, "execute"):
                    try:
                        # 尝试实例化
                        instance = attr()
                        tool_def = instance.get_tool_definition()

                        if tool_def.name == target_tool_name:
                            return instance
                    except TypeError:
                        # 需要依赖注入，跳过
                        continue
                    except Exception:
                        continue

            return None

        except Exception as e:
            logger.debug("[自动加载] 模块加载失败: %s, 错误: %s", file_path, e)
            return None

    def _cache_python_guide(self, tool_name: str, tool_def: Tool) -> None:
        """缓存 Python 工具的使用指南"""
        guide_parts: list[str] = []

        guide_parts.append(f"## 工具描述\n{tool_def.description}")

        if tool_def.when_to_use:
            items = "\n".join(f"- {item}" for item in tool_def.when_to_use)
            guide_parts.append(f"## 适用场景\n{items}")

        if tool_def.when_not_to_use:
            items = "\n".join(f"- {item}" for item in tool_def.when_not_to_use)
            guide_parts.append(f"## 不适用场景\n{items}")

        if tool_def.examples:
            guide_parts.append("## 使用示例")
            for i, ex in enumerate(tool_def.examples, 1):
                guide_parts.append(f"### 示例 {i}")
                if ex.description:
                    guide_parts.append(f"说明: {ex.description}")

        if tool_def.caveats:
            items = "\n".join(f"- {item}" for item in tool_def.caveats)
            guide_parts.append(f"## 注意事项\n{items}")

        self._tool_guides[tool_name] = "\n\n".join(guide_parts)

    def _create_db_handler(
        self,
        tool_name: str,
        db_tool: Any,
    ) -> ToolHandler:
        """创建数据库工具的处理器"""

        async def handler(_inputs: dict[str, Any]) -> dict[str, Any]:
            # 检查是否有对应的 Python 实现
            source_code = getattr(db_tool, "source_code", None)
            if source_code:
                # 动态代码执行暂不支持，需要安全沙箱
                return {
                    "success": False,
                    "error": f"工具 {tool_name} 需要动态代码执行支持",
                    "metadata": {"tool_name": tool_name},
                }

            return {
                "success": False,
                "error": f"工具 {tool_name} 没有可执行的实现",
                "metadata": {"tool_name": tool_name},
            }

        return handler

    def get_tool_guide(self, tool_name: str) -> str | None:
        """获取工具使用指南"""
        return self._tool_guides.get(tool_name)

    async def search_available_tools(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """搜索可用工具（从数据库和已注册的工具中搜索）"""
        results: list[dict[str, Any]] = []
        query_lower = query.lower()

        # 1. 搜索已注册的工具
        for tool in self._registry.list_all():
            if self._match_query(query_lower, tool):
                results.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "source": "registered",
                        "category": (tool.category.value if tool.category else None),
                    }
                )

        # 2. 搜索数据库中的工具
        if self._db_session:
            db_tools = await self._search_database_tools(query_lower, limit)
            for db_tool in db_tools:
                if db_tool["name"] not in [r["name"] for r in results]:
                    results.append(db_tool)

        return results[:limit]

    def _match_query(self, query: str, tool: Tool) -> bool:
        """检查工具是否匹配查询"""
        if query in tool.name.lower():
            return True
        if query in tool.description.lower():
            return True
        return any(query in tag.lower() for tag in tool.tags)

    async def _search_database_tools(
        self,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """搜索数据库中的工具"""
        if not self._db_session:
            return []

        try:
            from db.models import ToolLibrary  # noqa: PLC0415
            from sqlalchemy import or_, select  # noqa: PLC0415

            # 使用 ilike 进行模糊搜索
            stmt = (
                select(ToolLibrary)
                .where(
                    or_(
                        ToolLibrary.name.ilike(f"%{query}%"),
                        ToolLibrary.description.ilike(f"%{query}%"),
                    )
                )
                .limit(limit)
            )
            result = await self._db_session.execute(stmt)
            db_tools = result.scalars().all()

            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "source": "database",
                    "category": t.category,
                }
                for t in db_tools
            ]

        except Exception as e:
            logger.warning("[自动加载] 数据库搜索失败: %s", e)
            return []


# 全局自动加载器实例
_global_auto_loader: ToolAutoLoader | None = None


def get_tool_auto_loader() -> ToolAutoLoader | None:
    """获取全局自动加载器"""
    return _global_auto_loader


def init_tool_auto_loader(
    registry: ToolRegistry,
    db_session: Any | None = None,
) -> ToolAutoLoader:
    """初始化全局自动加载器"""
    global _global_auto_loader  # noqa: PLW0603
    _global_auto_loader = ToolAutoLoader(registry, db_session)
    return _global_auto_loader


def reset_tool_auto_loader() -> None:
    """重置全局自动加载器"""
    global _global_auto_loader  # noqa: PLW0603
    _global_auto_loader = None
