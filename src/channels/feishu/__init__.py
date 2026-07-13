"""飞书通道适配器模块。

提供飞书 Stream 模式的完整通道适配能力：
- FeishuAdapter: 组合模式入口，管理输入/输出适配器生命周期
- FeishuStreamClient: 飞书 Stream WebSocket 客户端
- CardBuilder: 飞书卡片消息构建器
"""

from channels.feishu.adapter import FeishuAdapter

__all__ = ["FeishuAdapter"]
