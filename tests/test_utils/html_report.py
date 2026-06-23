"""HTML 测试报告渲染。

从 TestReport 数据生成自包含的 HTML 报告页面，
包含通过率进度条、用例详情表格、环境信息等。

生产级升级：AC 状态矩阵、功能覆盖率、E2E 截图嵌入、总览仪表盘。

来源：docs/构建统一CICD测试体系_solution.md §3.4.2
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.test_utils.ac_matrix import ACMatrixTracker
    from tests.test_utils.feature_coverage import FeatureCoverageTracker
    from tests.test_utils.report_generator import TestReport

_STATUS_COLOR = {
    "passed": "#4caf50",
    "failed": "#f44336",
    "error": "#ff9800",
    "skipped": "#9e9e9e",
}

# ── 生产级 CSS（所有报告共用）────────────────────────────

_PRODUCTION_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; background: #f5f5f5; color: #333; }
.container { max-width: 1400px; margin: 0 auto; padding: 2rem; }
h1 { color: #333; margin-bottom: 0.25rem; }
h2 { color: #444; margin-top: 2rem; border-bottom: 2px solid #e0e0e0; padding-bottom: 0.5rem; }
.timestamp { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }

/* 总览仪表盘 */
.dashboard { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin: 1.5rem 0; }
.dash-card { background: white; padding: 1.25rem; border-radius: 10px;
             box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align: center; }
.dash-card .number { font-size: 2rem; font-weight: 700; }
.dash-card .label { color: #666; font-size: 0.85rem; margin-top: 0.25rem; }
.dash-card .sub { font-size: 0.75rem; color: #999; margin-top: 0.25rem; }

/* 进度条 */
.bar { height: 10px; border-radius: 5px; background: #e0e0e0; margin: 1rem 0; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 5px; transition: width 0.3s; }

/* 卡片 */
.summary { display: flex; gap: 1rem; margin: 1rem 0; flex-wrap: wrap; }
.card { background: white; padding: 1rem 1.5rem; border-radius: 8px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 100px; text-align: center; }
.card .number { font-size: 2rem; font-weight: bold; }
.card .label { color: #666; font-size: 0.9rem; }

/* 表格 */
table { width: 100%; border-collapse: collapse; background: white;
        border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        margin-bottom: 1.5rem; }
th { background: #333; color: white; padding: 0.75rem; text-align: left; font-size: 0.85rem; }
td { padding: 0.75rem; border-bottom: 1px solid #eee; font-size: 0.85rem; vertical-align: top; }
tr:hover { background: #fafafa; }

/* Bug 定位 & 错误 */
.bug-loc { font-family: monospace; font-size: 0.85rem; color: #d32f2f; }
.error-msg { font-family: monospace; font-size: 0.8rem; color: #666;
             margin-top: 0.25rem; white-space: pre-wrap; max-width: 600px; }

/* AC 矩阵 */
.section-header { display: flex; justify-content: space-between; align-items: center;
                  flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem; }
.ac-summary, .coverage-summary { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.ac-stat { padding: 0.25rem 0.75rem; border-radius: 12px; font-size: 0.8rem; font-weight: 500; }
.ac-id { font-family: monospace; font-weight: 600; }
.ac-badge { padding: 0.2rem 0.6rem; border-radius: 10px; font-size: 0.8rem; font-weight: 500;
            white-space: nowrap; }
.ac-tests { font-size: 0.75rem; color: #666; margin-top: 0.25rem; }
.ac-detail, .ac-evidence { font-size: 0.8rem; color: #555; margin-top: 0.15rem; }
.ac-table td:nth-child(3) { text-align: center; min-width: 90px; }

/* 功能覆盖率 */
.feature-tag { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
               font-size: 0.75rem; margin: 0.15rem; }
.feature-tag.tested { background: #e8f5e9; color: #2e7d32; }
.feature-tag.untested { background: #f5f5f5; color: #999; }
.cat-name { font-weight: 600; white-space: nowrap; min-width: 120px; }
.cat-progress { min-width: 140px; }
.mini-bar { height: 6px; border-radius: 3px; background: #e0e0e0; margin-bottom: 0.3rem; overflow: hidden; }
.mini-bar-fill { height: 100%; border-radius: 3px; }
.cat-features { max-width: 500px; }

/* E2E 截图画廊 */
.screenshot-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                      gap: 1rem; margin: 1rem 0; }
.screenshot-card { background: white; border-radius: 8px; overflow: hidden;
                   box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.screenshot-card img { width: 100%; display: block; cursor: pointer; }
.screenshot-card .caption { padding: 0.5rem 0.75rem; font-size: 0.8rem; color: #555; }

/* 环境信息 */
.env-table td:first-child { font-weight: 600; white-space: nowrap; }

/* 响应式 */
@media (max-width: 768px) {
    .dashboard { grid-template-columns: repeat(2, 1fr); }
    .container { padding: 1rem; }
}
"""


def render_html_report(report: TestReport) -> str:
    """渲染基础 HTML 测试报告（兼容原有调用）。"""
    rows_html = ""
    for case in report.test_cases:
        bug_html = ""
        if case.bug_location and case.bug_location.bug_candidates:
            top = case.bug_location.bug_candidates[0]
            bug_html = (
                f'<span class="bug-loc">🎯 {top.file_path}:{top.line_number}'
                f" in {top.function_name}</span>"
            )

        error_html = ""
        if case.error_message:
            escaped = (
                case.error_message.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            error_html = f'<div class="error-msg">{escaped[:300]}</div>'

        color = _STATUS_COLOR.get(case.outcome, "#333")
        rows_html += f"""
        <tr>
            <td><span style="color:{color};font-weight:bold">{case.outcome.upper()}</span></td>
            <td title="{case.node_id}">{case.name}</td>
            <td>{case.duration_ms:.0f}ms</td>
            <td>{bug_html}{error_html}</td>
        </tr>"""

    pass_pct = report.pass_rate * 100

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>测试报告 - {report.timestamp}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; background: #f5f5f5; }}
h1 {{ color: #333; }}
.summary {{ display: flex; gap: 1rem; margin: 1rem 0; }}
.card {{ background: white; padding: 1rem 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.card .number {{ font-size: 2rem; font-weight: bold; }}
.card .label {{ color: #666; font-size: 0.9rem; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
th {{ background: #333; color: white; padding: 0.75rem; text-align: left; }}
td {{ padding: 0.75rem; border-bottom: 1px solid #eee; }}
.bug-loc {{ font-family: monospace; font-size: 0.85rem; color: #d32f2f; }}
.error-msg {{ font-family: monospace; font-size: 0.8rem; color: #666; margin-top: 0.25rem; white-space: pre-wrap; }}
.bar {{ height: 8px; border-radius: 4px; background: #e0e0e0; margin: 1rem 0; }}
.bar-fill {{ height: 100%; border-radius: 4px; background: linear-gradient(90deg, #4caf50 {pass_pct}%, #f44336 {pass_pct}%); width: 100%; }}
</style>
</head>
<body>
<h1>📊 测试报告</h1>
<p>生成时间: {report.timestamp}</p>

<div class="bar"><div class="bar-fill"></div></div>

<div class="summary">
  <div class="card"><div class="number" style="color:#333">{report.total}</div><div class="label">总计</div></div>
  <div class="card"><div class="number" style="color:#4caf50">{report.passed}</div><div class="label">通过</div></div>
  <div class="card"><div class="number" style="color:#f44336">{report.failed}</div><div class="label">失败</div></div>
  <div class="card"><div class="number" style="color:#ff9800">{report.errors}</div><div class="label">错误</div></div>
  <div class="card"><div class="number" style="color:#9e9e9e">{report.skipped}</div><div class="label">跳过</div></div>
  <div class="card"><div class="number">{pass_pct:.1f}%</div><div class="label">通过率</div></div>
  <div class="card"><div class="number">{report.duration_ms:.0f}ms</div><div class="label">总耗时</div></div>
</div>

<h2>测试用例详情</h2>
<table>
<tr><th>状态</th><th>测试名</th><th>耗时</th><th>详情</th></tr>
{rows_html}
</table>

<h2>环境信息</h2>
<table>
<tr><th>项目</th><th>值</th></tr>
{''.join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in report.environment.items())}
</table>
</body>
</html>"""


def _collect_screenshots_base64(screenshot_dir: str | Path) -> list[dict[str, str]]:
    """从目录收集截图并转为 base64。"""
    result: list[dict[str, str]] = []
    path = Path(screenshot_dir)
    if not path.exists():
        return result

    for img_path in sorted(path.rglob("*.png")):
        try:
            data = base64.b64encode(img_path.read_bytes()).decode("ascii")
            result.append(
                {
                    "name": img_path.stem,
                    "caption": img_path.stem.replace("-", " ").replace("_", " "),
                    "data_uri": f"data:image/png;base64,{data}",
                }
            )
        except (OSError, ValueError):
            continue
    return result


def _render_screenshots_html(screenshots: list[dict[str, str]]) -> str:
    """渲染截图画廊 HTML 片段。"""
    if not screenshots:
        return """
        <div class="screenshot-section">
            <div class="section-header"><h2>📸 E2E 截图画廊</h2></div>
            <p style="color:#999">暂无截图数据（运行 Playwright E2E 测试后自动生成）</p>
        </div>"""

    cards = ""
    for shot in screenshots:
        cards += f"""
        <div class="screenshot-card">
            <img src="{shot['data_uri']}" alt="{shot['name']}" loading="lazy" />
            <div class="caption">{shot['caption']}</div>
        </div>"""

    return f"""
    <div class="screenshot-section">
        <div class="section-header">
            <h2>📸 E2E 截图画廊</h2>
            <span style="color:#666;font-size:0.85rem">{len(screenshots)} 张截图</span>
        </div>
        <div class="screenshot-gallery">{cards}</div>
    </div>"""


def render_combined_report(
    backend_report: TestReport | None = None,
    frontend_data: dict[str, Any] | None = None,
    e2e_data: dict[str, Any] | None = None,
    ac_tracker: ACMatrixTracker | None = None,
    coverage_tracker: FeatureCoverageTracker | None = None,
    screenshot_dir: str | Path = "",
) -> str:
    """渲染生产级汇总 HTML 报告。

    Args:
        backend_report: 后端 pytest 测试报告。
        frontend_data: 前端 vitest 测试结果（含 total/passed/failed/duration_ms）。
        e2e_data: Playwright E2E 测试结果（含 total/passed/failed/duration_ms）。
        ac_tracker: AC 状态矩阵追踪器。
        coverage_tracker: 功能覆盖率追踪器。
        screenshot_dir: E2E 截图目录路径。

    Returns:
        自包含 HTML 字符串。
    """
    # ── 统计数据 ──
    backend_total = backend_report.total if backend_report else 0
    backend_passed = backend_report.passed if backend_report else 0
    backend_failed = backend_report.failed if backend_report else 0
    backend_duration = backend_report.duration_ms if backend_report else 0

    fe_total = frontend_data.get("total", 0) if frontend_data else 0
    fe_passed = frontend_data.get("passed", 0) if frontend_data else 0
    fe_failed = frontend_data.get("failed", 0) if frontend_data else 0
    fe_duration = frontend_data.get("duration_ms", 0) if frontend_data else 0

    e2e_total = e2e_data.get("total", 0) if e2e_data else 0
    e2e_passed = e2e_data.get("passed", 0) if e2e_data else 0
    e2e_failed = e2e_data.get("failed", 0) if e2e_data else 0
    e2e_duration = e2e_data.get("duration_ms", 0) if e2e_data else 0

    grand_total = backend_total + fe_total + e2e_total
    grand_passed = backend_passed + fe_passed + e2e_passed
    grand_failed = backend_failed + fe_failed + e2e_failed
    grand_pct = grand_passed / grand_total * 100 if grand_total > 0 else 0
    grand_duration = backend_duration + fe_duration + e2e_duration

    # ── AC 矩阵 HTML ──
    ac_html = ""
    if ac_tracker:
        ac_html = ac_tracker.to_html()

    # ── 功能覆盖率 HTML ──
    coverage_html = ""
    if coverage_tracker:
        coverage_html = coverage_tracker.to_html()

    # ── 截图 HTML ──
    screenshots = _collect_screenshots_base64(screenshot_dir) if screenshot_dir else []
    screenshots_html = _render_screenshots_html(screenshots)

    # ── 后端用例表格 ──
    backend_rows = ""
    if backend_report:
        for case in backend_report.test_cases:
            color = _STATUS_COLOR.get(case.outcome, "#333")
            error_html = ""
            if case.error_message:
                escaped = (
                    case.error_message.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                error_html = f'<div class="error-msg">{escaped[:200]}</div>'
            backend_rows += f"""
            <tr>
                <td><span style="color:{color};font-weight:bold">{case.outcome.upper()}</span></td>
                <td title="{case.node_id}">{case.name}</td>
                <td>{case.duration_ms:.0f}ms</td>
                <td>{error_html}</td>
            </tr>"""

    # ── 前端用例表格 ──
    frontend_rows = ""
    if frontend_data and frontend_data.get("test_cases"):
        for tc in frontend_data["test_cases"]:
            color = _STATUS_COLOR.get(tc.get("outcome", ""), "#333")
            frontend_rows += f"""
            <tr>
                <td><span style="color:{color};font-weight:bold">{tc.get('outcome', '?').upper()}</span></td>
                <td>{tc.get('name', '')}</td>
                <td>{tc.get('duration_ms', 0):.0f}ms</td>
                <td>{tc.get('error_message', '')}</td>
            </tr>"""

    # ── E2E 用例表格 ──
    e2e_rows = ""
    if e2e_data and e2e_data.get("test_cases"):
        for tc in e2e_data["test_cases"]:
            color = _STATUS_COLOR.get(tc.get("outcome", ""), "#333")
            e2e_rows += f"""
            <tr>
                <td><span style="color:{color};font-weight:bold">{tc.get('outcome', '?').upper()}</span></td>
                <td>{tc.get('name', '')}</td>
                <td>{tc.get('duration_ms', 0):.0f}ms</td>
                <td>{tc.get('error_message', '')}</td>
            </tr>"""

    # ── 环境信息 ──
    env_rows = ""
    if backend_report:
        for k, v in backend_report.environment.items():
            env_rows += f"<tr><td>{k}</td><td>{v}</td></tr>"

    # ── 总进度条颜色 ──
    bar_color = "#4caf50" if grand_pct >= 80 else "#ff9800" if grand_pct >= 50 else "#f44336"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>灵汐系统 — 测试总报告</title>
<style>{_PRODUCTION_CSS}</style>
</head>
<body>
<div class="container">

<h1>📊 灵汐系统测试总报告</h1>
<p class="timestamp">生成时间: {backend_report.timestamp if backend_report else 'N/A'}</p>

<!-- ── 总览仪表盘 ── -->
<div class="bar"><div class="bar-fill" style="width:{grand_pct:.1f}%;background:{bar_color}"></div></div>

<div class="dashboard">
  <div class="dash-card">
    <div class="number" style="color:#333">{grand_total}</div>
    <div class="label">总测试数</div>
    <div class="sub">后端 {backend_total} / 前端 {fe_total} / E2E {e2e_total}</div>
  </div>
  <div class="dash-card">
    <div class="number" style="color:#4caf50">{grand_passed}</div>
    <div class="label">总通过</div>
    <div class="sub">后端 {backend_passed} / 前端 {fe_passed} / E2E {e2e_passed}</div>
  </div>
  <div class="dash-card">
    <div class="number" style="color:#f44336">{grand_failed}</div>
    <div class="label">总失败</div>
    <div class="sub">后端 {backend_failed} / 前端 {fe_failed} / E2E {e2e_failed}</div>
  </div>
  <div class="dash-card">
    <div class="number" style="color:{bar_color}">{grand_pct:.1f}%</div>
    <div class="label">总通过率</div>
    <div class="sub">总耗时 {grand_duration:.0f}ms</div>
  </div>
</div>

<!-- ── AC 状态矩阵 ── -->
{ac_html}

<!-- ── 功能覆盖率矩阵 ── -->
{coverage_html}

<!-- ── 后端测试用例详情 ── -->
<h2>🧪 后端测试用例</h2>
<p style="color:#666;font-size:0.85rem">共 {backend_total} 个测试，通过 {backend_passed}，失败 {backend_failed}</p>
<table>
<tr><th>状态</th><th>测试名</th><th>耗时</th><th>详情</th></tr>
{backend_rows}
</table>

<!-- ── 前端 Vitest 用例 ── -->
<h2>⚛️ 前端 Vitest 测试</h2>
<p style="color:#666;font-size:0.85rem">共 {fe_total} 个测试，通过 {fe_passed}，失败 {fe_failed}</p>
<table>
<tr><th>状态</th><th>测试名</th><th>耗时</th><th>详情</th></tr>
{frontend_rows}
</table>

<!-- ── E2E 浏览器测试用例 ── -->
<h2>🌐 E2E 浏览器测试</h2>
<p style="color:#666;font-size:0.85rem">共 {e2e_total} 个测试，通过 {e2e_passed}，失败 {e2e_failed}</p>
<table>
<tr><th>状态</th><th>测试名</th><th>耗时</th><th>详情</th></tr>
{e2e_rows}
</table>

<!-- ── E2E 截图画廊 ── -->
{screenshots_html}

<!-- ── 环境信息 ── -->
<h2>🔧 环境信息</h2>
<table class="env-table">
<tr><th>项目</th><th>值</th></tr>
{env_rows}
</table>

</div>
</body>
</html>"""


__all__ = ["render_html_report", "render_combined_report"]
