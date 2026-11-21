#!/usr/bin/env python3
"""角色扮演模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 mode.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，ADR 2026-09-17-mode-panel-mature-interfaces；
2026-09-17 宿主融合：对话回归对话框，面板不再内置对话流）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_roleplay/data/{bootstrap,sessions,messages,cards,lorebooks,packs}。
  messages 端点保留（零连带），面板 UI 不再消费（对话在宿主聊天区）。
  packs 独立清点端点（面板数据驱动刀 2026-10-13）：双根载荷（出厂∪用户层，
  同 id 接管；fail-soft 目录缺失=空列表）——向导玩法勾选组与概览计数的直接
  数据源（bootstrap 聚合键保留不动，载荷形状由既有测试钉死）；一并返回自定义
  玩法 combos（RP 面板 v3.1 刀1，面板大厅自定义卡）。pack 条目含呈现面声明
  归一载荷 ui（icon/style_tags/lineage/theme_css/page，键缺席式）+ has_page
  布尔（ui.page 有效声明=true，大厅「点玩法直达游戏页」路由依据）+ init_ui
  （开演设置页 routes/trait_pools 载荷，键缺席式——无声明=面板回落通用组合
  配置）。结构判定单源在本插件 card_lint（lint_ui/lint_init_ui），本侧做归一
  剔除（lineage 非法剔除回落缺省档、theme_css/page 文件缺失剔除 + warning）。
  sessions 会话行增 pack_ids（玩法归属：行 agent_id 卡 pack_ids ∪ 行内
  wizard_pack_ids 持久键，都不可得=空数组）与 state_summary（行内持久 state
  摘要；键不在=键缺席），支持 ?pack=<id> 过滤（「继续上局」按玩法归属）。
  bootstrap 聚合玩法包清点（RP 面板刀-Ⅰ 开演向导玩法勾选页数据源）：包内
  packs/ ∪ 用户层 <user_config_dir>/packs 双根合并展示面（id/origin/计数），
  零新增端点——向导为纯前端消费面（复用 data/cards / data/lorebooks /
  data/personas + bootstrap 载荷）。
- 向导四轴候选集（C2 整合刀）：GET /data/wizard/axes——文风轴动态列包内
  styles/ 档位（档位名+特征清单首行做选项描述；目录缺失=空列表降级）+
  尺度/节奏/代理权三静态轴（考据清单，注释在轴定义处）。面板四轴选择组装为
  mode.session extra_context 的 rp_axes 快照键（k=v 逗号串，B8 既有通道零新
  透传面）；prompt 组装的注入消费=后续刀（material.py 契约注释登记）。
- 写动作：/data/actions/play（possess-only；开演已会话化，变体生成收编进
  扮演会话的宿主消息操作——重新生成/‹i·n› 多代切换，本插件零任务派发面）
  （eval_harness 先例），args 携 mode=roleplay；行级锚定来源管道 thread_id，
  body 显式 session_id 兜底。/data/actions/play 为 possess-only（校验卡 + 回执
  presenter；fresh 开演已会话化——面板走 roleplay.continue 宿主桥建扮演会话，
  扮演是对话不是任务，无任务派发无评估语义，2026-09-25 用户痛点修复）。
  卡 = 模式命名空间 agent 键（mode_roleplay/card_<id>，模式体系设计 §8.1）。
- 卡 theme/avatar：/data/cards 透出卡 yaml 的 theme（ThemeConfig 档，面板选卡时
  经 theme.apply 桥注入对话框）与 avatar（画廊头像）。
- 卡库双根（成熟化 Wave B）：/data/cards = 包内出厂卡（origin="builtin"）∪ 用户层
  <user_config_dir>/agents/mode_roleplay（origin="user"；同 id 接管出厂，出厂件
  永不被写）。写面 /data/cards/{save,delete,import,export}：save/import 落用户层
  （出厂同 id 覆盖即接管语义，不产出错误）；delete 仅用户卡（出厂卡拒绝并指引
  复制修改）；import 收 ST V2/V3 PNG/JSON（card_io，data 字段平铺）并对
  description+system_prompt 拼文做可疑指令模式扫描（只标注 warnings 不阻断）；
  export 出 ST V2 信封（format=json 缺省/PNG 可选，base64 载荷对称下载）。
- 世界书双根（用户层物料双根化）：/data/lorebooks = 包内出厂书（origin=
  "builtin"）∪ 用户层 <user_config_dir>/lorebooks（origin="user"；同 id 接管，
  出厂件永不被写）。写面 /data/lorebooks/{save,delete,import}：save/import 落
  用户层（同 id 覆盖即接管语义）；delete 仅用户书（出厂书拒绝并指引复制修改）；
  import 收 SillyTavern World Info JSON（entries 数字键对象/数组双形态，字段
  映射见 _entry_from_st），命中可疑指令模式只标注 warnings 不阻断。世界书经卡
  lorebook_ids 绑定生效：扮演组装器（material.py）按同款双根查找注入，用户
  新建/导入书入卡绑定即进扮演上下文。
- persona 档案：<user_config_dir>/persona/roleplay_personas.json（name 唯一键，
  原子写）；GET /data/personas + POST /data/personas/{save,delete}（开演档用户
  设定的档案面，面板选中档经桥上行 description 文本）。
- 自定义玩法 combos（RP 面板 v3.1 刀1）：<user_config_dir>/rp_combos/<id>.json
  （用户件同 id 覆盖；{id, name, card_id, lorebook_ids, route_id, trait_ids,
  pack_ids, created_at, origin:"user"}）；GET /data/packs 一并返回 + POST
  /data/combos/{save,delete}（name/card_id 必填、card_id 须在卡库 fail-closed、
  id 缺省按 name 派生避让）——大厅自定义卡「一键重开」的存储面。
- 沉淀回路（缺口批次三，§7.4）：GET /data/precipitation/candidates?session_id=
  ——会话审计事件流水复盘统计，命名动作→同状态键同向同幅转移重复出现（≥2 次）
  聚为候选规则（可枚举性判据：幅度漂移/被拒/作弊旁路/behavior/簿记行不成例；
  未知会话/无流水=空列表）；POST /data/precipitation/promote——人工审核闸
  （显式调用才固化），候选规则体（标准 behaviors 形态）写入用户层玩法包
  （双根同 id 接管即生效，求值器 4a 通道零 LLM 消费），条目带来源标记
  （source=precipitated+时间+session_id）可单独识别回退；作者声明 behavior
  同 id=409 不覆写作者面，沉淀条目自身幂等更新。
- 场景批编排（缺口批次四，§6 M2 / 支柱 3）：POST /data/group/scene_turn——
  入参 {session_id, scene.background, participants, visibility?}，一次 LLM 调用
  产出在场全员结构化反应（成本 N→1，互参一致）；多角色 prompt 组装复用物料组装
  器 agent 键展开（material.build_injection 同路径：文风随卡注入），每角色一份
  情境投影 = 认知迷雾（缺省全场景背景+自身状态+全体在场名单，visibility 覆盖
  收窄可见名单）；产出 fail-closed 解析为 N 份 {card_id, reaction, state_ops?}
  （单角色解析失败该份缺省记因不炸整轮；LLM 传输面失败 502），经响应载荷
  {reactions: [...]} 返回——落 thread/面板消费是后续批次边界。
- 创作草稿五态状态机（扩展波 B-Ⅰ，设计文档 §9.2）：GET /data/wizards/drafts +
  POST /data/wizards/draft/{register,lint,smoke,submit,approve,reject}——AI 创作
  流（创作 agent 起草 → 登记 → lint → 试玩 → 人审 → 入库），草稿落
  <user_config>/rp_drafts/；三闸必经（approve 强制 lint/smoke 证据 = 409）、
  creator AI 署名强制、非法迁移 fail-closed；smoke 一期人工试玩登记，自动探针
  经 B-Ⅱ 考据维持降级（诚实收敛）：eval 域 suite.smoke 是 agent 编排任务链管道
  （eval_view_suite/eval_summarize 落自进化账本），与草稿卡探针错配（草稿卡非
  agent 键/账本耦合/同步 HTTP vs 分钟级任务链），升级触发 = eval 域提供与进化
  账本解耦的卡级探针库化服务面；probe 参数携带即 400 显式降级说明（三面登记：
  plugin.json 端点描述 + 本 docstring + 面板注记）。方法论喂养（C4 整合刀）：
  起草工艺五段指引（建卡/intake 蒸馏/模板组织/玩法包四件/「插件的插件」定位）
  载于 agents/card_author.yaml system_prompt（来源考据件名随文），端点契约面
  零变化。
- 开演端点 possess-only（2026-09-25 会话化，扮演是对话不是任务）：校验卡存在
  后回卡档案（presenter 含 theme），附身状态由前端持久化并在发送时注入
  execution_context，服务端无状态；fresh 开演已移除（面板走 roleplay.continue
  宿主桥建扮演会话，开场白/用户设定经会话执行选项快照随消息透传），未知
  play_mode 400 显式拒绝。

种子单元的共享边界：profile 服务与面板页 HttpHandleResponse 封装收敛到
共享根 _libs 基面（mode_server_base；bootstrap 的 SDK 位置锚使播种副本可达
共享根），读数桥与卡/包物料面仍在本文件内联。
"""
# config-write-surface-exempt: 模式物料写用户数据层(user_space双根,出厂件只读)
from __future__ import annotations

import base64
import importlib.util  # noqa: F401
import json
import logging
import os
import re
import sys  # noqa: F401
import time  # noqa: F401
from datetime import UTC, datetime  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, TypeGuard  # noqa: F401

import yaml  # noqa: F401
from _libs.card_contract import flatten_variables  # noqa: E402 — 卡片变量扁平化（_pack_tick_listing）
from card_lint import TICK_WHEN_OPS, lint_tick  # noqa: E402 — when 键域与 lint 单源（_pack_tick_listing）

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin  # noqa: F401
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_roleplay")


_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode.yaml")

_AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")

_LOREBOOKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lorebooks")

_PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")

_STYLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles")

# 卡 id 即用户层文件名（<id>.yaml）：正则白名单同时是路径穿越防线（越权字符
# 进不了文件名）。出厂根保留 card_* 前缀命名约定，id 空间两侧同一正则。

_CARD_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# 玩法包 id 白名单：与装载侧 rp_packs._PACK_ID_RE 同口径（大小写字母数字下划线
# 1-64）——清点面只展示装载面进得去的 id（内联同款正则，种子自包含不 import
# 管道步模块）。

_PACK_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# 模式卡扁平字段词表（读/写/导出共用）：角色卡标准字段词汇的属主单源在
# card_io（STR_FIELDS/LIST_FIELDS/DICT_FIELDS），本侧不自持词表副本，只做
# 目标侧派生 + 模式局部扩展（_card_vocab）：
# - 字符串字段 = card_io.STR_FIELDS 剔 creator/character_version 两例外（str 归一，
#   缺失补空串）；
# - 列表字段 = card_io.LIST_FIELDS + lorebook_ids/pack_ids（rp_packs 资产契约，
#   随读面透传供向导预选、随写面落盘防面板编辑丢失；非 list 一律 []）；
# - 透传字段 = 原样 .get 宽容（合法性由消费端把关）：creator 是 ST V3 作者
#   署名字段，走透传属有意设计——缺席卡 None 不落盘（既有 save 载荷零变化），
#   创作流草稿/入库卡携带时随读写保真（AI 署名强制载体，扩展波 B-Ⅰ）；
#   avatar/voice_ref/state_schema/cover/portrait 为模式扩展；theme 例外见
#   _card_from_data（非 dict 归 None）；
# - character_version 现状不入任何本侧表（与 card_io 的归属分歧按现状行为
#   保持）；覆盖关系机械断言见 _libs/mode_tests/test_roleplay_card_vocab_sso.py。

_PROVIDERS: dict[str, Any] = {}

_WARNED: set[str] = set()


logger = logging.getLogger("mode_roleplay")



def _set_provider(name: str, fn: Any) -> None:
    """注册内核读 provider（幂等，重复注入以后者为准）。"""
    _PROVIDERS[name] = fn
    _WARNED.discard(name)



def reset_providers() -> None:
    """清空全部 provider（单测隔离用）。"""
    _PROVIDERS.clear()
    _WARNED.clear()



async def _call_provider(name: str, **kwargs: Any) -> Any:
    """调用已注入 provider；未注入/调用失败时 warn 一次并返回空（读面降级）。"""
    fn = _PROVIDERS.get(name)
    if fn is None:
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning("provider %s 未注入（内核能力不可用），降级空数据", name)
        return []
    try:
        return await fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 读面降级：能力失败不崩 handler
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning("provider %s 调用失败（降级空数据）: %s", name, exc)
        return []



async def _resolve_workspace(session_id: str) -> str | None:
    """pipeline-state 行 workspace 解析（单点）：会话行存在且带 workspace 才有
    值，否则 None（调用方决定降级/报错语义）。"""
    rows = await _call_provider("pipeline-state")
    return next(
        (
            str(row["workspace"])
            for row in rows
            if isinstance(row, dict)
            and str(row.get("pipeline_id") or "") == session_id
            and row.get("workspace")
        ),
        None,
    )



@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """注入内核读 providers（调用时惰性取能力——on_load 时机能力握手未必完成）。"""

    async def _state_rows() -> Any:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    async def _messages(pipeline_id: str, limit: int | None = None) -> Any:
        params: dict[str, Any] = {"pipeline_id": pipeline_id}
        if limit is not None:
            params["limit"] = int(limit)
        handle = plugin.get_capability("service-registry")
        return await handle.call("messages.list", params)

    async def _invoke(payload: dict[str, Any]) -> Any:
        te = plugin.get_capability("tool-executor")
        caller = bind_capability_caller(te, "tool-executor")
        return await caller("tool-executor.invoke", payload)

    _set_provider("pipeline-state", _state_rows)
    _set_provider("messages", _messages)
    _set_provider("tool-executor", _invoke)


# ── 数据归一（pipeline-state 行 = 扁平点号键摘要，mode/task.* 经出口白名单）────────


_SCALE_AXIS_OPTIONS = (
    {"id": "explicit", "name": "直写", "description": "尺度全开，露骨内容直接成文"},
    {"id": "implication", "name": "暗示", "description": "点到为止：以气氛、感官与留白暗示，不直写"},
    {"id": "fade_to_black", "name": "转场黑幕", "description": "露骨段落在切入前转场带过（缺省档）"},
)

# 节奏轴三档（蛛网推进世界观换装）

_PACE_AXIS_OPTIONS = (
    {"id": "reasonable", "name": "合理推进", "description": "世界是活的：巧合、伏笔回收与突发事件推动剧情流动向前（缺省档）"},
    {"id": "butterfly", "name": "蝴蝶效应", "description": "因果共振：外部变化催化内心，微小决定连锁塑造格局"},
    {"id": "normal", "name": "普通推进", "description": "平实线性推进，不主动加戏、不引入额外波澜"},
)

# 代理权轴三态（DEM 玩家代理权）

_AGENCY_AXIS_OPTIONS = (
    {"id": "you_control", "name": "You-control", "description": "AI 只演 NPC 与世界，绝不代写你的行动与台词（缺省档）"},
    {"id": "director", "name": "Director", "description": "你的输入视为导演指令，AI 代演其戏剧化版本（含代写你的台词）"},
    {"id": "input_improver", "name": "Input Improver", "description": "AI 首段润色扩写你的输入，此后仍只演 NPC"},
)



_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "roleplay_panel.html"
)

# 出厂游戏页（Tier 1 第一宿主呈现页：HTML 路由 /page/game-host——无独立
# workspace tab（2026-10-14 单页化裁定：contributes.pages 不再声明 game_host 条目），
# 由面板玩法子页内嵌挂载 document 注入；引擎件挂载点占位随批 4-Ⅱ）

_GAME_HOST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """任意 JSON 对象 → HttpHandleResponse（body base64，内核约定）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }



_SANITIZE_INCLUDE_MARK = "/*@agentos-include:rp-sanitize*/"

_SANITIZE_SOURCE_RE = re.compile(
    r"rp-sanitize:single-source-begin \*/([\s\S]*?)/\* rp-sanitize:single-source-end"
)

_BRIDGE_INCLUDE_MARK = "/*@agentos-include:rp-bridge*/"

_BRIDGE_SOURCE_RE = re.compile(
    r"rp-bridge:single-source-begin \*/([\s\S]*?)/\* rp-bridge:single-source-end"
)

_PAGE_ASSEMBLIES = (
    (_SANITIZE_INCLUDE_MARK, _SANITIZE_SOURCE_RE, "rp-sanitize"),
    (_BRIDGE_INCLUDE_MARK, _BRIDGE_SOURCE_RE, "rp-bridge"),
)
# rp-core（RP 面板 v3.1 刀4「工作区玩法页全 webview 化」）：面板 rp-panel-core
# 纯函数块（rp_init 快照串组装含 `\,`/`\\` 转义契约、路线/词条寻址、profile
# 拼接）作为第三单源供给 webview 页内开演设置。**可选装配**：标记在场才注入
# ——前刀用户自包含页无此标记继续原样装配（兼容零变化）；声明了依赖而面板
# 单源块缺失 = 作者面事实，fail-closed 同既有两标记语义。

_CORE_INCLUDE_MARK = "/*@agentos-include:rp-core*/"

_CORE_SOURCE_RE = re.compile(
    r"rp-panel-core:single-source-begin \*/([\s\S]*?)/\* rp-panel-core:single-source-end"
)



def _game_routed(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """通道端点响应（HttpHandleResponse 信封；错误码随 payload 语义 400/409）。"""
    return {"success": True, "data": _json_response(payload, status)}



def _pack_tick_listing(data: dict[str, Any], pack_id: str) -> list[dict[str, Any]]:
    """pack 顶层 tick 声明 → 归一 steps（消费侧归一剔除面）。

    结构单源 card_lint.lint_tick（零 IO 形态面）；**when 键域唯一执法位在此**
    ——key 须在本包 variables 声明内（条件求值不经 Validator，fail-closed 整步
    剔除 + warning）；effects 键域不在此判（归 Validator adjudicate fail-closed，
    与 operations effects 同闸）。坏条目整步剔除不整组作废（ui.lineage 剔除
    回落同构）。"""
    raw = data.get("tick")
    if raw is None:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        logger.warning("[mode_roleplay] pack %s tick 声明形态非法，整组剔除", pack_id)
        return []
    for warning in lint_tick(raw):
        logger.warning("[mode_roleplay] pack %s %s", pack_id, warning)
    variables = data.get("variables")
    flat, _skipped = flatten_variables(variables) if isinstance(variables, dict) else ({}, [])
    items: list[dict[str, Any]] = []
    for entry in raw["steps"]:
        if not isinstance(entry, dict):
            continue
        effects = entry.get("effects")
        if not isinstance(effects, dict) or not effects:
            continue
        norm_effects: dict[str, Any] = {}
        bad = False
        for key, spec in effects.items():
            if isinstance(spec, dict) and isinstance(spec.get("cycle"), list) and spec["cycle"]:
                norm_effects[str(key)] = {"cycle": list(spec["cycle"])}
            elif isinstance(spec, str) and spec.strip():
                norm_effects[str(key)] = spec
            else:
                bad = True  # 效果组是一个结算单元：坏值整步剔除（无部分声明）
                break
        if bad:
            continue
        when = entry.get("when")
        norm_when: dict[str, Any] | None = None
        if when is not None:
            if not isinstance(when, dict):
                continue
            cond_key = when.get("key")
            cond_op = when.get("op")
            if (
                not isinstance(cond_key, str)
                or not cond_key.strip()
                or cond_op not in TICK_WHEN_OPS
                or "value" not in when
                or cond_key not in flat  # when 键域 fail-closed（本函数为唯一执法位）
            ):
                continue
            norm_when = {"key": cond_key, "op": cond_op, "value": when.get("value")}
        item: dict[str, Any] = {"effects": norm_effects}
        if norm_when is not None:
            item["when"] = norm_when
        items.append(item)
    return items


def _is_game_index(value: Any) -> bool:
    """快照序号薄 sanity：非负整数（bool 是 int 子类，显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
