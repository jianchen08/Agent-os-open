"""工具结果内存缓存核心（SDK 单一真值源）。

pipeline_tool_cache（input，读缓存）与 pipeline_tool_cache_writer（output，
写缓存）共用的缓存实现集中于此，两端同源导入同一模块级单例字典——
替代早期 output 端 importlib 按文件路径加载邻插件源码的暗道。

暴露接口：
- make_cache_key(tool_call, namespace=None) -> str：MD5 缓存键
  （可选 namespace 身份隔离维度 + tool_name + 参数规范 JSON）
- namespace_from_state(state) -> str | None：pipeline state 身份键 →
  隔离 namespace（input 查询端与 output 写入端同源调用，保证两端同维度）
- evict_expired(cache, max_size)：TTL 过期清理 + LRU 超限淘汰（就地修改）
- global_cache() -> dict：进程内共享单例缓存字典
- ToolResultCache：缓存核（is_excluded / get / put），管道插件组合使用，
  writer 亦可直接实例化

本模块自包含（仅标准库）。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

# 默认不缓存的工具：
# - 有副作用工具（写操作 / 外部副作用，结果不可复用）；
# - 路径型读工具 file_read：同内容合法两次独立读（读→改→
#   再读）在 TTL 窗口内会被内容键误合并，第二次读返回改前内容。
DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset(
    {
        "bash_execute",
        "file_read",
        "file_write",
        "file_delete",
        "file_move",
        "file_copy",
        "create_directory",
        "web_operate",
        "task_submit",
        "task_manage",
        "state_update",
    }
)

# 进程内共享单例：input 端查询 / output 端写入必须命中同一条目。
# 两端同 import 本模块，sys.modules 去重保证同一字典。
_GLOBAL_CACHE: dict[str, tuple[Any, float, float]] = {}


def global_cache() -> dict[str, tuple[Any, float, float]]:
    """返回进程内共享的单例缓存字典。"""
    return _GLOBAL_CACHE


def evict_expired(cache: dict[str, tuple[Any, float, float]], max_size: int) -> None:
    """清理过期的缓存条目。

    如果清理后仍超过 max_size，按 LRU 策略移除最久未访问的条目。

    Args:
        cache: 缓存字典（就地修改）
        max_size: 最大条目数
    """
    now = time.time()
    expired_keys = [k for k, (_, exp, _) in cache.items() if now >= exp]
    for k in expired_keys:
        del cache[k]

    if len(cache) > max_size:
        # LRU: 按 last_access_time 排序，移除最久未访问的条目
        sorted_items = sorted(
            cache.items(),
            key=lambda item: item[1][2],
        )
        to_remove = len(cache) - max_size
        for k, _ in sorted_items[:to_remove]:
            del cache[k]


def make_cache_key(tool_call: dict[str, Any], namespace: str | None = None) -> str:
    """根据工具名称和参数生成缓存 key。

    使用 (tool_name + sorted_args_json) 的 MD5 哈希作为 key，
    确保相同参数的工具调用命中同一缓存条目。

    namespace 不为 None 时作为隔离维度前缀参与哈希：同一合宿进程服务
    多会话，返回用户私有数据的工具（memory 族等）同参调用必须按身份
    隔离，跨 namespace 不命中。None 时键与无 namespace 维度完全一致
    （向后兼容）。

    参数读取兼容两种生产形状：
    - 0.2 llm_core 产出 OpenAI 风格 {"id", "name", "arguments"}（arguments
      是 JSON 字符串）；
    - 工具链/测试构造的 {"name", "args"}（args 是 dict）。
    arguments 字符串先 json.loads 解析为 dict 再参与 key 组装，
    保证两种形状下同参调用 key 一致（否则字符串原文参与哈希，
    同一调用在查询/写入两端 key 分叉）。

    Args:
        tool_call: 工具调用描述，包含 name 和 args/arguments
        namespace: 身份隔离维度（如 pipeline_id + user/session 键拼接），
            None 表示无隔离（旧语义）

    Returns:
        MD5 哈希字符串
    """
    tool_name = tool_call.get("name", "")
    args = tool_call.get("args", tool_call.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            args = {"_raw": args}
    raw = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
    if namespace:
        raw = f"{namespace}\x1f{raw}"
    # usedforsecurity=False：非安全用途的缓存键哈希（摘要与裸 md5 相同）
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


# pipeline state 中的身份键（按序拼接为缓存 namespace）：
# - pipeline_id 必有（内核创建管道时注入）；
# - user_id / session_id 存在时并入（param_inject/prompt_build 同源键名）。
_NAMESPACE_STATE_KEYS: tuple[str, ...] = ("pipeline_id", "user_id", "session_id")


def namespace_from_state(state: dict[str, Any]) -> str | None:
    """从管道 state 组装缓存 namespace（身份隔离维度）。

    读 _NAMESPACE_STATE_KEYS 列出的身份键，非空值按序以 \\x1f 拼接；
    全部缺失（如测试构造的最小 state）返回 None——调用端以 None 走
    旧键语义，行为向后兼容。

    Args:
        state: 管道状态字典（PluginContext.state）

    Returns:
        namespace 字符串；无任何身份键时 None
    """
    parts = [str(state.get(key) or "") for key in _NAMESPACE_STATE_KEYS]
    joined = "\x1f".join(p for p in parts if p)
    return joined or None


class ToolResultCache:
    """工具结果缓存核。

    查询（get）/ 写入（put）/ 排除判定（is_excluded）共用一份配置；
    进程内共享 global_cache() 单例字典。管道插件（input 端）组合本核
    实现缓存查询插件，output 端 writer 直接实例化本核写缓存。

    配置项：
    - enabled: 是否启用（默认 True）
    - default_ttl: 过期时间，单位秒（默认 300）
    - max_size: 最大条目数（默认 100）
    - per_pipeline_max: 每管道保留的最大条目数（默认 0 = 不限制）。
      仅在 put 显式传 pipeline_id 时生效，超限按写入序逐出该管道最旧条目
    - exclude_tools: 追加不缓存的工具名列表（与 DEFAULT_EXCLUDE_TOOLS 合并）
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", True))
        self._default_ttl = config.get("default_ttl", 300)
        self._max_size = config.get("max_size", 100)
        self._per_pipeline_max = int(config.get("per_pipeline_max", 0))
        self._exclude_tools: set[str] = set(DEFAULT_EXCLUDE_TOOLS)
        self._exclude_tools.update(config.get("exclude_tools", []))
        # per_pipeline_max 索引：pipeline_id → 按写入序的 cache_key 列表
        self._pipeline_order: dict[str, list[str]] = {}

    @property
    def enabled(self) -> bool:
        """是否启用缓存。"""
        return self._enabled

    def is_excluded(self, tool_name: str) -> bool:
        """判断工具是否在排除列表（不缓存）。

        Args:
            tool_name: 工具名

        Returns:
            True 表示该工具不查/不写缓存
        """
        return tool_name in self._exclude_tools

    def get(self, cache_key: str, now: float | None = None) -> tuple[bool, Any]:
        """查询单个缓存条目，命中时做 LRU 访问时间触碰。

        Args:
            cache_key: make_cache_key 产出的键
            now: 当前时间戳（测试注入用，缺省 time.time()）

        Returns:
            (hit, result)：命中返回 (True, 缓存结果)；未命中或已过期
            返回 (False, None)，过期条目就地删除
        """
        now = time.time() if now is None else now
        entry = _GLOBAL_CACHE.get(cache_key)
        if entry is None:
            return False, None
        result, expire_time, _last_access = entry
        if now < expire_time:
            # LRU: 命中时更新访问时间
            _GLOBAL_CACHE[cache_key] = (result, expire_time, now)
            return True, result
        del _GLOBAL_CACHE[cache_key]
        return False, None

    def put(
        self,
        tool_call: dict[str, Any],
        result: Any,
        now: float | None = None,
        pipeline_id: str | None = None,
        namespace: str | None = None,
    ) -> None:
        """将工具执行结果写入缓存。

        未启用或排除列表中的工具（有副作用）不写缓存。缓存条目数
        超过 max_size 时清理所有已过期的条目。

        Args:
            tool_call: 工具调用描述，包含 name 和 args/arguments
            result: 工具执行结果
            now: 当前时间戳（测试注入用，缺省 time.time()）
            pipeline_id: 归属管道（配 per_pipeline_max 做每管道条数上限）
            namespace: 身份隔离维度（与查询端 make_cache_key 同源，跨
                namespace 不命中）
        """
        if not self._enabled:
            return
        if self.is_excluded(tool_call.get("name", "")):
            return

        cache_key = make_cache_key(tool_call, namespace=namespace)
        now = time.time() if now is None else now
        _GLOBAL_CACHE[cache_key] = (result, now + self._default_ttl, now)
        if pipeline_id and self._per_pipeline_max > 0:
            order = self._pipeline_order.setdefault(pipeline_id, [])
            order.append(cache_key)
            while len(order) > self._per_pipeline_max:
                stale = order.pop(0)
                if not any(stale in lst for lst in self._pipeline_order.values()):
                    _GLOBAL_CACHE.pop(stale, None)

        if len(_GLOBAL_CACHE) > self._max_size:
            evict_expired(_GLOBAL_CACHE, self._max_size)
