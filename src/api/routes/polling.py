"""
轮询机制 API 端点

提供基于 HTTP 轮询的任务结果查询接口
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.agents.polling_system import TaskResult, polling_task_manager
from src.auth.dependencies import get_current_user
from src.core.states import ExecutionStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/polling", tags=["轮询"])


class TaskStatusResponse(BaseModel):
    """任务状态响应"""

    task_id: str
    status: str
    progress_percentage: float
    current_step: str
    step_index: int
    total_steps: int
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    is_finished: bool


class TaskResultResponse(BaseModel):
    """任务结果响应"""

    task_id: str
    status: str
    progress_percentage: float
    current_step: str
    result: dict | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    metadata: dict


class TaskListResponse(BaseModel):
    """任务列表响应"""

    tasks: list[TaskStatusResponse]
    total: int


def _task_to_status_response(task: TaskResult) -> TaskStatusResponse:
    """转换任务结果为状态响应"""
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status.value,
        progress_percentage=task.progress_percentage,
        current_step=task.current_step,
        step_index=task.step_index,
        total_steps=task.total_steps,
        created_at=task.created_at.isoformat(),
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        duration_seconds=task.duration_seconds,
        is_finished=task.is_finished,
    )


def _task_to_result_response(task: TaskResult) -> TaskResultResponse:
    """转换任务结果为完整响应"""
    return TaskResultResponse(
        task_id=task.task_id,
        status=task.status.value,
        progress_percentage=task.progress_percentage,
        current_step=task.current_step,
        result=task.result,
        error=task.error,
        created_at=task.created_at.isoformat(),
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        duration_seconds=task.duration_seconds,
        metadata=task.metadata,
    )


@router.get("/tasks/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, current_user=Depends(get_current_user)):
    """
    获取任务状态（轻量级，只返回状态和进度信息）

    适用于高频轮询场景
    """
    task = polling_task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return _task_to_status_response(task)


@router.get("/tasks/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(task_id: str, current_user=Depends(get_current_user)):
    """
    获取任务完整结果（包含执行结果和错误信息）

    适用于获取最终结果
    """
    task = polling_task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return _task_to_result_response(task)


@router.get("/tasks/{task_id}/wait")
async def wait_for_task(
    task_id: str,
    timeout: int = Query(default=60, ge=1, le=300, description="等待超时时间（秒）"),
    current_user=Depends(get_current_user),
):
    """
    等待任务完成（长轮询）

    会阻塞直到任务完成或超时，减少客户端轮询频率
    """

    from src.agents.polling_system import PollingClient

    client = PollingClient(polling_task_manager)

    try:
        task = await client.wait_for_completion(task_id, timeout=timeout)
        return _task_to_result_response(task)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    status: str | None = Query(default=None, description="按状态过滤"),
    limit: int = Query(default=50, ge=1, le=100, description="返回数量限制"),
    current_user=Depends(get_current_user),
):
    """
    列出任务

    支持按状态过滤和分页
    """
    status_filter = None
    if status:
        try:
            status_filter = ExecutionStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的状态值: {status}")

    tasks = polling_task_manager.list_tasks(status_filter)
    tasks = tasks[:limit]  # 简单分页

    return TaskListResponse(
        tasks=[_task_to_status_response(task) for task in tasks], total=len(tasks)
    )


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str, current_user=Depends(get_current_user)):
    """
    取消任务
    """
    success = polling_task_manager.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在或无法取消")

    return {"message": "任务已取消", "task_id": task_id}


# 客户端使用示例
CLIENT_EXAMPLE = """
# Python 客户端轮询示例
import asyncio
import aiohttp
import time

class PollingTaskClient:
    def __init__(self, base_url: str, auth_token: str):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {auth_token}"}

    async def poll_task_status(self, task_id: str, interval: float = 2.0, timeout: float = 300.0):
        \"\"\"轮询任务状态直到完成\"\"\"
        start_time = time.time()

        async with aiohttp.ClientSession(headers=self.headers) as session:
            while True:
                # 检查超时
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"轮询超时: {task_id}")

                # 获取任务状态
                async with session.get(f"{self.base_url}/api/polling/tasks/{task_id}/status") as resp:
                    if resp.status == 404:
                        raise ValueError(f"任务不存在: {task_id}")

                    data = await resp.json()
                    logger.debug(f"任务进度: {data['progress_percentage']:.1f}% - {data['current_step']}")

                    # 检查是否完成
                    if data['is_finished']:
                        # 获取完整结果
                        async with session.get(f"{self.base_url}/api/polling/tasks/{task_id}/result") as result_resp:
                            return await result_resp.json()

                # 等待下次轮询
                await asyncio.sleep(interval)

    async def wait_for_task(self, task_id: str, timeout: int = 60):
        \"\"\"使用长轮询等待任务完成\"\"\"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(
                f"{self.base_url}/api/polling/tasks/{task_id}/wait",
                params={"timeout": timeout}
            ) as resp:
                if resp.status == 408:
                    raise TimeoutError("任务执行超时")
                elif resp.status == 404:
                    raise ValueError("任务不存在")

                return await resp.json()

# 使用示例
async def main():
    client = PollingTaskClient("http://localhost:8888", "your-auth-token")

    # 方式1: 主动轮询
    try:
        result = await client.poll_task_status("task-123", interval=1.0, timeout=120.0)
        print("任务完成:", result)
    except TimeoutError:
        logger.info("任务执行超时")

    # 方式2: 长轮询
    try:
        result = await client.wait_for_task("task-456", timeout=60)
        print("任务完成:", result)
    except TimeoutError:
        logger.info("等待超时")
"""


# JavaScript 客户端示例
# 注意：以下是 JavaScript 代码示例，不是 Python 代码
# class PollingTaskClient {
#     constructor(baseUrl, authToken) {
#         this.baseUrl = baseUrl;
#         this.headers = {
#             'Authorization': `Bearer ${authToken}`,
#             'Content-Type': 'application/json'
#         };
#     }
#
#     async pollTaskStatus(taskId, interval = 2000, timeout = 300000) {
#         const startTime = Date.now();
#
#         while (true) {
#             // 检查超时
#             if (Date.now() - startTime > timeout) {
#                 throw new Error(`轮询超时: ${taskId}`);
#             }
#
#             // 获取任务状态
#             const response = await fetch(`${this.baseUrl}/api/polling/tasks/${taskId}/status`, {
#                 headers: this.headers
#             });
#
#             if (response.status === 404) {
#                 throw new Error(`任务不存在: ${taskId}`);
#             }
#
#             const data = await response.json();
#             console.log(`任务进度: ${data.progress_percentage.toFixed(1)}% - ${data.current_step}`);
#
#             // 检查是否完成
#             if (data.is_finished) {
#                 // 获取完整结果
#                 const resultResponse = await fetch(`${this.baseUrl}/api/polling/tasks/${taskId}/result`, {
#                     headers: this.headers
#                 });
#                 return await resultResponse.json();
#             }
#
#             // 等待下次轮询
#             await new Promise(resolve => setTimeout(resolve, interval));
#         }
#     }
#
#     async waitForTask(taskId, timeout = 60) {
#         const response = await fetch(`${this.baseUrl}/api/polling/tasks/${taskId}/wait?timeout=${timeout}`, {
#             headers: this.headers
#         });
#
#         if (response.status === 408) {
#             throw new Error('任务执行超时');
#         } else if (response.status === 404) {
#             throw new Error('任务不存在');
#         }
#
#         return await response.json();
#     }
# }
