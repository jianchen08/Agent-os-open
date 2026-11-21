"""Provider 适配器抽象基类。

复制锚点：源文件 ``plugins/shared/llm/provider_adapters/base.py`` @
c0fe6922fc8cf68cc0c46b6ff1e11c0db021056a（批 2 收口基线）；逐行同源。

职责边界：
    在消息发送给 API 前，按 provider 特定规则适配消息格式。
    只处理"对外发送"的差异，不修改系统内部的消息结构。

设计原则：
    - 系统内部 state["messages"] 统一保留所有字段（含 reasoning_content）
    - 发送给 API 前，由 ProviderAdapter 按 provider 规则裁剪/转换
    - 每个 provider 一个子类，独立文件，互不影响
    - 加新 provider：1. 实现 ProviderAdapter 子类；2. 在 __init__.py 注册

与 _message_normalizer.py 的边界：
    - _message_normalizer：消息结构标准化（provider 无关，如 tool_calls 配对校验）
    - ProviderAdapter：provider 特定适配（rc 回放档位 + 各 provider 格式修正）
"""

from __future__ import annotations

from typing import Any

#: rc（reasoning_content）回放的**基类默认档**：True=字段回放（保留并原样发送）。
#:
#: 2026-10-21 用户裁定：默认档由"剥离（fail-closed）"翻转为"回放"——剥离是
#: 过时口径（DeepSeek 新 thinking 模式带 tools 时缺 rc 直接 400；Codex/Claude
#: 工具循环均逐轮回放思考内容），思考内容是思考模型的耐久上下文主体，剥了等于
#: 结构性失忆；例外（剥离 / 折叠）走 provider 级名单或模型条目显式声明，
#: 见 __init__.resolve_replay_reasoning。
DEFAULT_REPLAY_REASONING = True

#: rc 折叠档（``"fold"``）：rc 以 ``<think>`` 块并入正文并去掉 rc 字段。
#:
#: 2026-10-21 三路实验实锤的**中间代理丢字段**通用通道：同一段思考，字段通道
#: 经 CLIProxyAPI 网关转发上游后模型失忆（回复空），正文折叠通道正确召回
#: 4283，直连智谱字段通道正确召回——字段可用性与链路上每一跳相关，未经实测
#: 验证"端到端字段可达"的 provider（网关/中转族）一律走正文折叠，正文任何
#: 代理都转发。回退杠杆：模型条目 ``replay_reasoning: true`` 强制字段档。
REPLAY_MODE_FOLD = "fold"

#: 生效档位的合法值：True（字段回放）/ REPLAY_MODE_FOLD（折叠正文）/ False（剥离）。
ReplayMode = bool | str


class ProviderAdapter:
    """Provider 适配器基类。

    rc（reasoning_content）回放档位（``replay_reasoning`` 类属性 + 逐次调用
    ``replay_reasoning`` 覆盖参数）：assistant 消息携带的思考内容如何随请求
    回传给上游。

    - ``True``（默认，见 :data:`DEFAULT_REPLAY_REASONING`）：字段回放——rc
      原样保留在 reasoning_content 字段（内容级裁剪如采样/预算由上游出站
      构造或子类覆写负责，本基类只做整字段级策略）。
    - ``"fold"``（:data:`REPLAY_MODE_FOLD`）：折叠回放——rc 以 ``<think>``
      块并入 assistant 正文并去掉 rc 字段。**字段被中间代理丢弃时的通用
      通道**（正文任何代理都转发）。
    - ``False``：剥离 rc 字段（仅供确证拒绝该字段的 provider 使用）。

    生效档位由三层解析（:func:`provider_adapters.resolve_replay_reasoning`）：
    模型条目显式声明 > provider 级名单（字段实测可用 → True / 确证拒绝 →
    False）> 未登记 provider 默认 ``"fold"``（正文通道最通用）。逐次调用的
    ``replay_reasoning`` 参数（由 llm_service 侧按模型条目解析后传入）优先于
    类属性。

    所有适配方法返回新列表，不修改原数据（保护 state["messages"]）。
    """

    #: 该 provider 形态的固有回放档位（True / "fold" / False）。
    replay_reasoning: ReplayMode = DEFAULT_REPLAY_REASONING

    def adapt_messages_before_send(
        self,
        messages: list[dict[str, Any]],
        *,
        replay_reasoning: ReplayMode | None = None,
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        """发送给 API 前适配消息。

        默认实现按生效回放档处理 rc（见 :meth:`_adapt_by_mode`）：
        True=字段回放；``"fold"``=折叠进正文；False=剥离。
        provider 特有格式修复由子类覆写并自行遵守同一档位。

        Args:
            messages: 标准化后的消息列表（含 reasoning_content 等内部字段）
            replay_reasoning: 本次调用的生效档位；None=沿用类属性默认档。
            **kwargs: 模型的 API 参数（即 default_params），子类可按需读取

        Returns:
            适配后的消息列表（新列表，原数据不变）
        """
        return self._adapt_by_mode(messages, self._effective_replay(replay_reasoning))

    def _effective_replay(self, replay_reasoning: ReplayMode | None) -> ReplayMode:
        """生效回放档：逐次调用参数优先，其次类属性档位。"""
        if replay_reasoning is None:
            return self.replay_reasoning
        return replay_reasoning

    def _adapt_by_mode(
        self,
        messages: list[dict[str, Any]],
        effective: ReplayMode,
    ) -> list[dict[str, Any]]:
        """按生效档位逐条适配（返回新列表新 dict，不改写输入）。

        - ``True``：原样保留 rc（字段回放）。
        - ``"fold"``：rc 折叠进正文 ``<think>`` 块并去掉 rc 字段。已实测字段
          链路端到端可用的子类（minimax/deepseek）覆写本方法时 fold 可退化为
          字段回放；基类一律真折叠（未实测 provider 的字段可达性不可假设）。
        - ``False``：剥离 rc。
        """
        if effective is True:
            return [dict(m) for m in messages]
        if effective == REPLAY_MODE_FOLD:
            folded: list[dict[str, Any]] = []
            for m in messages:
                rc = m.get("reasoning_content")
                new_m = {k: v for k, v in m.items() if k != "reasoning_content"}
                if isinstance(rc, str) and rc.strip():
                    content = new_m.get("content")
                    think_block = f"<think>\n{rc}\n</think>\n"
                    new_m["content"] = (
                        think_block + content
                        if isinstance(content, str) and content
                        else think_block.rstrip("\n")
                    )
                folded.append(new_m)
            return folded
        return [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
