"""
Round2 Auth 认证授权模块 — 测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- AC-AUTH-06: Token刷新流程完整（端到端 → 新Token可用 / 旧Token撤销）
- AC-AUTH-10: 登录限流5次/分钟（AUTH类别限流 + RateLimitExceededError）
- AC-AUTH-11: Redis不可用时降级内存（生命周期：撤销→刷新→全设备撤销）
"""
from tests.unit.test_round2_infra_gaps import (  # noqa: F401
    TestTokenRefreshFullFlow,
    TestLoginRateLimit,
    TestRedisFallbackDeep,
)
