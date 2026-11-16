"""组合适配器基类（channel_common 渠道共享包）。

单一事实源：四渠道组合适配器的公共实现只在本文维护，各渠道插件目录不得再放
同名 base_combo_adapter.py（scripts/check_channel_copy_guard.py 守卫复制回潮）。
路径注入契约：本目录由各渠道 server.py 以 sys.path.append 引入、绝不 insert(0)——
本目录模块名是通用名，insert(0) 会遮蔽其他目录的同名模块，
谁在前谁生效。完整背景见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三。

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
