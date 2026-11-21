"""pack 与场景内容（packs / precipitation / scene / pack assets）（自 server.py 按域拆分）。"""
from __future__ import annotations

import base64  # noqa: F401
import importlib.util
import json
import logging  # noqa: F401
import os
import re
import sys
import time  # noqa: F401
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

import yaml

# 段内自由名依赖的原 import（自动复制）
# 段内自由名依赖的原 import（自动复制）
from card_lint import (  # noqa: E402 —— 声明面结构/内容安检单源（本插件内部模块）
    OPERATION_ID_RE,
    TICK_WHEN_OPS,  # noqa: F401
    lint_actions,  # noqa: F401
    lint_behaviors,  # noqa: F401
    lint_card_extensions,  # noqa: F401
    lint_engine,  # noqa: F401
    lint_init_ui,
    lint_operations,
    lint_tick,  # noqa: F401
    lint_ui,
    lint_variables,  # noqa: F401
    scan_html_danger,  # noqa: F401
    scan_html_danger_texts,  # noqa: F401
    scan_suspicious,  # noqa: F401
    walk_str_values,  # noqa: F401
)
from rp_cards import (
    _PRECIP_BOOKKEEPING_ACTIONS,
    _PRECIP_SOURCE,
    _PRECIP_STAT_WINDOW,
    _llm_default_chat_model,
    _load_cards,
    _user_config_root,
)
from rp_common import (
    _CORE_INCLUDE_MARK,
    _CORE_SOURCE_RE,
    _GAME_HOST_ID_RE,
    _PACK_ID_RE,
    _PACKS_DIR,
    _PAGE_ASSEMBLIES,
    _PANEL_HTML_PATH,
    _call_provider,
    _game_routed,
    _pack_tick_listing,
    logger,
    plugin,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: F401
from agentos_plugin_sdk.bootstrap import bootstrap_plugin  # noqa: F401
from agentos_plugin_sdk.capability import bind_capability_caller


def _user_packs_dir() -> str | None:
    """用户层玩法包目录：<user_config_dir>/packs（lorebooks 用户层同模式双根）。"""
    root = _user_config_root()
    return os.path.join(root, "packs") if root else None



def _user_pipelines_dir() -> str | None:
    """登记管道配置目录：<user_config_dir>/pipelines（内核按名加载用户层优先
    同源同链——resolve_config_path ADR 2026-09-13-unified-user-root；装机形态
    electron 注入值恒等）。目录不可得 = None（读面降级：声明一律回落缺省）。"""
    root = _user_config_root()
    return os.path.join(root, "pipelines") if root else None



def _registered_pipeline_names(pipelines_dir: str | None = None) -> set[str]:
    """登记管道名集合：目录下 *.yaml 的 stem（内核 load_pipeline_config_by_name
    按名加载同锚）。显式传参供测试注入隔离；缺省现场解析用户空间。"""
    directory = pipelines_dir if pipelines_dir is not None else _user_pipelines_dir()
    if not directory or not os.path.isdir(directory):
        return set()
    return {path.stem for path in Path(directory).glob("*.yaml")}



def _pack_pipeline_listing(
    data: dict[str, Any], pack_id: str, pipelines_dir: str | None = None
) -> str | None:
    """pack 顶层 pipeline 声明 → 消费面透出值（键缺席式：声明且合法才非 None）。

    形态（card_lint 单源 lint_pack_pipeline）+ 登记存在性（_registered_pipeline_names）
    双闸；任一不过 = 剔除声明回落契约缺省 roleplay + warning（与 ui.lineage
    非法剔除回落同构，面板/开演面永远只见合法绑定）。None = 键缺席（未声明
    或已剔除），消费方按缺省折叠。
    """
    from card_lint import lint_pack_pipeline

    raw = data.get("pipeline")
    if raw is None:
        return None
    warnings = lint_pack_pipeline(raw)
    if warnings:
        for warning in warnings:
            logger.warning("[mode_roleplay] pack %s %s", pack_id, warning)
        return None
    if str(raw) not in _registered_pipeline_names(pipelines_dir):
        logger.warning(
            "[mode_roleplay] pack %s pipeline 未登记（config/pipelines 无此名），"
            "剔除声明回落缺省 %s: %r",
            pack_id,
            "roleplay",
            raw,
        )
        return None
    return str(raw)


# ── pack 呈现面声明归一（RP 面板 v3.1 刀1：大厅卡 ui/has_page + 开演设置页
# init_ui）────────────────────────────────────────────────────────────────────
# 结构判定单源在本插件 card_lint（lint_ui / lint_init_ui / PACK_LINEAGES；装载侧
# agent_config_load 同源 warning 面），本侧做归一剔除面：lineage 非法剔除回落
# 契约缺省档 full、theme_css/page 文件缺失剔除声明（warning 留痕）、routes/
# trait_pools 坏条目剔除——面板永远只见合法形态。



def _pack_rel_path_in_dir(pack_dir: str | None, rel: str) -> str | None:
    """包内相对路径 → 实存文件绝对路径（越界/包目录不可得/非文件 = None）。"""
    if not pack_dir or not rel:
        return None
    base = os.path.realpath(pack_dir)
    resolved = os.path.realpath(os.path.join(base, rel))
    if resolved != base and not resolved.startswith(base + os.sep):
        return None
    return resolved if os.path.isfile(resolved) else None



def _pack_ui_listing(
    data: dict[str, Any], pack_dir: str | None, pack_id: str
) -> tuple[dict[str, Any] | None, bool]:
    """pack ui 声明 → (大厅条目 ui 载荷, has_page)。

    归一剔除：icon/style_tags/lineage 形态合法才透出（lineage 非法剔除，面板
    按契约缺省档 full 折叠）；theme_css/page 须包内文件实存（缺失 = 声明无效
    剔除 + warning）；has_page 据有效 page 声明判定（大厅「点玩法直达游戏页」
    路由依据）。无声明 = (None, False)。
    """
    from card_lint import PACK_LINEAGES

    raw = data.get("ui")
    if not isinstance(raw, dict) or not raw:
        return None, False
    for warning in lint_ui(raw):
        logger.warning("[mode_roleplay] pack %s %s", pack_id, warning)
    ui: dict[str, Any] = {}
    icon = raw.get("icon")
    if isinstance(icon, str) and icon.strip():
        ui["icon"] = icon
    tags = raw.get("style_tags")
    if isinstance(tags, list):
        cleaned = [str(tag) for tag in tags if isinstance(tag, str) and tag.strip()]
        if cleaned:
            ui["style_tags"] = cleaned
    lineage = raw.get("lineage")
    if lineage in PACK_LINEAGES:
        ui["lineage"] = lineage
    elif lineage is not None:
        logger.warning(
            "[mode_roleplay] pack %s ui.lineage 非法，剔除声明（缺省档 full 折叠）: %r",
            pack_id,
            lineage,
        )
    has_page = False
    for key in ("theme_css", "page"):
        rel = raw.get(key)
        if not isinstance(rel, str) or not rel.strip():
            continue
        resolved = _pack_rel_path_in_dir(pack_dir, rel.strip())
        if resolved is None:
            logger.warning(
                "[mode_roleplay] pack %s ui.%s 文件缺失，声明无效剔除: %s",
                pack_id,
                key,
                rel,
            )
            continue
        ui[key] = rel.strip()
        if key == "page":
            has_page = True
        elif key == "theme_css":
            # 附加缝（面板 init 页主题消费，超出任务书命名契约的只增键——
            # 台账登记待刀2回判）：清点载荷内联包内主题文本，面板开演设置页
            # 「主题=包内 CSS」的直接数据源（game/page 之外的第二送达面）。
            try:
                with open(resolved, encoding="utf-8") as fh:
                    ui["theme_css_text"] = fh.read()
            except OSError as exc:
                logger.warning(
                    "[mode_roleplay] pack %s ui.theme_css 读取失败，内容缺省: %s (%s)",
                    pack_id,
                    rel,
                    exc,
                )
    return (ui or None), has_page



def _pack_init_ui_listing(data: dict[str, Any], pack_id: str) -> dict[str, Any] | None:
    """pack init_ui 声明 → 开演设置页载荷（坏条目剔除，全灭 = None 视同无声明）。

    归一口径：title/intro 字符串透传、show_variables 布尔透传；routes/
    trait_pools 只留 id+label 齐备且池内 id 未重复的条目（profile/effects 选填，
    effects 只透出不判键域——键域执法在消费侧 Validator fail-closed）。整键
    缺席 = None（该包无自定义开演页，面板回落通用组合配置）。
    """

    raw = data.get("init_ui")
    if not isinstance(raw, dict) or not raw:
        return None
    for warning in lint_init_ui(raw):
        logger.warning("[mode_roleplay] pack %s %s", pack_id, warning)

    def _clean_option(entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        option_id = str(entry.get("id") or "").strip()
        label = entry.get("label")
        if not option_id or not isinstance(label, str) or not label.strip():
            return None
        option: dict[str, Any] = {"id": option_id, "label": label}
        profile = entry.get("profile")
        if isinstance(profile, str) and profile.strip():
            option["profile"] = profile
        if isinstance(entry.get("effects"), dict) and entry["effects"]:
            option["effects"] = dict(entry["effects"])
        return option

    init: dict[str, Any] = {}
    for key in ("title", "intro"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            init[key] = value
    if isinstance(raw.get("show_variables"), bool):
        init["show_variables"] = raw["show_variables"]
    routes_raw = raw.get("routes")
    if isinstance(routes_raw, list):
        routes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in routes_raw:
            cleaned = _clean_option(entry)
            if cleaned is None or cleaned["id"] in seen:
                continue
            seen.add(cleaned["id"])
            routes.append(cleaned)
        if routes:
            init["routes"] = routes
    pools_raw = raw.get("trait_pools")
    if isinstance(pools_raw, list):
        pools: list[dict[str, Any]] = []
        seen_pools: set[str] = set()
        for pool in pools_raw:
            if not isinstance(pool, dict):
                continue
            pool_id = str(pool.get("id") or "").strip()
            pool_label = pool.get("label")
            if (
                not pool_id
                or pool_id in seen_pools
                or not isinstance(pool_label, str)
                or not pool_label.strip()
            ):
                continue
            options_raw = pool.get("options")
            options: list[dict[str, Any]] = []
            seen_options: set[str] = set()
            for entry in options_raw if isinstance(options_raw, list) else []:
                cleaned = _clean_option(entry)
                if cleaned is None or cleaned["id"] in seen_options:
                    continue
                seen_options.add(cleaned["id"])
                options.append(cleaned)
            if not options:
                continue
            seen_pools.add(pool_id)
            pools.append({"id": pool_id, "label": pool_label, "options": options})
        if pools:
            init["trait_pools"] = pools
    return init or None



def _pack_from_data(
    data: dict[str, Any],
    pack_id: str,
    origin: str,
    pack_dir: str | None = None,
    pipelines_dir: str | None = None,
) -> dict[str, Any]:
    """pack 源数据 → 向导玩法勾选页展示条目（清点面，非装载面）。

    pack 资产契约（rp_packs）：顶层 {variables, behaviors}（Tier 0 面）——
    name/description 非契约字段，宽容读取供作者标注（缺席回落 id/空串）；
    计数 = 各面顶层键数（勾选页摘要）。装载语义（双根查找/合并）在
    agent_config_load 步，本函数只供展示。
    呈现面声明（RP 面板 v3.1 刀1）：ui/init_ui 归一载荷（键缺席式——无声明
    包条目零新增键）+ has_page 布尔恒在（ui.page 有效声明 = true，大厅据此
    「点玩法直达游戏页」路由；无声明/文件缺失 = false）。
    玩法级管道绑定（批次一）：pipeline 键缺席式（声明且登记合法才透出，
    _pack_pipeline_listing 同闸面；开演载荷据此透传包绑定管道）。
    """
    variables = data.get("variables")
    behaviors = data.get("behaviors")
    ui, has_page = _pack_ui_listing(data, pack_dir, pack_id)
    item: dict[str, Any] = {
        "id": pack_id,
        "origin": origin,
        "name": str(data.get("name") or pack_id),
        "description": str(data.get("description") or ""),
        "variables_count": len(variables) if isinstance(variables, dict) else 0,
        "behaviors_count": len(behaviors) if isinstance(behaviors, dict) else 0,
        "has_page": has_page,
    }
    if ui is not None:
        item["ui"] = ui
    pipeline = _pack_pipeline_listing(data, pack_id, pipelines_dir)
    if pipeline is not None:
        item["pipeline"] = pipeline
    init_ui = _pack_init_ui_listing(data, pack_id)
    if init_ui is not None:
        item["init_ui"] = init_ui
    return item



def _scan_packs_dir(directory: str | None, origin: str) -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (pack_id, 源数据) 有序列表；损坏/非 dict 文件跳过不崩面板。

    id 白名单两侧同闸（_PACK_ID_RE，与装载侧 rp_packs 同口径——非白名单
    stem 的 pack 装载面也进不去，清点面不展示死条目）。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for path in sorted(Path(directory).glob("*.yaml")):
        pack_id = path.stem
        if not _PACK_ID_RE.fullmatch(pack_id):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            found.append((pack_id, data))
    return found



def _load_packs_listing(
    packs_dir: str = _PACKS_DIR,
    user_dir: str | None = None,
    pipelines_dir: str | None = None,
) -> list[dict[str, Any]]:
    """双根合并列玩法包：出厂（包内 packs/*.yaml，origin="builtin"）∪ 用户层
    <user_config_dir>/packs（origin="user"；同 id 接管，出厂件永不被写）。

    向导玩法勾选页 + 面板大厅卡片流数据源（data/packs 独立端点与 bootstrap
    聚合键同源本函数——载荷形状一处改两处生效）；出厂播种两件玩法包（C3
    整合刀：修仙战斗/星露谷好感）。user_dir=None（缺省）经 user_space 现场
    解析，用户空间不可得 → 只出厂根（读面降级）；显式传路径供测试注入隔离。
    pipelines_dir 同款显式传参（玩法级管道绑定登记校验锚，None=现场解析）。
    """
    resolved_user_dir = _user_packs_dir() if user_dir is None else user_dir
    merged: dict[str, dict[str, Any]] = {}
    for origin, directory in (("builtin", packs_dir), ("user", resolved_user_dir)):
        for pack_id, data in _scan_packs_dir(directory, origin):
            # 同 id 后根接管（pack_dir 随根：呈现面声明相对路径按接管根解析）
            merged[pack_id] = _pack_from_data(
                data, pack_id, origin, pack_dir=directory, pipelines_dir=pipelines_dir
            )
    return list(merged.values())


# ── 自定义玩法 combos（RP 面板 v3.1 刀1：大厅自定义卡）───────────────────────────
#
# 存储 = <user_config_dir>/rp_combos/<id>.json（用户件，同 id 覆盖）：
# {id, name, card_id, lorebook_ids: [], route_id, trait_ids: [], pack_ids（可选
# 直挂）, profile_extra（可选：自由文本初始状态段，面板组合③承载——键缺席式
# 只增不破）, created_at, origin: "user"}。读面 fail-soft：目录缺失 = 空表、损坏件
# 跳过 + warning（卡/世界书读取同款降级）；id 命名空间与卡同款白名单闸（stem
# 即 id，路径安全）。组合是「卡 + 该玩法开演选择」的命名快照：面板大厅自定义
# 卡据此一键重开（lorebook_ids/route_id/trait_ids 语义 = 开演向导对应选择的
# 透传载荷，消费归面板开演流）。


_COMBOS_ROUTE_SAVE = "/ext/mode_roleplay/data/combos/save"

_COMBOS_ROUTE_DELETE = "/ext/mode_roleplay/data/combos/delete"



def _precip_is_number(value: Any) -> TypeGuard[float]:
    """数值判定（bool 是 int 子类，显式排除——与 Validator 同口径）；TypeGuard
    供相对变更幅度算术收窄（state_contract 同款）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)



def _precip_effect_value(op: str, value: Any) -> str:
    """候选效果值 → behaviors op 串契约形态：相对变更带符号（负数自带 -），
    绝对赋值 = 前缀。"""
    if op == "=":
        return f"={value}"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return f"+{value}"
    return str(value)



def _precip_event_turn(row: dict[str, Any]) -> Any:
    """事件行 → 样本轮次：idem 尾段（``{pipeline_id}:{seq}`` 基式）数字还原
    int；非数字尾段回退整 idem 串；idem 缺席 = None（不入样本轮次）。"""
    idem = row.get("idem")
    if not isinstance(idem, str) or ":" not in idem:
        return None
    tail = idem.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else idem



def precipitation_candidates(events: list[Any]) -> list[dict[str, Any]]:
    """审计事件流水 → 候选规则列表（纯函数，面板复盘统计面）。

    可枚举判据（§7.4）：行来源 = 命名动作通道（source=action、action 非 op
    动词）、非被拒（layer 缺席）、变更 op 可枚举（+/-/=）、相对变更幅度可计算
    （from/to 数值齐）——按 (actor, action, target, op, 幅度) 聚组，出现 ≥2 次
    成候选。字段：actor/action/target/op/value/count/sample_turns/rule；rule =
    标准 behaviors 形态 {id, trigger, effect}（id 前缀 precipitated: 随固化面
    落来源标记）。输出按 (action, target, actor) 稳定排序（面板呈现确定性）；
    非法行跳过，无成例 = 空列表。
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in events:
        if not isinstance(row, dict):
            continue
        if row.get("source") != "action" or row.get("layer") is not None:
            continue
        action = row.get("action")
        if not isinstance(action, str) or not action or action in _PRECIP_BOOKKEEPING_ACTIONS:
            continue
        op = row.get("op")
        target = row.get("target")
        if op not in ("+", "-", "=") or not isinstance(target, str) or not target:
            continue
        to = row.get("to")
        if op == "=":
            if to is None or isinstance(to, (dict, list)):
                continue
            value: Any = to
        else:
            frm = row.get("from")
            if not _precip_is_number(frm) or not _precip_is_number(to):
                continue
            value = to - frm
        key = (action, target, op, value, str(row.get("actor") or ""))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "action": action,
                "target": target,
                "op": op,
                "value": value,
                "actor": key[4],
                "count": 0,
                "turns": set[Any](),
            }
        group["count"] += 1
        turn = _precip_event_turn(row)
        if turn is not None:
            group["turns"].add(turn)
    candidates: list[dict[str, Any]] = []
    for group in groups.values():
        if group["count"] < 2:
            continue
        candidates.append({
            "actor": group["actor"],
            "action": group["action"],
            "target": group["target"],
            "op": group["op"],
            "value": group["value"],
            "count": group["count"],
            "sample_turns": sorted(
                group["turns"], key=lambda t: (0, t, "") if isinstance(t, int) else (1, 0, str(t))
            ),
            "rule": {
                "id": f"precipitated:{group['action']}:{group['target']}",
                "trigger": group["action"],
                "effect": {group["target"]: _precip_effect_value(group["op"], group["value"])},
            },
        })
    candidates.sort(key=lambda c: (c["action"], c["target"], c["actor"]))
    return candidates



def _precipitation_stream(workspace: str | None) -> list[dict[str, Any]]:
    """会话工作空间审计流水全量读（rp_state_audit 读面单源复用；ws 不可得 =
    空表，与读面 fail-soft 同口径）。"""
    if not workspace:
        return []
    from _root_api import recent_events  # noqa: PLC0415 （共享根裸名，惰性同 import 面）

    return recent_events(workspace, limit=_PRECIP_STAT_WINDOW)



def _precipitation_promote_request(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /data/precipitation/promote：人工审核闸（显式调用才固化）。

    fail-closed 校验（session_id/pack_id/rule 三面 + rule.id/trigger/effect
    形态与 trigger 正则可编译）→ 双根取包（用户层同 id 接管，无用户副本 = 整包
    承接出厂内容追加，_load_pack_assets 同口径）→ 作者面冲突闸（id 撞出厂
    behaviors 或用户副本非沉淀条目 = 409 不覆写）→ 用户层落盘（原子写，条目带
    source=precipitated + 时间 + session_id 来源标记，沉淀 id 自身幂等更新）。
    返回 (payload, status)，draft 域同款元组先例：校验缺/坏 = 400、包不存在 =
    404、作者面冲突 = 409、固化成功 = 200。
    """
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return {"error": "缺少 session_id"}, 400
    pack_id = str(body.get("pack_id") or "").strip()
    if not _PACK_ID_RE.fullmatch(pack_id):
        return {"error": f"非法 pack_id: {pack_id}"}, 400
    rule = body.get("rule")
    if not isinstance(rule, dict):
        return {"error": "缺少 rule"}, 400
    rule_id = str(rule.get("id") or "").strip()
    if not rule_id:
        return {"error": "缺少 rule.id"}, 400
    trigger = rule.get("trigger")
    if not isinstance(trigger, str) or not trigger.strip():
        return {"error": "rule.trigger 无效（须为非空字符串）"}, 400
    try:
        re.compile(trigger)
    except re.error as exc:
        return {"error": f"rule.trigger 正则无效: {exc}"}, 400
    effect = rule.get("effect")
    if not isinstance(effect, dict) or not effect:
        return {"error": "rule.effect 无效（须为非空映射）"}, 400
    for key, value in effect.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            return {"error": f"rule.effect 形态无效（键/值须非空字符串）: {key!r}"}, 400
    loaded = _load_pack_assets([pack_id], _user_packs_dir())
    if not loaded:
        return {"error": f"玩法包不存在: {pack_id}"}, 404
    _, base, _pack_dir = loaded[0]
    builtin = _read_yaml_dict(os.path.join(_PACKS_DIR, f"{pack_id}.yaml"))
    builtin_behaviors = builtin.get("behaviors") if isinstance(builtin, dict) else None
    if isinstance(builtin_behaviors, dict) and rule_id in builtin_behaviors:
        return {"error": f"规则 id 与作者声明 behavior 冲突，审核闸不覆写: {rule_id}"}, 409
    behaviors = dict(base["behaviors"]) if isinstance(base.get("behaviors"), dict) else {}
    existing = behaviors.get(rule_id)
    if existing is not None and not (
        isinstance(existing, dict) and existing.get("source") == _PRECIP_SOURCE
    ):
        return {"error": f"规则 id 与既有非沉淀条目冲突，审核闸不覆写: {rule_id}"}, 409
    user_dir = _user_packs_dir()
    if user_dir is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），沉淀规则无处落盘"}, 400
    behaviors[rule_id] = {
        "trigger": trigger,
        "effect": dict(effect),
        "source": _PRECIP_SOURCE,
        "precipitated_at": datetime.now(UTC).isoformat(),
        "precipitated_from": session_id,
    }
    payload = dict(base)
    payload["behaviors"] = behaviors
    os.makedirs(user_dir, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(user_dir, f"{pack_id}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"status": "promoted", "pack_id": pack_id, "rule_id": rule_id}, 200


# ── 场景批编排（缺口批次四，§6 M2 / 支柱 3：按场景批处理，一次调用成本 N→1）─────────
#
# POST /data/group/scene_turn：入参 {session_id, scene.background, participants,
# visibility?} → 一次 LLM 调用产出在场全员结构化反应。多角色 prompt 组装复用物料
# 组装器（material.build_injection）的 agent 键展开路径（文风随卡注入，与扮演链
# 同路径非手搓副本），每角色一份情境投影 = 认知迷雾：缺省全场景背景 + 自身状态 +
# 全体在场名单，visibility 覆盖收窄该角色可见名单（被遮蔽者不进该角色投影段）。
# 产出按契约 fail-closed 解析为 N 份 {card_id, reaction, state_ops?}——单角色解析
# 失败该份缺省并记因，不炸整轮；LLM 传输面失败（信封/流状态/模型档缺位）= 502
# 显式。反应经响应载荷 {reactions: [...]} 返回；落 thread/面板消费是后续批次边界。


_SCENE_TURN_ROUTE = "/ext/mode_roleplay/data/group/scene_turn"


_SCENE_LLM_TIMEOUT_SECS = 110.0


_SCENE_SYSTEM_PROMPT = (
    "你是群像场景导演：根据各角色情境投影，为在场每个角色各产出一份反应。要求：\n"
    "1. 每份反应符合该角色的人设、文风与自身状态，以该角色第一人称写出；\n"
    "2. 认知迷雾：每份反应只使用该角色「可见范围」内的信息，可见名单之外的角色"
    "对其不存在，反应不得提及或指向；\n"
    "3. 各角色反应互参一致（同一事件在各方反应中描述吻合）；\n"
    "4. 只输出 JSON 数组，不输出任何其他文本。数组每项形态：\n"
    '   {"card_id": "<在场名单中的卡 id>", "reaction": "<该角色第一人称反应>", '
    '"state_ops": {"<状态键>": <新值>}}\n'
    "   card_id 必须取自在场名单；state_ops（该角色状态拟变更）可省略。"
)



def _load_material():
    """包内 material.py 装载（唯一模块名——多模式包同进程 sys.path 撞名防线，
    material._load_pkg_module 同款形态）。"""
    name = "mode_roleplay_scene_material"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "material.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"material.py spec 构造失败: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod



def _scene_material_injection(card_id: str) -> str:
    """单卡物料追加注入（material.build_injection agent 键展开——与扮演链同路径）。

    目录参数对齐生产调用方（mode_material_inject._user_dirs 同语义）：
    user_agents_dir = <user_config>/agents（组装器内部自拼 mode_roleplay/<id>.yaml）、
    books_dir = <user_config>/lorebooks。伪 state 只携 agent.id 键（卡键路：
    mes_example 文风段 + 世界书扫描；扫描窗口空 = 世界书零注入）。降级契约随
    material：任何失败返回空串不阻断组装；组装器装载失败按零注入（读面降级，
    warning 留痕）。
    """
    try:
        material = _load_material()
    except Exception as exc:  # noqa: BLE001 — 组装器装载失败按零注入（读面降级）
        logger.warning("[mode_roleplay] 场景批物料组装器装载失败（零注入）| err=%s", exc)
        return ""
    config_root = _user_config_root()
    pseudo_state: dict[str, Any] = {"agent.id": f"mode_roleplay/{card_id}"}
    return material.build_injection(
        pseudo_state,
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(config_root, "lorebooks") if config_root else None,
        os.path.join(config_root, "agents") if config_root else None,
    )



def _scene_participants_resolve(
    raw: Any, cards: list[dict[str, Any]]
) -> tuple[list[str], dict[str, dict[str, Any]], str | None]:
    """participants 归一（去重保序）+ 双根卡解析；非法返回 (…, …, error)。"""
    if not isinstance(raw, list) or not raw:
        return [], {}, "participants 须为非空列表"
    by_id = {card["id"]: card for card in cards}
    participants: list[str] = []
    for pid in raw:
        pid_s = str(pid or "").strip()
        if not pid_s:
            return [], {}, "participants 含空 id"
        if pid_s not in by_id:
            return [], {}, f"参与者不在卡库: {pid_s}"
        if pid_s not in participants:
            participants.append(pid_s)
    return participants, by_id, None



def _scene_visibility_resolve(
    raw: Any, participants: list[str]
) -> tuple[dict[str, list[str]], str | None]:
    """可见性覆盖归一：{card_id → 可见他人 id 列表}；键/值越界 = error（fail-closed）。
    键缺席 = None（缺省全名单）；自见条目无害忽略。"""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "visibility 须为对象（card_id → 可见参与者 id 列表）"
    visibility: dict[str, list[str]] = {}
    for key, value in raw.items():
        key_s = str(key)
        if key_s not in participants:
            return {}, f"visibility 键不在参与者中: {key_s}"
        if not isinstance(value, list):
            return {}, f"visibility[{key_s}] 须为参与者 id 列表"
        visible: list[str] = []
        for vid in value:
            vid_s = str(vid or "").strip()
            if vid_s not in participants:
                return {}, f"visibility[{key_s}] 含非参与者: {vid_s}"
            if vid_s != key_s and vid_s not in visible:
                visible.append(vid_s)
        visibility[key_s] = visible
    return visibility, None



def _scene_prompt(
    background: str,
    cards: list[dict[str, Any]],
    visibility: dict[str, list[str]],
    state_texts: dict[str, str],
) -> tuple[str, str]:
    """多角色单调用 prompt 组装（导演 system 契约 + 逐角色情境投影 user 段）。

    每角色一份情境投影 = 认知迷雾：全场景背景 + 自身状态（有则注入）+ 可见范围
    名单（visibility 覆盖收窄，缺省 = 其余全体在场；被遮蔽者的名字与设定不进该
    角色投影段）。返回 (system_text, user_text)。
    """
    names = {card["id"]: str(card.get("name") or card["id"]) for card in cards}
    roster = "\n".join(f"- {card['id']}（{names[card['id']]}）" for card in cards)
    sections: list[str] = []
    for card in cards:
        cid = card["id"]
        visible_ids = visibility.get(cid, [other["id"] for other in cards if other["id"] != cid])
        visible_names = "、".join(
            f"{vid}（{names[vid]}）" for vid in visible_ids if vid in names
        )
        lines = [f"## 角色 {cid}（{names[cid]}）"]
        for label, field in (("人设", "description"), ("性格", "personality")):
            text = str(card.get(field) or "").strip()
            if text:
                lines.append(f"- {label}：{text}")
        state_text = state_texts.get(cid) or ""
        if state_text:
            lines.append(f"- 自身状态：\n{state_text}")
        lines.append(f"- 可见范围（在场的他人）：{visible_names or '无'}")
        lines.append(f"- 情境投影（你所处的场景）：\n{background}")
        injection = _scene_material_injection(cid)
        if injection:
            lines.append(injection)
        sections.append("\n".join(lines))
    user_text = f"# 在场角色\n{roster}\n\n# 各角色情境投影\n" + "\n\n".join(sections)
    return _SCENE_SYSTEM_PROMPT, user_text



async def _scene_card_state_text(card_id: str) -> str:
    """角色自身状态读面（character_state.get 经 tool-executor 显式 plugin_id，
    state_marker_parse 跨插件调用同通道）。未初始化/服务不可用 = 空串（状态是
    可选增强，fail-soft——读面降级契约同口径）。"""
    try:
        te = plugin.get_capability("tool-executor")
        caller = bind_capability_caller(te, "tool-executor")
        res = await caller(
            "tool-executor.invoke",
            {
                "tool_name": "character_state.get",
                "plugin_id": "character_state",
                "args": {"card_id": card_id},
            },
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 — 状态读面 fail-soft（未初始化属常态）
        logger.debug("[mode_roleplay] 场景批状态读面降级 | card=%s | err=%s", card_id, exc)
        return ""
    if not (isinstance(res, dict) and res.get("success") is True):
        return ""
    data = res.get("data")
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict) or not values:
        return ""
    return "\n".join(f"- {key}: {values[key]}" for key in values)



def _scene_json_entries(text: str) -> list[Any] | None:
    """LLM 产出文本 → JSON 条目列表候选（裸解析 → 围栏剥离 → 括号截取；
    {"reactions": [...]} 信封展开；全败 = None）。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    parsed: Any = None
    for candidate in (stripped,):
        try:
            parsed = json.loads(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            start = stripped.find(open_ch)
            end = stripped.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    parsed = json.loads(stripped[start : end + 1])
                    break
                except ValueError:
                    continue
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("reactions"), list):
        return parsed["reactions"]
    return None



def _scene_reactions_parse(text: str, participants: list[str]) -> list[dict[str, Any]]:
    """LLM 产出文本 → N 份逐角色反应（fail-closed 解析，输入序对位）。

    单份判据：card_id 命中参与者、reaction 非空字符串；state_ops 出场必须为
    dict（否则该份整体记因——fail-closed 不静默丢字段）。重复条目取首个，未知卡
    条目忽略；解析不出任何 JSON = 全员记因。输出与参与者一一对应同序。
    """
    entries = _scene_json_entries(text)
    by_card: dict[str, dict[str, Any]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        card_id = entry.get("card_id")
        if isinstance(card_id, str) and card_id not in by_card:
            by_card[card_id] = entry
    out: list[dict[str, Any]] = []
    for pid in participants:
        entry = by_card.get(pid)
        if entry is None:
            out.append({"card_id": pid, "error": "无该角色反应条目"})
            continue
        reaction = entry.get("reaction")
        if not isinstance(reaction, str) or not reaction.strip():
            out.append({"card_id": pid, "error": "reaction 缺失或为空"})
            continue
        item: dict[str, Any] = {"card_id": pid, "reaction": reaction}
        state_ops = entry.get("state_ops")
        if state_ops is not None:
            if not isinstance(state_ops, dict):
                out.append({"card_id": pid, "error": "state_ops 形态非法（须为对象）"})
                continue
            item["state_ops"] = state_ops
        out.append(item)
    return out



async def _scene_llm_text(model: str, system_text: str, user_text: str) -> str:
    """单次 LLM 调用（tool-executor.invoke 显式 plugin_id=llm_service 同步通道，
    与其余 LLM 调用方一致）→ 产出文本。

    传输面失败（capability 异常 / 信封 success=false / 流状态非 streamed / 文本
    空）= RuntimeError 上抛，调用方 502 显式（不假成功）。
    """
    payload = {
        "tool_name": "llm.complete_stream",
        "plugin_id": "llm_service",
        "args": {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        },
    }
    te = plugin.get_capability("tool-executor")
    caller = bind_capability_caller(te, "tool-executor")
    res = await caller("tool-executor.invoke", payload, timeout=_SCENE_LLM_TIMEOUT_SECS)
    if not isinstance(res, dict) or res.get("success") is not True:
        detail = str(res.get("error") if isinstance(res, dict) else res)[:200]
        raise RuntimeError(f"LLM 调用失败: {detail}")
    data = res.get("data")
    if not isinstance(data, dict) or data.get("status") != "streamed":
        raise RuntimeError(f"LLM 流未正常完成: {str(data)[:200]}")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("LLM 产出为空")
    return text



async def _handle_scene_turn(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/group/scene_turn：场景批一次调用编排（域段头注释载完整契约）。

    校验 fail-closed（缺参/会话不存在或非扮演会话/参与者不在卡库/可见性越界 =
    400）→ 模型档解析（缺位 502）→ 逐角色状态读面（fail-soft）→ 多角色单调用
    prompt 组装（material 同路径 + 认知迷雾投影）→ 一次 LLM 调用（传输面失败
    502）→ fail-closed 解析 N 份反应（单角色降级记因不炸整轮）→ 200 {reactions}。
    """
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return _game_routed({"error": "缺少 session_id"}, 400)
    rows = await _call_provider("pipeline-state")
    if not any(
        isinstance(row, dict)
        and str(row.get("pipeline_id") or "") == session_id
        and str(row.get("mode") or "") == "roleplay"
        for row in rows
    ):
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    scene = body.get("scene")
    if not isinstance(scene, dict):
        return _game_routed({"error": "scene 须为对象（含 background 情境描述）"}, 400)
    background = str(scene.get("background") or "").strip()
    if not background:
        return _game_routed({"error": "缺少 scene.background（情境描述为空）"}, 400)
    participants, by_id, err = _scene_participants_resolve(body.get("participants"), _load_cards())
    if err:
        return _game_routed({"error": err}, 400)
    visibility, err = _scene_visibility_resolve(body.get("visibility"), participants)
    if err:
        return _game_routed({"error": err}, 400)
    model = _llm_default_chat_model()
    if not model:
        return _game_routed({"error": "模型缺省档不可得（llm.yaml defaults.chat 缺失或损坏）"}, 502)
    state_texts = {pid: await _scene_card_state_text(pid) for pid in participants}
    system_text, user_text = _scene_prompt(
        background, [by_id[pid] for pid in participants], visibility, state_texts
    )
    try:
        text = await _scene_llm_text(model, system_text, user_text)
    except Exception as exc:  # noqa: BLE001 — 传输面失败 502 显式（不假成功）
        logger.warning("[mode_roleplay] 场景批 LLM 调用失败 | session=%s | err=%s", session_id, exc)
        return _game_routed({"error": f"场景批 LLM 调用失败: {exc}"}, 502)
    return _game_routed({"reactions": _scene_reactions_parse(text, participants)})


# ── 开演向导四轴候选集（C2 整合刀：物料整合选项面接入既有向导）────────────────────
#
# 快照契约（B8 既有通道，零新透传面）：面板把四轴选择组装为单个 execution_context
# 键 rp_axes（mode.session extra_context，桥 fail-closed 只收非空字符串），形态
# 「k=v 对以逗号串承载」：scale=fade_to_black,pace=reasonable,agency=you_control
# （选了文风档再追加 style=<styles/ 档 id>；文风不选=无 style 对）。prompt 组装
# 的注入消费=后续刀（material.py 头部契约注释登记），本端点只供候选集。
#
# 考据（候选集来源=物料整合 docs/working/rp_prompt_material/，措辞原创）：
# - 尺度轴：SUMMARY.md G2 领域内容注记「尺度维度（explicit/暗示/fade-to-black
#   三档）按 G2 枚举档形态落」——与 WBU 叙事控制滑块（wbu-craft-control-sliders，
#   MIT）的参数化枚举档同构；情色文体 craft 件（2026-10-13 收全轮）为档位语义
#   底料，本处只立档不录原文。
# - 节奏轴：gameplay/spiderweb-zh-summary-options-time.md「剧情推进三选一」
#   世界观换装（合理（默认）/蝴蝶效应/普通；第三档原设定配套互斥组涉 NSFW
#   定位按收录纪律不录，此处只取推进哲学本身）。
# - 代理权轴：gameplay/dem-agency-set.md 玩家代理权三态（You control=默认/
#   Director/Input Improver；DEUS.EX.MACHINA V2.5，CC-BY-NC-SA）。


_WIZARD_AXES_ROUTE = "/ext/mode_roleplay/data/wizard/axes"

# 玩法包清点独立端点（面板数据驱动刀）：向导玩法勾选组与概览计数数据源

_PACKS_ROUTE = "/ext/mode_roleplay/data/packs"

# 尺度轴三档（id 面向 rp_axes 快照，name/description 面向向导展示）

def _assemble_page_single_source(html: str, page_label: str) -> str:
    """serve-time 单源装配（rp-sanitize / rp-bridge 必选两标记 + rp-core 可选
    标记原位替换；出厂游戏页与包内自包含 ui.page 共用单点）。

    fail-closed：装配标记/面板单源块缺失 = RuntimeError 上抛（页面缺消毒/
    桥函数属包事实，不静默降级为未装配渲染面）；rp-core 反之（标记缺席=跳过，
    见 _CORE_INCLUDE_MARK 注释）。panel 文件每次现场读（调用频度低，免去进程
    级缓存失效面）。
    """
    with open(_PANEL_HTML_PATH, encoding="utf-8") as fh:
        panel = fh.read()
    for mark, source_re, name in _PAGE_ASSEMBLIES:
        if mark not in html:
            raise RuntimeError(f"{page_label} 缺 {name} 装配标记（{mark}）")
        match = source_re.search(panel)
        if match is None:
            raise RuntimeError(f"{name} 单源块缺失（roleplay_panel.html）")
        html = html.replace(mark, match.group(1))
    if _CORE_INCLUDE_MARK in html:
        match = _CORE_SOURCE_RE.search(panel)
        if match is None:
            raise RuntimeError("rp-core 单源块缺失（roleplay_panel.html）")
        html = html.replace(_CORE_INCLUDE_MARK, match.group(1))
    return html



def _read_yaml_dict(path: str) -> dict[str, Any] | None:
    """单 yaml 文件读取；缺失/损坏/顶层非映射 = None（读面 fail-soft）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None



def _load_pack_assets(
    pack_ids: Any, user_dir: str | None
) -> list[tuple[str, dict[str, Any], str]]:
    """卡 pack_ids → 声明序 (pack_id, pack 原始内容, 所在目录)（双根：用户层同
    id 接管 → 回落包内）。

    游戏域读面专用（server.py 种子自包含，不 import 管道步 rp_packs）；fail-soft：
    id 形态非法/两根皆缺/解析失败 = 该 pack 跳过（读面降级，不阻断 config）。
    目录随根透出——呈现面声明（ui.theme_css/ui.page）相对路径按接管根解析。
    """
    ids = [str(pid) for pid in pack_ids] if isinstance(pack_ids, list) else []
    loaded: list[tuple[str, dict[str, Any], str]] = []
    for pack_id in ids:
        if not _PACK_ID_RE.fullmatch(pack_id):
            continue
        data = _read_yaml_dict(os.path.join(user_dir, f"{pack_id}.yaml")) if user_dir else None
        pack_dir = user_dir or ""
        if data is None:
            data = _read_yaml_dict(os.path.join(_PACKS_DIR, f"{pack_id}.yaml"))
            pack_dir = _PACKS_DIR
        if data is not None:
            loaded.append((pack_id, data, pack_dir))
    return loaded



def _merge_game_actions(sink: list[dict[str, Any]], by_id: dict[str, int], raw: Any) -> None:
    """actions 声明拼接：非映射/无 id 条目剔除（宿主白名单以 id 为键）；同 id
    后者胜（卡覆盖 pack）；逐条补 target 缺省 engine（与装载侧 lint_actions
    缺省同口径——宿主 handler 只执行 target=engine）。"""
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        action_id = str(entry.get("id") or "").strip()
        if not action_id:
            continue
        normalized = dict(entry)
        normalized["id"] = action_id
        normalized.setdefault("target", "engine")
        if action_id in by_id:
            sink[by_id[action_id]] = normalized
        else:
            by_id[action_id] = len(sink)
            sink.append(normalized)



def _norm_pack_choices(raw: Any) -> list[dict[str, Any]]:
    """pack choices 声明 → 载荷条目（归一剔除面，坏条目剔除不整组作废）。

    条目契约（pack yaml 头注释：text/category/hint，点击语义 = 清单全等选取）：
    text 必填非空字符串；category/hint 可选非空字符串（缺席 = 键缺席）。全部
    坏条目/非列表 = 空表（调用方按无声明处理 = 键缺席）。
    """
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        item: dict[str, Any] = {"text": text.strip()}
        for key in ("category", "hint"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()
        items.append(item)
    return items



def _pack_operations_listing(data: dict[str, Any], pack_id: str) -> list[dict[str, Any]]:
    """pack 顶层 operations 声明 → 载荷条目（归一剔除面，坏条目整条剔除不整组作废）。

    结构单源 card_lint.lint_operations（零 IO 形态面）；条目契约
    ``{id, label, params_schema?, effects}``——id/label/effects 任一非法或
    params_schema 形态非法 = 整条剔除 + warning（与 ui.lineage 剔除回落同构，
    玩法页/操作通道永远只见合法声明——未声明=不可发，枚举声明制）。effects 键
    域不在此判（归 Adjudicate 侧 Validator fail-closed，与 init_ui effects 同闸）。
    """
    raw = data.get("operations")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "[mode_roleplay] pack %s operations 须为列表，声明剔除: %s",
            pack_id,
            type(raw).__name__,
        )
        return []
    for warning in lint_operations(raw):
        logger.warning("[mode_roleplay] pack %s %s", pack_id, warning)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        op_id = entry.get("id")
        label = entry.get("label")
        if (
            not isinstance(op_id, str)
            or not op_id
            or not OPERATION_ID_RE.fullmatch(op_id)
            or op_id in seen
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(entry.get("effects"), dict)
        ):
            continue
        params_schema = entry.get("params_schema")
        if params_schema is not None and not isinstance(params_schema, dict):
            continue
        seen.add(op_id)
        item: dict[str, Any] = {"id": op_id, "label": label, "effects": dict(entry["effects"])}
        if isinstance(params_schema, dict):
            item["params_schema"] = dict(params_schema)
        items.append(item)
    return items



def _aggregate_game_decls(
    card: dict[str, Any],
    packs: list[dict[str, Any]],
    pipelines_dir: str | None = None,
) -> dict[str, Any]:
    """卡+pack Tier 1 声明聚合 → 宿主载荷增量（键缺席式：无声明零新增键）。

    - rules / narrative_graph / template / ui：映射浅合并——pack 声明序后者逐键
      覆盖 → 卡逐键覆盖 pack（与 rp_packs Tier 0 合并语义同构：键值整体替换、
      不深合并，嵌套覆盖边界归作者显式声明）；pack 侧 narrative_graph 键名为
      graph，template/ui 与卡侧同名（rp_packs _DEFERRED_PACK_KEYS 契约名）。
    - actions：pack 声明序拼接 + 卡追加（同 id 卡胜，见 _merge_game_actions）。
    - choices（RP 面板 v3.1 刀6 增透，只增键）：包级静态清单，pack 声明序后者
      非空清单整体覆盖（pack yaml 头「同键合并 = 后到覆盖」同构）；卡侧不参与
      （choices 为包级声明面，卡 extensions 无此声明位）。消费方 = 工作区玩法页
      叙事流按钮卡（4e；消息流 webview 卡退役后无对话流消费位）。
    - pipeline（世界内嵌谱系右移批次一，只增键）：玩法级管道绑定，pack 声明序
      后者胜（逐包独立过 _pack_pipeline_listing 同闸：形态+登记存在性，非法
      剔除不整组作废）；卡侧不参与（编排绑定是包级声明面，ADR 权威光谱框架
      §③ 两级正交——玩法级管道绑定 × 角色级 agent 键）。消费方 = 宿主/开演面
      （会话出生显式覆盖缺省 mode 绑定管道，键缺席回落 roleplay）。
    - operations（世界内嵌谱系右移批次二，只增键）：自由操作→结构化命令声明
      清单（调研 §7.6 真缺口①）——包级声明面（卡侧不参与，choices 同构），
      pack 声明序后者非空清单整体覆盖；逐包过 _pack_operations_listing 归一
      剔除闸。消费方 = 出厂游戏页操作区（声明清单→按钮/参数表单）+
      POST game/operation（清单外 id fail-closed 拒绝，未声明=不可发）。
    - tick（世界内嵌谱系右移批次三，只增键）：世界演化周期推进声明
      （调研 §7.6 真缺口②）——包级声明面（卡侧不参与），pack 声明序后者非空
      清单整体覆盖；逐包过 _pack_tick_listing 归一剔除闸（when 键域唯一执法
      位）。消费方 = 出厂游戏页世界演化区（声明在场才渲染⏩推进）+
      POST game/tick（逐 tick×逐 step 顺序结算，state.adjudicate 单源）+
      触发器定时直达（登记不实现）。
    """
    rules: dict[str, Any] = {}
    graph: dict[str, Any] = {}
    template: dict[str, Any] = {}
    ui: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    choices: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    tick_steps: list[dict[str, Any]] = []
    pipeline: str | None = None
    for index, pack in enumerate(packs):
        for src, sink in (
            ("rules", rules),
            ("graph", graph),
            ("template", template),
            ("ui", ui),
        ):
            value = pack.get(src)
            if isinstance(value, dict):
                sink.update(value)
        _merge_game_actions(actions, by_id, pack.get("actions"))
        normalized = _norm_pack_choices(pack.get("choices"))
        if normalized:
            choices = normalized
        normalized_ops = _pack_operations_listing(
            pack, str(pack.get("id") or f"#{index}")
        )
        if normalized_ops:
            operations = normalized_ops
        normalized_tick = _pack_tick_listing(
            pack, str(pack.get("id") or f"#{index}")
        )
        if normalized_tick:
            tick_steps = normalized_tick
        declared_pipeline = _pack_pipeline_listing(
            pack, str(pack.get("id") or f"#{index}"), pipelines_dir
        )
        if declared_pipeline is not None:
            pipeline = declared_pipeline
    extensions = card.get("extensions")
    if isinstance(extensions, dict):
        card_rules = extensions.get("rules")
        if isinstance(card_rules, dict):
            rules.update(card_rules)
        card_graph = extensions.get("narrative_graph")
        if isinstance(card_graph, dict):
            graph.update(card_graph)
        card_template = extensions.get("template")
        if isinstance(card_template, dict):
            template.update(card_template)
        card_ui = extensions.get("ui")
        if isinstance(card_ui, dict):
            ui.update(card_ui)
        _merge_game_actions(actions, by_id, extensions.get("actions"))
    payload: dict[str, Any] = {}
    if rules:
        payload["rules"] = rules
    if graph:
        payload["narrative_graph"] = graph
    if actions:
        payload["actions"] = actions
    if template:
        payload["template"] = template
    if ui:
        payload["ui"] = ui
    if choices:
        payload["choices"] = choices
    if operations:
        payload["operations"] = operations
    if tick_steps:
        payload["tick"] = {"steps": tick_steps}
    if pipeline is not None:
        payload["pipeline"] = pipeline
    return payload



def _aggregate_pack_init_ui(packs: list[tuple[str, dict[str, Any], str]]) -> dict[str, Any] | None:
    """pack init_ui 声明本体聚合（/game/config 透出面，声明序后者逐键浅覆盖）。

    本刀只透出不消费（开演设置渲染归面板 init_ui 载荷；此处供包页/面板未来
    自渲染声明源）。卡侧不参与（init_ui 是包级声明）。无声明 = None（键缺席）。
    """
    merged: dict[str, Any] = {}
    for _, data, _ in packs:
        value = data.get("init_ui")
        if isinstance(value, dict):
            merged.update(value)
    return merged or None



def _pack_page_asset(
    packs: list[tuple[str, dict[str, Any], str]],
) -> tuple[str, str, str | None, str] | None:
    """声明序第一个 ui.page 有效包 → (pack_id, page 相对路径, theme_css 相对
    路径或 None, 包目录)；无 = None。

    有效 = ui.page 归一剔除面通过（文件实存，_pack_ui_listing 同闸）；theme_css
    取同一包（页与主题同源自包含，不跨包拼装）。
    """
    for pack_id, data, pack_dir in packs:
        ui, has_page = _pack_ui_listing(data, pack_dir, pack_id)
        if has_page and ui is not None and ui.get("page"):
            return (
                pack_id,
                str(ui["page"]),
                str(ui["theme_css"]) if ui.get("theme_css") else None,
                pack_dir,
            )
    return None



def _assemble_pack_page(page_asset: tuple[str, str, str | None, str]) -> str:
    """(pack_id, page 相对路径, theme_css 相对路径或 None, 包目录) → 装配后页
    HTML（theme <style> 内联页头 + serve-time 单源装配）。

    session 通道与 pack 通道（刀4）共用的装配单点；IO/装配失败上抛，回落语义
    由调用方定（两通道同为 fail-soft null + warning）。
    """
    _pack_id, page_rel, theme_rel, pack_dir = page_asset
    page_path = _pack_rel_path_in_dir(pack_dir, page_rel)
    if page_path is None:
        raise OSError(f"page 文件不可定位: {page_rel}")
    with open(page_path, encoding="utf-8") as fh:
        html = fh.read()
    if theme_rel:
        theme_path = _pack_rel_path_in_dir(pack_dir, theme_rel)
        if theme_path is not None:
            with open(theme_path, encoding="utf-8") as fh:
                html = _inject_page_theme(html, fh.read())
    return _assemble_page_single_source(html, f"pack {page_rel}")



def _inject_page_theme(html: str, css: str) -> str:
    """包内主题 css → <style> 内联注入页头（<head> 在场 = 紧随其后；否则页首）。"""
    style = f"<style>\n{css}\n</style>"
    lowered = html.lower()
    head_index = lowered.find("<head")
    if head_index == -1:
        return f"{style}\n{html}"
    close_index = html.find(">", head_index)
    if close_index == -1:
        return f"{style}\n{html}"
    return html[: close_index + 1] + f"\n{style}" + html[close_index + 1 :]



async def _handle_game_config(session_id: str) -> dict[str, Any]:
    """GET /game/config：会话绑定卡+pack 的 Tier 1 声明聚合（宿主引擎件配置面）。

    session → 行 agent_id（mode_roleplay/<card_id> 卡键，面板开演同款）→ 卡 →
    pack_ids 双根加载 → 聚合。无会话行/agent_id 非本模式卡键/卡不可得/无声明 =
    键缺席（{session_id} 起步；Tier 0 兼容态，宿主引擎件空转）。
    """
    raw = str(session_id or "").strip()
    if not raw:
        return _game_routed({"error": "缺少 session_id"}, 400)
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return _game_routed(
            {"error": f"非法 session_id（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"}, 400
        )
    payload: dict[str, Any] = {"session_id": raw}
    rows = await _call_provider("pipeline-state")
    agent_id = next(
        (
            str(row.get("agent_id") or "")
            for row in rows
            if isinstance(row, dict) and str(row.get("pipeline_id") or "") == raw
        ),
        "",
    )
    prefix = "mode_roleplay/"
    if not agent_id.startswith(prefix):
        return _game_routed(payload)
    card_id = agent_id[len(prefix):]
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return _game_routed(payload)
    payload["card_id"] = card_id
    packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
    payload.update(_aggregate_game_decls(card, [data for _, data, _ in packs],
                                         pipelines_dir=_user_pipelines_dir()))
    init_ui = _aggregate_pack_init_ui(packs)
    if init_ui is not None:
        payload["init_ui"] = init_ui
    if _pack_page_asset(packs) is not None:
        payload["ui_page"] = True
    return _game_routed(payload)



def _handle_game_page_by_pack(pack_id: str) -> dict[str, Any]:
    """GET /game/page?pack=<id>：按包 id 直取自包含玩法页装配载荷（刀4 setup 通道）。

    会话未开（开演设置阶段）无从经 session 通道解析包——双根按 id 直取（用户层
    同 id 接管 → 回落包内，_load_pack_assets 同口径），装配与 session 通道共用
    _assemble_pack_page 单点。响应 {pack_id, page_html}：包未知/无有效 page 声明/
    装配失败 = page_html null（200 非 404，fail-soft 消费方回落契约同 session
    通道）。
    """
    if not _GAME_HOST_ID_RE.fullmatch(pack_id):
        return _game_routed(
            {"error": f"非法 pack（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {pack_id}"}, 400
        )
    payload: dict[str, Any] = {"pack_id": pack_id, "page_html": None}
    loaded = _load_pack_assets([pack_id], _user_packs_dir())
    if not loaded:
        return _game_routed(payload)
    hit_id, data, pack_dir = loaded[0]
    ui, has_page = _pack_ui_listing(data, pack_dir, hit_id)
    if not has_page or ui is None or not ui.get("page"):
        return _game_routed(payload)
    try:
        html = _assemble_pack_page((
            hit_id,
            str(ui["page"]),
            str(ui["theme_css"]) if ui.get("theme_css") else None,
            pack_dir,
        ))
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "[mode_roleplay] 自包含玩法页装配失败（pack 通道），回落 null | "
            "pack=%s | err=%s",
            hit_id,
            exc,
        )
        return _game_routed(payload)
    payload["page_html"] = html
    return _game_routed(payload)



async def _handle_game_page(session_id: str, pack: str = "") -> dict[str, Any]:
    """GET /game/page：自包含玩法页装配载荷（{page_html}；无任何有效 page 声明
    或装配失败 = page_html 为 null——非 404，消费方据此回落通用 game_host 渲染，
    Tier 0 兼容态）。双通道（刀4）：

    - ?session_id=<sid>（刀1 契约零变化）：会话绑定卡+pack，声明序第一个
      ui.page 有效包 → 读包内 page.html serve-time 单源装配 + 同包 theme_css
      <style> 内联页头；
    - ?pack=<id>（setup 通道）：会话未开时按包 id 直取（_handle_game_page_by_pack）。

    装配失败（作者页缺装配标记）= fail-soft 回落 null + warning（面板不可因坏
    作者页断链；出厂件由装配测试执法不会走到）。
    """
    raw_pack = str(pack or "").strip()
    if raw_pack:
        return _handle_game_page_by_pack(raw_pack)
    raw = str(session_id or "").strip()
    if not raw:
        return _game_routed({"error": "缺少 session_id"}, 400)
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return _game_routed(
            {"error": f"非法 session_id（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"}, 400
        )
    payload: dict[str, Any] = {"session_id": raw, "page_html": None}
    rows = await _call_provider("pipeline-state")
    agent_id = next(
        (
            str(row.get("agent_id") or "")
            for row in rows
            if isinstance(row, dict) and str(row.get("pipeline_id") or "") == raw
        ),
        "",
    )
    prefix = "mode_roleplay/"
    if not agent_id.startswith(prefix):
        return _game_routed(payload)
    card = next((c for c in _load_cards() if c["id"] == agent_id[len(prefix):]), None)
    if card is None:
        return _game_routed(payload)
    packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
    page_asset = _pack_page_asset(packs)
    if page_asset is None:
        return _game_routed(payload)
    try:
        html = _assemble_pack_page(page_asset)
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "[mode_roleplay] 自包含玩法页装配失败，回落通用渲染（page_html=null）| "
            "session=%s | err=%s",
            raw,
            exc,
        )
        return _game_routed(payload)
    payload["page_html"] = html
    return _game_routed(payload)
