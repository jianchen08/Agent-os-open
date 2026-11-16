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
import importlib.util
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_roleplay")

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

# bootstrap 后导入面（_libs 单源三件套同款惰性位置）
from _libs.card_contract import flatten_variables  # noqa: E402
from _libs.mode_server_base import (  # noqa: E402
    html_file_response,
    html_text_response,
    load_mode_profile,
    register_profile_services,
)

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
def _card_vocab() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """card_io 属主词汇 → 模式卡三表（字符串归一表/列表表/透传表）。

    惰性导入 card_io：其顶层拉起 Pillow，而 Pillow 非本插件运行时依赖——
    插件装载不得被 PNG 能力绑架（与 _write_user_card 的 atomic_io 惰性导入同款）。
    """
    from card_io import DICT_FIELDS, LIST_FIELDS, STR_FIELDS

    str_fields = tuple(f for f in STR_FIELDS if f not in ("creator", "character_version"))
    list_fields = (*LIST_FIELDS, "lorebook_ids", "pack_ids")
    passthrough_fields = ("creator", "avatar", "voice_ref", "state_schema", "cover", "portrait", *DICT_FIELDS)
    return str_fields, list_fields, passthrough_fields

# 创作卡类别（扩展波 B-Ⅰ）：category=creation 的 agent 卡是创作工作台的执行者
# （card_author），不是扮演卡——卡库读面过滤，不用工作台 = 卡库现状零变化。
_CREATION_CATEGORY = "creation"

# 内容安全扫描单源在本插件 card_lint（扩展波 B-Ⅱ 收编：suspicious 注入形状 +
# 高危 HTML 形态两模式表迁址，卡导入/世界书导入/创作 lint/出厂内容提案四消费
# 面同源禁第四套）；本文件 _scan_card_danger/_scan_card_html_danger 为薄委托。

def load_profile(path: str = _PROFILE_PATH) -> dict[str, Any]:
    """读取包内 mode.yaml（缺省本包种子；fail-closed 实现在 _libs 基面）。"""
    return load_mode_profile(path)


# 模式元数据服务（manifest capabilities.services；handler 别名供契约测试直调）
mode_describe, mode_get_profile = register_profile_services(
    plugin,
    _PROFILE_PATH,
    "返回角色扮演模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
    "返回角色扮演模式包内 mode.yaml 解析后的内容",
)


# ── 卡面质检服务（provides namespace ``card``，T6c；消费方 agent_config_load /
#    mode_evolution 经 requires_services + capability handle 直连，禁模块级 import）──
#
# 提供方形态跟 skill_manager 先例（服务方法 = sidecar 同名工具，内核 McpBridge
# 经 tool_prefix 转发 card.<method>）；wire 契约 = 工具函数裸 dict 返回（无信封）。
# 逐方法为 card_lint 纯函数的 1:1 薄透出（零策略加工，消费面语义与本插件直调
# 完全同源），入参出参全 JSON 可序列化。

from card_lint import (  # noqa: E402 —— 声明面结构/内容安检单源（本插件内部模块）
    OPERATION_ID_RE,
    TICK_WHEN_OPS,
    lint_actions,
    lint_behaviors,
    lint_card_extensions,
    lint_engine,
    lint_init_ui,
    lint_operations,
    lint_tick,
    lint_ui,
    lint_variables,
    scan_html_danger,
    scan_html_danger_texts,
    scan_suspicious,
    walk_str_values,
)


@plugin.tool(
    name="card.lint_decl",
    schema={
        "type": "object",
        "properties": {
            "variables": {"type": ["object", "null"]},
            "behaviors": {"type": ["object", "null"]},
            "engine": {"type": ["object", "null"]},
        },
    },
    description="卡声明结构 lint（variables/behaviors/engine；单源 card_lint）",
)
async def card_lint_decl(
    variables: dict[str, Any] | None = None,
    behaviors: dict[str, Any] | None = None,
    engine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合并产物声明面 warnings（lint_variables + lint_behaviors + lint_engine）。"""
    warnings: list[str] = []
    if variables is not None:
        warnings.extend(lint_variables(variables))
    if behaviors is not None:
        warnings.extend(lint_behaviors(behaviors))
    if engine is not None:
        warnings.extend(lint_engine(engine))
    return {"warnings": warnings}


@plugin.tool(
    name="card.lint_actions",
    schema={
        "type": "object",
        "properties": {"actions": {"type": ["array", "null"]}},
    },
    description="actions target 面校验+归一（缺省补 engine、非法剔除）",
)
async def card_lint_actions(actions: list[Any] | None = None) -> dict[str, Any]:
    """actions 规范化列表 + warnings（装载侧消费，归一化产物写 context.rp_actions）。"""
    valid, warnings = lint_actions(actions)
    return {"actions": valid, "warnings": warnings}


@plugin.tool(
    name="card.lint_pack_ui",
    schema={
        "type": "object",
        "properties": {
            "ui": {"type": ["object", "null"]},
            "init_ui": {"type": ["object", "null"]},
        },
    },
    description="pack 呈现面 ui/init_ui 结构 lint",
)
async def card_lint_pack_ui(
    ui: dict[str, Any] | None = None,
    init_ui: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """pack 呈现声明 warnings（lint_ui + lint_init_ui）。"""
    return {"warnings": [*lint_ui(ui), *lint_init_ui(init_ui)]}


@plugin.tool(
    name="card.lint_extensions",
    schema={
        "type": "object",
        "properties": {"extensions": {"type": ["object", "null"]}},
    },
    description="卡 extensions 四面结构 lint 汇总",
)
async def card_lint_extensions_service(
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """卡/包声明结构 warnings 汇总（content_audit 消费）。"""
    return {"warnings": lint_card_extensions(extensions)}


@plugin.tool(
    name="card.scan_suspicious",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}, "subject": {"type": "string"}},
        "required": ["text"],
    },
    description="可疑指令模式扫描（大小写不敏感子串）",
)
async def card_scan_suspicious(text: str, subject: str = "角色设定") -> dict[str, Any]:
    """可疑注入形状单条可读标注（命中才非空）。"""
    return {"warnings": scan_suspicious(text, subject)}


@plugin.tool(
    name="card.scan_html_card",
    schema={
        "type": "object",
        "properties": {"card": {"type": "object"}},
        "required": ["card"],
    },
    description="卡形态高危 HTML 扫描（prompt 载体全字段）",
)
async def card_scan_html_card(card: dict[str, Any]) -> dict[str, Any]:
    """卡各文本值逐形态独立标注。"""
    return {"warnings": scan_html_danger(card)}


@plugin.tool(
    name="card.scan_html_texts",
    schema={
        "type": "object",
        "properties": {"texts": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"}},
        "required": ["texts"],
    },
    description="高危 HTML 形态扫描（文本集入参）",
)
async def card_scan_html_texts(texts: list[str], subject: str = "角色卡内容") -> dict[str, Any]:
    """文本集逐形态独立标注（同形态多文本命中合并）。"""
    return {"warnings": scan_html_danger_texts(texts, subject)}


@plugin.tool(
    name="card.texts_of_tree",
    schema={
        "type": "object",
        "properties": {"data": {"type": "object"}},
        "required": ["data"],
    },
    description="任意嵌套结构收集全部字符串叶子",
)
async def card_texts_of_tree(data: dict[str, Any]) -> dict[str, Any]:
    """嵌套声明数据全部字符串叶子（玩法包内容扫描取料）。"""
    return {"texts": walk_str_values(data)}


# ── 内核读数桥（monitoring kernel_reads 同构：provider 注册 + 未就绪降级空）────────

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

def _session_pack_ids(row: dict[str, Any], cards: list[dict[str, Any]]) -> list[str]:
    """会话行 → 玩法归属 id 列表（行内可得什么透什么，都不可得 = 空数组如实）。

    两源：行 agent_id（mode_roleplay/<card_id> 卡键）→ 卡 pack_ids 归一；行
    wizard_pack_ids 持久键（mode.session 透传键若随行出口——非出口保证，键在
    才并入）兼容 list 与逗号串两形态。卡在前、向导增量在后，去重保序。
    """
    ids: list[str] = []
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    if agent_id.startswith(prefix):
        card = next((c for c in cards if c["id"] == agent_id[len(prefix):]), None)
        if card:
            ids.extend(str(p) for p in card.get("pack_ids") or [])
    wizard = row.get("wizard_pack_ids")
    wizard_ids = (
        [str(p) for p in wizard] if isinstance(wizard, list) else (
            [seg.strip() for seg in str(wizard).split(",")] if isinstance(wizard, str) else []
        )
    )
    for pack_id in wizard_ids:
        if pack_id and pack_id not in ids:
            ids.append(pack_id)
    return ids


def _session_state_summary(row: dict[str, Any]) -> dict[str, Any] | None:
    """会话行持久 state 键 → 摘要 dict（键不在 = None，调用方键缺席——禁止发明）。

    行内出口键候选：context.character_state_text（state_marker_parse 每轮回写的
    状态渲染文本，context.* 前缀随插件出口声明随行）。game_host_delta 等其余
    持久键不进摘要（delta 是转发面不是状态面）。
    """
    state_text = row.get("context.character_state_text")
    if isinstance(state_text, str) and state_text.strip():
        return {"character_state_text": state_text}
    return None


def _norm_sessions(rows: Any, cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式会话行（mode 键过滤 + 面板字段裁剪）。

    RP 面板 v3.1 刀1 行增：pack_ids（玩法归属，?pack= 过滤与「继续上局」按包
    过滤依据）+ state_summary（持久 state 摘要 dict；行内键不在 = 键缺席）。
    """
    resolved_cards = _load_cards() if cards is None else cards
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "roleplay":
            continue
        item = {
            "pipeline_id": str(row.get("pipeline_id") or ""),
            "thread_id": str(row.get("thread_id") or ""),
            "agent_id": str(row.get("agent_id") or ""),
            "run_status": str(row.get("run_status") or ""),
            "goal": str(row.get("task.goal") or row.get("input") or ""),
            "task_status": str(row.get("task.status") or ""),
            # task.created_at：任务域出生键（tasks http_api 同源约定）；缺席如实空
            "created_at": str(row.get("task.created_at") or ""),
            "message_count": row.get("message_count") or 0,
            "pack_ids": _session_pack_ids(row, resolved_cards),
        }
        summary = _session_state_summary(row)
        if summary is not None:
            item["state_summary"] = summary
        items.append(item)
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板对话流行（content_preview = 读时重建全文）。"""
    items: list[dict[str, Any]] = []
    for msg in rows if isinstance(rows, list) else []:
        if not isinstance(msg, dict):
            continue
        items.append(
            {
                "role": str(msg.get("role") or ""),
                "content": str(msg.get("content_preview") or ""),
                "status": str(msg.get("status") or ""),
                "created_at": str(msg.get("created_at") or ""),
            }
        )
    return items


# ── 物料读取（卡/世界书 = 出厂根 ∪ 用户层双根合并）───────────────────────────────

def _user_config_root() -> str | None:
    """用户配置层根（user_space 共享裸模块；裸名导入放函数内维持种子自包含）。

    bootstrap_plugin 已把 plugins/shared 根推上 sys.path（用户空间播种副本由
    SDK 位置锚回退兜底，见模块头注），函数内裸名导入即达；模块顶层不新增
    共享根导入前提。极端环境解析失败 → None，调用方按「无用户空间」定语义
    （读面降级空，写面如实报错不静默假成功）。
    """
    from _root_api import user_config_dir

    resolved = user_config_dir()
    return str(resolved) if resolved is not None else None


def _user_cards_dir() -> str | None:
    """用户层角色卡目录：<user_config_dir>/agents/mode_roleplay（镜像包内布局）。"""
    root = _user_config_root()
    return os.path.join(root, "agents", "mode_roleplay") if root else None


def _user_lorebooks_dir() -> str | None:
    """用户层世界书目录：<user_config_dir>/lorebooks（镜像包内布局）。"""
    root = _user_config_root()
    return os.path.join(root, "lorebooks") if root else None


def _personas_path() -> str | None:
    """persona 档案文件：<user_config_dir>/persona/roleplay_personas.json。"""
    root = _user_config_root()
    return os.path.join(root, "persona", "roleplay_personas.json") if root else None


def _card_from_data(data: dict[str, Any], card_id: str, origin: str) -> dict[str, Any]:
    """卡源数据 → 模式卡扁平 dict（词表归一 + 新字段宽容透传，词表见模块头常量）。

    name 缺失回落卡 id（画廊不可无名）；lorebook_ids/voice_ref/state_schema/
    cover/portrait/extensions 原样透传（.get 宽容，合法性由消费端把关）。
    """
    str_fields, list_fields, passthrough_fields = _card_vocab()
    card: dict[str, Any] = {"id": card_id, "origin": origin}
    for field in str_fields:
        card[field] = str(data.get(field) or "")
    for field in list_fields:
        value = data.get(field)
        card[field] = [str(item) for item in value] if isinstance(value, list) else []
    theme = data.get("theme")
    card["theme"] = theme if isinstance(theme, dict) else None
    for field in passthrough_fields:
        card[field] = data.get(field)
    if not card["name"]:
        card["name"] = card_id
    return card


def _scan_cards_dir(directory: str | None, origin: str) -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (card_id, 源数据) 有序列表；损坏/非 dict 文件跳过不崩面板。

    出厂根按包内约定只认 card_*.yaml；用户层受理面 = save/import 写入的 id
    命名空间（stem 须过 _CARD_ID_RE）——import 派生 id（如 seraphina）不带
    card_ 前缀，按前缀过滤会静默隐身，故用户层按 stem 校验全量扫描。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for entry in sorted(Path(directory).iterdir()):
        fname = entry.name
        if not fname.endswith(".yaml"):
            continue
        card_id = fname[: -len(".yaml")]
        if origin == "builtin":
            if not fname.startswith("card_"):
                continue
        elif not _CARD_ID_RE.fullmatch(card_id):
            continue
        try:
            with open(os.path.join(directory, fname), encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            found.append((card_id, data))
    return found


def _load_cards(agents_dir: str = _AGENTS_DIR, user_dir: str | None = None) -> list[dict[str, Any]]:
    """双根合并列卡：出厂（包内 agents/card_*.yaml，origin="builtin"）∪ 用户层
    （<user_config_dir>/agents/mode_roleplay，origin="user"）。

    用户层同 id 接管出厂卡（文件级所有权转移，与 user_space 覆盖语义同构；
    出厂件永不被写）。user_dir=None（缺省）经 user_space 现场解析，用户空间
    不可得 → 只出厂根（读面降级）；显式传路径供测试注入隔离。损坏文件跳过。
    """
    resolved_user_dir = _user_cards_dir() if user_dir is None else user_dir
    merged: dict[str, dict[str, Any]] = {}
    for origin, directory in (("builtin", agents_dir), ("user", resolved_user_dir)):
        for card_id, data in _scan_cards_dir(directory, origin):
            if str(data.get("category") or "") == _CREATION_CATEGORY:
                continue  # 创作 agent 卡不进扮演卡库（工作台 mode.session 直附身）
            merged[card_id] = _card_from_data(data, card_id, origin)  # 同 id 后根接管
    return list(merged.values())


def _entry_from_st(entry: dict[str, Any]) -> dict[str, Any]:
    """ST World Info 条目 → 模式书条目（字段映射单一真值，缺省补默认）。

    keys / secondary_keys / content / enable→enabled / insertion_order /
    constant / position / case_sensitive / comment。enabled 存储键优先、
    ST 导出键 enable 次之（自有 save/import 落盘形态与 ST 导出双兼容）。

    批 1c 字面扩展（匹配语义见 lorebook.py）：源键回落归一 key→keys /
    keysecondary→secondary_keys / disable→enabled 取反 / order→insertion_order
    / caseSensitive→case_sensitive；批 1c 新增字面字段（keysecondary/
    selectiveLogic/disable/order/probability/useProbability/caseSensitive/
    matchWholeWords）随条目透传保留——存在才写，旧形态条目存储形状零增。
    """
    if "enabled" in entry:
        enabled = bool(entry["enabled"])
    elif "enable" in entry:
        enabled = bool(entry["enable"])
    elif "disable" in entry:
        enabled = not bool(entry["disable"])
    else:
        enabled = True
    normalized: dict[str, Any] = {
        "keys": [str(k) for k in (entry.get("keys") or entry.get("key") or [])],
        "secondary_keys": [
            str(k) for k in (entry.get("secondary_keys") or entry.get("keysecondary") or [])
        ],
        "content": str(entry.get("content") or ""),
        "enabled": enabled,
        "insertion_order": entry.get("insertion_order", entry.get("order", 100)),
        "constant": bool(entry.get("constant", False)),
        "position": str(entry.get("position") or "before_char"),
        "case_sensitive": bool(entry.get("case_sensitive", entry.get("caseSensitive", False))),
        "comment": str(entry.get("comment") or ""),
    }
    for field in (
        "keysecondary",
        "selectiveLogic",
        "disable",
        "order",
        "probability",
        "useProbability",
        "caseSensitive",
        "matchWholeWords",
    ):
        if field in entry:
            normalized[field] = entry[field]
    return normalized


def _book_from_data(data: dict[str, Any], book_id: str, origin: str) -> dict[str, Any]:
    """书源数据 → 面板书 dict（条目经 _entry_from_st 归一 + origin 标来源）。

    name 缺失回落书 id；entries 缺席 = 空表（空书合法，可在面板编辑后使用）。
    """
    entries = [
        _entry_from_st(entry)
        for entry in (data.get("entries") or [])
        if isinstance(entry, dict)
    ]
    return {
        "id": book_id,
        "origin": origin,
        "name": str(data.get("name") or book_id),
        "description": str(data.get("description") or ""),
        "scan_depth": data.get("scan_depth", 4),
        "token_budget": data.get("token_budget", 512),
        "entries": entries,
    }


def _scan_books_dir(directory: str | None, origin: str) -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (book_id, 源数据) 有序列表；损坏/非 dict 文件跳过不崩面板。

    用户层受理面 = save/import 写入的 id 命名空间（stem 须过 _CARD_ID_RE，
    与卡同款路径安全闸）；出厂根全量 .yaml（包内物料随包命名）。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for path in sorted(Path(directory).glob("*.yaml")):
        book_id = path.stem
        if origin != "builtin" and not _CARD_ID_RE.fullmatch(book_id):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            found.append((book_id, data))
    return found


def _load_lorebooks(lorebooks_dir: str = _LOREBOOKS_DIR, user_dir: str | None = None) -> list[dict[str, Any]]:
    """双根合并列书：出厂（包内 lorebooks/*.yaml，origin="builtin"）∪ 用户层
    <user_config_dir>/lorebooks（origin="user"）。

    用户层同 id 接管出厂书（与卡库双根/文件级所有权转移同构，出厂件永不被
    写）。user_dir=None（缺省）经 user_space 现场解析，用户空间不可得 → 只
    出厂根（读面降级）；显式传路径供测试注入隔离。损坏文件跳过。
    """
    resolved_user_dir = _user_lorebooks_dir() if user_dir is None else user_dir
    merged: dict[str, dict[str, Any]] = {}
    for origin, directory in (("builtin", lorebooks_dir), ("user", resolved_user_dir)):
        for book_id, data in _scan_books_dir(directory, origin):
            merged[book_id] = _book_from_data(data, book_id, origin)  # 同 id 后根接管
    return list(merged.values())


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
    from card_lint import PACK_LINEAGES, lint_ui

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
    from card_lint import lint_init_ui

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


def _user_combos_dir() -> str | None:
    """用户层 combos 目录：<user_config_dir>/rp_combos（rp_drafts 同区双根风格）。"""
    root = _user_config_root()
    return os.path.join(root, "rp_combos") if root else None


def _combos_raw() -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (combo_id, 源数据) 有序列表；损坏/非 dict 文件跳过 + warning。"""
    directory = _user_combos_dir()
    if not directory or not os.path.isdir(directory):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("combo 文件损坏，跳过: %s (%s)", path.name, exc)
            continue
        if isinstance(data, dict):
            found.append((path.stem, data))
    return found


def _combo_from_data(data: dict[str, Any], combo_id: str) -> dict[str, Any]:
    """combo 源数据 → 面板载荷（词表归一：列表字段非 list 一律空表，id/origin
    服务端语义键不信任存储值）。"""
    combo: dict[str, Any] = {
        "id": combo_id,
        "origin": "user",
        "name": str(data.get("name") or combo_id),
        "card_id": str(data.get("card_id") or ""),
        "route_id": str(data.get("route_id") or ""),
        "created_at": str(data.get("created_at") or ""),
    }
    for field in ("lorebook_ids", "trait_ids", "pack_ids"):
        value = data.get(field)
        combo[field] = [str(item) for item in value] if isinstance(value, list) else []
    extra = data.get("profile_extra")
    if isinstance(extra, str) and extra.strip():
        combo["profile_extra"] = extra  # 可选承载：键缺席式（旧件/未填零新增键）
    return combo


def _load_combos() -> list[dict[str, Any]]:
    """combos 读面（GET data/packs 一并返回，面板少一枪）；id 字典序稳定。"""
    return [_combo_from_data(data, combo_id) for combo_id, data in _combos_raw()]


def _combo_save_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/combos/save：{combo} 嵌套与平铺双形态宽容（卡 save 同款）。

    name/card_id 必填，card_id 须在卡库（fail-closed——组合挂在未知卡上不可
    开演）；id 缺省按 name 派生避让（卡 id 派生器复用），显式 id 过白名单闸；
    同 id 覆盖（用户件），created_at 首存定格、覆盖保留；profile_extra 可选
    透传（自由文本初始状态段，空值键缺席只增不破）。
    """
    combo = body["combo"] if isinstance(body.get("combo"), dict) else body
    name = str(combo.get("name") or "").strip()
    card_id = str(combo.get("card_id") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    if not card_id:
        return {"error": "缺少 card_id"}
    if not any(c["id"] == card_id for c in _load_cards()):
        return {"error": f"未知角色卡: {card_id}"}
    explicit_id = str(combo.get("id") or body.get("combo_id") or "").strip()
    if explicit_id:
        if not _CARD_ID_RE.fullmatch(explicit_id):
            return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {explicit_id}"}
        combo_id = explicit_id
    else:
        combo_id = _derive_card_id(name, {cid for cid, _ in _combos_raw()})
    directory = _user_combos_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），combo 无处落盘"}
    created_at = datetime.now(UTC).isoformat()
    for existing_id, data in _combos_raw():
        if existing_id == combo_id:
            created_at = str(data.get("created_at") or created_at)
            break
    lorebook_ids = combo.get("lorebook_ids")
    trait_ids = combo.get("trait_ids")
    pack_ids = combo.get("pack_ids")
    # profile_extra（可选透传，刀2：面板组合③自由文本初始状态段的承载字段）——
    # 只增不破：空值键缺席，旧读面/旧件零感知。
    profile_extra = str(combo.get("profile_extra") or "").strip()
    payload: dict[str, Any] = {
        "id": combo_id,
        "name": name,
        "card_id": card_id,
        "lorebook_ids": [str(i) for i in lorebook_ids] if isinstance(lorebook_ids, list) else [],
        "route_id": str(combo.get("route_id") or ""),
        "trait_ids": [str(i) for i in trait_ids] if isinstance(trait_ids, list) else [],
        "pack_ids": [str(i) for i in pack_ids] if isinstance(pack_ids, list) else [],
        "created_at": created_at,
        "origin": "user",
    }
    if profile_extra:
        payload["profile_extra"] = profile_extra
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{combo_id}.json"),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return {"combo_id": combo_id, "created_at": created_at}


def _combo_delete_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/combos/delete {id}：仅用户件（combos 本就全用户层），不存在如实报错。"""
    combo_id = str(body.get("id") or body.get("combo_id") or "").strip()
    if not combo_id:
        return {"error": "缺少 id"}
    if not _CARD_ID_RE.fullmatch(combo_id):
        return {"error": f"非法 id: {combo_id}"}
    directory = _user_combos_dir()
    path = os.path.join(directory, f"{combo_id}.json") if directory else None
    if path is not None and os.path.isfile(path):
        os.remove(path)
        return {"combo_id": combo_id, "deleted": True}
    return {"error": f"combo 不存在: {combo_id}"}


# ── 沉淀回路（缺口批次三：§7.4 游玩流水→统计→候选→审核闸→固化）───────────────────
#
# 输入 = 审计事件流水（rp_state_audit 单源写面产物，事件四件套 actor/action/
# target/summary 批次二已落）；统计判据 = 可枚举性（§7.4）：命名动作（行来源
# source=action）对同状态键同向同幅的转移重复出现才聚为候选规则（标准
# behaviors 形态 trigger/effect）；幅度漂移/被拒行/作弊旁路/既有 behavior 行/
# op 动词簿记行不成例——非枚举文本永久留 LLM。固化 = 人工审核闸（POST promote
# 显式调用才写，本批不做自动沉淀）：候选规则体写入用户层玩法包 behaviors（双根
# 装载同 id 接管即生效，消费走既有求值器 state_marker_parse 4a 通道零 LLM），
# 条目带来源标记（source=precipitated + precipitated_at + precipitated_from）
# 可单独识别回退；作者声明 behavior 同 id = 409 不覆写作者面，沉淀条目自身
# 幂等更新。

_PRECIP_CANDIDATES_ROUTE = "/ext/mode_roleplay/data/precipitation/candidates"
_PRECIP_PROMOTE_ROUTE = "/ext/mode_roleplay/data/precipitation/promote"

_PRECIP_SOURCE = "precipitated"
# 复盘统计读面窗口上限（recent_events 复用单源读面；会话游玩流水远小于此，
# 上限仅内存护栏——语义即全量史）。
_PRECIP_STAT_WINDOW = 10000
# op 动词映射动作名（事件 action 面的簿记动词）：这些是 op 派生词不是声明动作
# id，无「动作→状态」映射可言，不成例。
_PRECIP_BOOKKEEPING_ACTIONS = {"increase", "decrease", "set", "observe"}


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
    from _root_api import recent_events  # noqa: PLC0415（共享根裸名，惰性同 import 面）

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


def _llm_yaml_candidates() -> list[str]:
    """llm.yaml 候选路径（用户空间优先 → 项目根回落）；测试注入单点。"""
    candidates = []
    config_root = _user_config_root()
    if config_root:
        candidates.append(os.path.join(config_root, "plugins", "llm", "llm.yaml"))
    candidates.append(
        os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "..",
                "..",
                "config",
                "plugins",
                "llm",
                "llm.yaml",
            )
        )
    )
    return candidates


def _llm_default_chat_model() -> str:
    """defaults.chat 读面（系统模型注册真值 llm.yaml）：用户空间优先 → 项目根回落
    （hindsight _load_llm_yaml 同款双根）。缺失/损坏 = 空串（调用方 502 显式，
    不猜默认模型）。"""
    for path in _llm_yaml_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("[mode_roleplay] llm.yaml 读取失败 | path=%s | err=%s", path, exc)
            return ""
        if not isinstance(data, dict):
            continue
        defaults = data.get("defaults")
        model = defaults.get("chat") if isinstance(defaults, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""


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


def _scan_styles_dir(directory: str | None) -> list[dict[str, Any]]:
    """文风档目录扫描 → 展示条目列表（按文件名有序；损坏/缺字段文件跳过）。

    条目 = {id, name, description}，description 取特征清单首行（档位一眼可辨，
    全清单属注入面=后续刀）。fail-soft：目录缺失 = 空列表（文风轴空选项，
    向导照常开演=无文风注入常态），不炸候选集端点。
    """
    found: list[dict[str, Any]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        style_id = str(data.get("id") or "")
        name = str(data.get("name") or "")
        traits = data.get("traits")
        if not style_id or not name or not isinstance(traits, list):
            continue
        first_trait = traits[0] if traits and isinstance(traits[0], str) else ""
        found.append({"id": style_id, "name": name, "description": str(first_trait)})
    return found


def _wizard_axes_listing() -> list[dict[str, Any]]:
    """GET /data/wizard/axes 载荷：四轴候选集（文风轴动态读 styles/，其余三轴
    静态清单）。轴 id = rp_axes 快照键词表，轴序即面板渲染序。"""
    return [
        {"id": "style", "label": "文风", "options": _scan_styles_dir(_STYLES_DIR)},
        {"id": "scale", "label": "内容尺度", "options": list(_SCALE_AXIS_OPTIONS)},
        {"id": "pace", "label": "剧情节奏", "options": list(_PACE_AXIS_OPTIONS)},
        {"id": "agency", "label": "你的代理权", "options": list(_AGENCY_AXIS_OPTIONS)},
    ]


# ── 卡/世界书/persona 写面（用户空间落盘；出厂件只读，接管=用户层同 id 覆盖）────────

def _write_user_card(card: dict[str, Any]) -> dict[str, Any]:
    """模式卡 dict → 用户层 yaml（<user_cards_dir>/<id>.yaml）。

    载荷 = 已知字段并集宽容写入（None 不落）；id 随文件名不入载荷（读面以
    stem 为 id，两处写必然漂移）。写经 atomic_io（同目录 tmp + os.replace）
    防半写。出厂同 id 覆盖即接管语义（读面用户赢），不产出错误。
    返回 {card_id}；用户空间不可得 → {error}（写面不静默降级）。
    """
    directory = _user_cards_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），用户卡无处落盘"}
    str_fields, list_fields, passthrough_fields = _card_vocab()
    payload = {
        field: card[field]
        for field in (*str_fields, *list_fields, "theme", *passthrough_fields)
        if card.get(field) is not None
    }
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{card['id']}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"card_id": card["id"]}


def _card_id_from_filename(filename: str) -> str:
    """导入文件名 → 卡 id：去扩展名、小写、非法字符转 _、截 64；派生为空返空。"""
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")[:64].strip("_")


def _derive_card_id(name: str, taken: set[str]) -> str:
    """name → 合法卡 id（面板新建/出厂复制流不持 id，id 由服务端派生——面板契约）。

    非法字符转 _ 截 64；纯 CJK 等不可派生名以秒级时间戳兜底；与既有 id 冲突
    递增后缀避让（不静默覆盖他卡）。
    """
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:64].strip("_")
    if not slug:
        slug = "card_" + time.strftime("%Y%m%d_%H%M%S")
    candidate = slug
    suffix = 2
    while candidate in taken:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    return candidate


def _scan_card_danger(card: dict[str, Any]) -> list[str]:
    """内容安全简化闸：description+system_prompt 拼文扫描可疑指令模式。

    只标注不阻断（人工检查指引随 warnings 上行）；大小写不敏感子串匹配。
    模式表单源在本插件 card_lint（B-Ⅱ 收编），本函数为消费面薄委托。
    """
    from card_lint import scan_suspicious  # noqa: PLC0415（本插件单源，惰性同 import 面）

    haystack = str(card.get("description") or "") + str(card.get("system_prompt") or "")
    return scan_suspicious(haystack, "角色设定")


def _scan_card_html_danger(card: dict[str, Any]) -> list[str]:
    """高危 HTML 形态扫描（面板刀-Ⅱ）：description/personality/scenario/
    first_mes/alternate_greetings 及 extensions 各文本值，逐形态独立 warnings。

    只标注不清洗（卡资产原样落盘——与 suspicious 指令同待遇，作者知情）；
    模式表单源在本插件 card_lint（B-Ⅱ 收编），本函数为消费面薄委托。
    """
    from card_lint import scan_html_danger  # noqa: PLC0415（本插件单源，惰性同 import 面）

    return scan_html_danger(card)


def _save_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/save → 用户层落卡（{card} 嵌套与面板平铺双形态宽容）。

    id 来源优先级：card.id > body.card_id（面板键）> name 派生（新建流，面板
    契约「出厂卡副本 id 由服务端派生」；name 缺席如实拒绝——无名卡不入库）；
    显式 id 须过 _CARD_ID_RE（路径安全闸）。已存在出厂同 id → 覆盖写用户层
    =接管语义，不产出错误。
    """
    card = body["card"] if isinstance(body.get("card"), dict) else body
    card_id = str(card.get("id") or body.get("card_id") or "").strip()
    if not card_id:
        name = str(card.get("name") or "").strip()
        if not name:
            return {"error": "缺少 id/name（新建卡须带 name 以派生合法 id）"}
        card_id = _derive_card_id(name, {c["id"] for c in _load_cards()})
    if not _CARD_ID_RE.fullmatch(card_id):
        return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {card_id}"}
    saved = _write_user_card(_card_from_data(card, card_id, "user"))
    return saved


def _delete_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/delete：仅用户层可删。

    用户文件先行（接管副本删除即解除接管，出厂房仍在）；出厂卡拒绝（面板
    指引复制后修改）；都不存在如实报错。card_id 先过正则再拼路径（穿越防线）。
    """
    card_id = str(body.get("card_id") or "").strip()
    if not card_id:
        return {"error": "缺少 card_id"}
    if not _CARD_ID_RE.fullmatch(card_id):
        return {"error": f"非法 card_id: {card_id}"}
    directory = _user_cards_dir()
    user_path = os.path.join(directory, f"{card_id}.yaml") if directory else None
    if user_path is not None and os.path.isfile(user_path):
        os.remove(user_path)
        return {"card_id": card_id, "deleted": True}
    if any(c["id"] == card_id for c in _load_cards()):
        return {"error": "出厂卡不可删除，可复制后修改"}
    return {"error": f"卡不存在: {card_id}"}


def _import_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/import：ST V2/V3 卡文件 → 用户层落盘。

    字节经 base64 解码后交 card_io.load_card（PNG tEXt/JSON 双形态自动识别，
    平铺共享模块函数内裸名导入），data 字段平铺为模式卡（extensions 原样入
    extensions 键）。file_b64（规格键）/data_base64（面板键）双形态宽容。
    响应 {card_id, issues}（issues = card_io.validate_card 结构问题清单），
    命中可疑指令模式加 warnings（只标注不阻断）。解码/解析失败如实 error。
    """
    raw_b64 = str(body.get("file_b64") or body.get("data_base64") or "")
    filename = str(body.get("filename") or "")
    if not raw_b64 or not filename:
        return {"error": "缺少 file_b64/filename"}
    try:
        raw = base64.b64decode(raw_b64)
    except ValueError as exc:  # binascii.Error 是 ValueError 子类
        return {"error": f"file_b64 base64 解码失败: {exc}"}
    from card_io import load_card, validate_card
    from card_lint import lint_card_extensions

    try:
        envelope = load_card(raw)
    except ValueError as exc:
        return {"error": f"角色卡解析失败: {exc}"}
    issues = validate_card(envelope)
    card_id = _card_id_from_filename(filename)
    if not card_id:
        return {"error": f"无法从 filename 派生合法卡 id: {filename}"}
    raw_data = envelope.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    stored = _card_from_data(data, card_id, "user")
    saved = _write_user_card(stored)
    if "error" in saved:
        return saved
    response: dict[str, Any] = {"card_id": card_id, "issues": issues}
    # 声明面结构 lint（card_lint 单源，RP 批 2-Ⅱ 2d——与装载侧 agent_config_load
    # 同一套判定）：variables 类型面/enum 面 + behaviors trigger/effect 面 +
    # actions target 面的问题清单随 warnings 上行（只标注不阻断）；高危 HTML
    # 形态扫描（面板刀-Ⅱ）同通道合流（只标注不清洗）。
    warnings = (
        _scan_card_danger(stored)
        + _scan_card_html_danger(stored)
        + lint_card_extensions(stored.get("extensions"))
    )
    if warnings:
        response["warnings"] = warnings
    return response


def _export_envelope(card: dict[str, Any]) -> dict[str, Any]:
    """合并卡 → ST V2 信封（export 唯一构造点，JSON/PNG 两形态共用）。

    data = 卡扁平字段去 id/origin（信封层语义键不落 data），None 字段不落。
    """
    data = {k: v for k, v in card.items() if k not in ("id", "origin") and v is not None}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def _export_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/export：合并后卡 → ST V2 信封 → base64 载荷下载。

    format 缺省 "json"（既有契约零变化）；"png" 走 card_io.write_png_card
    （tEXt 手写 PNG，与 import 的 base64 载荷对称，content_base64 为面板
    data URI 下载别名）。其余取值参数错误（fail-closed，不静默回落 json）。
    """
    card_id = str(body.get("card_id") or "").strip()
    if not card_id:
        return {"error": "缺少 card_id"}
    fmt = str(body.get("format") or "json")
    if fmt not in ("json", "png"):
        return {"error": f"非法 format: {fmt}（可选 json/png）"}
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"error": f"未知角色卡: {card_id}"}
    envelope = _export_envelope(card)
    if fmt == "png":
        from card_io import write_png_card

        payload = base64.b64encode(write_png_card(envelope)).decode("ascii")
        return {"filename": f"{card_id}.png", "file_b64": payload, "content_base64": payload}
    payload = base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return {"filename": f"{card_id}.json", "file_b64": payload, "content_base64": payload}


def _write_user_lorebook(book: dict[str, Any]) -> dict[str, Any]:
    """世界书 dict → 用户层 yaml（<user_lorebooks_dir>/<id>.yaml）。

    载荷 = 归一书字段去 id/origin（id 随文件名不入载荷，读面以 stem 为 id，
    两处写必然漂移）。写经 atomic_io（同目录 tmp + os.replace）防半写。出厂
    同 id 覆盖即接管语义（读面用户赢），不产出错误。返回 {book_id}；用户空间
    不可得 → {error}（写面不静默降级）。
    """
    directory = _user_lorebooks_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），世界书无处落盘"}
    payload = {k: v for k, v in book.items() if k not in ("id", "origin")}
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{book['id']}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"book_id": book["id"]}


def _save_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/save → 用户层落书（{book} 嵌套与面板平铺双形态宽容）。

    id 来源优先级：book.id > body.book_id（面板键）> name 派生（新建流，复用
    卡 id 派生器——面板新建书不持 id 的契约同卡）；显式 id 须过 _CARD_ID_RE
    （路径安全闸）。出厂同 id 覆盖即接管语义（读面用户赢），不产出错误。
    """
    book = body["book"] if isinstance(body.get("book"), dict) else body
    book_id = str(book.get("id") or body.get("book_id") or "").strip()
    if not book_id:
        name = str(book.get("name") or "").strip()
        if not name:
            return {"error": "缺少 id/name（新建世界书须带 name 以派生合法 id）"}
        book_id = _derive_card_id(name, {b["id"] for b in _load_lorebooks()})
    if not _CARD_ID_RE.fullmatch(book_id):
        return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {book_id}"}
    return _write_user_lorebook(_book_from_data(book, book_id, "user"))


def _delete_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/delete：仅用户层可删。

    用户文件先行（接管副本删除即解除接管，出厂书仍在）；出厂书拒绝（面板
    指引复制后修改）；都不存在如实报错。book_id 先过正则再拼路径（穿越防线）。
    """
    book_id = str(body.get("book_id") or "").strip()
    if not book_id:
        return {"error": "缺少 book_id"}
    if not _CARD_ID_RE.fullmatch(book_id):
        return {"error": f"非法 book_id: {book_id}"}
    directory = _user_lorebooks_dir()
    user_path = os.path.join(directory, f"{book_id}.yaml") if directory else None
    if user_path is not None and os.path.isfile(user_path):
        os.remove(user_path)
        return {"book_id": book_id, "deleted": True}
    if any(b["id"] == book_id for b in _load_lorebooks()):
        return {"error": "出厂世界书不可删除，可复制后修改"}
    return {"error": f"世界书不存在: {book_id}"}


def _st_world_info_entries(data: Any) -> list[dict[str, Any]]:
    """ST World Info 导出 → 条目 list（数字键对象/数组双形态兼容，同源序保持）。

    ST 导出为数字键对象（{"entries": {"0": {...}}}），按数字键序转 list；
    数组形态原样；顶层裸数组 = 无信封直接是条目集；其余形态 = 空表（空书合法）。
    """
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    if isinstance(entries, dict):
        items = [(key, value) for key, value in entries.items() if isinstance(value, dict)]
        try:
            items.sort(key=lambda pair: int(pair[0]))
        except ValueError:
            pass  # 非数字键形态：保持 JSON 原序
        return [value for _, value in items]
    return []


def _import_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/import：ST World Info JSON → 用户层落书。

    file_b64（规格键）/data_base64（面板键）双形态宽容；条目形态兼容见
    _st_world_info_entries，字段映射见 _entry_from_st；id 由 filename 派生
    （卡导入同款规范化），name/description 取导出顶层（缺省回落 id/空串）。
    响应 {book_id, warnings}（warnings = 条目拼文可疑指令模式清单，只标注不
    阻断）；解码/解析失败如实 error 不落盘。
    """
    raw_b64 = str(body.get("file_b64") or body.get("data_base64") or "")
    filename = str(body.get("filename") or "")
    if not raw_b64 or not filename:
        return {"error": "缺少 file_b64/filename"}
    try:
        raw = base64.b64decode(raw_b64)
    except ValueError as exc:  # binascii.Error 是 ValueError 子类
        return {"error": f"file_b64 base64 解码失败: {exc}"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"error": f"世界书 JSON 解析失败: {exc}"}
    book_id = _card_id_from_filename(filename)
    if not book_id:
        return {"error": f"无法从 filename 派生合法书 id: {filename}"}
    source = data if isinstance(data, dict) else {}
    entries = _st_world_info_entries(data)
    stored = _book_from_data(
        {"name": source.get("name"), "description": source.get("description"), "entries": entries},
        book_id,
        "user",
    )
    saved = _write_user_lorebook(stored)
    if "error" in saved:
        return saved
    from card_lint import scan_suspicious  # noqa: PLC0415（模式表单源，B-Ⅱ 收编）

    haystack = "\n".join(str(entry.get("content") or "") for entry in entries)
    return {"book_id": book_id, "warnings": scan_suspicious(haystack, "世界书内容")}


def _personas_raw() -> list[dict[str, Any]]:
    """persona 档案原样读（存储形态 {personas: [{name, description, avatar}]}）。

    缺文件 → 空（首次使用）；损坏 → 空并 warn（卡读取同款降级：不崩面板，
    下一次 save 以重写自愈）。
    """
    path = _personas_path()
    if path is None or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("persona 档案损坏，降级空列表（下次保存自愈）: %s (%s)", path, exc)
        return []
    items = data.get("personas") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _personas_load() -> list[dict[str, Any]]:
    """persona 档案读面：面板以 id 选择/删除，name 即唯一键 → id 与 name 同值。"""
    return [
        {
            "id": str(item.get("name") or ""),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "avatar": item.get("avatar"),
        }
        for item in _personas_raw()
        if str(item.get("name") or "").strip()
    ]


def _write_personas(path: str, personas: list[dict[str, Any]]) -> None:
    """persona 档案原子写（atomic_io：同目录 tmp + os.replace，防半写/截断）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(path, json.dumps({"personas": personas}, ensure_ascii=False, indent=2))


def _persona_save_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/personas/save：name 唯一键 upsert（已存在即更新，保持原位）。"""
    name = str(body.get("name") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    path = _personas_path()
    if path is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），persona 无处落盘"}
    entry: dict[str, Any] = {"name": name, "description": str(body.get("description") or "")}
    if body.get("avatar") is not None:
        entry["avatar"] = body.get("avatar")
    personas = _personas_raw()
    for index, item in enumerate(personas):
        if str(item.get("name") or "") == name:
            personas[index] = entry
            break
    else:
        personas.append(entry)
    _write_personas(path, personas)
    return {"name": name, "id": name}


def _persona_delete_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/personas/delete：persona_id（面板键）/name（规格键）同值二选一。"""
    name = str(body.get("persona_id") or body.get("name") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    path = _personas_path()
    if path is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），persona 无法删除"}
    personas = _personas_raw()
    remaining = [item for item in personas if str(item.get("name") or "") != name]
    if len(remaining) == len(personas):
        return {"error": f"persona 不存在: {name}"}
    _write_personas(path, remaining)
    return {"deleted": name}


# ── 创作草稿五态状态机（扩展波 B-Ⅰ，设计文档 §9.2；零新增插件进本端点组）─────────
#
# 个人内容级 AI 创作流：创作 agent（agents/card_author.yaml，工作台「AI 帮我
# 生成」经 mode.session 附身）在会话工作空间 drafts/ 起草完整卡 yaml → 登记
# （本面校验形态落 <user_config>/rp_drafts/，与卡库同区双根风格）→ lint（card_lint
# 单源 + suspicious 指令 + HTML 高危三面）→ smoke（一期人工试玩登记；自动探针
# 接 eval 域 = B-Ⅱ，probe 参数契约预留）→ 提交人审 → approve（受控搬移入用户层
# 卡库，save 通道语义）/ reject（意见回写元数据回创作会话迭代）。
#
# 安全线（制度级）：AI 生成内容一律不可直通用户层——三闸（lint/smoke/人审）全部
# 必经，approve 端点强制执法（状态机来源态 + 证据元数据缺一不可 = 409）；AI 署名
# 强制（卡 creator 字段服务端写定 _AI_CREATOR_MARK，客户端值一律覆盖）。
#
# 落点契约：草稿 = <user_config>/rp_drafts/<draft_id>.yaml（meta+card 双键映射，
# 原子写），审核留痕/报告全随草稿文件（rp_state_audit 不适用此域）；reject/批准
# 均不删文件（草稿数据保留）。draft_id 即入库卡 id（登记时按卡名派生避让，approve
# 以同 id 写用户层——与卡库同 id 接管语义一致）。

_DRAFTS_ROUTE = "/ext/mode_roleplay/data/wizards/drafts"
_DRAFT_ROUTE_PREFIX = "/ext/mode_roleplay/data/wizards/draft/"

# AI 署名强制标记（登记写入 + 入库复写双保险；草稿文件用户可手改，入库端点
# 不信任文件内容再断言一次）。
_AI_CREATOR_MARK = "AI"

# 状态机：register（重登记）合法来源态（内容替换=证据失效重置 draft；审核中/
# 已入库拒绝登记）+ 各动作来源态表。非法迁移 fail-closed 409。
_DRAFT_REREGISTER_FROM = ("draft", "lint_pass", "smoke_pass", "rejected")
_DRAFT_TRANSITIONS = {
    "lint": ("draft", "lint_pass"),
    "smoke": ("lint_pass",),
    "submit": ("smoke_pass",),
    "approve": ("awaiting_review",),
    "reject": ("awaiting_review",),
}
_DRAFT_STATES = ("draft", "lint_pass", "rejected", "smoke_pass", "awaiting_review", "approved")


def _user_drafts_dir() -> str | None:
    """用户层创作草稿目录：<user_config_dir>/rp_drafts（与卡库同区双根风格）。"""
    root = _user_config_root()
    return os.path.join(root, "rp_drafts") if root else None


def _draft_read(draft_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """读单草稿 → (meta, card)；id 形态/文件缺失/损坏/形态非法 = None（读面降级）。"""
    if not _CARD_ID_RE.fullmatch(draft_id):
        return None
    directory = _user_drafts_dir()
    path = os.path.join(directory, f"{draft_id}.yaml") if directory else None
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
        return None
    raw_card = data.get("card")
    card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    return data["meta"], card


def _draft_write(draft_id: str, meta: dict[str, Any], card: dict[str, Any]) -> None:
    """草稿原子落盘（meta+card 双键；id 随文件名不入载荷）。调用方保证 id 过闸。"""
    directory = _user_drafts_dir()
    if directory is None:
        raise RuntimeError("用户配置目录不可得（user_config_dir()=None），草稿无处落盘")
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{draft_id}.yaml"),
        yaml.safe_dump({"meta": meta, "card": card}, allow_unicode=True, sort_keys=False),
    )


def _draft_transition_guard(meta: dict[str, Any], action: str) -> str | None:
    """状态机闸：action 在当前态合法来源集内 = None；否则可读错误（409 用）。"""
    state = str(meta.get("state") or "")
    legal: tuple[str, ...]
    label: str
    if action == "register":
        legal, label = _DRAFT_REREGISTER_FROM, "重登记"
    else:
        legal, label = _DRAFT_TRANSITIONS[action], action
    if state not in legal:
        return f"状态机非法迁移：{label} 仅受理 {'/'.join(legal)} 态，草稿当前 {state or '未知'} 态"
    return None


def _draft_card_from_content(content: Any) -> tuple[dict[str, Any] | None, str]:
    """登记内容 → 卡 dict（yaml 文本 / dict / {card} 信封三形态宽容）；
    非法返回 (None, 可读错误)。形态闸只卡「映射 + 非空 name」，深校验归 lint。"""
    if isinstance(content, str):
        try:
            content = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return None, f"草稿 yaml 解析失败: {exc}"
    if not isinstance(content, dict):
        return None, "草稿内容须为卡字段映射（yaml 文本或对象）"
    envelope = content.get("card")
    card = envelope if isinstance(envelope, dict) else content
    if not str(card.get("name") or "").strip():
        return None, "草稿卡缺非空 name（id 由服务端按名派生）"
    return dict(card), ""


async def _draft_register_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/register：{author_session, path|content, draft_id?} → draft 态。

    内容来源二选一：path（相对会话工作空间，经 pipeline-state 行解析 +
    realpath 越界拒绝）或 content（yaml 文本 / 卡对象 / {card} 信封）。形态
    校验过闸后落盘：creator 署名强制、meta 记会话来源/时间；重登记（显式
    draft_id 且已存在）内容替换证据失效重置 draft、审核留痕保留；审核中/
    已入库 409。draft_id 缺省按卡名派生避让（卡库 ∪ 既有草稿占用）。
    """
    session_id = str(body.get("author_session") or "").strip()
    if not session_id:
        return {"error": "缺少 author_session（草稿须记创作会话来源）"}, 400
    if not _GAME_HOST_ID_RE.fullmatch(session_id):
        return {"error": f"非法 author_session（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {session_id}"}, 400
    rel_path = str(body.get("path") or "").strip()
    content = body.get("content")
    if bool(rel_path) == (content is not None):
        return {"error": "path 与 content 二选一（登记内容来源）"}, 400
    if rel_path:
        workspace = await _resolve_workspace(session_id)
        if workspace is None:
            return {"error": f"会话工作空间不可得（无该会话行或缺 workspace）: {session_id}"}, 400
        ws_real = os.path.realpath(workspace)
        target = os.path.realpath(os.path.join(ws_real, rel_path))
        if target != ws_real and not target.startswith(ws_real + os.sep):
            return {"error": f"草稿路径越出会话工作空间: {rel_path}"}, 400
        try:
            with open(target, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return {"error": f"草稿文件不可读: {rel_path}（{exc}）"}, 400
    card, parse_err = _draft_card_from_content(content)
    if card is None:
        return {"error": parse_err}, 400

    explicit_id = str(body.get("draft_id") or "").strip()
    if explicit_id:
        if not _CARD_ID_RE.fullmatch(explicit_id):
            return {"error": f"非法 draft_id（须为 ^[a-z0-9_]{{1,64}}$）: {explicit_id}"}, 400
        draft_id = explicit_id
    else:
        directory = _user_drafts_dir()
        taken = {c["id"] for c in _load_cards()}
        if directory and os.path.isdir(directory):
            taken.update(entry.name[: -len(".yaml")] for entry in Path(directory).glob("*.yaml"))
        draft_id = _derive_card_id(str(card.get("name")), taken)

    now = datetime.now(UTC).isoformat()
    existing = _draft_read(draft_id)
    if existing is not None:
        guard_err = _draft_transition_guard(existing[0], "register")
        if guard_err:
            return {"error": guard_err}, 409
        created_at = str(existing[0].get("created_at") or now)
        trail = existing[0].get("review_trail") if isinstance(existing[0].get("review_trail"), list) else []
    else:
        created_at, trail = now, []
    card["creator"] = _AI_CREATOR_MARK  # 署名强制（客户端值覆盖）
    meta = {
        "draft_id": draft_id,
        "state": "draft",
        "created_at": created_at,
        "updated_at": now,
        "author_session": session_id,
        "creator": _AI_CREATOR_MARK,
        "lint_report": None,   # 内容替换 = 旧证据失效，重登记同步清空
        "smoke_record": None,
        "reject_reason": None,
        "approved_card_id": None,
        "review_trail": trail,
    }
    try:
        _draft_write(draft_id, meta, card)
    except RuntimeError as exc:
        return {"error": str(exc)}, 500
    return {"draft_id": draft_id, "state": "draft", "name": str(card.get("name")), "creator": _AI_CREATOR_MARK}, 200


async def _draft_lint_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/lint：card_lint 单源结构校验 + suspicious 指令扫描 +
    HTML 高危扫描 → lint_pass / rejected（报告落元数据）。

    阻断面 = 结构 warnings（card_lint 三面）+ suspicious 注入形状（AC-6：lint
    拦注入形状）；HTML 高危只标注不阻断（渲染面 rpSanitizeHtml 白名单消毒兜底，
    与 import 面同待遇）。同源契约：结构判定直调本插件单源 lint_card_extensions，
    不复刻第二套。
    """
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "lint")
    if err:
        return {"error": err}, 409
    from card_lint import lint_card_extensions

    structure = lint_card_extensions(card.get("extensions"))
    suspicious = _scan_card_danger(card)
    html = _scan_card_html_danger(card)
    lint_pass = not structure and not suspicious
    report = {
        "lint_pass": lint_pass,
        "structure": structure,
        "suspicious": suspicious,
        "html": html,
        "linted_at": datetime.now(UTC).isoformat(),
    }
    meta["lint_report"] = report
    meta["state"] = "lint_pass" if lint_pass else "rejected"
    meta["updated_at"] = report["linted_at"]
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"], "report": report}, 200


async def _draft_smoke_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/smoke：一期人工试玩登记（试玩会话 id 引用 + 结论）→
    smoke_pass。自动探针经 B-Ⅱ 考据维持降级（诚实收敛，考据结论见 400 消息与
    模块 docstring）：eval 域 suite.smoke = agent 编排任务链管道 + 自进化账本
    耦合 + agent 键目标，与草稿卡探针三处错配；升级触发 = eval 域提供与进化
    账本解耦的卡级探针库化服务面。probe 参数携带即 400 显式降级说明。"""
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "smoke")
    if err:
        return {"error": err}, 409
    if body.get("probe"):
        return {"error": "probe 自动探针经 B-Ⅱ 考据维持降级（诚实收敛优于假接线）："
                         "eval 域 suite.smoke 是 agent 编排的任务链管道"
                         "（eval_view_suite 展开 → agent task_submit 派发 → "
                         "eval_summarize 落自进化账本），与草稿卡探针三处错配——"
                         "①草稿卡非 agent 键，临时注册须写出厂面（红线）；"
                         "②eval_summarize 落进化账本（rounds/material_ref 重放键），"
                         "卡级探针注入会污染基线链；③本端点为同步 HTTP，探针轮是"
                         "分钟级真 LLM 任务链运行，需异步两段式新编排面。升级触发 = "
                         "eval 域提供模式插件可消费的卡级探针库化服务面（展开/派发/"
                         "聚合与进化账本解耦）。一期请人工开演试玩后登记会话引用"}, 400
    smoke_session_id = str(body.get("smoke_session") or "").strip()
    if not smoke_session_id:
        return {"error": "缺少 smoke_session（试玩会话 id 引用）"}, 400
    record = {
        "smoke_session": smoke_session_id,
        "conclusion": str(body.get("conclusion") or ""),
        "smoked_at": datetime.now(UTC).isoformat(),
    }
    meta["smoke_record"] = record
    meta["state"] = "smoke_pass"
    meta["updated_at"] = record["smoked_at"]
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"], "record": record}, 200


async def _draft_submit_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/submit：smoke_pass → awaiting_review（进审核队列）。"""
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "submit")
    if err:
        return {"error": err}, 409
    meta["state"] = "awaiting_review"
    meta["updated_at"] = datetime.now(UTC).isoformat()
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"]}, 200


async def _draft_approve_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/approve：三闸强制（安全底线）→ 受控搬移入用户层卡库。

    三闸 = ①状态机来源态 awaiting_review（人审动作本体）②lint 通过证据元数据
    ③smoke 记录元数据——缺一不可 409 可读错误（手工篡改文件态也进不来）；审核人
    = 当前请求用户（token 解析，未认证 401），留痕 who/when/approve 随草稿。
    入库 = save 通道语义（_write_user_card，同 id 接管），creator 署名入库前
    复写断言。草稿文件保留（审核历史随文件）。"""
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return {"error": "未认证：token 缺失或无效"}, 401
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "approve")
    if err:
        return {"error": err}, 409
    report = meta.get("lint_report")
    if not isinstance(report, dict) or report.get("lint_pass") is not True:
        return {"error": "三闸未齐：lint 闸无通过证据（先走 lint 校验到 lint_pass）"}, 409
    if not isinstance(meta.get("smoke_record"), dict):
        return {"error": "三闸未齐：smoke 闸无试玩记录（先登记试玩到 smoke_pass）"}, 409
    card["creator"] = _AI_CREATOR_MARK  # 入库复写断言（草稿文件用户可手改，不信任）
    saved = _write_user_card(_card_from_data(card, draft_id, "user"))
    if "error" in saved:
        return {"error": str(saved["error"])}, 500
    now = datetime.now(UTC).isoformat()
    meta["state"] = "approved"
    meta["approved_card_id"] = draft_id
    meta["updated_at"] = now
    trail = meta["review_trail"] if isinstance(meta.get("review_trail"), list) else []
    trail.append({"who": user_id, "when": now, "action": "approve"})
    meta["review_trail"] = trail
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": "approved", "card_id": draft_id}, 200


async def _draft_reject_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/reject：意见回写元数据 + 留痕（who/when/reject/reason）
    → rejected（草稿数据保留，回创作会话迭代后重登记）。"""
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return {"error": "未认证：token 缺失或无效"}, 401
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "reject")
    if err:
        return {"error": err}, 409
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return {"error": "缺少 reason（打回意见回创作会话迭代）"}, 400
    now = datetime.now(UTC).isoformat()
    meta["state"] = "rejected"
    meta["reject_reason"] = reason
    meta["updated_at"] = now
    trail = meta["review_trail"] if isinstance(meta.get("review_trail"), list) else []
    trail.append({"who": user_id, "when": now, "action": "reject", "reason": reason})
    meta["review_trail"] = trail
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": "rejected"}, 200


def _drafts_query(query: dict[str, str]) -> tuple[dict[str, Any], int]:
    """GET /wizards/drafts：列表（state 过滤）/ 单草稿详情（三件套）。

    详情按 draft_id 定位（卡内容 + lint 报告 + smoke 记录 + 审核留痕全随 meta
    透出）；列表项 = 摘要行（updated_at 降序）。读面降级：损坏文件跳过；用户
    空间不可得 = 空表。"""
    draft_id = str(query.get("draft_id") or "").strip()
    if draft_id:
        existing = _draft_read(draft_id)
        if existing is None:
            return {"error": f"草稿不存在或已损坏: {draft_id}"}, 400
        meta, card = existing
        return {"draft_id": draft_id, "meta": meta, "card": card}, 200
    state_filter = str(query.get("state") or "").strip()
    if state_filter and state_filter not in _DRAFT_STATES:
        return {"error": f"非法 state 过滤值（可选 {'/'.join(_DRAFT_STATES)}）: {state_filter}"}, 400
    directory = _user_drafts_dir()
    items: list[dict[str, Any]] = []
    if directory and os.path.isdir(directory):
        for entry in sorted(Path(directory).glob("*.yaml")):
            existing = _draft_read(entry.name[: -len(".yaml")])
            if existing is None:
                continue
            meta, card = existing
            if state_filter and str(meta.get("state") or "") != state_filter:
                continue
            items.append({
                "draft_id": str(meta.get("draft_id") or entry.stem),
                "name": str(card.get("name") or ""),
                "state": str(meta.get("state") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "updated_at": str(meta.get("updated_at") or ""),
                "author_session": str(meta.get("author_session") or ""),
                "creator": str(meta.get("creator") or ""),
            })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"drafts": items}, 200


_DRAFT_HANDLERS = {
    "register": _draft_register_request,
    "lint": _draft_lint_request,
    "smoke": _draft_smoke_request,
    "submit": _draft_submit_request,
    "approve": _draft_approve_request,
    "reject": _draft_reject_request,
}


async def _handle_draft_routes(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str] | None
) -> dict[str, Any] | None:
    """创作草稿路由分发；未命中返回 None（交回 404 路径）。"""
    if path == _DRAFTS_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        payload, status = _drafts_query(query)
        return {"success": True, "data": _json_response(payload, status)}
    action = path[len(_DRAFT_ROUTE_PREFIX):] if path.startswith(_DRAFT_ROUTE_PREFIX) else ""
    handler = _DRAFT_HANDLERS.get(action)
    if handler is None:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    payload, status = await handler(_parse_body(raw_body), headers)
    return {"success": True, "data": _json_response(payload, status)}


# ── 写动作（task_submit 真派发；eval_harness 先例：tool-executor 显式 plugin_id）──


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_roleplay/page/roleplay-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "roleplay_panel.html"
)

# 出厂游戏页（Tier 1 第一宿主呈现页：HTML 路由 /page/game-host——无独立
# workspace tab（2026-10-14 单页化裁定：contributes.pages 不再声明 game_host 条目），
# 由面板玩法子页内嵌挂载 document 注入；引擎件挂载点占位随批 4-Ⅱ）
_GAME_PAGE_ENDPOINT = "/ext/mode_roleplay/page/game-host"
_GAME_PAGE_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "game_host.html"
)

# message 卡型页退役（2026-10-17 用户裁定：消息流不挂固定 webview 卡——对话流
# 原生渲染，状态/选项呈现归工作区玩法页）：roleplay_bubble_card /
# roleplay_state_card 两 message-style 页、端点与 webview 页件一并移除，
# message_cards marker='state' 条目保留（标记切除的激活门）。

# ── 游戏域会话通道（RP 批 4-Ⅰ）───────────────────────────────────────────────────
#
# 双域分治（设计文档 §二）：游戏域宿主权威，本通道 = 宿主↔后端同步面：
# 单宿主租约（attach/detach，第二宿主 attach=接管+对账）+ 快照 push（薄 sanity
# 后只留最新，不否决宿主权威）+ delta 回推（state_marker_parse 落账步经
# mode.game.push_delta 入队，GET state 取后清）。session_id = RP 扮演会话的
# pipeline_id（游戏页会话绑定指示轮询 data/sessions 跟随）。
#
# 落点契约：租约/快照/待发 delta = 本插件进程内寄存器（索引级只留最新）——
# 持久写面按 manifest state.writes 声明制，本域未声明持久键；进程内寄存器是
# 现状落点（state_marker_parse._latest 同形态先例）；重启即失，宿主重 attach/
# 重推快照自愈（快照权威在宿主，后端只做薄 sanity+审计）。
_GAME_HOST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_GAME_CHANNEL_CAP = 200  # 待发 delta 队列上限（宿主离线保护；超出丢最旧）
_GAME_CHANNELS: dict[str, dict[str, Any]] = {}

_GAME_ATTACH_ROUTE = "/ext/mode_roleplay/game/session/attach"
_GAME_SNAPSHOT_ROUTE = "/ext/mode_roleplay/game/snapshot"
_GAME_STATE_ROUTE = "/ext/mode_roleplay/game/state"
_GAME_DETACH_ROUTE = "/ext/mode_roleplay/game/detach"
_GAME_CONFIG_ROUTE = "/ext/mode_roleplay/game/config"
_GAME_TURN_ROUTE = "/ext/mode_roleplay/game/turn"
# 自由操作→结构化命令端点（世界内嵌谱系右移批次二，调研 §7.6 真缺口①）：
# 玩家 Command 与 NPC Decision 同一队列同一校验（ADR 权威光谱框架支柱⑦）——
# Propose（清单/params fail-closed 400）→ Adjudicate（Validator 同源结算，
# 拒绝必须带原因）。
_GAME_OPERATION_ROUTE = "/ext/mode_roleplay/game/operation"
# 世界演化周期推进端点（世界内嵌谱系右移批次三，调研 §7.6 真缺口②）：宿主页/
# 触发器直达管道接口——不经任务壳（任务状态机/评估闸门与对话轮语义错配，
# ADR 2026-10-17 被否方案④）；确定性引擎结算零 LLM（支柱⑧去 AI 退化判据）。
# 消费登记：定时直达（triggers_ext 定时调用本端点=离台推进）只登记不实现
# ——触发器侧接线属触发器插件域，本端点契约即登记面。
_GAME_TICK_ROUTE = "/ext/mode_roleplay/game/tick"
# 自包含玩法页装配载荷端点（RP 面板 v3.1 刀1）：JSON 载荷 {session_id, page_html}
# 非 HTML 文档——消费方（game_host / 面板）取 page_html 自行注入渲染。
_GAME_PAGE_DATA_ROUTE = "/ext/mode_roleplay/game/page"

# ── turn 服务类型（桥 API 契约，设计文档 §六.1；RP 批 5）──────────────────────────
# turn 请求带 service 字段，契约全集四类型。一期实开 main（玩家主循环输入）；
# npc_chat 一期消费形态 = mode.session 专属会话（游戏页 NPC 深聊入口，会话出生
# 不经 turn 通道）；barks/event 后置占位——批量/事件 LLM 面暂无真实消费方，
# 不实现死代码（触发条件登记 GAME_HOST_SDK.md 服务类型节，消费落地后在此扩分支）。
_TURN_SERVICES = ("main", "npc_chat", "barks", "event")
_TURN_SERVICE_PLACEHOLDERS = {
    "npc_chat": "service=npc_chat 一期走 mode.session 专属会话（NPC 深聊入口），不经 turn 通道",
    "barks": "service=barks 后置占位：批量台词 LLM 面暂无消费方（触发条件见 GAME_HOST_SDK.md 服务类型节）",
    "event": "service=event 后置占位：事件叙事 LLM 面暂无消费方（触发条件见 GAME_HOST_SDK.md 服务类型节）",
}


def _reset_game_channels() -> None:
    """清空通道注册表（单测隔离用；生产随进程生命周期）。"""
    _GAME_CHANNELS.clear()


def _game_channel_id(body: dict[str, Any], key: str) -> tuple[str, str | None]:
    """通道 id 校验（缺参/形态非法 = ("", 可读错误)；fail-closed）。

    失败态返回空串（调用方凭第二元错误短路 400，空串不再下行）。
    """
    raw = str(body.get(key) or "").strip()
    if not raw:
        return "", f"缺少 {key}"
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return "", f"非法 {key}（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"
    return raw, None


def _is_game_index(value: Any) -> bool:
    """快照序号薄 sanity：非负整数（bool 是 int 子类，显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0

# GET 数据端点（无副作用）+ POST 写动作端点（regenerate 会真实派发任务进
# 管道；play 为 possess-only 回执面；cards/personas 写面为用户空间文件操作，
# 不派任务）
_DATA_ROUTES = {
    "/ext/mode_roleplay/data/bootstrap",
    "/ext/mode_roleplay/data/sessions",
    "/ext/mode_roleplay/data/cards",
    "/ext/mode_roleplay/data/lorebooks",
    "/ext/mode_roleplay/data/packs",
    _WIZARD_AXES_ROUTE,
    _PRECIP_CANDIDATES_ROUTE,
}
_ACTION_ROUTES = {
    "/ext/mode_roleplay/data/actions/play",
    "/ext/mode_roleplay/data/actions/regenerate",
    "/ext/mode_roleplay/data/cards/save",
    "/ext/mode_roleplay/data/cards/delete",
    "/ext/mode_roleplay/data/cards/import",
    "/ext/mode_roleplay/data/cards/export",
    "/ext/mode_roleplay/data/lorebooks/save",
    "/ext/mode_roleplay/data/lorebooks/delete",
    "/ext/mode_roleplay/data/lorebooks/import",
    "/ext/mode_roleplay/data/personas/save",
    "/ext/mode_roleplay/data/personas/delete",
    _COMBOS_ROUTE_SAVE,
    _COMBOS_ROUTE_DELETE,
    _PRECIP_PROMOTE_ROUTE,
    _SCENE_TURN_ROUTE,
}


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """任意 JSON 对象 → HttpHandleResponse（body base64，内核约定）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def _panel_html_response() -> dict[str, Any]:
    """包内 webview 面板页 → HttpHandleResponse（text/html，body base64）。"""
    return html_file_response(_PANEL_HTML_PATH)


# 游戏页单源装配（RP 批 4-Ⅲ 机制，评审修复刀增桥面）：rpSanitizeHtml 与
# agentosFetch/unwrap 单源均在 roleplay_panel.html（rp-sanitize / rp-bridge
# single-source 块）；游戏页磁盘上不持手抄副本——下发时从面板单源块原位抽取、
# 替换页内装配标记（serve-time include），装配结果与单源逐字节一致由测试执法。
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


def _game_page_html_response() -> dict[str, Any]:
    """包内 webview 出厂游戏页 → HttpHandleResponse（serve-time 装配面板单源）。

    fail-closed：装配标记/面板单源块缺失 = RuntimeError 上抛（页面缺消毒/
    桥函数属包事实，不静默降级为未装配渲染面）。
    """
    with open(_GAME_PAGE_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    return html_text_response(_assemble_page_single_source(html, "game page"))


# 请求体解析失败哨兵（_parse_body invalid= 形态返回值，调用方可辨——
# object() 恒不等任何 JSON 解析产物）。
_BODY_INVALID: Any = object()


def _parse_body(raw_body: str, *, invalid: Any | None = None) -> Any:
    """POST 动作载荷解析（内核 base64 契约 + 裸 JSON 双形态，先 base64 后原文）。

    内核 http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入
    （kernel/crates/api/src/http_dispatcher.rs），宿主桥直发 JSON 的 MCP 直调
    形态则是裸 JSON 字符串——两形态都收。解码顺序与语义对齐
    plugins/shared/http_json.py::decode_body（先 base64，解出 {/[ 开头才采用，
    否则回退原文）；两形态都失败或顶层非 dict → {}（交缺参 400 路径）。
    invalid 传值（如 _BODY_INVALID）时解析失败/顶层非 dict 改返该值供调用方
    可辨（缺省 None = 失败回落 {}，既有调用方零变化）。单点复用面：
    _handle_actions / game 域 / draft 域 / pipeline/open。
    内联而非 import 共享根模块：种子单元自包含（用户空间播种副本不可达共享根）。
    """
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        data = json.loads(decoded)
    except ValueError:
        return invalid if invalid is not None else {}
    if not isinstance(data, dict):
        return invalid if invalid is not None else {}
    return data


def _routed_json(result: dict[str, Any]) -> dict[str, Any]:
    """动作结果 → HttpHandleResponse：error 按类分诊（不存在/未知/非法/缺少/
    无效/不可删除 → 400 客户端可修正；其余 409 冲突/通道类），成功 200。"""
    if "error" not in result:
        return {"success": True, "data": _json_response(result)}
    message = str(result["error"])
    status = (
        400
        if any(k in message for k in ("不存在", "未知", "非法", "缺少", "无效", "不可删除"))
        else 409
    )
    return {"success": True, "data": _json_response(result, status)}


async def _handle_play(body: dict[str, Any]) -> dict[str, Any]:
    """开演端点（possess-only）。

    fresh 开演已会话化（2026-09-25 用户痛点修复：扮演是对话不是任务）——面板
    改走 roleplay.continue 宿主桥建扮演会话（开场白/用户设定经执行选项快照随
    消息透传），本端点不再派发任务；仅保留附身（possess）：校验卡存在后回
    {card_id, play_mode, presenter}，presenter 另附 theme；附身状态由前端持久化
    并在发送时注入 execution_context，服务端无状态。未知/缺省 play_mode 之外
    的取值 400 显式拒绝（缺省按 possess——现存的唯一模式）。
    """
    card_id = str(body.get("card_id") or "")
    if not card_id:
        return {"success": True, "data": _json_response({"error": "缺少 card_id"}, 400)}
    play_mode = str(body.get("play_mode") or "possess")
    if play_mode != "possess":
        return {"success": True, "data": _json_response({"error": f"未知 play_mode: {play_mode}"}, 400)}
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"success": True, "data": _json_response({"error": f"未知角色卡: {card_id}"}, 400)}
    return {"success": True, "data": _json_response({
        "card_id": card_id,
        "play_mode": "possess",
        "presenter": {
            "card_id": card_id,
            "name": card["name"],
            "avatar": card.get("avatar"),
            "theme": card.get("theme"),
        },
    })}


# ── 游戏域通道端点（RP 批 4-Ⅰ；四端点 + push_delta 服务，零新增插件）──────────────


def _game_routed(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """通道端点响应（HttpHandleResponse 信封；错误码随 payload 语义 400/409）。"""
    return {"success": True, "data": _json_response(payload, status)}


async def _handle_game_attach(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/session/attach：单宿主租约登记（设计文档 §二 同步时序）。

    三态：无租约=新租 attached；同 host 重入=幂等续租 renewed（可补带
    game_save_id）；异 host=接管 takeover——返回前宿主信息 + 对账要求
    （reconcile_required），租约换手且清前宿主待发 delta（新宿主从自己的存档
    起步，陈旧 delta 不转移）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    raw_save = body.get("game_save_id")
    if raw_save is not None and not isinstance(raw_save, str):
        return _game_routed({"error": "game_save_id 须为字符串"}, 400)
    channel = _GAME_CHANNELS.setdefault(
        session_id, {"lease": None, "snapshot": None, "pending_deltas": []}
    )
    lease = channel["lease"]
    now = datetime.now(UTC).isoformat()
    if lease is not None and lease["host_id"] == host_id:
        lease["game_save_id"] = raw_save
        lease["attached_at"] = now
        return _game_routed({"status": "renewed", "lease": dict(lease)})
    if lease is not None:
        previous = dict(lease)
        channel["lease"] = {"host_id": host_id, "game_save_id": raw_save, "attached_at": now}
        channel["pending_deltas"] = []
        return _game_routed({
            "status": "takeover",
            "previous_host": previous,
            "reconcile_required": True,
            "lease": dict(channel["lease"]),
        })
    channel["lease"] = {"host_id": host_id, "game_save_id": raw_save, "attached_at": now}
    return _game_routed({"status": "attached", "lease": dict(channel["lease"])})


async def _handle_game_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/snapshot：宿主快照 push——薄 sanity 后只留最新 + 审计追加。

    薄 sanity 仅形态面（state 为 dict/键为字符串/turn_id·seq 非负整数）；内容
    零校验零 clamp（宿主权威，后端不否决，设计文档 §二）。未 attach/非租约
    持有宿主 = 409（单宿主租约执法）。审计复用共享单源 rp_state_audit
    （source=host_snapshot，幂等键含 turn_id+seq；ws 经 pipeline-state 行
    workspace 解析，不可得 = 审计降级跳过不阻断）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    if not _is_game_index(body.get("turn_id")):
        return _game_routed({"error": "turn_id 须为非负整数"}, 400)
    if not _is_game_index(body.get("seq")):
        return _game_routed({"error": "seq 须为非负整数"}, 400)
    snap_state = body.get("state")
    if not isinstance(snap_state, dict):
        return _game_routed({"error": "state 须为 JSON 对象（薄 sanity）"}, 400)
    if not all(isinstance(key, str) for key in snap_state):
        return _game_routed({"error": "state 键须全为字符串（薄 sanity）"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再推快照"}, 409)
    lease = channel["lease"]
    if lease["host_id"] != host_id:
        return _game_routed(
            {"error": f"非租约持有宿主（当前租约: {lease['host_id']}），快照拒绝"}, 409
        )
    turn_id = body["turn_id"]
    previous_turn = channel["snapshot"]["turn_id"] if channel["snapshot"] else None
    channel["snapshot"] = {
        "turn_id": turn_id,
        "seq": body["seq"],
        "state": snap_state,
        "host_id": host_id,
        "ts": datetime.now(UTC).isoformat(),
    }
    from _root_api import append_audit_rows

    workspace = await _resolve_workspace(session_id)
    append_audit_rows(
        workspace,
        [{
            "key": "game_host_snapshot",
            "op": "=",
            "from": previous_turn,
            "to": turn_id,
            "seq": body["seq"],
            "reason": "host_snapshot",
        }],
        f"{session_id}:host_snapshot:{turn_id}:{body['seq']}",
        "host_snapshot",
    )
    return _game_routed({"status": "accepted", "snapshot": dict(channel["snapshot"])})


async def _handle_game_state(session_id: str) -> dict[str, Any]:
    """GET /game/state：最新快照 + 租约信息 + 待发 deltas（取后清）。

    无快照 = 显式空对象非 404（宿主轮询友好）；未知会话 = 全空形态 200。
    """
    raw = str(session_id or "").strip()
    if not raw:
        return _game_routed({"error": "缺少 session_id"}, 400)
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return _game_routed(
            {"error": f"非法 session_id（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"}, 400
        )
    channel = _GAME_CHANNELS.get(raw)
    if channel is None:
        return _game_routed(
            {"session_id": raw, "lease": None, "snapshot": {}, "pending_deltas": []}
        )
    pending = channel["pending_deltas"]
    channel["pending_deltas"] = []
    return _game_routed({
        "session_id": raw,
        "lease": dict(channel["lease"]) if channel["lease"] else None,
        "snapshot": dict(channel["snapshot"]) if channel["snapshot"] else {},
        "pending_deltas": pending,
    })


async def _handle_game_detach(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/detach：租约释放（快照保留=宿主离线降级读面；待发 delta 清）。

    无租约 = 幂等成功（released=False）；他宿主 = 409（不可释他宿主租约）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"status": "detached", "released": False})
    lease = channel["lease"]
    if lease["host_id"] != host_id:
        return _game_routed(
            {"error": f"非租约持有宿主，不可释放（当前租约: {lease['host_id']}）"}, 409
        )
    channel["lease"] = None
    channel["pending_deltas"] = []
    return _game_routed({"status": "detached", "released": True})


def _user_id_from_headers(headers: dict[str, str] | None) -> str:
    """请求 token → user_id（内核 access token 载荷 `access:<uid>:<username>:<exp>:
    <jti>:<h>` 自包含 uid；内核转发 /ext 带全量 headers）。解析失败/缺失 = 空串
    （调用方按未认证拒绝——chat.send_message 契约 user_id 非空，不猜默认用户）。"""
    headers_lc = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    auth = headers_lc.get("authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    try:
        payload_b64 = auth[7:].strip().split(".", 1)[0]
        pad = "=" * (-len(payload_b64) % 4)
        parts = base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8").split(":", 5)
        if len(parts) == 6 and parts[0] == "access":
            return parts[1]
    except (ValueError, IndexError):
        return ""
    return ""


async def _handle_game_turn(body: dict[str, Any], headers: dict[str, str] | None) -> dict[str, Any]:
    """POST /game/turn：玩家输入经服务类型框架派发进绑定 RP 会话（批 5 turn 通道）。

    - service 字段（契约全集见 _TURN_SERVICES）：一期实开 main（缺省）；
      npc_chat/barks/event = 400 可读占位（消费形态/触发条件见 GAME_HOST_SDK.md，
      不实现死代码）；
    - 会话绑定校验：session 须为扮演会话行（pipeline-state mode=roleplay）；
    - 单宿主租约：未 attach = 409（服务点位=同租约内请求类型，设计文档 §六.1）；
    - 派发 = chat.send_message 注入分支薄代理（pipeline_id 定位既有管道 +
      background:true 后台派发即时回执——宿主不等 LLM，叙事经既有 data/messages
      轮询自然回流，零新回推通道）；user_id 从请求 token 解（缺失 401）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    text = str(body.get("text") or "").strip()
    if not text:
        return _game_routed({"error": "缺少 text（玩家输入为空）"}, 400)
    service = str(body.get("service") or "main")
    if service not in _TURN_SERVICES:
        return _game_routed(
            {"error": f"非法 service: {service}（可选 {'/'.join(_TURN_SERVICES)}）"}, 400
        )
    placeholder = _TURN_SERVICE_PLACEHOLDERS.get(service)
    if placeholder:
        return _game_routed({"error": placeholder}, 400)
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return _game_routed({"error": "未认证：token 缺失或无效"}, 401)
    rows = await _call_provider("pipeline-state")
    is_roleplay_session = any(
        isinstance(row, dict)
        and str(row.get("pipeline_id") or "") == session_id
        and str(row.get("mode") or "") == "roleplay"
        for row in rows
    )
    if not is_roleplay_session:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 turn"}, 409)
    try:
        cap = plugin.get_capability("chat")
        from agentos_plugin_sdk.capability import bind_capability_caller

        res = await bind_capability_caller(cap, "chat")(
            "chat.send_message",
            {"message": text, "user_id": user_id, "pipeline_id": session_id, "background": True},
            timeout=25.0,
        )
    except Exception as exc:  # noqa: BLE001 — 薄代理：capability 失败原样透传
        return {"success": True, "data": _json_response({"error": f"turn 派发失败: {exc}"}, 502)}
    if not isinstance(res, dict) or res.get("status") != "dispatched":
        return {
            "success": True,
            "data": _json_response({"error": f"turn 未走派发分支: {str(res)[:200]}"}, 502),
        }
    return _game_routed({
        "status": "dispatched",
        "session_id": session_id,
        "service": "main",
        "pipeline_id": session_id,
    })


# ── 自由操作→结构化命令（世界内嵌谱系右移批次二，调研 §7.6 真缺口①）────────────
# 玩家在玩法页内对世界直接操作，页经桥发结构化操作命令进与对话同一套校验结算：
# Propose（id 须在绑定包 operations 清单 / params 按 params_schema 校验，均
# fail-closed 400 带原因）→ Adjudicate（effects 走 State Contract Validator
# 同源结算——pipeline_state_marker_parse 的 state.adjudicate 纯委托服务，跨插件
# tool-executor 显式 plugin_id 同通道；玩家也是 Agent，同一校验不做 cheat 旁路；
# 原子结算：任一效果被拒 = 整体 applied:false + 具体原因，审计照记全流水）。
_SOURCE_OPERATION = "operation"
_ADJUDICATE_SERVICE_PLUGIN = "pipeline_state_marker_parse"


async def _invoke_tool(tool_name: str, plugin_id: str, args: dict[str, Any], timeout: float = 10.0) -> Any:
    """tool-executor 显式 plugin_id 跨插件调用（character_state.get 同通道先例）。"""
    te = plugin.get_capability("tool-executor")
    caller = bind_capability_caller(te, "tool-executor")
    return await caller(
        "tool-executor.invoke",
        {"tool_name": tool_name, "plugin_id": plugin_id, "args": args},
        timeout=timeout,
    )


def _validate_operation_params(
    params_schema: dict[str, Any] | None, params: Any
) -> tuple[dict[str, Any], str | None]:
    """params 按 params_schema 校验（Propose 面，fail-closed）。

    返回（校验后参数, 错误原因）：未声明键拒绝（声明制——清单外不可发同闸）、
    类型不符拒绝、min/max 越界拒绝、必填（无 default）缺席拒绝；有 default 的
    字段缺席按缺省补齐。params_schema 缺席 = 零参数契约（携参即拒）。
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {}, f"params 须为映射（字段名 → 值），得 {type(params).__name__}"
    schema = params_schema or {}
    validated: dict[str, Any] = {}
    for key in params:
        if key not in schema:
            return {}, f"参数 {key!r} 未在操作 params_schema 声明（未声明=不可发）"
    for field, decl in schema.items():
        value = params.get(field)
        if value is None:
            if "default" in decl and decl["default"] is not None:
                validated[field] = decl["default"]
                continue
            return {}, f"缺少必填参数 {field!r}（无 default 可补）"
        decl_type = decl.get("type")
        type_ok = (
            isinstance(value, bool) if decl_type == "bool"
            else isinstance(value, str) if decl_type == "string"
            else isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (decl_type == "float" or isinstance(value, int))
        )
        if not type_ok:
            return {}, (
                f"参数 {field!r} 类型不符（声明 {decl_type}，"
                f"得 {type(value).__name__}: {value!r}）"
            )
        minimum, maximum = decl.get("min"), decl.get("max")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if minimum is not None and value < minimum:
                return {}, f"参数 {field!r} 越界：{value} < min {minimum}"
            if maximum is not None and value > maximum:
                return {}, f"参数 {field!r} 越界：{value} > max {maximum}"
        validated[field] = value
    return validated, None


def _operation_reject_reason(rejected: list[dict[str, Any]]) -> str:
    """被拒效果 → 具体原因一行（拒绝必须带原因：键 + reason + layer 可辨）。"""
    parts = [
        f"{row.get('key')} 被拒（{row.get('reason')}/{row.get('layer')}）"
        for row in rejected
        if isinstance(row, dict)
    ]
    return "效果结算未通过：" + "；".join(parts)


async def _handle_game_operation(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/operation：自由操作→结构化命令（Propose→Adjudicate）。

    - 会话校验/单宿主租约与 game/turn 同闸（会话行 mode=roleplay + attach 在场）；
    - Propose：operation.id 须在会话绑定包 operations 清单（fail-closed 400，
      未声明=不可发——枚举声明制）；params 按 params_schema 类型/边界校验
      （fail-closed 400）；
    - Adjudicate：声明 effects（请求不携 effects——玩家不能发明效果，支柱⑦）
      走 state.adjudicate 纯委托服务（Validator 单源）结算：schema/hard_rules
      自会话行 context.rp_state_schema/hard_rules，现值经 character_state.get，
      accepted 经 character_state.update 落账；原子结算（任一被拒 = 整体拒绝，
      回执 ``{applied: false, reason}`` 带具体原因）；审计进 rp_state_audit 流水
      （source=operation，actor=player，summary 带操作 id——沉淀回路输入）。
      传输面失败（服务不可用）= 502；规则面拒绝 = 200 回执（可辨非错误）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    operation = body.get("operation")
    if not isinstance(operation, dict):
        return _game_routed({"error": "operation 须为映射（{id, params}）"}, 400)
    op_id = str(operation.get("id") or "").strip()
    if not op_id:
        return _game_routed({"error": "缺少 operation.id"}, 400)
    rows = await _call_provider("pipeline-state")
    row = next(
        (
            r for r in rows
            if isinstance(r, dict)
            and str(r.get("pipeline_id") or "") == session_id
            and str(r.get("mode") or "") == "roleplay"
        ),
        None,
    )
    if row is None:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 operation"}, 409)
    # Propose：会话绑定包解析（game/config 同源链）→ 清单全等选取
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    decl: dict[str, Any] | None = None
    if agent_id.startswith(prefix):
        card = next(
            (c for c in _load_cards() if c["id"] == agent_id[len(prefix):]), None
        )
        if card is not None:
            packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
            listing: list[dict[str, Any]] = []
            for index, (_, data, _dir) in enumerate(packs):
                normalized = _pack_operations_listing(
                    data, str(data.get("id") or f"#{index}")
                )
                if normalized:
                    listing = normalized  # 声明序后者非空清单整体覆盖（choices 同构）
            decl = next((e for e in listing if e["id"] == op_id), None)
    if decl is None:
        return _game_routed({
            "error": (
                f"操作 {op_id!r} 未在会话绑定玩法包 operations 清单声明"
                "（未声明=不可发，枚举声明制）"
            )
        }, 400)
    params, param_err = _validate_operation_params(decl.get("params_schema"), operation.get("params"))
    if param_err:
        return _game_routed({"error": param_err}, 400)
    # Adjudicate：schema 缺席 = 无契约可结，fail-closed 回执拒绝（不静默透传）
    schema = row.get("context.rp_state_schema")
    workspace = await _resolve_workspace(session_id)
    from _root_api import append_audit_rows, new_ulid  # noqa: PLC0415（共享根裸名，惰性同 import 面）

    audit_extra = {"source": _SOURCE_OPERATION, "action": op_id, "params": params}
    idem = f"{session_id}:operation:{op_id}:{new_ulid()}"

    def _audit(rows_: list[dict[str, Any]]) -> None:
        append_audit_rows(
            workspace,
            [{**r, **audit_extra} for r in rows_],
            idem,
            _SOURCE_OPERATION,
        )

    if not isinstance(schema, dict):
        reason = (
            "状态契约未装载（会话行无 context.rp_state_schema），"
            f"操作 {op_id!r} 拒绝结算（fail-closed）"
        )
        _audit([{"key": op_id, "op": None, "from": None, "to": None, "reason": "no_schema"}])
        return _game_routed({"applied": False, "operation": op_id, "reason": reason})
    hard_rules = row.get("context.rp_state_hard_rules")
    card_id = agent_id[len(prefix):] if agent_id.startswith(prefix) else ""
    try:
        current_res = await _invoke_tool(
            "character_state.get", "character_state", {"card_id": card_id}
        )
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"操作现值读取失败（character_state 不可用）: {exc}"}, 502)}
    if not (isinstance(current_res, dict) and current_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "操作现值读取失败（character_state 回执异常）"}, 502)}
    current = (current_res.get("data") or {}).get("values")
    if not isinstance(current, dict):
        current = {}
    try:
        adj_res = await _invoke_tool("state.adjudicate", _ADJUDICATE_SERVICE_PLUGIN, {
            "changes": dict(decl["effects"]),
            "schema": schema,
            "hard_rules": hard_rules if isinstance(hard_rules, dict) else None,
            "current": current,
        })
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"操作结算失败（state.adjudicate 不可用）: {exc}"}, 502)}
    if not (isinstance(adj_res, dict) and adj_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "操作结算失败（state.adjudicate 回执异常）"}, 502)}
    result = adj_res.get("data") or {}
    rejected = result.get("rejected")
    accepted = result.get("accepted")
    audit = result.get("audit")
    if not isinstance(rejected, list) or not isinstance(accepted, dict) or not isinstance(audit, list):
        return {"success": True, "data": _json_response(
            {"error": "操作结算回执形态非法（accepted/rejected/audit 缺失）"}, 502)}
    # 原子结算：任一效果被拒 = 整体拒绝（玩家离散命令无部分应用），审计照记
    if rejected:
        _audit(audit)
        return _game_routed({
            "applied": False,
            "operation": op_id,
            "reason": _operation_reject_reason(rejected),
        })
    if accepted:
        try:
            await _invoke_tool(
                "character_state.update", "character_state",
                {"card_id": card_id, "changes": accepted},
            )
        except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
            return {"success": True, "data": _json_response(
                {"error": f"操作落账失败（character_state 不可用）: {exc}"}, 502)}
    _audit(audit)
    return _game_routed({
        "applied": True,
        "operation": op_id,
        "state_delta": accepted,
    })


# ── tick/长时段演化（世界内嵌谱系右移批次三，调研 §7.6 真缺口②）──────────────
# 玩法包声明世界演化规则（pack yaml 顶层 `tick`），宿主页/触发器经 game 通道
# 直达推进世界状态——不经任务壳；确定性引擎结算零 LLM（支柱⑧：AI 只出现在
# 枚举必败处，确定性段零 LLM）。结算逐 tick × 逐 step 顺序进行，每步一次
# state.adjudicate（Validator 单源）；cycle 算子在编排侧解析为绝对值——
# Validator 契约零改动（禁第二套结算语义）。when 键域执法位 = 消费侧
# _pack_tick_listing（条件求值不经 Validator）；effects 键域照旧归 Validator。
_SOURCE_TICK = "tick"
_TICK_MAX_TICKS = 10  # 单次调用上限钳制（防打爆；定时直达消费侧自限循调用）


def _tick_baseline(running: dict[str, Any], key: str, flat: dict[str, dict[str, Any]]) -> tuple[Any, bool]:
    """when/cycle 基线：running 现值 → schema 声明 default → 双缺 = (None, False)
    （不可判/不可结算，调用方 fail-closed 跳过——不猜）。"""
    if key in running:
        return running[key], True
    decl = flat.get(key)
    if isinstance(decl, dict) and decl.get("default") is not None:
        return decl["default"], True
    return None, False


def _tick_when_met(when: dict[str, Any], running: dict[str, Any], flat: dict[str, dict[str, Any]]) -> bool:
    """结构化简单条件判定（全等函数不抛）：==/!= 全类型；>=/< 仅数值
    （bool 排除）或同型字符串，混型 = 不可判 = 未满足（fail-closed 跳过）。"""
    baseline, ok = _tick_baseline(running, str(when.get("key")), flat)
    if not ok:
        return False
    op = when.get("op")
    value = when.get("value")
    if op == "==":
        return baseline == value
    if op == "!=":
        return baseline != value
    if isinstance(baseline, bool) or isinstance(value, bool):
        return False
    if isinstance(baseline, (int, float)) and isinstance(value, (int, float)):
        return baseline >= value if op == ">=" else baseline < value
    if isinstance(baseline, str) and isinstance(value, str):
        return baseline >= value if op == ">=" else baseline < value
    return False


def _tick_cycle_next(values: list[Any], current: Any) -> Any:
    """enum cycle 推进一位：current 在值域内 = 下一位；不在（含缺席取 default
    后仍不在）= 从头循环 values[0]（确定性总函数——枚举循环是引擎事实）。"""
    try:
        index = values.index(current)
    except ValueError:
        index = -1
    return values[(index + 1) % len(values)]


def _tick_step_changes(
    step: dict[str, Any], running: dict[str, Any], flat: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """声明 effects → 本步结算 changes：op 串原样透传（Validator 同源）；
    cycle 算子按 running 基线解析为绝对值 ``=next``。基线不可得（键缺席且无
    default）= None（步不可结算，调用方跳过——不发明世界没有的状态）。"""
    changes: dict[str, Any] = {}
    for key, spec in step["effects"].items():
        if isinstance(spec, dict):
            baseline, ok = _tick_baseline(running, str(key), flat)
            if not ok:
                return None
            changes[str(key)] = f"={_tick_cycle_next(list(spec['cycle']), baseline)}"
        else:
            changes[str(key)] = spec
    return changes


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


async def _handle_game_tick(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/tick：世界演化周期推进（宿主/触发器直达管道接口，不经任务壳）。

    - 会话校验/单宿主租约与 game/operation 同闸；ticks 正整数缺省 1，上限
      ``_TICK_MAX_TICKS`` 钳制（防打爆）；
    - 无声明包 = 空转回执 ``{applied: true, ticks, advanced: false}`` 不 404
      （tick 是通用推进接口）；有声明但会话行无状态契约 = 400 fail-closed
      （无契约可结，兼容态透传会绕开 clamp）；
    - 逐 tick × 逐 step 顺序结算（声明序）：when 判定与 cycle 解析基线 =
      running（随 accepted 更新，后续步见前序结算结果）；每步一次
      state.adjudicate（Validator 单源）；被拒步不入 steps_fired、审计照记
      全流水（漂移可辨不静默，不劫持其余步的世界推进）；全部 tick 终了一次
      character_state.update 合并落账（键后者胜 = 序结算语义）；
    - 审计 default_source=tick、actor=narrator 缺省（世界演化），批标记行
      ``{key: "tick", to: {ticks, steps_fired}}`` summary 载 ticks 数与 fired
      steps——沉淀回路输入；idem 离散事件不幂等去重（operation 同口径）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    ticks = body.get("ticks")
    if ticks is None:
        ticks = 1
    if not isinstance(ticks, int) or isinstance(ticks, bool) or ticks < 1:
        return _game_routed({"error": f"ticks 须为正整数（缺省 1，上限 {_TICK_MAX_TICKS} 钳制），得 {ticks!r}"}, 400)
    clamped = min(ticks, _TICK_MAX_TICKS)
    rows = await _call_provider("pipeline-state")
    row = next(
        (
            r for r in rows
            if isinstance(r, dict)
            and str(r.get("pipeline_id") or "") == session_id
            and str(r.get("mode") or "") == "roleplay"
        ),
        None,
    )
    if row is None:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 tick"}, 409)
    # 会话绑定包解析（game/config 同源链）→ tick 声明归一（when 键域执法位）
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    steps: list[dict[str, Any]] = []
    if agent_id.startswith(prefix):
        card = next(
            (c for c in _load_cards() if c["id"] == agent_id[len(prefix):]), None
        )
        if card is not None:
            packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
            for index, (_, data, _dir) in enumerate(packs):
                normalized = _pack_tick_listing(
                    data, str(data.get("id") or f"#{index}")
                )
                if normalized:
                    steps = normalized  # 声明序后者非空清单整体覆盖（operations 同构）
    if not steps:
        return _game_routed({"applied": True, "ticks": clamped, "advanced": False})
    schema = row.get("context.rp_state_schema")
    if not isinstance(schema, dict):
        return _game_routed({
            "error": (
                "状态契约未装载（会话行无 context.rp_state_schema），"
                "tick 拒绝结算（fail-closed，无契约可结）"
            )
        }, 400)
    flat, _skipped = flatten_variables(schema)
    hard_rules = row.get("context.rp_state_hard_rules")
    card_id = agent_id[len(prefix):] if agent_id.startswith(prefix) else ""
    try:
        current_res = await _invoke_tool(
            "character_state.get", "character_state", {"card_id": card_id}
        )
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"tick 现值读取失败（character_state 不可用）: {exc}"}, 502)}
    if not (isinstance(current_res, dict) and current_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "tick 现值读取失败（character_state 回执异常）"}, 502)}
    running = (current_res.get("data") or {}).get("values")
    if not isinstance(running, dict):
        running = {}
    from _root_api import append_audit_rows, new_ulid  # noqa: PLC0415（共享根裸名，惰性同 import 面）

    workspace = await _resolve_workspace(session_id)
    audit_rows: list[dict[str, Any]] = []
    delta: dict[str, Any] = {}
    fired: list[int] = []
    settled_any = False
    for _tick_no in range(clamped):
        for step_index, step in enumerate(steps):
            when = step.get("when")
            if when is not None and not _tick_when_met(when, running, flat):
                continue  # 条件不满足跳过（不猜不阻断）
            changes = _tick_step_changes(step, running, flat)
            if changes is None:
                continue  # cycle 基线不可得：步不可结算（不发明状态）
            try:
                adj_res = await _invoke_tool("state.adjudicate", _ADJUDICATE_SERVICE_PLUGIN, {
                    "changes": changes,
                    "schema": schema,
                    "hard_rules": hard_rules if isinstance(hard_rules, dict) else None,
                    "current": running,
                })
            except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
                return {"success": True, "data": _json_response(
                    {"error": f"tick 结算失败（state.adjudicate 不可用）: {exc}"}, 502)}
            if not (isinstance(adj_res, dict) and adj_res.get("success") is True):
                return {"success": True, "data": _json_response(
                    {"error": "tick 结算失败（state.adjudicate 回执异常）"}, 502)}
            result = adj_res.get("data") or {}
            rejected = result.get("rejected")
            accepted = result.get("accepted")
            audit = result.get("audit")
            if not isinstance(rejected, list) or not isinstance(accepted, dict) or not isinstance(audit, list):
                return {"success": True, "data": _json_response(
                    {"error": "tick 结算回执形态非法（accepted/rejected/audit 缺失）"}, 502)}
            audit_rows.extend(audit)
            if rejected:
                continue  # 被拒步不入 fired（审计已照记全流水，漂移可辨）
            running.update(accepted)
            delta.update(accepted)
            fired.append(step_index)
            settled_any = True
    if settled_any:
        try:
            await _invoke_tool(
                "character_state.update", "character_state",
                {"card_id": card_id, "changes": delta},
            )
        except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
            return {"success": True, "data": _json_response(
                {"error": f"tick 落账失败（character_state 不可用）: {exc}"}, 502)}
    if audit_rows:
        idem = f"{session_id}:tick:{new_ulid()}"
        audit_rows.append({"key": _SOURCE_TICK, "op": None, "from": None,
                           "to": {"ticks": clamped, "steps_fired": fired}})
        append_audit_rows(workspace, audit_rows, idem, _SOURCE_TICK)
    if not settled_any:
        return _game_routed({"applied": True, "ticks": clamped, "advanced": False})
    return _game_routed({
        "applied": True,
        "ticks": clamped,
        "advanced": True,
        "state_delta": delta,
        "steps_fired": fired,
    })


@plugin.tool(
    name="mode.game.push_delta",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "RP 扮演会话 pipeline_id"},
            "turn_seq": {
                "type": ["integer", "null"],
                "description": "产生 delta 的尾条 assistant 消息 seq（对账序）",
            },
            "deltas": {
                "type": "array",
                "items": {"type": "object"},
                "description": "game.* 变更行（raw 值，宿主按自身规则解释）",
            },
        },
        "required": ["session_id", "deltas"],
    },
    description="游戏域 delta 入队（state_marker_parse 推送通道；无租约丢弃=Tier 0，队列上限丢最旧）",
    output_schema={
        "type": "object",
        "required": ["queued", "dropped"],
        "properties": {
            "queued": {"type": "integer"},
            "dropped": {"type": "integer"},
        },
    },
)
async def mode_game_push_delta(
    session_id: str, deltas: list[dict[str, Any]], turn_seq: int | None = None
) -> dict[str, Any]:
    """游戏域 delta 入队（state_marker_parse 落账步经 tool-executor 显式插件调用）。

    仅租约在场的会话入队（无宿主 = Tier 0，delta 无去处，如实 dropped 回执）；
    队列上限 _GAME_CHANNEL_CAP（宿主离线保护，超出丢最旧）。入参 fail-closed
    （ValueError 上行，调用方降级不阻断）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("缺少 session_id")
    if not _GAME_HOST_ID_RE.fullmatch(sid):
        raise ValueError(f"非法 session_id: {sid}")
    if not isinstance(deltas, list):
        raise ValueError("deltas 须为列表")
    if not deltas:
        raise ValueError("deltas 须非空（零 delta 轮不推送）")
    clean: list[dict[str, Any]] = []
    for item in deltas:
        if not isinstance(item, dict) or not str(item.get("key") or "").strip():
            raise ValueError("每条 delta 须为带非空 key 的对象")
        entry = dict(item)
        if turn_seq is not None:
            entry.setdefault("turn_seq", turn_seq)
        clean.append(entry)
    channel = _GAME_CHANNELS.get(sid)
    if channel is None or channel["lease"] is None:
        return {"queued": 0, "dropped": len(clean)}
    queue = channel["pending_deltas"]
    queue.extend(clean)
    dropped = 0
    while len(queue) > _GAME_CHANNEL_CAP:
        queue.pop(0)
        dropped += 1
    return {"queued": len(queue), "dropped": dropped}


# ── 声明聚合通道（RP 批 4-Ⅱ-核；template/ui 透出随批 4-Ⅲ）：GET /game/config ─────
#
# 会话绑定卡+pack 的 Tier 1 声明（rules / narrative_graph / template / ui /
# actions）聚合进向宿主透出的载荷，宿主引擎件（游戏页内 4a/4b/4c/4d/4e）按此
# 装载。游戏域读面自持聚合——装载侧 context.rp_state_* 管线（agent_config_load）
# 只承载 Tier 0 面，不感知本通道；无声明 = 键缺席（宿主引擎件空转 = Tier 0 兼容态）。

# pack 侧 Tier 1 声明键名：graph = 卡侧 narrative_graph 的包内名（rp_packs
# _DEFERRED_PACK_KEYS 契约名，packs/README 同口径）；template/ui 同为
# _DEFERRED_PACK_KEYS 契约名（批 4-Ⅲ 起启用消费，装载侧 warning 忽略随之解除面
# 归宿主）；rules/actions 与卡侧同名。pack 的 variables/behaviors（Tier 0）不进
# 本载荷（归装载侧管线）。


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


async def _handle_actions(path: str, method: str, raw_body: str) -> dict[str, Any] | None:
    """写动作分发；未命中返回 None（交回 404 路径）。"""
    if path not in _ACTION_ROUTES:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    body = _parse_body(raw_body)
    if path.endswith("/play"):
        return await _handle_play(body)
    if path.endswith("/regenerate"):
        # 已收编：变体生成在扮演会话内走宿主消息操作（重新生成/‹i·n› 多代
        # 切换），本动作面退役——保留 410 语义化响应而非 404，旧面板缓存可辨。
        return {"success": True,
                "data": _json_response({"error": "regenerate 已收编进扮演会话的宿主消息操作"}, 410)}
    if path.endswith("/cards/save"):
        return _routed_json(_save_card_request(body))
    if path.endswith("/cards/delete"):
        return _routed_json(_delete_card_request(body))
    if path.endswith("/cards/import"):
        return _routed_json(_import_card_request(body))
    if path.endswith("/cards/export"):
        return _routed_json(_export_card_request(body))
    if path.endswith("/lorebooks/save"):
        return _routed_json(_save_lorebook_request(body))
    if path.endswith("/lorebooks/delete"):
        return _routed_json(_delete_lorebook_request(body))
    if path.endswith("/lorebooks/import"):
        return _routed_json(_import_lorebook_request(body))
    if path.endswith("/personas/save"):
        return _routed_json(_persona_save_request(body))
    if path.endswith("/combos/save"):
        return _routed_json(_combo_save_request(body))
    if path.endswith("/combos/delete"):
        return _routed_json(_combo_delete_request(body))
    if path.endswith("/group/scene_turn"):
        # 场景批一次调用编排（缺口批次四）：校验分诊显式（缺参/会话/卡库/可见性
        # 400，模型档/LLM 传输面 502），不走 _routed_json 两档映射——沉淀域段
        # 元组先例同款理由（状态语义多档，映射面表达不了）。
        return await _handle_scene_turn(body)
    if path.endswith("/precipitation/promote"):
        # 审核闸固化（缺口批次三）：显式调用才写用户层玩法包——人工审核即端点
        # 语义；状态分诊显式（校验 400/包不存在 404/作者面冲突 409），draft 域
        # 元组先例同款，不走 _routed_json 两档映射。
        payload, status = _precipitation_promote_request(body)
        return {"success": True, "data": _json_response(payload, status)}
    return _routed_json(_persona_delete_request(body))


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/mode_roleplay/** (面板页+游戏页+数据面+写动作+游戏域通道)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：面板页 HTML / 数据端点 / 写动作；未路由返回 404 JSON。"""
    query = query or {}
    if path == _PANEL_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _panel_html_response()}
        except OSError as exc:
            return {
                "success": False,
                "error": f"panel html read failed: {exc}",
                "data": _json_response({"error": "panel html unavailable"}, 500),
            }
    if path == _GAME_PAGE_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _game_page_html_response()}
        except (OSError, RuntimeError) as exc:
            return {
                "success": False,
                "error": f"game page html unavailable: {exc}",
                "data": _json_response({"error": "game page html unavailable"}, 500),
            }
    if path == _GAME_STATE_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_state(str(query.get("session_id") or ""))
    if path == _GAME_CONFIG_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_config(str(query.get("session_id") or ""))
    if path == _GAME_PAGE_DATA_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        # 双通道（刀4）：session_id=（刀1 契约零变化）| pack=（setup 通道，会话
        # 未开按包 id 直取页）；两参都缺 = 400（session 通道缺参语义同现状）。
        return await _handle_game_page(
            str(query.get("session_id") or ""), str(query.get("pack") or "")
        )
    if path in (_GAME_ATTACH_ROUTE, _GAME_SNAPSHOT_ROUTE, _GAME_DETACH_ROUTE, _GAME_TURN_ROUTE):
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        body = _parse_body(raw_body)
        if path == _GAME_ATTACH_ROUTE:
            return await _handle_game_attach(body)
        if path == _GAME_SNAPSHOT_ROUTE:
            return await _handle_game_snapshot(body)
        if path == _GAME_TURN_ROUTE:
            return await _handle_game_turn(body, headers)
        return await _handle_game_detach(body)
    if path == _GAME_OPERATION_ROUTE:
        # 自由操作→结构化命令（世界内嵌批次二）：Propose→Adjudicate，与 game/turn
        # 同闸（会话校验+单宿主租约）；独立分支不走 _ACTION_ROUTES（游戏域通道族）。
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_operation(_parse_body(raw_body))
    if path == _GAME_TICK_ROUTE:
        # 世界演化周期推进（世界内嵌批次三）：直达管道接口不经任务壳，与
        # game/operation 同闸（会话校验+单宿主租约）；游戏域通道族独立分支。
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_tick(_parse_body(raw_body))
    if path in _DATA_ROUTES and method == "GET":
        if path == _PRECIP_CANDIDATES_ROUTE:
            # 沉淀候选（缺口批次三）：会话审计流水复盘统计，读面无副作用；
            # session_id 必填（缺参 400 显式，与 messages 同款），未知会话/无
            # 流水 = 空列表（200，fail-soft 非错误）。
            session_id = str(query.get("session_id") or "").strip()
            if not session_id:
                return {"success": True, "data": _json_response({"error": "缺少 session_id"}, 400)}
            workspace = await _resolve_workspace(session_id)
            return {
                "success": True,
                "data": _json_response({
                    "candidates": precipitation_candidates(_precipitation_stream(workspace))
                }),
            }
        if path == _WIZARD_AXES_ROUTE:
            # 四轴候选集（C2 整合刀）：文风轴动态 + 三静态考据轴，读面无副作用
            return {"success": True, "data": _json_response({"axes": _wizard_axes_listing()})}
        if path == _PACKS_ROUTE:
            # 玩法包清点独立端点（面板数据驱动刀）：双根载荷（出厂∪用户层，同 id
            # 接管），fail-soft 目录缺失=空列表；读面无副作用。packs_dir 显式传
            # 全局（axes 的 _STYLES_DIR 同款：调用时读，测试可 monkeypatch 注入）。
            # RP 面板 v3.1：一并返回自定义玩法 combos（刀2 少一枪）。
            return {
                "success": True,
                "data": _json_response({
                    "packs": _load_packs_listing(packs_dir=_PACKS_DIR),
                    "combos": _load_combos(),
                }),
            }
        if path.endswith("/bootstrap"):
            profile = load_profile()
            payload = {
                "mode": profile["mode"],
                "name": profile["name"],
                "chain": profile.get("chain"),
                "panel_page_id": profile.get("panel_page_id"),
                # 玩法包清点（RP 面板刀-Ⅰ）：向导玩法勾选页数据源，聚合进既有
                # 端点零新增（出厂播种两件 + 用户层合并展示）
                "packs": _load_packs_listing(),
            }
            return {"success": True, "data": _json_response(payload)}
        if path.endswith("/sessions"):
            rows = await _call_provider("pipeline-state")
            sessions = _norm_sessions(rows)
            # ?pack=<id> 过滤（RP 面板 v3.1「继续上局」按玩法归属）：pack_ids
            # 命中即留；无过滤参数 = 全量（现状零变化）。
            pack_filter = str(query.get("pack") or "").strip()
            if pack_filter:
                sessions = [s for s in sessions if pack_filter in s["pack_ids"]]
            return {"success": True, "data": _json_response({"sessions": sessions})}
        if path.endswith("/cards"):
            return {"success": True, "data": _json_response({"cards": _load_cards()})}
        return {"success": True, "data": _json_response({"lorebooks": _load_lorebooks()})}
    if path == "/ext/mode_roleplay/data/personas":
        # GET 端点但在 _DATA_ROUTES 集合外——messages 端点同款独立分发形态
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return {"success": True, "data": _json_response({"personas": _personas_load()})}
    if path == "/ext/mode_roleplay/data/messages":
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        pipeline_id = str(query.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        rows = await _call_provider("messages", pipeline_id=pipeline_id)
        return {"success": True, "data": _json_response({"messages": _norm_messages(rows)})}
    if path == _DRAFTS_ROUTE or path.startswith(_DRAFT_ROUTE_PREFIX):
        routed = await _handle_draft_routes(path, method, raw_body, query, headers)
        if routed is not None:
            return routed
    routed = await _handle_actions(path, method, raw_body)
    if routed is not None:
        return routed
    if path == "/ext/mode_roleplay/pipeline/open" and method == "POST":
        # B8 管道标签（2026-09-29）：向既有会话开通扮演子管道——薄代理内核
        # chat.send_message 创建分支（create:true + thread_id 归属既有会话，
        # 内核测试 create_with_ownership_thread_links_real_session 钉死）。
        # 职责就地（用户裁定）：面板直调本插件端点，不设独立代理插件。
        # body 解码走 _parse_body 单点（内核 base64 契约 + 裸 JSON 双形态，
        # 与 _handle_actions 同源）；非法可辨（_BODY_INVALID）→ 400 显式拒绝。
        req = _parse_body(raw_body, invalid=_BODY_INVALID)
        if req is _BODY_INVALID:
            return {"success": True, "data": _json_response({"error": "请求体非法 JSON"}, 400)}
        session_id = str(req.get("session_id", "") or "").strip()
        if not session_id:
            return {"success": True, "data": _json_response({"error": "session_id 必填"}, 400)}
        agent_id = str(req.get("agent_id", "") or "").strip()
        # user_id 从请求 token 解（内核转发 /ext 带全量 headers；token 载荷
        # `access:<uid>:<username>:<exp>:<jti>:<h>` 自包含 uid）——chat.send_message
        # 契约 user_id 非空，缺失即 401 如实拒绝（不猜默认用户）。
        user_id = _user_id_from_headers(headers)
        if not user_id:
            return {"success": True, "data": _json_response({"error": "未认证：token 缺失或无效"}, 401)}
        args: dict[str, Any] = {
            "create": True,
            "thread_id": session_id,
            "message": str(req.get("first_message", "") or ""),
            "user_id": user_id,
            "pipeline_config_id": "roleplay",
        }
        # 玩法级管道绑定（世界内嵌谱系右移批次一，调研 §6.4）：显式携带
        # pipeline_config_id 时过登记存在性闸——未登记名 fail-closed 回落缺省
        # roleplay + 响应 pipeline_warning（错误面可见），派发照常（会话出生
        # 绝不失败；内核对未知名本就回落空管道，闸门把回落收拢到契约缺省）；
        # 不携带 = 现役 roleplay 路径零变化。
        requested_pipeline = str(req.get("pipeline_config_id", "") or "").strip()
        pipeline_warning = ""
        if requested_pipeline and requested_pipeline in _registered_pipeline_names():
            args["pipeline_config_id"] = requested_pipeline
        elif requested_pipeline:
            pipeline_warning = (
                f"pipeline_config_id {requested_pipeline!r} 未登记"
                "（config/pipelines 无此名），回落缺省 roleplay"
            )
            logger.warning("[mode_roleplay] pipeline/open %s", pipeline_warning)
        if agent_id:
            # run 执行身份 = 管道 state 持久键 agent.id（出生方经 state 透传，
            # context_build 据此加载卡 agent yaml；顶层 agent_id 只是派发簿记）
            args["state"] = {"agent.id": agent_id}
        try:
            cap = plugin.get_capability("chat")
            from agentos_plugin_sdk.capability import bind_capability_caller

            res = await bind_capability_caller(cap, "chat")(
                "chat.send_message", args, timeout=25.0
            )
        except Exception as exc:  # noqa: BLE001 — 薄代理：capability 失败原样透传
            return {"success": True, "data": _json_response({"error": f"管道开通失败: {exc}"}, 502)}
        if not isinstance(res, dict) or res.get("status") != "created":
            return {
                "success": True,
                "data": _json_response({"error": f"未走创建分支: {str(res)[:200]}"}, 502),
            }
        pipeline_id = str(res.get("pipeline_id", "") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "回执缺 pipeline_id"}, 502)}
        return {
            "success": True,
            "data": _json_response({
                "pipeline_id": pipeline_id,
                "thread_id": session_id,
                "status": "created",
                "pipeline_config_id": str(args["pipeline_config_id"]),
                # 键缺席式警示面：显式未登记名回落缺省时才带（bind 侧校验线，
                # 错误面可见——前端据此通知，派发本身不受阻）
                **({"pipeline_warning": pipeline_warning} if pipeline_warning else {}),
            }),
        }
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


if __name__ == "__main__":
    plugin.run()
