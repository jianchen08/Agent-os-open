"""角色卡声明面结构校验单源（卡片/渲染域主 mode_roleplay 内部实现，T6c）。

把卡声明三面——variables（类型面/enum 面）、behaviors（trigger regex 面/effect
形态面）、actions（target 面）——的结构校验提为可复用纯函数。生产消费面：
① 本插件 server.py 卡导入（warnings 随响应上行，只标注不阻断）+ 创作 lint
（草稿工作台，阻断面）+ pack 呈现面归一剔除；② 装载侧 agent_config_load 经
capability 服务消费（card.lint_decl / card.lint_actions / card.lint_pack_ui）；
③ 出厂内容提案安检（mode_evolution content_audit 经 card.lint_extensions /
card.scan_* 服务）。跨插件一律服务直连（requires_services），禁模块级 import。

与执行面的同源关系：声明契约三件套（``SUPPORTED_VARIABLE_TYPES`` 类型执法集 /
``flatten_variables`` 声明叶遍历 / ``keep_state_machine_spec`` keep 规格归一）
单源在 ``_libs/card_contract`` 包，本模块与 State Contract Validator
（state_marker_parse/state_contract.py）经该包共用同一套判定。本模块只做结构
判定，不做值结算（op/clamp/hard_rules 归 Validator）。

内容安全扫描单源（扩展波 B-Ⅱ 收编）：suspicious 注入形状 + 高危 HTML 形态
两模式表收编于此（收编自本插件 server.py，模式表只此一份禁第四套），
出厂内容提案安检面（mode_evolution content_audit，B-Ⅱ FR-F6）经服务消费。

全部纯函数、零 IO：输入声明 dict/list，输出 warnings 字符串列表（作者可读中文）
+ actions 返回过滤后的规范化列表（缺省 target 补齐，入参不被突变）。

反劣化词表扫描（C1 整合刀 2026-10-13）：禁词/禁式样两张数据表（英/中）单源在
姊妹数据模块 card_lint_slop_en / card_lint_slop_zh（裸名 import，插件目录同级
兄弟；条目 = 事实性词汇/短语 + note（source 考据在数据文件条目行尾注释——说明面，不进数据与命中输出）
回链），本模块只做匹配与可读消息组装（advisory 标注，不阻断）。
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

from _libs.card_contract import (
    SUPPORTED_VARIABLE_TYPES,
    flatten_variables,
    keep_state_machine_spec,
)
from card_lint_slop_en import ANTISLOP_EN_PATTERNS, ANTISLOP_EN_TERMS
from card_lint_slop_zh import ANTISLOP_ZH_PATTERNS, ANTISLOP_ZH_TERMS

# actions[].target 合法值（批 4/5 宿主下发消费；本刀仅声明校验）
ACTION_TARGETS = ("engine", "game")
DEFAULT_ACTION_TARGET = "engine"

# ── pack 呈现面声明（RP 面板 v3.1 刀1）：pack 顶层 ui / init_ui 两可选键的
# 结构校验单源——装载侧（agent_config_load 经 card.lint_pack_ui 服务的 warning
# 面）与呈现消费侧（本插件 server 归一剔除面：大厅卡 ui/has_page、开演设置页
# init_ui、自包含 ui.page）同源禁第二套。lineage 谱系档位：pure 纯扮演 /
# world ＋世界卡 / full 完整玩法
# （按包声明档位折叠，缺省 full；非法值消费侧剔除回落缺省档 + warning）。
# effects 键域校验放消费侧（须在卡+包 variables 声明内，Validator fail-closed），
# 本模块只做形态校验；theme_css/page 文件存在性归消费侧（本模块零 IO）。
PACK_LINEAGES = ("pure", "world", "full")
DEFAULT_PACK_LINEAGE = "full"

# pack 顶层 ui 已知键（icon 大厅图标 / style_tags 画风标签 / lineage 谱系档位 /
# theme_css 包内主题相对路径 / page 自包含玩法页相对路径）
_UI_KEYS = ("icon", "style_tags", "lineage", "theme_css", "page")

# init_ui 已知键（title/intro 文案、show_variables 变量缺省档开关、routes
# 开局路线单选、trait_pools 词条池多选）
_INIT_UI_KEYS = ("title", "intro", "show_variables", "routes", "trait_pools")

# pack 顶层 pipeline 声明（世界内嵌谱系右移批次一，调研 §6.4）：玩法级管道
# 绑定——玩法自带交互形态（纯对话链/规则链/跑团链各配管道），与角色级 agent
# 键两级正交（ADR 2026-10-17 权威光谱框架）。登记名形态与 agent_manager
# mode.yaml pipelines[].name 同形（跨插件禁模块级直连，两处各持同形正则；
# 执法闸 = 消费侧登记存在性，形态仅作者早反馈），值合法性校验在消费面
# （server 侧 <user_config>/pipelines 登记存在，非法剔除回落缺省 + warning，
# 与 ui.lineage 剔除回落同构）——本模块零 IO 只做形态。
PACK_PIPELINE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DEFAULT_PACK_PIPELINE = "roleplay"

# pack 顶层 operations 声明（世界内嵌谱系右移批次二，调研 §7.6 真缺口①）：自由
# 操作→结构化命令的声明面——枚举声明制（页上可发的操作只来自清单，光谱判据：
# 枚举必败的才归 LLM）。清单条目 = {id, label, params_schema?, effects}；effects
# 形态与 behaviors effect 同构（标准 op 串），键域校验归消费侧 Validator
# fail-closed（与 init_ui effects 同闸，本模块零 IO 不判键域）。
OPERATION_ID_RE = PACK_PIPELINE_RE
_OPERATION_KEYS = ("id", "label", "params_schema", "effects")
_OPERATION_PARAM_TYPES = ("string", "bool", "int", "float")

# pack 顶层 tick 声明（世界内嵌谱系右移批次三，调研 §7.6 真缺口②）：世界演化
# 周期推进——{steps: [{when?, effects}]}，steps 声明序逐条判定，when 缺省恒真。
# 结构化简单条件（{key, op, value}）不引入表达式语言（4a 求值器在前端引擎区，
# 服务端不复制第二套）；effects 值两形态：op 串（±/=，Validator 同源）或
# enum cycle 算子 {cycle: [非空列表]}（编排侧解析为绝对值，Validator 契约零
# 改动）。when 键域（key ∈ 包 variables）归消费侧 fail-closed（server 侧
# _pack_tick_listing——条件求值不经 Validator，消费侧是唯一执法位），本模块
# 零 IO 只判形态。
TICK_WHEN_OPS = ("==", "!=", ">=", "<")
_TICK_KEYS = ("when", "effects")
_TICK_WHEN_KEYS = ("key", "op", "value")


def lint_tick(tick: Any) -> list[str]:
    """pack 顶层 tick 世界演化声明结构校验；返回 warnings（合法 = 空表）。

    None（键缺席 = 无周期推进面，tick 端点空转）= 空；非映射 = 单条警告。steps
    须为非空列表；逐步骤：条目须映射、effects 须非空映射且逐值 = 非空 op 串或
    ``{cycle: [非空列表]}``（cycle 规格外未知键警告）、when（选填）须为
    ``{key, op ∈ TICK_WHEN_OPS, value}`` 映射、未知键警告（作者笔形可见）。
    只报结构，不判 when 键域与 effects 键域（归消费侧/Validator）。
    """
    if tick is None:
        return []
    if not isinstance(tick, dict):
        return ["tick: 须为映射（世界演化周期推进声明 {steps: [...]}）"]
    steps = tick.get("steps")
    if not isinstance(steps, list):
        return [f"tick.steps: 须为步骤列表，得 {type(steps).__name__}"]
    if not steps:
        return ["tick.steps: 须为非空列表（空声明 = 永久空转，疑笔误）"]
    warnings: list[str] = []
    for index, entry in enumerate(steps):
        path = f"tick.steps[{index}]"
        if not isinstance(entry, dict):
            warnings.append(f"{path}: 条目须为映射")
            continue
        for key in entry:
            if key not in _TICK_KEYS:
                warnings.append(f"{path}.{key}: 未知声明键（已知 {list(_TICK_KEYS)}）")
        warnings.extend(_lint_tick_effects(entry.get("effects"), path))
        warnings.extend(_lint_tick_when(entry.get("when"), path))
    return warnings


def _lint_tick_effects(effects: Any, path: str) -> list[str]:
    """effects 形态校验：非空映射，逐值 = 非空 op 串或 {cycle: 非空列表}。"""
    if not isinstance(effects, dict) or not effects:
        return [f"{path}.effects: 须为非空映射（状态键 → op 串/cycle 算子）"]
    warnings: list[str] = []
    for key, spec in effects.items():
        if isinstance(spec, dict):
            for spec_key in spec:
                if spec_key != "cycle":
                    warnings.append(f"{path}.effects.{key}.{spec_key}: cycle 算子规格外未知键")
            values = spec.get("cycle")
            if not isinstance(values, list) or not values:
                warnings.append(
                    f"{path}.effects.{key}: cycle 须为非空列表（枚举循环序列），"
                    f"得 {type(values).__name__}"
                )
        elif not isinstance(spec, str) or not spec.strip():
            warnings.append(
                f"{path}.effects.{key}: 须为非空 op 串（±/=，Validator 同构）"
                f"或 {{cycle: [...]}} 算子，得 {type(spec).__name__}"
            )
    return warnings


def _lint_tick_when(when: Any, path: str) -> list[str]:
    """when 形态校验（选填；缺席 = 恒真）：{key 非空, op ∈ TICK_WHEN_OPS, value 在场}。"""
    if when is None:
        return []
    if not isinstance(when, dict):
        return [f"{path}.when: 须为映射（{{key, op, value}} 结构化简单条件）"]
    warnings: list[str] = []
    for key in when:
        if key not in _TICK_WHEN_KEYS:
            warnings.append(f"{path}.when.{key}: 未知声明键（已知 {list(_TICK_WHEN_KEYS)}）")
    cond_key = when.get("key")
    if not isinstance(cond_key, str) or not cond_key.strip():
        warnings.append(f"{path}.when.key: 须为非空字符串（本包 variables 声明路径）")
    if when.get("op") not in TICK_WHEN_OPS:
        warnings.append(
            f"{path}.when.op: {when.get('op')!r} 不在条件算子集 {list(TICK_WHEN_OPS)}"
        )
    if "value" not in when:
        warnings.append(f"{path}.when.value: 缺席（比较基准值必填）")
    return warnings


def lint_operations(operations: Any) -> list[str]:
    """pack 顶层 operations 操作声明结构校验；返回 warnings（合法 = 空表）。

    None（键缺席 = 无自由操作面，页零操作区）= 空；非列表 = 单条警告。逐条目：
    条目须映射、id 须非空且登记名形态（``OPERATION_ID_RE``）且池内唯一、label
    须非空、effects 须映射、params_schema（选填）须为映射且逐字段
    {type ∈ int/float/string/bool, min?/max? 数值, default 与 type 同型}；
    未知键警告（作者笔形可见）。只报结构，不判值域与 effects 键域。
    """
    if operations is None:
        return []
    if not isinstance(operations, list):
        return ["operations 须为列表"]
    warnings: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(operations):
        path = f"operations[{index}]"
        if not isinstance(entry, dict):
            warnings.append(f"{path}: 条目须为映射")
            continue
        for key in entry:
            if key not in _OPERATION_KEYS:
                warnings.append(f"{path}.{key}: 未知声明键（已知 {list(_OPERATION_KEYS)}）")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            warnings.append(f"{path}: id 须为非空字符串（登记名）")
        elif not OPERATION_ID_RE.fullmatch(entry_id):
            warnings.append(
                f"{path}: id {entry_id!r} 形态非法（须为登记名 ^[a-z][a-z0-9_-]{{0,63}}$）"
            )
        elif entry_id in seen:
            warnings.append(f"{path}: id {entry_id!r} 重复")
        else:
            seen.add(entry_id)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            warnings.append(f"{path}: label 须为非空字符串")
        if not isinstance(entry.get("effects"), dict):
            warnings.append(f"{path}: effects 须为映射（状态键 → op，与 behaviors effect 同构）")
        warnings.extend(_lint_operation_params_schema(entry.get("params_schema"), path))
    return warnings


def _lint_operation_params_schema(params_schema: Any, path: str) -> list[str]:
    """params_schema 子集形态校验（选填；缺席 = None = 零参数契约）。"""
    if params_schema is None:
        return []
    if not isinstance(params_schema, dict):
        return [f"{path}.params_schema: 须为映射（字段名 → {{type, min?, max?, default?}}）"]
    warnings: list[str] = []
    for field, decl in params_schema.items():
        field_path = f"{path}.params_schema.{field}"
        if not isinstance(decl, dict):
            warnings.append(f"{field_path}: 须为映射")
            continue
        decl_type = decl.get("type")
        if decl_type not in _OPERATION_PARAM_TYPES:
            warnings.append(
                f"{field_path}: type {decl_type!r} 不在参数类型集 {list(_OPERATION_PARAM_TYPES)}"
            )
            continue
        for bound in ("min", "max"):
            value = decl.get(bound)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                warnings.append(f"{field_path}: {bound} 须为数值，得 {type(value).__name__}")
        default = decl.get("default")
        if default is None:
            continue
        type_ok = (
            isinstance(default, bool) if decl_type == "bool"
            else isinstance(default, str) if decl_type == "string"
            else isinstance(default, (int, float))
            and not isinstance(default, bool)
            and (decl_type == "float" or isinstance(default, int))
        )
        if not type_ok:
            warnings.append(f"{field_path}: default 与声明 type {decl_type} 不同型")
    return warnings


def lint_variables(variables: Any) -> list[str]:
    """variables 类型面/enum 面结构校验；返回 warnings（合法 = 空表）。

    None（无声明，出厂卡形态）= 空；非映射 = 单条警告。逐叶：叶缺 type / type
    出支持集 / enum 缺 values 列表 / 非法子树（skipped）各记一条（路径可定位）。
    只报结构，不判值域（min/max 语义归 Validator）。
    """
    if variables is None:
        return []
    if not isinstance(variables, dict):
        return ["variables 须为映射（键 → 声明/子组）"]
    flat, skipped = flatten_variables(variables)
    warnings = [f"variables 声明跳过（叶结构非法）: {path}" for path in skipped]
    for path, decl in flat.items():
        decl_type = decl.get("type")
        if decl_type not in SUPPORTED_VARIABLE_TYPES:
            warnings.append(
                f"variables.{path}: type {decl_type!r} 不在支持集 {list(SUPPORTED_VARIABLE_TYPES)}"
            )
        elif decl_type == "enum" and not isinstance(decl.get("values"), list):
            warnings.append(f"variables.{path}: enum 缺 values 列表")
    return warnings


def lint_behaviors(behaviors: Any) -> list[str]:
    """behaviors trigger regex 面/effect 形态面结构校验；返回 warnings。

    逐行为：声明非映射 / trigger 缺失或非字符串 / trigger 编译失败 / effect
    缺失或非映射各记一条（判定与消费面 fail-soft 跳过同判据）。
    """
    if behaviors is None:
        return []
    if not isinstance(behaviors, dict):
        return ["behaviors 须为映射（行为名 → {trigger, effect}）"]
    warnings: list[str] = []
    for name, decl in behaviors.items():
        if not isinstance(decl, dict):
            warnings.append(f"behaviors.{name}: 声明须为映射")
            continue
        trigger = decl.get("trigger")
        if not isinstance(trigger, str) or not trigger:
            warnings.append(f"behaviors.{name}: trigger 须为非空 regex 字符串")
        else:
            try:
                re.compile(trigger)
            except re.error as exc:
                warnings.append(f"behaviors.{name}: trigger regex 非法（{exc}）")
        if not isinstance(decl.get("effect"), dict):
            warnings.append(f"behaviors.{name}: effect 须为映射（状态键 → op）")
    return warnings


def lint_actions(actions: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """actions target 面校验：返回（有效条目规范化列表, warnings）。

    合法值 ``{engine, game}``；缺省补 ``engine``（显式值原样保留）；非法值剔除
    + 警告（fail-soft，与 regex 坏脚本同待遇）；非映射条目剔除。规范化落副本，
    入参不被突变。game 下发消费随批 4/5 宿主（本刀仅装载校验）。
    """
    if actions is None:
        return [], []
    if not isinstance(actions, list):
        return [], ["actions 须为列表"]
    valid: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            warnings.append(f"actions[{index}]: 条目须为映射，已剔除")
            continue
        target = action.get("target")
        if target is None:
            normalized = dict(action)
            normalized["target"] = DEFAULT_ACTION_TARGET
            valid.append(normalized)
        elif target in ACTION_TARGETS:
            valid.append(dict(action))
        else:
            warnings.append(
                f"actions[{index}]: target {target!r} 非法（可选 {list(ACTION_TARGETS)}），已剔除"
            )
    return valid, warnings


def lint_ui(ui: Any) -> list[str]:
    """pack 顶层 ui 呈现声明结构校验；返回 warnings（合法 = 空表）。

    None（未声明包形态）= 空；非映射 = 单条警告。逐键：icon 须字符串；
    style_tags 须字符串列表（空表合法 = 无标签）；lineage 须在谱系档枚举；
    theme_css / page 须非空字符串相对路径。未知键警告（作者笔形可见）。
    """
    if ui is None:
        return []
    if not isinstance(ui, dict):
        return ["ui 须为映射（呈现声明字段 → 值）"]
    warnings: list[str] = []
    for key, value in ui.items():
        if key not in _UI_KEYS:
            warnings.append(f"ui.{key}: 未知声明键（已知 {list(_UI_KEYS)}）")
        elif key == "style_tags":
            if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
                warnings.append("ui.style_tags: 须为字符串列表")
        elif key == "lineage":
            if value not in PACK_LINEAGES:
                warnings.append(f"ui.lineage: {value!r} 不在谱系档位 {list(PACK_LINEAGES)}")
        elif not isinstance(value, str) or not value.strip():
            warnings.append(f"ui.{key}: 须为非空字符串（相对路径或文本），得 {type(value).__name__}")
    return warnings


def lint_pack_pipeline(pipeline: Any) -> list[str]:
    """pack 顶层 pipeline 玩法级管道绑定声明形态校验；返回 warnings（合法=空表）。

    None（键缺席 = 缺省回落 roleplay，现役行为零变化）= 空；非字符串 / 空串 /
    登记名形态非法（``PACK_PIPELINE_RE``）各记一条。登记存在性归消费侧
    （server 侧同闸剔除回落，本函数零 IO）。
    """
    if pipeline is None:
        return []
    if not isinstance(pipeline, str) or not pipeline.strip():
        return [
            f"pipeline: {pipeline!r} 须为非空字符串（config/pipelines 登记名），"
            f"得 {type(pipeline).__name__}"
        ]
    if not PACK_PIPELINE_RE.fullmatch(pipeline):
        return [f"pipeline: {pipeline!r} 形态非法（须为登记名 ^[a-z][a-z0-9_-]{{0,63}}$）"]
    return []


def lint_init_ui(init_ui: Any) -> list[str]:
    """pack 顶层 init_ui 开演设置页声明结构校验；返回 warnings（合法 = 空表）。

    None（整键缺席 = 无自定义开演页，面板回落通用组合配置）= 空；非映射 =
    单条警告。title/intro 须字符串、show_variables 须布尔、routes/trait_pools
    逐条目结构校验（id 唯一、label 必填、profile/effects 选填——effects 只做
    形态校验，键域归消费侧）。
    """
    if init_ui is None:
        return []
    if not isinstance(init_ui, dict):
        return ["init_ui 须为映射（开演设置页声明）"]
    warnings: list[str] = []
    for key, value in init_ui.items():
        if key not in _INIT_UI_KEYS:
            warnings.append(f"init_ui.{key}: 未知声明键（已知 {list(_INIT_UI_KEYS)}）")
        elif key in ("title", "intro") and not isinstance(value, str):
            warnings.append(f"init_ui.{key}: 须为字符串，得 {type(value).__name__}")
        elif key == "show_variables" and not isinstance(value, bool):
            warnings.append(f"init_ui.show_variables: 须为布尔，得 {type(value).__name__}")
        elif key == "routes":
            warnings.extend(_lint_ui_routes(value))
        elif key == "trait_pools":
            warnings.extend(_lint_ui_trait_pools(value))
    return warnings


def _lint_ui_routes(routes: Any) -> list[str]:
    """routes 结构校验：条目映射、id 非空且池内唯一、label 必填、profile 选填。"""
    if routes is None:
        return []
    if not isinstance(routes, list):
        return ["init_ui.routes: 须为列表"]
    warnings: list[str] = []
    seen: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            warnings.append(f"init_ui.routes[{index}]: 条目须为映射")
            continue
        warnings.extend(_lint_ui_option(route, f"init_ui.routes[{index}]", seen, check_effects=False))
    return warnings


def _lint_ui_trait_pools(pools: Any) -> list[str]:
    """trait_pools 结构校验：池条目同 routes 口径 + options 词条逐条校验。"""
    if pools is None:
        return []
    if not isinstance(pools, list):
        return ["init_ui.trait_pools: 须为列表"]
    warnings: list[str] = []
    seen: set[str] = set()
    for index, pool in enumerate(pools):
        if not isinstance(pool, dict):
            warnings.append(f"init_ui.trait_pools[{index}]: 条目须为映射")
            continue
        warnings.extend(_lint_ui_option(pool, f"init_ui.trait_pools[{index}]", seen, check_effects=False))
        options = pool.get("options")
        if not isinstance(options, list) or not options:
            warnings.append(f"init_ui.trait_pools[{index}]: options 须为非空列表")
            continue
        seen_option: set[str] = set()
        for opt_index, option in enumerate(options):
            if not isinstance(option, dict):
                warnings.append(f"init_ui.trait_pools[{index}].options[{opt_index}]: 条目须为映射")
                continue
            path = f"init_ui.trait_pools[{index}].options[{opt_index}]"
            warnings.extend(_lint_ui_option(option, path, seen_option, check_effects=True))
    return warnings


def _lint_ui_option(
    entry: dict[str, Any], path: str, seen: set[str], *, check_effects: bool
) -> list[str]:
    """单条目（route/trait/option）共用口径：id 非空唯一、label 必填、
    profile 选填字符串、effects（仅词条层）选填映射——键域归消费侧。"""
    warnings: list[str] = []
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        warnings.append(f"{path}: id 须为非空字符串")
    elif entry_id in seen:
        warnings.append(f"{path}: id {entry_id!r} 重复")
    else:
        seen.add(entry_id)
    label = entry.get("label")
    if not isinstance(label, str) or not label.strip():
        warnings.append(f"{path}: label 须为非空字符串")
    profile = entry.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        warnings.append(f"{path}: profile 须为非空字符串（选填）")
    if check_effects and "effects" in entry and not isinstance(entry["effects"], dict):
        warnings.append(f"{path}: effects 须为映射（状态键 → op）")
    return warnings


def lint_card_extensions(extensions: Any) -> list[str]:
    """卡 extensions 四面（variables/behaviors/actions/engine）warnings 汇总便捷入口。

    导入侧/创作 lint 用；extensions 非映射（含 None，无声明卡形态）= 空表。
    """
    if not isinstance(extensions, dict):
        return []
    _, actions_warnings = lint_actions(extensions.get("actions"))
    return [
        *lint_variables(extensions.get("variables")),
        *lint_behaviors(extensions.get("behaviors")),
        *actions_warnings,
        *lint_engine(extensions.get("engine")),
    ]


# extensions.engine 已知键（RP 批 3，ADR 2026-10-06 作者权威声明面）：
# allow_cheat / cheat_overrides.keep_state_machine / allow_state_proposals 消费；
# llm / rule_complexity / contract_level 声明面先行只透传不消费（不假装生效）。
# allow_state_proposals（2026-10-13 动作驱动基线回退开关）：默认关——`<state>`
# 标记 state 字段解析忽略+审计；显式 true 恢复 LLM 提议合法来源（消费面
# state_marker_parse，读取口径同 allow_cheat 声明链）。
ENGINE_KEYS = (
    "llm",
    "rule_complexity",
    "allow_cheat",
    "allow_state_proposals",
    "contract_level",
    "cheat_overrides",
)


def lint_engine(engine: Any) -> list[str]:
    """extensions.engine 声明面结构校验（类型面）；返回 warnings（合法 = 空表）。

    None/空映射（未声明卡形态）= 空；非映射 = 单条警告。逐键类型面：llm /
    rule_complexity 须字符串、allow_cheat / allow_state_proposals 须布尔、
    contract_level 须整数（bool 是 int 子类显式排除）、cheat_overrides 须映射、
    未知键警告（作者笔形可见）；keep_state_machine 形态判定与消费面共用
    keep_state_machine_spec。只报结构，值域语义归消费面（allow_cheat=false
    拦截面 / keep 执法归 Validator / allow_state_proposals 缺席即默认关）。
    """
    if engine is None:
        return []
    if not isinstance(engine, dict):
        return ["engine 须为映射（声明字段 → 值）"]
    if not engine:
        return []
    warnings: list[str] = []
    for key, value in engine.items():
        if key not in ENGINE_KEYS:
            warnings.append(f"engine.{key}: 未知声明键（已知 {list(ENGINE_KEYS)}）")
        elif key in ("llm", "rule_complexity") and not isinstance(value, str):
            warnings.append(f"engine.{key}: 须为字符串，得 {type(value).__name__}")
        elif key in ("allow_cheat", "allow_state_proposals") and not isinstance(value, bool):
            warnings.append(f"engine.{key}: 须为布尔，得 {type(value).__name__}")
        elif key == "contract_level" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            warnings.append(f"engine.contract_level: 须为整数，得 {type(value).__name__}")
        elif key == "cheat_overrides":
            warnings.extend(_lint_cheat_overrides(value))
    return warnings


def _lint_cheat_overrides(overrides: Any) -> list[str]:
    """cheat_overrides 结构校验；keep_state_machine 形态判定单源在 spec 函数。"""
    if not isinstance(overrides, dict):
        return ["engine.cheat_overrides: 须为映射"]
    _, warnings = keep_state_machine_spec({"cheat_overrides": overrides})
    return warnings


# ── 内容安全扫描单源（扩展波 B-Ⅱ 收编自本插件 server.py，模式表
# 只此一份）：suspicious 注入形状 + 高危 HTML 形态。消费面四处同源——卡导入
# （mode_roleplay import）/ 世界书导入 / 创作 draft lint / 出厂内容提案安检
# （mode_evolution content_audit）。两表只标注语义由消费面定（创作 lint 与出厂
# 提案 = suspicious 阻断、HTML 标注；import 面 = 全部只标注），本模块只做
# 模式匹配与可读消息组装。

#: 可疑指令模式（大小写不敏感子串；命中建议人工检查）。
SUSPICIOUS_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "jailbreak",
    "developer mode",
    "dan mode",
    "绕过",
    "忽略之前",
)

#: 高危 HTML 形态（afengy/ST 卡正文常内嵌 HTML/CSS；渲染面由 webview
#: rpSanitizeHtml 按白名单消毒，命中建议人工检查）。
HTML_DANGER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("script 标签", re.compile(r"<script\b", re.IGNORECASE)),
    ("事件属性（on*=）", re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)),
    ("javascript: URL", re.compile(r"javascript\s*:", re.IGNORECASE)),
    ("嵌入元素（iframe/object/embed）", re.compile(r"<(iframe|object|embed)\b", re.IGNORECASE)),
    ("style 标签（可远程引入）", re.compile(r"<style\b", re.IGNORECASE)),
)


def walk_str_values(node: Any) -> list[str]:
    """任意嵌套结构收集全部字符串叶子（extensions/pack 各文本值扫描用）。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in walk_str_values(value)]
    if isinstance(node, (list, tuple)):
        return [s for value in node for s in walk_str_values(value)]
    return []


def scan_suspicious(haystack: str, subject: str = "角色设定") -> list[str]:
    """可疑指令模式扫描（大小写不敏感子串）；命中返回单条可读标注。

    subject 指明检查对象（卡=角色设定 / 世界书=世界书内容 / 玩法包=玩法包内容），
    消息措辞与收编前各消费面逐字一致。
    """
    lowered = haystack.lower()
    if any(pattern.lower() in lowered for pattern in SUSPICIOUS_PATTERNS):
        return [f"检测到可疑指令模式，建议人工检查{subject}"]
    return []


def scan_html_danger_texts(texts: list[str], subject: str = "角色卡内容") -> list[str]:
    """高危 HTML 形态扫描（文本集入参）；逐形态独立标注（同形态多文本命中合并）。"""
    return [
        f"检测到高危 HTML 形态（{label}），渲染面将按白名单消毒，建议人工检查{subject}"
        for label, pattern in HTML_DANGER_PATTERNS
        if any(pattern.search(text) for text in texts)
    ]


def scan_html_danger(card: dict[str, Any]) -> list[str]:
    """卡形态高危 HTML 扫描：description/personality/scenario/first_mes/
    alternate_greetings 及 extensions 各文本值（prompt 载体全字段）。"""
    texts = [str(card.get(field) or "") for field in ("description", "personality", "scenario", "first_mes")]
    greetings = card.get("alternate_greetings")
    if isinstance(greetings, list):
        texts.extend(str(greeting) for greeting in greetings)
    texts.extend(walk_str_values(card.get("extensions")))
    return scan_html_danger_texts(texts)


# ── 反劣化词表扫描（C1 整合刀）：词表数据单源在 card_lint_slop_en/zh 两姊妹
# 数据模块（顶部 import），本节只做匹配与可读消息组装——模式表惯例同上节
# （只此一份，禁第四套）。advisory 语义：命中=标注（数据面无源头字段，考据在数据文件行尾注释
# 回链与人工判断提示），是否采纳/阻断归消费面（C2 开演向导起接线）。

#: 命中展示截断（禁式样 match 文本可能很长，消息可读性优先）。
_ANTISLOP_MATCH_DISPLAY_MAX = 48


@cache
def _antislop_compiled(pattern: str) -> re.Pattern[str]:
    """禁式样正则编译缓存（数据表为常量集，同 pattern 串只编译一次）。"""
    return re.compile(pattern, re.IGNORECASE)


def scan_antislop(text: str, lang: str = "both") -> list[str]:
    """反劣化词表扫描：输入文本 → 命中词表条目 → 可读命中清单。

    lang 选表：``en``=英文表 / ``zh``=中文表 / ``both``=两表（缺省，RP 正文
    常中英混排）；非法值 ValueError 传播。词条目=大小写不敏感子串，禁式样=
    IGNORECASE 正则；每条目至多报一次（不按出现次数重复）。词表可空（空表
    零命中不炸），text 空串自然返回空表。
    """
    tables: dict[str, tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...]]] = {
        "en": (ANTISLOP_EN_TERMS, ANTISLOP_EN_PATTERNS),
        "zh": (ANTISLOP_ZH_TERMS, ANTISLOP_ZH_PATTERNS),
    }
    langs = ("en", "zh") if lang == "both" else (lang,)
    if any(code not in tables for code in langs):
        raise ValueError(f"lang {lang!r} 非法（可选 en/zh/both）")
    lowered = text.lower()
    notes: list[str] = []
    for code in langs:
        terms, patterns = tables[code]
        for entry in terms:
            if entry["term"].lower() in lowered:
                notes.append(
                    f"反劣化词表命中[{code}]: {entry['term']!r}——{entry['note']}"
                )
        for entry in patterns:
            match = _antislop_compiled(entry["pattern"]).search(text)
            if match:
                hit = match.group(0)
                shown = hit[:_ANTISLOP_MATCH_DISPLAY_MAX] + ("…" if len(hit) > _ANTISLOP_MATCH_DISPLAY_MAX else "")
                notes.append(
                    f"反劣化词表命中[{code}]: {shown!r}——{entry['note']}"
                )
    return notes
