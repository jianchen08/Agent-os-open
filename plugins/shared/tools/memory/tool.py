"""记忆工具（0.2 重写版）。

0.1 的记忆服务（memory.service.MemoryService）已在 0.2 中删除，本模块改为
注入式 IMemoryBackend（见 plugins/shared/system/hindsight_memory/memory_backend.py
定义的端口：add / search / delete / import_document，全部 async）。
本模块保持自包含：不导入 0.1 已删除的 core/tools/memory 包，Tool 与
ToolExecutionResult 在本模块内就地定义（与 0.1 结构对齐）。

设计要点：
- 后端可注入（构造参数或 set_memory_backend()），测试可传 AsyncMock。
- 未注入后端时不崩溃，返回错误结果「memory backend 未注入」。
- 所有后端调用包 try/except，异常转为失败结果——与记忆后端韧性约定一致。
- 动作映射：
  - store        → backend.add(user_id, content, memory_type, tags, source="memory_tool")
  - retrieve     → backend.search(query, user_id, top_k, memory_type)
  - import_text  → backend.import_document(user_id, text=..., name=...)
  - import_file  → backend.import_document(user_id, file_path=..., name=...)
  - delete       → backend.delete(user_id, memory_id)
  - update       → 后端支持 update 则调用；否则降级为 backend.add（同内容）
  - get_context  → backend.search 更宽泛查询（更大 top_k，不过滤类型）

暴露接口：
- create_success_result(data: Any, metadata: dict | None) -> ToolExecutionResult
- create_failure_result(error: str, error_code: str | None) -> ToolExecutionResult
- Tool：工具定义（与 0.1 tools.types.Tool 关键字段对齐）
- ToolExecutionResult：工具执行结果（与 0.1 core.results.ToolExecutionResult 对齐）
- MemoryTool：记忆工具类
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 本地类型定义（0.1 core.results / tools.types 在 0.2 已删除，就地对齐定义）
# ═══════════════════════════════════════════════════════════


@dataclass
class ToolExecutionResult:
    """工具执行结果——与 0.1 core.results.ToolExecutionResult 结构对齐。

    Attributes:
        success: 是否成功
        output: 输出数据（成功时有值）
        error: 错误信息（失败时有值）
        error_code: 错误代码（可选）
        metadata: 附加元数据
    """

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create_completed(
        cls,
        output: Any,
        metadata: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """创建成功结果。"""
        return cls(
            success=True,
            output=output,
            metadata=metadata or {},
        )

    @classmethod
    def create_failed(
        cls,
        error: str,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """创建失败结果。"""
        return cls(
            success=False,
            error=error,
            error_code=error_code,
            metadata=metadata or {},
        )


class ToolCategory(str, Enum):
    """工具功能分类（与 0.1 tools.types.ToolCategory 对齐）。"""

    FILE = "file"
    FILE_SYSTEM = "file_system"
    SEARCH = "search"
    WEB = "web"
    MEMORY = "memory"
    TASK = "task"
    SYSTEM = "system"
    EXECUTION = "execution"
    ANALYSIS = "analysis"
    EVALUATION = "evaluation"
    AGENT = "agent"
    MONITORING = "monitoring"


class ToolLevel(str, Enum):
    """工具级别分类（与 0.1 tools.types.ToolLevel 对齐）。"""

    SYSTEM = "system"
    USER = "user"
    L1_ONLY = "l1_only"
    L1_L2_ONLY = "l1_l2_only"
    ALL = "all"


class ToolSource(str, Enum):
    """工具来源（与 0.1 tools.types.ToolSource 对齐）。"""

    CODE = "code"
    BUILTIN = "builtin"
    MCP = "mcp"
    HTTP = "http"
    DATABASE = "database"


@dataclass
class Tool:
    """工具定义——与 0.1 tools.types.Tool 的关键字段对齐。

    Attributes:
        name: 工具唯一标识
        description: 工具功能描述
        input_schema: 输入参数 JSON Schema
        category: 功能分类
        level: 工具级别
        source: 工具来源
        injected_params: 运行时注入参数列表（不暴露给 LLM）
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    category: ToolCategory | None = None
    level: ToolLevel = ToolLevel.USER
    source: ToolSource = ToolSource.CODE
    injected_params: list[str] = field(default_factory=list)


def create_success_result(
    data: Any = None,
    metadata: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    """创建成功结果。"""
    return ToolExecutionResult.create_completed(
        output=data,
        metadata=metadata or {},
    )


def create_failure_result(
    error: str,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    """创建失败结果。"""
    return ToolExecutionResult.create_failed(
        error=error,
        error_code=error_code,
        metadata=metadata or {},
    )


# ═══════════════════════════════════════════════════════════
# MemoryTool
# ═══════════════════════════════════════════════════════════


class MemoryTool:
    """
    记忆工具（IMemoryBackend 注入版）

    提供：
    - store：存储记忆（backend.add）
    - retrieve：检索记忆（backend.search）
    - import_text：导入文本知识（backend.import_document）
    - import_file：导入文件知识（backend.import_document）
    - update：更新知识（后端不支持时降级为 add）
    - delete：删除记忆（backend.delete）
    - get_context：获取会话上下文（更宽泛的 backend.search）

    后端为 duck-type 的 IMemoryBackend（AsyncMock / HindsightBackend /
    KernelMemoryBackend 均可）；未注入时执行返回错误结果，不崩溃。
    """

    SYSTEM_USER_ID = "system"

    def __init__(self, memory_backend: Any | None = None):
        """初始化记忆工具。

        Args:
            memory_backend: IMemoryBackend 实例（add/search/delete/import_document，
                全部 async）；可后续通过 set_memory_backend() 注入。
        """
        self._memory_backend = memory_backend
        # 服务端可信 caller 身份（由运行时注入，防客户端篡改 user_id 实施 IDOR）。
        self._trusted_user_id: str | None = None

    def set_memory_backend(self, backend: Any) -> None:
        """注入记忆后端（IMemoryBackend 实例或兼容 duck-type）。

        Args:
            backend: IMemoryBackend 实例；None 表示清除后端。
        """
        self._memory_backend = backend

    def set_trusted_user_id(self, user_id: str | None) -> None:
        """注入服务端可信 caller 身份（鉴权层解析后调用）。

        安全语义：一旦注入，``_resolve_user_id`` 将**无条件**使用此身份作为
        记忆隔离 key，彻底忽略客户端 ``inputs["user_id"]``（后者可被任意调用者
        篡改以实施 IDOR——读写他人记忆）。仅在未注入时才回退到 inputs（兼容旧路径，
        标注为不可信）。

        Args:
            user_id: 可信 caller 用户 ID；None 表示清除（回退到 inputs/系统默认）。
        """
        self._trusted_user_id = user_id or None

    def _resolve_user_id(self, inputs: dict[str, Any]) -> str:
        """解析用户隔离 key。

        鉴权优先级（高 → 低）：
        1. ``self._trusted_user_id``：服务端注入的可信 caller 身份——**忽略**
           客户端 ``inputs["user_id"]``，防 IDOR。
        2. ``inputs["user_id"]``：**不可信**回退（仅在未注入可信身份时沿用，
           兼容旧调用路径）。
        3. ``SYSTEM_USER_ID``：缺省系统态。

        Args:
            inputs: 工具输入

        Returns:
            用户隔离 key
        """
        if self._trusted_user_id:
            return self._trusted_user_id
        # 回退：无服务端可信注入时沿用 inputs（不可信，仅向后兼容）。
        return str(inputs.get("user_id") or self.SYSTEM_USER_ID)

    @staticmethod
    def _extract_memory_id(value: Any) -> str:
        """从 file_path（可能带 memory:// 前缀）或裸 id 提取 memory_id。

        Args:
            value: file_path 或 memory_id 原始值

        Returns:
            提取后的 memory_id；空值返回 ""
        """
        if not value:
            return ""
        text = str(value)
        if text.startswith("memory://"):
            return text.removeprefix("memory://")
        return text

    @staticmethod
    def _inject_agent_tags(
        inputs: dict[str, Any], tags: list[str]
    ) -> list[str]:
        """把 agent_config_id 自动注入为标签（与 0.1 行为对齐）。

        Args:
            inputs: 工具输入（含 agent_config_id）
            tags: 原始标签列表（会被拷贝）

        Returns:
            注入 agent_config_id 后的标签列表
        """
        out = list(tags)
        agent_config_id = inputs.get("agent_config_id", "")
        if agent_config_id and agent_config_id not in out:
            out.append(agent_config_id)
        return out

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="memory",
            description=(
                "记忆工具：存储和检索知识、情景记忆，支持导入文本和文件知识。\n"
                "⚠️ 重要：存储记忆时必须填写 tags 参数，用简洁的关键词标签标记内容分类和主题，"
                "便于后续精准检索。系统会自动将当前 Agent 名称作为标签注入，无需手动添加。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "store",
                            "retrieve",
                            "import_text",
                            "import_file",
                            "update",
                            "delete",
                            "get_context",
                            "list",
                        ],
                        "description": "操作类型",
                    },
                    "content": {
                        "type": "string",
                        "description": "内容（store/import_text时使用）",
                    },
                    "name": {
                        "type": "string",
                        "description": "知识名称（import_text时使用）",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "文件路径（import_file/update/delete时使用）",
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "记忆ID（delete时使用，与 file_path 二选一）",
                    },
                    "query": {
                        "type": "string",
                        "description": "检索查询（retrieve时使用）",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "标签列表（强烈建议填写）。用于标记内容分类和主题，"
                            "支持后续按标签精准检索。例如：['coding_standards', 'python']、"
                            "['bug_pattern', 'timeout']。系统会自动追加当前 Agent 名称作为标签。"
                        ),
                    },
                    "filter": {
                        "type": "object",
                        "description": "筛选条件（第一层决策）",
                        "properties": {
                            "memory_type": {
                                "type": "string",
                                "enum": ["semantic", "episode"],
                                "default": "semantic",
                                "description": "记忆类型",
                            },
                            "knowledge_id": {
                                "type": "string",
                                "description": "知识库ID（与 knowledge_name 二选一）",
                            },
                            "knowledge_name": {
                                "type": "string",
                                "description": "知识库名称（与 knowledge_id 二选一）",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "标签筛选",
                            },
                            "session_id": {
                                "type": "string",
                                "description": "会话ID",
                            },
                        },
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "description": "检索数量",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "会话ID（get_context时使用）",
                    },
                },
                "required": ["action"],
            },
            category=ToolCategory.MEMORY,
            level=ToolLevel.SYSTEM,
            source=ToolSource.CODE,
            injected_params=["session_id", "user_id", "agent_config_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行记忆操作。

        Args:
            inputs: 工具输入，必须含 action；其余参数按动作映射读取

        Returns:
            ToolExecutionResult：成功含 output 字典，失败含 error
        """
        if self._memory_backend is None:
            return create_failure_result("memory backend 未注入")

        action = inputs.get("action")

        if action == "store":
            return await self._store(inputs)
        if action == "retrieve":
            return await self._retrieve(inputs)
        if action == "import_text":
            return await self._import_text(inputs)
        if action == "import_file":
            return await self._import_file(inputs)
        if action == "update":
            return await self._update(inputs)
        if action == "delete":
            return await self._delete(inputs)
        if action == "get_context":
            return await self._get_context(inputs)
        if action == "list":
            return await self._list(inputs)
        return create_failure_result(f"未知操作: {action}")

    async def _store(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """存储记忆，自动将 agent_config_id 注入为标签。"""
        backend = self._memory_backend
        content = inputs.get("content")
        memory_type = inputs.get("memory_type", "semantic")
        tags = self._inject_agent_tags(inputs, list(inputs.get("tags", [])))

        if not content:
            return create_failure_result("缺少 content 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            memory_id = await backend.add(
                user_id=user_id,
                content=content,
                memory_type=memory_type,
                tags=tags,
                source="memory_tool",
            )
            return create_success_result(
                {"success": True, "memory_id": memory_id}
            )
        except Exception as e:
            logger.warning("[MemoryTool] 存储失败 | error=%s", e)
            return create_failure_result(f"存储失败: {str(e)}")

    async def _retrieve(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """检索记忆。"""
        backend = self._memory_backend
        query = inputs.get("query")
        top_k = inputs.get("top_k", 5)
        filter_ = inputs.get("filter", {}) or {}
        memory_type = inputs.get("memory_type") or filter_.get("memory_type")

        if not query:
            return create_failure_result("检索需要提供 query 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            results = await backend.search(
                query=query,
                user_id=user_id,
                top_k=top_k,
                memory_type=memory_type,
            )
            return create_success_result(
                {
                    "success": True,
                    "query": query,
                    "top_k": top_k,
                    "results": results or [],
                }
            )
        except Exception as e:
            logger.warning("[MemoryTool] 检索失败 | error=%s", e)
            return create_failure_result(f"检索失败: {str(e)}")

    async def _import_text(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """导入文本知识（name 作为知识标签）。"""
        backend = self._memory_backend
        content = inputs.get("content")
        name = inputs.get("name")

        if not content:
            return create_failure_result("缺少 content 参数")
        if not name:
            return create_failure_result("缺少 name 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            result = await backend.import_document(
                user_id=user_id,
                text=content,
                name=name,
            )
            return create_success_result({"success": True, **result})
        except Exception as e:
            logger.warning("[MemoryTool] 导入文本失败 | error=%s", e)
            return create_failure_result(f"导入文本失败: {str(e)}")

    async def _import_file(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """导入文件知识（name 缺省取文件名）。"""
        backend = self._memory_backend
        file_path = inputs.get("file_path")

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        name = inputs.get("name") or os.path.basename(str(file_path))
        try:
            user_id = self._resolve_user_id(inputs)
            result = await backend.import_document(
                user_id=user_id,
                file_path=str(file_path),
                name=name,
            )
            return create_success_result({"success": True, **result})
        except Exception as e:
            logger.warning("[MemoryTool] 导入文件失败 | error=%s", e)
            return create_failure_result(f"导入文件失败: {str(e)}")

    async def _update(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """更新记忆；后端不支持 update 时降级为 add（同内容），永不崩溃。

        降级逻辑：以相同 content/tags 调用 backend.add，返回新 memory_id，
        并标记 degraded=True。
        """
        backend = self._memory_backend
        content = inputs.get("content")
        tags = self._inject_agent_tags(inputs, list(inputs.get("tags", [])))

        if not content:
            return create_failure_result("更新记忆需要提供 content 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            # 原生 update 能力（在类上而非实例属性上探测，避免 mock 自动子属性误判）
            updater = getattr(type(backend), "update", None)
            if callable(updater):
                result = await updater(
                    backend,
                    user_id=user_id,
                    memory_id=self._extract_memory_id(
                        inputs.get("memory_id") or inputs.get("file_path")
                    ),
                    content=content,
                    tags=tags,
                )
                return create_success_result(
                    {"success": True, "memory_id": result, "updated": True}
                )

            # 降级：重新写入同内容记忆
            memory_id = await backend.add(
                user_id=user_id,
                content=content,
                memory_type="semantic",
                tags=tags,
                source="memory_tool:update",
            )
            logger.info(
                "[MemoryTool] update 降级为 add | new_memory_id=%s", memory_id
            )
            return create_success_result(
                {
                    "success": True,
                    "memory_id": memory_id,
                    "updated": True,
                    "degraded": True,
                }
            )
        except Exception as e:
            logger.warning("[MemoryTool] 更新失败 | error=%s", e)
            return create_failure_result(f"更新失败: {str(e)}")

    async def _delete(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """删除记忆。"""
        backend = self._memory_backend
        memory_id = self._extract_memory_id(
            inputs.get("memory_id") or inputs.get("file_path")
        )

        if not memory_id:
            return create_failure_result("缺少 memory_id 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            deleted = await backend.delete(user_id=user_id, memory_id=memory_id)
            return create_success_result(
                {"success": deleted, "deleted": deleted, "memory_id": memory_id}
            )
        except Exception as e:
            logger.warning("[MemoryTool] 删除失败 | error=%s", e)
            return create_failure_result(f"删除失败: {str(e)}")

    async def _get_context(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """获取会话上下文：以更宽泛的查询（更大 top_k，不过滤类型）检索记忆。"""
        backend = self._memory_backend
        query = inputs.get("query") or inputs.get("session_id") or ""
        top_k = inputs.get("top_k", 10)

        try:
            user_id = self._resolve_user_id(inputs)
            results = await backend.search(
                query=query,
                user_id=user_id,
                top_k=top_k,
            )
            return create_success_result(
                {
                    "success": True,
                    "top_k": top_k,
                    "results": results or [],
                }
            )
        except Exception as e:
            logger.warning("[MemoryTool] 获取上下文失败 | error=%s", e)
            return create_failure_result(f"获取上下文失败: {str(e)}")

    async def _list(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """列举记忆：以空查询宽泛检索（更大 top_k，可选按类型过滤）。"""
        backend = self._memory_backend
        filter_ = inputs.get("filter", {}) or {}
        memory_type = inputs.get("memory_type") or filter_.get("memory_type")
        limit = inputs.get("limit", inputs.get("top_k", 20))

        try:
            user_id = self._resolve_user_id(inputs)
            results = await backend.search(
                query="",
                user_id=user_id,
                top_k=limit,
                memory_type=memory_type,
            )
            return create_success_result(
                {
                    "success": True,
                    "limit": limit,
                    "results": results or [],
                    "count": len(results or []),
                }
            )
        except Exception as e:
            logger.warning("[MemoryTool] 列举失败 | error=%s", e)
            return create_failure_result(f"列举失败: {str(e)}")
