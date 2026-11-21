# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_qq 默认端口策略测试——防回退守卫。

意图（WHY）：8080 是 zcode-proxy 基础设施保留端口（网关 cpa-gateway 的
zcode-proxy 车道后端，承载 glm-5.3-flash plan 包月额度）。channel_qq 任何
默认端口落点（manifest 内联默认 / server 兜底默认 / 构造签名默认）不得为
8080——默认值撞车会抢占端口使 zcode-proxy 启动守卫静默退出，LLM plan
车道静默失效（充值也不恢复）。

断言三处默认落点同源（单一真值 onebot_client.DEFAULT_WS_PORT）+ 显式
配置优先于默认（守卫不影响配置可覆盖性）。

[来源: docs/working/24e904061cb2_8080端口冲突根治报告.md]
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel  # noqa: E402

use_channel("qq")
import onebot_client  # noqa: E402
from onebot_client import OneBotClient  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_QQ_DIR = _REPO_ROOT / "plugins" / "shared" / "channel_qq"

# 端口保留策略（本文件单点声明）：8080 归 zcode-proxy 基础设施，插件默认禁占。
RESERVED_INFRA_PORTS = frozenset({8080})
DEFAULT_WS_PORT = 8081


def _load_server_module():
    """以独立模块名加载 channel_qq/server.py（逐次新载，供逐测试桩注入）。

    车道共跑语境：收集阶段后导入的兄弟渠道测试文件会把同名裸模块
    （adapter/helpers 等）换入 sys.modules——server.py 的裸名导入会解析到
    他人实现。exec 前重跑 use_channel 重申 qq 目录解析优先权并逐出同名
    缓存（conftest 同款纪律），保证任意收集顺序下解析正确。
    """
    use_channel("qq")
    sys.modules.pop("qq_server_port_guard", None)
    spec = importlib.util.spec_from_file_location(
        "qq_server_port_guard", _QQ_DIR / "server.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestQqDefaultPortPolicy:
    def test_manifest_ws_port_default_follows_policy(self) -> None:
        """manifest 内联默认 == 保留安全端口（plugin.json 声明面）。"""
        manifest = json.loads((_QQ_DIR / "plugin.json").read_text(encoding="utf-8"))
        fields = {f["name"]: f for f in manifest["config_files"][0]["fields"]}
        assert fields["ws_port"]["default"] == DEFAULT_WS_PORT

    def test_policy_value_is_reserved_safe(self) -> None:
        """策略值本身不在基础设施保留端口集合内（守卫的自明前提）。"""
        assert DEFAULT_WS_PORT == 8081
        assert DEFAULT_WS_PORT not in RESERVED_INFRA_PORTS

    def test_policy_constant_exists_in_single_source(self) -> None:
        """单一真值存在：onebot_client.DEFAULT_WS_PORT == 策略值（禁散落字面量）。"""
        assert onebot_client.DEFAULT_WS_PORT == DEFAULT_WS_PORT

    def test_client_signature_default_uses_policy_constant(self) -> None:
        """构造签名默认 == 策略常量（观察实例化结果，非源码文本断言）。"""
        assert OneBotClient()._ws_port == DEFAULT_WS_PORT

    def test_client_explicit_port_overrides_default(self) -> None:
        """显式配置优先：非默认端口原样生效（防实现硬编码 _ws_port）。"""
        assert OneBotClient(ws_port=9999)._ws_port == 9999

    @pytest.mark.asyncio
    @pytest.mark.parametrize("explicit_port", [8081, 18080])
    async def test_on_load_explicit_settings_win(
        self, monkeypatch: pytest.MonkeyPatch, explicit_port: int
    ) -> None:
        """settings 显式 ws_port 原样传递给适配器（两组区分度输入防拟合）。"""
        observed = await self._on_load_ws_port(monkeypatch, {"ws_port": explicit_port})
        assert observed == explicit_port

    @pytest.mark.asyncio
    async def test_on_load_fallback_follows_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings 缺 ws_port 时兜底 == 策略常量（不得落保留端口 8080）。"""
        observed = await self._on_load_ws_port(monkeypatch, {})
        assert observed == DEFAULT_WS_PORT

    async def _on_load_ws_port(
        self, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]
    ) -> int:
        """以桩替身执行 server._on_load，捕获传给 QQAdapter 的 ws_port。

        桩只落在模块边界（QQAdapter/ChannelInboundBridge/插件配置读取），
        on_load 装配逻辑走真实执行路径。
        """
        module = _load_server_module()
        captured: dict[str, Any] = {}

        class _StubAdapter:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                # bridge 构造参数哨兵：server 只传递引用，不访问其成员
                self.input_adapter = object()
                self.output_adapter = object()

            async def start(self) -> None:
                return None

        class _StubBridge:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def start(self) -> None:
                return None

        monkeypatch.setattr(module, "QQAdapter", _StubAdapter)
        monkeypatch.setattr(module, "ChannelInboundBridge", _StubBridge)
        monkeypatch.setattr(module, "user_data_dir", lambda: None)
        monkeypatch.setattr(
            module.plugin,
            "get_config",
            lambda: {"settings": {"access_token": "guard-test", **settings}},
        )
        await module._on_load({})
        return captured["ws_port"]
