"""
记忆工具

暴露接口：
- memory_service(self)：memory_service功能
- set_tag_network(self, tag_network: Any)：set_tag_network功能
- set_knowledge_importer(self, knowledge_importer: Any)：set_knowledge_importer功能
- get_tool_definition() -> Tool：get_tool_definition功能
- MemoryTool：MemoryTool类
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.results import ToolExecutionResult
from memory.service import MemoryService
from memory.types import ContextRequest, Episode, Knowledge
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class MemoryTool:
    """
    记忆工具

    提供：
    - 存储情景记忆（任务执行记录）
    - 存储知识（支持向量化/文件/两者）
    - 检索记忆（支持向量/Tag/混合模式）
    - 获取会话上下文
    - 搜索相似 Tag
    - 导入文本知识
    - 导入文件知识
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        user_id: str | None = None,
        tag_network: Any | None = None,
        knowledge_importer: Any | None = None,
    ):
        """初始化记忆工具"""
        self._session = session
        self._memory_service = None
        self.user_id = user_id
        self.tag_network = tag_network
        self._knowledge_importer = knowledge_importer

    def _get_session(self, inputs: dict[str, Any]) -> AsyncSession:
        """从构造函数或注入参数获取 session"""
        if self._session:
            return self._session

        # 从注入的参数获取
        session = inputs.get("_session") or inputs.get("db_session")
        if session:
            return session

        # 从上下文变量获取（如果有的话）
        try:
            from db.connection import get_current_session

            current_session = get_current_session()
            if current_session:
                return current_session
        except (ImportError, AttributeError):
            pass

        # 如果都没有，抛出异常
        raise RuntimeError(
            "数据库会话未注入。请确保：\n"
            "1. 工具在 WebSocket 连接时正确注册（传入 session）\n"
            "2. 或者在工具执行时通过 injected_params 注入 _session"
        )

    @property
    def memory_service(self):
        """获取记忆服务（延迟初始化）"""
        if self._memory_service is None and self.session:
            self._memory_service = MemoryService(session=self.session)
        return self._memory_service

    def set_tag_network(self, tag_network: Any):
        """设置 Tag 网络检索器"""
        self.tag_network = tag_network

    def set_knowledge_importer(self, knowledge_importer: Any):
        """设置知识导入器"""
        self._knowledge_importer = knowledge_importer

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="memory",
            description="记忆工具：存储和检索知识、情景记忆，支持导入文本和文件知识",
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
                    "query": {
                        "type": "string",
                        "description": "检索查询（retrieve时使用）",
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
                    "inject_type": {
                        "type": "string",
                        "enum": ["full", "retrieval", "summary"],
                        "default": "retrieval",
                        "description": "注入方式（第二层决策）：full(全量注入)/retrieval(检索注入)/summary(摘要注入)",
                    },
                    "retrieval_method": {
                        "type": "string",
                        "enum": ["vector", "keyword", "tagwave"],
                        "default": "vector",
                        "description": "检索方法（第三层决策，仅 retrieval 注入方式时使用）：vector(向量检索)/keyword(关键词检索)/tagwave(浪潮算法)",
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
            injected_params=["session_id", "user_id", "_session"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行记忆操作"""
        # 获取数据库会话
        try:
            self.session = self._get_session(inputs)
        except RuntimeError as e:
            return create_failure_result(
                error=str(e),
                error_code="SESSION_NOT_INJECTED",
            )

        # 从注入参数获取 user_id（如果没有则保留构造函数中的值）
        if not self.user_id:
            self.user_id = inputs.get("user_id")

        action = inputs.get("action")

        if action == "store":
            return await self._store(inputs)
        elif action == "retrieve":
            return await self._retrieve(inputs)
        elif action == "import_text":
            return await self._import_text(inputs)
        elif action == "import_file":
            return await self._import_file(inputs)
        elif action == "update":
            return await self._update(inputs)
        elif action == "delete":
            return await self._delete(inputs)
        elif action == "get_context":
            return await self._get_context(inputs)
        else:
            return create_failure_result(f"未知操作: {action}")

    async def _store(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """存储记忆"""
        content = inputs.get("content")
        tags = inputs.get("tags", [])
        memory_type = inputs.get("memory_type", "semantic")

        if not content:
            return create_failure_result("缺少 content 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        try:
            if memory_type == "episode":
                episode = Episode(
                    user_id=uuid.UUID(self.user_id),
                    intent_text=content,
                    tags=tags,
                )
                result = await self.memory_service.store_episode(episode)
                return create_success_result({"success": True, "episode_id": result})
            else:
                knowledge = Knowledge(
                    user_id=uuid.UUID(self.user_id),
                    content=content,
                    source_type="manual",
                    extra_data={"tags": tags},
                )
                result = await self.memory_service.store_knowledge(knowledge)
                return create_success_result({"success": True, "knowledge_id": result})

        except Exception as e:
            return create_failure_result(f"存储失败: {str(e)}")

    async def _retrieve(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        检索记忆 - 三层决策模型

        决策流程：
        1. 第一层：筛选条件 - 缩小数据范围
        2. 第二层：注入方式 - 决定如何处理结果
        3. 第三层：检索方法 - 选择检索算法
        """
        query = inputs.get("query")
        top_k = inputs.get("top_k", 5)
        filter = inputs.get("filter", {})
        inject_type = inputs.get("inject_type", "retrieval")
        retrieval_method = inputs.get("retrieval_method", "vector")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        if inject_type == "retrieval" and not query:
            return create_failure_result("retrieval 注入方式需要提供 query")

        try:
            results = await self.memory_service.retrieve(
                user_id=uuid.UUID(self.user_id),
                filter=filter,
                inject_type=inject_type,
                retrieval_method=retrieval_method,
                query=query,
                top_k=top_k,
            )

            if not results:
                return create_success_result(
                    {
                        "success": True,
                        "inject_type": inject_type,
                        "retrieval_method": retrieval_method,
                        "filter": filter,
                        "results": [],
                    }
                )

            if inject_type == "summary":
                combined_content = "\n\n".join([r.content for r in results])
                summary = await self._generate_summary(combined_content)
                return create_success_result(
                    {
                        "success": True,
                        "inject_type": inject_type,
                        "retrieval_method": retrieval_method,
                        "filter": filter,
                        "summary": summary,
                        "source_count": len(results),
                    }
                )
            else:
                return create_success_result(
                    {
                        "success": True,
                        "inject_type": inject_type,
                        "retrieval_method": retrieval_method,
                        "filter": filter,
                        "results": [
                            {
                                "id": str(r.id),
                                "content": r.content,
                                "score": r.score,
                                "metadata": r.metadata,
                            }
                            for r in results
                        ],
                    }
                )

        except Exception as e:
            return create_failure_result(f"检索失败: {str(e)}")

    async def _generate_summary(self, content: str) -> str:
        """生成内容摘要"""
        if len(content) <= 500:
            return content
        return content[:500] + "..."

    async def _import_text(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """导入文本知识"""
        content = inputs.get("content")
        name = inputs.get("name")
        tags = inputs.get("tags", [])

        if not content:
            return create_failure_result("缺少 content 参数")

        if not name:
            return create_failure_result("缺少 name 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        if not self._knowledge_importer:
            return create_failure_result("知识导入器未初始化")

        try:
            result = await self._knowledge_importer.import_text(
                content=content,
                name=name,
                user_id=self.user_id,
                tags=tags,
            )

            if result.success:
                return create_success_result(
                    {
                        "success": True,
                        "knowledge_id": result.knowledge_id,
                        "file_path": result.file_path,
                    }
                )
            else:
                return create_failure_result(result.error or "导入失败")

        except Exception as e:
            return create_failure_result(f"导入文本失败: {str(e)}")

    async def _import_file(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """导入文件知识"""
        file_path = inputs.get("file_path")
        tags = inputs.get("tags", [])

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        if not self._knowledge_importer:
            return create_failure_result("知识导入器未初始化")

        try:
            result = await self._knowledge_importer.import_file(
                source_path=file_path,
                user_id=self.user_id,
                tags=tags,
            )

            if result.success:
                return create_success_result(
                    {
                        "success": True,
                        "knowledge_id": result.knowledge_id,
                        "file_path": result.file_path,
                    }
                )
            else:
                return create_failure_result(result.error or "导入失败")

        except Exception as e:
            return create_failure_result(f"导入文件失败: {str(e)}")

    async def _update(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """更新知识"""
        file_path = inputs.get("file_path")
        new_content = inputs.get("content")
        new_tags = inputs.get("tags")

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        if not self._knowledge_importer:
            return create_failure_result("知识导入器未初始化")

        try:
            result = await self._knowledge_importer.update_knowledge(
                file_path=file_path,
                user_id=self.user_id,
                new_content=new_content,
                new_tags=new_tags,
            )

            if result.success:
                return create_success_result(
                    {
                        "success": True,
                        "knowledge_id": result.knowledge_id,
                        "file_path": result.file_path,
                    }
                )
            else:
                return create_failure_result(result.error or "更新失败")

        except Exception as e:
            return create_failure_result(f"更新失败: {str(e)}")

    async def _delete(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """删除知识"""
        file_path = inputs.get("file_path")
        delete_file = inputs.get("delete_file", True)

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        if not self._knowledge_importer:
            return create_failure_result("知识导入器未初始化")

        try:
            success = await self._knowledge_importer.delete_knowledge(
                file_path=file_path,
                user_id=self.user_id,
                delete_file=delete_file,
            )

            return create_success_result({"success": success})

        except Exception as e:
            return create_failure_result(f"删除失败: {str(e)}")

    async def _get_context(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """获取会话上下文"""
        session_id = inputs.get("session_id")

        if not session_id:
            return create_failure_result("缺少 session_id 参数")

        if not self.user_id:
            return create_failure_result("缺少用户ID")

        try:
            request = ContextRequest(
                user_id=uuid.UUID(self.user_id),
                session_id=session_id,
            )
            context = await self.memory_service.get_context(request)

            return create_success_result(
                {
                    "success": True,
                    "context": context,
                }
            )

        except Exception as e:
            return create_failure_result(f"获取上下文失败: {str(e)}")
