"""
记忆工具

暴露接口：
- memory_service(self)：memory_service功能
- set_tag_network(self, tag_network: Any)：set_tag_network功能
- set_knowledge_importer(self, knowledge_importer: Any)：set_knowledge_importer功能
- get_tool_definition() -> Tool：get_tool_definition功能
- MemoryTool：MemoryTool类
"""

import logging
from typing import Any

from core.results import ToolExecutionResult
from memory.types import Episode, Knowledge
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class MemoryTool(BuiltinTool):
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

    数据库会话按需获取：仅向量检索等需要 DB 的操作才要求 session，
    纯文件存储/检索操作通过注入的 _memory_service 工作，不依赖数据库。
    """

    SYSTEM_USER_ID = "system"

    def __init__(
        self,
        session: Any | None = None,
        tag_network: Any | None = None,
        knowledge_importer: Any | None = None,
    ):
        """初始化记忆工具"""
        self._session = session
        self._memory_service = None
        self.tag_network = tag_network
        self._knowledge_importer = knowledge_importer

    def _get_memory_service(self, inputs: dict[str, Any]):
        """
        获取记忆服务实例

        优先级：
        1. 从注入参数获取（ToolCore 通过 _SERVICE_INJECT_MAP 注入）
        2. 使用已缓存的实例
        3. 降级：创建空壳 MemoryService（无持久化存储，仅内存字典，重启数据丢失）
        """
        # 第一优先级：从注入参数获取（ToolCore 通过 _SERVICE_INJECT_MAP 注入）
        injected = inputs.get("_memory_service")
        if injected is not None:
            self._memory_service = injected
            return injected

        # 第二优先级：使用已缓存的实例
        if self._memory_service is not None:
            return self._memory_service

        # 降级：创建空壳 MemoryService（无存储，仅内存字典）
        from memory.service import MemoryService  # noqa: PLC0415

        self._memory_service = MemoryService()
        logger.warning("[MemoryTool] memory_service 未注入，使用内存降级模式（重启数据丢失）")
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
            injected_params=["session_id", "_session", "_memory_service", "agent_config_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """执行记忆操作"""
        ms = self._get_memory_service(inputs)
        if ms is None:
            return create_failure_result(
                error="记忆服务未初始化。请确保 memory_service 已注册到管道共享服务中。",
                error_code="MEMORY_SERVICE_NOT_AVAILABLE",
            )

        self._memory_service = ms

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
        return create_failure_result(f"未知操作: {action}")

    async def _store(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """存储记忆，自动将 agent_config_id 注入为标签"""
        content = inputs.get("content")
        tags = list(inputs.get("tags", []))
        memory_type = inputs.get("memory_type", "semantic")
        agent_config_id = inputs.get("agent_config_id", "")

        if agent_config_id and agent_config_id not in tags:
            tags.append(agent_config_id)

        if not content:
            return create_failure_result("缺少 content 参数")

        try:
            if memory_type == "episode":
                episode = Episode(
                    user_id=self.SYSTEM_USER_ID,
                    intent_text=content,
                    tags=tags,
                )
                result = await self._memory_service.store_episode(episode)
                return create_success_result({"success": True, "episode_id": result})
            knowledge = Knowledge(
                user_id=self.SYSTEM_USER_ID,
                content=content,
                source_type="manual",
                extra_data={"tags": tags},
            )
            result = await self._memory_service.store_knowledge(knowledge)
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

        if inject_type == "retrieval" and not query:
            return create_failure_result("retrieval 注入方式需要提供 query")

        try:
            results = await self._memory_service.retrieve(
                user_id=self.SYSTEM_USER_ID,
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

    async def _import_text(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """导入文本知识，自动将 agent_config_id 注入为标签"""
        content = inputs.get("content")
        name = inputs.get("name")
        tags = list(inputs.get("tags", []))
        agent_config_id = inputs.get("agent_config_id", "")

        if agent_config_id and agent_config_id not in tags:
            tags.append(agent_config_id)

        if not content:
            return create_failure_result("缺少 content 参数")

        if not name:
            return create_failure_result("缺少 name 参数")

        if self._knowledge_importer:
            try:
                result = await self._knowledge_importer.import_text(
                    content=content,
                    name=name,
                    user_id=self.SYSTEM_USER_ID,
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
                return create_failure_result(result.error or "导入失败")
            except Exception as e:
                return create_failure_result(f"导入文本失败: {str(e)}")

        # 降级：使用 MemoryService 直接存储知识
        try:
            ms = self._get_memory_service(inputs)
            knowledge = Knowledge(
                user_id=self.SYSTEM_USER_ID,
                content=content,
                source_type="text_import",
                extra_data={"name": name, "tags": tags},
            )
            knowledge_id = await ms.store_knowledge(knowledge)
            logger.info(
                "[MemoryTool] import_text 降级存储成功 | name=%s | knowledge_id=%s",
                name,
                knowledge_id,
            )
            return create_success_result(
                {
                    "success": True,
                    "knowledge_id": knowledge_id,
                    "file_path": f"memory://{knowledge_id}",
                }
            )
        except Exception as e:
            return create_failure_result(f"导入文本失败（MemoryService降级）: {str(e)}")

    async def _import_file(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """导入文件知识，自动将 agent_config_id 注入为标签"""
        file_path = inputs.get("file_path")
        tags = list(inputs.get("tags", []))
        agent_config_id = inputs.get("agent_config_id", "")

        if agent_config_id and agent_config_id not in tags:
            tags.append(agent_config_id)

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if self._knowledge_importer:
            try:
                result = await self._knowledge_importer.import_file(
                    source_path=file_path,
                    user_id=self.SYSTEM_USER_ID,
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
                return create_failure_result(result.error or "导入失败")
            except Exception as e:
                return create_failure_result(f"导入文件失败: {str(e)}")

        # 降级：读取文件内容后用 MemoryService 存储
        import os as _os  # noqa: PLC0415

        if not _os.path.exists(file_path):  # noqa: PTH110
            return create_failure_result(f"文件不存在: {file_path}")
        try:
            with open(file_path, encoding="utf-8") as f:
                file_content = f.read()
        except Exception as e:
            return create_failure_result(f"读取文件失败: {str(e)}")

        try:
            ms = self._get_memory_service(inputs)
            knowledge = Knowledge(
                user_id=self.SYSTEM_USER_ID,
                content=file_content,
                source_type="file_import",
                extra_data={
                    "name": _os.path.basename(file_path),  # noqa: PTH119
                    "tags": tags,
                    "source_file": file_path,
                },
            )
            knowledge_id = await ms.store_knowledge(knowledge)
            logger.info(
                "[MemoryTool] import_file 降级存储成功 | file=%s | knowledge_id=%s",
                file_path,
                knowledge_id,
            )
            return create_success_result(
                {
                    "success": True,
                    "knowledge_id": knowledge_id,
                    "file_path": file_path,
                }
            )
        except Exception as e:
            return create_failure_result(f"导入文件失败（MemoryService降级）: {str(e)}")

    async def _update(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """更新知识"""
        file_path = inputs.get("file_path")
        new_content = inputs.get("content")
        new_tags = inputs.get("tags")

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if self._knowledge_importer:
            try:
                result = await self._knowledge_importer.update_knowledge(
                    file_path=file_path,
                    user_id=self.SYSTEM_USER_ID,
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
                return create_failure_result(result.error or "更新失败")
            except Exception as e:
                return create_failure_result(f"更新失败: {str(e)}")

        # 降级：从 file_path 提取 knowledge_id，删除旧知识 + 存储新知识
        knowledge_id_raw = file_path.removeprefix("memory://") if file_path.startswith("memory://") else file_path
        if not new_content:
            return create_failure_result("更新知识需要提供 content 参数")

        try:
            ms = self._get_memory_service(inputs)
            # 尝试删除旧知识（忽略删除失败，可能是新知识）
            await ms.delete_knowledge(knowledge_id_raw, self.SYSTEM_USER_ID)
            # 存储新知识
            knowledge = Knowledge(
                user_id=self.SYSTEM_USER_ID,
                content=new_content,
                source_type="manual",
                extra_data={"tags": new_tags or [], "updated_from": file_path},
            )
            knowledge_id = await ms.store_knowledge(knowledge)
            logger.info(
                "[MemoryTool] update 降级成功 | old_id=%s | new_knowledge_id=%s",
                knowledge_id_raw,
                knowledge_id,
            )
            return create_success_result(
                {
                    "success": True,
                    "knowledge_id": knowledge_id,
                    "file_path": file_path,
                }
            )
        except Exception as e:
            return create_failure_result(f"更新失败（MemoryService降级）: {str(e)}")

    async def _delete(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """删除知识"""
        file_path = inputs.get("file_path")
        delete_file = inputs.get("delete_file", True)

        if not file_path:
            return create_failure_result("缺少 file_path 参数")

        if self._knowledge_importer:
            try:
                success = await self._knowledge_importer.delete_knowledge(
                    file_path=file_path,
                    user_id=self.SYSTEM_USER_ID,
                    delete_file=delete_file,
                )
                return create_success_result({"success": success})
            except Exception as e:
                return create_failure_result(f"删除失败: {str(e)}")

        # 降级：从 file_path 提取 knowledge_id，调用 MemoryService
        # file_path 格式可能是 "memory://<id>" 或直接就是 knowledge_id
        knowledge_id = file_path.removeprefix("memory://") if file_path.startswith("memory://") else file_path
        try:
            ms = self._get_memory_service(inputs)
            success = await ms.delete_knowledge(knowledge_id, self.SYSTEM_USER_ID)
            logger.info(
                "[MemoryTool] delete 降级成功 | knowledge_id=%s | deleted=%s",
                knowledge_id,
                success,
            )
            return create_success_result({"success": success})
        except Exception as e:
            return create_failure_result(f"删除失败（MemoryService降级）: {str(e)}")

    async def _get_context(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """获取记忆统计信息"""
        try:
            stats = await self._memory_service.get_stats(self.SYSTEM_USER_ID)

            return create_success_result(
                {
                    "success": True,
                    "stats": stats,
                }
            )

        except Exception as e:
            return create_failure_result(f"获取统计信息失败: {str(e)}")
