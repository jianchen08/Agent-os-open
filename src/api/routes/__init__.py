"""
API 路由模块

消息功能已整合到 Session 的 ExecutionRecord 中
"""

from fastapi import APIRouter

from src.api.routes.agent_calls import router as agent_calls_router
from src.api.routes.agents import router as agents_router
from src.api.routes.auth import router as auth_router
from src.api.routes.config import router as config_router
from src.api.routes.cost_control import router as cost_control_router
from src.api.routes.evaluation_metrics import router as evaluation_metrics_router
from src.api.routes.execution import router as execution_router
from src.api.routes.execution_debug import router as execution_debug_router
from src.api.routes.floating_chat import router as floating_chat_router
from src.api.routes.health import router as health_router
from src.api.routes.memory import router as memory_router
from src.api.routes.monitoring import router as monitoring_router
from src.api.routes.task_evaluation import router as task_evaluation_router
from src.api.routes.tasks import router as tasks_router
from src.api.routes.thinking_mode import router as thinking_mode_router
from src.api.routes.threads import router as threads_router
from src.api.routes.threads import _create_thread_internal
from src.api.routes.tokens import router as tokens_router
from src.api.routes.tools import router as tools_router
from src.api.routes.triggers import router as triggers_router
from src.api.routes.users import router as users_router
from src.api.routes.websocket_types import router as websocket_types_router
from src.api.routes.workflows import router as workflows_router

# 创建 v1 版本路由
v1_router = APIRouter(prefix="/api/v1")

# 注册子路由
v1_router.include_router(auth_router, prefix="/auth", tags=["认证"])
v1_router.include_router(health_router, prefix="/health", tags=["健康检查"])
v1_router.include_router(threads_router, prefix="/threads", tags=["线程/会话"])
# 先注册调试路由，确保 /records 在 /records/{record_id} 之前匹配
v1_router.include_router(execution_debug_router, prefix="/execution", tags=["执行记录调试"])
v1_router.include_router(execution_router, prefix="/execution", tags=["执行控制"])
v1_router.include_router(agents_router, prefix="/agents", tags=["Agent"])
v1_router.include_router(
    agent_calls_router, prefix="/agent-calls", tags=["Agent调用记录"]
)
v1_router.include_router(tools_router, prefix="/tools", tags=["工具"])
v1_router.include_router(workflows_router, prefix="/workflows", tags=["工作流"])
v1_router.include_router(memory_router, prefix="/memory", tags=["记忆"])
v1_router.include_router(config_router, prefix="/config", tags=["配置"])
v1_router.include_router(users_router, prefix="/users", tags=["用户管理"])
v1_router.include_router(monitoring_router, prefix="/monitoring", tags=["监控"])
v1_router.include_router(cost_control_router, prefix="/cost-control", tags=["成本控制"])
v1_router.include_router(thinking_mode_router, tags=["思考模式"])
v1_router.include_router(tasks_router, prefix="/tasks", tags=["任务管理"])
v1_router.include_router(
    task_evaluation_router, prefix="/task-evaluation", tags=["任务评估"]
)
# 新增：评估指标 API
v1_router.include_router(evaluation_metrics_router, tags=["评估指标"])
v1_router.include_router(triggers_router, tags=["触发器"])
v1_router.include_router(websocket_types_router, tags=["WebSocket 类型"])
v1_router.include_router(floating_chat_router, tags=["悬浮窗"])
v1_router.include_router(tokens_router, tags=["Token计算"])

__all__ = [
    "v1_router",
    "agent_calls_router",
    "auth_router",
    "health_router",
    "threads_router",
    "execution_router",
    "agents_router",
    "tools_router",
    "workflows_router",
    "memory_router",
    "config_router",
    "users_router",
    "monitoring_router",
    "cost_control_router",
    "thinking_mode_router",
    "tasks_router",
    "task_evaluation_router",
    "evaluation_metrics_router",
    "triggers_router",
    "websocket_types_router",
]
