# @feature: FP-0.2.二 工具 schema 契约对齐 | @ci: python-coverage
"""tool-schema-contract 闸「模块级 dict 形态」补盲回归锁（片1 复核查 2 盲区）。

缺陷真源 = docs/working/自主审查20261103/_修复复核_片1.md 查 2 附注：闸的 R3
只 AST 比对 ``@plugin.tool(schema=<字面 dict>)`` 装饰器形态；task_submit 以
BuiltinTool 类 + 模块级 dict（``Tool(input_schema=_TASK_SUBMIT_INPUT_SCHEMA)``）
注册，不在比对面——「红项不含 task_submit」对双源一致无证明力。

行为契约（断输入→输出，不钉实现）：
- 模块级字面 dict 经 Name 引用的两种形态（装饰器 schema=<Name>、类形态
  Tool(input_schema=<Name>，AnnAssign/Assign 均可）都必须被提取进比对面；
- 双源漂移场景现形：模块 dict ≠ manifest input_schema → VIOLATION 双源漂移
  （端到端子进程跑闸，exit 1 + 机器归因行）；
- 对齐场景不误红（同构造，两面全等 → 无该工具违规）；
- 形态无法静态执法时诚实计数不静默：动态构造（input_schema=<调用表达式>）
  与类内联 Dict 字面量均计入 unparsed 计数（后者属类内联字面量形态，执法
  面=下轮，ADR 遗留移交）；
- 防拟合：漂移例与对齐例同构造不同值（区分度在双源值本身，非构造差异）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_tool_schema_contracts as ctsc  # noqa: E402

pytestmark = pytest.mark.unit

_SCHEMA_A = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "查询词"}},
    "required": ["q"],
}
# 漂移面：同构造不同值（多一个 manifest 没有的字段）——区分度在值本身
_SCHEMA_A_DRIFTED = {
    "type": "object",
    "properties": {
        "q": {"type": "string", "description": "查询词"},
        "extra": {"type": "string", "description": "仅代码面有的字段"},
    },
    "required": ["q"],
}

_MANIFEST = {
    "id": "mini_probe",
    "capabilities": {
        "tools": [{"name": "probe_tool", "input_schema": _SCHEMA_A, "output_schema": {}, "render": "card"}]
    },
}

_SERVER_TEMPLATE = '''\
from typing import Any

{dict_decl}

def register(plugin):
    @plugin.tool(
        name="probe_tool",
        description="探针",
        schema={schema_ref},
    )
    async def probe_tool(ctx, inp):
        return {{}}
'''

_TOOL_TEMPLATE = '''\
from agentos_plugin_sdk.builtin_tool import BuiltinTool
from agentos_plugin_sdk.tool_types import Tool, ToolSource, ToolCategory, ToolLevel

{dict_decl}

class ProbeTool(BuiltinTool):
    """探针工具（task_submit 同构最小形）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="probe_tool",
            description="探针",
            input_schema={schema_ref},
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
        )
'''


def _make_plugin(root: Path, body: str) -> Path:
    """构造迷你插件：scan_root/shared/mini_probe/{plugin.json, server.py, tool.py}。"""
    pdir = root / "shared" / "mini_probe"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(json.dumps(_MANIFEST, ensure_ascii=False), encoding="utf-8")
    (pdir / "server.py").write_text(body, encoding="utf-8")
    (pdir / "tool.py").write_text("", encoding="utf-8")
    return root


class TestExtraction:
    """模块级字面 dict 经 Name 引用的两种形态都进比对面。"""

    def test_decorator_name_form_extracted(self, tmp_path):
        body = _SERVER_TEMPLATE.format(
            dict_decl="_SCHEMA: dict[str, Any] = " + json.dumps(_SCHEMA_A),
            schema_ref="_SCHEMA",
        )
        _make_plugin(tmp_path, body)
        schemas, skipped = ctsc._inline_tool_schemas(tmp_path / "shared" / "mini_probe")
        assert schemas.get("probe_tool") == _SCHEMA_A, "装饰器 schema=<模块级 dict> 必须被提取"
        assert skipped == 0

    def test_class_assign_and_annassign_extracted(self, tmp_path):
        # task_submit 同构：类形态 Tool(input_schema=<Name>)；AnnAssign 与 Assign 都算
        decls = (
            ("ann", "_PROBE_SCHEMA: dict[str, Any] = " + json.dumps(_SCHEMA_A)),
            ("assign", "_PROBE_SCHEMA = " + json.dumps(_SCHEMA_A)),
        )
        for tag, dict_decl in decls:
            root = tmp_path / tag
            body = _TOOL_TEMPLATE.format(dict_decl=dict_decl, schema_ref="_PROBE_SCHEMA")
            _make_plugin(root, body)
            schemas, skipped = ctsc._inline_tool_schemas(root / "shared" / "mini_probe")
            assert schemas.get("probe_tool") == _SCHEMA_A, f"类形态必须被提取：{dict_decl[:30]}…"
            assert skipped == 0

    def test_unparseable_forms_counted_not_silent(self, tmp_path):
        # 动态构造（调用表达式）与类内联 Dict 字面量：不执法但必须计数留痕
        root = tmp_path / "dynamic"
        body = _TOOL_TEMPLATE.format(
            dict_decl="def _build():\n    return " + json.dumps(_SCHEMA_A),
            schema_ref="_build()",
        )
        _make_plugin(root, body)
        schemas, skipped = ctsc._inline_tool_schemas(root / "shared" / "mini_probe")
        assert "probe_tool" not in schemas, "动态构造不可静态提取，不得入比对面"
        assert skipped == 1, "不可执法锚必须计入 unparsed 计数（禁静默）"

        root2 = tmp_path / "inline"
        body2 = _TOOL_TEMPLATE.format(dict_decl="", schema_ref=json.dumps(_SCHEMA_A))
        _make_plugin(root2, body2)
        schemas2, skipped2 = ctsc._inline_tool_schemas(root2 / "shared" / "mini_probe")
        assert "probe_tool" not in schemas2, "类内联 Dict 字面量本闸不执法（ADR 遗留移交）"
        assert skipped2 == 1, "类内联 Dict 锚必须计数留痕"


class TestDualSourceDrift:
    """双源漂移端到端现形（迷你模块场景 → 闸红 + 机器归因行）。"""

    def _run_gate(self, scan_root: Path) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_tool_schema_contracts.py"),
             "--scan-root", str(scan_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_drift_red_end_to_end(self, tmp_path):
        # 代码面模块 dict 多一个 manifest 没有的字段 = 双源漂移，闸必须现形
        root = tmp_path / "drift"
        body = _TOOL_TEMPLATE.format(
            dict_decl="_PROBE_SCHEMA: dict[str, Any] = " + json.dumps(_SCHEMA_A_DRIFTED),
            schema_ref="_PROBE_SCHEMA",
        )
        _make_plugin(root, body)
        rc, out = self._run_gate(root)
        assert rc == 1, "双源漂移必须 exit 1（盲区补后 task_submit 同构形态不再真空通过）"
        assert "VIOLATION: plugin=mini_probe probe_tool 双源漂移" in out, "必须有机器归因行"

    def test_aligned_green_no_false_red(self, tmp_path):
        # 同构造、两面全等：不得误红（区分度=双源值本身）
        root = tmp_path / "aligned"
        body = _TOOL_TEMPLATE.format(
            dict_decl="_PROBE_SCHEMA: dict[str, Any] = " + json.dumps(_SCHEMA_A),
            schema_ref="_PROBE_SCHEMA",
        )
        _make_plugin(root, body)
        rc, out = self._run_gate(root)
        assert rc == 0, f"对齐面不得误红：{out}"
        assert "violations=0" in out
