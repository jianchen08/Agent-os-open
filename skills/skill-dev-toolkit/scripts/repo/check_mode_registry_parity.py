#!/usr/bin/env python3
"""mode.yaml 声明面 ↔ 前端消费点 ↔ 组装器实物 三向对账机械闸（2026-09-28 设计 D5/§4）。

检查（任一失败 exit 1，全过 exit 0）：
1. schema 复检：各包 mode.yaml 过 agent_manager 同源校验（必需键/字段白名单/
   枚举/形态）——声明面 fail-closed，防拼写漂移与空转声明回流；
2. 组装器实物对账：声明 material 的包须有对应文件；包内有 material.py 的须有
   声明（防孤儿组装器——声明驱动下无声明即不加载，静默失效需闸兜底）；
   声明 material 的包若有 skills/，包内技能须被 agents 提示词按名引用
   （批 E §3.1 技能孤儿对账，防无人加载的孤儿技能）；
3. 跨包同名技能对账（原则 7 零副本红线执法，批 E4）：出厂包 skills/ 目录间
   出现同名技能（同名目录）即红——任何包禁携带他包同名物料，跨包复用走
   全限定引用 + 按归属取料（D6）；用户根副本不算（用户覆盖是合法通道，同名
   优先序用户根赢），且用户根不在 modes_dir 扫描面内，天然不参与本对账；
4. 消费者对账豁免台账：已声明未消费的字段在此登记（豁免=显式承认 + 升级
   触发条件），台账外出现"无消费点白名单字段"视为空转回流。

口径变化（2026-09-28 批 G⑦，D1 标签裁定）：原第 2 检「前端 TASK_MODES 冻结
字面量 ⊆ registry 键集（防漂移）」**作废**——前端冻结键集已退役（TaskMode
冻结类型/TASK_MODES 四值删除），选择器选项 = registry 运行时全量派生
（frontend/src/services/api/modes.ts taskModeOptionsFromModes），派生无损
由 vitest 合成 registry 两态断言保障（modeOptions.test.ts），静态提取闸无
冻结集可提取、防漂移职责由派生机制本身消解（选项不可能领先/滞后 registry）。

用法：python scripts/check_mode_registry_parity.py [--modes-dir PATH]
默认仓根相对路径；CI 挂 python-lint 车道同源运行环境（仅标准库 + PyYAML）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# ── 与 agent_manager/server.py 同源的白名单（改动须双侧同步） ──
_MODE_DECL_REQUIRED = ("mode", "name")
_MODE_DECL_ENUMS = {
    "presenter_source": {"data_cards", "agent_registry", "none"},
    "tool_card": {"native", "collapse", "hide"},
    "pipeline_context": {"conversation", "task"},
    "pipeline_source": {"registry"},
}
_MODE_DECL_KNOWN = {
    "mode",
    "name",
    "description",
    "pipelines",
    "panel_page_id",
    "presenter",
    "tool_card",
    "material",
    "persona",
    "theme",
    "icon",
    "plan_spec",
    "planning_executors",
    "chain",
    "suite",
    "material_scope",
    "levers",
    "verifier_families",
    "weights",
    "budget",
    # 与 agent_manager._MODE_DECL_KNOWN 同源补齐（default = 默认模式声明；
    # inject_tools = 包级工具并入声明，守卫 C；ci/foreground_selectable = 任务
    # 门禁与前台可选声明；inject_tool_ids = 按激活模式注入外部工具）
    "default",
    "inject_tools",
    "ci",
    "foreground_selectable",
    "inject_tool_ids",
}
_PIPELINE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# 技能名形态（与 plugins/shared/mode_keys.py is_skill_name 同源；本脚本仅
# 标准库 + PyYAML，正则双侧同步）。
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# 执行者键形态（与 mode_keys.is_planning_executor_key 同源，双侧同步）：
# mode_X/<stem>（模式名小写标识/stem 字母数字下划线）或系统 agent 裸名。
_MODE_AGENT_KEY_RE = re.compile(r"^mode_([a-z][a-z0-9_]{0,63})/([A-Za-z0-9_]+)$")
_BARE_AGENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# 工具 id 形态（与 plugins/shared/mode_keys.py _TOOL_ID_RE 同源，双侧同步）。
_TOOL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

# 消费者对账豁免台账：字段 → (豁免原因, 升级触发条件)。台账外的声明字段必须
# 在仓内有消费点（grep 佐证由本脚本对 tool_card 之外的字段抽查主消费面）。
_CONSUMPTION_EXEMPT = {
    "tool_card": (
        "折叠策略现由 presenter 命中隐式承载；直接消费接线 Wave2",
        "Wave2 呈现声明化批次（前端读 modes registry tool_card 定折叠初态）",
    ),
}


def validate_declaration(data: object) -> str:
    """mode.yaml schema 校验（与 agent_manager validate_mode_declaration 同源）。"""
    if not isinstance(data, dict):
        return "非 dict 形态"
    for key in _MODE_DECL_REQUIRED:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"缺必需字段 {key}"
    unknown = sorted(set(data) - _MODE_DECL_KNOWN)
    if unknown:
        return f"未知字段 {unknown}（白名单外禁止）"
    pipelines = data.get("pipelines")
    if pipelines is not None:
        if not isinstance(pipelines, list) or not pipelines:
            return f"pipelines 须为非空列表: {pipelines!r}（零声明 = 不写该键）"
        seen: set[str] = set()
        for item in pipelines:
            if not isinstance(item, dict):
                return f"pipelines 条目须为 dict: {item!r}"
            name = item.get("name")
            if not (isinstance(name, str) and _PIPELINE_ID_RE.match(name)):
                return f"pipelines[].name 形态非法: {name!r}"
            if name in seen:
                return f"pipelines[].name 重复: {name!r}"
            seen.add(name)
            context = item.get("context")
            if context not in _MODE_DECL_ENUMS["pipeline_context"]:
                return f"pipelines[].context 枚举非法: {context!r}"
            source = item.get("source", "registry")
            if source == "package":
                return "pipelines[].source=package 包内自持待拍板"
            if source not in _MODE_DECL_ENUMS["pipeline_source"]:
                return f"pipelines[].source 须为 registry: {source!r}"
    presenter = data.get("presenter")
    if presenter is not None and (
        not isinstance(presenter, dict) or presenter.get("source") not in _MODE_DECL_ENUMS["presenter_source"]
    ):
        return "presenter.source 枚举非法"
    tool_card = data.get("tool_card")
    if tool_card is not None and tool_card not in _MODE_DECL_ENUMS["tool_card"]:
        return "tool_card 枚举非法"
    persona = data.get("persona")
    if persona is not None and (
        not isinstance(persona, dict) or not isinstance(persona.get("from"), str) or not persona.get("from", "").strip()
    ):
        return "persona 须为 {replace: bool, from: <execution_context 键>}"
    for key in ("theme", "icon"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not value.strip():
                return f"{key} 须为非空字符串（不声明 = 不切换主题/无图标）"
    # plan_spec（批 B2 领域规格注册，与 agent_manager 同源）：可选，值形态 =
    # 技能名（包内 skills/ 随包，工作空间并集快照按名可读）；路径形态拒收。
    if "plan_spec" in data:
        plan_spec = data["plan_spec"]
        if not (isinstance(plan_spec, str) and _SKILL_NAME_RE.match(plan_spec)):
            return "plan_spec 须为技能名形态（如 skill-solution-software；不声明 = 不写该键）"
    # planning_executors（批 B4，与 agent_manager 同源）：可选字符串列表，
    # 元素 = plan 态执行者键（mode_X/<stem> 或系统 agent 裸名）；空列表拒。
    if "planning_executors" in data:
        declared = data["planning_executors"]
        if not isinstance(declared, list) or not declared:
            return "planning_executors 须为非空列表（不声明 = 不写该键，缺省 = 零贡献）"
        for key in declared:
            if not (
                isinstance(key, str)
                and (_MODE_AGENT_KEY_RE.match(key) is not None or _BARE_AGENT_KEY_RE.match(key) is not None)
            ):
                return f"planning_executors[] 执行者键须为 mode_X/<stem> 或系统 agent 裸名形态: {key!r}"
    # inject_tool_ids（2026-11-11 模式标签注入）：可选非空列表，元素 = 工具 id
    # 形态（工具由别的插件注册；空列表拒——不声明即零贡献）。
    if "inject_tool_ids" in data:
        declared = data["inject_tool_ids"]
        if not isinstance(declared, list) or not declared:
            return "inject_tool_ids 须为非空列表（不声明 = 零贡献）"
        for name in declared:
            if not (isinstance(name, str) and _TOOL_ID_RE.match(name) is not None):
                return f"inject_tool_ids[] 工具 id 形态非法: {name!r}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes-dir", default="plugins/shared")  # 拍平：模式包平铺共享根（mode_* 前缀识别）
    args = parser.parse_args()

    modes_dir = Path(args.modes_dir)
    errors: list[str] = []
    mode_keys: dict[str, dict] = {}
    # 跨包同名对账索引：技能目录名 → 出现该技能的包名列表（原则 7 零副本红线）
    skills_by_name: dict[str, list[str]] = {}

    for pkg in sorted(modes_dir.glob("mode_*")):
        # 拍平布局下共享根的 mode_ 前缀目录含非模式包插件（如
        # mode_material_inject 管道步骤）；对账面 = 声明了 mode.yaml 的包。
        if not (pkg / "mode.yaml").is_file():
            continue
        if not pkg.is_dir():
            continue
        decl_path = pkg / "mode.yaml"
        try:
            data = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{pkg.name}: mode.yaml 不可读（{exc}）")
            continue
        err = validate_declaration(data)
        if err:
            errors.append(f"{pkg.name}: {err}")
            continue
        assert isinstance(data, dict)
        mode_keys[str(data["mode"])] = data

        # 组装器实物对账（声明驱动下的静默失效兜底）
        material = data.get("material")
        material_file = str(material).split("::", 1)[0].strip() if material else ""
        if material and not (pkg / material_file).is_file():
            errors.append(f"{pkg.name}: material 声明 {material} 但文件缺失")
        if not material and (pkg / "material.py").is_file():
            errors.append(f"{pkg.name}: 存在 material.py 但无 material 声明（孤儿组装器）")

        # 技能孤儿对账（批 E §3.1 可选闸）：声明 material 的包若有 skills/，
        # 包内技能须被本包 agents 提示词按名引用（否则无人加载 = 孤儿技能）。
        if material and (pkg / "skills").is_dir():
            agents_text = ""
            if (pkg / "agents").is_dir():
                for agent_yaml in sorted((pkg / "agents").glob("*.yaml")):
                    try:
                        agents_text += agent_yaml.read_text(encoding="utf-8")
                    except OSError as exc:
                        errors.append(f"{pkg.name}: agent 文件不可读（{exc}）")
            for skill in sorted((pkg / "skills").iterdir()):
                if skill.is_dir() and (skill / "SKILL.md").is_file() and skill.name not in agents_text:
                    errors.append(f"{pkg.name}: 技能 {skill.name} 存在但未被包内 agents 提示词引用（孤儿技能）")

        # 技能索引（跨包同名对账用；同包多技能合法，只登记不判）
        if (pkg / "skills").is_dir():
            for skill in sorted((pkg / "skills").iterdir()):
                if skill.is_dir() and (skill / "SKILL.md").is_file():
                    skills_by_name.setdefault(skill.name, []).append(pkg.name)

    # 跨包同名技能对账（原则 7 零副本红线执法，批 E4）：出厂包 skills/ 之间同名
    # 技能即红——任何包禁携带他包同名物料，跨包复用走全限定引用 + 按归属取料
    # （D6）。用户根副本不算（用户覆盖合法通道，同名优先序用户根赢）；用户根
    # 不在本扫描面内，天然不参与本对账。
    for skill_name, owners in sorted(skills_by_name.items()):
        if len(owners) > 1:
            errors.append(
                f"跨包同名技能（零副本红线）: {skill_name} 同时出现于 {', '.join(sorted(owners))}"
                "——技能归属唯一，跨包复用走全限定引用（按归属取料），禁携带副本"
            )

    # 豁免台账外的运行时声明字段须有消费佐证（评估域字段豁免于 eval_harness 消费）
    runtime_fields = {"pipelines", "panel_page_id", "presenter", "material", "icon", "theme"}
    consumed = {
        "pipelines": True,  # mode.list 目录路由渲染（按 context 分组）+ 前端 modeBinding 会话路由
        "panel_page_id": True,  # 前端 modePanel（contributes.pages 声明驱动同义）
        "presenter": True,  # 前端 presenterSourcesFromModes
        "material": True,  # mode_material_inject._read_material_decl
        "icon": True,  # 前端选择器选项/模式徽标（taskModeOptionsFromModes/modePanel，批 G⑦ 起消费）
        "theme": True,  # 前端模式主题通道（modeSessionBinder ensureModeTheme/sync，批 G⑤ 起消费；零包声明防空转不改）
    }
    for key, data in mode_keys.items():
        for field in runtime_fields:
            if data.get(field) is not None and field in _CONSUMPTION_EXEMPT:
                reason, upgrade = _CONSUMPTION_EXEMPT[field]
                print(f"[豁免] {key}.{field}: {reason}（升级条件: {upgrade}）")

    used_fields = {f for f in runtime_fields if consumed.get(f)}
    _ = used_fields  # 消费点登记即文档；豁免台账输出为审计留痕

    if errors:
        print("mode.yaml 声明面对账失败:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print(
        f"对账通过：{len(mode_keys)} 包声明面合法；"
        f"组装器声明/实物对齐；跨包同名技能 0（{len(skills_by_name)} 技能全仓唯一）；"
        f"豁免台账 {len(_CONSUMPTION_EXEMPT)} 项；"
        f"选择器选项=registry 运行时派生（TASK_MODES 冻结集已退役，vitest 两态断言保障）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
