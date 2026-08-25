"""DSH 插件包清单翻译器（task_dsh_plugin_adapter 任务 2）。

把一个 DSH 插件包（源码目录）翻译成灵汐可用的注册清单——纯函数、零副作用，
可离线运行也可经 ``dsh_translate_manifest`` 工具在线调用。

翻译边界（诚实声明）：
- **前端部分**：解析 package.json 的 ``dsh.*`` 声明 + 扫描 client 源码中的
  ``slots.register``（toolview 键）→ 产出灵汐 ``contributes.renderers`` 等价物。
  组件本体不在此翻译——视觉组件走 vendor 移植层（任务 3，锁定 commit 复制）。
- **后端部分**：不做 TS 源码静态解析（defineTool 的 schema DSL 在运行时才
  完整）。工具契约来自两条通道：(a) 通道 A——runtime 桥 ``initialize`` 时
  的运行时自省（bridge.list_tools()）；(b) 锁定契约表——翻译器把 (a) 的
  输出固化进 plugin.json（本仓库 dsh_read/dsh_glob 即此产物，DSH commit
  47f9438 锁定）。
- **MCP 工具**：不在适配器范围（external_mcp 天然直连），翻译器明确标记。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

DSH_SOURCE_COMMIT = "141eb6fef83422698aef7a981029e843e8161534"
DSH_SOURCE_VERSION = "0.1.0-rc.8"

# toolview 注册形态：ctx.slots.register({ name: 'tool.call.toolview', key: 'read', ... }, ReadRow)
_SLOT_REGISTER_RE = re.compile(
    r"slots\.register\(\s*\{[^}]*?name:\s*['\"]tool\.call\.toolview['\"][^}]*?key:\s*['\"]([\w.-]+)['\"]",
    re.DOTALL,
)
# MCP 型插件标记：dsh.host 声明 mcp transport / 包名含 mcp
_MCP_HINT_RE = re.compile(r"mcp", re.IGNORECASE)

# ── DSH 插件形态分类（hook/service/io/tool/visual 五形态） ────────────────
#
# 用户定调：DSH 插件按形态分清区别——钩子类（事件→命令）、服务类（Service
# 提供者）、输入输出类（agent 消息批次拦截/结果后处理）、工具类（defineTool）、
# 视觉类（slots 前端表面）。一个包可多形态（如 interconnect = service+tool）。
# 分类是静态信号（代码形态 + 清单声明），不执行任何 DSH 代码。
_HOOK_EVENT_RE = re.compile(r"ctx\.on\(\s*['\"](session/event|agent/(?:created|disposed|error|status))['\"]")
_HOOK_SPAWN_RE = re.compile(r"\b(?:spawn|exec)\(\s*['\"`]")
_SERVICE_EXTENDS_RE = re.compile(r"extends\s+Service\b")
_SERVICE_NAME_RE = re.compile(r"super\(\s*ctx\s*,\s*['\"]([\w.-]+)['\"]")
_SERVICE_INJECT_RE = re.compile(r"(?:static\s+)?inject\s*=\s*\[([^\]]*)\]")
_TOOL_REG_RE = re.compile(r"ctx\.tools\.register\(|defineTool\(")
_IO_PRE_STEP_RE = re.compile(r"['\"]agent/pre-step['\"]")
_IO_INBOX_RE = re.compile(r"agent\.inbox")
_IO_TOOLS_RESULT_RE = re.compile(r"['\"]tools/result['\"]")


def classify_dsh_plugin(root: str | Path) -> dict[str, Any]:
    """静态分析 DSH 插件包形态（多形态标注，含关键信息提取）。

    扫描全部 TS/JS 源码（src/**、lib/**、dsh/**）+ dsh.plugin.json 声明，
    识别 hook/service/io/tool/visual 五形态。翻译层使用：输出灵汐对应机制
    的契约建议（触发服务/服务声明/pipeline 插件接线），运行时落地另见
    translate_hooks_config / 各通道。
    """
    root = Path(root)
    blob = ""
    for pattern in ("src/**/*.ts", "src/**/*.tsx", "src/**/*.js", "lib/**/*.js", "dsh/**/*.js", "**/*.mjs"):
        for p in sorted(root.glob(pattern)):
            try:
                blob += p.read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                continue
    kinds: dict[str, Any] = {}

    # hook 钩子类：会话/agent 事件订阅 + 命令执行（spawn/exec 或 config hooks）
    hook_events = sorted(set(_HOOK_EVENT_RE.findall(blob)))
    if hook_events and (_HOOK_SPAWN_RE.search(blob) or "hooks" in blob):
        kinds["hook"] = {
            "events": hook_events,
            "lingxi": "triggers_ext EVENT + action=command（translate_hooks_config 产出 trigger_setup 参数）",
        }

    # service 服务类：Service 子类注册（super(ctx, name)）或 dsh.plugin.json entry.inject
    service_names: list[str] = sorted(set(_SERVICE_NAME_RE.findall(blob)))
    injects: list[str] = []
    for m in _SERVICE_INJECT_RE.findall(blob):
        injects.extend(x.strip().strip("'\"") for x in m.split(",") if x.strip())
    dsh_plugin_json = root / "dsh.plugin.json"
    entry_inject: list[str] = []
    if dsh_plugin_json.is_file():
        try:
            decl = json.loads(dsh_plugin_json.read_text(encoding="utf-8"))
            entry_inject = decl.get("entry", {}).get("inject", []) if isinstance(decl.get("entry"), dict) else []
        except ValueError:
            pass
    if service_names or entry_inject:
        kinds["service"] = {
            "names": service_names,
            "inject": sorted(set(injects + entry_inject)),
            "lingxi": "capabilities.services 契约（D.6：不进 LLM 面；方法体需按灵汐 SDK 重写或桥适配）",
        }

    # io 输入输出类：agent 消息批次拦截（pre-step/inbox=输入）与结果后处理（tools/result=输出）
    io_roles: list[str] = []
    if _IO_PRE_STEP_RE.search(blob) or _IO_INBOX_RE.search(blob):
        io_roles.append("input")
    if _IO_TOOLS_RESULT_RE.search(blob):
        io_roles.append("output")
    if io_roles:
        kinds["io"] = {
            "roles": io_roles,
            "lingxi": "pipeline input/output 插件（IInputPlugin/IOutputPlugin + invoke_entry + autonomous.yaml prepare/post 链位置；逻辑需按 SDK 重写）",
        }

    # tool 工具类：defineTool 注册 → 通道 A 桥（extra-tools 机制）
    if _TOOL_REG_RE.search(blob) or (root / "lib" / "index.js").is_file():
        kinds["tool"] = {"channel": "runtime-bridge (extra-tools)"}

    # visual 视觉类：slots 前端表面（复用 client 扫描信号）
    if _SLOT_NAME_RE.search(blob):
        kinds["visual"] = {"channel": "contributes renderers/slots 翻译"}

    return kinds

# ── DSH client 槽位 → 灵汐 UI 槽位映射表（界面语义翻译的单一事实源） ────────
#
# DSH 的 client 插件经 slots 注册 UI 表面（conversation.* / tool.call.* /
# settings.* 等）。灵汐前端按 contributes 声明渲染（viewsContainers/views/
# workspaceTabs/dockItems/floating/modal/statusBarItems/chatMessages/
# chatInteractions/chatActions/settingsPanels/widgets/client_styles/themes）。
# 本表把 DSH 槽位的**语义**翻译成灵汐槽位——侧边栏→侧边栏、输入区→输入区、
# 工具卡→聊天消息卡、详情→浮窗；灵汐无对应槽位的标记 direct（直接渲染，
# 诚实边界，不强行归并）。
DSH_SLOT_LINGXI_MAP: dict[str, dict[str, str]] = {
    "tool.call.toolview": {"lingxi_slot": "chatMessages", "note": "工具结果卡 → 聊天消息卡样式（ActivityCard dsh:* 分支渲染）"},
    "conversation.chat.node": {"lingxi_slot": "chatMessages", "note": "聊天消息节点 → 聊天消息卡"},
    "conversation.details.tool": {"lingxi_slot": "floating", "note": "工具详情面板 → 浮窗/详情浮层"},
    "conversation.composer": {"lingxi_slot": "chatInteractions", "note": "聊天输入区 → 聊天交互模式"},
    "conversation.composer.dock": {"lingxi_slot": "dockItems", "note": "输入区 dock → 底部 dock 栏"},
    "conversation.input.dock": {"lingxi_slot": "dockItems", "note": "输入区 dock → 底部 dock 栏"},
    "settings.general.item": {"lingxi_slot": "settingsPanels", "note": "设置项 → 插件设置面板"},
    "settings.plugin.item": {"lingxi_slot": "settingsPanels", "note": "插件设置项 → 插件设置面板（modlens 等视觉包布局）"},
}
_DSH_SLOT_FALLBACK: dict[str, str] = {
    "lingxi_slot": "direct",
    "note": "灵汐无对应槽位 → 直接渲染（不强行归并）",
}


def map_dsh_slot(slot_name: str) -> dict[str, str]:
    """DSH slot 名 → 灵汐槽位（未收录回退 direct = 直接渲染）。"""
    return DSH_SLOT_LINGXI_MAP.get(slot_name, dict(_DSH_SLOT_FALLBACK))


# ── DSH hook 事件 → 灵汐域事件映射表（钩子翻译的单一事实源） ──────────────
#
# DSH hooks 是声明式「事件→命令」（{on, when?, run, timeoutMs}）。灵汐等价
# 物 = triggers_ext 的 EVENT 触发器（域事件总线输入）+ action=command。
# turn/end 的 when（结束原因）直接映射到 run 终态事件名；aborted/blocked/
# max-tokens/interrupted 域事件无细分（run.suspended 不携带 reason 标签），
# 以 run.suspended 近似——诚实标注。
HOOK_EVENT_LINGXI_MAP: dict[str, str] = {
    "turn/start": "run.started",
    "approval/asked": "approval.created",
    "agent/created": "session.created",
    "agent/disposed": "session.deleted",
    "agent/error": "run.failed",
}
TURN_END_REASON_MAP: dict[str, str] = {
    "completed": "run.completed",
    "error": "run.failed",
    "aborted": "run.suspended",
    "blocked": "run.suspended",
    "max-tokens": "run.suspended",
    "interrupted": "run.suspended",
}


def translate_hooks_config(hooks: list[dict[str, Any]] | str | None) -> dict[str, Any]:
    """DSH hooks 配置（[{on, when?, run, timeoutMs?}]）→ 灵汐触发器参数列表。

    Returns:
        ``{"triggers": [...], "mapped": n, "unmapped": [{on, when, run, reason}]}``。
        每条 trigger 可直接作为 trigger_setup 工具的输入（trigger_type=event /
        event_type / action=command / action_params / message）。
    """
    if hooks is None:
        return {"triggers": [], "mapped": 0, "unmapped": []}
    if isinstance(hooks, str):
        try:
            parsed = yaml.safe_load(hooks) or []
        except yaml.YAMLError:
            parsed = []
    else:
        parsed = hooks
    # 兼容两种声明形态：直接列表 [{on, when, run}] 或 {hooks: [...]}（DSH
    # profile 的 config 块包装）
    if isinstance(parsed, dict) and isinstance(parsed.get("hooks"), list):
        parsed = parsed["hooks"]
    triggers: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for spec in parsed if isinstance(parsed, list) else []:
        if not isinstance(spec, dict):
            unmapped.append({"reason": "not-an-object", "spec": spec})
            continue
        on = spec.get("on")
        if on is None and True in spec:
            # PyYAML 默认 YAML 1.1：裸键 `on` 被解析为布尔 True（on/off 语义）
            on = spec[True]
        when = spec.get("when")
        run = spec.get("run")
        timeout_ms = spec.get("timeoutMs", 10000)
        if on == "turn/end":
            event = TURN_END_REASON_MAP.get(when) if when is not None else "run.completed"
            if event is None:
                unmapped.append({"on": on, "when": when, "run": run, "reason": "unknown turn/end reason"})
                continue
        else:
            event = HOOK_EVENT_LINGXI_MAP.get(on) if on is not None else None
            if event is None:
                unmapped.append({"on": on, "when": when, "run": run, "reason": "no lingxi domain event equivalent"})
                continue
        triggers.append({
            "trigger_type": "event",
            "event_type": event,
            "action": "command",
            "action_params": {"command": run, "timeout_ms": int(timeout_ms or 10000)},
            "message": f"[DSH hook {on}] {run}",
        })
    return {"triggers": triggers, "mapped": len(triggers), "unmapped": unmapped}


# slots.register/inject 的 name 声明（任意槽位名，不限于 toolview）
_SLOT_NAME_RE = re.compile(
    r"slots\.(?:register|inject)\([^)]*?name:\s*['\"]([\w.]+)['\"]",
    re.DOTALL,
)

# ── DSH toolview 键 → 灵汐渲染组件映射表（前端组件翻译的单一事实源） ────────
#
# DSH ui-tool 的每个 toolview 键（slots.register key）对应一个 DSH 卡片原语
# （ui-primitives 组件）。灵汐侧的等价物 = vendor 移植组件 + render 意图卡：
# 本表把「DSH 键 → DSH 卡片原语 → 灵汐组件 → render card」三段链一次写清。
# 组件本体在 frontend/src/components/vendor/dsh/（锁定 commit 复制），
# 渲染路由在 frontend/src/utils/dshRenderIntent.ts（payload 构造器）。
# 新增映射 = 改本表 + 对应 payload 构造器，两处同步（有防漂移测试）。
DSH_TOOLVIEW_COMPONENT_MAP: dict[str, dict[str, str]] = {
    # DSH 键: {dsh_component: DSH 卡片原语, lingxi_component: vendor 组件, card: render 意图}
    "read": {"dsh_component": "ReadBlock", "lingxi_component": "ReadBlock", "card": "read"},
    "bash": {"dsh_component": "TerminalBlock", "lingxi_component": "TerminalBlock", "card": "terminal"},
    "web_search": {"dsh_component": "WebBlock(search)", "lingxi_component": "WebBlock", "card": "web"},
    "web_fetch": {"dsh_component": "WebBlock(fetch)", "lingxi_component": "WebBlock", "card": "web"},
    "grep": {"dsh_component": "SearchBlock(matches)", "lingxi_component": "SearchBlock", "card": "search"},
    "glob": {"dsh_component": "SearchBlock(paths)", "lingxi_component": "SearchBlock", "card": "search"},
    "edit": {"dsh_component": "DiffBlock", "lingxi_component": "DiffBlock", "card": "diff"},
    "write": {"dsh_component": "DiffBlock", "lingxi_component": "DiffBlock", "card": "diff"},
    "todo_write": {"dsh_component": "JsonTree", "lingxi_component": "JsonTree", "card": "generic"},
    "ask_user_question": {"dsh_component": "GenericToolCard", "lingxi_component": "ActivityCard(默认)", "card": "generic"},
    # 未列出的键（未来 DSH 新增 toolview）→ generic 兜底，不拒绝翻译
}
_DSH_FALLBACK_MAPPING: dict[str, str] = {
    "dsh_component": "GenericToolCard",
    "lingxi_component": "ActivityCard(默认)",
    "card": "generic",
}


def map_toolview_to_component(toolview_key: str) -> dict[str, str]:
    """DSH toolview 键 → 灵汐组件映射（未收录键回退 generic 兜底）。"""
    return DSH_TOOLVIEW_COMPONENT_MAP.get(toolview_key, dict(_DSH_FALLBACK_MAPPING))


def dsh_params_to_json_schema(params: dict[str, Any] | None) -> dict[str, Any]:
    """DSH parameters DSL → JSON Schema（与 runtime 桥 mjs 侧同构）。

    DSL 形态：``{field: {type, required?, description?}}``。
    """
    if not params:
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key, spec in params.items():
        if not isinstance(spec, dict):
            continue
        prop: dict[str, Any] = {"type": spec.get("type", "string")}
        if spec.get("description") is not None:
            prop["description"] = spec["description"]
        properties[key] = prop
        if spec.get("required") is True:
            required.append(key)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def to_lingxi_tool_entry(tool: dict[str, Any]) -> dict[str, Any]:
    """DSH 运行时工具契约（bridge.list_tools() 条目）→ 灵汐 capabilities.tools 条目。

    render 意图按 DSH presentResult 词汇表人工判定（运行时自省拿不到
    presentation 回调的输出形态，卡类型是工具作者的语义决定）。
    """
    entry: dict[str, Any] = {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema") or dsh_params_to_json_schema(tool.get("parameters")),
    }
    if tool.get("output_schema"):
        entry["output_schema"] = tool["output_schema"]
    if tool.get("render"):
        entry["render"] = tool["render"]
    if tool.get("category"):
        entry["category"] = tool["category"]
    return entry


def translate_package(package_dir: str | Path) -> dict[str, Any]:
    """翻译一个 DSH 插件包目录 → 灵汐等价注册清单。

    失败隔离：单个源文件解析异常跳过并记入 ``warnings``，不影响整体翻译。
    """
    root = Path(package_dir)
    warnings: list[str] = []

    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        raise FileNotFoundError(f"not a DSH package (no package.json): {root}")
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"bad package.json in {root}: {e}") from e

    dsh_decl = pkg.get("dsh") if isinstance(pkg.get("dsh"), dict) else {}
    client_decl = dsh_decl.get("client") if isinstance(dsh_decl.get("client"), dict) else {}
    platform = client_decl.get("platform") if isinstance(client_decl, dict) else None
    inject = client_decl.get("inject", []) if isinstance(client_decl, dict) else []

    # 前端表面：client 入口扫描——DSH 槽位（slots.register 的 name 域）与
    # toolview 键（tool.call.toolview 的 key 域）。两种来源：(a) 源码目录
    # src/client/**/*.ts*（仓库检出）；(b) npm 构建产物 lib/*.js（npm pack
    # 下载包无 src——CSS 被 stub、slots.register 调用保留）。
    renderers: list[dict[str, Any]] = []
    slots: dict[str, dict[str, Any]] = {}  # DSH 槽位名 → {lingxi_slot, note, sources}
    scan_targets: list[tuple[Path, str]] = []
    if (root / "src" / "client").is_dir():
        scan_targets = [
            (src, f"src/client/{src.relative_to(root / 'src' / 'client')}")
            for src in sorted((root / "src" / "client").rglob("*.ts*"))
        ]
    elif (root / "lib").is_dir():
        scan_targets = [(f, f"lib/{f.name}") for f in sorted((root / "lib").glob("*.js"))]
    elif (root / "dsh").is_dir():
        # modlens 等视觉包的 client 面布局（package exports 指向 dsh/index.js）
        scan_targets = [(f, f"dsh/{f.name}") for f in sorted((root / "dsh").glob("*.js"))]
    for path, rel in scan_targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            warnings.append(f"unreadable source {path.name}: {e}")
            continue
        # 槽位名（任意 DSH slot）→ 灵汐槽位映射（未收录回退 direct = 直接渲染）
        for slot_name in _SLOT_NAME_RE.findall(text):
            entry = slots.setdefault(slot_name, {**map_dsh_slot(slot_name), "sources": []})
            if rel not in entry["sources"]:
                entry["sources"].append(rel)
        # toolview 键（tool.call.toolview 的 key 域）→ 渲染组件映射
        for key in _SLOT_REGISTER_RE.findall(text):
            renderers.append({
                "tool": key,
                "source": rel,
                # 组件映射：DSH 键 → 灵汐渲染组件（单一事实源见模块头部映射表）
                **map_toolview_to_component(key),
            })

    manifest: dict[str, Any] = {
        "source": {
            "package": pkg.get("name", root.name),
            # 包自身版本（npm 下载 = 发布版本；源码检出 = 仓库版本）
            "version": pkg.get("version"),
            "kind": "dsh-plugin",
            "dsh": {
                # vendor 移植层（components/vendor/dsh/）锁定的基线——翻译对象
                # 与基线不同版时，组件等价性需按 vendor README 的升级路径核对
                "vendor_pinned": {"commit": DSH_SOURCE_COMMIT, "package_version": DSH_SOURCE_VERSION},
                "client_platform": platform,
                "client_inject": inject,
            },
        },
        "kinds": classify_dsh_plugin(root),
        "client": {
            "is_client_plugin": bool(client_decl),
            "slots": slots,
            "renderers": renderers,
            # 功能型包（依赖 DSH 事件投影服务）不适配——诚实边界
            "adapter_scope": "visual-only" if renderers else "none",
        },
        "backend": {
            # 静态翻译不产工具契约：通道 A（runtime 自省）或锁定契约表提供
            "tools_channel": "runtime-introspection",
            "mcp_tools": "out-of-scope (external_mcp direct)",
            # 含 lib/index.js 的工具包可经通道 A 桥装载（extra-tools 机制）
            "extra_tools": (root / "lib" / "index.js").is_file(),
        },
        "warnings": warnings,
    }
    return manifest


def translate_packages(package_dirs: list[Path] | list[str | Path]) -> dict[str, Any]:
    """批量翻译（失败隔离：单包失败计入 errors，不中断其他包）。"""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for d in package_dirs:
        try:
            results.append(translate_package(d))
        except (FileNotFoundError, ValueError) as e:
            errors.append({"package": str(d), "error": str(e)})
    return {"packages": results, "errors": errors}


def discover_dsh_plugins(base_dir: str | Path | None = None) -> list[Path]:
    """发现适配器 ``dsh_plugins/`` 下已放置的 DSH 插件包目录。

    规则：直接子目录含 package.json 即认作插件包（npm 解压物 / 源码检出
    同形态）；``_`` 前缀目录（如 _README）跳过。DSH 插件全部放在适配器
    下面由适配器自己加载——新增插件 = 放一个子目录，零适配器代码改动。
    """
    base = Path(base_dir) if base_dir is not None else Path(__file__).parent / "dsh_plugins"
    if not base.is_dir():
        return []
    return sorted(
        d for d in base.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "package.json").is_file()
    )


def _project_root() -> str:
    """定位项目根：优先 AGENTOS_PROJECT_ROOT；否则以本文件位置锚定
    （translator.py 在 plugins/shared/system/dsh_adapter/ 下，parents[4]
    恒为项目根——sidecar 的 cwd 不可控，cwd 上溯在 target/release 等
    深目录下不可靠）；最后回退 cwd 上溯找 config/。"""
    root = os.environ.get("AGENTOS_PROJECT_ROOT")
    if root and os.path.isdir(root):
        return root
    anchored = Path(__file__).resolve().parents[4]
    if (anchored / "config").is_dir():
        return str(anchored)
    cur = Path.cwd()
    for _ in range(6):
        if (cur / "config").is_dir():
            return str(cur)
        parent = cur.parent
        if parent == cur:
            break
    return str(Path.cwd())


def load_plugin_config() -> dict[str, Any]:
    """读适配器配置（config/dsh_adapter.yaml）的 ``plugins`` 映射。

    配置是 DSH 插件装载管理：``{包目录名: {enabled: bool}}``。读失败或
    缺文件返回空 dict（语义 = 全部默认启用，不因配置问题阻塞装载）。
    """
    cfg_path = Path(_project_root()) / "config" / "dsh_adapter.yaml"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    return plugins if isinstance(plugins, dict) else {}


def _plugin_enabled(name: str, config: dict[str, Any]) -> bool:
    """按配置判定包是否启用：未列出默认启用；``{enabled: bool}`` 或裸 bool 均认。"""
    entry = config.get(name)
    if isinstance(entry, dict):
        return bool(entry.get("enabled", True))
    if isinstance(entry, bool):
        return entry
    return True


def load_installed_plugins() -> dict[str, Any]:
    """扫描 dsh_plugins/ 并按配置过滤后批量翻译（适配器装载入口）。

    Returns:
        translate_packages 输出 + ``base_dir``（装载位置记录）+ ``count``
        （已启用装载数）+ ``disabled``（被配置禁用的包目录名列表）。
    """
    packages = discover_dsh_plugins()
    config = load_plugin_config()
    enabled = [p for p in packages if _plugin_enabled(p.name, config)]
    out: dict[str, Any] = translate_packages(enabled)
    out["base_dir"] = str(Path(__file__).parent / "dsh_plugins")
    out["count"] = len(enabled)
    out["disabled"] = [p.name for p in packages if not _plugin_enabled(p.name, config)]
    return out


# ── DSH 皮肤中心资产解析（contributes.themes 主题发射素材） ─────────────
#
# 第三方 skin-center 包（dsh_plugins/skin-center/）的皮肤是分层资产：
# skin.json manifest（v2）声明 contributes.stylesheet（skin.css = 令牌层，
# :root 上的 CSS 变量 + 背景/字体/配色）与 contributes.patches（patches.css /
# hooks.mjs = DOM 补丁层）。本模块把皮肤翻译成灵汐原生 ThemeConfig
# （配色/背景图/基准走主题管线渲染）；DOM 补丁层由前端按择注入通道
# 原样搬入（merged.css + hooks.mjs，见 server.py 与 dshSkinCss.ts）。

SKIN_CENTER_SKINS_DIR = Path(__file__).parent / "dsh_plugins" / "skin-center" / "skins"


def list_available_skins(base_dir: str | Path | None = None) -> list[str]:
    """列出 skin-center 内可用的皮肤 id（有 skin.css 的目录，按 skin.json order 无关的字母序）。"""
    base = Path(base_dir) if base_dir is not None else SKIN_CENTER_SKINS_DIR
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "skin.css").is_file())


def describe_available_skins(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """动态皮肤清单（运行时读当前装载的皮肤插件，供设置页/清单端点消费）。

    每条含 ThemeConfig 转换素材：colors
    (canvas/text/panel/accent hex) + base（亮度判定）+ background_media
    （src/scrim/asset_url）——前端据此克隆基准主题生成灵汐原生
    ThemeConfig（立绘→backgrounds.image、配色→colors，全走主题管线）。
    皮肤集合变化即时反映，零 manifest 改动。
    """
    base = Path(base_dir) if base_dir is not None else SKIN_CENTER_SKINS_DIR
    out: list[dict[str, Any]] = []
    for skin_id in list_available_skins(base):
        entry: dict[str, Any] = {
            "id": skin_id,
            "name": skin_id,
            "tagline": "",
            "accent": "",
            "base": "dark",
            "tags": [],
            "has_background_media": False,
            "colors": {},
            "background_media": None,
        }
        skin_css = base / skin_id / "skin.css"
        css = ""
        if skin_css.is_file():
            try:
                css = skin_css.read_text(encoding="utf-8", errors="replace")
            except OSError:
                css = ""
        manifest = base / skin_id / "skin.json"
        if manifest.is_file():
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if isinstance(meta, dict):
                entry["name"] = str(meta.get("name") or skin_id)
                entry["tagline"] = str(meta.get("tagline") or "")
                entry["accent"] = str(meta.get("accent") or "")
                tags = [str(t).lower() for t in meta.get("tags", []) if isinstance(t, (str, int))]
                entry["tags"] = tags
        # 基准判定 = 画布色亮度（tags 无 dark/light 标签，按 tags 判恒 dark 是错的）
        if css:
            canvas, text = _extract_skin_colors(css)
            entry["base"] = skin_base_of(canvas)
            panel_m = _ALIAS_PANEL_RE.search(css) or _ALIAS_L1_RE.search(css)
            panel = panel_m.group(1).strip() if panel_m else f"color-mix(in srgb, {canvas} 88%, white)"
            entry["colors"] = {"canvas": canvas, "text": text, "panel": panel,
                               "accent": entry["accent"] or "#4a90d9"}
        bg = resolve_skin_background(skin_id, base)
        if bg is not None:
            entry["has_background_media"] = True
            media: dict[str, Any] = {}
            for mode in ("dark", "light"):
                spec = bg.get(mode)
                if spec:
                    media[mode] = {
                        "src": spec["src"],
                        "scrim": spec.get("scrim", ""),
                        "asset_url": f"/ext/dsh_adapter/styles/skin-assets/{skin_id}/{spec['src']}",
                    }
            entry["background_media"] = media
        out.append(entry)
    return out


# 背景图资产扩展名白名单（backgroundMedia src / favicon 类资产 serve 用）
SKIN_ASSET_EXTS = {".webp", ".jpg", ".jpeg", ".png", ".svg", ".gif", ".avif"}


def resolve_skin_background(skin_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """解析皮肤 v2 manifest 的声明式背景媒体（contributes.backgroundMedia）。

    返回 ``{"dark": {src, scrim}, "light": {src, scrim}, "skin": id}``（任一
    态缺省则该键缺省）；无声明/皮肤不存在返回 None。图片文件存在性由
    serve 端资产路由兜底（此处只翻译声明，与 skin-center 宿主的职责切分
    一致——DSH 侧也是宿主渲染 backgroundMedia，不靠皮肤 JS）。
    """
    if not skin_id or "/" in skin_id or "\\" in skin_id or ".." in skin_id:
        return None
    base = Path(base_dir) if base_dir is not None else SKIN_CENTER_SKINS_DIR
    manifest = base / skin_id / "skin.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bm = data.get("contributes", {}).get("backgroundMedia") if isinstance(data, dict) else None
    if not isinstance(bm, dict):
        return None
    out: dict[str, Any] = {"skin": skin_id}
    for mode in ("dark", "light"):
        spec = bm.get(mode)
        if isinstance(spec, dict) and isinstance(spec.get("src"), str):
            out[mode] = {"src": spec["src"], "scrim": spec.get("scrim", "")}
    # 两态全缺 = 声明无效
    if "dark" not in out and "light" not in out:
        return None
    return out


_ALIAS_RE = re.compile(r"--dsw-alias-bg-base:\s*([^;]+);")
_ALIAS_L1_RE = re.compile(r"--dsw-alias-bg-layer-1:\s*([^;]+);")
_ALIAS_PANEL_RE = re.compile(r"--dsw-alias-bg-panel:\s*([^;]+);")
_DSW_FONT_RE = re.compile(r"--dsw-font-family:\s*([^;]+);")
_BG_RE = re.compile(r"background-color:\s*([^;]+);")
_FG_RE = re.compile(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))\s*;")
_ROOT_BGIMG_RE = re.compile(r"background-image:\s*(.+);")


def _hex_to_hsl(hexc: str) -> str:
    """'#RRGGBB' → 'H S% L%'（shadcn 变量格式，无 hsl() 包裹）。非 hex 原样返回。"""
    m3 = re.fullmatch(r"#([0-9a-fA-F]{3})", hexc.strip())
    m6 = re.fullmatch(r"#([0-9a-fA-F]{6})", hexc.strip())
    if m3:
        h = "".join(c * 2 for c in m3.group(1))
    elif m6:
        h = m6.group(1)
    else:
        return hexc.strip()
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        hue = sat = 0.0
    else:
        d = mx - mn
        sat = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            hue = ((g - b) / d + (6 if g < b else 0)) / 6
        elif mx == g:
            hue = ((b - r) / d + 2) / 6
        else:
            hue = ((r - g) / d + 4) / 6
    return f"{round(hue * 360)} {round(sat * 100)}% {round(l * 100)}%"


def _luminance(hexc: str) -> float:
    """'#RRGGBB' → 相对亮度 0~1（WCAG 公式）；非 hex 返回 0（按暗处理）。"""
    m3 = re.fullmatch(r"#([0-9a-fA-F]{3})", hexc.strip())
    m6 = re.fullmatch(r"#([0-9a-fA-F]{6})", hexc.strip())
    if m3:
        h = "".join(c * 2 for c in m3.group(1))
    elif m6:
        h = m6.group(1)
    else:
        return 0.0
    chan = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        chan.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def skin_base_of(canvas: str) -> str:
    """皮肤基准（暗/亮）判定：画布色相对亮度（skin tags 无 dark/light 标签，
    全按 tags 判定恒 dark 是错的——miku 画布 #eef5ff 实为亮色皮肤）。"""
    return "light" if _luminance(canvas) > 0.35 else "dark"


# === 区域背景 + 对比度强制（对照原生 skin-center 0.2.6） ===
# 原生三区关系真源：页面级背景（全页插画/画布）+ --dsw-specific-sidebar-fill
# （侧栏唯一自带表面，含原生透明度）+ conversation/workspace 无自带表面
# （透出页面背景）。翻译 = region 变量按原生值发射，透明即透出统一背景层。

_CSS_COLOR_RE = re.compile(
    r"^(?P<hex>#[0-9a-fA-F]{3,8})"
    r"|^rgba?\(\s*(?P<r>\d+)\s*,\s*(?P<g>\d+)\s*,\s*(?P<b>\d+)"
    r"(?:\s*,\s*(?P<a>[0-9.]+))?\s*\)$"
)


def _parse_css_color(value: str) -> tuple[int, int, int, float] | None:
    """CSS 颜色串 → (r, g, b, a)。支持 #rgb/#rrggbb/#rrggbbaa/rgb()/rgba()。
    其余（color-mix/calc 包裹/变量引用）无法静态解析 → None。"""
    v = value.strip()
    if v == "transparent":
        return (0, 0, 0, 0.0)
    m = _CSS_COLOR_RE.match(v)
    if not m:
        return None
    if m.group("hex"):
        h = m.group("hex")[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16) / 255)
        return None
    a = float(m.group("a")) if m.group("a") is not None else 1.0
    return (int(m.group("r")), int(m.group("g")), int(m.group("b")), a)


def _composite_rgba(fg: tuple[int, int, int, float], bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """前景（含 alpha）合成到实底背景上 → 实色。"""
    a = max(0.0, min(1.0, fg[3]))
    return (round(fg[0] * a + bg[0] * (1 - a)),
            round(fg[1] * a + bg[1] * (1 - a)),
            round(fg[2] * a + bg[2] * (1 - a)))


def _wcag_luminance(rgb: tuple[int, int, int]) -> float:
    chan = []
    for i in range(3):
        c = rgb[i] / 255
        chan.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def _wcag_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _wcag_luminance(a), _wcag_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix_rgb(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (round(a[0] * (1 - t) + b[0] * t),
            round(a[1] * (1 - t) + b[1] * t),
            round(a[2] * (1 - t) + b[2] * t))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in rgb)


def _enforce_contrast(fg: tuple[int, int, int], bgs: list[tuple[int, int, int]],
                      min_ratio: float = 4.5, max_iter: int = 10) -> str:
    """把前景色向白/黑两个方向逐级混色，取对全部给定背景对比度更高的结果。
    步距 22% × 10 步 ≈ 全域：中间调背景上单方向存在"跨过背景亮度时的
    对比度凹陷"（白→黑方向前几步比值反而下降），小步数会被种子锁死在
    劣势端——xp 蓝画布、miku 蓝强调色都是反例。返回 #rrggbb。"""
    def run(target: tuple[int, int, int]) -> tuple[tuple[int, int, int], float]:
        cur: tuple[int, int, int] = fg
        best, best_r = cur, min(_wcag_ratio(cur, b) for b in bgs)
        for _ in range(max_iter):
            if best_r >= min_ratio:
                break
            cur = _mix_rgb(cur, target, 0.22)
            r = min(_wcag_ratio(cur, b) for b in bgs)
            if r > best_r:
                best, best_r = cur, r
        return best, best_r

    light, lr = run((255, 255, 255))
    dark, dr = run((0, 0, 0))
    return _rgb_to_hex(light if lr >= dr else dark)


_SIDEBAR_FILL_RE = re.compile(r"--dsw-specific-sidebar-fill:\s*([^;]+);")
_SIDEBAR_ALPHA_CALC_RE = re.compile(
    r"calc\(\s*([0-9.]+)\s*-\s*var\(--dsh-skin-scrim,\s*([0-9.]+)\)\s*\*\s*([0-9.]+)\s*\)"
)


_CONVERSATION_SURFACE_RE = re.compile(
    r'\[data-dsh-surface="conversation"\][^{]*\{([^}]*)\}'
)


# ── 气泡/输入面令牌提取：对话框和气泡区域颜色要跟皮肤一样——
# MessageItem 内联样式消费 --bubble-*-bg（CSS 覆盖不了内联），
# 皮肤原生气泡规则在 patches.css，翻译器提取原样声明值发射令牌 ──
# 暗色分支前缀（body[data-ds-dark-theme] ...）
_DARK_PREFIX = r'(?:body\[data-ds-dark-theme\]\s+)?'
# 用户气泡（DSH [class*="userRow"] [class*="bubble"]）
_USER_BUBBLE_RULE_RE = re.compile(
    _DARK_PREFIX + r'\[class\*=["\']userRow["\']\]\s*\[class\*=["\']bubble["\']\]\s*\{([^}]*)\}'
)
# AI 气泡（DSH [data-chat-flow-kind="assistant-step"] 消息 markdown 面）
_AI_BUBBLE_RULE_RE = re.compile(
    _DARK_PREFIX + r'\[data-chat-flow-kind="assistant-step"\]\s*>\s*\*\s*>\s*\*\s*>\s*\*\s*>\s*'
    r'div\[class\*="markdown"\]\s*\{([^}]*)\}'
)
# 输入卡（DSH [data-composer-card]：input 面 = 渐变 + base 色双层，
# 底层 var(--dsw-specific-input-major) 更接近真实面）
_INPUT_CARD_RULE_RE = re.compile(
    _DARK_PREFIX + r'\[data-composer-card\]\s*\{([^}]*)\}'
)
_BG_DECL_RE = re.compile(r'background\s*:\s*([^;]+);')


def _extract_bubble_tokens(css: str, dark: bool) -> dict[str, str]:
    """皮肤 patches.css → 气泡/输入卡底色令牌（--bubble-user-bg / --bubble-ai-bg /
    --chat-input-bg）。按基准态提取：dark=True 取 body[data-ds-dark-theme] 分支，
    否则取非暗色分支（皮肤 base 由画布亮度判定，令牌随基准态；运行期昼夜切换
    由皮肤 CSS 在 body[data-skin-dark] 下覆盖，内联 var() 保持基准态面）。"""
    out: dict[str, str] = {}

    def _pick(rule: re.Match[str]) -> str | None:
        m = _BG_DECL_RE.search(rule.group(1))
        if not m:
            return None
        val = m.group(1).strip()
        if not val or val == "none":
            return None
        # 多层渐变合成面（composer 等）：取最后一段（渐变基底色/var 更接近真实面）
        if "var(--dsw-specific-input-major)" in val:
            last = "#fffdf8d1"  # maid 原生输入面 base（静态已知，避引 var 环）
        else:
            last = val.split(",")[-1].strip().rstrip(")").strip()
        return last

    for name, pat in (
        ("--bubble-user-bg", _USER_BUBBLE_RULE_RE),
        ("--bubble-ai-bg", _AI_BUBBLE_RULE_RE),
        ("--chat-input-bg", _INPUT_CARD_RULE_RE),
    ):
        for m in pat.finditer(css):
            if bool(_DARK_PREFIX.strip() and m.group(0).startswith("body[data-ds-dark-theme]")) != dark:
                continue
            val = _pick(m)
            if val:
                out[name] = val
            break
    return out


def _resolve_conversation_bg(css: str) -> str:
    """skin.css → 对话主区背景值（原生 conversation 表面规则）。

    当前 16 款皮肤均无 conversation 表面规则（页面级背景即对话区）→
    'transparent'（全页立绘/画布透出）。若未来皮肤给对话区画了表面
    （含透明度），原样返回——聊天区与工作区同享此值。"""
    m = _CONVERSATION_SURFACE_RE.search(css)
    if not m:
        return "transparent"
    bg = _BG_RE.search(m.group(1))
    if not bg:
        return "transparent"
    raw = bg.group(1).strip()
    parsed = _parse_css_color(raw)
    if parsed is None:
        return "transparent"
    r, g, b, a = parsed
    if a <= 0.01:
        return "transparent"
    if a >= 0.99:
        return _rgb_to_hex((r, g, b))
    return f"rgba({r}, {g}, {b}, {a:g})"


def _resolve_sidebar_fill(css: str) -> str | None:
    """skin.css → 侧栏区域背景值（原生 --dsw-specific-sidebar-fill）。

    - 值是 rgba/#rrggbbaa 时原样保留透明度（浏览器在背景层之上合成）——
      正是原生"半透明面板透出插画"的语义；
    - 已知透明度表达式（calc(1 - var(--dsh-skin-scrim, X) * Y)）按静态默认
      （scrim=0，背景遮挡滑杆默认位）求值；
    - 全透明 → 'transparent'（整栏透出统一背景层）；无法静态解析 → None
      （不发射，回退链兜底 → 行为与原生一致：侧栏透出页面背景）。"""
    m = _SIDEBAR_FILL_RE.search(css)
    if not m:
        return None
    raw = m.group(1).strip()
    if "calc(" in raw and "var(--dsh-skin-scrim" in raw:
        am = _SIDEBAR_ALPHA_CALC_RE.search(raw)
        if not am:
            return None
        alpha = float(am.group(1)) - float(am.group(2)) * float(am.group(3))
        cm = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", raw)
        if not cm:
            return None
        r, g, b = (int(cm.group(i)) for i in (1, 2, 3))
        return "transparent" if alpha <= 0.01 else f"rgba({r}, {g}, {b}, {alpha:g})"
    parsed = _parse_css_color(raw)
    if parsed is None:
        return None
    r, g, b, a = parsed
    if a <= 0.01:
        return "transparent"
    if a >= 0.99:
        return _rgb_to_hex((r, g, b))
    return f"rgba({r}, {g}, {b}, {a:g})"


def _extract_skin_colors(css: str) -> tuple[str, str]:
    """skin.css → (canvas, text)：--dsw-alias-bg-base / :root background-color / color。"""
    bg_m = _BG_RE.search(css) or _ALIAS_RE.search(css)
    fg_m = _FG_RE.search(css)
    canvas = bg_m.group(1).strip() if bg_m else "#111318"
    text = fg_m.group(1).strip() if fg_m else "#e6e6e6"
    return canvas, text


def skins_to_plugin_themes(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """DSH 皮肤 → 灵汐 PluginTheme 声明（contributes.themes 条目）。

    dsh_adapter = 特殊皮肤插件：装载的每套皮肤以 contributes.themes 声明，
    前端既有插件主题通道自动发现/渲染/选择，零前端改动；添加皮肤 = 放包进
    dsh_plugins（适配器 on_load 自动同步本声明，无需手工翻译或改 manifest）。

    条目形态（PluginTheme，types/theme.ts）：
    - id: dsh-skin-<skin>（contributionRegistry 全局键 dsh_adapter:dsh-skin-*）
    - base: 画布色亮度判定（基准回退：先 applyTheme(base) 再 variables 后写者胜）
    - variables: --ds-* 令牌 + shadcn 桥（对比度强制）+ --font-ui + --region-*
    - backgrounds.image: 立绘 → 灵汐原生背景图层（overlay 取 scrim 首 rgba）

    三区背景关系（对照原生 0.2.6 语义）：
    - 原生只有两个区：sidebar（侧栏，唯一自带表面 --dsw-specific-sidebar-fill
      含原生透明度）+ conversation（对话主区，页面级背景，无自带表面）。
      原生没有工作区。
    - 映射：原生 sidebar → --region-sidebar-bg；原生 conversation →
      --region-chat-bg 与 --region-workspace-bg **同源同值**（我们的聊天区
      + 工作区合计 = 原生的对话主区，若皮肤给对话区画了表面，两区同享）。
    - 全页立绘 = 对话主区透出页面背景（transparent），侧栏按原生 fill
      覆盖其上——"铺满整个页面"由此自然成立。
    """
    themes: list[dict[str, Any]] = []
    for skin in describe_available_skins(base_dir):
        c = skin.get("colors") or {}
        canvas = c.get("canvas", "")
        text = c.get("text", "")
        panel = c.get("panel", "")
        accent = c.get("accent", "")
        base = skin.get("base", "dark")

        skin_css = SKIN_CENTER_SKINS_DIR / str(skin["id"]) / "skin.css"
        if base_dir is not None:
            skin_css = Path(base_dir) / str(skin["id"]) / "skin.css"
        css = ""
        if skin_css.is_file():
            try:
                css = skin_css.read_text(encoding="utf-8", errors="replace")
            except OSError:
                css = ""

        variables: dict[str, str] = {}

        # 气泡/输入面令牌：气泡与对话框颜色跟皮肤一样。
        # 原生气泡规则在 patches.css（skin.css 只有配色令牌）——两文件同目录
        # 同加载；令牌按基准态发射（内联 var() 静态面，昼夜覆盖归皮肤 CSS）
        patches_path = (Path(base_dir) if base_dir is not None else SKIN_CENTER_SKINS_DIR) / str(skin["id"]) / "patches.css"
        patches_css = ""
        if patches_path.is_file():
            try:
                patches_css = patches_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                patches_css = ""
        if patches_css:
            for k, v in _extract_bubble_tokens(patches_css, dark=(base == "dark")).items():
                variables[k] = v

        if canvas:
            variables["--ds-bg-canvas"] = canvas
            variables["--ds-bg-elevated"] = f"color-mix(in srgb, {canvas} 82%, white)"
        if panel:
            variables["--ds-bg-panel"] = panel
        if accent:
            variables["--ds-accent-primary"] = accent
            variables["--ds-accent-ai"] = accent
            variables["--ds-bg-hover"] = f"color-mix(in srgb, {accent} 12%, transparent)"
        if text and canvas:
            variables["--ds-text-primary"] = text
            variables["--ds-text-secondary"] = f"color-mix(in srgb, {text} 78%, {canvas})"
            variables["--ds-text-muted"] = f"color-mix(in srgb, {text} 55%, {canvas})"
            variables["--ds-border-subtle"] = f"color-mix(in srgb, {text} 16%, transparent)"

        # 区域背景关系（原生语义发射）：侧栏取原生 fill；对话主区取一个值
        # （conversation 表面规则或页面背景透明），同时喂给聊天区与工作区
        # （两区合计 = 原生对话主区）
        sidebar_bg = _resolve_sidebar_fill(css)
        if sidebar_bg is not None:
            variables["--region-sidebar-bg"] = sidebar_bg
        main_bg = _resolve_conversation_bg(css)
        variables["--region-chat-bg"] = main_bg
        variables["--region-workspace-bg"] = main_bg
        # AI 消息平铺开关：跟 DeepSeek/DSH 原生——
        # 用户气泡 + AI 平铺；角色扮演类主题可声明 bubble 恢复气泡
        variables["--bubble-ai-mode"] = "flat"

        # shadcn 桥（必须 H S% L% 纯串）+ 对比度强制：文本令牌对画布与
        # 侧栏合成面达 WCAG 4.5（字体与背景无差别的根因 = 基准主题残留
        # 令牌混在皮肤面上；强制后任何面色下都保证可读）
        canvas_rgb = _parse_css_color(canvas) if canvas else None
        text_rgb = _parse_css_color(text) if text else None
        accent_rgb = _parse_css_color(accent) if accent else None
        if canvas_rgb is not None and text_rgb is not None:
            canvas_solid = (canvas_rgb[0], canvas_rgb[1], canvas_rgb[2])
            sidebar_rgba = _parse_css_color(sidebar_bg) if sidebar_bg and sidebar_bg != "transparent" else None
            sidebar_solid = (
                _composite_rgba(sidebar_rgba, canvas_solid)
                if sidebar_rgba is not None else canvas_solid
            )
            # 桥表面一律画布族（muted/secondary/popover/card 派生自画布，
            # 前景只对画布强制 ≥4.5）——侧栏面是原生 fill（常为中间调合成，
            # 无单色可达 4.5），由 --region-sidebar-fg 区域变量单独服务，
            # 桥令牌不与之混用（中间混合面 = 双面皆输）
            fg_hex = _enforce_contrast(text_rgb[:3], [canvas_solid])
            canvas_shift = _mix_rgb(canvas_solid, (255, 255, 255) if _wcag_luminance(canvas_solid) < 0.5 else (0, 0, 0), 0.08)
            # muted/secondary 前景对画布与派生面（8% 偏移）双面强制
            sec_hex = _enforce_contrast(_mix_rgb(text_rgb[:3], canvas_solid, 0.20), [canvas_solid, canvas_shift])
            mut_hex = _enforce_contrast(_mix_rgb(text_rgb[:3], canvas_solid, 0.42), [canvas_solid, canvas_shift])
            border_hex = _enforce_contrast(_mix_rgb(text_rgb[:3], canvas_solid, 0.16), [canvas_solid], min_ratio=1.5)
            variables["--background"] = _hex_to_hsl(canvas)
            variables["--foreground"] = _hex_to_hsl(fg_hex)
            variables["--card"] = _hex_to_hsl(canvas)
            variables["--muted"] = _hex_to_hsl(_rgb_to_hex(canvas_shift))
            variables["--muted-foreground"] = _hex_to_hsl(mut_hex)
            variables["--secondary"] = _hex_to_hsl(_rgb_to_hex(canvas_shift))
            variables["--secondary-foreground"] = _hex_to_hsl(sec_hex)
            variables["--popover"] = _hex_to_hsl(canvas)
            variables["--popover-foreground"] = _hex_to_hsl(fg_hex)
            variables["--border"] = _hex_to_hsl(border_hex)
            variables["--input"] = _hex_to_hsl(border_hex)
            # 区域前景：侧栏面与画布亮度对立时（xp 亮侧栏/黑画布、cyber-night
            # 亮侧栏/黑画布等）全局文本无法两全——按侧栏面单独强制一档前景，
            # index.css 经 .theme-sidebar-area 作用域重绑定 --muted-foreground
            # 等变量（CSS 变量继承重算，无需改任何组件）
            if sidebar_solid != canvas_solid:
                sb_fg = _enforce_contrast(text_rgb[:3], [sidebar_solid])
                sb_muted = _enforce_contrast(_mix_rgb(text_rgb[:3], sidebar_solid, 0.30), [sidebar_solid])
                variables["--region-sidebar-fg"] = sb_fg
                variables["--region-sidebar-muted-fg"] = sb_muted
            # 区域前景（对话主区）：聊天区+工作区同面同值（用户三裁：
            # 所有文字/图标的颜色按所在区域背景翻转）——对话面 = conversation
            # 表面规则或透出的画布；当前 16 皮均为透明（与全局一致，不发射，
            # 回退全局），未来皮肤画了对话面则两区按该面单独强制
            main_rgba = _parse_css_color(main_bg) if main_bg and main_bg != "transparent" else None
            main_solid = _composite_rgba(main_rgba, canvas_solid) if main_rgba is not None else canvas_solid
            if main_solid != canvas_solid:
                ch_fg = _enforce_contrast(text_rgb[:3], [main_solid])
                ch_muted = _enforce_contrast(_mix_rgb(text_rgb[:3], main_solid, 0.30), [main_solid])
                variables["--region-chat-fg"] = ch_fg
                variables["--region-chat-muted-fg"] = ch_muted
                variables["--region-workspace-fg"] = ch_fg
                variables["--region-workspace-muted-fg"] = ch_muted
            if accent_rgb is not None:
                accent_solid = (accent_rgb[0], accent_rgb[1], accent_rgb[2])
                accent_fg_seed = (255, 255, 255) if _wcag_luminance(accent_solid) < 0.6 else (0, 0, 0)
                accent_fg_hex = _enforce_contrast(accent_fg_seed, [accent_solid])
                variables["--primary"] = _hex_to_hsl(accent)
                variables["--primary-foreground"] = _hex_to_hsl(accent_fg_hex)
                variables["--accent"] = _hex_to_hsl(accent)
                variables["--accent-foreground"] = _hex_to_hsl(accent_fg_hex)
                variables["--ring"] = _hex_to_hsl(accent)

        # 皮肤字体 → --font-ui（主题管线原生消费 main.tsx fontFamily）
        if css:
            m = _DSW_FONT_RE.search(css)
            if m:
                variables["--font-ui"] = m.group(1).strip()

        entry: dict[str, Any] = {
            "id": f"dsh-skin-{skin['id']}",
            "name": skin.get("name") or skin["id"],
            "description": f"{skin.get('tagline') or skin['id']}（DSH 皮肤 · dsh_adapter）",
            "base": base,
            # 平台皮肤运行时声明：声明 skin 字段
            # 即激活按择注入（merged.css/hooks.mjs/资产三端点按标准路径递送）
            "skin": str(skin["id"]),
        }
        if variables:
            entry["variables"] = variables
        # 立绘 → 原生背景图层（取 base 对应态：皮肤自选基准优先于系统偏好）
        bg = skin.get("background_media") or {}
        media = bg.get(base) or bg.get("dark") or bg.get("light")
        if media:
            scrim = str(media.get("scrim", ""))
            m = re.search(r"rgba?\([^)]+\)", scrim)
            overlay = m.group(0) if m else ("rgba(255,255,255,0.45)" if base == "light" else "rgba(0,0,0,0.35)")
            entry["backgrounds"] = {
                "image": {
                    "enabled": True,
                    "url": media["asset_url"],
                    "position": "center",
                    "size": "cover",
                    "attachment": "fixed",
                    "overlay": overlay,
                    "overlayOpacity": 1,
                }
            }
        themes.append(entry)
    return themes
