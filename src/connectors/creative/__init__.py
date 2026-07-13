"""
创意生产连接器包

提供与外部创作软件的连接器实现：
- ComfyUI：AI 图像生成
- GameEngine：游戏引擎集成
- GenericConnector：通用 HTTP/WebSocket 连接器

暴露接口：
- ComfyUIConnector: ComfyUI 连接器
- GameEngineConnector: 游戏引擎连接器
- GenericCreativeConnector: 通用创意软件连接器
"""

from .comfyui import ComfyUIConnector
from .game_engine import GameEngineConnector
from .generic import GenericCreativeConnector

__all__ = [
    "ComfyUIConnector",
    "GameEngineConnector",
    "GenericCreativeConnector",
]
