#!/usr/bin/env python3
"""tool_policy.yaml 覆盖门禁（grants P1.5，ADR D2「未分类 deny」的 CI 兜底）。

设计真值：ADR 2026-11-19-grants-ledger-and-tool-classification（D2 动作级分级 +
CI 覆盖门禁）+ docs/working/grants账本执行方案_20261119.md §3/§6。

执法契约（缺口即红，fail-closed）：

- 源面 = LLM 可见工具面（三源并集）：
    1. ``plugins/shared/**/plugin.json`` 的 ``capabilities.tools``（声明即注册）
       ∩ ``config/agents/**`` 的 ``tool_ids`` 白名单（条目等于插件 id = 整包注入，
       展开为其全部声明工具，与内核 tool-surface 契约同构）；
    2. ``plugins/shared/*/mode.yaml`` 的 ``inject_tool_ids``（模式激活注入面）；
    3. ``call_tool`` / ``dsh.tool.call`` 动态转发壳的可达目标 = 全部 manifest
       声明工具（resource_search 引入 → call_tool 转发，P1 执法按目标键分级）。
- 索引面 = ``config/plugins/grants/tool_policy.yaml``。覆盖判定镜像运行时查表序
  （plugins/shared/grants/policy.py）：``tool.action`` 精确键 → 整工具键 →
  ``tool.*`` 通配 → 未分类。动作位派生契约镜像 P1 执法面
  （security_check._grants_action_key）：声明 action 枚举 > 命令执行类固定
  ``execute``（单源 = ``config/plugins/isolation/isolation_policy.yaml`` 的
  ``execution: command_in_container``，静态近似只取 tools 精确匹配 + default，
  categories 分支不近似——该分支若出现命令执行类即转红转人工）> 整工具键。
- 红的条件：
    1. 必需 ``(tool, action)`` 对无任何形态键覆盖 = 缺口（精确键 / 整工具键 /
       通配键任一命中即覆盖，镜像运行时整工具回落语义）；
    2. 多动作工具（input_schema.action 枚举）落整工具裸键 = 红（§6 schema：
       整工具键仅限单动作工具；整工具盖多动作须走 ``tool.*`` 通配）；
    3. ``tool.*`` 通配键缺非空 ``notes`` = 红（防通配滥用：通配须登记理由）；
    4. 条目 verdict/effect 越界闭集 = 红（P0 引擎会按 invalid_entry deny）；
    5. ``defaults.uncategorized`` ≠ deny = 红（fail-closed 常量，ADR D2 固化）。
- 已知局限（有意不做，防过度工程）：外部/用户空间提供方
  （tool_schema.EXTERNAL_PROVIDED_TOOL_IDS）仓内无声明面，不可静态推导键——
  白名单命中它们仅报告不执法（运行时同走未分类 deny 兜底）；策略表内无声明面
  背书的键（前瞻键/admin 服务键）不回溯红，报告列出供 P1 复核；隔离策略的
  categories 分支不做静态近似（该分支出现命令执行类即红转人工，误红方向安全）。

用法：
    python scripts/check_tool_policy_coverage.py                  # 门禁（exit 0/1/2）
    python scripts/check_tool_policy_coverage.py --report <path>  # 另存缺口集文本（供 P1 消费）

退出码：0 = 覆盖全；1 = 有缺口/表卫生违规；2 = 上下文解析失败（fail-closed）。
``--report`` 不改变退出码语义（报告不是豁免通道）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins" / "shared"
AGENTS_DIR = ROOT / "config" / "agents"
POLICY_PATH = ROOT / "config" / "plugins" / "grants" / "tool_policy.yaml"
ISOLATION_POLICY_PATH = ROOT / "config" / "plugins" / "isolation" / "isolation_policy.yaml"

#: 动态转发壳工具名：任一在声明面即视为「全部声明工具动态可达」。
DYNAMIC_SHELL_TOOLS = ("call_tool", "dsh.tool.call")

#: 命令执行类的隔离策略执行形态（P1 动作位派生契约第二层的命中值）。
COMMAND_EXECUTION = "command_in_container"

# 剪枝宿主/构建目录（check_agent_tool_ids 同款，防 dsh runtime 连接点迷宫）。
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "target",
    "runtime",
    "dsh_plugins",
    ".venv",
    ".venv-hindsight",
    ".ai_workspaces",
}

Pair = tuple[str, str | None]


class ContextError(RuntimeError):
    """上下文解析失败（声明面/单源清单不可解析）——fail-closed，不静默放行。"""


def _pruned_files(base: Path) -> list[Path]:
    """剪枝遍历目录下全部文件（跳过宿主/构建巨目录）。"""
    found: list[Path] = []
    for cur, subdirs, files in os.walk(base):
        subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
        found.extend(Path(cur) / f for f in files)
    return sorted(found)


@dataclass(frozen=True)
class ToolDecl:
    """一个 manifest 工具声明的门禁视角切片。"""

    name: str
    plugin_dir: str
    plugin_id: str
    #: input_schema.properties.action 枚举；None = 无枚举（含无 action 属性）。
    actions: tuple[str, ...] | None
    schema_type: str | None


def _action_enum(tool: dict[str, Any]) -> tuple[str, ...] | None:
    """提取 input_schema.properties.action 枚举；非枚举形态返回 None。"""
    schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    action = props.get("action")
    if isinstance(action, dict) and isinstance(action.get("enum"), list) and action["enum"]:
        return tuple(str(x) for x in action["enum"])
    return None


def load_manifest_surface(plugins_dir: Path) -> tuple[dict[str, list[ToolDecl]], dict[str, list[str]]]:
    """声明面：工具名 → 声明切片清单；声明了 ≥1 工具的插件 id → 工具名清单。

    同名工具跨插件重声明时要求 action 枚举全等（tool-schema-contract 闸已执法
    input_schema 全等，此处同名异枚举按上下文漂移 fail-closed）。
    """
    surface: dict[str, list[ToolDecl]] = {}
    plugin_tools: dict[str, list[str]] = {}
    for path in _pruned_files(plugins_dir):
        if path.name != "plugin.json":
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContextError(f"manifest 不可解析 {path}: {exc}") from exc
        tools = (manifest.get("capabilities") or {}).get("tools") or []
        plugin_id = str(manifest.get("id") or path.parent.name)
        for tool in tools:
            name = tool.get("name")
            if not name:
                continue
            decl = ToolDecl(
                name=str(name),
                plugin_dir=path.parent.name,
                plugin_id=plugin_id,
                actions=_action_enum(tool),
                schema_type=(tool.get("input_schema") or {}).get("type"),
            )
            surface.setdefault(decl.name, []).append(decl)
            plugin_tools.setdefault(plugin_id, []).append(decl.name)
    if not surface:
        raise ContextError(f"{plugins_dir} 下未发现任何工具声明（manifest 面漂移或遍历剪枝过宽）")
    for name, decls in surface.items():
        enums = {d.actions for d in decls}
        if len(enums) > 1:
            raise ContextError(
                f"工具 {name} 跨插件声明 action 枚举不一致（{sorted(enums)}）——同名声明漂移，拒绝静默"
            )
    return surface, plugin_tools


def _collect_entries(path: Path, base: Path, key: str) -> dict[str, set[str]]:
    """收集一个 yaml 面的 ``key`` 列表 → {条目: {来源（相对路径）}}。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    ids = data.get(key)
    if ids is None:
        return {}
    if not isinstance(ids, list):
        raise ContextError(f"{path}: {key} 应为列表，实为 {type(ids).__name__}")
    rel = path.relative_to(base).as_posix() if path.is_relative_to(base) else str(path)
    out: dict[str, set[str]] = {}
    for entry in ids:
        if isinstance(entry, str) and entry.strip():
            out.setdefault(entry.strip(), set()).add(rel)
    return out


def load_whitelist(agents_dir: Path) -> dict[str, set[str]]:
    """config/agents/** 的 tool_ids 白名单 → {条目: {来源文件}}。"""
    if not agents_dir.is_dir():
        raise ContextError(f"agent 配置根不存在：{agents_dir}")
    merged: dict[str, set[str]] = {}
    scanned = 0
    for path in _pruned_files(agents_dir):
        if path.suffix not in (".yaml", ".yml"):
            continue
        scanned += 1
        for entry, sources in _collect_entries(path, agents_dir, "tool_ids").items():
            merged.setdefault(entry, set()).update(sources)
    if scanned == 0:
        raise ContextError(f"{agents_dir} 下无 yaml（扫描面为空，拒绝假绿）")
    return merged


def load_mode_injects(plugins_dir: Path) -> dict[str, set[str]]:
    """plugins/shared/*/mode.yaml 的 inject_tool_ids → {条目: {来源（相对路径）}}。"""
    merged: dict[str, set[str]] = {}
    for path in _pruned_files(plugins_dir):
        if path.name != "mode.yaml":
            continue
        for entry, sources in _collect_entries(path, plugins_dir, "inject_tool_ids").items():
            merged.setdefault(entry, set()).update(sources)
    return merged


def _load_module_constant(plugins_dir: Path, rel: str, const: str, label: str) -> Any:
    """importlib 加载插件单源常量（check_agent_tool_ids 同款，含共享根可达）。"""
    path = plugins_dir / rel
    if not path.is_file():
        raise ContextError(f"{label}单源缺失：{path}")
    # 单源模块可能 import 共享根裸件（如 _libs.pipeline_base）——共享根入
    # sys.path 与 check_agent_tool_ids.load_external_tool_ids 同口径。
    for entry in (str(path.parent), str(plugins_dir)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(f"_ctp_{const}", path)
    if spec is None or spec.loader is None:
        raise ContextError(f"无法加载{label}单源：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, const, None)
    if not value:
        raise ContextError(f"{label}常量为空——单源被清空，拒绝静默放行")
    return value


def load_external_ids(plugins_dir: Path) -> frozenset[str]:
    """外部/用户空间提供方 id 单源（tool_schema.EXTERNAL_PROVIDED_TOOL_IDS）。"""
    ids = _load_module_constant(plugins_dir, "tool_schema/plugin.py", "EXTERNAL_PROVIDED_TOOL_IDS", "外部工具清单")
    return frozenset(str(x) for x in ids)


def load_verdict_effects(plugins_dir: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """verdict/effect 闭集单源（grants/policy.py 的 VERDICTS/EFFECTS）。"""
    verdicts = _load_module_constant(plugins_dir, "grants/policy.py", "VERDICTS", "verdict 闭集")
    effects = _load_module_constant(plugins_dir, "grants/policy.py", "EFFECTS", "effect 闭集")
    return tuple(str(x) for x in verdicts), tuple(str(x) for x in effects)


def load_command_execution_tools(policy_path: Path) -> frozenset[str]:
    """命令执行类工具单源（isolation_policy.yaml ``execution: command_in_container``）。

    P1 动作位派生契约第二层（security_check._grants_action_key）：命令执行类
    固定按 ``tool.execute`` 精确键查表。静态近似只取 tools 精确匹配；default
    层若被改为命令执行类 = 全工具动作位语义翻转，静态不可近似 → fail-closed；
    categories 分支命中命令执行类时（现况无）同样红转人工（误红方向安全）。
    """
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContextError(f"隔离策略不可读 {policy_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContextError(f"隔离策略 YAML 不可解析 {policy_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContextError(f"隔离策略顶层应为映射：{policy_path}")
    default_exec = (data.get("default") or {}).get("execution")
    if default_exec == COMMAND_EXECUTION:
        raise ContextError(
            f"隔离策略 default.execution={COMMAND_EXECUTION}（全工具动作位翻转，"
            "静态门禁不可近似）——请人工核并扩本闸派生逻辑"
        )
    tools_cfg = data.get("tools") or {}
    found = {
        str(name)
        for name, cfg in tools_cfg.items()
        if isinstance(cfg, dict) and cfg.get("execution") == COMMAND_EXECUTION
    }
    return frozenset(found)


def expand_tool(name: str, surface: dict[str, list[ToolDecl]], command_tools: frozenset[str]) -> set[Pair]:
    """按派生契约展开一个工具的必需键：action 枚举 > 命令执行类 execute > 整工具。"""
    actions = surface[name][0].actions
    if actions:
        return {(name, a) for a in actions}
    if name in command_tools:
        return {(name, "execute")}
    return {(name, None)}


def compute_required(
    surface: dict[str, list[ToolDecl]],
    plugin_tools: dict[str, list[str]],
    whitelist: dict[str, set[str]],
    injects: dict[str, set[str]],
    external: frozenset[str],
    command_tools: frozenset[str],
) -> tuple[dict[Pair, set[str]], list[str]]:
    """必需键集 → {pair: {来源标签}}；附报告面备注（记录不执法项）。"""
    required: dict[Pair, set[str]] = {}
    notes: list[str] = []

    def add(pair: Pair, source: str) -> None:
        required.setdefault(pair, set()).add(source)

    def add_entry(entry: str, sources: set[str], kind: str) -> None:
        label = f"{kind}:{';'.join(sorted(sources))}"
        if entry in surface:
            for pair in expand_tool(entry, surface, command_tools):
                add(pair, label)
        elif entry in plugin_tools:
            for name in plugin_tools[entry]:
                for pair in expand_tool(name, surface, command_tools):
                    add(pair, f"{label} (整包注入 {entry})")
        elif entry in external:
            notes.append(
                f"白名单条目 {entry}（{kind}）为外部/用户空间提供方，仓内无声明面——"
                "不可静态推导键，仅记录不执法（运行时未分类 deny 兜底）"
            )
        else:
            notes.append(f"白名单条目 {entry}（{kind}）不解析到声明面/插件 id——漂移归 agent-tool-ids 闸执法，此处仅记录")

    for entry, sources in sorted(whitelist.items()):
        add_entry(entry, sources, "tool_ids")
    for entry, sources in sorted(injects.items()):
        add_entry(entry, sources, "inject_tool_ids")

    # 动态转发壳可达面：任一壳在声明面 → 全部声明工具按目标键分级，均须覆盖。
    shells = [s for s in DYNAMIC_SHELL_TOOLS if s in surface]
    if shells:
        for name in sorted(surface):
            for pair in expand_tool(name, surface, command_tools):
                required.setdefault(pair, set()).add(f"动态可达面({'+'.join(shells)})")
    return required, notes


def _covered(tool: str, action: str | None, keys: set[str]) -> bool:
    """镜像 grants/policy.py 查表序的覆盖判定（含整工具回落与 tool.* 通配）。

    运行时语义：精确 ``tool.action`` 未命中即回落整工具键——故整工具键覆盖该
    工具全部动作位。多动作工具落整工具裸键属 §6 schema 形状违规，由表卫生
    检查单独红（此处只裁「运行时是否有分类」，形状与覆盖分离）。``tool.*``
    通配为本门禁侧扩展形态（覆盖判定上与整工具键等价，另受 notes 理由约束）。
    """
    if action is not None and f"{tool}.{action}" in keys:
        return True
    return tool in keys or f"{tool}.*" in keys


def _fmt_sources(sources: set[str]) -> str:
    """来源标签压缩：≤3 全列，超出取前 3 + 计数（缺口键本身是主信息）。"""
    ordered = sorted(sources)
    if len(ordered) <= 3:
        return ";".join(ordered)
    return ";".join(ordered[:3]) + f"(+{len(ordered) - 3} 处)"


def check_policy_table(
    table: Any,
    surface: dict[str, list[ToolDecl]],
    required: dict[Pair, set[str]],
    verdicts: tuple[str, ...],
    effects: tuple[str, ...],
    command_tools: frozenset[str] = frozenset(),
) -> list[str]:
    """索引面卫生 + 覆盖缺口 → 违规清单（空 = 通过）。"""
    violations: list[str] = []
    if not isinstance(table, dict):
        return [f"策略表不可解析（顶层应为映射，实为 {type(table).__name__}）——全表未分类即全 deny"]
    tools = table.get("tools")
    if not isinstance(tools, dict):
        violations.append("策略表缺 tools 映射（或形态非法）——全表未分类即全 deny")
        tools = {}
    defaults = table.get("defaults")
    if not isinstance(defaults, dict) or defaults.get("uncategorized") != "deny":
        violations.append('defaults.uncategorized 必须为 deny（fail-closed 常量，ADR D2 固化，表数据不得改写）')

    multi_action = {name for name, decls in surface.items() if decls[0].actions}
    for raw_key, entry in sorted(tools.items(), key=lambda kv: str(kv[0])):
        key = str(raw_key)
        if not isinstance(entry, dict):
            violations.append(f'策略表条目 "{key}" 非映射形态——P0 引擎按 invalid_entry deny，请修正')
            continue
        if entry.get("verdict") not in verdicts or entry.get("effect") not in effects:
            violations.append(
                f'策略表条目 "{key}" verdict/effect 越界闭集'
                f"（verdict={entry.get('verdict')!r} effect={entry.get('effect')!r}；"
                f"合法 verdict={list(verdicts)} effect={list(effects)}）"
            )
        if key.endswith(".*") and not str(entry.get("notes") or "").strip():
            violations.append(f'通配键 "{key}" 缺登记理由（notes 非空必填，防通配滥用）')
        base = key[:-2] if key.endswith(".*") else key
        if base in multi_action and not key.endswith(".*"):
            actions = surface[base][0].actions
            violations.append(
                f'"{key}" 为多动作工具整工具裸键（声明枚举 {list(actions)}）——'
                f'§6 schema 整工具键仅限单动作工具；请逐 action 列键或改 "{base}.*" 通配（notes 登记理由）'
            )

    keys = {str(k) for k in tools}
    gaps = [pair for pair in required if not _covered(pair[0], pair[1], keys)]
    for tool, action in sorted(gaps):
        sources = _fmt_sources(required[(tool, action)])
        decl_dirs = ",".join(sorted({d.plugin_dir for d in surface[tool]}))
        key_hint = f"{tool}.{action}" if action else f"{tool}（整工具键）"
        extra = f"（命令执行类：登记 {tool}.execute）" if tool in command_tools else ""
        violations.append(
            f"策略表缺口: {key_hint}（声明: {decl_dirs}）无任何分级键覆盖（必需来源: {sources}）"
            f'→ 请在 config/plugins/grants/tool_policy.yaml 登记（多动作工具逐 action；'
            f'整工具默认仅限单动作；通配 {tool}.* 须 notes 登记理由）{extra}'
        )
    return violations


def load_policy_table(policy_path: Path) -> Any:
    """读策略表；文件缺失/不可解析返回错误标记 dict（转违规，不炸上下文）。"""
    try:
        table = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"__error__": f"策略表不可读 {policy_path}: {exc}"}
    except yaml.YAMLError as exc:
        return {"__error__": f"策略表 YAML 不可解析 {policy_path}: {exc}"}
    return table


def build_report(
    violations: list[str],
    surface: dict[str, list[ToolDecl]],
    required: dict[Pair, set[str]],
    notes: list[str],
) -> str:
    """缺口集文本（P1 消费）：按维度分组，含记录面证据。"""
    lines: list[str] = [
        "tool_policy 覆盖报告（P1 收口输入）",
        "=" * 60,
        f"声明面工具数: {len(surface)}（多动作 {sum(1 for d in surface.values() if d[0].actions)} 个）",
        f"必需 (tool, action) 对数: {len(required)}",
        f"违规数: {len(violations)}",
        "",
    ]
    gaps = [v for v in violations if v.startswith("策略表缺口")]
    hygiene = [v for v in violations if not v.startswith("策略表缺口")]
    if hygiene:
        lines.append("── 表卫生违规 ──")
        lines += [f"  - {v}" for v in hygiene]
        lines.append("")
    if gaps:
        lines.append("── 分级缺口（P1 补表清单）──")
        lines += [f"  - {v}" for v in gaps]
        lines.append("")
    if not violations:
        lines.append("── 缺口集为空（覆盖全）──")
        lines.append("")
    if notes:
        lines.append("── 记录面（不执法，P1 复核）──")
        lines += [f"  - {v}" for v in notes]
        lines.append("")
    return "\n".join(lines)


def check(
    plugins_dir: Path = PLUGINS_DIR,
    agents_dir: Path = AGENTS_DIR,
    policy_path: Path = POLICY_PATH,
    isolation_policy_path: Path = ISOLATION_POLICY_PATH,
) -> tuple[list[str], str]:
    """全量检查 → (违规清单, 报告文本)。参数注入供单测建临时仓树。"""
    surface, plugin_tools = load_manifest_surface(plugins_dir)
    whitelist = load_whitelist(agents_dir)
    injects = load_mode_injects(plugins_dir)
    external = load_external_ids(plugins_dir)
    verdicts, effects = load_verdict_effects(plugins_dir)
    command_tools = load_command_execution_tools(isolation_policy_path)
    required, notes = compute_required(surface, plugin_tools, whitelist, injects, external, command_tools)
    table = load_policy_table(policy_path)
    error = table.pop("__error__", None) if isinstance(table, dict) else None
    violations = check_policy_table(table, surface, required, verdicts, effects, command_tools)
    if error:
        violations.append(str(error))
    notes.append(
        "动态转发壳证据: "
        + ("; ".join(f"{s} 声明于 " + ",".join(sorted({d.plugin_dir for d in surface[s]})) for s in DYNAMIC_SHELL_TOOLS if s in surface) or "无")
        + "——任一壳在声明面即全部声明工具按目标键分级（执行方案 §3 第④项）"
    )
    report = build_report(violations, surface, required, notes)
    return violations, report


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，报告含制表符/CJK 时 print 直接 UnicodeEncodeError
    # ——入口重配置 UTF-8（run_gates.py 同款；与 CI Linux UTF-8 行为对齐，
    # 不依赖 PYTHONIOENCODING（python -I 下被忽略））。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugins-root", type=Path, default=PLUGINS_DIR, help="插件声明根（单测注入）")
    parser.add_argument("--agents-root", type=Path, default=AGENTS_DIR, help="agent 配置根（单测注入）")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH, help="策略表路径（单测注入）")
    parser.add_argument(
        "--isolation-policy", type=Path, default=ISOLATION_POLICY_PATH, help="隔离策略路径（命令执行类单源，单测注入）"
    )
    parser.add_argument("--report", type=Path, default=None, help="缺口集报告写出路径（不改变退出码）")
    args = parser.parse_args(argv)
    try:
        violations, report = check(args.plugins_root, args.agents_root, args.policy, args.isolation_policy)
    except ContextError as exc:
        print(f"[tool-policy-coverage] 上下文解析失败：{exc}", file=sys.stderr)
        return 2
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"[tool-policy-coverage] 报告已写出 {args.report}")
    if violations:
        print(f"[tool-policy-coverage] 门禁红：{len(violations)} 项违规\n")
        print(report)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
