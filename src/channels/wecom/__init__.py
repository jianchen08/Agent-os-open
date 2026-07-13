"""企业微信通道适配器模块。

提供企业微信回调模式的完整通道适配能力：
- WeComAdapter: 组合模式入口，管理输入/输出适配器生命周期
- WeComStreamClient: 企业微信 HTTP API 客户端（消息发送 + access_token 管理）
- WecomCrypto: 企业微信消息加解密
"""

from channels.wecom.adapter import WeComAdapter

__all__ = ["WeComAdapter"]
