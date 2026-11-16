# @feature: FP-T07 llm api | @ci: python-coverage
"""reasoning_content 回放契约测试（线① 2026-10-21；R2 默认档翻转）——矩阵 + 实机探针。

契约（provider_adapters/base.py ``replay_reasoning`` 档位化，R2 三路实验后）：

- **字段端到端实测可用者（minimax/deepseek/zhipu）字段回放**（返回新列表
  新 dict，不改写输入；全量回放 append-only 缓存友好，上下文增长由既有
  压缩层承担）；
- **未登记 provider（网关/中转族）默认 "fold" 折叠正文**——rc 以 ``<think>``
  块并入 assistant 正文并去掉字段。CLIProxyAPI 网关转发上游时丢弃 rc 字段
  （同题对照实验：字段 rc 模型失忆、正文折叠正确召回），正文任何代理都转发；
- 生效档位解析（``resolve_replay_reasoning``）：模型条目显式声明
  （``llm.yaml`` ``models.<id>.replay_reasoning: true|false|"fold"``）>
  provider 名单（字段实测可用 → True / 确证拒绝 → False）> 未登记 → fold；
- 策略值**不得进上游请求参数**（参数拒绝）——专用参数
  ``replay_reasoning`` 由 llm_service 侧按条目解析后传递。

实机探针（本文件 ``real_api`` 标记档）由 .zctmp/queue_probe_reasoning.py 转正
（一次性脚本按工作区约定会被还原清除，故转正为可复跑测试）：同一组消息
带/不带 rc 各打一次，区分"字段被拒"与"请求本身坏"。探针不进常规门禁：
需真实密钥（读装机 .env）且显式 ``AGENTOS_REASONING_PROBE=1`` 才执行，
两条件任一缺失即 skip（CI 天然不生效）。

外部依赖：单测部分无（纯消息变换，不触网）；实机档需真实 LLM key。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

import adapter as _adapter_mod  # noqa: E402
import provider_adapters as _provider_adapters  # noqa: E402
from provider_adapters import (  # noqa: E402
    DEFAULT_REPLAY_REASONING,
    DeepSeekAdapter,
    MiniMaxAdapter,
    ProviderAdapter,
    get_provider_adapter,
    resolve_replay_reasoning,
)


def _assistant_with_rc(rc: str = "思考过程") -> dict[str, Any]:
    return {"role": "assistant", "content": "答", "reasoning_content": rc}


# ─────────────────── 策略矩阵：rc 保留/剥离 × provider ───────────────────


def test_base_adapter_default_replays_rc() -> None:
    """基类默认档=回放（R2 用户裁定：默认不丢思考，确证拒绝者才例外）。"""
    assert DEFAULT_REPLAY_REASONING is True
    out = ProviderAdapter().adapt_messages_before_send(
        [{"role": "user", "content": "u"}, _assistant_with_rc()]
    )

    assert out[1]["reasoning_content"] == "思考过程"
    assert out[1]["content"] == "答"


def test_base_adapter_per_call_strip_overrides_default() -> None:
    """逐次调用参数覆盖默认档：replay_reasoning=False 时剥离（确证拒绝的紧急回退）。"""
    out = ProviderAdapter().adapt_messages_before_send(
        [{"role": "user", "content": "u"}, _assistant_with_rc()], replay_reasoning=False
    )

    assert "reasoning_content" not in out[1]
    assert out[1]["content"] == "答"


def test_base_adapter_replay_keeps_rc_and_returns_new_dicts() -> None:
    """回放档：rc 原样保留；返回新 dict，不改写输入。"""
    adapter = ProviderAdapter()
    original = [_assistant_with_rc("rc 原文")]

    out = adapter.adapt_messages_before_send(original)

    assert out[0]["reasoning_content"] == "rc 原文"
    assert out is not original
    assert out[0] is not original[0]
    out[0]["reasoning_content"] = "被改写"
    assert original[0]["reasoning_content"] == "rc 原文"


def test_minimax_adapter_replays_rc() -> None:
    """MiniMax 策略=True（2026-10-21 探针 HTTP 200 实测）：rc 保留 + system 修复正交。"""
    adapter = MiniMaxAdapter()
    assert adapter.replay_reasoning is True

    out = adapter.adapt_messages_before_send(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            _assistant_with_rc("minimax rc"),
            {"role": "system", "content": "late", "name": "reminder"},
        ]
    )

    assert out[2]["reasoning_content"] == "minimax rc"
    assert out[3]["role"] == "user"  # 非首位 system 修复与 rc 回放正交
    assert "name" not in out[3]


def test_minimax_adapter_no_mutation() -> None:
    """MiniMax 返回新列表新 dict：输入消息不被就地改写（纯函数契约）。"""
    adapter = MiniMaxAdapter()
    original = [_assistant_with_rc("rc"), {"role": "system", "content": "late", "name": "n"}]

    out = adapter.adapt_messages_before_send(original)

    assert out[0] is not original[0]
    assert original[0]["reasoning_content"] == "rc"
    assert original[1]["role"] == "system"
    assert original[1].get("name") == "n"


def test_deepseek_adapter_replay_policy_and_sampling_coherent() -> None:
    """DeepSeek 策略=True（文档口径：thinking 带 tools 缺 rc 即 400），
    且类属性与实际行为一致——采样保留路径确实回传 rc。"""
    adapter = DeepSeekAdapter()
    assert adapter.replay_reasoning is True

    out = adapter.adapt_messages_before_send(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
            }
        ]
    )

    # thinking 模式硬约束：assistant(tool_calls) 必须带 rc 键（内容可为空串）
    assert "reasoning_content" in out[0]


def test_registry_maps_replay_policy_by_provider_prefix() -> None:
    """注册表映射：minimax/deepseek 前缀 → 回放档；未注册 provider（zai/glm、
    网关族）→ 基类默认档（回放）——R2 装机排障结论：默认剥离曾让网关模型
    全量丢思考（本测试锁死该回归）。"""
    assert get_provider_adapter("minimax/MiniMax-M3").replay_reasoning is True
    assert get_provider_adapter("deepseek/deepseek-v4-pro").replay_reasoning is True
    assert get_provider_adapter("zai/glm-5.2").replay_reasoning is True
    assert get_provider_adapter("openai/gw-glm-5.3-flash").replay_reasoning is True
    assert get_provider_adapter("").replay_reasoning is True


def test_resolve_replay_reasoning_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """生效档位解析（R2 三路实验后）：条目显式声明 > provider 名单 > 未登记默认折叠。

    名单分两份：字段端到端实测可用（minimax/deepseek/zhipu）→ True；确证拒绝
    → False；**未登记 provider（网关/中转族 cpa_gateway 等）→ "fold" 折叠正文**
    ——CLIProxyAPI 网关转发上游时丢弃 rc 字段（字段 rc 模型失忆、正文折叠正确
    召回，2026-10-21 同题对照实验），字段可达性未经逐跳实测不可假设。
    """
    # 未登记 provider → 折叠正文（网关族装机失效根修的落点）
    assert resolve_replay_reasoning(None) == "fold"
    assert resolve_replay_reasoning({"provider": "cpa_gateway"}) == "fold"
    assert resolve_replay_reasoning({"provider": "some_new_relay"}) == "fold"
    # 字段端到端实测可用名单 → 字段回放
    assert resolve_replay_reasoning({"provider": "minimax"}) is True
    assert resolve_replay_reasoning({"provider": "deepseek"}) is True
    assert resolve_replay_reasoning({"provider": "zhipu_coding"}) is True
    # 条目显式声明最高优先级（改配置不改码的档位杠杆）
    assert resolve_replay_reasoning({"provider": "cpa_gateway", "replay_reasoning": True}) is True
    assert resolve_replay_reasoning({"provider": "minimax", "replay_reasoning": False}) is False
    assert (
        resolve_replay_reasoning({"provider": "cpa_gateway", "replay_reasoning": "fold"})
        == "fold"
    )
    # 非 bool/fold 声明视为未声明（配置写错不静默改档）
    assert resolve_replay_reasoning({"replay_reasoning": "false"}) == "fold"
    # provider 级剥离名单（注入假名单验证匹配逻辑）
    monkeypatch.setattr(_provider_adapters, "_PROVIDER_STRIP_PREFIXES", frozenset({"legacy"}))
    assert resolve_replay_reasoning({"provider": "legacy_proxy"}) is False
    assert resolve_replay_reasoning({"provider": "legacy_proxy", "replay_reasoning": True}) is True
    assert resolve_replay_reasoning({"provider": "cpa_gateway"}) == "fold"


def test_fold_mode_merges_rc_into_content_and_drops_field() -> None:
    """折叠档：rc 以 <think> 块并入正文、rc 字段移除；空 rc 消息原样透传；
    输入不被改写（纯函数契约）。"""
    adapter = ProviderAdapter()
    original = [
        {"role": "user", "content": "u"},
        _assistant_with_rc("第一轮想到的数字是 4283"),
        {"role": "assistant", "content": "无思考的回复", "reasoning_content": "   "},
        {"role": "system", "content": "sys"},
        # 中断轮半截形态（llm_core 纯思考落库，用户裁定 2026-11-04）：空正文
        # + rc 非空 → rc 全量并入正文（正文不再为空，直传安全）
        {"role": "assistant", "content": "", "reasoning_content": "中断前的思考"},
    ]

    out = adapter.adapt_messages_before_send(
        [dict(m) for m in original], replay_reasoning="fold"
    )

    assert out[1]["content"].startswith("<think>\n第一轮想到的数字是 4283\n</think>\n答")
    assert "reasoning_content" not in out[1]
    assert out[2]["content"] == "无思考的回复"  # 空 rc 不注入空 think 块
    assert "reasoning_content" not in out[2]
    assert out[0] == original[0]
    assert out[3] == original[3]
    assert out[4]["content"] == "<think>\n中断前的思考\n</think>"  # 空正文全量折叠
    assert "reasoning_content" not in out[4]
    # 输入不被改写
    assert original[1]["content"] == "答"
    assert original[1]["reasoning_content"] == "第一轮想到的数字是 4283"


class _SimpleWireCapture:
    """wire 层捕获桩：记录 build 后的 body 与调用参数。"""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def build_body(self, *a: Any, **k: Any) -> dict[str, Any]:
        import wire_client as _real  # noqa: PLC0415

        return _real.build_body(*a, **k)

    async def post_chat(self, base_url: str, api_key: str, body: dict[str, Any], *, timeout: float) -> Any:
        self._captured.update(body)
        self._captured["_wire_api_key"] = api_key
        raise RuntimeError("probe-stop")

    async def stream_chat(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("probe-stop")


def test_replay_policy_not_leaked_into_upstream_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """策略专用参数不得外发上游（参数拒绝）：
    出站请求参数无 replay_reasoning 键，且档位实际生效（剥离档出站无 rc）。"""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("probe-stop")

    adapter = _adapter_mod.KeyPoolAdapter(router=None)
    monkeypatch.setattr(_adapter_mod, "wire_client", _SimpleWireCapture(captured))

    with pytest.raises(Exception, match="probe-stop"):  # noqa: B017, PT011
        asyncio.run(
            adapter.completion(
                model="minimax/MiniMax-M3",
                messages=[{"role": "user", "content": "u"}, _assistant_with_rc("rc 原文")],
                replay_reasoning=False,
                api_base="http://127.0.0.1:9/v1",
                api_key="sk-probe",
                max_tokens=8,
            )
        )

    assert "replay_reasoning" not in captured
    out_msgs = captured.get("messages") or []
    assert len(out_msgs) > 0
    assert all("reasoning_content" not in m for m in out_msgs)


def test_policy_matrix_behavior_matches_declared_flag() -> None:
    """矩阵不变量：任一 adapter 实例的实际行为（rc 是否保留）与声明档一致。"""
    adapters = [ProviderAdapter(), MiniMaxAdapter(), DeepSeekAdapter()]
    for adapter in adapters:
        out = adapter.adapt_messages_before_send([_assistant_with_rc("matrix rc")])
        kept = "reasoning_content" in out[0]
        assert kept is adapter.replay_reasoning, type(adapter).__name__
        if kept:
            assert out[0]["reasoning_content"] == "matrix rc"


# ─────────────────── 实机探针（.zctmp/queue_probe_reasoning.py 转正）───────────────────

_REAL_API_MARKER = pytest.mark.real_api
_PROBE_OPT_IN_ENV = "AGENTOS_REASONING_PROBE"


def _load_installed_env() -> dict[str, str]:
    """读装机实例 .env（%APPDATA%/agentos/.env）；缺失返回空 dict。"""
    env_path = Path(os.environ.get("APPDATA", "")) / "agentos" / ".env"
    if not env_path.is_file():
        return {}
    env: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _probe_targets() -> list[tuple[str, str, str, str]]:
    """探针目标 (名称, base_url, api_key 环境键, model)；key 空的目标由调用方跳过。"""
    env = _load_installed_env()
    return [
        (
            "MiniMax",
            "https://api.minimaxi.com/v1",
            env.get("MINIMAX_API_KEY", ""),
            "MiniMax-M3.1-Flash-Preview",
        ),
        (
            "GLM",
            "https://open.bigmodel.cn/api/coding/paas/v4",
            env.get("ZHIPU_CODING_API_KEY", ""),
            "glm-5.2",
        ),
        (
            "SiliconFlow-DeepSeek",
            "https://api.siliconflow.cn/v1",
            env.get("SILICONFLOW_API_KEY", ""),
            "deepseek-ai/DeepSeek-V3.2-Exp",
        ),
    ]


def _probe_available_targets() -> list[tuple[str, str, str, str]]:
    if os.environ.get(_PROBE_OPT_IN_ENV) != "1":
        return []
    return [(name, base, key, model) for name, base, key, model in _probe_targets() if key]


def _call_chat_completion(base: str, key: str, model: str, *, with_rc: bool) -> str:
    """单次 chat/completions 调用（assistant 回传/不回传 rc），返回回复文本。

    HTTP 非 2xx 抛 HTTPError（断言失败即"该 provider 拒绝 rc 回传"实证）。
    """
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "1+1=?只回答数字"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "再加3呢？只回答数字"},
    ]
    if with_rc:
        msgs[1]["reasoning_content"] = "用户问1+1，答案是2。"
    body: dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": 300, "stream": False}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return str(msg.get("content") or "")


@_REAL_API_MARKER
@pytest.mark.skipif(
    not _probe_available_targets(),
    reason=(
        "实机探针需真实密钥（装机 .env）且显式 AGENTOS_REASONING_PROBE=1 "
        "（缺密钥 skip，不对 CI 与常规门禁生效）"
    ),
)
@pytest.mark.parametrize("with_rc", [False, True], ids=["no_rc", "with_rc"])
def test_live_providers_tolerate_reasoning_content_replay(with_rc: bool) -> None:
    """实机容忍探针：带/不带 rc 各打一次，均应 HTTP 200 正常应答。

    对照设计区分"字段被拒"与"请求本身坏"：no_rc 组失败 = 请求本身坏
    （与 rc 无关的环境问题），with_rc 组失败 = 该 provider 拒绝 rc 回传。
    """
    failures: list[str] = []
    for name, base, key, model in _probe_available_targets():
        try:
            reply = _call_chat_completion(base, key, model, with_rc=with_rc)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            failures.append(f"{name} HTTP {exc.code}: {detail}")
            continue
        assert reply.strip(), f"{name} 回复为空（with_rc={with_rc}）"
    assert not failures, f"provider 拒绝 rc 回传或请求失败: {failures}"
