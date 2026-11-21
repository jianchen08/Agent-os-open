# @feature: FP-GATE 插件 webview 内联脚本语法闸 | @vision: V3 可嵌入 | @ci: python-coverage
"""check_webview_inline_script_syntax.py 单测（进化面板内联脚本语法错误回归锁）。

缺陷契约（2026-11-14 修复）：插件 webview 把全部渲染逻辑写在 `<script>` 内联块，
pytest 不解析 HTML、前端 lint/vitest 不收集 `plugins/**/webview/*.html`，所以内联
脚本语法错误两类车道都看不见——页面照常出静态骨架、数据区块全空白，只在真实浏览器
报 `SyntaxError`。本文件锁两件事：

- 检出能力：注入的语法错误必须被 check_html 判红并定位到行（防"检查器恒绿"）；
- 无误报：转义引号 / 模板串 / 正则内引号 / 双引号串内单引号等合法写法必须判绿；
- 真实面：本仓跟踪面内全部 webview 内联脚本当前全绿（进化面板那一处不再复发）。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

_NEED_NODE = pytest.mark.skipif(
    shutil.which("node") is None, reason="JS 语法解析需要 node（缺失时检查器 fail-closed 退出非零）"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_webview_inline_script_syntax", SCRIPTS / "check_webview_inline_script_syntax.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_webview_inline_script_syntax"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


def _write_html(tmp_path: Path, body: str, name: str = "panel.html") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


# ── 检出能力：注入的语法错误必须判红（多形态、含真实缺陷形态）──────────────

BROKEN_CASES = [
    # 真实缺陷形态：单引号串内嵌未转义单引号（进化面板第 483 行原样）
    '<p class="note">proposal(action=',
    # 双引号串内嵌未转义双引号
    'var s = "他说"好"就走";',
    # 括号未闭合
    'function f() { return 1;',
]


@pytest.mark.parametrize("broken", BROKEN_CASES)
@_NEED_NODE
def test_broken_inline_script_is_flagged(mod, tmp_path: Path, broken: str):
    """任一类语法错误都必须判红，且报出位置（文件:行）。"""
    if "action=" in broken:
        # 组装成完整缺陷行：引号提前闭合 → 后续 apply') 成为游离 token
        script = "el.innerHTML = '" + broken + "'apply') 落账）';"
    else:
        script = broken
    path = _write_html(tmp_path, f"<html><body>\n<script>\n{script}\n</script>\n</body></html>")
    findings = mod.check_html(path)
    assert findings, f"语法错误未被检出: {script!r}"
    assert str(path) in findings[0]
    assert ":2:" in findings[0], f"未定位到 <script> 块行号: {findings[0]}"


@_NEED_NODE
def test_broken_block_detected_among_multiple_blocks(mod, tmp_path: Path):
    """多脚本块中只有坏块判红（不因好块吞掉坏块，也不因坏块误报好块）。"""
    html = (
        "<html><body>\n"
        "<script>var ok1 = 'a';</script>\n"
        "<script>\nvar bad = 'x' + 'y'\nvar = ;\n</script>\n"
        "<script>var ok2 = 'b';</script>\n"
        "</body></html>"
    )
    path = _write_html(tmp_path, html)
    findings = mod.check_html(path)
    assert len(findings) == 1


# ── 无误报：合法写法必须判绿 ────────────────────────────────────────────

VALID_CASES = [
    # 转义引号（本次修复形态）
    r"""el.innerHTML = '<p>proposal(action=\'apply\') 落账</p>';""",
    # 双引号串内用单引号、单引号串内用双引号
    """var a = "proposal(action='apply')"; var b = 'say "hi"';""",
    # 模板串 + 内插
    "var n = 1; var s = `共 ${n} 条`;",
    # 正则字面量内含引号（esc() 同款）
    """var r = /[&<>"']/g;""",
]


@pytest.mark.parametrize("valid", VALID_CASES)
@_NEED_NODE
def test_valid_inline_script_passes(mod, tmp_path: Path, valid: str):
    """合法写法必须判绿——检查器不得靠"见引号就红"刷存在感。"""
    path = _write_html(tmp_path, f"<html><body>\n<script>\n{valid}\n</script>\n</body></html>")
    assert mod.check_html(path) == []


# ── 提取器契约：跳过非 JS 块，行号可定位 ────────────────────────────────

def test_extract_skips_src_and_non_js_blocks(mod):
    """外链脚本（src=）与数据模板块（type=application/json）不进解析面。"""
    html = (
        '<script src="https://x/y.js"></script>\n'
        '<script type="application/json">{"a": "b"}</script>\n'
        "<script>var a = 1;</script>\n"
        '<script type="module">const b = 2;</script>\n'
    )
    blocks = mod.extract_inline_scripts(html)
    assert len(blocks) == 2
    assert blocks[0][1] is False
    assert "var a = 1;" in blocks[0][2]
    assert blocks[1][1] is True
    assert "const b = 2;" in blocks[1][2]


def test_extract_reports_body_start_line(mod):
    """块行号 = `<script>` 标签在 HTML 中的实际行号（报错定位回文件用）。"""
    html = "<html>\n<head></head>\n<body>\n<script>\nvar a = 1;\nvar b = 2;\n</script>\n</body></html>"
    blocks = mod.extract_inline_scripts(html)
    assert [b[0] for b in blocks] == [4]


# ── 退出码契约：好坏文件分别 0 / 1（门禁语义，脚本可独立执行）──────────

@_NEED_NODE
def test_main_exit_codes(mod, tmp_path: Path):
    """好文件退出 0；坏文件退出 1 并打印定位行（CI 靠退出码判红）。"""
    good = _write_html(tmp_path, "<script>var a = 1;</script>", name="good.html")
    bad = _write_html(tmp_path, "<script>\nvar s = 'a'x';</script>", name="bad.html")
    assert mod.main([str(good)]) == 0
    assert mod.main([str(bad)]) == 1


def test_main_fails_when_no_targets(mod, monkeypatch):
    """空扫描面必须判红——防止改坏 glob 后"零文件即绿"（fail-closed）。"""
    monkeypatch.setattr(mod, "_git_webview_files", lambda: [])
    monkeypatch.setattr(mod, "DEFAULT_GLOBS", ())
    assert mod.collect_targets([]) == []
    assert mod.main([]) == 1


@_NEED_NODE
def test_main_fails_on_unreadable_path(mod, tmp_path: Path):
    """显式指定的路径不存在时判红，不因"没读到"而放行。"""
    missing = tmp_path / "nope.html"
    assert mod.main([str(missing)]) == 1


# ── 真实面：本仓全部 webview 内联脚本当前全绿（缺陷不复发）──────────────

@_NEED_NODE
def test_repo_webviews_all_pass(mod):
    """真实仓库面：跟踪面内全部 webview 内联脚本语法通过（含进化面板）。"""
    targets = mod.collect_targets([])
    names = {p.name for p in targets}
    assert "evolution_panel.html" in names, "进化面板未进入检查面，检查器扫描面失效"
    findings: list[str] = []
    for path in targets:
        findings.extend(mod.check_html(path))
    assert findings == []


@_NEED_NODE
def test_evolution_panel_defect_line_parses(mod):
    """回归锁：进化面板内联文案（proposal(action='apply')）现可解析。

    缺陷形态为引号内嵌引号——本用例直接对该文件做解析，断言面板脚本块通过，
    等价于"第 483 行那行字符串不再撕裂 script 块"。
    """
    panel = REPO_ROOT / "plugins/shared/mode_evolution/webview/evolution_panel.html"
    assert panel.is_file()
    assert mod.check_html(panel) == []
    body = "\n".join(b[2] for b in mod.extract_inline_scripts(panel.read_text(encoding="utf-8")))
    assert "只展示已生效提案" in body, "进化日志文案缺失（面板内容被误删/误改）"
