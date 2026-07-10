"""配置兜底默认值 — 单一来源。

所有代码中的硬编码配置默认值必须从这里 import，禁止再写裸数字。
这些值只在 yaml 加载失败时作为最后防线，正常运行时不应被读到（yaml 配置生效）。

真配置源：config/system/context_window_config.yaml（被 ConfigCenter 加载）。
本模块只负责"yaml 读不到时的兜底"，与 yaml 保持同步。
"""

from __future__ import annotations

# ── 上下文窗口 ──
CONTEXT_WINDOW_DEFAULT: int = 128000

# ── 上下文压缩 ──
COMPRESS_TRIGGER_RATIO: float = 0.55

# ── 压缩预算比例（与 context_window_config.yaml budgets 同步）──
BUDGET_SYSTEM_PROMPT: float = 0.08
BUDGET_TOOLS_DESCRIPTION: float = 0.0
BUDGET_STATIC_VARS: float = 0.05
BUDGET_DYNAMIC_VARIABLES: float = 0.05
BUDGET_L3: float = 0.03
BUDGET_L2: float = 0.08
BUDGET_L1: float = 0.15
BUDGET_RECENT: float = 0.25
BUDGET_RETRIEVAL: float = 0.08
BUDGET_RESPONSE_RESERVE: float = 0.23

# ── 压缩批处理 ──
# 分片大小 = compression_window * COMPRESSION_BATCH_RATIO
# 太大会导致压缩模型生成质量下降，0.5 是性能/质量平衡点
COMPRESSION_BATCH_RATIO: float = 0.5

# ── 单轮次最大占比（recent_budget 的比例）──
MAX_TURN_RATIO: float = 0.5
