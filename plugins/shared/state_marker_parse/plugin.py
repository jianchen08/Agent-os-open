"""状态统一标记解析步（通用输出管道步骤，2026-09-28 设计）。

设计真值 docs/working/角色状态统一标记机制设计_20260928.md §2/§3：
- 提取本轮 assistant 回复（state["messages"] 尾条 role=assistant）的
  `<state>` 标记（语法/区间单源在共享平铺模块 state_marker.py）；
- **账本白名单分发**（键 = 目标账本，未知键丢弃 + warning——fail-closed 不
  透传）：`state` → character_state.update（card_id 取自 state["agent.id"]
  卡键尾段；非卡键 = 状态账本不落账，warning 降级）；`memory` → memory 工具
  store（action=store）。跨插件调用经 tool-executor.invoke 显式 plugin_id；
  调用失败 = 该键留痕跳过，其余键继续；
- **render 回写**：每轮调 character_state.render(card_id) 写
  `context.character_state_text`——供下轮动态变量消息注入（消息尾部形态，
  system_prompt 前缀不动，缓存零损失）；无标记也回写（首轮初始状态）；
- **写 `context.state_updates`**：{entries（落账成功的账本键载荷）, span,
  ts}——前端 pull-from-state 渲染状态小卡并按 span 隐藏标记原文；无标记 =
  不写该键（零渲染语义）。
- **AI 输出 regex 脚本（RP 批 1a）**：state 携带 `context.regex_scripts`
  （agent_config_load 按卡装载）时，落账前应用 affects=AI Response 脚本
  （引擎 = 卡所属模式包约定模块 regex_scripts.py，按 agent.id 模式键双根
  解析，约定发现零模式特判）；替换文本经 messages set op 落账，标记在脚本后
  文本上解析（span 自洽）；变量变更 warning 记录不落账（批 2 Validator
  收编前临时桥）。无脚本卡零行为变化。
- **State Contract Validator（RP 批 2-Ⅰ 2b）**：state 账本键变更对落账前过
  `state_contract.validate_and_apply`（op/类型/clamp/hard_rules，契约见该
  模块 docstring）——schema 源 = `context.rp_state_schema`（卡
  extensions.variables，agent_config_load 装载）、规则源 =
  `context.rp_state_hard_rules`（卡 extensions.rules.hard_rules）。
  schema=None（无声明卡/出厂卡）= 全 accepted 逐字透传，兼容态零行为变化。
  accepted → character_state.update（既有账本写面，本步仍是唯一落账通道——
  LLM 永不直写状态，schema 外键 rejected 即执法）；rejected 进载荷
  `context.state_updates.rejected`（含 reason，前端状态卡可展开），全
  accepted 时不写该键（载荷零新增）。
- **动作驱动基线（2026-10-13 ADR，翻案批 0a①）**：状态变化只走两条作者声明
  通道，LLM 不发明状态增量。①**动作通道（主）**：`context.rp_actions`
  （卡 extensions.actions，agent_config_load 装载 + target 面 lint 归一）
  声明 typed 清单，选取面 = 本轮玩家输入（尾条 user 消息 trim）与声明
  id/name 全等——与游戏页 choices 点击「文本入输入区+自动发送」既有约定同
  构，全等之外零触发（fail-closed）；target=engine 声明 effect（op 语法同
  behaviors）与 behaviors 效果同键同轮合并经 Validator（结算序：动作先、
  标记[回退开启时]次之、behaviors 后，「缺省取最后」= 序列后者胜绝对/叠加
  相对）；target=game 转发宿主（Tier 0 形态 = 宿主 delta 待发队列，桥面
  actions_for_game 随 game_bridge 扩展波），不进 story 账本。②**行为触发
  通道（辅）**：见下节。零标记零动作仍是常态合法形态。
- **会话首轮 init effects（RP 面板 v3.1 刀1，动作驱动基线同构）**：
  `context.rp_init_effects`（agent_config_load 解析 execution_context.rp_init
  快照串归一）对 Validator 全套结算，**结算序 init 最先**（先于动作/标记/
  behaviors）；行级审计 source=init，幂等闸 = 审计文件会话级 idem
  `{pipeline_id}:init`（同轮重放/重启重入只生效一次）；ws 不可得 = fail-closed
  跳过（一次性增量防每轮重放复利放大）；键域不在卡+包 variables 声明内 =
  rejected + 审计 fail-closed。无 rp_init_effects = 现状零变化（兼容态）。
- **`<state>` 尾标记收窄为 memory 通道 + 回退开关（2026-10-13 ADR）**：
  `memory` 键照旧进账本；`state` 键由卡 `engine.allow_state_proposals`
  门控（默认关，读取口径同 allow_cheat 声明链，非映射/非布尔 = 缺席默认关
  fail-closed）——**默认关**：解析层忽略（不进 Validator 不落状态账，对用户
  不可见），审计流水记一行 `source=state_field_ignored`（原样载荷摘要，截断
  上限 STATE_FIELD_SUMMARY_MAX_CHARS；并入 story 批幂等空间；作弊轮行级来源
  不被缺省 cheat 覆盖）；**显式 true**：恢复 LLM 提议合法来源，走 Validator
  全套（path/类型/clamp/hard_rules 照旧执法），审计缺省来源=llm（作弊轮仍
  =cheat）。坏 JSON fail-soft 不变，绝不报错中断回合。
- **behaviors 声明层（RP 批 2-Ⅱ 2c）**：state 携带 `context.rp_state_behaviors`
  （卡 extensions.behaviors ∪ pack 合并，agent_config_load 装载）时，对标记
  剥离后正文（strip_by_span 产物；无标记 = 全文）逐 behavior 跑 trigger
  regex，命中产出代理变更（来源=behavior）与动作声明效果**合并进同一
  validate_and_apply 结算**——同键同轮结算序见动作驱动基线节。匹配不依赖
  标记在场（无标记轮命中照常落账+审计，载荷仍随标记形态——span 承载前端
  隐藏区间，无标记零渲染语义不变，动作结算同款）。fail-soft：坏 trigger
  regex / 声明形态非法 = 该 behavior 跳过 warning；无 behaviors 声明 = 零
  动作（兼容态零行为变化）。learned_behaviors / 写规则 LLM 后置登记。
- **拒绝回灌 + 近况事件（缺口批次一+二，§7.5 #2/#5/#6）**：下轮 LLM 动态
  注入源 `context.character_state_text` 在 render 文本后并入两节（空节不
  出现，纯文本轮逐字等于 render）——①「上轮被拒变更」：本轮 state 账本
  rejected 摘要（layer/reason/键，一行一条，最近 8 条，行级截断），layer
  分层单源 state_contract（hard=契约级不可商量 / soft=值域类可申诉）；②
  「近期事件」：审计流水近 K=8 条事件（rp_state_audit 写面补 actor/action/
  target/summary，append 先于本读取故同轮变更入窗口），ws 不可得 = 空节。
  该出口前端消费面（状态摘要）只增不删。
- **状态审计（RP 批 2-Ⅰ，§5.3 全流水）**：state 账本键落账成功后，把
  accepted + rejected 全流水 append 会话工作空间 `<ws>/rp_state_audit.jsonl`
  （每行：ts / source（缺省由落账步补：作弊轮=cheat、回退开启=llm、否则
  state_marker；行为行 source=behavior、动作行 source=action、state 字段
  忽略行 source=state_field_ignored 行级随行）/ idem / key / op /
  from→to（rejected 记提议原值）/ reason）。
  ws = `state["workspace"]`（workspace_lifecycle 写入的执行空间）；不可得或
  IO 失败 = warning 跳过审计，不阻断落账与对话。**追加幂等**：idem =
  ``{pipeline_id}:{尾条 assistant 消息 seq}``，append 前扫文件已含同 idem =
  整批跳过（同轮重放不双记；契约前提 = 同轮重放携带同 pipeline_id 与消息
  seq，与落账步现状幂等水位一致）。pipeline-state 键只留最新值（审计是增量
  文件，state 只存索引裁定）。

降级契约：任何失败（无卡键/服务失败/坏 JSON）→ warning + 不阻断管道；
解析只读 messages 尾条（regex 脚本在场时改写经 messages set op 落账，见上节；
无脚本卡原文不动；展示剥离归前端渲染层）。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from _libs.card_contract import keep_state_machine_spec
from _libs.pipeline_base.plugin import IOutputPlugin, OutputResult, PluginContext
from _root_api import (
    append_audit_rows,
    find_mode_package_dir as _find_mode_package_dir,
    has_idem,
    last_assistant_seq,
    parse_mode_agent_key as _parse_mode_agent_key,
    recent_events,
    story_idem,
    suffixed,
)
from state_contract import ChangeItem, validate_and_apply
from state_marker import parse_state_marker, strip_by_span

logger = logging.getLogger(__name__)

# 账本白名单（键 → 处理器名）；未知键 = 丢弃 + warning（fail-closed 不透传）。
KNOWN_LEDGER_KEYS = frozenset({"state", "memory"})

# ── game 域分域（RP 批 4-Ⅰ，双域分治 设计文档 §二/§5.2）──────────────────────────
# `game.` 前缀键 = 游戏域：宿主权威——不入 story 账本（character_state），改产出
# delta 推宿主（宿主按自身规则执行/修订后回推快照）；后端薄 sanity + 审计，
# 不 clamp 不解释 op（与 story Validator 相反的权威方向）。无 game.* 键 = 零
# 变化（Tier 0 兼容态）。
GAME_KEY_PREFIX = "game."
# delta 落点三面：state 键 game_host_delta（持久面只留最新轮，manifest
# persistent_fields/export_fields 声明）+ 宿主模式包进程内待发队列
# （mode.game.push_delta 服务推送，GET /game/state 取后清）+ 审计流水。
# 推送目标插件 ID 按 agent 键命名空间派生（`mode_X/<stem>` → `mode_X`，通用
# 步骤零模式特判——新游戏宿主模式包零包外改动即接入）；非模式命名空间 =
# 无宿主可推，走推送失败同款降级（warning + state 键 + 审计留痕）。
GAME_DELTA_STATE_KEY = "game_host_delta"
GAME_DELTA_PUSH_TOOL = "mode.game.push_delta"
# 薄 sanity 上限（拒绝+审计，不 clamp 不截断——宿主权威，后端不否决内容）。
GAME_DELTA_MAX_CHARS = 2048  # 单条 delta 值序列化长度上限
GAME_DELTA_ROUND_CAP = 32  # 单轮转发条数上限（超出拒绝+审计）
SOURCE_GAME_DELTA = "game_delta"
REASON_GAME_DELTA = "game_delta"
REASON_GAME_DELTA_TOO_LARGE = "game_delta_too_large"
REASON_GAME_DELTA_NOT_SERIALIZABLE = "game_delta_not_serializable"
REASON_GAME_DELTA_OVERFLOW = "game_delta_overflow"

# ── 动作驱动基线（2026-10-13 ADR）：来源与 state 字段回退开关 ─────────────────
# 变更来源枚举（审计归因）：动作行 source=action；回退开启时标记 state 字段行
# 缺省 source=llm（作弊轮仍缺省 cheat，落账步 _append_audit 决定）；行为行
# source=behavior（批 2-Ⅱ 既有）。state 字段默认关（回退开关缺席）时解析层
# 忽略，唯一痕迹 = 审计行 source=state_field_ignored（原样载荷摘要，对用户
# 不可见——不进 Validator 不落账不上卡）。
SOURCE_LLM = "llm"
SOURCE_ACTION = "action"
SOURCE_STATE_FIELD_IGNORED = "state_field_ignored"
REASON_STATE_FIELD_IGNORED = "state_field_ignored"
# state 字段忽略审计行的载荷摘要截断上限（原样摘要可读不吞真，防超长 JSON 撑爆流水行）。
STATE_FIELD_SUMMARY_MAX_CHARS = 512

# ── 会话首轮 init effects（RP 面板 v3.1 刀1；动作驱动基线同构——开演增量也
# 只走作者声明通道，LLM 不发明）───────────────────────────────────────────────
# 源 = ``context.rp_init_effects``（agent_config_load 解析 execution_context.rp_init
# 快照串归一，契约见其 RP_INIT_KEY 常量注释）。结算序 init 最先（先于动作/标记/
# behaviors），行级 source=init；键域不在卡+包 variables 声明内 = rejected + 审计
# （fail-closed）。幂等闸 = 审计文件会话级 idem ``{pipeline_id}:init``（不带 seq
# ——会话作用域：同轮重放、重启重入同 idem 幂等；探针 rp_state_audit.has_idem 与
# 写面 append_audit_rows 同空间）。ws 不可得 = fail-closed 跳过（一次性增量若
# fail-open 每轮重放会复利放大，宁可诚实缺席；审计降级 warning 留痕）。
# 无 rp_init_effects = 现状零变化（兼容态）。
SOURCE_INIT = "init"

# ── 拒绝回灌 + 近况事件（缺口批次一+二：§7.5 #2/#5/#6）───────────────────────
# 下轮 LLM 动态注入源 = context.character_state_text（卡 dynamic_vars 占位符
# {{state:context.character_state_text}} 每轮取新鲜值）。本轮 rejected 摘要
# （layer/reason/键，一行一条）与近 K 条事件（审计流水事件面）并入该出口——
# 无原因反馈 LLM 会反复撞墙（设计真值 §7.1 支柱 4）。两节任一为空即不出现：
# 无拒绝且无事件可读的轮次该出口与纯 render 文本逐字相等（前端消费面只增不删）。
REJECT_FEEDBACK_HEADER = "上轮被拒变更："
REJECT_FEEDBACK_MAX_LINES = 8  # 最近 8 条（超出丢最早——与事件窗口同量级）
EVENT_FEEDBACK_HEADER = "近期事件："
EVENT_FEEDBACK_MAX_ROWS = 8  # §7.5 #5 的 K=8（读取面缺省同值）
FEEDBACK_LINE_MAX_CHARS = 160  # 注入行级截断保护（超长键/摘要不撑爆注入面）


async def _noop_caller(_tool_name: str, _plugin_id: str, _args: dict) -> dict[str, Any]:
    raise RuntimeError("tool-executor caller 未注入")


class StateMarkerParsePlugin(IOutputPlugin):
    """状态标记解析输出插件（通用步骤，零模式特判）。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        invoke_caller: Callable[[str, str, dict], Any] | None = None,
    ) -> None:
        self._config = config or {}
        # tool-executor 调用句柄（async (tool_name, plugin_id, args) -> dict）；
        # 生产侧由 server.py 经 plugin.get_capability 注入，测试侧注入 fake。
        self._invoke = invoke_caller or _noop_caller
        # 最近载荷内存态（pipeline_id → state_updates）：前端状态小卡的拉取源
        # （GET /ext/state_marker_parse/latest）。pipelineStates 是内核白名单
        # 裁剪视图不透出该键，零内核边界下端点拉取是唯一通道；重启丢内存态 =
        # 卡消失（载荷本为本轮瞬态，可接受）。
        self._latest: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "state_marker_parse"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        try:
            return OutputResult(state_updates=await self._do_work(ctx.state))
        except Exception as exc:  # noqa: BLE001 — 降级语义：解析失败不阻断管道
            logger.warning("[state_marker_parse] 执行失败，零写入直通 | err=%s", exc)
            return OutputResult(state_updates={})

    async def _do_work(self, state: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        text = self._last_assistant_text(state)
        # AI 输出 regex 脚本（RP 批 1a）：落账前应用，标记在脚本后文本上解析
        # （span 与落账消息自洽）；无脚本卡原文直通零行为变化。
        text, regex_op = self._apply_regex_scripts(state, text)
        marker = parse_state_marker(text) if text else None
        card_id = self._resolve_card_id(state)
        # behaviors 代理变更（RP 批 2-Ⅱ 2c）：标记剥离后正文（无标记 = 全文）
        # 逐 behavior 跑 trigger；无 behaviors 声明 = 零动作（兼容态）。
        matched_text = (
            strip_by_span(text, marker["span"]) if marker is not None and text else (text or "")
        )
        behavior_changes = self._match_behaviors(state, matched_text)
        # 动作通道（2026-10-13 动作驱动基线）：声明清单全等选取 → 声明效果；
        # 独立于 state 字段回退开关（默认形态）。
        action_story, action_game = self._match_actions(state)
        # 会话首轮 init effects（RP 面板 v3.1 刀1）：结算序 init 最先——先落账
        # 再进主结算（主结算 current_values 读到 init 后现值，同键叠加语义正确）；
        # 幂等闸内只生效一次。无 rp_init_effects = 零动作（兼容态零行为变化）。
        init_changes = self._init_changes(state)
        if init_changes and card_id:
            try:
                await self._apply_init_effects(state, card_id, init_changes)
            except Exception as exc:  # noqa: BLE001 — init 结算失败不拖垮本轮其余管道
                logger.warning(
                    "[state_marker_parse] init effects 结算失败，跳过 | err=%s", exc
                )

        entries: dict[str, Any] = {}
        span: list[int] | None = None
        rejected: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []
        state_payload: Any = None
        if marker is not None:
            span = marker["span"]
            for key, payload in marker["entries"].items():
                if key not in KNOWN_LEDGER_KEYS:
                    logger.warning(
                        "[state_marker_parse] 未知账本键已丢弃（白名单外）| key=%s", key
                    )
                    continue
                if key == "state":
                    # state 推迟：回退开关裁决后与动作/behaviors 合并结算（2026-10-13）
                    state_payload = payload
                    continue
                try:
                    entries[key], key_rejected, key_audit = await self._dispatch(
                        key, payload, card_id, state
                    )
                    rejected.extend(key_rejected)
                    audit_rows.extend(key_audit)
                except Exception as exc:  # noqa: BLE001 — 单键失败不拖垮其余键
                    logger.warning(
                        "[state_marker_parse] 账本落账失败，跳过 | key=%s | err=%s",
                        key,
                        exc,
                    )

        if state_payload is not None and not self._allow_state_proposals(state):
            # 回退开关默认关：state 字段解析忽略（不进 Validator 不落账，对用户
            # 不可见）；唯一痕迹 = 审计一行（原样载荷摘要）。绝不报错中断回合。
            audit_rows.append(self._state_field_ignored_row(state_payload))
            state_payload = None
        state_payload_dict = state_payload if isinstance(state_payload, dict) else None
        if state_payload is not None and state_payload_dict is None:
            logger.warning(
                "[state_marker_parse] state 账本载荷须为 JSON 对象，标记载荷已忽略"
                "（type=%s）",
                type(state_payload).__name__,
            )
        # game 域分域（RP 批 4-Ⅰ）：标记载荷与 behaviors 代理变更中的 game.* 键
        # 分流为宿主 delta（薄 sanity + 审计 + 推送 + game_host_delta state 键），
        # story 侧只余其余键；动作 game 落点选取随批转发（序见 _split_game_changes）。
        # 无 game.* 键且无 game 落点选取 = 分流零产出（Tier 0 兼容态零行为变化）。
        game_changes = self._split_game_changes(state_payload_dict, behavior_changes, action_game)
        story_payload = (
            {k: v for k, v in state_payload_dict.items() if not k.startswith(GAME_KEY_PREFIX)}
            if state_payload_dict is not None
            else None
        )
        story_behaviors = [
            change for change in behavior_changes if not change[0].startswith(GAME_KEY_PREFIX)
        ]
        if state_payload_dict is not None or story_behaviors or action_story:
            try:
                entries["state"], state_rejected, state_audit = await self._dispatch_state(
                    story_payload, story_behaviors, card_id, state, action_story,
                )
                rejected.extend(state_rejected)
                audit_rows.extend(state_audit)
            except Exception as exc:  # noqa: BLE001 — 结算失败不拖垮其余键/render
                logger.warning(
                    "[state_marker_parse] state 账本结算失败，跳过 | err=%s", exc
                )
        if game_changes:
            try:
                game_delta = await self._forward_game_deltas(game_changes, state)
                if game_delta is not None:
                    updates[GAME_DELTA_STATE_KEY] = game_delta
            except Exception as exc:  # noqa: BLE001 — 分域失败不拖垮 story 侧
                logger.warning("[state_marker_parse] game delta 转发失败，跳过 | err=%s", exc)

        # 审计全流水（accepted + rejected）append 会话工作空间（降级不阻断）
        if audit_rows:
            self._append_audit(state, audit_rows)

        # render 回写（每轮，无标记也回写——首轮初始状态注入；非卡键零动作）。
        # 注入源并入拒绝回灌与近况事件节（空节不出现，纯文本轮逐字等于 render）。
        if card_id:
            try:
                rendered = await self._invoke(
                    "character_state.render",
                    "character_state",
                    {"card_id": card_id},
                )
                state_text = self._service_text(rendered)
                if state_text:
                    feedback = self._state_feedback(state, rejected)
                    updates["context.character_state_text"] = (
                        f"{state_text}\n{feedback}" if feedback else state_text
                    )
            except Exception as exc:  # noqa: BLE001 — 降级语义：渲染失败不阻断
                logger.warning(
                    "[state_marker_parse] 状态渲染回写失败 | card_id=%s | err=%s",
                    card_id,
                    exc,
                )

        if marker is not None and (entries or rejected):
            # 全部键落账失败且无 rejected（entries/rejected 均空）= 无可渲染内容
            # → 不写载荷（前端零渲染语义）；rejected 非空即载荷出（状态卡可展开）
            if self._cheating(state):
                # /cheat 徽标载荷（RP 批 3）：entries 整体经宿主 hook 原样透传，
                # state_card.html 读 entries.meta.cheating 渲染红色徽标；off 后
                # 不再写入即消失。meta 只进载荷不进账本（display-only 键）。
                entries["meta"] = {"cheating": True}
            payload = {
                "entries": entries,
                "span": span,
                "ts": datetime.now(UTC).isoformat(),
            }
            if rejected:
                payload["rejected"] = rejected
            updates["context.state_updates"] = payload
            pipeline_id = str(state.get("pipeline_id", "") or "")
            if pipeline_id:
                self._latest[pipeline_id] = payload
        if regex_op is not None:
            updates["messages"] = {"_ops": [regex_op]}
        return updates

    def _state_feedback(
        self, state: dict[str, Any], rejected: list[dict[str, Any]]
    ) -> str:
        """下轮动态注入源追加块：拒绝回灌节 + 近况事件节（两节独立，空节不出现）。

        - 拒绝节（缺口批次二 §7.5 #2）：本轮 state 账本 rejected 摘要
          （layer/reason/键），一行一条，取最近 REJECT_FEEDBACK_MAX_LINES 条
          （超出丢最早）——契约级 hard 不可商量、值域类 soft 可申诉，分层随行；
        - 事件节（§7.5 #5）：审计流水近 K 条事件（rp_state_audit 写面补的
          actor/action/target/summary），一行一条；ws 不可得/文件缺失 = 空节
          （诚实缺席，降级不阻断）。审计 append 先于本读取（同轮变更入窗口）。
        """
        sections: list[str] = []
        reject_lines = [
            self._feedback_line(f"- [{row.get('layer')}|{row.get('reason')}] {row.get('key')}")
            for row in rejected[-REJECT_FEEDBACK_MAX_LINES:]
        ]
        if reject_lines:
            sections.append("\n".join([REJECT_FEEDBACK_HEADER, *reject_lines]))
        workspace = state.get("workspace")
        if isinstance(workspace, str) and workspace:
            events = recent_events(workspace, EVENT_FEEDBACK_MAX_ROWS)
            event_lines = [
                self._feedback_line(f"- {event['summary']}")
                for event in events
                if event.get("summary")
            ]
            if event_lines:
                sections.append("\n".join([EVENT_FEEDBACK_HEADER, *event_lines]))
        return "\n".join(sections)

    @staticmethod
    def _feedback_line(line: str) -> str:
        """注入行级截断保护（超长键/摘要以省略号收尾，行有界）。"""
        if len(line) > FEEDBACK_LINE_MAX_CHARS:
            return line[:FEEDBACK_LINE_MAX_CHARS] + "…"
        return line

    def _apply_regex_scripts(
        self, state: dict[str, Any], text: str
    ) -> tuple[str, dict[str, Any] | None]:
        """AI 输出 regex 脚本应用（RP 批 1a；无脚本卡零行为变化）。

        脚本源 = ``state["context.regex_scripts"]``（agent_config_load 按卡
        yaml extensions 装载，既有 context.* 通道）；引擎 = 卡所属模式包约定
        模块 ``<pkg>/regex_scripts.py``（按 agent.id 模式键经 mode_keys 双根
        解析——通用步骤零模式特判，约定发现机制同模式包 agent 注册表；包缺
        引擎模块/非模式键 = 合法形态零动作）。

        应用 affects=AI Response 脚本（depth=0 当前消息）：替换文本经 messages
        set op 落账（ephemerality 两态本刀统一持久，语义区分见引擎模块契约），
        span 在落账文本上计算故自洽；变量变更 **warning 记录不落账**——批 2
        Validator 收编前的临时桥（引擎 apply_scripts 契约注释同源）。fail-soft：
        引擎缺失/坏脚本/无 seq/非字符串内容 = 原文直通不阻断。
        """
        if not text:
            return text, None
        scripts = state.get("context.regex_scripts")
        if not isinstance(scripts, list) or not scripts:
            return text, None
        engine = self._load_regex_engine(state)
        if engine is None:
            return text, None
        try:
            new_text, var_changes = engine.apply_scripts(
                text, scripts, "AI Response", 0, {}
            )
        except Exception as exc:  # noqa: BLE001 — 降级语义：脚本面失败原文直通
            logger.warning(
                "[state_marker_parse] regex 脚本应用失败，原文直通 | err=%s", exc
            )
            return text, None
        if var_changes:
            logger.warning(
                "[state_marker_parse] regex 变量变更（批 2 前仅记录不落账）| vars=%s",
                var_changes,
            )
        if not isinstance(new_text, str) or new_text == text:
            return text, None
        msg = self._last_assistant_message(state)
        if msg is None or msg.get("seq") is None:
            return text, None
        new_msg = dict(msg)
        new_msg["content"] = new_text
        return new_text, {"op": "set", "seq": msg["seq"], "msg": new_msg}

    @staticmethod
    def _load_regex_engine(state: dict[str, Any]) -> Any | None:
        """卡所属模式包的 regex 引擎约定模块装载（None = 零动作合法形态）。

        双根解析（用户副本优先）走 mode_keys.find_mode_package_dir；sys.modules
        按真实导入名（mode_X.regex_scripts）注册防双实例（material 侧同机制
        同名——合宿进程内两侧共享同一模块对象）；装载失败 warning 降级。
        """
        parsed = _parse_mode_agent_key(str(state.get("agent.id", "") or ""))
        if parsed is None:
            return None
        name = f"mode_{parsed[0]}.regex_scripts"
        cached = sys.modules.get(name)
        if cached is not None:
            return cached
        pkg_dir = _find_mode_package_dir(parsed[0])
        if pkg_dir is None:
            return None
        path = pkg_dir / "regex_scripts.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 — 降级语义：坏引擎不阻断解析步
            sys.modules.pop(name, None)
            logger.warning(
                "[state_marker_parse] regex 引擎装载失败，零动作 | pkg=%s | err=%s",
                pkg_dir,
                exc,
            )
            return None
        return mod

    def latest_payload(self, pipeline_id: str) -> dict[str, Any] | None:
        """端点读面：该管道最近一轮 state_updates 载荷（无 = None）。"""
        return self._latest.get(pipeline_id)

    async def _dispatch(
        self, key: str, payload: Any, card_id: str | None, state: dict[str, Any]
    ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        """单账本落账；返回（载荷回执, rejected 记录, 审计行）。

        state 账本键走 `_dispatch_state`（与 behaviors 合并结算，批 2-Ⅱ 2c），
        本分发面只余 memory 键（rejected/审计恒空）。
        """
        if key == "memory":
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError("memory 账本载荷须为非空字符串")
            await self._invoke(
                "memory",
                "memory",
                {"action": "store", "content": payload.strip()},
            )
            return {"stored": True}, [], []
        raise ValueError(f"未知账本键: {key}")

    @staticmethod
    def _init_changes(state: dict[str, Any]) -> list[ChangeItem]:
        """context.rp_init_effects → 变更序列（行级 source=init 随行）。

        键缺席/非映射/空 = 空表（兼容态零动作）；op 字面原样透传（相对/绝对
        语法归 Validator 解释），键域校验同轮 fail-closed。
        """
        effects = state.get("context.rp_init_effects")
        if not isinstance(effects, dict) or not effects:
            return []
        return [
            (str(key), value, {"source": SOURCE_INIT}) for key, value in effects.items()
        ]

    async def _apply_init_effects(
        self, state: dict[str, Any], card_id: str, init_changes: list[ChangeItem]
    ) -> None:
        """会话首轮 init effects 结算（幂等闸 + Validator 全套 + 审计 source=init）。

        - 幂等闸：审计文件会话级 idem ``{pipeline_id}:init``（has_idem 探针）；
          已结算过（含全拒绝的记账形态）= 跳过；ws 不可得 = fail-closed 跳过
          （一次性增量防每轮重放复利放大，契约见 SOURCE_INIT 常量注释）。
        - 结算：validate_and_apply 全套（schema/类型/clamp/hard_rules/cheat_keep
          与主结算同口径）；键域不在卡+包 variables 声明内 = rejected + 审计
          （fail-closed）；accepted 经 character_state.update 落账（本步仍是
          唯一结算通道）。
        - 审计：会话级 idem 整批一行组（探针与写面同幂等空间，重放不双记不
          双结算）；rejected 逐行 warning（作者面键域错误可见）。
        """
        pipeline_id = str(state.get("pipeline_id", "") or "") or "-"
        idem = f"{pipeline_id}:init"
        workspace = state.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            logger.warning(
                "[state_marker_parse] state.workspace 缺失，init effects fail-closed"
                " 跳过（一次性增量防重放复利）| pipeline=%s",
                pipeline_id,
            )
            return
        if has_idem(workspace, idem):
            return
        schema = state.get("context.rp_state_schema")
        schema_dict = schema if isinstance(schema, dict) else None
        hard_rules = state.get("context.rp_state_hard_rules")
        current = await self._current_values(card_id, require=schema_dict is not None)
        result = validate_and_apply(
            current,
            init_changes,
            schema_dict,
            hard_rules if isinstance(hard_rules, dict) else None,
            cheat_keep=self._cheat_keep(state),
        )
        if result.accepted:
            await self._invoke(
                "character_state.update",
                "character_state",
                {"card_id": card_id, "changes": result.accepted},
            )
        for row in result.rejected:
            logger.warning(
                "[state_marker_parse] init effects 拒绝（键域 fail-closed）| "
                "key=%s | reason=%s",
                row.get("key"),
                row.get("reason"),
            )
        append_audit_rows(workspace, result.audit, idem, SOURCE_INIT)

    @staticmethod
    def _split_game_changes(
        payload: dict[str, Any] | None,
        behavior_changes: list[ChangeItem],
        action_changes: Sequence[ChangeItem] = (),
    ) -> list[ChangeItem]:
        """标记载荷 + behaviors 代理变更 + 动作 game 落点选取中的 game 域分流。

        序：标记 payload game.*（声明序）→ behaviors game.*（声明序）→ 动作
        game 落点选取（声明序）——game 域无合并语义（宿主权威面不解释 op），
        批序不承载优先级契约（与 story 侧结算序契约分立）。
        """
        pairs: list[ChangeItem] = []
        if payload:
            pairs.extend(
                (str(key), value, {})
                for key, value in payload.items()
                if str(key).startswith(GAME_KEY_PREFIX)
            )
        pairs.extend(
            change for change in behavior_changes if change[0].startswith(GAME_KEY_PREFIX)
        )
        pairs.extend(action_changes)
        return pairs

    @staticmethod
    def _game_push_plugin_id(state: dict[str, Any]) -> str | None:
        """game delta 推送目标插件 ID（agent 键命名空间派生，零模式包硬编码）。

        模式会话 agent.id = ``mode_X/<stem>`` → 宿主插件 ``mode_X``；非模式
        命名空间 = None（无宿主可推，调用方走推送失败同款降级留痕）。
        """
        parsed = _parse_mode_agent_key(str(state.get("agent.id", "") or ""))
        return f"mode_{parsed[0]}" if parsed is not None else None

    async def _forward_game_deltas(
        self, changes: list[ChangeItem], state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """game.* 变更 → 宿主 delta 通道（薄 sanity + 审计 + 推送 + state 键）。

        薄 sanity 仅形态面：单条值可 JSON 序列化（YAML 装载的 behaviors 可携带
        date 等非 JSON 值）且序列化长度 ≤ GAME_DELTA_MAX_CHARS、单轮转发条数
        ≤ GAME_DELTA_ROUND_CAP；违规 = 拒绝+审计（reason 可辨），不 clamp 不截
        断不解释 op（宿主权威，后端不否决内容，raw 值透传）。三面落点：审计
        （idem = ``{pipeline_id}:{seq}:game``，与 story 批分幂等空间，缺省
        source=game_delta、behavior 行行级覆盖）→ state 键 game_host_delta
        （持久面只留最新轮）→ mode.game.push_delta 推送宿主模式包（目标插件
        ID 按 agent 键命名空间派生 `mode_X/<stem>` → `mode_X`；非模式命名
        空间无宿主可推同降级；无租约宿主侧丢弃；推送失败 warning 降级——
        state 键+审计留痕，不阻断对话）。

        Returns:
            game_host_delta 载荷（``{turn_seq, deltas, ts}``）；零条可转发 = None
            （零新增 state 键，Tier 0 兼容态）。
        """
        pipeline_id = str(state.get("pipeline_id", "") or "") or "-"
        seq = last_assistant_seq(state)
        idem = suffixed(story_idem(pipeline_id, seq), ":game")
        audit_rows: list[dict[str, Any]] = []
        deltas: list[dict[str, Any]] = []
        for key, value, extra in changes:
            row_extra = dict(extra)
            if len(deltas) >= GAME_DELTA_ROUND_CAP:
                audit_rows.append(self._game_audit_row(key, value, REASON_GAME_DELTA_OVERFLOW, row_extra))
                continue
            try:
                serialized = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                # 非序列化值本体进不了 JSONL 审计行——以类型标记留痕（可读不丢真）
                audit_rows.append({
                    "key": key,
                    "op": None,
                    "from": None,
                    "to": f"<不可序列化:{type(value).__name__}>",
                    "reason": REASON_GAME_DELTA_NOT_SERIALIZABLE,
                    **row_extra,
                })
                continue
            if len(serialized) > GAME_DELTA_MAX_CHARS:
                audit_rows.append(
                    self._game_audit_row(key, value, REASON_GAME_DELTA_TOO_LARGE, row_extra)
                )
                continue
            deltas.append({"key": key, "value": value, **row_extra})
            audit_rows.append(self._game_audit_row(key, value, REASON_GAME_DELTA, row_extra))
        self._append_audit(state, audit_rows, idem=idem, default_source=SOURCE_GAME_DELTA)
        if not deltas:
            return None
        if pipeline_id != "-":
            push_plugin = self._game_push_plugin_id(state)
            if push_plugin is None:
                logger.warning(
                    "[state_marker_parse] game delta 推送无宿主（agent 键非模式"
                    "命名空间），state 键+审计留痕 | pipeline=%s | agent=%s",
                    pipeline_id,
                    state.get("agent.id", ""),
                )
            else:
                try:
                    await self._invoke(
                        GAME_DELTA_PUSH_TOOL,
                        push_plugin,
                        {"session_id": pipeline_id, "turn_seq": seq, "deltas": deltas},
                    )
                except Exception as exc:  # noqa: BLE001 — 推送失败降级（留痕照常）
                    logger.warning(
                        "[state_marker_parse] game delta 推送宿主失败（state 键+审计留痕）| "
                        "pipeline=%s | err=%s",
                        pipeline_id,
                        exc,
                    )
        return {"turn_seq": seq, "deltas": deltas, "ts": datetime.now(UTC).isoformat()}

    @staticmethod
    def _game_audit_row(
        key: str, value: Any, reason: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        """game 域审计行体：op=None from=None（raw 透传不做 op 解释），to=原值。"""
        return {"key": key, "op": None, "from": None, "to": value, "reason": reason, **extra}

    async def _dispatch_state(
        self,
        payload: dict[str, Any] | None,
        behavior_changes: list[ChangeItem],
        card_id: str | None,
        state: dict[str, Any],
        action_changes: Sequence[ChangeItem] = (),
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """state 账本结算：动作声明效果 + 标记载荷（回退开启时）+ behaviors
        代理变更合并进同一 validate_and_apply（2026-10-13 动作驱动基线）。

        变更序列定序 = 动作效果对在前（动作声明序）→ 标记载荷对次之（标记
        JSON 声明序；回退开关默认关时本段恒空）→ behavior effect 对在后
        （behaviors 声明序）——同键同轮「缺省取最后」，相对 op 依次叠加于
        前序结果、绝对 op 覆盖（契约见 state_contract 模块 docstring 序列
        形态节）。返回（accepted, rejected, audit）；rejected/审计行自带来源
        （动作行 source=action、behavior 行 source=behavior；标记行无行级
        source，缺省由落账步补：作弊轮=cheat、回退开启=llm、否则 state_marker）。
        """
        if not card_id:
            raise ValueError("非卡键会话状态账本不落账（归属卡未定）")
        schema = state.get("context.rp_state_schema")
        hard_rules = state.get("context.rp_state_hard_rules")
        schema_dict = schema if isinstance(schema, dict) else None
        # 现值读取：schema 态校验（相对 op/delta）依赖现值，拿不到 =
        # 校验前提缺失，fail-closed 整键跳过；兼容态现值仅审计参考，缺失
        # 降级空表不阻断（现状行为保真）。
        current = await self._current_values(card_id, require=schema_dict is not None)
        changes: list[ChangeItem] = []
        changes.extend(action_changes)
        if payload:
            changes.extend((str(key), value, {}) for key, value in payload.items())
        changes.extend(behavior_changes)
        result = validate_and_apply(
            current, changes, schema_dict, hard_rules if isinstance(hard_rules, dict) else None,
            cheat_keep=self._cheat_keep(state),
        )
        if result.accepted:
            await self._invoke(
                "character_state.update",
                "character_state",
                {"card_id": card_id, "changes": result.accepted},
            )
        return result.accepted, result.rejected, result.audit

    @staticmethod
    def _cheating(state: dict[str, Any]) -> bool:
        """/cheat 生效标志（守卫步维护的 pipeline-state 键 meta.cheating）。"""
        return bool(state.get("meta.cheating"))

    @classmethod
    def _cheat_keep(cls, state: dict[str, Any]) -> bool | list[str] | None:
        """Validator 旁路规格（RP 批 3）：未作弊 = None（现状执法）；作弊 = 按
        卡 engine 声明 cheat_overrides.keep_state_machine 归一（归一单源
        card_contract.keep_state_machine_spec，垃圾声明 fail-closed 全保留）。"""
        if not cls._cheating(state):
            return None
        spec, warnings = keep_state_machine_spec(state.get("context.rp_engine"))
        for warning in warnings:
            logger.warning("[state_marker_parse] %s", warning)
        return spec

    @staticmethod
    def _match_behaviors(
        state: dict[str, Any], text: str
    ) -> list[ChangeItem]:
        """卡 behaviors 声明匹配（RP 批 2-Ⅱ 2c；装载见 agent_config_load）。

        源 = ``state["context.rp_state_behaviors"]``（卡 extensions.behaviors ∪
        pack 合并产物）；形态 ``{<行为名>: {trigger: <regex>, effect: {<状态键>:
        <op>}}}``（op 语法与 `<state>` 标记一致）。逐 behavior 按**声明序**对
        剥离后正文 re.search：命中 = effect 键值对依声明序产出，审计附加字段
        ``{"source": "behavior", "behavior": <名>}`` 随行上行（来源枚举第二态，
        标记行缺省由落账步补 state_marker）。fail-soft：声明非 dict/空/正文空 =
        零动作；trigger 非法（编译失败/形态非法）= 该 behavior 跳过 warning，
        其余不受牵连；effect 非映射同跳过。
        learned_behaviors / 写规则 LLM：后置登记（本刀不做）。
        """
        behaviors = state.get("context.rp_state_behaviors")
        if not isinstance(behaviors, dict) or not behaviors or not text:
            return []
        pairs: list[ChangeItem] = []
        for name, decl in behaviors.items():
            if not isinstance(decl, dict):
                logger.warning(
                    "[state_marker_parse] behavior 声明形态非法，跳过 | behavior=%r", name
                )
                continue
            trigger = decl.get("trigger")
            effect = decl.get("effect")
            if not isinstance(trigger, str) or not isinstance(effect, dict):
                logger.warning(
                    "[state_marker_parse] behavior 声明形态非法，跳过 | behavior=%r", name
                )
                continue
            try:
                hit = re.search(trigger, text) is not None
            except re.error as exc:
                logger.warning(
                    "[state_marker_parse] behavior trigger regex 非法，跳过 | "
                    "behavior=%r | err=%s",
                    name,
                    exc,
                )
                continue
            if hit:
                extra = {"source": "behavior", "behavior": str(name)}
                pairs.extend(
                    (str(key), value, dict(extra)) for key, value in effect.items()
                )
        return pairs

    @staticmethod
    def _allow_state_proposals(state: dict[str, Any]) -> bool:
        """state 字段回退开关（作者声明 engine.allow_state_proposals，默认关）。

        读取口径同 allow_cheat 声明链（``context.rp_engine``；非映射/非布尔 =
        缺席默认关，fail-closed）。默认关 = `<state>` 标记 state 字段解析忽略
        + 审计 state_field_ignored；显式 true = 恢复 LLM 提议合法来源（来源
        llm，走 Validator 全套）。动作通道不受本开关影响（动作驱动基线是默认
        形态）。
        """
        engine = state.get("context.rp_engine")
        return isinstance(engine, dict) and engine.get("allow_state_proposals") is True

    @classmethod
    def _state_field_ignored_row(cls, payload: Any) -> dict[str, Any]:
        """state 字段默认关审计行（字段整体一行，非逐键）。

        行级 source=state_field_ignored 随行（落账步缺省来源不被其覆盖的
        机制同 behavior 行——作弊轮归因仍可辨）；op/from 不承载（未进结算），
        to = 原样载荷摘要（截断上限见 STATE_FIELD_SUMMARY_MAX_CHARS）。
        """
        return {
            "key": "state",
            "op": None,
            "from": None,
            "to": cls._summarize_payload(payload),
            "reason": REASON_STATE_FIELD_IGNORED,
            "source": SOURCE_STATE_FIELD_IGNORED,
        }

    @staticmethod
    def _summarize_payload(payload: Any) -> str:
        """载荷原样摘要（可序列化 = JSON 原文截断；非序列化 = 类型标记留痕）。"""
        try:
            summary = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return f"<不可序列化:{type(payload).__name__}>"
        if len(summary) > STATE_FIELD_SUMMARY_MAX_CHARS:
            return summary[:STATE_FIELD_SUMMARY_MAX_CHARS] + "…(截断)"
        return summary

    @staticmethod
    def _match_actions(
        state: dict[str, Any],
    ) -> tuple[list[ChangeItem], list[ChangeItem]]:
        """actions 作者声明清单选取（2026-10-13 动作驱动基线，动作通道主）。

        源 = ``state["context.rp_actions"]``（agent_config_load 装载，target
        面 lint 归一：{engine, game}、缺省 engine）。选取面 = 本轮玩家输入
        （尾条 user 消息 trim）与声明 id/name（trim 后）全等——与游戏页
        choices 点击「文本入输入区+自动发送」既有约定同构；全等之外零触发
        （fail-closed 防子串/叙述误触）。LLM 不新增结构化选取通道（叙述触发
        走 behaviors 通道——两通道本就同经 Validator）。

        命中按声明序产出：
        - target=engine（缺省）：声明 effect（{状态键: op}，语法同 behaviors）
          逐对产出 story 变更，审计附加 ``{"source": "action", "action": <id>}``；
          effect 缺失/非映射 = 该动作跳过 warning（fail-closed 不发明空效果）；
        - target=game：转发宿主（Tier 0 形态 = 宿主 delta 待发队列，桥面
          actions_for_game 随 game_bridge 扩展波），键 = 可选取键、值 = 声明
          params（缺省 {}），不进 story 账本。

        Returns:
            (story 变更对, game 变更对)；无声明/无玩家输入/无全等选取 = 两空表
            （兼容态零动作）。
        """
        actions = state.get("context.rp_actions")
        if not isinstance(actions, list) or not actions:
            return [], []
        user_text = StateMarkerParsePlugin._last_user_text(state)
        if not user_text:
            return [], []
        story: list[ChangeItem] = []
        game: list[ChangeItem] = []
        for entry in actions:
            if not isinstance(entry, dict):
                logger.warning(
                    "[state_marker_parse] action 声明形态非法，跳过 | entry=%r", entry
                )
                continue
            action_id = str(entry.get("id") or "").strip()
            action_name = str(entry.get("name") or "").strip()
            selectable = {key for key in (action_id, action_name) if key}
            if user_text not in selectable:
                continue
            label = action_id or action_name
            extra = {"source": SOURCE_ACTION, "action": label}
            if entry.get("target") == "game":
                params = entry.get("params")
                game.append(
                    (label, params if params is not None else {}, dict(extra))
                )
                continue
            effect = entry.get("effect")
            if not isinstance(effect, dict) or not effect:
                logger.warning(
                    "[state_marker_parse] action 声明缺 effect（engine 落点），"
                    "跳过（fail-closed 不发明空效果）| action=%r",
                    label,
                )
                continue
            story.extend(
                (str(key), value, dict(extra)) for key, value in effect.items()
            )
        return story, game

    @staticmethod
    def _last_user_text(state: dict[str, Any]) -> str:
        """尾条 user 消息文本（动作选取面；无消息/形态非法 = 空串零选取）。"""
        messages = state.get("messages")
        if not isinstance(messages, list):
            return ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                return content.strip() if isinstance(content, str) else ""
        return ""

    async def _current_values(self, card_id: str, require: bool) -> dict[str, Any]:
        """character_state.get 现值（Validator 基线/审计旧值源）。

        require=True（schema 态）：get 失败上抛（落账步既有降级——整键跳过）；
        require=False（兼容态）：失败 warning + 空表（审计旧值记 None）。
        """
        try:
            res = await self._invoke(
                "character_state.get", "character_state", {"card_id": card_id}
            )
        except Exception as exc:
            if require:
                raise
            logger.warning(
                "[state_marker_parse] 现值读取失败（兼容态降级空表）| card_id=%s | err=%s",
                card_id,
                exc,
            )
            return {}
        return self._service_values(res)

    def _append_audit(
        self,
        state: dict[str, Any],
        rows: list[dict[str, Any]],
        idem: str | None = None,
        default_source: str | None = None,
    ) -> None:
        """审计流水 append `<ws>/rp_state_audit.jsonl`（契约见模块 docstring）。

        幂等与 IO 降级单源在共享模块 rp_state_audit（批 3 自本步提取——/cheat
        命令行守卫同文件同机制写入，防两处漂移）。缺省 source（行级无 source
        的行，即回退开启时标记 state 字段行）：作弊轮 = cheat（全流水可归因，
        ADR 2026-10-06）；回退开关开启 = llm（2026-10-13 动作驱动基线，LLM
        提议合法来源）；否则 state_marker。behavior/action/state_field_ignored
        行自带行级 source 覆盖缺省。idem = ``{pipeline_id}:{尾条 assistant
        消息 seq}``；game 域批（RP 批 4-Ⅰ）显式传 ``{pipeline_id}:{seq}:game``
        + source=game_delta（与 story 批分幂等空间，同轮双批互不吞）。
        """
        workspace = state.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            logger.warning(
                "[state_marker_parse] state.workspace 缺失，审计跳过（降级不阻断）"
            )
            return
        if idem is None:
            idem = story_idem(
                str(state.get("pipeline_id", "") or "") or "-", last_assistant_seq(state)
            )
        if default_source is None:
            if self._cheating(state):
                default_source = "cheat"
            elif self._allow_state_proposals(state):
                default_source = SOURCE_LLM
            else:
                default_source = "state_marker"
        append_audit_rows(workspace, rows, idem, default_source)

    @staticmethod
    def _last_assistant_message(state: dict[str, Any]) -> dict[str, Any] | None:
        """尾条 assistant 消息本体（regex set op 需携带完整消息 dict 改写）。"""
        messages = state.get("messages")
        if not isinstance(messages, list):
            return None
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                return msg
        return None

    @classmethod
    def _last_assistant_text(cls, state: dict[str, Any]) -> str:
        msg = cls._last_assistant_message(state)
        if msg is None:
            return ""
        content = msg.get("content")
        return content if isinstance(content, str) else ""

    @staticmethod
    def _resolve_card_id(state: dict[str, Any]) -> str | None:
        agent_id = str(state.get("agent.id", "") or "")
        parsed = _parse_mode_agent_key(agent_id)
        if parsed is None:
            return None
        return parsed[1]

    @staticmethod
    def _service_text(res: Any) -> str:
        """服务回执 → 文本（容忍 {data: ...} 信封或文本本体）。"""
        data = res.get("data") if isinstance(res, dict) else None
        payload = data if data is not None else res
        if isinstance(payload, dict):
            payload = payload.get("text") or payload.get("content") or ""
        return payload.strip() if isinstance(payload, str) else ""

    @staticmethod
    def _service_values(res: Any) -> dict[str, Any]:
        """character_state.get 回执 → 现值表（容忍 {data: ...} 信封；缺失 = 空表）。"""
        data = res.get("data") if isinstance(res, dict) else None
        payload = data if data is not None else res
        if isinstance(payload, dict) and isinstance(payload.get("values"), dict):
            return payload["values"]
        return {}
