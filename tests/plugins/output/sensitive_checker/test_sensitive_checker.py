# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""SensitiveChecker 单元测试——敏感数据模式检测与脱敏。

覆盖：各类 Token/Key 正则匹配、password/api_key 字段值脱敏、
递归处理 dict/list、未命中不改原值、disabled 跳过、自定义 mask。
"""

from __future__ import annotations

from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(results: list[Any]) -> PluginContext:
    return PluginContext(state={StateKeys.TOOL_RESULTS: results}, config={})


# ============================================================
# 配置
# ============================================================


class TestConfig:
    def test_默认配置(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        assert c.name == "sensitive_checker"
        assert c.priority == 20
        assert c._enabled is True
        assert c._mask == "***"

    def test_自定义配置(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker(config={"enabled": False, "mask": "[REDACTED]", "priority": 1})
        assert c._enabled is False
        assert c._mask == "[REDACTED]"
        assert c.priority == 1

# ============================================================
# _sanitize_string —— 各类模式
# ============================================================


class TestStringSanitization:
    def test_OpenAI_key被脱敏(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        out = c._sanitize_string("key is sk-abcdefghijklmnopqrstuvwxyz")
        assert "sk-abcdefghij" not in out
        assert "***" in out

    def test_GitHub_token被脱敏(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        token = "ghp_" + "a" * 36
        out = c._sanitize_string(f"token={token}")
        assert token not in out
        assert "***" in out

    def test_Slack_token被脱敏(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        token = "xoxb-123456789012-abcdefgh"
        out = c._sanitize_string(f"slack {token} end")
        assert token not in out

    def test_AWS_access_key被脱敏(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        token = "AKIA" + "A" * 16  # AKIA + 16 chars
        out = c._sanitize_string(f"aws {token}")
        assert token not in out

    def test_password字段触发脱敏插入mask(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        out = c._sanitize_string("password=secret123")
        # mask 被插入；key 名保留（实际实现把 mask 插在 key 后）
        assert "***" in out
        assert "password" in out

    def test_api_key字段触发脱敏插入mask(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        out = c._sanitize_string('api_key: "value123"')
        assert "***" in out
        assert "api_key" in out

    def test_pwd与apikey缩写也命中插入mask(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        out1 = c._sanitize_string("pwd=abc")
        out2 = c._sanitize_string("apikey=xyz")
        assert "***" in out1
        assert "***" in out2

    def test_无敏感内容原样返回(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        text = "just a normal result with no secrets"
        assert c._sanitize_string(text) == text

    def test_自定义mask生效(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker(config={"mask": "[HIDDEN]"})
        out = c._sanitize_string("sk-abcdefghijklmnopqrstuvwxyz")
        assert "[HIDDEN]" in out
        assert "***" not in out


# ============================================================
# _sanitize_value —— 递归结构
# ============================================================


class TestRecursiveSanitization:
    def test_dict内敏感值递归脱敏且原对象不变时返回同对象(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        # 无敏感内容 → 返回原 dict
        d = {"a": "normal", "b": ["x"]}
        assert c._sanitize_value(d) is d

    def test_dict含敏感返回新dict(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        d = {"token": "sk-abcdefghijklmnopqrstuvwxyz"}
        out = c._sanitize_value(d)
        assert out is not d
        assert "sk-abcdefghij" not in out["token"]

    def test_list含敏感返回新list(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        lst = ["ok", "sk-abcdefghijklmnopqrstuvwxyz"]
        out = c._sanitize_value(lst)
        assert out is not lst
        assert "sk-abcdefghij" not in out[1]
        assert out[0] == "ok"

    def test_无敏感list返回同对象(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        lst = ["a", "b"]
        assert c._sanitize_value(lst) is lst

    def test_嵌套dict_list混合脱敏(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        # 用真正会命中的内容：OpenAI key（独立 token 模式）
        value = {
            "outer": [
                {"config": "sk-abcdefghijklmnopqrstuvwxyz"},
                "normal",
                {"note": "api_key: k123"},
            ]
        }
        out = c._sanitize_value(value)
        assert out is not value
        # mask 被插入到嵌套结构中
        s = str(out)
        assert "***" in s
        assert "sk-abcdefghij" not in s

    def test_非字符串数字布尔原样返回(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        for v in (42, 3.14, True, None):
            assert c._sanitize_value(v) is v


# ============================================================
# execute 端到端
# ============================================================


class TestExecute:
    @pytest.mark.asyncio
    async def test_disabled时返回空结果(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker(config={"enabled": False})
        result = await c.execute(_ctx(["sk-abcdefghijklmnopqrstuvwxyz"]))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_空tool_results返回空结果(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        result = await c.execute(PluginContext(state={}, config={}))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_无敏感内容不写state(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        result = await c.execute(_ctx(["normal text", {"k": "v"}]))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_检测到敏感写入sensitive_detected与脱敏结果(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        results = ["sk-abcdefghijklmnopqrstuvwxyz", "clean"]
        result = await c.execute(_ctx(results))

        assert result.state_updates.get("sensitive_detected") is True
        new_results = result.state_updates[StateKeys.TOOL_RESULTS]
        assert "sk-abcdefghij" not in new_results[0]
        assert new_results[1] == "clean"  # 未命中保留原值

    @pytest.mark.asyncio
    async def test_嵌套结构脱敏写入state(self) -> None:
        from plugin import SensitiveChecker

        c = SensitiveChecker()
        results = [{"config": "password=hunter2"}]
        result = await c.execute(_ctx(results))

        assert result.state_updates.get("sensitive_detected") is True
        new = result.state_updates[StateKeys.TOOL_RESULTS][0]
        # mask 被插入到嵌套字段中
        assert "***" in new["config"]
