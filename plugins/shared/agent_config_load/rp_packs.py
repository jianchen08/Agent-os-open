"""RP 玩法包（pack）双根装载与声明合并（RP 批 2-Ⅱ 2c+，agent_config_load 步内模块）。

资产契约（FR-B6 Tier 0）：卡 yaml 顶层 ``pack_ids: [<id>...]``（与 lorebook_ids
同位同风格）引用玩法包；包内容 = ``{variables: {...}, behaviors: {...}}``（Tier 0
可表达面）；``graph``/``template``/``ui`` 键本步**不入装载 state（静默忽略）**——
三键皆模式插件 server 侧消费面（批 4-Ⅲ 起启用消费，延迟键 warning 随之解除：
出厂包 graph 声明自 2026-10-17 标杆补齐起合法）。
（_DEFERRED_PACK_KEYS 口径注释：指管道注入路径不承载，呈现面消费在模式插件
server 侧——/game/config 聚合与面板数据面，两处口径不得混改）。

双根查找（同 lorebooks 用户层物料双根模式，用户层同 id 接管 = BuiltinFirst）：
- 用户层：``<user_config_dir>/packs/<id>.yaml``（用户自建/导入玩法包）；
- 包内：``<mode_pkg>/packs/<id>.yaml``（mode_pkg 经 mode_keys.find_mode_package_dir
  按卡 agent.id 模式键解析——通用机制零模式特判；非模式键 = 仅用户层）。
  出厂播种两件玩法包（C3 整合刀：xianxia_combat / stardew_social，见包内
  packs/README）。
- 两根皆缺 = 该 pack 零并入 + warning；用户层损坏 = warning 回落包内；包内损坏
  上抛（出厂种子损坏属包事实，不静默当缺席）；id 形态非法 = 按缺失（路径安全
  闸，白名单同 lorebook 书 id 口径）。

合并语义（tests 锁定）：
- 多 pack 按**声明序**逐键合并，后者覆盖前者；**卡覆盖 pack**（卡内同键最后
  生效）。合并粒度 = variables/behaviors 顶层键 shallow 合并，键值整体替换、
  不深合并（嵌套覆盖边界归作者显式声明，避免半懂合并篡改声明树）。
- 装载产物并入 ``context.rp_state_schema`` / ``context.rp_state_behaviors``，
  与卡内声明走同一合并管线（消费面 state_marker_parse 无感知来源）。

呈现面声明（RP 面板 v3.1 刀1）：pack 顶层 ``ui``（大厅卡 icon/style_tags/
lineage/theme_css/page）与 ``init_ui``（开演设置页 routes/trait_pools 等）由
呈现消费侧（mode_roleplay server 归一剔除面）消费，本步零并入 state、只做
card.lint_pack_ui 服务同源结构 warning 面（id 唯一/label 必填/
lineage 枚举等形态面；effects 键域校验放消费侧 Validator）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

# pack id 白名单（路径安全闸）：^[A-Za-z0-9_]{1,64}$，与 lorebook 书 id 口径一致
# ——分隔符/`..` 穿越形态进不了文件名。
_PACK_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Tier 0 可表达面 + 本步不入 state 键（口径注释见模块 docstring：呈现面消费在
# 模式插件 server 侧——/game/config 聚合与面板数据面；ui/init_ui 另有 card 质检服务
# 同源结构 warning 面，见 merge_state_decl；pipeline 同为 server 侧消费键
# ——玩法级管道绑定，清点/聚合/bind 三面同闸，2026-10-17 批次一起）
_DEFERRED_PACK_KEYS = ("graph", "template", "ui", "pipeline")


def _load_pack_yaml(path: Path) -> dict[str, Any]:
    """单 pack yaml 读取；顶层非映射 = ValueError（损坏语义，调用方分根处置）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"pack 顶层须为映射: {path}")
    return data


def load_packs(
    pack_ids: Sequence[str], pkg_dir: str | None, user_packs_dir: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """声明序逐 pack 双根加载；返回（pack 内容列表[声明序], warnings）。

    用户层 ``<user_packs_dir>/<id>.yaml`` 优先（同 id 接管，损坏 warning 回落
    包内）→ 包内 ``<pkg_dir>/packs/<id>.yaml``（损坏上抛——种子事实）；两根皆
    缺 / id 形态非法 = 零并入 + warning（fail-soft 不阻断装载）。
    """
    packs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for pack_id in pack_ids:
        if not _PACK_ID_RE.fullmatch(pack_id):
            warnings.append(f"pack id 形态非法，按缺失跳过: {pack_id!r}")
            continue
        if user_packs_dir:
            user_path = Path(user_packs_dir) / f"{pack_id}.yaml"
            if user_path.is_file():
                try:
                    packs.append(_load_pack_yaml(user_path))
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    warnings.append(f"用户层 pack 损坏，回落包内: {pack_id}（{exc}）")
                else:
                    continue
        builtin_path = Path(pkg_dir) / "packs" / f"{pack_id}.yaml" if pkg_dir else None
        if builtin_path is None or not builtin_path.is_file():
            warnings.append(f"pack 两根皆缺，零并入: {pack_id}")
            continue
        packs.append(_load_pack_yaml(builtin_path))
    return packs, warnings


def merge_state_decl(
    card_variables: dict[str, Any] | None,
    card_behaviors: dict[str, Any] | None,
    packs: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Tier 0 声明合并：多 pack 声明序后者逐键覆盖 → 卡逐键覆盖 pack。

    返回（variables, behaviors, warnings）；空表归 None（调用方据此不写键——
    兼容态载荷零新增）。graph/template/ui 键静默忽略不入装载 state（消费面在
    模式插件 server 侧，见模块 docstring）；pack 顶层非映射 /
    variables/behaviors 非映射 warning 跳过（fail-soft）。
    """
    warnings: list[str] = []
    variables: dict[str, Any] = {}
    behaviors: dict[str, Any] = {}
    for index, pack in enumerate(packs):
        if not isinstance(pack, dict):
            warnings.append(f"pack 内容须为映射，已跳过: #{index}")
            continue
        # graph/template/ui（_DEFERRED_PACK_KEYS）静默忽略：三键消费面在模式插件
        # server 侧（game/config 聚合，批 4-Ⅲ 起），Tier 0 装载不感知不警告。
        for key, sink in (("variables", variables), ("behaviors", behaviors)):
            value = pack.get(key)
            if isinstance(value, dict):
                sink.update(value)
            elif value is not None:
                warnings.append(f"pack {key} 须为映射，已跳过: #{index}")
    if isinstance(card_variables, dict):
        variables.update(card_variables)
    if isinstance(card_behaviors, dict):
        behaviors.update(card_behaviors)
    return (variables or None), (behaviors or None), warnings
