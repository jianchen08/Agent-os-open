"""Provider 适配器工厂 + rc（reasoning_content）回放档位解析。

两件事：

1. **适配器选择**（:func:`get_provider_adapter`）：按 model 串子串匹配 provider
   前缀，选出带 provider 特有消息修正的适配器（MiniMax 的 system 角色修复、
   DeepSeek 的 rc 采样保留与补键）。未命中返回基类适配器。
2. **回放档位解析**（:func:`resolve_replay_reasoning`）：按模型条目算出本次调用
   的生效档位（True=回放 / False=剥离）。

加新 provider：
    1. 新建 provider_adapters/<provider>.py 实现 ProviderAdapter 子类
    2. 在本文件的 _REGISTRY 注册一行（provider 前缀 → adapter 实例）
    3. 确证该 provider 拒绝 rc 回传时，在 _PROVIDER_STRIP_PREFIXES 登记一行
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import DEFAULT_REPLAY_REASONING, REPLAY_MODE_FOLD, ProviderAdapter, ReplayMode
from .deepseek import DeepSeekAdapter
from .minimax import MiniMaxAdapter

__all__ = [
    "DEFAULT_REPLAY_REASONING",
    "DeepSeekAdapter",
    "MiniMaxAdapter",
    "ProviderAdapter",
    "REPLAY_MODE_FOLD",
    "ReplayMode",
    "get_provider_adapter",
    "resolve_replay_reasoning",
]


# 复用单例（adapter 无状态，无需每次创建）
_DEFAULT_ADAPTER = ProviderAdapter()
_DEEPSEEK_ADAPTER = DeepSeekAdapter()
_MINIMAX_ADAPTER = MiniMaxAdapter()

# provider 前缀 → adapter（按 model 字符串子串匹配）
_REGISTRY: dict[str, ProviderAdapter] = {
    "deepseek": _DEEPSEEK_ADAPTER,
    "minimax": _MINIMAX_ADAPTER,
}

def get_provider_adapter(model: str) -> ProviderAdapter:
    """根据 model 字符串返回对应的 ProviderAdapter。

    匹配规则：model 字符串中包含 provider 前缀即命中。
    未命中返回默认 adapter（基类：回放 rc，无 provider 特有修正）。

    Args:
        model: model 标识（如 "minimax/MiniMax-M3"、"deepseek-v4-flash"、
               "gw-glm-5.3-flash"）

    Returns:
        对应的 ProviderAdapter 实例
    """
    if not model:
        return _DEFAULT_ADAPTER

    model_lower = model.lower()
    for provider_prefix, adapter in _REGISTRY.items():
        if provider_prefix in model_lower:
            return adapter
    return _DEFAULT_ADAPTER


#: provider 名前缀 → 显式**剥离**名单（确证拒绝 rc 回传的 provider 才入列）。
#:
#: 键取 llm.yaml ``models.<id>.provider`` 的 provider 名，前缀匹配（"zhipu"
#: 同时覆盖 "zhipu_coding"）。当前为空——2026-10-21 实机探针实测
#: MiniMax-M3.1-Flash-Preview、GLM-5.2（zhipu_coding 端点）、SiliconFlow-DeepSeek
#: 回传 rc 均 HTTP 200 正常应答；DeepSeek 新版 thinking 模式带 tools 时缺 rc 直接
#: 400（官方文档口径）→ 须回放。
_PROVIDER_STRIP_PREFIXES: frozenset[str] = frozenset()

#: provider 名前缀 → **字段回放已实测可用**名单（端到端：AgentOS → 中间代理 →
#: 上游全链路 rc 字段可达，模型确实消费）。
#:
#: 2026-10-21 三路实验：直连智谱（zhipu_coding/glm-5.2）字段 rc 正确召回；
#: MiniMax 直连 HTTP 200 且任务侧行为正常；DeepSeek 文档口径必须回传。
#: **未入列的 provider（网关/中转族如 cpa_gateway）默认折叠正文**——CLIProxyAPI
#: 网关转发上游时丢弃 rc 字段（同实验：字段 rc 模型失忆、正文折叠正确召回），
#: 字段可达性未经逐跳实测不可假设。
_PROVIDER_FIELD_REPLAY_PREFIXES: tuple[str, ...] = ("minimax", "deepseek", "zhipu")


def resolve_replay_reasoning(model_entry: Mapping[str, Any] | None) -> ReplayMode:
    """解析模型条目的生效 rc 回放档位（True=字段回放 / "fold"=折叠正文 / False=剥离）。

    解析序（2026-10-21 用户裁定：默认档翻转为回放；R2 网关丢字段实验后引入
    折叠档）：

    ① **模型条目显式声明**：``llm.yaml`` ``models.<id>.replay_reasoning:
       true|false|"fold"``（条目顶层键——**不可放进 default_params**，后者会随
       kwargs 透传上游触发参数拒绝）——最高优先级，
       改配置不改码的档位杠杆；
    ② **provider 级名单**：字段端到端实测可用
       （:data:`_PROVIDER_FIELD_REPLAY_PREFIXES`）→ True；确证拒绝
       （:data:`_PROVIDER_STRIP_PREFIXES`）→ False；
    ③ **默认档**：未登记 provider（网关/中转族）→ ``"fold"`` 折叠正文——
       正文任何中间代理都转发，字段可达性不可假设。

    Args:
        model_entry: llm.yaml 的模型条目（None=模型未在配置表内）

    Returns:
        生效回放档位（True / "fold" / False）
    """
    if isinstance(model_entry, Mapping):
        declared = model_entry.get("replay_reasoning")
        if declared is True or declared is False or declared == REPLAY_MODE_FOLD:
            return declared
        provider = str(model_entry.get("provider") or "")
        for prefix in _PROVIDER_STRIP_PREFIXES:
            if provider.startswith(prefix):
                return False
        for prefix in _PROVIDER_FIELD_REPLAY_PREFIXES:
            if provider.startswith(prefix):
                return True
    return REPLAY_MODE_FOLD
