"""飞书卡片消息构建器。

提供流式 API 构建飞书卡片消息，兼容飞书消息卡片 JSON 格式。
支持添加标题、Markdown 内容、交互按钮等元素，并提供预置模板。

飞书卡片消息格式参考：
https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-components/overview

Example::

    card = (
        CardBuilder()
        .add_header("标题")
        .add_markdown("正文内容")
        .add_action([{"tag": "button", "text": {"tag": "plain_text", "content": "确认"}}])
        .build()
    )
"""

from __future__ import annotations

from typing import Any


class CardBuilder:
    """飞书卡片消息构建器。

    使用流式 API 逐步构建飞书卡片 JSON，最终通过 build() 产出
    可直接用于飞书发送卡片消息的字典。

    Example::

        card = CardBuilder.build_text_card(title="提示", content="操作成功")
    """

    def __init__(self) -> None:
        """初始化空卡片。"""
        self._header: dict[str, Any] | None = None
        self._elements: list[dict[str, Any]] = []

    def add_header(self, title: str, template: str = "default") -> CardBuilder:
        """添加卡片标题。

        Args:
            title: 标题文本
            template: 标题模板样式，默认 "default"

        Returns:
            self，支持链式调用
        """
        self._header = {
            "title": {
                "tag": "plain_text",
                "content": title,
            },
            "template": template,
        }
        return self

    def add_markdown(self, content: str) -> CardBuilder:
        """添加 Markdown 内容元素。

        Args:
            content: Markdown 格式文本

        Returns:
            self，支持链式调用
        """
        self._elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": content,
                },
            }
        )
        return self

    def add_action(self, actions: list[dict[str, Any]]) -> CardBuilder:
        """添加交互元素（按钮等）。

        Args:
            actions: 交互元素列表，每个元素为飞书卡片 action 格式

        Returns:
            self，支持链式调用
        """
        self._elements.append(
            {
                "tag": "action",
                "actions": actions,
            }
        )
        return self

    def add_hr(self) -> CardBuilder:
        """添加分割线。

        Returns:
            self，支持链式调用
        """
        self._elements.append({"tag": "hr"})
        return self

    def add_note(self, content: str) -> CardBuilder:
        """添加备注（底部灰色小字）。

        Args:
            content: 备注文本

        Returns:
            self，支持链式调用
        """
        self._elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": content,
                    }
                ],
            }
        )
        return self

    def build(self) -> dict[str, Any]:
        """构建卡片 JSON 字典。

        Returns:
            飞书卡片消息格式的字典，可直接传给 send_card 方法
        """
        card: dict[str, Any] = {"elements": self._elements}
        if self._header is not None:
            card["header"] = self._header
        return card

    # ── 预置模板 ──────────────────────────────────────

    @staticmethod
    def build_text_card(title: str, content: str) -> dict[str, Any]:
        """构建纯文本卡片模板。

        Args:
            title: 卡片标题
            content: 卡片正文

        Returns:
            飞书卡片 JSON 字典
        """
        return CardBuilder().add_header(title).add_markdown(content).build()

    @staticmethod
    def build_action_card(
        title: str,
        content: str,
        buttons: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """构建带操作按钮的卡片模板。

        Args:
            title: 卡片标题
            content: 卡片正文
            buttons: 按钮列表，每个按钮格式为
                {"text": "按钮文本", "value": {"action": "xxx"}}

        Returns:
            飞书卡片 JSON 字典
        """
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn.get("text", "")},
                "value": btn.get("value", {}),
            }
            for btn in buttons
        ]
        return CardBuilder().add_header(title).add_markdown(content).add_action(actions).build()
