"""流式协议翻译共享根裸模块——DSH 8 事件形态（块索引化）。

真源单源：本文件由 plugins/shared/llm/streaming.py 迁入（2026-11-04 网关化
第二步），llm 侧同名件退化为 re-export shim。消费者=网关（llm_service）与
provider 插件（llm_provider.* 契约方，如 llm_provider_sample/dsh_adapter），
双方经共享根裸名 import（bootstrap 注入 plugins/shared 根可达）。

—— 以下为原 streaming.py docstring ——
llm.complete_stream 流式协议翻译——DSH 8 事件形态（块索引化）。

把 adapter 归一化 chunk（``on_chunk`` 契约：``{"type", "content"}`` 与
``{"type": "tool_call", "tool_calls": [...]}``）翻译为块索引化事件序列：

- ``block_start{index, block_type}`` / ``text_delta{index, text}`` /
  ``reasoning_delta{index, text}`` / ``tool_call_delta{index, id?, name?,
  arguments_delta}`` / ``block_end{index, block}`` / ``usage{input_tokens,
  output_tokens, ...}`` / ``finish{reason}`` / ``keepalive``。
- 每个 text/reasoning/tool-call 块独立 index，增量按 index 归组；类型切换先
  闭旧块再开新块。并行 tool_call 块（不同流 index）同时保持打开，增量按各自
  块索引发出，收尾统一闭合。
- tool 参数 ``arguments_delta`` 为原始 JSON 字符串增量累积，不中途解析。
- ``finish`` 幂等：断流兜底（finish 前异常）由调用方补发 ``finish{reason:
  error}``，重复触发安全。

事件词汇为 DSH 形态定稿（方案 2026-08-26 变更#1），旧名
``thinking_start/thinking_chunk/thinking_end/stream_chunk/stream_keepalive``
退役，本模块不产出旧事件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from simurg import CORRUPT, SUSPECT, Simurg
from simurg.core import REGISTRY


@dataclass(frozen=True)
class StreamEvent:
    """一个待推送的流式事件（event 名 + 载荷）。"""

    event: str
    payload: dict[str, Any]


# finish reason 词汇收敛：LLM 返回的未知结束原因（如 content_filter）映射 stop，
# 消费端只按 stop/length/tool_calls/error/interrupted 五值渲染（interrupted =
# 调用方停止，llm_core 取消路径的半截返回语义）。
_FINISH_REASON_MAP: dict[str | None, str] = {
    None: "stop",
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "error": "error",
    "interrupted": "interrupted",
}


def map_finish_reason(reason: str | None) -> str:
    """把 adapter 返回的 finish_reason 收敛为协议五值之一（stop/length/tool_calls/error/interrupted）。"""
    return _FINISH_REASON_MAP.get(reason, "stop")


# ── 流式退化循环检测（单点真源）────────────────────────────────────────
# 落点：协议翻译器，不是某个 provider 的消费回调。网关（llm_service）与所有
# provider 插件（llm_provider.<type>.complete_stream）都经 StreamTranslator 产出
# DSH 事件，检测器放在这里才是一次覆盖两条路径（2026-11-07 事故：思考通道原地
# 刷同一 emoji 452 次，13 分钟无任何一层拦截）。
#
# 引擎 = 成熟件 SIMURG 1.0.4（Apache-2.0，流中截断 LLM 解码退化的 conformal
# 五检测器融合，自带 reasons/onset），自研四信号 RepetitionGuard 整体退役
# （2026-11-12 装机 12bb 会话一天 14 次误判、ev06 留证四信号重放全不命中=
# 不可自证，换件裁定见 ADR 2026-11-12-degen-detector-simurg-adoption）。
# 必带配置两条（调研文档 §3 vetting 实测，缺一必炸）：
_EXPECTED_SCRIPTS = ("latin", "cjk")
"""默认 ``('latin','cyrillic')`` 不含汉字 → 中英混排被 foreign-script 规则
误杀（实测 8/14 真实留证误杀）。必须显式带 cjk。"""
_SIMURG_DETECTORS = ("rules", "ngram", "sketch", "simhash", "entropy")
"""显式剔除 pulse 深度分层：环境有 torch 时它自动激活且不受 corrupt_at 控制
（实测融合 p 恒 1.00 乱报）。显式列表同时杜绝「生产 numpy-only 静默降级 vs
开发环境 torch 在场」的环境差异。"""
_HOLD_CHARS = 350
"""首检前滞留字符数（引擎默认）：simurg 的零泄漏 hold 语义不启用（Verdict
的 released 面不被消费，流式 UX 照常直出），此值只决定首个检查点位置——
350 字符处做首检并冻结干净前缀基线。"""
_CHECK_EVERY_CHARS = 400
"""检查点间隔（引擎默认，字符）：检测延迟上界。"""

# ── 结构豁免仲裁（旧守卫唯一保留资产）───────────────────────────────
# simurg 无结构内容豁免：schema/代码模板倾倒（合法重复）会被拦（调研文档 §D，
# 11-10 误判回归的教训形态）。仲裁：corrupt ∧ 主因含 repetition ∧ 重复单元
# 结构符号密集 → 降级 suspect 放行 + 留证注明豁免。粒度=重复单元级（容忍数字
# 漂移的最优周期对齐导出单元），勿用窗级粗比替代语义——窗级在混合流（干净
# 前缀+退化尾部）会把前缀的结构密度错记到退化段头上。
_STRUCTURAL_CHARS = frozenset('=(){}[]<>|&;"\':-*#')
"""结构符号集（旧守卫 ``_REPETITION_LINE_STRUCTURAL_CHARS`` 原样继承——经
生产回归验证的资产）。"""
_THINKING_SOFT_REASONS = (
    "numeric dump",
    "semantic drift",
    "structural artifacts",
    "surprise collapse",
)
"""思考通道只标记不杀的判定原因（非复读统计信号）。

思考通道常含编号清单、哈希/计数、代码/脚本片段与技术英文说明，simurg 的
``digit_frac > 0.28`` 数字倾倒线、semantic drift、``structural_density > 1.2``
结构符号线与 surprise collapse 会在这些合法内容上触发：

- 2026-11-12 装机 15:19~15:42 六例 ``digit_frac 0.29~0.37``（编号核验清单）；
- 17:58 一例 371 字符技术英文（凭据/端口排查）被 ``structural artifacts
  density=1.3`` 杀断，原文无任何重复；
- 19:01 一例构建脚本内容被 ``surprise collapse low_frac=0.96; semantic drift
  hamming=0.56`` 杀断，原文是 ``deploy/build_mobile.sh`` 的 for 循环片段。

思考面不面向用户，这些非复读统计信号单独出现时降级 suspect；复读/词汇崩塌/
同字符长串仍按原判据杀断（正文通道不豁免）。
"""
_VETO_THRESHOLD = 0.10
"""重复单元结构符号占比的豁免线（可调）。实测分开线：schema 单元 ~0.19 /
代码单元 ~0.13 要放，模板 ~0.05 / 块复读 ~0.02 / emoji 0.000 要拦。"""
_UNIT_MIN_PERIOD = 8
"""重复单元的最小周期（1-7 字符周期属字符级倾倒，不是结构内容，不适用豁免）。"""
_UNIT_MAX_PERIOD = 240
"""重复单元的最大周期（更长周期由 simurg 引擎直接判，结构豁免不适用）。"""
_UNIT_AGREE_BAR = 0.55
"""周期确认线：末窗对该周期的全窗一致率下限（容忍数字漂移——schema 的
``field_9``→``field_10``、模板的时间戳递增都靠它放行单元导出）。"""
_VETO_NEIGHBORHOOD_CHARS = 480
"""豁免分析的 onset 邻域长度（任务口径 ~400 字符；取 480 = 2×最大周期，
保证窗口装得下两个完整周期做对齐）。"""
_REPETITION_EVIDENCE_CHARS = 2000
"""判定退化时留证落日志的原文尾部长度（字符）。"""
_STREAM_BUFFER_CHARS = 2000
"""每通道滚动缓冲长度（留证尾部 + 豁免邻域共用）。"""


def _structural_ratio(text: str) -> float:
    """结构符号字符占比（JSON/代码排版判据的度量）。"""
    if not text:
        return 0.0
    hits = sum(1 for ch in text if ch in _STRUCTURAL_CHARS)
    return hits / len(text)


def _repetition_unit(tail: str) -> tuple[str | None, int | None, float]:
    """从 onset 邻域导出重复单元（容忍数字漂移的最优周期对齐）。

    两阶段：先全窗一致率选主导周期（schema 的 ``field_N`` 下标递增、模板的
    时间戳递增只差个别字符，一致率仍高），再在窗内找该周期的最优对齐窗，
    取一个完整周期作为重复单元。严格精确匹配（simurg 自带
    ``find_loop_onset`` 的第二阶段）会被数字漂移击碎——实测代码/schema 夹具
    全数失配，故自持容忍对齐。

    Returns:
        (重复单元文本或 None, 周期或 None, 最优对齐窗一致率)。
        无可确认周期（一致率低于 ``_UNIT_AGREE_BAR`` 或窗太短）时单元为 None
        ——豁免不适用，corrupt 照常生效。
    """
    n = len(tail)
    if n < _UNIT_MIN_PERIOD * 3:
        return None, None, 0.0
    best_p, best_agree = None, 0.0
    for p in range(_UNIT_MIN_PERIOD, min(_UNIT_MAX_PERIOD, n // 2) + 1):
        agree = sum(tail[i] == tail[i + p] for i in range(n - p)) / (n - p)
        if agree > best_agree:
            best_p, best_agree = p, agree
    if best_p is None or best_agree < _UNIT_AGREE_BAR:
        return None, None, best_agree
    p = best_p
    best_j, best_matches = 0, -1
    for j in range(0, n - 2 * p + 1):
        matches = sum(tail[j + k] == tail[j + p + k] for k in range(p))
        if matches > best_matches:
            best_j, best_matches = j, matches
    return tail[best_j : best_j + p], p, best_matches / p


def _thinking_soft_verdict(reasons: list[str]) -> bool:
    """思考通道判定是否只由非复读统计信号构成（不含复读/词汇崩塌）。"""
    return bool(reasons) and all(
        any(tag in reason for tag in _THINKING_SOFT_REASONS) for reason in reasons
    )


def _make_engine() -> Simurg:
    """构造一个 simurg 引擎实例（必带配置在此单点收敛）。"""
    return Simurg(
        expected_scripts=_EXPECTED_SCRIPTS,
        hold=_HOLD_CHARS,
        check_every=_CHECK_EVERY_CHARS,
        detectors=REGISTRY.build(list(_SIMURG_DETECTORS)),
    )


@dataclass
class _ChannelVerdict:
    """单通道最近一次判定快照（留证与 suspect 标记的数据面）。"""

    state: str = "clean"
    p_corrupt: float = 0.0
    reasons: list[str] = field(default_factory=list)
    onset_char: int | None = None
    """环起点（绝对字符位：通道已收字符 + 引擎相对位）。"""
    exempted: bool = False
    """豁免是否生效（结构豁免或思考通道软信号降级时均为真）。"""
    unit_structural_ratio: float | None = None
    """重复单元结构符号占比（豁免度量；未导出单元时为 None）。"""
    unit_period: int | None = None
    """重复单元周期（字符）。"""


class DegenerationDetector:
    """流式输出退化循环检测（正文/思考双通道，simurg 引擎 + 结构豁免仲裁）。

    退化形态 = 模型在同一次调用内部原地打转。判定三态：

    - **clean**：照常。
    - **suspect**：融合分越过 suspect 线（或 corrupt 被结构豁免降级）——只
      标记不杀（流照常产出，``suspect_channels`` 留痕供结果面携带）。
    - **corrupt**（未被豁免）：判退化，调用方应停止生成。

    结构豁免仲裁：corrupt ∧ 判定原因含 repetition ∧ 重复单元结构符号密集
    （≥ ``_VETO_THRESHOLD``）→ 降级 suspect 放行。schema/代码模板倾倒属
    合法重复，simurg 裸判定会拦（11-10 误判回归教训），仲裁放行后旧引擎已
    终态不可逆，换新引擎续跑（基线重冻结，等价旧守卫的 streak 复位）。

    思考通道软信号豁免：corrupt 原因仅由非复读统计信号（numeric dump /
    semantic drift / structural artifacts / surprise collapse）构成 → 降级
    suspect 放行（编号清单/哈希/技术英文/构建脚本属合法思考内容，2026-11-12
    装机误杀现场）；复读/词汇崩塌/同字符长串仍杀，正文通道保留原判据。

    按通道独立计度（思考先退化不代表正文退化），每通道一个 simurg 实例。
    检测器只判不杀：kill 位置由路径决定——网关侧 on_chunk 返回 ``"stop"``
    由 adapter 截断；provider 插件侧读 ``StreamTranslator.stopped`` 自行收流。
    """

    def __init__(
        self,
        *,
        corrupt_at: float = 0.7,
        suspect_at: float = 0.5,
    ) -> None:
        self._corrupt_at = corrupt_at
        self._suspect_at = suspect_at
        self._engines: dict[str, Simurg] = {}
        self._rebase: dict[str, int] = {}
        """当前引擎出生时的通道已收字符数（引擎相对 onset → 绝对位换算）。"""
        self._buffers: dict[str, str] = {}
        self._seen: dict[str, int] = {}
        self._verdicts: dict[str, _ChannelVerdict] = {}
        self._suspects: dict[str, _ChannelVerdict] = {}

    # ── 判定入口 ──────────────────────────────────────────

    def feed(self, channel: str, text: str) -> bool:
        """喂入一段增量文本，返回该通道是否已判退化（corrupt 且未豁免）。

        Args:
            channel: 通道标识（``text`` 正文 / ``thinking`` 思考）——各自独立计度。
            text: 本次增量（可为空串，空串不参与判定）。

        Returns:
            True = 该通道 corrupt 判定未被结构豁免，调用方应停止生成。
        """
        if not text:
            return False
        self._buffers[channel] = (self._buffers.get(channel, "") + text)[-_STREAM_BUFFER_CHARS:]
        seen = self._seen.get(channel, 0) + len(text)
        self._seen[channel] = seen
        engine = self._engines.get(channel)
        if engine is None:
            engine = _make_engine()
            self._engines[channel] = engine
            self._rebase[channel] = seen - len(text)
        return self._arbitrate(channel, engine.feed(text))

    def finish(self, channel: str) -> bool:
        """流收尾终判：返回该通道是否判退化（corrupt 且未豁免）。

        短应答连发不足首个检查点长度（``_HOLD_CHARS``），流中永不触发；引擎
        的 finish 终判补上拦口径（「重复×60」确属退化内容，2026-11-12 裁定）。
        通道从未喂过文本时恒 False。
        """
        engine = self._engines.get(channel)
        if engine is None:
            return False
        return self._arbitrate(channel, engine.finish())

    # ── 仲裁 ──────────────────────────────────────────────

    def _arbitrate(self, channel: str, verdict: Any) -> bool:
        """引擎判定 → 结构豁免仲裁 → 三态落账。返回是否应杀（corrupt 未豁免）。"""
        reasons = [str(r) for r in (verdict.reasons or [])]
        state = str(verdict.state)
        if state != CORRUPT:
            info = _ChannelVerdict(
                state=state,
                p_corrupt=float(verdict.p_corrupt),
                reasons=reasons,
                onset_char=self._absolute_onset(channel, verdict),
            )
            self._verdicts[channel] = info
            if state == SUSPECT:
                self._suspects[channel] = info
            return False

        # corrupt：结构豁免仲裁（重复单元级，onset 邻域）
        unit, period, unit_agree = _repetition_unit(
            self._buffers.get(channel, "")[-_VETO_NEIGHBORHOOD_CHARS:]
        )
        unit_ratio = _structural_ratio(unit) if unit is not None else None
        repetition_driven = any("repetition" in r for r in reasons)
        soft_thinking = channel == "thinking" and _thinking_soft_verdict(reasons)
        exempted = bool(
            (
                repetition_driven
                and unit is not None
                and unit_ratio is not None
                and unit_ratio >= _VETO_THRESHOLD
            )
            or soft_thinking
        )
        info = _ChannelVerdict(
            state=SUSPECT if exempted else CORRUPT,
            p_corrupt=float(verdict.p_corrupt),
            reasons=reasons,
            onset_char=self._absolute_onset(channel, verdict),
            exempted=exempted,
            unit_structural_ratio=unit_ratio,
            unit_period=period,
        )
        self._verdicts[channel] = info
        if exempted:
            # 降级 suspect 放行 + 豁免留痕；旧引擎 corrupt 终态不可逆，换新续跑
            self._suspects[channel] = info
            self._engines[channel] = _make_engine()
            self._rebase[channel] = self._seen.get(channel, 0)
            return False
        return True

    def _absolute_onset(self, channel: str, verdict: Any) -> int | None:
        """引擎相对环起点 → 通道绝对字符位（跨引擎重建换算）。"""
        onset = verdict.onset_char
        if onset is None:
            return None
        return self._rebase.get(channel, 0) + int(onset)

    # ── 状态查询（translator 留证/结果面消费）────────────────

    def seen(self, channel: str) -> int:
        """该通道累计字符数（截断留证用）。"""
        return self._seen.get(channel, 0)

    def recent(self, channel: str, limit: int = _REPETITION_EVIDENCE_CHARS) -> str:
        """该通道最近 ``limit`` 字符原文（截断留证；与判定同源同缓冲）。"""
        return self._buffers.get(channel, "")[-max(0, limit) :]

    def verdict(self, channel: str) -> _ChannelVerdict | None:
        """该通道最近一次判定快照（无判定记录返回 None）。"""
        return self._verdicts.get(channel)

    def suspect_channels(self) -> dict[str, _ChannelVerdict]:
        """suspect 态通道快照（只标记不杀；含结构豁免降级的通道）。"""
        return dict(self._suspects)


@dataclass
class _ToolBlockState:
    """单个 tool_call 块的累积状态（按块索引独立，并行工具互不污染）。"""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class StreamTranslator:
    """chunk → 块索引化事件 的纯翻译器（无 IO，可独立单测）。

    状态：当前活动的块（类型 + 索引）与各块累积内容。块索引按开块顺序分配
    （text/reasoning/tool_call 共享同一递增序列——DSH 形态块索引化）。
    并行 tool_call 块保持打开直到类型切换或 finish 统一闭合。
    """

    def __init__(
        self,
        degeneration_detector: DegenerationDetector | None = None,
    ) -> None:
        self._next_index = 0
        # 当前活动块（最近写入的块）
        self._open_type: str | None = None
        self._open_index = -1
        self._open_text: list[str] = []
        # tool_call 块：流 index → 块索引；块索引 → 累积状态；打开顺序表
        self._tool_stream_to_block: dict[int, int] = {}
        self._tool_blocks: dict[int, _ToolBlockState] = {}
        self._open_tool_indices: list[int] = []
        self.finished = False
        # 退化循环检测器：缺省启用（显式传 None 也用默认档；测试注入口子保留）
        self._degeneration = degeneration_detector or DegenerationDetector()
        self.stopped = False
        """判定退化循环后置真：此后 translate 不再产出事件，调用方应停止生成。"""
        self.stop_reason: str | None = None
        """停止原因（现役仅 ``repetition``；None = 未触发）。"""
        self.stop_channel: str | None = None
        """命中退化的通道（``text`` / ``thinking``；None = 未触发）。"""

    # ── 块管理 ────────────────────────────────────────────

    def _tool_block_end(self, index: int) -> StreamEvent:
        """构造 tool_call 块的 block_end（块索引 → 累积 payload）。"""
        state = self._tool_blocks.get(index, _ToolBlockState())
        return StreamEvent(
            "block_end",
            {
                "index": index,
                "block": {
                    "block_type": "tool_call",
                    "id": state.id,
                    "name": state.name,
                    "arguments": "".join(state.arguments),
                },
            },
        )

    def _close_block(self) -> list[StreamEvent]:
        """闭当前活动块：产出 block_end{index, block}。无活动块返回空。"""
        if self._open_type is None:
            return []
        if self._open_type == "tool_call":
            index = self._open_index
            event = self._tool_block_end(index)
            self._open_tool_indices = [i for i in self._open_tool_indices if i != index]
        else:
            event = StreamEvent(
                "block_end",
                {
                    "index": self._open_index,
                    "block": {"block_type": self._open_type, "text": "".join(self._open_text)},
                },
            )
        self._open_type = None
        self._open_text = []
        return [event]

    def _close_all_tool_blocks(self) -> list[StreamEvent]:
        """闭全部仍打开的 tool_call 块（类型切换离开 tool_call 族时）。"""
        events = self._close_block()  # 活动块若是 tool_call 先闭
        for index in list(self._open_tool_indices):
            events.append(self._tool_block_end(index))
        self._open_tool_indices = []
        return events

    def _open_block(self, block_type: str) -> list[StreamEvent]:
        """开新块：先闭旧块（类型切换闭旧块），再产出 block_start。"""
        if self._open_type == "tool_call":
            # 离开 tool_call 族：闭全部打开的 tool 块（并行块不残留）
            events = self._close_all_tool_blocks()
        else:
            events = self._close_block()
        self._open_type = block_type
        self._open_index = self._next_index
        self._next_index += 1
        events.append(StreamEvent("block_start", {"index": self._open_index, "block_type": block_type}))
        return events

    # ── 事件翻译 ──────────────────────────────────────────

    def translate(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        """翻译单个归一化 chunk 为事件列表（可为空）。

        退化循环判定内联在正文/思考两条增量通道上：corrupt（未豁免）即闭
        当前块并置 ``stopped``，此后本实例不再产出事件（调用方据 ``stopped``
        收流）。suspect 只标记（``suspect``/``suspect_channels``），流照常。
        """
        if self.finished or self.stopped:
            return []
        chunk_type = chunk.get("type", "text")
        if chunk_type == "thinking":
            content = chunk.get("content", "")
            if not content:
                return []
            events = self._open_block("reasoning") if self._open_type != "reasoning" else []
            self._open_text.append(content)
            events.append(
                StreamEvent("reasoning_delta", {"index": self._open_index, "text": content})
            )
            if self._degeneration.feed("thinking", content):
                self._trip("thinking")
                events.extend(self._close_block())
            return events
        if chunk_type == "thinking_end":
            # 思考块由 block_end 闭合
            if self._open_type == "reasoning":
                return self._close_block()
            return []
        if chunk_type == "tool_call":
            return self._translate_tool_call(chunk)
        # text：正文增量
        content = chunk.get("content", "")
        if not content:
            return []
        events = self._open_block("text") if self._open_type != "text" else []
        self._open_text.append(content)
        events.append(StreamEvent("text_delta", {"index": self._open_index, "text": content}))
        if self._degeneration.feed("text", content):
            self._trip("text")
            events.extend(self._close_block())
        return events

    def _trip(self, channel: str) -> None:
        """置退化停止标记（finish 事件仍由 finish() 按调用方给的 reason 产出）。"""
        self.stopped = True
        self.stop_reason = "repetition"
        self.stop_channel = channel

    @property
    def suspect(self) -> bool:
        """是否存在 suspect 态通道（只标记不杀；含结构豁免降级）。"""
        return bool(self._degeneration.suspect_channels())

    @property
    def suspect_channels(self) -> dict[str, _ChannelVerdict]:
        """suspect 态通道快照（通道 → 判定信息）。"""
        return self._degeneration.suspect_channels()

    def repetition_evidence(self, limit: int = _REPETITION_EVIDENCE_CHARS) -> dict[str, Any]:
        """退化截断现场（命中通道 + 判定信息 + 原文尾部），供调用方落日志。

        被截断的内容不会进 assistant 消息（零产出轮 llm_core 直接抛错），不留
        证则事后无法判断是真退化还是守卫误判——2026-11-10 实测 8 次命中全部
        无现场，只能靠猜。旧自研守卫的分析窗（3000）大于留证窗（2000），触发
        指纹可落在留证窗外（自证缺口）；现判定即分析即留证，同一缓冲同源。
        """
        channel = self.stop_channel or "text"
        info = self._degeneration.verdict(channel)
        evidence: dict[str, Any] = {
            "channel": channel,
            "chars": self._degeneration.seen(channel),
            "tail": self._degeneration.recent(channel, limit),
            "state": info.state if info else None,
            "p_corrupt": info.p_corrupt if info else None,
            "reasons": list(info.reasons) if info else [],
            "onset_char": info.onset_char if info else None,
            "exempted": info.exempted if info else False,
            "unit_structural_ratio": info.unit_structural_ratio if info else None,
            "unit_period": info.unit_period if info else None,
            "suspect_channels": {
                ch: v.state for ch, v in self._degeneration.suspect_channels().items()
            },
        }
        return evidence

    def _translate_tool_call(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        """工具调用增量：按流 index 归组累积，arguments_delta 原始字符串。"""
        events: list[StreamEvent] = []
        for tc in chunk.get("tool_calls", []):
            stream_idx = max(int(getattr(tc, "index", 0)), 0)
            # 流 index 首次出现 → 分配新块（并行工具块同时保持打开）
            if stream_idx not in self._tool_stream_to_block:
                if self._open_type is not None and self._open_type != "tool_call":
                    events.extend(self._close_block())
                block_index = self._next_index
                self._next_index += 1
                self._tool_stream_to_block[stream_idx] = block_index
                self._tool_blocks[block_index] = _ToolBlockState()
                self._open_tool_indices.append(block_index)
                self._open_type = "tool_call"
                self._open_index = block_index
                events.append(
                    StreamEvent("block_start", {"index": block_index, "block_type": "tool_call"})
                )
            block_index = self._tool_stream_to_block[stream_idx]
            state = self._tool_blocks[block_index]
            delta: dict[str, Any] = {"index": block_index}
            function = getattr(tc, "function", None)
            if function is not None:
                tc_id = getattr(tc, "id", None) or ""
                if tc_id and not state.id:
                    state.id = tc_id
                    delta["id"] = tc_id
                name = getattr(function, "name", "") or ""
                if name and not state.name:
                    state.name = name
                    delta["name"] = name
                arguments = getattr(function, "arguments", "") or ""
                if arguments:
                    state.arguments.append(arguments)
                    delta["arguments_delta"] = arguments
            if set(delta.keys()) != {"index"}:
                events.append(StreamEvent("tool_call_delta", delta))
        return events

    # ── 收尾 ──────────────────────────────────────────────

    def finish(self, reason: str, usage: dict[str, Any] | None = None) -> list[StreamEvent]:
        """收尾：终判 → 闭全部块 → usage → finish。幂等：已 finish 返回空。

        终判（检测器 finish）：短于首检长度的退化流（如「重复×60」短应答）
        流中永不触发，引擎收尾终判补拦——置 ``stopped``/``stop_reason``，
        调用方结果面据此把本轮判为退化轮（finish 事件 reason 仍按调用方
        传入值产出，协议词汇不变）。
        """
        if self.finished:
            return []
        if not self.stopped:
            for channel in ("thinking", "text"):
                if self._degeneration.finish(channel):
                    self._trip(channel)
                    break
        self.finished = True
        events = self._close_all_tool_blocks() if self._open_type == "tool_call" else self._close_block()
        if usage:
            events.append(
                StreamEvent(
                    "usage",
                    {
                        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
                    },
                )
            )
        events.append(StreamEvent("finish", {"reason": reason}))
        return events
