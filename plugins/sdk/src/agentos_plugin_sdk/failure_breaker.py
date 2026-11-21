"""同签名工具失败熔断器（run 生命周期防线③b）。

背景（监控台账第 198-200 轮）：同 plugin+tool 连续失败无机制打断——
llm.complete_stream 同签名失败 91 次（每分钟重试烧轮次与配额），每次失败
返回都不带「已连败多次、勿再原地重试」语义，agent 无从熔断。

设计（本模块，ADR 2026-11-17-run-lifecycle-defense-lines）：

- **语义标记熔断，不硬拒执行**：连续失败达阈值后，后续（第 N+1 次起）失败
  返回升级为聚合诊断 + ``retryable: False`` 语义标记（强制熔断上报）。调用
  仍照常执行——参数修正/供给修复后的首次调用必须能到达（计数在同点成功后
  清零的语义要求如此），硬拒会让恢复路径失活。
- **签名粒度 = 工具注册名**：独占形态一个 sidecar 即一个插件（进程隐含
  plugin 身份）；合宿形态工具名自带 ``{plugin_id}.`` 前缀——键即签名。
- **阈值**：``AGENTOS_TOOL_FAILURE_BREAKER_THRESHOLD``（默认 3，非法值回落
  默认；sidecar 进程启动时定型）。

消费方 = server.py 工具分发失败出口（异常 / 参数绑定 / 业务 ``success:
False``），三出口共用同一计数面。
"""

from __future__ import annotations

import os

__all__ = [
    "ToolFailureBreaker",
    "DEFAULT_FAILURE_BREAKER_THRESHOLD",
    "FAILURE_BREAKER_THRESHOLD_ENV",
    "resolve_failure_breaker_threshold",
]

# 熔断阈值默认值：连续 3 次同签名失败即升级失败返回（第 4 次带标记）。
DEFAULT_FAILURE_BREAKER_THRESHOLD = 3

# 阈值 env 键名（任务裁定「env 可调 N」）。
FAILURE_BREAKER_THRESHOLD_ENV = "AGENTOS_TOOL_FAILURE_BREAKER_THRESHOLD"

# 失败样本截断长度（聚合诊断只留签名摘要，不倒全量错误正文）。
_SAMPLE_TRUNCATE = 200

# 聚合诊断携带的最大样本数。
_MAX_SAMPLES = 5


def resolve_failure_breaker_threshold(environ: dict[str, str] | None = None) -> int:
    """从 env 解析阈值；未设/非法（非正整数）回落默认 3。"""
    source = os.environ if environ is None else environ
    raw = source.get(FAILURE_BREAKER_THRESHOLD_ENV, "")
    if raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_FAILURE_BREAKER_THRESHOLD


class ToolFailureBreaker:
    """同签名（plugin+tool）连续失败熔断器。

    计数语义：连续失败递增（带样本环形摘要）；同点成功即清零（含样本）。
    ``streak >= threshold`` 时调用处于「熔断标记态」——该次失败返回应携带
    聚合诊断 + ``retryable: False``。
    """

    def __init__(self, threshold: int | None = None) -> None:
        self.threshold = (
            threshold
            if threshold is not None and threshold > 0
            else resolve_failure_breaker_threshold()
        )
        self._streaks: dict[str, int] = {}
        self._samples: dict[str, list[str]] = {}

    def breaker_open(self, tool_key: str) -> bool:
        """该签名当前是否处于熔断标记态（连续失败已达阈值）。"""
        return self._streaks.get(tool_key, 0) >= self.threshold

    def record_failure(self, tool_key: str, error_text: str) -> None:
        """记一单失败：连击递增 + 样本环形摘要（截断，保留最近 N 条）。"""
        self._streaks[tool_key] = self._streaks.get(tool_key, 0) + 1
        samples = self._samples.setdefault(tool_key, [])
        samples.append(error_text[:_SAMPLE_TRUNCATE])
        del samples[:-_MAX_SAMPLES]

    def record_success(self, tool_key: str) -> None:
        """同点成功：连击与样本清零（参数修正/供给修复后自然恢复）。"""
        self._streaks.pop(tool_key, None)
        self._samples.pop(tool_key, None)

    def streak(self, tool_key: str) -> int:
        """当前连续失败数（观测面）。"""
        return self._streaks.get(tool_key, 0)

    def aggregated_diagnosis(self, tool_key: str) -> str:
        """聚合诊断：连击数 + 阈值 + 最近失败样本摘要 + 熔断指令。"""
        streak = self.streak(tool_key)
        samples = self._samples.get(tool_key, [])
        rendered = "; ".join(
            f"[{i + 1}] {sample}" for i, sample in enumerate(samples)
        ) or "(无样本)"
        return (
            f"BREAKER: 工具 {tool_key} 已连续 {streak} 次同签名失败"
            f"（熔断阈值 {self.threshold}）——retryable=false，勿原地重试；"
            f"请按上方诊断修复供给/配置后重试，或作为阻塞上报调度方。"
            f"最近失败样本（截断）: {rendered}"
        )
