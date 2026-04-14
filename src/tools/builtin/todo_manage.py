"""
TODO 清单管理工具

暴露接口：
- get(self, session_id: str, agent_id: str | None) -> list[dict[str, Any]]：get功能
- set(self, session_id: str, agent_id: str | None, todos: list[dict[str, Any]]) -> None：set功能
- get_tool_definition() -> Tool：get_tool_definition功能
- TodoStore：TodoStore类
- TodoManageTool：TodoManageTool类
"""

from typing import Any

from core.results import ToolExecutionResult
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class TodoStore:
    """TODO 清单存储（内存单例）"""
    _instance = None
    _todos: dict[str, list[dict[str, Any]]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _key(self, session_id: str, agent_id: str | None) -> str:
        return f"{session_id}:{agent_id or 'default'}"

    def get(self, session_id: str, agent_id: str | None) -> list[dict[str, Any]]:
        return self._todos.get(self._key(session_id, agent_id), [])

    def set(self, session_id: str, agent_id: str | None, todos: list[dict[str, Any]]) -> None:
        self._todos[self._key(session_id, agent_id)] = todos


_store = TodoStore()


class TodoManageTool:
    """TODO 清单管理工具"""

    @staticmethod
    def get_tool_definition() -> Tool:
        from tools.types import ToolLevel

        return Tool(
            name="todo_manage",
            description=(
                "TODO 清单管理工具。注意：items 参数必须是字符串数组，如 [\"步骤1\", \"步骤2\"]，"
                "不要传对象数组。操作：write(创建清单)、read(读取清单)、update(更新状态)。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "read", "update"],
                        "description": "操作类型",
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "TODO 项列表，必须是纯字符串数组，如 [\"步骤1\", \"步骤2\"]（仅 write 时使用）",
                    },
                    "index": {
                        "type": "integer",
                        "description": "TODO 项索引（update 时使用，从 0 开始）",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "状态（update 时使用）",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            dangerous_operations=[],
            tags=["todo", "checklist"],
            injected_params=["session_id", "agent_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        action = inputs.get("action")
        session_id = inputs.get("session_id")
        agent_id = inputs.get("agent_id")

        if not session_id:
            return create_failure_result(error="缺少 session_id", error_code="MISSING_SESSION_ID")

        if action == "write":
            items = inputs.get("items", [])
            if not items:
                return create_failure_result(error="items 不能为空", error_code="EMPTY_ITEMS")
            todos = [{"content": item, "status": "pending"} for item in items]
            _store.set(session_id, agent_id, todos)
            return create_success_result(data={
                "message": f"✅ 已创建 {len(todos)} 个待办项",
                "total": len(todos)
            })

        elif action == "read":
            todos = _store.get(session_id, agent_id)
            if not todos:
                return create_success_result(data={"message": "📋 暂无待办项"})
            status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "failed": "❌"}
            items = [{"content": t.get("content", ""), "status": status_emoji.get(t.get("status"), t.get("status"))} for t in todos]
            return create_success_result(data={
                "message": f"📋 待办清单 ({len(todos)} 项)",
                "items": items
            })

        elif action == "update":
            idx = inputs.get("index")
            status = inputs.get("status")
            if idx is None:
                return create_failure_result(error="缺少 index", error_code="MISSING_INDEX")
            if not status:
                return create_failure_result(error="缺少 status", error_code="MISSING_STATUS")
            todos = _store.get(session_id, agent_id)
            if not todos or idx < 0 or idx >= len(todos):
                return create_failure_result(error=f"索引无效: {idx}", error_code="INVALID_INDEX")
            old_status = todos[idx]["status"]
            todos[idx]["status"] = status
            _store.set(session_id, agent_id, todos)
            status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(status, status)
            return create_success_result(data={
                "message": f"{status_emoji} 第 {idx + 1} 项: {old_status} → {status}",
                "index": idx,
                "status": status
            })

        return create_failure_result(error=f"不支持的操作: {action}", error_code="INVALID_ACTION")
