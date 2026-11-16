"""模式物料注入 Input 插件 — 通用模式物料步骤（职责终局三件 + /cheat 守卫，
设计 D10 2026-09-28；守卫落点 ADR 2026-10-11）。

context_build 回归纯上下文构建后，模式物料由本独立管道步骤承载。本插件是
**通用**模式步骤（任何模式共用，零具体模式专属逻辑），按 state 激活：

0. /cheat 命令守卫（RP 批 3，先于全部物料逻辑）：链级门控键
   ``state["_cheat_guard"]``（roleplay.yaml prepare 组 context 写入，唯一闸门
   ——其余链不写 = 永不激活，autonomous 输 /cheat 是普通消息零拦截）在场时
   扫尾条用户消息；命令轮（/cheat on/off/status）写回执 assistant 消息 +
   ``_cheat_command=True``（roleplay.yaml llm 组 when 短路消费——回执即本轮
   回复，不空轮）+ skip_remaining 短路 prepare 余下步；非命令轮覆写
   ``_cheat_command=False`` 后照常物料。命令语义纯逻辑单源 rp_cheat 模块
   （on 二次输入确认降级——``/cheat on`` 置 pending、``/cheat on confirm``
   生效；allow_cheat=false 作者拒绝）；生效态写持久键 ``meta.cheating``
   （persistent_fields 投影，Validator 旁路/徽标/审计归因同源）；命令审计行
   落 ``<ws>/rp_state_audit.jsonl``（source=cheat，append 幂等单源
   rp_state_audit；on 生效无审计不生效——无审计不作弊）。守卫自身失败降级
   普通轮（消息直通 LLM，warning 可见），不阻断 RP 链。
1. 激活判定（优先序，设计 D6）：显式 state["execution_context"]["mode"]
   非空 > 按 state["agent.id"] 归属包派生（agent 键 mode_X/<stem> → X，包
   目录存在才派生）> 空。空 = 零动作直通（不写任何键）；形态非法 = warning
   后直通。
2. mode 观测回写：updates["mode"] = 解析结果（观测链出口：/pipelines/state
   摘要 mode → 前端模式面板自动弹出/模式徽标同源取数，无键零动作）。
3. persona 接管（通用机制，设计 D7）：mode.yaml persona 声明 + 携带文本 →
   state["context.persona_text"]（prompt_build 的 {{persona:}} 占位符据此
   换源，替换提示词人设段，骨架不动）。
4. 模式包组装器（模式专属物料出口，通用约定）：模式包可在包内提供
   material.py（种子自包含，不 import 共享根），约定暴露
   ``def build_injection(state, pkg_dir) -> str``——返回本模式的追加注入
   文本（无则空串）。本插件按真实名 ``mode_<mode>.material`` 动态加载
   （sys.modules 注册防双实例）后调用；组装器声明 books_dir/user_agents_dir
   形参时（用户层物料双根扩展点）注选传入真实用户目录，两参约定出口向后
   兼容零打扰；非空则以 "\n\n" 追加到 context.system_prompt 尾部。无
   material.py = 合法形态零追加；加载失败/函数缺失/调用异常 → warning 一次
   + 跳过（降级不阻断管道）。

模式知识注入唯一面 = prompt_build 的 {{mode_catalog}} 目录（描述+路由）；
工具面单真值 = agent yaml tool_ids 三态（模式级收窄退役）。

全路径失败均降级为 warning + 不写入（不抛出），管道行为与无模式一致。
优先级：11——紧跟 context_build（10）之后、tool_schema/prompt_build 之前
（persona 接管与物料追加须先于提示词组装落 state）。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _libs.card_contract import keep_state_machine_spec
from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _root_api import append_audit_rows, parse_mode_agent_key
from mode_material import (
    MODE_ID_RE,
    derive_mode_from_agent,
    find_package_dir,
    resolve_mode,
    resolve_persona_takeover,
)
from rp_cheat import RECEIPT_PREFIX, decide, parse_cheat_command

logger = logging.getLogger(__name__)

# ── /cheat 命令守卫键（RP 批 3，ADR 2026-10-11）───────────────────────────
# 门控键 = 链配置唯一闸门：roleplay.yaml prepare 组 context 写 _cheat_guard=true，
# 其余链不写 = 永不激活（autonomous 输 /cheat 是普通消息，零拦截契约）。
_CHEAT_GUARD_KEY = "_cheat_guard"
# 每轮命令标志：守卫逐轮覆写（命令轮 True / 其余 False），roleplay.yaml llm 组
# when（_cheat_command != True）据此短路——陈旧 True 不会泄漏到下一轮。
_CHEAT_COMMAND_KEY = "_cheat_command"
# /cheat on 待确认标志（下划线瞬态约定，不投影 persistent_fields；重启即失效 =
# 需重新确认，安全方向）。
_CHEAT_PENDING_KEY = "_cheat_pending_on"
# 作弊生效持久标志（mode_material_inject persistent_fields 投影，重启不静默脱敏
# ——徽标/审计口径与状态一致）。
CHEATING_KEY = "meta.cheating"
# /cheat 命令审计行的 idem 空间（复用 2-Ⅰ 扫描幂等机制；"cheat" 中缀不与数字
# seq 相撞）与缺省来源（ADR 2026-10-06：来源=cheat 全流水可归因）。
_CHEAT_IDEM_INFIX = "cheat"
_CHEAT_AUDIT_SOURCE = "cheat"


def _keep_display(spec: bool | list[str]) -> str:
    """keep 规格 → 回执展示文案（decide 模板直接拼接，量词在此收口）。"""
    if spec is True:
        return "全部键域"
    return f"{len(spec)} 项" if spec else "0 项"


class ModeMaterialInjectPlugin(IInputPlugin):
    """模式物料注入 Input 插件（通用模式步骤，职责终局三件）。

    激活时产出键：mode（观测回写）、context.persona_text（人设接管）、
    context.system_prompt（模式包物料追加）。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化模式物料注入插件。

        Args:
            config: 插件配置字典（预留；当前无消费键）。
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "mode_material_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return self._config.get("priority", 11)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """按 state 激活并注入模式物料。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含模式物料 state 更新的插件执行结果
        """
        guard = self._cheat_guard(ctx.state)
        if guard is not None:
            updates, skip = guard
            return PluginResult(state_updates=updates, skip_remaining=skip)
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    def _cheat_guard(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], bool] | None:
        """/cheat 命令守卫（RP 批 3，ADR 2026-10-11）。

        返回 None = 门控未激活（零动作直通物料注入）；(updates, skip) = 本轮
        已处理——skip=True 表示命令拦截轮（skip_remaining 短路 prepare 余下步，
        回执即本轮回复），skip=False 表示非命令轮（仅覆写命令标志后照常物料）。

        命令判定只认「本轮新输入」：尾条消息须为 user（工具迭代/回执轮不重扫）。
        守卫自身失败降级普通轮（消息直通 LLM，warning 可见），不阻断 RP 链。
        """
        if not state.get(_CHEAT_GUARD_KEY):
            return None
        updates: dict[str, Any] = {_CHEAT_COMMAND_KEY: False}
        try:
            return self._cheat_guard_work(state, updates)
        except Exception as exc:  # noqa: BLE001 — 降级语义：守卫失败不阻断 RP 链
            logger.warning(
                "[mode_material_inject] /cheat 守卫执行失败，降级普通轮 | err=%s", exc
            )
            return updates, False

    def _cheat_guard_work(
        self, state: dict[str, Any], updates: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        messages = state.get("messages")
        last = messages[-1] if isinstance(messages, list) and messages else None
        if not isinstance(last, dict) or last.get("role") != "user":
            return updates, False
        cmd = parse_cheat_command(last.get("content"))
        if cmd is None:
            return updates, False  # 非命令：清标志 + 正常物料注入
        if parse_mode_agent_key(str(state.get("agent.id", "") or "")) is None:
            # 无卡键会话（状态账本不存在）零拦截：作弊治理面只对卡会话有意义
            logger.warning(
                "[mode_material_inject] /cheat 出现在非卡键会话，零拦截直通 | "
                "agent.id=%r",
                state.get("agent.id", ""),
            )
            return updates, False
        engine = state.get("context.rp_engine")
        if not isinstance(engine, dict):
            engine = {}  # 声明坏形态（lint 面已警告）按未声明处理
        keep_spec, keep_warnings = keep_state_machine_spec(engine)
        for warning in keep_warnings:
            logger.warning("[mode_material_inject] %s", warning)
        outcome = decide(
            cmd,
            cheating=bool(state.get(CHEATING_KEY)),
            pending=bool(state.get(_CHEAT_PENDING_KEY)),
            allow_cheat=engine.get("allow_cheat"),
            keep_display=_keep_display(keep_spec),
        )
        updates[_CHEAT_COMMAND_KEY] = True
        if outcome.write_pending:
            updates[_CHEAT_PENDING_KEY] = outcome.pending
        if outcome.audit_row is not None:
            if outcome.write_cheating and outcome.cheating:
                # on 生效：无审计不作弊（ADR 制度底座）——审计落盘失败 = 开启
                # 失败，pending 保留供重试；off/拒绝：失败仅降级（关闭是安全方向）
                if not self._append_cheat_audit(state, last, outcome.audit_row):
                    updates[_CHEAT_PENDING_KEY] = True
                    updates["messages"] = {"_ops": [self._receipt_op(
                        state,
                        f"{RECEIPT_PREFIX}作弊开启失败：会话审计暂不可写"
                        "（无审计不作弊），恢复后请重新确认。",
                    )]}
                    return updates, True
            else:
                self._append_cheat_audit(state, last, outcome.audit_row)
        if outcome.write_cheating:
            updates[CHEATING_KEY] = outcome.cheating
        updates["messages"] = {"_ops": [self._receipt_op(state, outcome.receipt)]}
        return updates, True

    @staticmethod
    def _append_cheat_audit(
        state: dict[str, Any], user_msg: dict[str, Any], row: dict[str, Any]
    ) -> bool:
        """/cheat 命令审计行落盘（idem = {pipeline_id}:cheat:{用户消息 seq}）。"""
        pipeline_id = str(state.get("pipeline_id", "") or "") or "-"
        seq = user_msg.get("seq")
        idem = f"{pipeline_id}:{_CHEAT_IDEM_INFIX}:{seq if seq is not None else '-'}"
        return append_audit_rows(
            state.get("workspace"), [row], idem, _CHEAT_AUDIT_SOURCE
        )

    @staticmethod
    def _receipt_op(state: dict[str, Any], text: str) -> dict[str, Any]:
        """回执 assistant 消息 append op（无 seq = 引擎分配；shape 同 llm_core）。"""
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        agent_id = str(state.get("agent.id", "") or "")
        if agent_id:
            msg["agent_id"] = agent_id  # 气泡卡名/卡头像身份戳记
        return {"op": "set", "msg": msg}

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行模式物料注入逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            要写入 state 的字段字典（未激活 = 空字典零写入）
        """
        updates: dict[str, Any] = {}
        state = ctx.state
        # 优先序（设计 D6）：显式 execution_context.mode > agent 归属派生 > 空。
        # 显式值在场即胜出（含无包的标签值——mode 是标签不是枚举）；显式值
        # 形态非法时走下方 warning 直通，不回落派生（在场意图不被静默替换）。
        mode = resolve_mode(state) or derive_mode_from_agent(state)
        # 无 mode（显式缺席且无归属派生：自动模式/聊天直连）= 零动作直通：
        # 不回写、不取数、零开销。
        if not mode:
            return updates
        if not MODE_ID_RE.match(mode):
            logger.warning(
                "[mode_material_inject] mode 键形态非法，跳过模式物料注入 | mode=%r",
                mode,
            )
            return updates
        # state 顶层 mode 键回写（观测链出口，消费契约见模块 docstring）。
        # 解析成功即回写、逐轮覆盖；物料注入降级（组装器失败）不回滚——回写
        # 是解析路径产物，不随注入成败翻转。
        updates["mode"] = mode
        pkg_dir = find_package_dir(mode)
        persona_text = resolve_persona_takeover(pkg_dir, state)
        if persona_text:
            # 人设接管（通用机制）：mode.yaml persona 声明 + 携带文本 →
            # prompt_build 的 {{persona:}} 占位符换源（替换提示词人设段，
            # 骨架不动）。注意缓存语义：接管改变系统提示词前缀，破坏前缀缓存
            # 命中——提示职责在前端附身建立时告知用户。
            updates["context.persona_text"] = persona_text
            logger.info(
                "[mode_material_inject] 人设接管生效 | mode=%s | chars=%d",
                mode,
                len(persona_text),
            )
        self._append_package_material(state, mode, pkg_dir, updates)
        self._promote_staged_message_ops(state, updates)
        return updates

    @staticmethod
    def _promote_staged_message_ops(
        state: dict[str, Any], updates: dict[str, Any]
    ) -> None:
        """组装器暂存消息变更 op 提升（通用约定出口，零具体模式知识）。

        material.py 组装期可对用户消息做脚本改写（RP 批 1a regex 面），以
        set op 列表暂存 ``state["_material_message_ops"]``（组装器声明键，
        下划线键不参与引擎合并、暂存即过桥）；本插件原样提升到
        ``state_updates["messages"] = {"_ops": [...]}``（引擎 set/insert 契约，
        llm_core 同通道）并清除暂存键（快照即弃，清除防同进程残留语义误读）。
        非列表/空表/非 dict op = warning 丢弃（降级不阻断）。
        """
        staged = state.pop("_material_message_ops", None)
        if staged is None:
            return
        if not isinstance(staged, list) or not staged:
            logger.warning(
                "[mode_material_inject] 组装器消息暂存形态非法，丢弃 | got=%r",
                type(staged).__name__,
            )
            return
        if not all(isinstance(op, dict) for op in staged):
            logger.warning(
                "[mode_material_inject] 组装器消息暂存含非 dict op，丢弃 | n=%d",
                len(staged),
            )
            return
        updates["messages"] = {"_ops": staged}

    def _append_package_material(
        self,
        state: dict[str, Any],
        mode: str,
        pkg_dir: Path | None,
        updates: dict[str, Any],
    ) -> None:
        """模式包组装器追加段：material.py::build_injection 非空则以空行衔接。

        用户层物料目录（卡/世界书双根）按组装器签名注选传：约定出口为
        build_injection(state, pkg_dir) 两参形态（向后兼容零打扰），声明
        books_dir / user_agents_dir 形参的模式包才收到真实用户目录
        （user_config_dir()/lorebooks 与 /agents；None = 用户空间不可得，
        组装器侧语义为仅包内）。
        """
        builder = self._load_builder(mode, pkg_dir, self._read_material_decl(pkg_dir))
        if builder is None:
            return
        kwargs = self._user_layer_dir_kwargs(builder)
        try:
            text = builder(state, pkg_dir, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 降级语义：组装器失败不阻断管道
            logger.warning(
                "[mode_material_inject] 模式包物料组装失败，跳过追加 | mode=%s | err=%s",
                mode,
                exc,
            )
            return
        if isinstance(text, str) and text.strip():
            self._append_section(state, updates, text)
            logger.info(
                "[mode_material_inject] 模式包物料段已注入 | mode=%s | chars=%d",
                mode,
                len(text),
            )

    @staticmethod
    def _user_layer_dir_kwargs(builder: Callable[..., Any]) -> dict[str, str | None]:
        """按组装器签名注入用户层物料目录 kwargs（声明形参才传）。

        user_space 共享根函数内导入（server.py 种子内联同形态）；解析失败/
        用户空间未配置 = None（组装器侧语义：仅包内，降级不阻断）。
        """
        try:
            params = set(inspect.signature(builder).parameters)
        except (TypeError, ValueError):
            return {}
        wanted = {"books_dir", "user_agents_dir"} & params
        if not wanted:
            return {}
        root: Path | None = None
        try:
            from _root_api import user_config_dir

            root = user_config_dir()
        except Exception as exc:  # noqa: BLE001 — 降级语义：用户空间不可得即仅包内
            # 降级留痕（用户可见面：物料组装少了用户层目录，作者排查需可见）
            logger.warning(
                "[mode_material_inject] 用户空间不可得，用户层物料目录缺席（仅包内）| err=%s",
                exc,
            )
            root = None
        dirs: dict[str, str | None] = {
            "books_dir": str(root / "lorebooks") if root is not None else None,
            "user_agents_dir": str(root / "agents") if root is not None else None,
        }
        return {key: dirs[key] for key in wanted}

    @staticmethod
    def _read_material_decl(pkg_dir: Path | None) -> str:
        """直读包内 mode.yaml 的 material 声明（组装器入口，声明面单一真值）。

        文件面事实直读不经服务通道（modes API 同源口径）：包缺失/
        mode.yaml 缺失/解析失败/字段缺席 = 无声明（组装器不加载，合法形态）。
        """
        if pkg_dir is None:
            return ""
        decl_path = pkg_dir / "mode.yaml"
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "[mode_material_inject] mode.yaml 不可读，组装器声明缺席 | path=%s | err=%s",
                decl_path,
                exc,
            )
            return ""
        if isinstance(data, dict):
            decl = data.get("material")
            if isinstance(decl, str):
                return decl.strip()
        return ""

    @staticmethod
    def _load_builder(
        mode: str, pkg_dir: Path | None, material_decl: str
    ) -> Callable[..., Any] | None:
        """按 mode.yaml material 声明动态加载组装器（``<file>::<出口>`` 形态）。

        - 无声明 = 包未带组装器（合法形态）→ None 静默（声明驱动：包内文件
          不再按约定名回退发现）；
        - 声明了但文件缺失 = 声明与实物不符 → warning + None；
        - 加载失败 / 声明出口缺失 → warning 一次 + None（降级不阻断）；
        - sys.modules 按声明派生名注册防双实例（同进程重复激活共享模块对象）。
        """
        if pkg_dir is None or not material_decl:
            return None
        file_part, _, func_name = material_decl.partition("::")
        func_name = func_name.strip() or "build_injection"
        file_name = file_part.strip()
        mod_name = f"mode_{mode}._material_{file_name.replace('/', '_').replace('.', '_')}"
        cached = sys.modules.get(mod_name)
        if cached is not None:
            func = getattr(cached, func_name, None)
            return func if callable(func) else None
        path = pkg_dir / file_name
        if not path.is_file():
            logger.warning(
                "[mode_material_inject] material 声明文件缺失，跳过追加 | mode=%s | decl=%s",
                mode,
                material_decl,
            )
            return None
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            logger.warning(
                "[mode_material_inject] material spec 构造失败，跳过追加 | mode=%s",
                mode,
            )
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 — 降级语义：坏组装器不阻断管道
            sys.modules.pop(mod_name, None)
            logger.warning(
                "[mode_material_inject] material 加载失败，跳过追加 | mode=%s | err=%s",
                mode,
                exc,
            )
            return None
        func = getattr(mod, func_name, None)
        if not callable(func):
            logger.warning(
                "[mode_material_inject] material 缺声明出口 %s，跳过追加 | mode=%s",
                func_name,
                mode,
            )
            return None
        return func

    @staticmethod
    def _append_section(
        state: dict[str, Any], updates: dict[str, Any], section: str
    ) -> None:
        """追加注入段到 context.system_prompt 尾部（基线提示词保留，追加非替换）。

        基座 = 本轮已追加结果（updates）优先，回落上游（context_build）写入的
        state 值；上游未写（断链/单测裸 state）= 本段自成基座。
        """
        base = str(updates.get("context.system_prompt") or state.get("context.system_prompt") or "")
        updates["context.system_prompt"] = f"{base}\n\n{section}" if base else section
