#!/usr/bin/env python3
"""工具契约静态闸（2026-10-16，工具调用形态修复 ADR 的执法面）。

统一三件事（同日 ADR 2026-10-16-tool-call-shape-repair「修复层与工具契约
对齐」节）：
- **R1 自由 object 表态**：``input_schema`` 里 ``type: object`` 且无
  ``properties`` 的字段，必须声明 ``additionalProperties``（map 形表态）或
  携带非空 ``description``（自描述自由形态）——否则 schema 驱动的修复层
  对它失明或误修（headers 被「单元素数组→取元素」修坏成
  ``{"name":..., "value":...}`` 的实证）。
- **R2 同名同形**：不同插件的同名工具、同名字段，声明了 ``input_schema``
  就必须全等——G2 注册闸 ``compare_tools`` 对 manifest schema 与 SDK 上报
  schema 做全等比对（verify.rs），双源漂移 = 工具被拒注册（形式修复也要
  双面同改，漏一面即事故）。
- **R3 双源一致**：manifest ``input_schema`` vs 代码注册锚 schema 全等（R2
  的仓库侧执法位）。锚形态两种：``@plugin.tool(schema=...)`` 字面量/模块级
  dict Name 引用、类形态 ``Tool(input_schema=<模块级 dict Name>)``（task_submit
  形态，2026-11-04 补盲——此前只比装饰器字面量，类形态真空通过）。
  不可静态提取的锚（动态构造/类内联 Dict 字面量）跳过不报不猜，仅计数留痕
  （unparsed_skipped）。

协议（与 check_service_declarations.py 同款，plugin_check 安装期委托）：
- 违规行 ``VIOLATION: plugin=<目录> <详情>``（机器归因）；
- exit 0 全绿 / 1 有违规 / 2 fail-closed（manifest 不可解析）；
- 扫描面：``--scan-root``（默认仓内 plugins/）下 ``shared/<id>/plugin.json``
  + 同目录生产码（tests/test_*/conftest/__pycache__ 剪枝）。

用法：
    python scripts/check_tool_schema_contracts.py                    # 全仓执法（CI 闸）
    python scripts/check_tool_schema_contracts.py --scan-root <dir>  # 仓外语料
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = ROOT / "plugins"

_SKIP_DIR_PARTS = {"__pycache__", "node_modules", ".venv", "tests"}


def _is_test_py(path: Path) -> bool:
    if any(part in _SKIP_DIR_PARTS for part in path.parts):
        return True
    return path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py"


def _module_literal_dicts(tree: ast.Module) -> dict[str, dict]:
    """模块顶层 Assign/AnnAssign 的纯字面 dict 常量表（变量名 → dict 值）。

    只收 ast.literal_eval 可提的顶层字面 dict——含 Name 引用/调用构造的
    不可静态提取，不入表（引用侧落 unparsed 计数，禁静默）。
    """
    out: dict[str, dict] = {}
    for node in tree.body:
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if not (isinstance(t, ast.Name) and isinstance(value, ast.Dict)):
                continue
            try:
                d = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict):
                out[t.id] = d
    return out


def _inline_tool_schemas(plugin_dir: Path) -> tuple[dict[str, dict], int]:
    """AST 提取工具注册锚的 schema（两种形态）。

    - 形态 A（装饰器）：``@plugin.tool(name=..., schema=<字面 dict>或<模块级
      dict Name>)``——SDK 插件工具的注册锚；
    - 形态 B（类，2026-11-04 补盲）：``Tool(name=..., input_schema=<模块级
      dict Name>)``——BuiltinTool 类工具（task_submit 形态）的注册锚，此前
      不在比对面=双源漂移真空（片1 复核查 2）。

    不可静态提取的锚（动态构造表达式/类内联 Dict 字面量/未解析 Name）诚实
    跳过并计数——「测不了」显性化也是补洞，禁静默通过。

    Returns:
        (工具名 → 内联 schema 字面量, unparsed 跳过计数)
    """
    schemas: dict[str, dict] = {}
    skipped = 0
    for py in plugin_dir.glob("*.py"):
        if _is_test_py(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, ValueError):
            skipped += 1
            continue
        literals = _module_literal_dicts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kws = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            name_v = kws.get("name")
            if not (isinstance(name_v, ast.Constant) and isinstance(name_v.value, str)):
                continue
            name = name_v.value
            # 形态 A：装饰器 @plugin.tool(..., schema=...)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "tool" and "schema" in kws:
                schema_v = kws["schema"]
                if isinstance(schema_v, ast.Dict):
                    try:
                        schema = ast.literal_eval(schema_v)
                    except (ValueError, SyntaxError):
                        schema = None
                    if isinstance(schema, dict):
                        schemas.setdefault(name, schema)
                    else:
                        skipped += 1
                elif isinstance(schema_v, ast.Name):
                    resolved = literals.get(schema_v.id)
                    if isinstance(resolved, dict):
                        schemas.setdefault(name, resolved)
                    else:
                        skipped += 1
                else:
                    skipped += 1
                continue
            # 形态 B：类形态 Tool(..., input_schema=...)（任意带 name+input_schema
            # 的调用——BuiltinTool.get_tool_definition 出口即此形态）
            if "input_schema" in kws:
                schema_v = kws["input_schema"]
                if isinstance(schema_v, ast.Name):
                    resolved = literals.get(schema_v.id)
                    if isinstance(resolved, dict):
                        schemas.setdefault(name, resolved)
                    else:
                        skipped += 1
                else:
                    # 动态构造 / 类内联 Dict 字面量：本闸不执法，计数留痕
                    skipped += 1
    return schemas, skipped


def _iter_manifest_tools(scan_root: Path) -> tuple[list[tuple[Path, str, dict]], int]:
    """收集 (manifest 路径, 插件目录名, 工具名 → input_schema)。"""
    tools: list[tuple[Path, str, dict]] = []
    broken = 0
    for pjson in sorted(scan_root.glob("shared/*/plugin.json")):
        try:
            manifest = json.loads(pjson.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            broken += 1
            continue
        table = {
            t["name"]: t["input_schema"]
            for t in manifest.get("capabilities", {}).get("tools", [])
            if t.get("input_schema") is not None
        }
        tools.append((pjson, pjson.parent.name, table))
    return tools, broken


def _check_stance(plugin: str, tool_name: str, schema: dict, violations: list[str]) -> None:
    """R1：自由 object 字段必须表态（additionalProperties 或非空 description）。"""
    for field_name, field_schema in (schema.get("properties") or {}).items():
        if not isinstance(field_schema, dict) or field_schema.get("type") != "object":
            continue
        if field_schema.get("properties"):
            continue  # 嵌套声明 = 已表态
        if field_schema.get("additionalProperties") is not None:
            continue  # map 形表态
        if str(field_schema.get("description") or "").strip():
            continue  # 自描述自由形态
        violations.append(
            f"VIOLATION: plugin={plugin} {tool_name}.{field_name} 自由 object 未表态"
            "（须声明 additionalProperties 或携带非空 description）"
        )


# ── R4/R5（2026-10-19 参数设计对齐：暴露面=模型必填的意图参数）──
# R4 动作选择器闭集：string 型 action/op 参数必须 enum（取值集只活在实现里 =
# LLM 盲猜）。mode 不入本规则——存量 mode 多为开集（模式包 ID/组合语义），
# 伪造 enum 即声明失真；其闭集化走逐工具声明改造。
_R4_ACTION_RE = re.compile(r"^(action|op|operation)$")
# R4 豁免（逐条留因，新增须同批补因）：
#   task_submit.mode —— oneOf 组合语义（字符串 | 字符串数组，可组合），enum 不表达
_R4_EXEMPT = {("task_submit", "task_submit", "mode")}

# R5 注入键不进声明面：`_NATIVE_INJECT_KEYS`（单一真值源
# plugins/shared/tool_args_inject/plugin.py，此处镜像须同批维护——检查器须独立
# 于仓内布局可跑 --scan-root，故不 import）。镜像漂移由 R5 漏报兜底审查兜住。
_R5_INJECT_KEYS = frozenset(
    {
        "session_id",
        "user_id",
        "timestamp",
        "task_id",
        "pipeline_id",
        "workspace",
        "isolation_level",
        "project_root",
        "authorized_zones",
        "authorized_read_zones",
        "parent_ws_meta",
        "parent_agent_level",
        "agent_config_id",
    }
)
# R5 豁免（逐条留因）：注入键兼任「目标引用/显式选择项」——缺省吃注入值，
# 显式填写指他者，属模型需要填写的参数。
_R5_EXEMPT = {
    # 双性质键：缺省注入=操作当前任务，显式填=操作别的任务
    ("task", "task_manage", "task_id"),
    ("review", "review_ops", "task_id"),
    # 显式派发选择项（注入面对 task_submit 刻意豁免，见 tool_args_inject）
    ("task_submit", "task_submit", "workspace"),
    ("task_submit", "task_submit", "isolation_level"),
    # 目标引用：收件人/目标工作空间/来源管道，语义≠调用者自身上下文
    ("channel_dingtalk", "dingtalk.send_message", "user_id"),
    ("channel_feishu", "feishu.send_message", "user_id"),
    ("channel_feishu", "feishu.send_card", "user_id"),
    ("channel_qq", "qq.send_message", "user_id"),
    ("channel_wecom", "wecom.send_message", "user_id"),
    ("resource_merge", "resource_merge", "workspace"),
    ("spill_retrieve", "spill_retrieve", "pipeline_id"),
}


def _check_r4_action_enum(plugin: str, tool_name: str, schema: dict, violations: list[str]) -> None:
    """R4：动作选择器（action/op）string 参数必须 enum 声明闭集。"""
    for field_name, field_schema in (schema.get("properties") or {}).items():
        if not isinstance(field_schema, dict) or field_schema.get("type") != "string":
            continue
        if not _R4_ACTION_RE.match(field_name):
            continue
        if field_schema.get("enum"):
            continue
        if (plugin, tool_name, field_name) in _R4_EXEMPT:
            continue
        violations.append(
            f"VIOLATION: plugin={plugin} {tool_name}.{field_name} "
            "动作选择器未声明 enum 闭集（取值集只活在实现里 = LLM 盲猜；"
            "开集参数改用其它参数名并留豁免说明）"
        )


def _check_r5_inject_keys(plugin: str, tool_name: str, schema: dict, violations: list[str]) -> None:
    """R5：注入键出现在 LLM 声明面必须命中显式豁免清单（默认即红）。"""
    required = set(schema.get("required") or [])
    for field_name in schema.get("properties") or {}:
        if field_name not in _R5_INJECT_KEYS:
            continue
        if (plugin, tool_name, field_name) in _R5_EXEMPT:
            continue
        extra = "（且为 required——注入会静默兜底，必填是假的）" if field_name in required else ""
        violations.append(
            f"VIOLATION: plugin={plugin} {tool_name}.{field_name} "
            f"注入键暴露给 LLM 声明面{extra}"
            "（系统能自动拿到的参数模型永远不需要填；双性质/显式选择项走豁免清单）"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="工具契约静态闸（表态/同名同形/双源一致）")
    parser.add_argument(
        "--scan-root", type=Path, default=DEFAULT_SCAN_ROOT, help="扫描根（同 <root>/shared/<id>/ 布局）"
    )
    args = parser.parse_args()

    if not args.scan_root.is_dir():
        print(f"fail-closed: scan-root 不存在: {args.scan_root}", file=sys.stderr)
        return 2

    tools, broken = _iter_manifest_tools(args.scan_root)
    if broken:
        print(f"fail-closed: {broken} 个 plugin.json 不可解析", file=sys.stderr)
        return 2

    violations: list[str] = []
    inline_skipped_total = 0

    # R1 表态 + R3 双源一致 + R4 动作闭集 + R5 注入键（逐插件）
    for _pjson, plugin, table in tools:
        inline_schemas, skipped = _inline_tool_schemas(args.scan_root / "shared" / plugin)
        inline_skipped_total += skipped
        for tool_name, schema in table.items():
            _check_stance(plugin, tool_name, schema, violations)
            _check_r4_action_enum(plugin, tool_name, schema, violations)
            _check_r5_inject_keys(plugin, tool_name, schema, violations)
            if tool_name in inline_schemas and inline_schemas[tool_name] != schema:
                violations.append(
                    f"VIOLATION: plugin={plugin} {tool_name} 双源漂移"
                    "（manifest input_schema ≠ 代码内联 @plugin.tool schema，G2 注册闸全等比对将拒注册；"
                    "修复须双面同改，富面/正确面胜出）"
                )

    # R2 同名同形（跨插件，(工具名, 字段名) 分组）
    by_field: dict[tuple[str, str], dict[tuple[str, str], dict]] = {}
    for _pjson, plugin, table in tools:
        for tool_name, schema in table.items():
            for field_name, field_schema in (schema.get("properties") or {}).items():
                by_field.setdefault((tool_name, field_name), {})[(plugin, tool_name)] = field_schema
    for (tool_name, field_name), holders in sorted(by_field.items()):
        distinct = {json.dumps(spec, sort_keys=True, ensure_ascii=False) for spec in holders.values()}
        if len(distinct) > 1:
            detail = " | ".join(
                f"{plugin}={json.dumps(spec, ensure_ascii=False)[:80]}"
                for (plugin, _t), spec in sorted(holders.items())
            )
            violations.append(
                f"VIOLATION: plugin={'+'.join(sorted(p for p, _ in holders))} "
                f"{tool_name}.{field_name} 同名同形破坏（{len(distinct)} 种声明形态）：{detail}"
            )

    for line in violations:
        print(line)
    print(
        f"[check_tool_schema_contracts] plugins={len(tools)} "
        f"violations={len(violations)} unparsed_skipped={inline_skipped_total}"
    )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
