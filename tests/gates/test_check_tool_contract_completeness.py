# @feature: FP-GATE 工具契约完备度闸（output_schema/render/input_schema） | @ci: python-coverage
"""check_tool_contract_completeness.py 单测（input_schema 执法 + 双源回归）。

行为契约（断输入→输出，不钉实现）：
- 缺 input_schema / null / 空对象 {} → 计数 1 且明细点名工具（零参工具必须
  显式写 ``{"type": "object", "properties": {}}``，空对象等同未声明）；
- 合规工具（显式零参 schema / 有参 schema）→ 不计数；
- 三计数互不串扰：同一工具可同时缺 output_schema/render/input_schema；
- 基线未列 ``missing_input_schema`` 键时按 0 硬执法（只减不增协议）；
- 真仓负控制：检查器子进程实跑退出 0（当前 7 个工具已补齐，不得回退）。

另有本轮修复的回归面：7 个 http.handle 机制件 manifest ``input_schema`` 与其
代码内联 ``@plugin.tool(schema=...)`` 全等（G2 ``compare_tools`` 全等比对，
漂移即拒注册）；*.status 三件已迁 services 面（B 档迁面 2026-10-19），不在
工具面回归清单内。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS = REPO_ROOT / "plugins" / "shared"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_tool_contract_completeness", SCRIPTS / "check_tool_contract_completeness.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()

COMPLETE_TOOL = {
    "name": "ok_tool",
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {}},
    "render": {"card": "generic"},
}


def _manifest(*tools: dict) -> dict:
    return {"capabilities": {"tools": list(tools)}}


# ── input_schema 判定（多组有区分度输入） ─────────────────────────────


def test_missing_input_schema_is_flagged() -> None:
    """缺键与 null 都算违规；明细点名工具。"""
    s, r, i, details = mod.scan_manifest(
        _manifest(
            COMPLETE_TOOL,
            {"name": "no_schema", "output_schema": {"type": "object"}, "render": {"card": "generic"}},
            {
                "name": "null_schema",
                "input_schema": None,
                "output_schema": {"type": "object"},
                "render": {"card": "generic"},
            },
        ),
        "plugins/shared/x/plugin.json",
    )
    assert (s, r, i) == (0, 0, 2)
    assert any("no_schema" in d and "缺 input_schema" in d for d in details)
    assert any("null_schema" in d and "缺 input_schema" in d for d in details)


def test_empty_input_schema_object_is_flagged() -> None:
    """空对象等同未声明（内核 K9 以 {} 补注册 = 盲调形态）；显式零参 schema 合规。"""
    zero_param = {
        "name": "zero_param",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "render": {"card": "generic"},
    }
    empty = {
        "name": "empty_schema",
        "input_schema": {},
        "output_schema": {"type": "object"},
        "render": {"card": "generic"},
    }
    s, r, i, details = mod.scan_manifest(_manifest(zero_param, empty), "m.json")
    assert (s, r, i) == (0, 0, 1)
    assert any("empty_schema" in d for d in details)
    assert not any("zero_param" in d for d in details)


def test_three_counters_are_independent() -> None:
    """同一工具可同时缺三键；三计数互不吞并。"""
    s, r, i, details = mod.scan_manifest(_manifest({"name": "bare"}), "m.json")
    assert (s, r, i) == (1, 1, 1)
    assert sum("缺 input_schema" in d for d in details) == 1


# ── 基线协议 ────────────────────────────────────────────────────────


def test_baseline_defaults_input_schema_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """基线文件未列 missing_input_schema 键 → 按 0 硬执法（无需改 .github 基线）。"""
    baseline = tmp_path / "tool-contract-baseline.txt"
    baseline.write_text("missing_output_schema=1\nmissing_render=2\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_PATH", baseline)
    assert mod.load_baseline() == (1, 2, 0)


# ── 真仓负控制 ──────────────────────────────────────────────────────


def test_repo_gate_is_green() -> None:
    """真仓实跑退出 0 且 input_schema 违规为 0（防闸退化成恒绿假闸的负控制）。"""
    proc = subprocess.run(
        [sys.executable, "scripts/check_tool_contract_completeness.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "缺 input_schema 0" in proc.stdout


# ── 本轮修复回归：10 个工具 manifest ↔ 内联 schema 全等 ─────────────


def _manifest_tool(plugin_id: str, tool_name: str) -> dict:
    manifest = json.loads((PLUGINS / plugin_id / "plugin.json").read_text(encoding="utf-8"))
    tool = next(t for t in manifest["capabilities"]["tools"] if t["name"] == tool_name)
    return tool


def _inline_schema(plugin_id: str, tool_name: str) -> dict:
    for py in (PLUGINS / plugin_id).glob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, ValueError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "tool"):
                continue
            name = schema = None
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
                if kw.arg == "schema" and isinstance(kw.value, ast.Dict):
                    schema = ast.literal_eval(kw.value)
            if name == tool_name and isinstance(schema, dict):
                return schema
    raise AssertionError(f"{plugin_id}.{tool_name} 未找到内联 schema 字面量")


HTTP_FIELDS = {"path", "method", "plugin_id", "raw_body", "headers", "query"}


@pytest.mark.parametrize(
    "plugin_id,tool_name,expected_fields",
    [
        ("db_admin", "http.handle", HTTP_FIELDS | {"query_multi"}),
        ("metrics_admin", "http.handle", HTTP_FIELDS),
        ("user_admin", "http.handle", HTTP_FIELDS),
        ("security_check", "http.handle", HTTP_FIELDS | {"query_multi"}),
        ("sync_agent", "http.handle", HTTP_FIELDS | {"query_multi"}),
        ("sync_relay", "http.handle", HTTP_FIELDS | {"query_multi"}),
        ("trace_ingestion", "http.handle", HTTP_FIELDS | {"query_multi"}),
    ],
)
def test_aligned_tool_schema_is_declared_and_dual_source_equal(
    plugin_id: str, tool_name: str, expected_fields: set[str]
) -> None:
    """7 个 http.handle 机制件：manifest 声明非空 input_schema、字段集与
    handler 契约一致、且与代码内联 schema 全等（G2 注册闸全等比对）。

    B 档迁面注（2026-10-19）：db_admin/metrics_admin/user_admin 的 *.status
    三件已迁 capabilities.services（诊断件退出 LLM 工具面），不再受 G2 注册
    闸约束，故从本回归清单摘除。
    """
    tool = _manifest_tool(plugin_id, tool_name)
    schema = tool.get("input_schema")
    assert isinstance(schema, dict) and schema, f"{plugin_id}.{tool_name} manifest 缺 input_schema"
    assert set(schema.get("properties") or {}) == expected_fields
    assert schema == _inline_schema(plugin_id, tool_name), (
        f"{plugin_id}.{tool_name} 双源漂移：G2 注册闸将拒注册；修复须双面同改"
    )


def test_http_handle_object_fields_state_their_shape() -> None:
    """http.handle 的 headers/query（及声明了的 query_multi）必须表态
    （additionalProperties），否则 schema 驱动的修复层对其失明（R1）。"""
    for plugin_id in (
        "db_admin",
        "metrics_admin",
        "user_admin",
        "security_check",
        "sync_agent",
        "sync_relay",
        "trace_ingestion",
    ):
        props = _manifest_tool(plugin_id, "http.handle")["input_schema"]["properties"]
        for field in ("headers", "query", "query_multi"):
            if field in props:
                assert props[field].get("additionalProperties") is not None, (
                    f"{plugin_id}.http.handle.{field} 自由 object 未表态"
                )
