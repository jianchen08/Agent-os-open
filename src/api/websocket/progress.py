"""
WebSocket 进度推送 API 端点

提供任务执行进度的实时 WebSocket 推送
"""

import json
import logging

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRouter

from src.agents.websocket_progress import websocket_progress_manager
from src.auth.dependencies import get_current_user_websocket

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/progress")
async def websocket_progress_endpoint(websocket: WebSocket, token: str | None = None):
    """
    WebSocket 进度推送端点

    连接后会接收任务执行的实时进度更新

    消息格式:
    {
        "type": "task_progress",
        "event": {
            "event_type": "task_started|task_progress|task_completed|...",
            "task_id": "task_uuid",
            "timestamp": "2024-01-01T12:00:00",
            "data": {...},
            "message": "进度描述"
        }
    }
    """
    await websocket.accept()

    user_id = None
    try:
        # 验证用户身份（通过 token 参数或其他方式）
        if token:
            # 这里应该验证 token 并获取 user_id
            # user = await verify_websocket_token(token)
            # user_id = user.id
            user_id = "default_user"  # 临时处理
        else:
            user_id = "anonymous"

        # 注册连接
        await websocket_progress_manager.connect_user(user_id, websocket)

        # 发送连接确认
        await websocket.send_text(
            json.dumps(
                {
                    "type": "connection_established",
                    "user_id": user_id,
                    "message": "WebSocket 连接已建立",
                },
                ensure_ascii=False,
            )
        )

        # 保持连接并处理客户端消息
        while True:
            try:
                # 接收客户端消息（如心跳、订阅特定任务等）
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "ping":
                    # 心跳响应
                    await websocket.send_text(
                        json.dumps(
                            {"type": "pong", "timestamp": message.get("timestamp")}
                        )
                    )
                elif message.get("type") == "subscribe_task":
                    # 订阅特定任务（可以实现任务级别的订阅）
                    task_id = message.get("task_id")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "subscribed",
                                "task_id": task_id,
                                "message": f"已订阅任务 {task_id} 的进度更新",
                            },
                            ensure_ascii=False,
                        )
                    )

            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps(
                        {"type": "error", "message": "无效的 JSON 格式"},
                        ensure_ascii=False,
                    )
                )
            except Exception as e:
                logger.error(f"[WebSocket] 处理消息错误: {e}")
                await websocket.send_text(
                    json.dumps(
                        {"type": "error", "message": f"处理消息时发生错误: {str(e)}"},
                        ensure_ascii=False,
                    )
                )

    except WebSocketDisconnect:
        logger.info(f"[WebSocket] 用户 {user_id} 主动断开连接")
    except Exception as e:
        logger.error(f"[WebSocket] 连接错误: {e}")
    finally:
        # 清理连接
        if user_id:
            await websocket_progress_manager.disconnect_user(user_id, websocket)


@router.get("/progress/{task_id}")
async def get_task_progress(
    task_id: str, current_user=Depends(get_current_user_websocket)
):
    """
    获取任务当前进度（HTTP 接口，用于不支持 WebSocket 的场景）

    Returns:
        任务进度信息
    """
    tracker = websocket_progress_manager.get_tracker(task_id)
    if not tracker:
        raise HTTPException(status_code=404, detail="任务不存在或已完成")

    return {
        "task_id": task_id,
        "current_step": tracker.current_step,
        "total_steps": tracker.total_steps,
        "progress_percentage": (
            (tracker.current_step / tracker.total_steps * 100)
            if tracker.total_steps > 0
            else 0
        ),
        "is_completed": tracker.is_completed,
        "start_time": tracker.start_time.isoformat(),
    }


# 前端 JavaScript 示例
FRONTEND_EXAMPLE = """
// 前端 WebSocket 连接示例
class TaskProgressClient {
    constructor(token) {
        this.token = token;
        this.ws = null;
        this.callbacks = {};
    }

    connect() {
        const wsUrl = `ws://localhost:8888/ws/progress?token=${this.token}`;
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket 连接已建立');
            // 发送心跳
            setInterval(() => {
                this.ping();
            }, 30000);
        };

        this.ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            this.handleMessage(message);
        };

        this.ws.onclose = () => {
            console.log('WebSocket 连接已关闭');
            // 自动重连
            setTimeout(() => this.connect(), 5000);
        };
    }

    handleMessage(message) {
        switch(message.type) {
            case 'task_progress':
                this.onTaskProgress(message.event);
                break;
            case 'connection_established':
                console.log('连接确认:', message.message);
                break;
            case 'pong':
                console.log('心跳响应');
                break;
        }
    }

    onTaskProgress(event) {
        console.log('任务进度:', event);

        // 更新 UI
        const progressBar = document.getElementById('progress-bar');
        if (progressBar && event.progress_percentage) {
            progressBar.style.width = event.progress_percentage + '%';
        }

        const statusText = document.getElementById('status-text');
        if (statusText) {
            statusText.textContent = event.message;
        }

        // 触发自定义回调
        if (this.callbacks[event.event_type]) {
            this.callbacks[event.event_type](event);
        }
    }

    subscribeTask(taskId) {
        this.send({
            type: 'subscribe_task',
            task_id: taskId
        });
    }

    ping() {
        this.send({
            type: 'ping',
            timestamp: new Date().toISOString()
        });
    }

    send(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    on(eventType, callback) {
        this.callbacks[eventType] = callback;
    }
}

// 使用示例
const progressClient = new TaskProgressClient('your-auth-token');
progressClient.connect();

// 监听特定事件
progressClient.on('task_started', (event) => {
    console.log('任务开始:', event);
});

progressClient.on('task_completed', (event) => {
    console.log('任务完成:', event);
    alert('任务执行完成！');
});
"""
