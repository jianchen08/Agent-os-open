"""KeyPoolAdapter——多 key 轮询负载均衡提供者策略（llm_provider_keypool）。

自 llm_core/adapter.py 拆出（task_kernel_cleanup_and_split 3a）。按 API key
做并发控制（信号量 + RPM + 配额），多 key 场景下请求前选最优 key，429 冷却。
作为"提供者策略"插件，需要多 key 时挂载；无 KeyPool 的 provider 回退 Router。

依赖说明：继承 llm_core 的 `_BaseLiteLLMAdapter` 并复用其模块级流式基础设施
（`_await_with_escape` / `_ThreadedStreamBridge` / `_ACLOSE_TIMEOUT_SECONDS`），
故导入本模块要求 llm_core 目录在 sys.path 上（llm_core/server.py 已设置；
外部使用方需自行加入）。基类方向为「提供者插件 → llm_core」，不构成循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from typing import Any

import litellm
from adapter import (  # noqa: F401 - 复用 llm_core 基类与流式基础设施
    _ACLOSE_TIMEOUT_SECONDS,
    _await_with_escape,
    _BaseLiteLLMAdapter,
    _ThreadedStreamBridge,
)
from error_classifier import ErrorKind, classify_error

logger = logging.getLogger(__name__)


class KeyPoolAdapter(_BaseLiteLLMAdapter):
    """基于 KeyPool 的 LLM 调用适配器。

    按 API key 做并发控制（一个 key 一个信号量 + RPM + 配额）。

    多 key 场景下：
    - 请求前从 KeyPool 选一个最优 key（余量最多）
    - 通过该 key 的信号量控制并发
    - 成功后记录 usage，429 后冷却该 key
    - 所有 key 共享同一个 litellm.Router 的 fallback 能力

    无 KeyPool 的 provider 回退到 Router 默认行为（不限流）。
    """

    def __init__(
        self,
        router: Any,
        *,
        default_max_concurrent: int = 2,
    ) -> None:
        self._router = router
        self._default_max_concurrent = default_max_concurrent

    def _resolve_provider(self, model: str) -> str:
        """从 model_id 查找 provider 名称。

        优先用 router_factory 的映射表（model_id → provider），
        兜底用 litellm 前缀反查。
        """
        from router_factory import (  # noqa: PLC0415
            get_key_pool,
            get_provider_for_model,
        )

        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        model_id = model.split("/", 1)[1] if "/" in model else model

        # 直接查映射表
        provider = get_provider_for_model(model_id)
        if provider and get_key_pool(provider):
            return provider
        return ""

    def _extract_model_name(self, kwargs: dict[str, Any]) -> str:
        """从 kwargs 中提取 model_name（去掉 provider 前缀）。"""
        model = kwargs.get("model", "")
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    async def _do_completion(self, **kwargs: Any) -> Any:  # noqa: PLR0912,PLR0915
        from key_pool import KeySlot  # noqa: PLC0415
        from router_factory import get_key_pool  # noqa: PLC0415

        model_str = kwargs.get("model", "")
        provider_name = self._resolve_provider(model_str)
        pool = get_key_pool(provider_name) if provider_name else None

        if pool is None:
            # 无 KeyPool，直接走 Router
            return await self._route_call(**kwargs)

        # 尝试每个可用 key，失败后自动换下一个重试
        max_retries = len(pool.slots)
        last_exc: Exception | None = None

        from exceptions import KeyPoolExhaustedError  # noqa: PLC0415

        try:
            for attempt in range(max_retries):
                slot: KeySlot = await pool.acquire_slot()
                logger.info(
                    "[KeyPoolAdapter] provider=%s 选用 key=%s attempt=%d/%d",
                    provider_name,
                    slot.key_id,
                    attempt + 1,
                    max_retries,
                )
                # 信号量释放：流式路径的真正传输在调用方消费 stream wrapper 期间，
                # 故 release 推迟到 stream.aclose；非流式 finally 立即 release。
                # 用 _defer_release 标志区分两条路径。
                _defer_release = False
                try:
                    key_kwargs = dict(kwargs)
                    key_kwargs["api_key"] = slot.api_key
                    if slot.api_base:
                        key_kwargs.setdefault("api_base", slot.api_base)

                    result = await self._direct_call_with_slot(slot=slot, **key_kwargs)

                    slot.on_success()
                    # 流式返回值是 async iterator（CustomStreamWrapper），其流式
                    # 传输尚未发生——把 release 绑定到 aclose，由消费方在流结束后触发。
                    if hasattr(result, "__aiter__"):
                        _defer_release = True
                        self._bind_release_to_stream(result, slot)
                    return result
                except asyncio.CancelledError:
                    # 用户取消：不冷却，直接抛
                    raise
                except Exception as exc:
                    # 统一异常处理：先翻译成 ErrorInfo，再按 kind 决策
                    info = classify_error(exc)

                    # BAD_REQUEST 是不可恢复的参数错误，直接抛（不换 key）
                    if info.kind == ErrorKind.BAD_REQUEST:
                        logger.warning(
                            "[KeyPoolAdapter] BAD_REQUEST 不可恢复 → key=%s: %s",
                            slot.key_id,
                            str(exc)[:200],
                        )
                        raise

                    # SERVICE_DOWN：上游临时挂，退避后重试。
                    # handle_error 会从第 2 次起给 key 置短冷却，所以 finally 的
                    # release + 下一轮 acquire_slot 中，select() 会暂时绕开这个
                    # key（单 key 场景则等到冷却到期再重试），避免无限选回坏 key。
                    if info.kind == ErrorKind.SERVICE_DOWN:
                        backoff = min(2.0 * (2**slot._consecutive_down), 16.0)
                        logger.warning(
                            "[KeyPoolAdapter] SERVICE_DOWN → key=%s 退避 %.1fs 重试 (attempt %d/%d): %s",
                            slot.key_id,
                            backoff,
                            attempt + 1,
                            max_retries,
                            str(exc)[:150],
                        )
                        slot.handle_error(info)
                        await asyncio.sleep(backoff)
                        last_exc = exc
                    else:
                        # 其他可恢复错误：交给 KeySlot 统一策略处理（冷却/降级/不冷却）
                        slot.handle_error(info)
                        logger.info(
                            "[KeyPoolAdapter] %s → key=%s 处理 (attempt %d/%d)",
                            info.kind.value,
                            slot.key_id,
                            attempt + 1,
                            max_retries,
                        )
                        last_exc = exc
                finally:
                    # 流式成功路径已把 release 延迟到 stream.aclose，这里跳过；
                    # 其余路径（异常/非流式成功）立即释放，保证换 key 重试时槽位归还。
                    if not _defer_release:
                        slot.release()
        except KeyPoolExhaustedError as exc:
            # 所有 key 不可用且等待超时：不可恢复的资源耗尽，
            # 转成业务可读的 RateLimitError，保留原始异常链（backend_rules §3.1）。
            logger.error(
                "[KeyPoolAdapter] key 池耗尽 provider=%s model=%s: %s",
                provider_name,
                model_str,
                exc,
            )
            last_exc = litellm.RateLimitError(
                message=f"所有 API key 不可用且等待超时（{exc.timeout:.0f}s）；不可用 key 诊断: {exc.unavailable}",
                model=model_str,
                llm_provider=provider_name or "unknown",
            )
            last_exc.__cause__ = exc

        # 所有 key 都试过了或 pool 已耗尽 → 尝试 Router fallback
        # 走 router.acompletion() 利用 llm.yaml 的 fallback_chain 配置
        # 切换到备用模型（如 deepseek-v4-pro → minimax-m3）
        logger.warning(
            "[KeyPoolAdapter] 所有 key 均失败 provider=%s model=%s，尝试 Router fallback...",
            provider_name,
            model_str,
        )
        try:
            return await self._route_call(**kwargs)
        except Exception as fb_exc:
            logger.error(
                "[KeyPoolAdapter] Router fallback 也失败: %s",
                fb_exc,
            )
            if last_exc is not None:
                raise last_exc  # noqa: B904
            raise fb_exc

    async def _route_call(self, **kwargs: Any) -> Any:
        """无 KeyPool 时的回退路径，动态获取最新 Router。

        不缓存 self._router，而是每次调用时通过 get_or_create_router() 动态获取最新
        Router，若模块级单例已被重置（前端修改模型配置后 invalidate_all_llm_caches()
        会清除）则自动从 YAML 重建，确保模型配置变更对 KeyPoolAdapter 立即生效。
        """
        from _config_models import get_model_config_loader  # noqa: PLC0415
        from router_factory import get_or_create_router  # noqa: PLC0415

        model_loader = get_model_config_loader()
        router = get_or_create_router(model_loader)
        return await router.acompletion(**kwargs)

    # aclose 超时上限（引用模块级常量，便于统一调整）。
    _ACLOSE_TIMEOUT_SECONDS: float = _ACLOSE_TIMEOUT_SECONDS

    @staticmethod
    def _bind_release_to_stream(stream: Any, slot: Any) -> None:
        """把 slot.release() 绑定到 stream.aclose()，流关闭时释放并发许可。

        流式调用返回的 stream wrapper（litellm CustomStreamWrapper）是惰性对象，
        真正的流式传输发生在调用方消费它期间。信号量许可必须覆盖整段传输，
        故 release 推迟到 stream 被关闭（_call_streaming 的 finally 调用 aclose）。

        用一次性标志保证 release 只执行一次（litellm 可能多次调用 aclose）。

        original_aclose 用超时包裹：Windows 上半死 SSL socket 会让
        httpx/httpcore 的 aclose 永久阻塞，导致 _call_streaming 的 finally 不
        返回 → _run_loop 永久卡死（其他管道因 slot.release 已执行而不受影响，
        但本管道僵死需重启才能恢复）。超时后放弃关闭，协程得以返回。
        ★ 用 _await_with_escape：即使 aclose 吞掉取消挂死，也能到点返回，
        异常不掩盖 finally 主路径。
        """
        original_aclose = getattr(stream, "aclose", None)
        released = False

        async def _aclose_with_release() -> None:
            nonlocal released
            if not released:
                released = True
                slot.release()
            if original_aclose is not None:
                try:
                    await _await_with_escape(
                        original_aclose(),
                        timeout=_ACLOSE_TIMEOUT_SECONDS,
                        what="stream.aclose",
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] stream.aclose 超时 %.0fs（半死 socket 放弃优雅关闭），"
                        "残留连接交由 GC 回收",
                        KeyPoolAdapter.__name__,
                        _ACLOSE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    # aclose 自身异常（非超时）不阻断 finally 返回
                    logger.debug("[%s] stream.aclose 异常（已忽略）", KeyPoolAdapter.__name__)

        stream.aclose = _aclose_with_release  # type: ignore[method-assign]

    async def _direct_call_with_slot(self, slot: Any, **kwargs: Any) -> Any:
        """用指定 slot 的 key 直接调用 litellm.acompletion。

        不经过 Router，直接构建 litellm 参数，确保使用 slot 的 key。
        关键：kwargs["model"] 此时是 model_id（yaml key），不是 model_name。
        需要反查 model_name 来拼 litellm 模型字符串（如 apigo/MiniMax-M3 → openai/MiniMax-M3），
        因为上游 API 只认 model_name，不认内部 model_id。
        """
        from router_factory import (  # noqa: PLC0415
            get_litellm_prefix,
            get_model_name_for_id,
            get_provider_for_model,
        )

        model_id = kwargs.get("model", "")
        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        bare = model_id.split("/", 1)[1] if "/" in model_id else model_id

        # 查 provider → 构建 litellm 模型字符串
        provider = get_provider_for_model(bare)
        prefix = get_litellm_prefix(provider) if provider else ""
        # 反查 model_name（yaml 的 model_name 字段），而非直接用 model_id
        # 例: bare="deepseek-v4-pro-apigo" → model_name="deepseek-v4-pro"
        model_name = get_model_name_for_id(bare)
        litellm_model = f"{prefix}/{model_name}" if prefix else model_name

        # 构建 kwargs：用 slot 的凭证，去掉 model 让 litellm_params 里的生效
        input_kwargs = {k: v for k, v in kwargs.items() if k not in ("model",)}
        input_kwargs["model"] = litellm_model
        input_kwargs["api_key"] = slot.api_key
        if slot.api_base:
            input_kwargs["api_base"] = slot.api_base

        # 禁用 litellm 内部重试：由 KeyPoolAdapter 自己用不同 key 重试
        input_kwargs["num_retries"] = 0

        # ★ 阶段日志：定位卡死在 litellm.acompletion 内部哪一步
        # _direct_call_with_slot 是「拿到 slot 后 → 调 litellm.acompletion」的唯一出口，
        # litellm.acompletion 返回 stream wrapper 后才真正建连/发请求。
        # 若卡死在 litellm 内部（建连/发请求/等首字节），下面「进入/返回」两条日志
        # 能精确定位耗时区间，配合 _open_and_first_chunk 的首chunk日志锁定卡死层。
        _dc_t0 = _time.monotonic()
        logger.info(
            "[%s] _direct_call_with_slot: 进入 litellm.acompletion model=%s api_base=%s kw_keys=%s t0=%.3f",
            type(self).__name__, litellm_model, input_kwargs.get("api_base", "?"),
            sorted(input_kwargs.keys()), _dc_t0,
        )
        # ★ 把首 token 超时"包括在" litellm.acompletion 调用本身（HTTP 层）。
        # litellm 内部存在事件循环线程内同步阻塞路径（如 get_llm_provider 的
        # 同步网络调用），冻结主事件循环时 asyncio 层超时（wait_for/
        # asyncio.wait/shield）全部失效。httpx 层 timeout 在线程池线程内由
        # socket 定时生效，不依赖事件循环调度——传 180s 后卡死的 HTTP
        # 请求必然到点抛异常透传。"将首 token 超时包括在调用本身"。
        _acompletion_timeout = float(kwargs.pop("first_chunk_timeout", 0)) or 180.0
        # 调用方已显式传 HTTP timeout（流式 _call_streaming 会把 first_chunk_timeout
        # pop 掉并转成 timeout 传入）→ 以调用方为准，避免默认 180 覆盖自定义值。
        if kwargs.get("timeout"):
            try:
                _acompletion_timeout = float(kwargs["timeout"])
            except (TypeError, ValueError):
                pass  # httpx.Timeout 对象等非数值：保持 first_chunk_timeout
        input_kwargs["timeout"] = _acompletion_timeout

        # ★ litellm.acompletion 在独立线程 + 独立事件循环中运行，流式迭代也
        # 在该线程完成，chunk 经线程安全队列送回主循环：
        # - litellm 内部同步阻塞（冻结自己的线程）不再影响主事件循环 → 其他管道
        #   永不陪葬（生产 17:05:34 全进程 0 日志 36 分钟的根因）
        # - CustomStreamWrapper 绑定 worker loop，主循环直接 await 会报
        #   "attached to a different loop"（生产 20:33:59 MidStreamFallbackError）
        #   → 主循环只从 queue.Queue 取 chunk，彻底避免跨 loop
        # - 主协程轮询 threading.Event（OS 层事件，到点必然置位/超时，不依赖
        #   任何事件循环调度），超时抛 TimeoutError 透传
        import queue  # noqa: PLC0415
        import threading  # noqa: PLC0415

        _done_evt = threading.Event()
        _result_box: list[Any] = []
        _exc_box: list[BaseException] = []
        _chunk_queue: queue.Queue[Any] = queue.Queue()
        _close_evt = threading.Event()
        _stream_obj: list[Any] = []

        async def _run_litellm() -> Any:
            return await litellm.acompletion(**input_kwargs)

        async def _worker_main() -> Any:
            """worker 线程主体：跑 litellm 并把流式 chunk 塞进队列。"""
            resp = await _run_litellm()
            # 非流式：直接返回结果对象
            if not hasattr(resp, "__aiter__"):
                return resp
            # 流式：在 worker 自己的 loop 里迭代，chunk 进线程安全队列
            _stream_obj.append(resp)
            try:
                async for chunk in resp:
                    _chunk_queue.put(chunk)
                    if _close_evt.is_set():
                        break
            finally:
                # 主循环可能已超时放弃（残留迭代），此处尽量关闭底层流
                aclose = getattr(resp, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
            return None

        def _worker() -> None:
            # ★ 不用 asyncio.run：它会在结束时 close() 线程事件循环，而
            # CustomStreamWrapper 的 logging 回调 / fallback 重试的 async client
            # 绑定该 loop → 消费时报 "Event loop is closed"（生产 20:08:02）。
            # 用 new_event_loop + run_until_complete，loop 保持存活
            # （daemon 线程持有，进程退出时由 OS 回收），流对象可被主循环消费。
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                _result_box.append(loop.run_until_complete(_worker_main()))
            except BaseException as exc:  # noqa: BLE001 - 异常装箱透传
                _exc_box.append(exc)
            finally:
                _done_evt.set()
                # 不 close loop：流 wrapper 绑定的 logging/fallback client 需要它存活。

        _worker_thread = threading.Thread(
            target=_worker,
            name=f"litellm-acompletion-{litellm_model[:24]}",
            daemon=True,
        )
        _worker_thread.start()

        # 主协程等待 worker 首次返回（建连 + 非流式结果 / 流式首个 chunk 入队前）
        _deadline = _time.monotonic() + _acompletion_timeout
        while not _done_evt.is_set() and _chunk_queue.empty():
            if _time.monotonic() >= _deadline:
                _close_evt.set()
                raise asyncio.TimeoutError(
                    f"litellm.acompletion 超时 {_acompletion_timeout:.0f}s"
                    f"（HTTP 层 timeout 已设 {_acompletion_timeout:.0f}s；残留线程由 daemon 回收）"
                    f"model={litellm_model}"
                )
            if _exc_box:
                raise _exc_box[0]
            await asyncio.sleep(0.1)

        if _exc_box:
            raise _exc_box[0]
        _result = _result_box[0] if _result_box else None
        _dc_t1 = _time.monotonic()
        logger.info(
            "[%s] _direct_call_with_slot: litellm.acompletion 返回(%.3fs) model=%s type=%s",
            type(self).__name__, _dc_t1 - _dc_t0, litellm_model, type(_result).__name__,
        )
        if _result is not None:
            return _result

        # 流式：返回桥接 iterator，主循环从线程安全队列消费 chunk
        return _ThreadedStreamBridge(
            queue=_chunk_queue,
            done_evt=_done_evt,
            exc_box=_exc_box,
            close_evt=_close_evt,
            completion_stream=_stream_obj[0] if _stream_obj else None,
        )
