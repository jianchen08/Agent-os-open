"""钉钉通道适配器模块。

提供钉钉 Stream 模式的完整通道适配能力：
- DingTalkAdapter: 组合模式入口，管理输入/输出适配器生命周期
- DingTalkStreamClient: 钉钉 Stream WebSocket 客户端
"""

from channels.dingtalk.adapter import DingTalkAdapter

__all__ = ["DingTalkAdapter"]
