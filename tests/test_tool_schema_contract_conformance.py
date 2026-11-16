# @feature: FP-0.2.二 工具 schema 契约对齐 | @ci: python-coverage
"""工具契约符合性测试——修复层与真实工具「两边相碰」。

背景（2026-10-16 工具调用形态修复 ADR）：修复层（tool_schema_validator）
解析产物必须与真实工具侧契约相容，且三处声明面（manifest input_schema /
代码内联 @plugin.tool schema / 工具函数签名注解）必须一致。本文件用
**真实 plugin.json + 真实源码 AST**（非手抄副本，杜绝测试自证）：

1. 全链相碰：真实 manifest schema → validator 修复 → task_submit 真实
   归一化入口（实证事故形态 {"files": {"item": 路径}} 直达通过）；
2. map 字段相碰：真实 manifest → validator 修复出 dict → 工具函数签名
   注解（AST 提取）为 dict 形；
3. 双源一致（定向回归）：本轮对齐过的 6 个工具，manifest 与内联 schema
   全等（全仓扫描执法在 scripts/check_tool_schema_contracts.py 闸）。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_PLUGINS = _ROOT / "plugins" / "shared"

from tests._pipeline_plugin_path import add_plugin_dir  # noqa: E402

add_plugin_dir("tool_schema_validator")
from plugin import ToolSchemaValidator as _Validator  # noqa: E402

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402


def _manifest_schema(plugin_id: str, tool_name: str) -> dict[str, Any]:
    manifest = json.loads((_PLUGINS / plugin_id / "plugin.json").read_text(encoding="utf-8"))
    schema = next(
        (t.get("input_schema") for t in manifest["capabilities"]["tools"] if t["name"] == tool_name),
        None,
    )
    assert schema is not None, f"{plugin_id}.{tool_name} manifest 未声明 input_schema"
    return schema


def _validate_args_with(schema: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """真实 validator + 真实 manifest schema 走一轮修复，返回 (修复后 args, updates)。"""
    add_plugin_dir("tool_schema_validator")
    from plugin import ToolSchemaValidator

    validator = ToolSchemaValidator()
    ctx = PluginContext(
        state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "conformance_probe", "args": args, "id": "c1"}],
            "messages": [],
        },
        _services={
            "tool_registry": type(
                "_R", (), {"list_all": staticmethod(lambda: [type("_T", (), {"name": "conformance_probe", "input_schema": schema})()])}
            )()
        },
    )
    updates = asyncio.run(validator.execute(ctx)).state_updates
    remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
    assert remaining, "调用不得被丢弃（预填拒绝也保留）"
    assert "pre_decided_results" not in updates, f"修复层不应拦截: {updates.get('pre_decided_results')}"
    return remaining[0]["args"], updates


def _annotation_sources(plugin_id: str, func_name: str, source_file: str = "server.py") -> dict[str, str]:
    """AST 提取工具函数参数注解源码文本（不 import 重依赖模块）。"""
    tree = ast.parse((_PLUGINS / plugin_id / source_file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return {a.arg: ast.unparse(a.annotation) for a in node.args.args if a.annotation}
    raise AssertionError(f"{plugin_id}.{source_file} 未找到函数 {func_name}")


# ── 1. 全链相碰：manifest → validator → task_submit 真实归一化 ──────────


def _load_task_submit() -> Any:
    mod_name = "task_submit_conformance_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    system_root = _PLUGINS
    for d in (system_root, system_root / "tasks"):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGINS / "task_submit" / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_incident_form_end_to_end_through_real_layers() -> None:
    """实证事故形态走全链：真实 manifest schema → validator 修复 →
    task_submit 真实归一化接受并展开逐文件检查。"""
    schema = _manifest_schema("task_submit", "task_submit")
    repaired, _ = _validate_args_with(
        schema,
        {
            # 2026-10-19 参数削减后的新契约：goal 单字符串主形态（required 2），
            # target_type 已内化（enum 仅 agent）；扁平字段降为可选细控形态。
            "goal": (
                "插件分层治理方案：出厂只读 + 用户可写双根\n"
                "产出一份可落地的插件分层治理方案文档"
            ),
            "target_id": "research_agent",
            "acceptance_criteria": {"files": {"item": "docs/plugin_layering_plan.md"}},
        },
    )
    mod = _load_task_submit()
    normalized, fail = mod.TaskSubmitTool._normalize_acceptance_criteria(
        repaired["acceptance_criteria"]
    )
    assert fail is None, f"工具侧仍拒绝修复产物: {fail.error if fail else ''}"
    assert set(normalized.keys()) == {"file_check::docs/plugin_layering_plan.md"}


# ── 2. map 字段相碰：修复产物 vs 工具函数签名注解 ─────────────────────


@pytest.mark.parametrize(
    "plugin_id,tool_name,func_name,field,source_file,annotation_param",
    [
        ("triggers_ext", "http.handle", "http_handle", "headers", "server.py", ""),
        ("triggers_ext", "http.handle", "http_handle", "query", "server.py", ""),
        ("e2e_lifecycle_probe", "http.handle", "http_handle", "headers", "server.py", ""),
        # web_ext server 入口是裸 **kwargs（无逐字段注解），真实逐字段契约
        # 在 tool.py 的 WebTool（_merge_headers 等），相碰点对到该文件；
        # 注解参数名为 caller_headers（实现侧命名），用 annotation_param 对上。
        ("web_ext", "web_operate", "_merge_headers", "headers", "tool.py", "caller_headers"),
    ],
)
def test_pairs_list_repair_conforms_to_signature(
    plugin_id: str, tool_name: str, func_name: str, field: str, source_file: str, annotation_param: str
) -> None:
    """name/value 对列表经真实 manifest schema 修复为 dict，且该字段在真实
    工具实现签名上注解为 dict 形（两边相碰：修复产物 ⊆ 实现契约）。"""
    schema = _manifest_schema(plugin_id, tool_name)
    args: dict[str, Any] = {field: [{"name": "A", "value": "1"}]}
    # 探针补齐 manifest 声明的 required（被测字段之外的挡路必填）
    args.update({k: {"action": "post", "url": "https://x"}.get(k, "") for k in schema.get("required", []) if k not in args})
    repaired, _ = _validate_args_with(schema, args)
    assert repaired[field] == {"A": "1"}, "修复层未按 map 契约收编对列表"
    annotations = _annotation_sources(plugin_id, func_name, source_file)
    param = annotation_param or field
    assert param in annotations, f"{plugin_id}.{source_file}:{func_name} 未注解 {param}"
    assert annotations[param].startswith("dict"), f"签名注解与 map 契约错位: {annotations[param]}"


def test_union_data_body_passthrough_conforms() -> None:
    """web_operate.data（union 声明）：列表体直通不被修坏，签名侧 data 经
    json=data 直传（union 声明与实现语义一致）。"""
    schema = _manifest_schema("web_ext", "web_operate")
    repaired, updates = _validate_args_with(
        schema, {"action": "post", "url": "https://x", "data": [{"a": 1}]}
    )
    assert repaired["data"] == [{"a": 1}], "union 字段被泛化解包修坏"
    assert "schema_fixes" not in updates
    source = (_PLUGINS / "web_ext" / "tool.py").read_text(encoding="utf-8")
    assert "json=data" in source, "实现侧 json=data 直传点缺失，union 声明失去依据"


# ── 3. 双源一致（定向回归：本轮对齐过的工具） ────────────────────────


def _inline_schema(plugin_id: str, tool_name: str) -> dict[str, Any] | None:
    for py in (_PLUGINS / plugin_id).glob("*.py"):
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
    return None


@pytest.mark.parametrize(
    "plugin_id,tool_name",
    [
        ("triggers_ext", "http.handle"),
        ("e2e_lifecycle_probe", "http.handle"),
        ("web_ext", "web_operate"),
        # review_ops：schema 单源改为模块常量引用（非内联字面量，AST 闸跳过），
        # 双源全等由插件内 test_review_ops_manifest_lockstep 承载（全等口径同 R3）。
        ("review", "review_improvement"),
        ("mode_personal", "pim"),
    ],
)
def test_dual_source_equality_for_aligned_tools(plugin_id: str, tool_name: str) -> None:
    """本轮对齐过的工具：manifest 与内联 schema 全等（G2 compare_tools 全等
    比对，漂移即拒注册）。"""
    manifest_schema = _manifest_schema(plugin_id, tool_name)
    inline_schema = _inline_schema(plugin_id, tool_name)
    assert inline_schema is not None, f"{plugin_id}.{tool_name} 未找到内联 schema 字面量"
    assert inline_schema == manifest_schema, (
        f"{plugin_id}.{tool_name} 双源漂移：G2 注册闸将拒注册该工具；修复须双面同改"
    )
