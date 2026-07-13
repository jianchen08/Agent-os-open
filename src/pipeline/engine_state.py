"""管道引擎 — 状态管理与挂起/恢复机制。

提供管道状态的安全复制、挂起引擎的全局注册表、
日志过滤等与管道状态生命周期相关的基础设施。

公共接口：
- _safe_deepcopy: 安全复制 state 字典
- register_suspended_engine / unregister_suspended_engine / get_global_suspended_engine
- _current_pipeline_id: 上下文变量
"""

from __future__ import annotations

import contextvars
import copy as _copy
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.engine import PipelineEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 上下文变量：当前正在执行的 pipeline_id
# ---------------------------------------------------------------------------
_current_pipeline_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_pipeline_id",
    default=None,
)

# ---------------------------------------------------------------------------
# 全局挂起引擎注册表
# ---------------------------------------------------------------------------
_GLOBAL_SUSPENDED_ENGINES: dict[str, PipelineEngine] = {}  # type: ignore[valid-type]


def register_suspended_engine(pipeline_id: str, engine: PipelineEngine) -> None:  # type: ignore[valid-type]
    """将挂起的引擎实例注册到全局表。"""
    _GLOBAL_SUSPENDED_ENGINES[pipeline_id] = engine
    logger.info(
        "[Engine] 全局注册挂起引擎: pipeline=%s, engine_pid=%s, total=%d",
        pipeline_id,
        id(engine),
        len(_GLOBAL_SUSPENDED_ENGINES),
    )


def unregister_suspended_engine(pipeline_id: str) -> None:
    """从全局表移除挂起的引擎实例。"""
    _GLOBAL_SUSPENDED_ENGINES.pop(pipeline_id, None)


def get_global_suspended_engine(pipeline_id: str) -> PipelineEngine | None:  # type: ignore[valid-type]
    """根据 pipeline_id 查找全局挂起的引擎实例。"""
    return _GLOBAL_SUSPENDED_ENGINES.get(pipeline_id)


# ---------------------------------------------------------------------------
# 安全深拷贝基础设施（避免 RecursionError）
# ---------------------------------------------------------------------------
_SAFE_JSON_TYPES = (str, int, float, bool, type(None))

_SKIP_COPY_KEYS = frozenset(
    {
        "on_chunk",
    }
)

_MAX_MANUAL_COPY_DEPTH = 20


def _safe_deepcopy(state: dict) -> dict:
    """安全复制 state，避免 RecursionError。

    不使用 copy.deepcopy（在 state 包含复杂对象时会触发 RecursionError，导致
    _apply_route(wait) 崩溃、管道异常退出），改用逐键手动复制：
    - JSON 安全类型 (str/int/float/bool/None) → 直接引用
    - list/dict → 递归手动复制（受深度限制保护）
    - 其他类型 → 浅拷贝或直接引用
    """
    safe: dict = {}
    for k, v in state.items():
        if k in _SKIP_COPY_KEYS:
            continue
        if isinstance(v, _SAFE_JSON_TYPES):
            safe[k] = v
        elif isinstance(v, list):
            safe[k] = _manual_copy_list(v, depth=0)
        elif isinstance(v, dict):
            safe[k] = _manual_copy_dict(v, depth=0)
        elif isinstance(v, (set, tuple)):
            try:
                safe[k] = type(v)(v)
            except (TypeError, ValueError):
                safe[k] = v
        else:
            # 优先浅拷贝，避免 JSON roundtrip 丢失类型信息
            safe[k] = _copy.copy(v)
    return safe


def _manual_copy_dict(d: dict, depth: int) -> dict:
    """手动深拷贝 dict，受深度限制保护。"""
    if depth > _MAX_MANUAL_COPY_DEPTH:
        return dict(d)
    result: dict = {}
    for k, v in d.items():
        if isinstance(v, _SAFE_JSON_TYPES):
            result[k] = v
        elif isinstance(v, list):
            result[k] = _manual_copy_list(v, depth + 1)
        elif isinstance(v, dict):
            result[k] = _manual_copy_dict(v, depth + 1)
        else:
            result[k] = v
    return result


def _manual_copy_list(lst: list, depth: int) -> list:
    """手动深拷贝 list，受深度限制保护。"""
    if depth > _MAX_MANUAL_COPY_DEPTH:
        return list(lst)
    result: list = []
    for v in lst:
        if isinstance(v, _SAFE_JSON_TYPES):
            result.append(v)
        elif isinstance(v, list):
            result.append(_manual_copy_list(v, depth + 1))
        elif isinstance(v, dict):
            result.append(_manual_copy_dict(v, depth + 1))
        else:
            result.append(v)
    return result


# ---------------------------------------------------------------------------
# 管道日志过滤器
# ---------------------------------------------------------------------------
class _PipelineLogFilter(logging.Filter):
    """只放行当前 context 中匹配 pipeline_id 的日志记录。"""

    def __init__(self, pipeline_id: str):
        super().__init__()
        self.pipeline_id = pipeline_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _current_pipeline_id.get() == self.pipeline_id
