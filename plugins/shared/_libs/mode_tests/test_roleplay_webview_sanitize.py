# @feature: FP-0.2.〇 RP面板刀-Ⅱ 卡内HTML/CSS消毒 | @vision: V1 可进化 | @ci: python-coverage
"""webview 渲染消毒 rpSanitizeHtml 行为断言（RP 面板刀-Ⅱ，交付 B/C）。

- 消毒函数为纯字符串实现（无 DOM 依赖），单源块（rp-sanitize:single-source）
  单源持方 = roleplay_panel.html（游戏页 serve-time include 装配同源；
  message 卡型页 2026-10-17 退役后无第二副本，parity 断言随之退役）；
- 消毒行为在 node 侧真实断言（抽函数执行恶意/良性输入——不允许"渲染时自然
  安全"的不可测说法；node 缺失 = 跳过，webview 无构建线）；
- 面板卡详情文本消毒接线结构性断言。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEBVIEW_DIR = (
    Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    / "mode_roleplay" / "webview"
)
_PANEL_HTML = WEBVIEW_DIR / "roleplay_panel.html"

_MARK = r"rp-sanitize:single-source-begin \*/([\s\S]*?)/\* rp-sanitize:single-source-end"

pytestmark = pytest.mark.unit


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_sanitize(src: str) -> str:
    match = re.search(_MARK, src)
    assert match is not None, "消毒单源块标记缺失"
    return match.group(1)


def _node() -> str | None:
    return shutil.which("node")


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    node = _node()
    assert node is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        return subprocess.run([node, path], capture_output=True, text=True, check=False)
    finally:
        Path(path).unlink(missing_ok=True)


def _sanitize_sandbox(script_body: str) -> str:
    """消毒函数源 + 断言体 → 单文件 node 脚本（用后即删，无工作区残留）。"""
    return _extract_sanitize(_source(_PANEL_HTML)) + "\n" + script_body


# ── 单源（panel 唯一副本）与整页语法自检 ─────────────────────────────────────


def test_sanitize_single_source_block_present() -> None:
    """单源块在 panel（serve-time 装配源）在场——退役卡型页后无第二副本。"""
    assert _extract_sanitize(_source(_PANEL_HTML)).strip() != ""


@pytest.mark.parametrize("path", [_PANEL_HTML], ids=["panel"])
def test_inline_script_syntax_valid(path: Path) -> None:
    """整页内联脚本 node --check（webview 无构建线的语法自检）。"""
    node = _node()
    if node is None:
        pytest.skip("node 不可用（webview 无构建线，语法自检择机补跑）")
    match = re.search(r"<script>(.*)</script>", _source(path), re.S)
    assert match is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(match.group(1))
        check_path = f.name
    try:
        run = subprocess.run(
            [node, "--check", check_path], capture_output=True, text=True, check=False
        )
        assert run.returncode == 0, f"{path.name} 内联脚本语法非法：{run.stderr}"
    finally:
        Path(check_path).unlink(missing_ok=True)


# ── 消毒行为真实断言（node 抽函数执行；输入经 JSON 字面量内联，无引号转义歧义）──

_MALICIOUS_CASES = [
    # (输入, 输出不得包含, 输出必须包含)
    ('<script>alert(1)</script>正文', ["<script", "alert(1)"], ["正文"]),
    ('<IMG SRC=x ONERROR=alert(1)>图', ["<img", "onerror", "alert(1)"], ["图"]),
    ('<div onclick="evil()">点</div>', ["onclick", "evil()"], ["<div>", "点", "</div>"]),
    ('<a href="javascript:alert(1)">链接</a>', ["javascript:", "href"], ["链接"]),
    ('<iframe src="http://evil.example"></iframe>外', ["<iframe", "evil.example"], ["外"]),
    ('<object data="http://evil.example">o</object>', ["<object", "evil.example"], []),
    ('<embed src="http://evil.example">', ["<embed", "evil.example"], []),
    ('<style>@import url("http://evil.example/x.css");</style>s', ["@import", "evil.example", "<style"], ["s"]),
    ('<div style="background:url(javascript:alert(1));color:blue">t</div>', ["url(", "javascript:"], ["<div", "color:blue", "t"]),
    ('<div style="width:expression(alert(1));height:2px">t</div>', ["expression"], ["<div", "height:2px"]),
    ('<div style="position:fixed;left:0;top:0">盖屏</div>', ["position:fixed"], ["盖屏"]),
    # CSS 转义序列绕过（评审修复刀）：\75rl( = url(、\66ixed = fixed——浏览器在
    # 剥除表 grep 之后才解码转义，字面裸文本匹配拦不住；值含 CSS 转义 → 整条剥除。
    ('<div style="background:\\75rl(//evil.example/x.png)">t</div>', ["evil.example", "\\75rl"], ["<div", "t"]),
    ('<div style="position:\\66ixed">盖屏</div>', ["\\66ixed"], ["盖屏"]),
    ('<div onmouseover=evil() title="x">t</div>', ["onmouseover", "evil"], ["<div", "t"]),
    ('<span style="color:red" class="prose" id="mainTitle">t</span>', ["class=", "id="], ["<span", "color:red", "t"]),
    ('<svg><script>alert(1)</script></svg>ok', ["<script", "alert(1)", "<svg"], ["ok"]),
    ('<!--<div>-->正文', ["<div>"], ["正文"]),
]

_BENIGN_CASES = [
    ('<b>粗体</b><i>斜体</i>', ["<b>粗体</b>", "<i>斜体</i>"]),
    ('<span style="color:red">红</span>', ["<span", 'style="color:red"', "红"]),
    ('<div class="statusbar"><ul><li>生命 100</li></ul></div>', ["<div>", "<ul><li>生命 100</li></ul>", "</div>"]),
    ('<details><summary>状态</summary><table><tr><td>HP</td></tr></table></details>',
     ["<details><summary>状态</summary>", "<table><tr><td>HP</td></tr></table>", "</details>"]),
    ('第一段<p>第二段</p>换行<br/>后', ["<p>第二段</p>", "<br>"]),
    ('纯文本无标签', ["纯文本无标签"]),
    ('你好 & <世界>', ["你好 &amp; &lt;世界&gt;"]),
    ('&amp;实体&lt;保留&gt;', ["&amp;实体&lt;保留&gt;"]),
]


def _assert_js(cond_expr: str, fail_tag: str, value_expr: str = "out") -> str:
    """生成一条 JS 断言（条件不满足 → exit(1) 并打印现场）。"""
    return (
        f"if (!({cond_expr})) {{ console.error('{fail_tag}: ' + {value_expr}); process.exit(1); }}\n"
    )


class TestSanitizeBehavior:
    """抽函数在 node 真实执行（消毒必须真实可测）。"""

    def _run_case(self, html: str, checks: str) -> None:
        if _node() is None:
            pytest.skip("node 不可用（webview 无构建线）")
        script = _sanitize_sandbox(
            f"var out = rpSanitizeHtml({json.dumps(html, ensure_ascii=False)});\n{checks}"
        )
        proc = _run_node(script)
        assert proc.returncode == 0, f"消毒断言失败: {proc.stderr or proc.stdout}"

    @pytest.mark.parametrize(
        ("html", "forbidden", "required"),
        _MALICIOUS_CASES,
        ids=[c[0][:24] for c in _MALICIOUS_CASES],
    )
    def test_malicious_forms_stripped(
        self, html: str, forbidden: list[str], required: list[str]
    ) -> None:
        checks = ""
        for piece in forbidden:
            checks += _assert_js(f"out.indexOf({json.dumps(piece)}) === -1", "LEAK")
        for piece in required:
            checks += _assert_js(f"out.indexOf({json.dumps(piece)}) !== -1", "MISS")
        self._run_case(html, checks)

    @pytest.mark.parametrize(
        ("html", "required"),
        _BENIGN_CASES,
        ids=[c[0][:24] for c in _BENIGN_CASES],
    )
    def test_benign_html_preserved(self, html: str, required: list[str]) -> None:
        checks = ""
        for piece in required:
            checks += _assert_js(f"out.indexOf({json.dumps(piece)}) !== -1", "MISS")
        self._run_case(html, checks)

    def test_empty_inputs(self) -> None:
        if _node() is None:
            pytest.skip("node 不可用（webview 无构建线）")
        script = _sanitize_sandbox(
            "if (rpSanitizeHtml(null) !== '') process.exit(1);\n"
            "if (rpSanitizeHtml(undefined) !== '') process.exit(1);\n"
            "if (rpSanitizeHtml('') !== '') process.exit(1);\n"
        )
        proc = _run_node(script)
        assert proc.returncode == 0, f"空输入断言失败: {proc.stderr}"


# ── 面板卡详情消毒接线（结构性轻量断言）────────────────────────────────────────


class TestPanelDetailSanitizeWiring:
    """交付 B 消费点：面板卡详情卡文本走 rpSanitizeHtml 注入。"""

    def test_card_text_fields_use_sanitize(self) -> None:
        src = _source(_PANEL_HTML)
        for call in (
            "rpSanitizeHtml(c.description)",
            "rpSanitizeHtml(c.personality)",
            "rpSanitizeHtml(c.scenario)",
            "rpSanitizeHtml(c.first_mes)",
            "rpSanitizeHtml(c.mes_example)",
            "rpSanitizeHtml(c.creator_notes)",
        ):
            assert call in src, f"卡详情 {call} 未接消毒"

    def test_alternate_greetings_use_sanitize(self) -> None:
        src = _source(_PANEL_HTML)
        assert "rpSanitizeHtml(g)" in src, "备选开场白走消毒注入"

    def test_non_text_fields_still_escaped(self) -> None:
        """非卡文本字段（名称/chips/桥错误等）仍走 escapeHtml（零扩散）。"""
        src = _source(_PANEL_HTML)
        assert "escapeHtml(b ? b.name : id)" in src
