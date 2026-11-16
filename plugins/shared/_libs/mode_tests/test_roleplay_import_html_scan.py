# @feature: FP-0.2.〇 RP面板刀-Ⅱ 卡内HTML/CSS消毒 | @vision: V1 可进化 | @ci: python-coverage
"""卡 import 高危 HTML 形态扫描（RP 面板刀-Ⅱ，交付 A）。

- 命中面：description/personality/scenario/first_mes/alternate_greetings 及
  extensions 各文本值（嵌套收集），逐形态独立 warnings（script 标签/事件属性
  on*=/javascript: URL/嵌入元素 iframe·object·embed/style 标签远程引入）；
- 数据保留：命中只进 warnings（与 suspicious 指令同一通道，不新增响应键），
  卡资产原样落盘（import 不清洗——渲染面消毒在 webview rpSanitizeHtml）；
- 良性 HTML（<b>/<span style> 等）不告警；无高危形态 = 响应零新增键。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（server maturity 测试同款真实解析链）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay/data"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_html_scan_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离（AGENTOS_USER_CONFIG_DIR → tmp 解析链）。"""
    user_config = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_config))
    module = _load_server()
    module.reset_providers()
    module.user_config = user_config
    return module


def _call(module: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return asyncio.run(module.http_handle(path=path, method=method, raw_body=raw_body, query={}))


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _post(module: Any, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
    return _call(module, f"{_EP}{suffix}", method="POST", raw_body=json.dumps(body))


def _import(module: Any, data: dict[str, Any], filename: str = "html_scan.json") -> dict[str, Any]:
    envelope = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    payload = base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return _body_json(_post(module, "/cards/import", {"file_b64": payload, "filename": filename}))


def _card_by_id(module: Any, card_id: str) -> dict[str, Any]:
    cards = _body_json(_call(module, f"{_EP}/cards"))["cards"]
    return next(c for c in cards if c["id"] == card_id)


def _assert_html_warning(warnings: list[str], label: str) -> None:
    matched = [w for w in warnings if "高危 HTML" in w and label in w]
    assert matched, f"缺 {label} 的 HTML 高危警告: {warnings}"


class TestHighRiskHtmlForms:
    """各高危形态独立命中（parametrize 跨形态 × 落点字段区分度）。"""

    @pytest.mark.parametrize(
        ("snippet", "label", "field"),
        [
            ('<script>alert(1)</script>', "script 标签", "description"),
            ('<img src=x onerror="alert(1)">', "事件属性", "personality"),
            ('点我 <a href="javascript:alert(1)">链接</a>', "javascript: URL", "scenario"),
            ('<iframe src="http://evil.example"></iframe>', "嵌入元素", "first_mes"),
            ('<object data="http://evil.example"></object>', "嵌入元素", "alternate_greetings"),
            ('<embed src="http://evil.example">', "嵌入元素", "alternate_greetings"),
            ('<style>@import url("http://evil.example/x.css");</style>', "style 标签", "description"),
        ],
        ids=["script-desc", "onerror-personality", "jsurl-scenario", "iframe-firstmes",
             "object-greet", "embed-greet", "style-tag-desc"],
    )
    def test_form_hits_warning_and_data_preserved(
        self, rp: Any, snippet: str, label: str, field: str
    ) -> None:
        """命中形态 → 专属 warnings；卡资产原样落盘（数据保留不清洗）。"""
        module = rp
        data: dict[str, Any] = {"name": "高危形态卡", "description": "正常描述"}
        if field == "alternate_greetings":
            data[field] = [snippet]
        else:
            data[field] = snippet
        body = _import(module, data)
        assert body["card_id"] == "html_scan"
        assert body["issues"] == []  # 只标注不阻断
        warnings = body.get("warnings") or []
        _assert_html_warning(warnings, label)
        # 数据保留：落盘卡该字段与输入逐字相等（import 不清洗）
        stored = _card_by_id(module, "html_scan")
        assert stored[field] == ([snippet] if field == "alternate_greetings" else snippet)

    def test_extensions_nested_text_values_scanned(self, rp: Any) -> None:
        """extensions 嵌套文本值（depth_prompt.prompt 深层）同样入扫描面。"""
        module = rp
        data = {
            "name": "扩展嵌套卡",
            "description": "正常描述",
            "extensions": {"depth_prompt": {"prompt": "<script>deep()</script>", "depth": 4}},
        }
        body = _import(module, data)
        _assert_html_warning(body.get("warnings") or [], "script 标签")
        # 数据保留：extensions 原样透传落盘
        stored = _card_by_id(module, "html_scan")
        assert stored["extensions"]["depth_prompt"]["prompt"] == "<script>deep()</script>"

    def test_multiple_forms_each_own_warning(self, rp: Any) -> None:
        """多形态同卡命中 → 逐形态独立 warnings（一形态至多一条）。"""
        module = rp
        data = {
            "name": "多形态卡",
            "description": '<script>a()</script><iframe src="http://x"></iframe>',
            "first_mes": "点击 <span onclick=\"evil()\">开始</span>",
        }
        body = _import(module, data)
        warnings = body.get("warnings") or []
        _assert_html_warning(warnings, "script 标签")
        _assert_html_warning(warnings, "嵌入元素")
        _assert_html_warning(warnings, "事件属性")
        assert len(warnings) == 3, f"一形态一条: {warnings}"


class TestBenignHtmlNoWarning:
    """良性 HTML 不告警（与高危形态区分度）。"""

    def test_benign_tags_and_inline_style_pass_silent(self, rp: Any) -> None:
        module = rp
        data = {
            "name": "良性HTML卡",
            "description": "<b>粗体</b>与<i>斜体</i>，<span style=\"color:red\">红色状态栏</span>",
            "first_mes": "<div class=\"status\"><ul><li>生命 100</li></ul></div>",
            "alternate_greetings": ["<p>段落</p>与换行"],
            "extensions": {"css_block": "<div style=\"background:linear-gradient(#fff,#000)\">美化块</div>"},
        }
        body = _import(module, data)
        assert "warnings" not in body, f"良性 HTML 不得告警: {body.get('warnings')}"

    def test_word_boundary_not_false_positive(self, rp: Any) -> None:
        """on*= 扫描词边界：person=/json= 等英文词内 on 不触发。"""
        module = rp
        data = {"name": "词边界卡", "description": "person=旅人, json={\"k\":1}, 来看 on = true 的散文"}
        body = _import(module, data)
        assert "warnings" not in body, f"词内 on 不该命中: {body.get('warnings')}"


class TestWarningsChannel:
    """与既有 suspicious/lint 同一 warnings 通道（响应键零新增）。"""

    def test_response_keys_unchanged(self, rp: Any) -> None:
        module = rp
        data = {"name": "键面卡", "description": "<script>x()</script>"}
        body = _import(module, data)
        assert set(body.keys()) <= {"card_id", "issues", "warnings"}

    def test_coexists_with_suspicious_warning(self, rp: Any) -> None:
        """可疑指令与高危 HTML 同卡命中 → 两条 warning 同通道合流。"""
        module = rp
        data = {"name": "合流卡", "description": "jailbreak 模式 <script>h()</script>"}
        body = _import(module, data)
        warnings = body.get("warnings") or []
        assert "检测到可疑指令模式，建议人工检查角色设定" in warnings
        _assert_html_warning(warnings, "script 标签")
        assert len(warnings) == 2

    def test_clean_card_has_no_warnings_key(self, rp: Any) -> None:
        """无高危形态（含 extensions）= 无 warnings 键（现状零变化）。"""
        module = rp
        data = {"name": "干净卡", "description": "正常描述", "extensions": {"talkativeness": "0.5"}}
        body = _import(module, data)
        assert "warnings" not in body
