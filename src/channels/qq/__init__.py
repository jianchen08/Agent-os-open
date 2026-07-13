"""QQ 通道适配器模块。

基于 OneBot v11 协议（go-cqhttp/OneBot），提供 QQ 通道的完整适配能力：
- QQAdapter: 组合模式入口，管理输入/输出适配器生命周期
- OneBotClient: OneBot v11 客户端，反向 WebSocket 接收消息 + HTTP API 发送消息

采用反向 WebSocket 模式：go-cqhttp 主动连接到我们的 WebSocket 服务端，
我们通过 HTTP API 向 go-cqhttp 发送消息。
"""

from channels.qq.adapter import QQAdapter

__all__ = ["QQAdapter"]
