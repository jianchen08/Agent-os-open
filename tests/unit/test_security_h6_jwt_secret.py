"""H6 安全回归：JWT secret 强度启动校验。

漏洞：src/config/settings.py 默认 jwt_secret_key 为已知占位串
（"dev-insecure-key-do-not-use-in-production"），.env 用
"development-secret-key-change-in-production"——攻击者知道这些值
即可伪造任意用户 token。

修复：model_validator 在非 development 环境强制校验 secret 强度
（非占位串且 >=32 字节），否则拒启动（fail-closed）。

本测试守护修复：若有人移除 validator 或放宽阈值，测试变红。
"""
from __future__ import annotations

import os

import pytest


def _make_settings(**env_overrides: str):  # noqa: ANN201
    """用给定环境变量构造一个全新的 Settings 实例（绕过模块单例缓存）。"""
    from src.config import settings as settings_module

    # 清掉所有可能干扰的既有环境变量
    for key in ("APP_ENVIRONMENT", "APP_JWT_SECRET_KEY"):
        os.environ.pop(key, None)
    os.environ.update(env_overrides)
    return settings_module.Settings()


class TestJwtSecretStrengthValidator:
    """H6: 非 development 环境必须配置强 JWT secret。"""

    def test_development_allows_default_weak_secret(self) -> None:
        """development 环境放行弱 secret（不影响本地开发）。

        不论是代码默认值还是 .env 占位串，development 环境都不应拒启动。
        """
        s = _make_settings(APP_ENVIRONMENT="development")
        assert s.environment == "development"
        # 不抛异常即通过（无论读到的是默认值还是 .env 占位串）

    def test_production_rejects_code_default_placeholder(self) -> None:
        """production 环境拒绝代码默认占位串。"""
        with pytest.raises(Exception, match="JWT"):
            _make_settings(
                APP_ENVIRONMENT="production",
                APP_JWT_SECRET_KEY="dev-insecure-key-do-not-use-in-production",
            )

    def test_production_rejects_env_placeholder(self) -> None:
        """production 环境拒绝 .env 里的常见占位串。"""
        with pytest.raises(Exception, match="JWT"):
            _make_settings(
                APP_ENVIRONMENT="production",
                APP_JWT_SECRET_KEY="development-secret-key-change-in-production",
            )

    def test_production_rejects_short_secret(self) -> None:
        """production 环境拒绝 <32 字节的过短 secret。"""
        with pytest.raises(Exception, match="JWT"):
            _make_settings(
                APP_ENVIRONMENT="production",
                APP_JWT_SECRET_KEY="shortkey",
            )

    def test_production_accepts_strong_secret(self) -> None:
        """production 环境 + 64 字节强 secret 正常构造。"""
        strong = "a" * 64
        s = _make_settings(
            APP_ENVIRONMENT="production",
            APP_JWT_SECRET_KEY=strong,
        )
        assert s.jwt_secret_key == strong
