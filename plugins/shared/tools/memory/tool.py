"""记忆工具（0.2 重写版）。

0.1 的记忆服务（memory.service.MemoryService）已在 0.2 中删除，本模块改为
注入式 IMemoryBackend（见 plugins/shared/system/hindsight_memory/memory_backend.py
定义的端口：add / search / delete / import_document，全部 async）。
Tool / ToolExecutionResult / 枚举 / 结果工厂均从 ``agentos_plugin_sdk`` 导入
（与 download/tool.py 的导入清单对齐；SDK 字段覆盖本地版用法）。

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
- MemoryTool：记忆工具类
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from agentos_plugin_sdk import (
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


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
    等实现均可）；未注入时执行返回错误结果，不崩溃。

    IDOR 防护（punch B6）：
    - 敏感 action（store/import_text/import_file/update/delete——写/删用户数据）
      在 ``set_trusted_user_id`` 未注入服务端可信身份时**明确拒绝**，
      不再静默回退 ``inputs.user_id``（后者可被客户端伪造以越权读写他人记忆）。
    - 只读 action（retrieve/get_context/list）保留 ``inputs.user_id`` 回退，
      维持旧调用路径向后兼容。
    - 服务端入口（server.py 的 memory()）应按 bash 的 ``_owner_from_inputs``
      模式从内核注入参数（``_owner`` / ``session_id`` / ...）解析可信身份后
      调用 ``set_trusted_user_id``。
    """

    SYSTEM_USER_ID = "system"

    # 敏感（写/删用户数据）action：无可信身份注入时拒绝（IDOR 防护）
    SENSITIVE_ACTIONS = frozenset(
        {"store", "import_text", "import_file", "update", "delete"}
    )

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
        2. ``inputs["user_id"]``：**不可信**回退——仅只读 action 沿用（敏感
           action 在 execute 入口已被拒绝，见 SENSITIVE_ACTIONS），向后兼容。
        3. ``SYSTEM_USER_ID``：缺省系统态。

        Args:
            inputs: 工具输入

        Returns:
            用户隔离 key
        """
        if self._trusted_user_id:
            return self._trusted_user_id
        # 回退：无服务端可信注入时沿用 inputs（不可信；仅只读路径可达——
        # 敏感 action 已在 execute 入口按无身份拒绝）。
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
        """把 agent_config_id / session_id 自动注入为标签（与 0.1 行为对齐）。

        - agent_config_id：内容归属 Agent 标签
        - session_id：会话标签（``session:<id>``）——store 侧注入、search 侧
          按同标签过滤（filter.session_id 定向会话：会话是内容维度标签而非
          隔离键，隔离键始终是可信 caller 身份）

        Args:
            inputs: 工具输入（含 agent_config_id / session_id）
            tags: 原始标签列表（会被拷贝）

        Returns:
            注入后的标签列表
        """
        out = list(tags)
        agent_config_id = inputs.get("agent_config_id", "")
        if agent_config_id and agent_config_id not in out:
            out.append(agent_config_id)
        session_id = inputs.get("session_id") or ""
        if session_id:
            session_tag = f"session:{session_id}"
            if session_tag not in out:
                out.append(session_tag)
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
                    "document_id": {
                        "type": "string",
                        "description": "文档ID（store/update时使用，作为记忆的文档锚点，delete 时按此定向删除）",
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
                            "tags_match": {
                                "type": "string",
                                "enum": ["any", "all", "any_strict", "all_strict", "exact"],
                                "default": "any",
                                "description": "标签匹配模式（any=OR 含无标签 / all=AND 含无标签 / any_strict=OR 排除无标签 / all_strict=AND 排除无标签 / exact=集合相等）",
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

        # IDOR 防护（B6）：敏感（写/删用户数据）action 无服务端可信身份注入时
        # 明确拒绝——不再静默回退 inputs.user_id（客户端可伪造，越权写/删他人记忆）。
        if action in self.SENSITIVE_ACTIONS and not self._trusted_user_id:
            return create_failure_result(
                "缺少可信调用方身份：敏感操作（"
                + "/".join(sorted(self.SENSITIVE_ACTIONS))
                + "）需服务端注入 _owner/session_id 等会话身份"
                "（经 set_trusted_user_id），已拒绝以防止客户端伪造 user_id 越权（IDOR）"
            )

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
        if backend is None:
            return create_failure_result("memory backend 未注入")
        content = inputs.get("content")
        memory_type = inputs.get("memory_type", "semantic")
        tags = self._inject_agent_tags(inputs, list(inputs.get("tags", [])))

        if not content:
            return create_failure_result("缺少 content 参数")

        try:
            user_id = self._resolve_user_id(inputs)
            # 调用方自持 document_id 时原样落库并回传；缺省时自动生成——
            # 同步 retain 下无 document_id 的写入服务端不返回任何 id（operation_id
            # 恒 None），工具层会误判"写入未确认"。document_id 即 delete/update
            # 的定向锚点。
            doc_id = inputs.get("document_id") or f"mem-{uuid4().hex}"
            memory_id = await backend.add(
                user_id=user_id,
                content=content,
                memory_type=memory_type,
                tags=tags,
                source="memory_tool",
                document_id=doc_id,
            )
            if not memory_id:
                # 空 id = 后端未确认写入（降级/静默失败），不得报成功
                return create_failure_result(
                    "存储失败：记忆后端未返回 memory id（写入未确认，可能后端降级）"
                )
            return create_success_result(
                {"success": True, "memory_id": memory_id}
            )
        except Exception as e:
            logger.warning("[MemoryTool] 存储失败 | error=%s", e)
            return create_failure_result(f"存储失败: {str(e)}")

    async def _retrieve(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """检索记忆。

        filter 接线：tags 投服务端精确过滤（hindsight tags 面），session_id
        决定隔离 bank，knowledge_name 客户端过滤。
        """
        backend = self._memory_backend
        if backend is None:
            return create_failure_result("memory backend 未注入")
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
                tags=filter_.get("tags") or None,
                tags_match=filter_.get("tags_match") or "any",
                session_id=filter_.get("session_id") or None,
                knowledge_name=filter_.get("knowledge_name") or None,
            )
            return create_success_result(
                {
                    "success": True,
                    "query": query,
                    "top_k": top_k,
                    "filter": filter_,
                    "results": results or [],
                }
            )
        except Exception as e:
            logger.warning("[MemoryTool] 检索失败 | error=%s", e)
            return create_failure_result(f"检索失败: {str(e)}")

    async def _import_text(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """导入文本知识（name 作为知识标签）。"""
        backend = self._memory_backend
        if backend is None:
            return create_failure_result("memory backend 未注入")
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
        if backend is None:
            return create_failure_result("memory backend 未注入")
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

        降级逻辑：以相同 content/tags 调用 backend.add（memory_id 作为
        document_id 原样保留——删除式更新/定向覆盖的锚点），返回新 memory_id，
        并标记 degraded=True。
        """
        backend = self._memory_backend
        if backend is None:
            return create_failure_result("memory backend 未注入")
        content = inputs.get("content")
        tags = self._inject_agent_tags(inputs, list(inputs.get("tags", [])))
        target_id = self._extract_memory_id(
            inputs.get("memory_id") or inputs.get("file_path")
        )

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
                    memory_id=target_id,
                    content=content,
                    tags=tags,
                )
                return create_success_result(
                    {"success": True, "memory_id": result, "updated": True}
                )

            # 降级：重新写入同内容记忆（保留原 memory_id 作 document_id——
            # 同一 document 覆盖写入，旧记忆单元由服务端合并/替换语义处理）
            memory_id = await backend.add(
                user_id=user_id,
                content=content,
                memory_type="semantic",
                tags=tags,
                source="memory_tool:update",
                document_id=target_id or "",
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
        if backend is None:
            return create_failure_result("memory backend 未注入")
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
        if backend is None:
            return create_failure_result("memory backend 未注入")
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
        """列举记忆。

        list 的语义是「罗列」而非「语义检索」：用非空宽泛查询（服务端拒绝
        空 query），filter 接线（memory_type/tags/session_id 过滤）。
        """
        backend = self._memory_backend
        if backend is None:
            return create_failure_result("memory backend 未注入")
        filter_ = inputs.get("filter", {}) or {}
        memory_type = inputs.get("memory_type") or filter_.get("memory_type")
        tags = filter_.get("tags") or inputs.get("tags") or None
        limit = inputs.get("limit", inputs.get("top_k", 20))
        # 宽泛列举查询：类型/标签过滤足以定向，query 用通用词（服务端拒绝空 query）
        query = inputs.get("query") or "记忆"

        try:
            user_id = self._resolve_user_id(inputs)
            results = await backend.search(
                query=query,
                user_id=user_id,
                top_k=limit,
                memory_type=memory_type,
                tags=tags,
                tags_match=filter_.get("tags_match") or "any",
                session_id=filter_.get("session_id") or None,
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
