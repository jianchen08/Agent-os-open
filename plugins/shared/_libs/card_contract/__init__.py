"""卡声明契约三件套单源（T6 规则④：双域共享、无单一对口的纯逻辑 → _libs 正经包）。

内容三件：
- ``SUPPORTED_VARIABLE_TYPES``  变量声明类型支持集（Validator 类型执法集单源）；
- ``flatten_variables``         任意嵌套 variables 展平为 {声明路径: 叶声明}
  （叶 = 含 ``type`` 键的 dict，路径 = 根到叶点连接）；
- ``keep_state_machine_spec``   cheat_overrides.keep_state_machine 声明 →
  执法规格归一（fail-closed：声明在而形态不可解析 = 全保留执法）。

双消费域（同一套判定，禁第二套）：
- lint 面（结构校验）：``mode_roleplay/card_lint.py``（装载/导入/创作 lint 的
  lint_variables / lint_engine 经此取类型集、叶遍历与 keep 规格）；
- 执法面（值结算）：``state_marker_parse/state_contract.py`` State Contract
  Validator（类型执法、schema 叶匹配、cheat 旁路规格——op/clamp/hard_rules
  归 Validator，本包不做值结算）。

全部纯函数、零 IO、同步直调：消费方是逐轮热路径（Validator）与装载/导入面，
不经 capability 服务（模块级导入 + 同步语义为契约前提）。
"""

from __future__ import annotations

from typing import Any

# 变量声明类型支持集（Validator 类型执法同源，契约见 state_contract 模块 docstring）
SUPPORTED_VARIABLE_TYPES = ("int", "float", "string", "bool", "enum")


def flatten_variables(
    variables: dict[str, Any], prefix: str = ""
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """任意嵌套 variables 展平为 {声明路径: 叶声明}；路径 = 点连接。

    叶 = 含 ``type`` 键的 dict；分支值非 dict / 叶缺 type 记入 skipped
    （fail-soft 跳过，消费面据此拒绝对应键——作者面错误由 lint 面收编）。
    """
    flat: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for name, child in variables.items():
        path = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(child, dict):
            if "type" in child:
                flat[path] = child
            else:
                sub_flat, sub_skipped = flatten_variables(child, path)
                flat.update(sub_flat)
                skipped.extend(sub_skipped)
        else:
            skipped.append(path)
    return flat, skipped


def keep_state_machine_spec(engine: Any) -> tuple[bool | list[str], list[str]]:
    """keep_state_machine 声明 → 执法规格（装载/拦截面展示与 Validator 旁路同源）。

    返回 (规格, warnings)：``True`` = 全键域保留执法；``list[str]`` = 命中键域
    保留（模式语言同 hard_rules：点分段 ``*`` 单段，归 Validator 匹配）；
    ``False`` = 无保留（作弊全旁路，缺省语义）。fail-closed 方向：声明在而
    形态不可解析（非布尔非列表 / 列表全非字符串项）= ``True``——作者的保留
    意图宁可执法过头，不得静默丢弃。
    """
    if not isinstance(engine, dict):
        return False, []
    overrides = engine.get("cheat_overrides")
    if overrides is None:
        return False, []
    if not isinstance(overrides, dict):
        return True, ["engine.cheat_overrides: 须为映射（fail-closed 全保留执法）"]
    keep = overrides.get("keep_state_machine")
    if keep is None or keep is False:
        return False, []
    if keep is True:
        return True, []
    if isinstance(keep, list):
        patterns = [item for item in keep if isinstance(item, str)]
        invalid = len(keep) - len(patterns)
        warnings = (
            [f"engine.cheat_overrides.keep_state_machine: {invalid} 个非字符串模式已剔除"]
            if invalid
            else []
        )
        if patterns:
            return patterns, warnings
        if invalid:
            # 声明在而合法条目全灭 = 保留意图不可执行，fail-closed 全保留
            return True, warnings
        return False, []  # 空表 = 作者显式无保留
    return True, [
        "engine.cheat_overrides.keep_state_machine: 形态非法（布尔或字符串列表），"
        "fail-closed 全保留执法"
    ]
