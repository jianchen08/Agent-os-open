"""
悬浮窗启动 API
提供启动桌面悬浮窗应用的接口
"""

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/floating-chat", tags=["floating-chat"])


class FloatingChatStatus(BaseModel):
    """悬浮窗状态"""

    available: bool
    executable_path: str | None = None
    message: str


class LaunchRequest(BaseModel):
    """启动请求"""

    session_id: str | None = None
    token: str | None = None  # 认证 token


class LaunchResult(BaseModel):
    """启动结果"""

    success: bool
    message: str


def _find_floating_chat_executable() -> Path | None:
    """
    查找悬浮窗可执行文件
    按优先级搜索：
    1. floating-chat/src-tauri/target/release/
    2. floating-chat/src-tauri/target/debug/
    """
    base_path = (
        Path(__file__).parent.parent.parent.parent
        / "floating-chat"
        / "src-tauri"
        / "target"
    )

    # Windows 可执行文件名
    exe_name = "Agent Chat.exe" if sys.platform == "win32" else "agent-chat"

    # 优先查找 release 版本
    release_path = base_path / "release" / exe_name
    if release_path.exists():
        return release_path

    # 其次查找 debug 版本
    debug_path = base_path / "debug" / exe_name
    if debug_path.exists():
        return debug_path

    return None


@router.get("/status", response_model=FloatingChatStatus)
async def get_floating_chat_status():
    """
    获取悬浮窗应用状态
    检查 Tauri 应用是否已编译可用
    """
    exe_path = _find_floating_chat_executable()

    if exe_path:
        return FloatingChatStatus(
            available=True, executable_path=str(exe_path), message="悬浮窗应用已就绪"
        )
    else:
        return FloatingChatStatus(
            available=False,
            executable_path=None,
            message="悬浮窗应用未编译，请先运行 'cd floating-chat && npm run tauri:build'",
        )


@router.post("/launch", response_model=LaunchResult)
async def launch_floating_chat(request: LaunchRequest = LaunchRequest()):
    """
    启动悬浮窗应用

    Args:
        request: 启动请求，包含可选的 session_id 和 token
    """
    import json
    import logging
    import os

    logger = logging.getLogger(__name__)
    exe_path = _find_floating_chat_executable()

    if not exe_path:
        raise HTTPException(
            status_code=404, detail="悬浮窗应用未找到，请先编译 Tauri 应用"
        )

    try:
        logger.info(
            "启动悬浮窗: session_id=%s, token=%s",
            request.session_id,
            "有" if request.token else "无",
        )

        # 方法1: 将参数写入配置文件（更可靠）
        config_data = {}
        if request.session_id:
            config_data["session_id"] = request.session_id
        if request.token:
            config_data["token"] = request.token

        # 写入用户目录下的配置文件
        config_dir = Path.home() / ".floating-chat"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "launch_config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")
        logger.info("配置文件已写入: %s", config_file)

        # 方法2: 同时设置环境变量（作为备用）
        env = os.environ.copy()
        if request.session_id:
            env["FLOATING_CHAT_SESSION_ID"] = request.session_id
        if request.token:
            env["FLOATING_CHAT_TOKEN"] = request.token

        # 构建命令
        args = [str(exe_path)]

        # 使用 subprocess 启动应用（不等待）
        if sys.platform == "win32":
            process = subprocess.Popen(
                args,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("悬浮窗进程已启动: PID=%s", process.pid)
        else:
            subprocess.Popen(
                args,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        return LaunchResult(success=True, message="悬浮窗应用已启动")
    except Exception as e:
        logger.exception("启动悬浮窗失败: %s", e)
        raise HTTPException(status_code=500, detail=f"启动悬浮窗失败: {str(e)}") from e
