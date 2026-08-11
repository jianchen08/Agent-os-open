"""媒体 Provider 实现模块。

提供具体的媒体 Provider 实现，如 ComfyUI 图像生成 Provider、MiniMax 图像/视频/音乐/TTS Provider、Edge TTS 语音合成 Provider。
"""

from tools.media.providers.comfyui_provider import ComfyUIProvider
from tools.media.providers.edge_tts_provider import EdgeTTSProvider
from tools.media.providers.minimax_music_provider import MiniMaxMusicProvider
from tools.media.providers.minimax_provider import MiniMaxImageProvider
from tools.media.providers.minimax_tts_provider import MiniMaxTTSProvider
from tools.media.providers.minimax_video_provider import MiniMaxVideoProvider

__all__ = [
    "ComfyUIProvider",
    "EdgeTTSProvider",
    "MiniMaxImageProvider",
    "MiniMaxMusicProvider",
    "MiniMaxTTSProvider",
    "MiniMaxVideoProvider",
]
