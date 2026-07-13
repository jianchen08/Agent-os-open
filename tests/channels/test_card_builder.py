"""飞书卡片构建器测试。"""

from __future__ import annotations

from channels.feishu.card_builder import CardBuilder


class TestCardBuilder:
    """CardBuilder 测试。"""

    def test_build_empty_card(self) -> None:
        """测试构建空卡片。"""
        card = CardBuilder().build()
        assert "elements" in card
        assert card["elements"] == []

    def test_add_markdown(self) -> None:
        """测试添加 markdown 元素。"""
        card = CardBuilder().add_markdown("Hello **world**").build()
        assert len(card["elements"]) == 1
        elem = card["elements"][0]
        assert elem["tag"] == "div"
        assert elem["text"]["content"] == "Hello **world**"
        assert elem["text"]["tag"] == "lark_md"

    def test_add_action(self) -> None:
        """测试添加交互元素。"""
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Click"},
                "value": {"action": "click"},
            }
        ]
        card = CardBuilder().add_action(actions).build()
        assert len(card["elements"]) == 1
        elem = card["elements"][0]
        assert elem["tag"] == "action"
        assert len(elem["actions"]) == 1

    def test_add_header(self) -> None:
        """测试添加标题。"""
        card = CardBuilder().add_header("My Title").build()
        assert "header" in card
        assert card["header"]["title"]["content"] == "My Title"
        assert card["header"]["title"]["tag"] == "plain_text"

    def test_build_text_card_template(self) -> None:
        """测试预置文本卡片模板。"""
        card = CardBuilder.build_text_card(title="Greeting", content="Hi there")
        assert card["header"]["title"]["content"] == "Greeting"
        assert len(card["elements"]) == 1
        assert card["elements"][0]["text"]["content"] == "Hi there"

    def test_build_action_card_template(self) -> None:
        """测试预置操作卡片模板。"""
        buttons = [
            {"text": "OK", "value": {"action": "ok"}},
            {"text": "Cancel", "value": {"action": "cancel"}},
        ]
        card = CardBuilder.build_action_card(
            title="Confirm",
            content="Are you sure?",
            buttons=buttons,
        )
        assert card["header"]["title"]["content"] == "Confirm"
        # 第二个元素应该是 markdown 内容
        assert card["elements"][0]["text"]["content"] == "Are you sure?"
        # 第三个元素应该是 action
        action_elem = card["elements"][1]
        assert action_elem["tag"] == "action"
        assert len(action_elem["actions"]) == 2

    def test_fluent_api(self) -> None:
        """测试流式 API 链式调用。"""
        card = (
            CardBuilder()
            .add_header("Title")
            .add_markdown("Line 1")
            .add_markdown("Line 2")
            .build()
        )
        assert card["header"]["title"]["content"] == "Title"
        assert len(card["elements"]) == 2

    def test_build_returns_dict(self) -> None:
        """测试 build 返回可序列化的 dict。"""
        import json
        card = CardBuilder().add_markdown("test").build()
        serialized = json.dumps(card)
        assert isinstance(serialized, str)
