"""重复检查 Output 插件 — 合并 duplicate_call + repetitive_output。

负责在管道循环的输出阶段检测工具调用重复和输出内容重复，
采用三级渐进策略：软提示 → 拦截重路由 → 终止管道。

合并收益：共享重复计数状态（router.duplicate_count / router.repetitive_count）+ 低维护成本。

策略说明（终止/回路由经控制状态键表达，引擎与 DSL 消费）：
    - 第一级（count < max）：注入软提示，工具调用仍执行
    - 第二级（count >= max）：移除重复调用 + 注入强警告 + 写
      router.duplicate_back_llm=true（DSL 路由回 LLM）
    - 第三级（拦截次数 >= hard_limit）：should_stop=true 终止管道
      - 主 agent：注入用户通知消息后终止
      - 子 agent：直接终止（署名 duplicate_loop，终态映射 Failed）

State 命名空间：
    - router.duplicate_count : 工具调用重复计数（窗口内同签名的多余出现次数最大值）
    - router.recent_tool_sigs : 最近 signature_window_size 条单调用签名（跨迭代滑动窗口）
    - router.repetitive_count : 输出内容重复计数（跨迭代）
    - router.duplicate_intercepts : 拦截总次数
    - router.last_response : 上一次 LLM 响应摘要
    - router.tool_fail_streak : {tool: 连续失败次数}（失败熔断，tool_execute 轮判定）
    - router.tool_fail_banned : 已被失败熔断摘除（禁用）的工具清单
    - router.llm_fail_streak : {fingerprint, count}（LLM 失败连击，llm 轮判定）
    - router.llm_error_cursor : {run_id, count}（已计入的 llm_core 错误条数游标）
    - task.error / task.failure_class / task.failure_reason : 熔断终止的失败
      归因注记（有任务身份时随写，上级视图/富通知透传死因）

消息修改一律经 state_updates["messages"]={"_ops":[...]} 回传——引擎 merge 只认
slot ops（set 缺 seq=append / set(seq,msg)=modify / set(seq,null)=delete），
直接改 ctx.state 的修改过不了 server 适配层的新 dict，真实链路上必丢。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from _libs.pipeline_base.plugin import IOutputPlugin, OutputResult, PluginContext
from _libs.pipeline_base.types import StateKeys

logger = logging.getLogger(__name__)

# 模型不可用错误标记（跨插件文本契约）：llm_core 的模型解析失败错误文本含本
# 标记，据此区分「模型不可用」类失败（配置错，永不自愈）与其它同类失败
# （瞬态，阈值更高）。两侧各自硬编码同一字符串——跨插件不共享 import
# （播种副本对共享根裸模块不可达）。
MODEL_UNAVAILABLE_MARKER = "MODEL_UNAVAILABLE"

# 连击指纹稳定化：供应商错误常携带每调用易变字段（请求号 request_id 等），
# 全文同文判定会把「同一失败」判成异文、连击永远攒不起来（2026-10-23 装机
# 实证：额度 429 连败 175 次熔断不触发，仅因每条 request_id 不同）。指纹与
# 同文判定前剥除这些字段的值（保留键名与前后结构），展示文本不动。
_VOLATILE_FIELD_RE = re.compile(
    # request/trace/call 三族易变 id，下划线与驼峰两种拼写都收（IGNORECASE）：
    # 2026-11-18 装机实证——gateway 错误正文用 camelCase ``requestId``，旧正则
    # 只匹配 ``request_id``，同型失败逐调用异文、连击永不达阈（634adfc6a364
    # 家族 169 连败未触发熔断的直接根因之一）。
    r"(\\?[\"']?(?:request|trace|call)[_]?id\\?[\"']?\s*[:=]\s*\\?[\"']?)([0-9A-Za-z-]{8,})",
    re.IGNORECASE,
)


DYNAMIC_FORWARD_TOOL = "call_tool"


def _stable_error_text(message: str) -> str:
    """剥除每调用易变字段后的错误文本（连击指纹/同文判定专用，不改写原文）。"""
    return _VOLATILE_FIELD_RE.sub(r"\1<volatile>", message)


def _effective_fail_target(tool: str, res: dict[str, Any]) -> tuple[str, bool]:
    """失败归属工具名 → ``(目标名, 是否动态类)``。

    动态工具经薄转发壳 ``call_tool`` 调用，按壳名计连败两个方向都错：摘壳出
    ``tool_ids`` = 全部动态工具一次性失联；不摘则好坏工具混计、真坏的永不被识别。

    **目标名的真实可达通道 = ``metadata.target_tool``**：``tool_results`` 条目是
    ``ToolResult`` 七键封闭结构（``tool_name/success/error/data/metadata/duration_ms``
    + ``extra``），**不含 ``arguments``**——只有 ``metadata`` 能把目标名带到下游。
    故优先级：``metadata.target_tool`` > ``arguments.tool``（guard 拦截路径的
    ``extra`` 扩展位）> 回落本名。

    带 ``target_tool`` 而本名不是壳的条目（如 guard 预填按目标名入账的历史形态）
    同样归动态桶——防「动态面误用一次就摘掉常驻工具」的跨桶污染。
    """
    meta = res.get("metadata")
    target_in_meta = ""
    if isinstance(meta, dict):
        target_in_meta = str(meta.get("target_tool") or "").strip()

    if tool == DYNAMIC_FORWARD_TOOL:
        if target_in_meta and target_in_meta != DYNAMIC_FORWARD_TOOL:
            return target_in_meta, True
        args = res.get("arguments")
        if isinstance(args, dict):
            target = str(args.get("tool") or "").strip()
            if target and target != DYNAMIC_FORWARD_TOOL:
                return target, True
        # 取不到目标名 = 壳自身失败（TOOL_NOT_FOUND / INVALID_ARGUMENTS /
        # DYNAMIC_TOOLS_UNAVAILABLE / TOOL_EXECUTOR_UNAVAILABLE）。这仍是「动态面
        # 被误用」而非「转发壳坏了」：落常规桶会让连败 10 次把 call_tool 摘出
        # tool_ids → 全部动态工具一次性失联，且当轮不可自愈（次轮 tool_schema 按
        # tool_ids 不再输出 call_tool 的 schema，agent_config_load 每 run 才重注入）。
        # 故一律归动态桶：只告警/终止，永不动 tool_ids。
        return tool, True

    if target_in_meta and target_in_meta != tool:
        return target_in_meta, True
    return tool, False


def _is_target_failure(res: dict[str, Any]) -> bool:
    """壳成功、内层信封失败 = **目标**失败（**仅动态类**适用）。

    转发壳的传输层 ``success`` 恒为 True（转发动作本身完成了），目标失败信息在
    ``data.success`` 内层。只看顶层会把目标失败误判成成功 → 连败计数永不累积。
    """
    if res.get("success") is not True:
        return False
    data = res.get("data")
    return isinstance(data, dict) and data.get("success") is False


def _failure_is_not_tool_fault(res: dict[str, Any]) -> bool:
    """失败但不是「工具坏了」的两类豁免（用户裁定 2026-09-28 / 2026-11-11）。

    - ``error_code`` 形如 ``HTTP_404``：请求已送达并拿到响应，目标资源不存在是
      LLM 可自愈的业务反馈（换 URL/换信源），计连败会把探索性 404 误判成工具故障；
    - ``metadata.exit_code`` 存在：命令进程已跑完、退出码非零 = 工具执行正常、
      命令业务失败（跑测试/编译报错、grep 无匹配），几轮就把 bash 摘出工具面。
    """
    code = str(res.get("error_code") or "")
    if code.startswith("HTTP_") and code[5:].isdigit():
        return True
    meta = res.get("metadata")
    return isinstance(meta, dict) and meta.get("exit_code") is not None


def _classify_failure_entry(
    res: Any, exempt_tools: list[str]
) -> tuple[str, str, bool]:
    """单条 ``tool_results`` 条目 → ``(kind, 归属名, 是否动态类)``。

    kind：``skip``（不参与）/ ``success``（成功清零）/ ``refusal``（审批通道故障，
    摘窗口不动计数）/ ``failure``（计连败）。

    审批通道故障豁免（BUG-41）：``security_check`` 审批等待链路异常时返回带
    ``retry_allowed=True`` 的软拦截——人审结果未知（用户可能已批准但响应丢失）、
    调用从未执行，「同参重调结果不变」的前提不成立，故不计连败，并从签名窗口摘除
    该次发射（人审后的重试不得被去重拦截）。
    """
    if not isinstance(res, dict):
        return "skip", "", False
    call_name = str(res.get("tool_name") or "")
    if not call_name or call_name in exempt_tools:
        return "skip", "", False

    target, is_dynamic = _effective_fail_target(call_name, res)
    # 内层失败只在**动态类**条目上算工具故障：常驻工具的业务载荷
    # （data.success=false，如 dsh.tool.call 透传的失败信封）是业务结果，
    # 计入连败会误摘常驻工具面。
    if is_dynamic and _is_target_failure(res):
        return "failure", target, is_dynamic
    if res.get("success") is True:
        return "success", target, is_dynamic
    if res.get("retry_allowed") is True:
        # 通道故障的窗口签名按**原调用名**摘（发射的是壳名那一条）
        return "refusal", call_name, False
    if _failure_is_not_tool_fault(res):
        return "skip", "", False
    return "failure", target, is_dynamic


class _FailBuckets:
    """失败计数两桶（常规 / 动态），带脏标记供调用方决定写哪些键。

    两桶分离是「动态与常驻分别记账」的落点：动态桶只出告警与硬上限终止，
    **永不写 ``tool_ids``**（动态目标不在白名单，每轮由搜索结果重建）。
    """

    __slots__ = ("regular", "dynamic", "regular_dirty", "dynamic_dirty")

    def __init__(self, regular: Any, dynamic: Any) -> None:
        self.regular: dict[str, int] = dict(regular or {})
        self.dynamic: dict[str, int] = dict(dynamic or {})
        self.regular_dirty = False
        self.dynamic_dirty = False

    def _pick(self, is_dynamic: bool) -> dict[str, int]:
        return self.dynamic if is_dynamic else self.regular

    def note_success(self, name: str, is_dynamic: bool) -> None:
        """成功即清零该桶该名（自愈不受伤害）。"""
        if self._pick(is_dynamic).pop(name, None) is not None:
            self._mark(is_dynamic)

    def note_failure(self, name: str, is_dynamic: bool) -> int:
        bucket = self._pick(is_dynamic)
        bucket[name] = bucket.get(name, 0) + 1
        self._mark(is_dynamic)
        return bucket[name]

    def _mark(self, is_dynamic: bool) -> None:
        if is_dynamic:
            self.dynamic_dirty = True
        else:
            self.regular_dirty = True


def _error_detail(message: str, window: int = 160) -> str:
    """失败详情展示文本：短文全文、长文首尾夹取（类别摘要在头、供应商原文
    在尾，中部传输前缀不值得整段保留）；易变字段一并剥除。"""
    stable = _stable_error_text(message)
    if len(stable) <= window * 2 + 20:
        return stable
    return f"{stable[:window]}……{stable[-window:]}"

# count = 重复计数（窗口内首次出现之后的累计出现次数，含本次）；total = count+1 = 含首次的总出现次数
_HINT_TEMPLATES = {
    1: "{tool} 以相同参数重复了 {count} 次（这是近期第 {total} 次调用），结果不会有变化。请考虑换一种方式完成任务。",
    2: "{tool} 已重复调用 {count} 次（这是近期第 {total} 次调用）。请立即停止使用该工具和参数，尝试完全不同的方法。",
}

_MAIN_AGENT_TERMINATE_MSG = (
    "抱歉，我在执行过程中陷入了重复调用同一工具的死循环，无法继续完成当前任务。"
    "请提供更多指示或调整任务要求，我将重新尝试。"
)


class DuplicateCheckPlugin(IOutputPlugin):
    """重复检查 Output 插件。

    合并了旧代码中 duplicate_call 和 repetitive_output 两个策略。
    两者都维护重复计数器，合并后共享 router.duplicate_count 命名空间。

    检查维度：
    1. 工具调用重复：单调用签名（工具+参数）在滑动窗口内的多余出现——
       整组只比上一轮的旧算法对多工具场景恒不计数（LLM 每轮微调一个调用
       或交替两个调用即可绕过），已退役
    2. 输出内容重复：LLM 连续返回相同或高度相似的内容

    三级渐进策略：
    - 第一级：注入软提示（工具调用仍执行）
    - 第二级：移除重复调用 + 强警告 + 路由回 LLM
    - 第三级：终止管道（主 agent 通知用户，子 agent 直接终止）

    优先级：4（系统级）
    重复检测异常必须终止管道。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化重复检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_duplicate_calls: 工具调用重复拦截阈值（默认 3）
                - max_repetitive_output: 输出内容重复拦截阈值（默认 3）
                - hard_limit_intercepts: 拦截次数硬上限，达到后终止管道（默认 4）
                - similarity_threshold: 输出相似度阈值（默认 0.9）
                - signature_window_size: 单调用签名滑动窗口条数（默认 8，
                  1-2 工具交替循环仍可升级到二级拦截，更大轮转循环保留软提示）
        """
        self._config = config or {}
        self._max_duplicate_calls = self._config.get("max_duplicate_calls", 3)
        self._max_repetitive_output = self._config.get("max_repetitive_output", 3)
        self._hard_limit_intercepts = self._config.get("hard_limit_intercepts", 4)
        self._similarity_threshold = self._config.get("similarity_threshold", 0.9)
        self._signature_window_size = self._config.get("signature_window_size", 8)
        # 失败熔断（ADR 2026-09-09）：同工具连续失败 ≥threshold 摘除工具面逼收尾，
        # ≥hard_limit 终止管道。阈值依据：合法自愈实测 1-2 次内成功；失败面已
        # 收窄到「工具本身执行不了」（见 _check_tool_failures），10/15 给足探索性
        # 重试空间（2026-11-11 用户裁定：原 5/8 过低，几轮探索即摘工具面）。
        self._fail_break_threshold = self._config.get("fail_break_threshold", 10)
        self._fail_break_hard_limit = self._config.get("fail_break_hard_limit", 15)
        self._fail_breaker_exempt_tools = self._config.get("fail_breaker_exempt_tools", []) or []
        # LLM 失败连击熔断（2026-10-20 用户裁定「连环失败要快速失败」）：
        # 连续 identical 的 llm_core 调用失败 → 快速失败，终态原因如实写
        # model_unavailable（模型不可用类：配置错，永不自愈）/ llm_fail_loop
        # （其它同类失败：瞬态，阈值同工具熔断给 2.5 倍裕度）。
        self._llm_fail_break_threshold = self._config.get("llm_fail_break_threshold", 3)
        self._llm_fail_break_threshold_transient = self._config.get(
            "llm_fail_break_threshold_transient", 5
        )

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "duplicate_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 4)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["next_llm", "end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行重复检查。

        采用三级渐进策略处理重复。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含重复检查结果和路由信号的输出结果
        """
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行重复检查逻辑。

        豁免门：
        - 管道已结束（ENDED）不判定——post-end 阶段不应判重复；
        - 仅 llm_call 轮次判定输出重复——工具结果文本不是 LLM 输出，误判会
          在 tool 消息后追加提示打断 assistant(tool_calls)→tool 序列；
        - 含评估结论 JSON 的输出不判重复（即使文本相似）。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典
        """
        # 管道已结束时跳过：post-end 阶段不应判定重复
        if ctx.state.get(StateKeys.ENDED, False):
            return {}

        # 失败熔断（ADR 2026-09-09）：本轮刚产出 tool_results（for-each collect
        # 置换键，恒新鲜）即是连败计数的判定时机。触发熔断（禁工具/终止）时
        # 独占本轮输出；未触发则计数簿记随轮合并、继续 LLM 输出重复判定——
        # 合并轮 LLM 恒有产出，重复判定对 LLM 输出面生效，工具结果文本不参与
        # （不会在 tool 消息后追加提示打断 assistant(tool_calls)→tool 序列）。
        breaker_updates = self._check_tool_failures(ctx)
        if breaker_updates.get("router.tool_fail_banned") or breaker_updates.get(
            "router.stop_reason"
        ):
            return breaker_updates

        updates: dict[str, Any] = dict(breaker_updates)

        # LLM 失败连击熔断（2026-10-20 裁定「连环失败要快速失败」）：连续
        # identical 的 llm_core 调用失败 → 快速失败（终态原因如实写
        # model_unavailable / llm_fail_loop）。失败轮同时**不计入**输出重复
        # 判定——失败轮的 raw_result 是上一轮残留（同一文本），计入即把
        # 「同一错误反复」误诊成「输出重复」（2026-10-20 事故：15 条同文错误
        # 的终态被写成 duplicate_loop，与真实死因不符）。
        llm_fail_updates, llm_failed = self._check_llm_failures(ctx)
        updates.update(llm_fail_updates)
        if llm_fail_updates.get("router.stop_reason"):
            return updates

        # 1. 工具调用重复检查
        dup_result = self._check_duplicate_calls(ctx)
        updates.update(dup_result)

        # 2. 输出内容重复检查
        rep_result = self._check_repetitive_output(ctx, llm_failed=llm_failed)
        updates.update(rep_result)

        # 3. 综合判断——两个检查对本轮计数恒有产出（无输入轮显式清零），
        #    绝不回退 ctx.state 的历史值：level-1 软提示不清零，陈旧计数会让
        #    后续无调用轮次误判重复（提示文本工具名还取空串）
        duplicate_count = updates.get("router.duplicate_count", 0)
        repetitive_count = updates.get("router.repetitive_count", 0)

        # 3a. 工具调用重复处理
        if duplicate_count > 0:
            return self._handle_duplicate_tool_calls(ctx, updates, duplicate_count)

        # 3b. 输出内容重复处理
        if repetitive_count > 0:
            return self._handle_repetitive_output(ctx, updates, repetitive_count)

        return updates

    def _check_tool_failures(self, ctx: PluginContext) -> dict[str, Any]:
        """失败熔断（ADR 2026-09-09）：同工具连败达阈值摘工具面逼收尾，
        达硬上限终止管道（stop_reason=tool_fail_loop）。

        连败计数与参数无关——失败循环无法靠改参数（递增序号/换措辞）洗白，
        补上签名级重复检测被绕过的缺口。成功即清零（自愈不受伤害）。

        三类豁免的判据集中在 ``_failure_is_not_tool_fault`` / ``_classify_failure_entry``：
        审批通道故障不计连败、HTTP 数字码与已完成执行（``metadata.exit_code``）不算工具坏。

        **动态类另设计数**（方案 §7.2 #14）：经 ``call_tool`` 的失败按目标工具名
        落 ``router.dynamic_tool_fail_streak``，只出「阈值告警 + 硬上限终止」两级，
        **永不写 ``tool_ids``**——动态目标不在白名单（每轮由 ``tool_schema`` 重建），
        且摘转发壳 = 全部动态工具一次性失联。

        State 命名空间：
            - router.tool_fail_streak : {tool: 连续失败次数}（常驻类）
            - router.tool_fail_banned : 已摘除（禁用）的常驻工具清单
            - router.dynamic_tool_fail_streak : {目标工具: 连续失败次数}（动态类另计）
            - router.dynamic_tool_fail_banned : 动态类已告警的目标工具清单
            - router.recent_tool_sigs : 通道故障时摘除对应签名后的窗口

        Returns:
            状态更新字典；无失败结果且无摘除时返回空（不动既有状态）
        """
        results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if not results:
            return {}

        buckets = _FailBuckets(
            ctx.state.get("router.tool_fail_streak", {}),
            ctx.state.get("router.dynamic_tool_fail_streak", {}),
        )
        window: list[str] = list(ctx.state.get("router.recent_tool_sigs", []) or [])
        window_pruned = self._replay_failure_entries(results, buckets, window)

        if not (buckets.regular_dirty or buckets.dynamic_dirty or window_pruned):
            return {}

        updates: dict[str, Any] = {}
        if buckets.regular_dirty:
            updates["router.tool_fail_streak"] = buckets.regular
        if buckets.dynamic_dirty:
            updates["router.dynamic_tool_fail_streak"] = buckets.dynamic
        if window_pruned:
            updates["router.recent_tool_sigs"] = window
        return self._apply_failure_breakers(ctx, updates, buckets)

    def _replay_failure_entries(
        self, results: list[Any], buckets: _FailBuckets, window: list[str]
    ) -> bool:
        """按条目重放计数 → 是否摘过窗口签名（通道故障那条）。"""
        window_pruned = False
        for res in results:
            kind, name, is_dynamic = _classify_failure_entry(res, self._fail_breaker_exempt_tools)
            if kind == "skip":
                continue
            if kind == "success":
                buckets.note_success(name, is_dynamic)
            elif kind == "refusal":
                window_pruned = self._drop_signature(window, name, res) or window_pruned
            else:
                buckets.note_failure(name, is_dynamic)
        return window_pruned

    def _drop_signature(self, window: list[str], call_name: str, res: dict[str, Any]) -> bool:
        """从窗口摘除最近一条同签名发射（人审后的重试不得被去重拦截）。"""
        sig = self._call_signature(call_name, res.get("arguments"))
        for i in range(len(window) - 1, -1, -1):
            if window[i] == sig:
                del window[i]
                return True
        return False

    def _apply_failure_breakers(
        self, ctx: PluginContext, updates: dict[str, Any], buckets: _FailBuckets
    ) -> dict[str, Any]:
        """硬上限终止 → 动态告警 → 常驻摘除，三级按序收口。"""
        hard = self._hard_limit_hit(buckets)
        if hard is not None:
            name, count = hard
            return self._terminate_pipeline(
                ctx,
                updates,
                f"工具 {name} 连续失败 {count} 次（失败熔断）",
                ctx.state.get("router.duplicate_intercepts", 0),
                message=(
                    "抱歉，执行过程中依赖的工具持续失败，已触发失败熔断，无法继续完成当前任务。"
                    "请提供更多指示或调整任务要求后重试。"
                ),
                stop_reason="tool_fail_loop",
            )
        self._warn_dynamic_targets(ctx, updates, buckets)
        self._ban_regular_tools(ctx, updates, buckets)
        return updates

    def _hard_limit_hit(self, buckets: _FailBuckets) -> tuple[str, int] | None:
        """首个达硬上限的归属名（两桶都查；动态类同样只终止、不摘面）。"""
        for bucket in (buckets.regular, buckets.dynamic):
            for name, count in bucket.items():
                if count >= self._fail_break_hard_limit:
                    return name, count
        return None

    def _warn_dynamic_targets(
        self, ctx: PluginContext, updates: dict[str, Any], buckets: _FailBuckets
    ) -> None:
        """动态类达阈值只告警（提示模型停用该目标），**不动 tool_ids**。"""
        warned: list[str] = list(ctx.state.get("router.dynamic_tool_fail_banned", []) or [])
        newly = [
            name for name, count in buckets.dynamic.items()
            if count >= self._fail_break_threshold and name not in warned
        ]
        if not newly:
            return
        updates["router.dynamic_tool_fail_banned"] = warned + newly
        self._inject_warning(ctx, updates, self._dynamic_warning_text(newly))
        logger.warning(
            "[%s] Dynamic tool fail breaker tripped | warned=%s streaks=%s",
            self.name,
            newly,
            {n: c for n, c in buckets.dynamic.items() if n in newly},
        )

    def _dynamic_warning_text(self, newly: list[str]) -> str:
        """按 streak 归属分文案：壳自身失败与目标失败是两种不同处境。

        ``DYNAMIC_FORWARD_TOOL`` 出现在 streak 键里 = **转发壳自己**连续失败
        （``TOOL_NOT_FOUND`` / ``INVALID_ARGUMENTS`` / ``DYNAMIC_TOOLS_UNAVAILABLE``
        / ``TOOL_EXECUTOR_UNAVAILABLE``）。此时说「请勿再经 call_tool 调用 call_tool」
        指代不成立，且真实含义是「动态面持续不可用或被持续误用」——给模型的出路
        应是回到常驻工具/基于已有信息收尾，而不是「换个工具名再试」。
        """
        shell_failed = DYNAMIC_FORWARD_TOOL in newly
        targets = [n for n in newly if n != DYNAMIC_FORWARD_TOOL]
        parts: list[str] = []
        if shell_failed:
            parts.append(
                f"动态工具面（{DYNAMIC_FORWARD_TOOL}）已连续失败 {self._fail_break_threshold} 次，"
                f"当前无法转发任何动态工具。请改用常驻工具，或直接基于已有信息收尾；"
                f"确需动态工具时先重新 resource_search 确认可引入。"
            )
        if targets:
            parts.append(
                f"动态工具 {'、'.join(targets)} 已连续失败 {self._fail_break_threshold} 次。"
                f"请勿再经 {DYNAMIC_FORWARD_TOOL} 调用它，改用其它已引入工具或直接基于已有信息收尾。"
            )
        return "".join(parts)

    def _ban_regular_tools(
        self, ctx: PluginContext, updates: dict[str, Any], buckets: _FailBuckets
    ) -> None:
        """常驻类达阈值摘出 ``tool_ids``（动态桶永不进这里）。"""
        banned: list[str] = list(ctx.state.get("router.tool_fail_banned", []) or [])
        newly = [
            name for name, count in buckets.regular.items()
            if count >= self._fail_break_threshold and name not in banned
        ]
        if not newly:
            return
        updates["router.tool_fail_banned"] = banned + newly
        tool_ids = ctx.state.get("tool_ids")
        if isinstance(tool_ids, list):
            updates["tool_ids"] = [t for t in tool_ids if t not in newly]
        self._inject_warning(
            ctx,
            updates,
            f"{'、'.join(newly)} 已连续失败 {self._fail_break_threshold} 次并被禁用"
            f"（后续调用将被拒绝）。请勿再尝试调用它，直接基于已有信息收尾任务，"
            f"并向用户如实汇报失败情况。",
        )
        logger.warning(
            "[%s] Tool fail breaker tripped | banned=%s streaks=%s",
            self.name,
            newly,
            {n: c for n, c in buckets.regular.items() if n in newly},
        )

    def _check_llm_failures(self, ctx: PluginContext) -> tuple[dict[str, Any], bool]:
        """LLM 失败连击熔断 → (状态更新, 本轮是否有 LLM 失败)。

        计数口径：引擎 ``_plugin_error_stats`` 里的 llm_core 账本（按
        `(plugin_id, message)` 聚合的 count + seq；run 开头清空、run 内累积）
        ——本轮新增计数即本轮失败次数（成功轮不新增）；错误文本按
        `_stable_error_text` 归一后相同才算「同一错误反复」（连击），文本不同
        则重新起计。账本有界（≤8 条），不再随失败次数线性膨胀。

        终态原因按错误类别署名：模型不可用类（错误文本含
        ``MODEL_UNAVAILABLE`` 标记，配置错、永不自愈）→ ``model_unavailable``，
        阈值 3；其它同类失败（瞬态）→ ``llm_fail_loop``，阈值 5（同工具熔断
        口径：合法自愈实测 1-2 次内成功，5 给 2.5 倍裕度）。

        State 命名空间：
            - router.llm_fail_streak : {fingerprint, count}（连击计数）
            - router.llm_error_cursor : {run_id, count, stable_fingerprint,
              stable_count}（已计入的总失败数 + 当前同文失败的已计入数）

        Returns:
            (状态更新字典, 本轮是否有 LLM 失败)
        """
        errors = self._llm_core_errors(ctx.state)
        run_id = str(ctx.state.get("run_id") or "")
        cursor = ctx.state.get("router.llm_error_cursor")
        same_cursor = isinstance(cursor, dict) and str(cursor.get("run_id") or "") == run_id
        seen_total = int(cursor.get("count") or 0) if same_cursor else 0
        seen_stable = int(cursor.get("stable_count") or 0) if same_cursor else 0

        total = sum(int(err.get("count") or 0) for err in errors)

        streak = ctx.state.get("router.llm_fail_streak")
        same_run = isinstance(streak, dict) and str(streak.get("run_id") or "") == run_id
        fingerprint = str(streak.get("fingerprint") or "") if same_run else ""
        count = int(streak.get("count") or 0) if same_run else 0

        if not errors or total <= seen_total:
            # 本轮无新增 LLM 错误（成功轮）→ 连击清零（自愈不误伤）。
            # 无历史连击时零状态更新——不碰无关 state 键。
            if count <= 0:
                return {}, False
            return {
                "router.llm_error_cursor": {
                    "run_id": run_id,
                    "count": total,
                    "stable_fingerprint": "",
                    "stable_count": 0,
                },
                "router.llm_fail_streak": {"run_id": run_id, "fingerprint": "", "count": 0},
            }, False

        # 最新一条 = seq 最大（并行迭代归并按归并序重发 seq）。
        latest = max(
            errors,
            key=lambda err: (
                int(err.get("seq") or 0),
                errors.index(err),
            ),
        )
        message = str(latest.get("message") or "")
        stable = _stable_error_text(message)
        current_fp = hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()[:12]  # noqa: S324
        # 同一稳定文本的累计失败数（覆盖 request_id 等易变字段：
        # 2026-10-23 教训——按全文指纹会把同一次 429 判成异文，连击永不触发）。
        stable_total = sum(
            int(err.get("count") or 0)
            for err in errors
            if _stable_error_text(str(err.get("message") or "")) == stable
        )
        # 本轮新增的同文失败数（游标跨 run/换文归零）。
        run = max(0, stable_total - seen_stable)
        new_total = total - seen_total
        # 本段是否全为同文失败：否则只计尾段同文数（异文交替不累加）。
        segment_uniform = run == new_total
        count = count + run if (fingerprint == current_fp and segment_uniform) else run
        updates: dict[str, Any] = {
            "router.llm_error_cursor": {
                "run_id": run_id,
                "count": total,
                "stable_fingerprint": current_fp,
                "stable_count": stable_total,
            },
            "router.llm_fail_streak": {
                "run_id": run_id,
                "fingerprint": current_fp,
                "count": count,
            },
        }

        model_unavailable = MODEL_UNAVAILABLE_MARKER in message
        threshold = (
            self._llm_fail_break_threshold
            if model_unavailable
            else self._llm_fail_break_threshold_transient
        )
        if count < threshold:
            logger.info(
                "[%s] LLM failure streak | count=%d threshold=%d class=%s",
                self.name,
                count,
                threshold,
                "model_unavailable" if model_unavailable else "transient",
            )
            return updates, True

        stop_reason = "model_unavailable" if model_unavailable else "llm_fail_loop"
        detail = _error_detail(message)
        if model_unavailable:
            user_msg = (
                f"模型不可用，已终止本轮执行（连续 {count} 次相同失败）：{detail}"
                "请检查模型配置（llm.yaml models / defaults.chat / model_tier）后重试。"
            )
        else:
            user_msg = f"模型调用连续失败，已终止本轮执行（连续 {count} 次相同失败）：{detail}"
        logger.warning(
            "[%s] LLM failure breaker tripped | stop_reason=%s count=%d",
            self.name,
            stop_reason,
            count,
        )
        terminated = self._terminate_pipeline(
            ctx,
            updates,
            f"LLM 连续失败 {count} 次（{stop_reason}）",
            ctx.state.get("router.duplicate_intercepts", 0),
            message=user_msg,
            stop_reason=stop_reason,
        )
        return terminated, True

    @staticmethod
    def _llm_core_errors(state: dict[str, Any]) -> list[dict[str, Any]]:
        """本 run 的 llm_core 错误账本条目。

        引擎 ``_plugin_error_stats``：按 `(plugin_id, message)` 聚合的
        `{plugin_id, code, message, count, seq}` 有界数组（run 开头清空、
        run 内累积）；不同文本各一条，同文本只累加 count。
        """
        errors = state.get("_plugin_error_stats")
        if not isinstance(errors, list):
            return []
        return [
            e
            for e in errors
            if isinstance(e, dict) and str(e.get("plugin_id") or "").endswith("llm_core")
        ]

    def _handle_duplicate_tool_calls(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        count: int,
    ) -> dict[str, Any]:
        """处理工具调用重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        tool_desc = self._build_tool_call_description(ctx)
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, tool_desc, intercepts)

        # 第二级：重复达到阈值 → 拦截 + 路由回 LLM
        if count >= self._max_duplicate_calls:
            warning = f"检测到重复工具调用{tool_desc}，已跳过执行。请不要再次使用相同的工具和参数，请尝试其他方法。"
            messages = list(ctx.state.get("messages", []))
            strip_ops, stripped = self._build_strip_ops(messages)
            self._apply_strip_locally(messages, strip_ops)
            merge_ops = self._build_merge_ops(messages, f"[DuplicateCheck] {warning}")
            updates[StateKeys.RAW_TOOL_CALLS] = []
            updates["router.duplicate_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Duplicate tool calls intercepted | count=%d intercepts=%d tool=%s stripped_assistants=%d",
                self.name,
                count,
                intercepts + 1,
                tool_desc,
                stripped,
            )
            # 二级路由回 LLM 经状态键表达（DSL 路由 router.duplicate_back_llm 消费）
            updates["router.duplicate_back_llm"] = True
            updates["messages"] = {"_ops": strip_ops + merge_ops}
            return updates

        # 第一级：早期重复 → 注入软提示，工具调用仍执行
        hint = self._build_hint(count, tool_desc)
        self._inject_hint(ctx, updates, hint)
        logger.info(
            "[%s] Duplicate tool call soft hint | count=%d tool=%s",
            self.name,
            count,
            tool_desc,
        )
        return updates

    def _handle_repetitive_output(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        count: int,
    ) -> dict[str, Any]:
        """处理输出内容重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, "重复输出", intercepts)

        # 第二级：重复达到阈值 → 清空输出 + 路由回 LLM
        if count >= self._max_repetitive_output:
            warning = "检测到重复输出相似内容，请尝试其他方法或给出不同的回复。"
            self._inject_warning(ctx, updates, warning)
            updates[StateKeys.RAW_RESULT] = ""
            updates["router.repetitive_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Repetitive output intercepted | count=%d intercepts=%d",
                self.name,
                count,
                intercepts + 1,
            )
            updates["router.duplicate_back_llm"] = True
            return updates

        # 第一级：早期重复 → 注入软提示（count+1 = 连续相似输出的总轮数）
        hint = f"你已连续 {count + 1} 次输出相似内容，请尝试换一种方式回复。"
        self._inject_hint(ctx, updates, hint)
        logger.info(
            "[%s] Repetitive output soft hint | count=%d",
            self.name,
            count,
        )
        return updates

    def _terminate_pipeline(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        desc: str,
        intercepts: int,
        message: str | None = None,
        stop_reason: str = "duplicate_loop",
    ) -> dict[str, Any]:
        """终止管道，主 agent 注入用户通知，子 agent 直接终止。

        有任务身份（task.id 在场）时同批写失败归因注记（task.error /
        task.failure_class / task.failure_reason），上级任务视图与 task_failed
        富通知据此透传真实死因。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            desc: 重复描述
            intercepts: 当前拦截次数
            message: 主 agent 的用户通知文案（缺省 = 重复死循环文案；同时是
                失败归因注记来源）

        Returns:
            包含终止路由信号的更新字典
        """
        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "L1")
        is_main = agent_level in ("L1", "L1_MAIN") or ctx.state.get("delegate_depth", 0) == 0

        # 失败归因随写（§10.2 裁定同款，先例 task_evaluate 三件套）：上级任务
        # 视图与 task_failed 富通知只认 state 注记，不写 = 上级只见 failed 不见
        # 死因（2026-10-23 额度耗尽空转事故）。不写 task.status——终态投影由
        # run 收尾派生（run.failed 派生 task_failed 通知上级并同批随写两面投
        # 影；提前写终态属越权裁决面）。
        task_id = str(ctx.state.get(StateKeys.TASK_ID) or "")
        reason = message or desc
        if task_id:
            updates["task.error"] = _error_detail(reason)
            updates["task.failure_class"] = stop_reason
            updates["task.failure_reason"] = reason

        if is_main:
            # 终止通知是给用户的自然语言收尾，append assistant 消息（不并入末尾消息）
            updates["messages"] = {
                "_ops": [{"op": "set", "msg": {"role": "assistant", "content": message or _MAIN_AGENT_TERMINATE_MSG}}]
            }
            logger.warning(
                "[%s] Pipeline terminating (main agent) | intercepts=%d desc=%s",
                self.name,
                intercepts,
                desc,
            )
        else:
            logger.warning(
                "[%s] Pipeline terminating (sub agent) | intercepts=%d desc=%s",
                self.name,
                intercepts,
                desc,
            )

        updates[StateKeys.SHOULD_STOP] = True
        updates["router.stop_reason"] = stop_reason
        return updates

    def _build_hint(self, count: int, tool_desc: str) -> str:
        """构建早期软提示消息。

        Args:
            count: 重复计数（窗口内首次出现之后的累计出现次数，含本次）
            tool_desc: 工具调用描述

        Returns:
            软提示消息字符串
        """
        template = _HINT_TEMPLATES.get(count, _HINT_TEMPLATES[max(_HINT_TEMPLATES)])
        # 模板来自 _HINT_TEMPLATES 配置常量的运行时渲染，非静态拼接（f-string 不适用）
        return template.format(count=count, total=count + 1, tool=tool_desc)

    def _build_tool_call_description(self, ctx: PluginContext) -> str:
        """构建工具调用的可读描述。

        Args:
            ctx: 插件执行上下文

        Returns:
            工具调用描述字符串，如 "file_read(path=xxx)"
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return ""
        parts = []
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            # raw_tool_calls 生产方（llm adapter _parse/_normalize_tool_calls）固定
            # 输出 {"id","name","arguments"}。
            args = tc.get("arguments", {})
            if isinstance(args, str):
                parts.append(f"{name}({args})")
            else:
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                parts.append(f"{name}({args_str})")
        return "、".join(parts)

    def _inject_warning(self, ctx: PluginContext, updates: dict[str, Any], message: str) -> None:
        """注入强警告（输出重复二级拦截时使用），merge ops 写入 updates。"""
        updates["messages"] = {
            "_ops": self._build_merge_ops(list(ctx.state.get("messages", [])), f"[DuplicateCheck] {message}")
        }

    def _inject_hint(self, ctx: PluginContext, updates: dict[str, Any], message: str) -> None:
        """注入软提示（第一级早期提示时使用），merge ops 写入 updates。"""
        updates["messages"] = {
            "_ops": self._build_merge_ops(list(ctx.state.get("messages", [])), f"[DuplicateCheck] {message}")
        }

    def _build_strip_ops(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """构造移除末尾连续 assistant(tool_calls) 的 delete ops。

        Level-2 拦截会清空 RAW_TOOL_CALLS，但 llm_core 已 append 的
        assistant(tool_calls) 仍残留 → 永远等不到 tool result → 未配对消息。
        因此拦截时同步产出删除 ops 撤销本次工具调用意图。

        Args:
            messages: 当前 messages 数组（元素带引擎分配的 seq）

        Returns:
            (delete ops 列表, 被剥离的 assistant 消息数量)
        """
        ops: list[dict[str, Any]] = []
        stripped = 0
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                seq = msg.get("seq")
                if seq is None:
                    break  # 无 seq 不可定位（真实链路消息恒带引擎 seq）
                ops.append({"op": "set", "seq": seq, "msg": None})
                stripped += 1
                continue
            break
        ops.reverse()
        return ops, stripped

    @staticmethod
    def _apply_strip_locally(messages: list[dict[str, Any]], strip_ops: list[dict[str, Any]]) -> None:
        """把 delete ops 应用到本地 messages 副本（后续 merge op 的定位基准）。"""
        removed = {op["seq"] for op in strip_ops}
        messages[:] = [m for m in messages if m.get("seq") not in removed]

    def _build_merge_ops(self, messages: list[dict[str, Any]], content: str) -> list[dict[str, Any]]:
        """构造把提醒合并进末尾消息的 slot ops（引擎三落点唯一通道）。

        合并规则：
        - 末尾为 tool/assistant/system 且带 seq → set modify（同 seq 替换，
          content 合并，保持 assistant(tool_calls)→tool 序列完整）
        - 末尾为 user、空数组或无 seq 可定位 → set 缺 seq = append 一条 user
          （append 无配对约束；无 seq 的受保护角色退化为 append 而非静默丢弃）

        Args:
            messages: 当前 messages 数组（元素带引擎分配的 seq）
            content: 要合并/追加的提醒文本

        Returns:
            slot ops 列表
        """
        if not messages:
            return [{"op": "set", "msg": {"role": "user", "content": content}}]

        last = messages[-1]
        seq = last.get("seq")
        if last.get("role") in ("tool", "assistant", "system") and seq is not None:
            merged = dict(last)
            original = merged.get("content") or ""
            merged["content"] = f"{original}\n\n{content}" if original else content
            return [{"op": "set", "seq": seq, "msg": merged}]

        return [{"op": "set", "msg": {"role": "user", "content": content}}]

    def _check_duplicate_calls(self, ctx: PluginContext) -> dict[str, Any]:
        """检查工具调用重复（单调用粒度滑动窗口）。

        对每个调用生成 工具名+参数 签名，与最近 signature_window_size 条
        签名窗口累计比对：重复计数 = 窗口 + 本轮中同一签名的"多余出现次数"
        （相对首次出现）最大值。整组只比上一轮的旧算法对多工具场景恒不
        计数——部分重复（组内一个调用变化即整组重置）与交替循环永不触发。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典；无工具调用轮返回显式清零（计数只表本轮，
            滑动窗口保留——签名未过窗，后续轮重复仍可由窗口重数检出）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"router.duplicate_count": 0}

        current_signatures = [self._call_signature(tc.get("name", ""), tc.get("arguments", {})) for tc in tool_calls]

        window = list(ctx.state.get("router.recent_tool_sigs", []))
        round_counts: dict[str, int] = {}
        for sig in current_signatures:
            round_counts[sig] = round_counts.get(sig, 0) + 1
        duplicate_count = max(
            (window.count(sig) + n - 1 for sig, n in round_counts.items()),
            default=0,
        )
        if duplicate_count > 0:
            logger.debug(
                "[%s] Duplicate tool call detected | count=%d",
                self.name,
                duplicate_count,
            )

        updated_window = (window + current_signatures)[-(self._signature_window_size) :]
        return {
            "router.duplicate_count": duplicate_count,
            "router.recent_tool_sigs": updated_window,
        }

    def _call_signature(self, name: str, args: Any) -> str:
        """单调用签名 = 工具名 + 全参数（排序后 md5 短摘要）。

        全参数入签名：reason 这类审计必填字段每次调用语义不同，参数微变
        即不同签名（可绕过签名去重的既有判例）。str JSON/非法 JSON/非 dict
        参数归一后参与（与 _check_duplicate_calls 的历史口径一致）。

        Args:
            name: 工具名
            args: 工具参数（dict / JSON 字符串 / 其他）

        Returns:
            8 位十六进制签名
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        return hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()[:8]  # noqa: S324

    def _check_repetitive_output(
        self, ctx: PluginContext, *, llm_failed: bool = False
    ) -> dict[str, Any]:
        """检查输出内容重复。

        通过对 LLM 输出文本的前 N 个字符生成签名，
        与上一次输出对比，高度相似则增加重复计数。

        llm_failed=True（本轮 llm_core 调用失败）→ 本轮**不计**重复输出：
        失败轮的 raw_result 是上一轮残留（同一文本），计入即把「同一错误
        反复」误诊成「输出重复」（2026-10-20 事故：终态原因被写成
        duplicate_loop，与真实死因不符）。此时计数与基线均保持不变——既不
        累加也不清零（清零会抹掉此前成功轮真实积累的重复计数）。

        Args:
            ctx: 插件执行上下文
            llm_failed: 本轮是否有 LLM 调用失败

        Returns:
            重复检查结果字典；无输出轮返回显式清零（上次输出签名保留，
            供下一个有输出轮次对比）
        """
        if llm_failed:
            return {
                "router.repetitive_count": int(
                    ctx.state.get("router.repetitive_count", 0) or 0
                )
            }
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if raw_result is None:
            return {"router.repetitive_count": 0}

        # 包含评估结论 JSON 的输出不应被判定为重复（即使文本相似）
        raw_text = str(raw_result)
        if "evaluation_result" in raw_text and '"passed"' in raw_text:
            return {
                "router.last_response": hashlib.md5(raw_text[:500].encode()).hexdigest()[:8],
                "router.last_response_text": raw_text[:500],
                "router.repetitive_count": 0,
            }

        # 生成当前输出签名（取前 500 字符）
        current_text = str(raw_result)[:500]
        current_hash = hashlib.md5(current_text.encode()).hexdigest()[:8]  # noqa: S324

        last_hash = ctx.state.get("router.last_response", "")

        # 对比
        repetitive_count = ctx.state.get("router.repetitive_count", 0)
        if current_hash and current_hash == last_hash:
            repetitive_count += 1
            logger.debug(
                "[%s] Repetitive output detected | count=%d",
                self.name,
                repetitive_count,
            )
        else:
            # 相似度检查（简单字符级对比）
            last_text = ctx.state.get("router.last_response_text", "")
            if last_text and self._compute_similarity(current_text, last_text) > self._similarity_threshold:
                repetitive_count += 1
                logger.debug(
                    "[%s] Similar output detected | similarity>%.2f | count=%d",
                    self.name,
                    self._similarity_threshold,
                    repetitive_count,
                )
            else:
                repetitive_count = 0  # 不同则重置

        return {
            "router.repetitive_count": repetitive_count,
            "router.last_response": current_hash,
            "router.last_response_text": current_text,
        }

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度。

        使用简单的 Jaccard 相似度（基于字符 n-gram）。

        Args:
            text1: 第一段文本
            text2: 第二段文本

        Returns:
            相似度值 [0, 1]
        """
        if not text1 or not text2:
            return 0.0

        # 简单 word-level Jaccard
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)
