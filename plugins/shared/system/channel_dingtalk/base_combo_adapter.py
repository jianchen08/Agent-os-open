"""组合适配器基类。

为通道组合适配器（DingTalk/Feishu/QQ/WeCom）提供通用的
is_connected、health_check、get_status 实现。

子类需要：
- 在 __init__ 中设置 self.stream_client
- 实现 channel_type 属性
"""

from __future__ import annotations

from typing import Any


class BaseComboAdapter:
    """组合适配器基类。

    提供基于 stream_client 的通用状态查询方法。
    子类需设置 self.stream_client 并实现 channel_type 属性。
    """

    @property
    def is_connected(self) -> bool:
        """适配器是否已连接。

        Returns:
            底层 stream_client 的连接状态
        """
        return self.stream_client.is_connected

    async def health_check(self) -> bool:
        """检查适配器是否健康。

        Returns:
            True 表示 stream_client 连接正常
        """
        return self.stream_client.is_connected

    def get_status(self) -> dict[str, Any]:
        """获取适配器状态信息。

        Returns:
            状态字典，包含类型、连接状态和健康信息
        """
        return {
            "type": self.channel_type,
            "connected": self.is_connected,
            "healthy": self.is_connected,
        }
