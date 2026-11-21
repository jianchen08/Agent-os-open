#!/usr/bin/env python3
"""Human Interaction 插件——双角色（LLM 工具 + 交互服务）。

合并自原 tools/human（工具）和 system/human_interaction（服务）两个 sidecar。
现在单进程自包含，service 状态唯一：

  角色 1：LLM 工具（human_interaction）
    LLM 调 choice/conversation/notification → 进程内直接调 service → event-bus.notify → 前端

  角色 2：交互服务（interaction.* 工具 + provides: human-interaction）
    其他插件（approval/security_check 等）或前端响应回路经 capability 调：
      其他插件 → cap.call("human-interaction","create_choice") → 内核 McpBridge
      → invoke_tool(本插件, "interaction.create_choice") → 进程内 service

链路（notification，3 跳）：
    LLM 调 human_interaction 工具
      → handler 进程内调 service.send_notification
      → service 经 _EventBusNotifier.notify → event-bus.emit → 内核 → 前端
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

# 包路径导入（human.models / human.service 等）需要 tools 目录在 sys.path——
# 组根不随 bootstrap 注入（ADR 2026-09-08-plugin-bootstrap-sink 决策 1）
if _paths.group_root not in sys.path:
    sys.path.insert(0, _paths.group_root)

from human.interfaces import IInteractionNotifier  # noqa: E402
from human.models import InteractionMode, Priority  # noqa: E402
from human.service import (  # noqa: E402
    HumanInteractionService,
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.approval_contract import InteractionErrorCode, is_valid_semantics  # noqa: E402

# run 停止看门狗（与 bash 工具同一根因、同一插件侧解法）：交互等待默认
# 86400s，run 卡在等待里时注入队列轮边界消费点（pipeline_loop.rs:814 →
# :1101）永远走不到，消息无限期滞留。解析失败一律降级为「不启用」——
# 看门狗是增强能力，缺它不改变交互本体行为。
try:  # 生产：bootstrap 已把插件目录加入 sys.path（与 bash 同款解析）
    from run_stop_watch import race_with_stop as _race_with_stop  # noqa: E402
    from run_stop_watch import start_watcher as _start_run_stop_watch  # noqa: E402
except ImportError:  # 测试装载/包布局：插件目录不在 sys.path，按包名解析
    try:
        from human.run_stop_watch import race_with_stop as _race_with_stop  # noqa: E402
        from human.run_stop_watch import start_watcher as _start_run_stop_watch  # noqa: E402
    except ImportError:  # pragma: no cover —— 模块缺失即关闭（缺一环不影响工具本体）
        _race_with_stop = None  # type: ignore[assignment]
        _start_run_stop_watch = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("human_interaction_tool")

# 进程内 service 单例（on_load 时初始化，注入 _EventBusNotifier）
_service: HumanInteractionService | None = None


class _EventBusNotifier(IInteractionNotifier):
    """把交互事件经 event-bus capability 推到前端（fire-and-forget）。

    一个 _emit 方法统一调 plugin.get_capability("event-bus").notify("emit", ...)，
    5 个 notify_xxx 只是构造不同 event 名/payload。用 notify 而非 call：避免
    service 在执行工具期间发起的 emit 与内核的 tools/call 响应形成嵌套死锁。
    """

    def __init__(self, plugin_ref: Any) -> None:
        self._plugin = plugin_ref

    def _bus(self) -> Any | None:
        try:
            return self._plugin.get_capability("event-bus")
        except (KeyError, AttributeError):
            return None

    async def _emit(self, event: str, payload: dict[str, Any], thread_id: str = "") -> bool:
        bus = self._bus()
        if bus is None:
            logger.warning("[HumanInteraction] event-bus not injected; skip %s", event)
            return False
        # thread_id 并入 payload（内核交互族路由只读 payload.thread_id——
        # capability_router.try_route_interaction_family，params 顶层不被读取，
        # 缺失即丢弃 + 诊断；params 顶层键保留作冗余）。无路由坐标（双方皆空）
        # 不加空键：内核 fail-closed 丢弃语义保持（宁丢不误投）。
        merged = dict(payload)
        route_id = thread_id or str(merged.get("thread_id") or "")
        if route_id:
            merged["thread_id"] = route_id
        try:
            await bus.notify("emit", {"event": event, "payload": merged, "thread_id": route_id})
            return True
        except Exception:
            logger.exception("[HumanInteraction] emit %s failed", event)
            return False

    async def notify_request(self, request: Any) -> bool:
        record = request if isinstance(request, dict) else {}
        msg = record.get("message_data", {}) if isinstance(record.get("message_data"), dict) else {}
        payload: dict[str, Any] = {
            "request_id": record.get("id", ""),
            "session_id": record.get("session_id", ""),
            "status": record.get("status", ""),
            "thread_id": msg.get("thread_id", ""),
            "tab_id": msg.get("tab_id", ""),
            "interaction_mode": msg.get("interaction_mode", ""),
            "title": msg.get("title", ""),
            "description": msg.get("description", ""),
            # 倒计时起点（BUG-14 有界等待可见化；记录创建时刻，前端换算剩余时间）
            "created_at": str(record.get("created_at") or ""),
        }
        for key in ("options", "initial_message", "suggestions",
                    "file_paths", "progress", "priority", "timeout_seconds",
                    "agent_level", "pipeline_id", "agent_id", "agent_name"):
            if msg.get(key) is not None:
                payload[key] = msg[key]
        return await self._emit("interaction_request", payload, payload.get("thread_id", ""))

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        return await self._emit("interaction_cancelled",
                                {"request_id": request_id, "reason": reason}, thread_id)

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        return await self._emit("interaction_timeout", {"request_id": request_id}, thread_id)

    async def notify_timeout_reminder(
        self, request_id: str, remaining_seconds: int, thread_id: str = "", *,
        title: str = "", mode: str = "",
        options: list[dict] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "request_id": request_id,
            "remaining_seconds": remaining_seconds,
            "title": title,
            "mode": mode,
        }
        if options is not None:
            payload["options"] = options
        return await self._emit("interaction_timeout_reminder", payload, thread_id)

    async def notify_conversation_start(
        self, thread_id: str, tab_id: str, title: str, request_id: str = "",
        initial_message: str | None = None, suggestions: list[str] | None = None,
    ) -> bool:
        return await self._emit("interaction_conversation_start", {
            "request_id": request_id, "thread_id": thread_id, "tab_id": tab_id,
            "title": title, "initial_message": initial_message, "suggestions": suggestions,
        }, thread_id)

    async def notify_settled(
        self, request_id: str, *,
        response_type: str, selected_option: str | None, selected_semantics: str,
        session_id: str = "", thread_id: str = "",
    ) -> bool:
        """结算广播 approval.taken（多端对账：其他前端连接据此下卡）。"""
        return await self._emit("approval.taken", {
            "request_id": request_id,
            "response_type": response_type,
            "selected_option": selected_option,
            "selected_semantics": selected_semantics,
            "session_id": session_id,
        }, thread_id)

    async def notify_anomaly(
        self, request_id: str, *,
        anomaly: str, detail: str, thread_id: str = "",
    ) -> bool:
        """交互应答异常告警（归一命中率/未知词形 24h 观测采集面）。"""
        return await self._emit("interaction_anomaly", {
            "request_id": request_id,
            "anomaly": anomaly,
            "detail": detail,
        }, thread_id)


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化 service 单例，注入 EventBusNotifier。"""
    global _service
    _service = HumanInteractionService()
    _service.set_notifier(_EventBusNotifier(plugin))

    # 审批非阻塞 M1：装配 ticket 持久写面——经 pipeline-state.update 单一路径
    # 落 approval.tickets.*（审计铁律：所有 state 变更经 update 留痕）。
    # 能力缺席（测试装载/内核未注入）跳过装配：writer=None 时 service 侧
    # 本就 best-effort 不写，插件本体行为不变。
    try:
        state_cap = plugin.get_capability("pipeline-state")

        async def _write_ticket_state(pipeline_id: str, fields: dict[str, Any]) -> None:
            await state_cap.call("update", {"pipeline_id": pipeline_id, "fields": fields})

        _service.set_state_writer(_write_ticket_state)
    except KeyError:
        logger.info("[HumanInteraction] pipeline-state 未注入，ticket 持久化停用（writer=None）")
    logger.info("Human interaction service initialized (dual-role: tool + service)")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    global _service
    _service = None
    logger.info("Human interaction service unloaded")


# ════════════════════════════════════════════════════════════════════
# 角色 1：LLM 工具（human_interaction）
# LLM 调此工具发起交互。进程内直接调 service，零中转。
# ════════════════════════════════════════════════════════════════════


def _normalize_options(raw: Any) -> list[dict[str, Any]] | None:
    """LLM 传参容错：把 options 归一化为前端 InteractionOption 的 [{id,label}]。

    schema 只声明 ``array``，LLM 常按直觉传字符串数组（``["批准","拒绝"]``）；
    前端 InteractionCard 渲染 ``opt.label``，字符串元素会导致按钮文字为空，
    表现为"审批卡片只有输入框、没有选项按钮"。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"id": str(i), "label": item})
        elif isinstance(item, dict):
            label = item.get("label") or item.get("text") or item.get("name")
            if not label:
                continue
            entry: dict[str, Any] = {"id": str(item.get("id", i)), "label": label}
            if item.get("description"):
                entry["description"] = item["description"]
            # semantics 建卡契约（ADR 2026-10-01 决策 2，只增不改）：声明值须为
            # 封闭枚举成员，非法值丢弃不带病入卡（该选项退化为无语义透传）。
            if is_valid_semantics(item.get("semantics")):
                entry["semantics"] = item["semantics"]
            out.append(entry)
    return out or None


def _resolved_file_paths(kwargs: dict[str, Any]) -> list[str] | None:
    """file_paths 归一化为宿主侧绝对路径（交互面板按绝对路径直读宿主文件系统）。

    param_inject 注入 workspace/project_root（宿主绝对路径）：容器挂载路径
    ``/workspace/*`` 重映射到宿主工作空间、相对路径锚定根——与 fs_tools 的
    ``_check_workspace_path`` 同款语义。未注入（L1 主会话）时原样透传，由前端
    ``_local`` 通道按项目根解析。非字符串/空列表归一为 None（缺省语义）。
    """
    raw = kwargs.get("file_paths")
    if not isinstance(raw, list) or not raw:
        return None
    root = kwargs.get("project_root") or kwargs.get("workspace")
    out: list[str] = []
    for p in raw:
        if not isinstance(p, str) or not p:
            continue
        if root and (p == "/workspace" or p.startswith("/workspace/")):
            out.append(str(Path(root) / p[len("/workspace/"):]))
        elif root and not Path(p).is_absolute():
            out.append(str(Path(root) / p))
        else:
            out.append(p)
    return out or None

@plugin.tool(
    name="human_interaction",
    schema={
            'type': 'object',
            'properties': {
                'title': {
                    'type': 'string',
                    'description': '交互标题',
                },
                'description': {
                    'type': 'string',
                    'description': '详细说明',
                },
                'options': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'id': {
                                'type': 'string',
                            },
                            'label': {
                                'type': 'string',
                            },
                        },
                    },
                    'description': '选项列表。必须是 JSON 数组，例如 ["批准","拒绝"] 或 [{"id":"1","label":"批准"},{"id":"2","label":"拒绝"}]。禁止包一层对象（如 {"item":[...]})。有 options=阻塞交互卡（点选/自由回复/进入对话）；无 options=非阻塞通知',
                },
                'timeout_seconds': {
                    'type': 'number',
                    'default': 86400,
                    'description': '超时时间（秒）',
                },
                'mode': {
                    'type': 'string',
                    'enum': ['async'],
                    'description': '仅在带 options 的审批场景使用：mode="async" 提交后立即返回（status=submitted + ticket_id），不阻塞等待用户——用户稍后在审批页决定，决定内容下一轮经审批注入面送达。缺省（不带 mode）保持阻塞等待用户作答',
                },
                'priority': {
                    'type': 'string',
                    'enum': [
                        'low',
                        'normal',
                        'high',
                        'critical',
                    ],
                    'default': 'normal',
                    'description': '优先级',
                },
                'progress': {
                    'type': 'number',
                    'description': '进度百分比 0-100（通知）',
                },
                'file_paths': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                    'description': '需要展示给用户的文件路径列表。系统会自动读取文件内容并在交互面板中展示。以下两种情况都必须使用此参数：（1）主动展示——当你需要将文件内容、设计方案、代码变更等信息呈现给用户查看或审批时（如通知推送文件、选择卡展示文件变更）；（2）用户请求——当用户明确要求查看某个文件、某个结果，或要求省略/跳过某些内容并需要确认时。带 options 时展示于选择卡，不带 options 时必须改用带 options 的调用（通知不支持文件展示）。支持相对路径（基于工作空间）和绝对路径，单文件不超过10MB，最多10个文件。工作空间范围限制仅对子任务（L2+）生效；主 agent（L1）可展示项目内任意路径。',
                },
            },
            'required': [
                'title',
            ],
        },
    description="与用户交互（双态，无需选模式）：带 options=阻塞等待用户作答（点选选项、输入框自由回复，或点「进入对话」转到对话页继续聊——此时返回 user_arrived，等待用户下一条消息），把要问的问题写进 title/description、候选答案写进 options；不带 options=非阻塞通知，一次性送达（返回 status: sent 即已送达）；决策类审批可再带 mode=\"async\"：提交即返回不阻塞，用户稍后在审批页决定。一个回合至多调用一次，不要重复调用确认、不要换措辞重发",
)
async def human_interaction(**kwargs: Any) -> dict[str, Any]:
    """LLM 工具入口——进程内直接调 service，零跨进程中转。"""
    # 冷启动竞态自愈：内核 spawn 完成后 on_load 是 fire-and-forget 通知
    # （invoker.rs get_or_create_mcp_client），紧随其后的首次 tools/call 可能
    # 抢在 _on_load（初始化 _service）之前到达——立即返回 error 会让 LLM 误判
    # "工具不可用"并放弃。on_load 完成后 _service 全局可见，短暂等待即可跨过窗口。
    if _service is None:
        for _ in range(50):  # 最多 ~10s，覆盖 Python import 慢的冷启动
            await asyncio.sleep(0.2)
            if _service is not None:
                break
    if _service is None:
        return {"success": False, "error": "service not initialized (on_load not finished in 10s)"}

    # 兼容 LLM 偶发的参数别名（message→title），避免参数名错配。
    # type 等历史参数已废（双态推断），传入即忽略不再误分派；mode 仅认
    # "async"（审批非阻塞 M1），其余历史值照旧忽略。
    if not kwargs.get("title") and kwargs.get("message"):
        kwargs["title"] = kwargs["message"]

    # 双态推断（2026-10-19 用户裁定：选择=对话，没有第二种交互态）：有 options
    # →conversation（统一交互卡：选项点选/输入框自由回复/进入对话三通道），
    # 都无→notification（非阻塞安全缺省——缺省不得使通知退化为阻塞交互）。
    mode = "conversation" if kwargs.get("options") else "notification"

    pipeline_id = kwargs.get("pipeline_id") or kwargs.get("session_id") or ""
    session_id = kwargs.get("session_id") or pipeline_id
    timeout = kwargs.get("timeout_seconds", 86400)

    try:
        if mode == InteractionMode.NOTIFICATION.value:
            return await _do_notification(kwargs, session_id)
        # 审批非阻塞 M1（设计 §三 ①/G1）：mode=async 且有 options → 提交即返回，
        # 决定经审批页写回 state、下一轮 approval.fold 注入（无 options 的 async
        # 无票可提交，按历史忽略口径走 notification）。
        if kwargs.get("mode") == "async" and kwargs.get("options"):
            return await _do_conversation(kwargs, session_id, pipeline_id, timeout, blocking=False)
        return await _do_conversation(kwargs, session_id, pipeline_id, timeout)
    except InteractionTimeoutError as e:
        return {
            "success": False,
            "error": f"人类交互超时（{e.timeout}秒）",
            "error_code": "INTERACTION_TIMEOUT",
        }
    except InteractionCancelledError as e:
        return {
            "success": False,
            "error": f"交互已取消: {e.reason or '用户取消'}",
            "error_code": "INTERACTION_CANCELLED",
        }
    except InteractionDeniedError as e:
        return {"status": "denied", "selected_option": "用户拒绝", "reason": e.reason or "用户拒绝"}
    except _RunStoppedDuringWait as e:
        # 停止语义的如实结果（不是交互自身出错）：run 被停止/被强制注入打断，
        # 交互已取消，工具立即返回让 run 回边界收尾，注入消息随派发链 drain 起跑。
        return {
            "success": False,
            "error": (
                "run 已停止（停止/强制注入），本次交互已取消并让出执行；"
                "注入的消息将由新一轮 run 接手"
            ),
            "error_code": "RUN_STOPPED",
            "run_stopped": True,
            "request_id": e.request_id,
        }
    except Exception as exc:
        logger.exception("[human_interaction] 执行失败")
        return {"success": False, "error": f"人类交互执行失败: {exc}"}


async def _do_notification(kwargs: dict[str, Any], session_id: str) -> dict[str, Any]:
    """通知——非阻塞，创建 record + event-bus.notify 后立即返回。"""
    assert _service is not None, "human_interaction 入口已保证 _service 初始化"
    rid = await _service.send_notification(
        session_id=session_id,
        thread_id=session_id,
        title=kwargs.get("title", ""),
        message=kwargs.get("description") or "",
        priority=Priority(kwargs.get("priority", "normal")),
        agent_id=kwargs.get("pipeline_id"),
        pipeline_id=kwargs.get("pipeline_id"),
        agent_level=_agent_level_from_kwargs(kwargs),
    )
    # status: sent 附终止语义：实测模型不把裸 sent 当"已送达"，同一回合换措辞
    # 反复重发（装机 2026-10-16 一分钟提醒事故）；明示送达完成 + 出路逼收尾。
    return {
        "status": "sent",
        "request_id": rid,
        "message": "通知已送达用户（通知中心+系统通知）。本回合不要再调用本工具，直接输出收尾文本即可",
    }


def _agent_level_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    """从注入参数归一 Agent 层级（卡片来源展示用）。

    param_inject 注入 ``parent_agent_level``（int 形态，task_submit 等既有消费
    契约）——归一为 "L2" 字符串；调用方已带字符串形态（L1/l1/2）同样归一。
    缺失/非法 → None（payload 不带该键，前端不显示级别）。
    """
    raw = kwargs.get("agent_level") or kwargs.get("parent_agent_level")
    if raw is None or raw == "":
        return None
    level_str = str(raw).upper().lstrip("L")
    try:
        return f"L{int(level_str)}"
    except ValueError:
        return None


class _RunStoppedDuringWait(Exception):
    """等待期检出 run 停止信号（停止 / 强制发送 / 注入的强制通道）。

    携带被取消的交互 request_id：入口据此回传 RUN_STOPPED 显式结果——
    这是停止语义的如实结果，不是交互本身出错。
    """

    def __init__(self, request_id: str, pipeline_id: str) -> None:
        super().__init__(f"run 已停止，交互已取消: {request_id}")
        self.request_id = request_id
        self.pipeline_id = pipeline_id


async def _await_choice_or_run_stop(
    request_id: str, timeout: float, pipeline_id: str
) -> dict[str, Any]:
    """等待用户作答，并与 run 停止看门狗赛跑（注入送达的插件侧前提）。

    脱困链：注入消息（chat.send_message force 通道）→ 内核暂停当前 run
    （`run_status=suspended`）→ 本看门狗在轮询窗内检出 → 取消交互请求
    （前端卡同步消失）→ 工具快速返回 → run 回 step 边界按既有停止语义收尾
    → 派发链 drain 队列里的注入消息起跑新一轮。

    看门狗缺席（无 pipeline_id / 能力未注入 / `AGENTOS_RUN_ABORT_POLL_SECS<=0`）
    → 直通旧行为（单纯等待），不改变交互语义。
    """
    assert _service is not None, "_await_choice_or_run_stop 调用方已保证 service 初始化"
    if _start_run_stop_watch is None or _race_with_stop is None:  # pragma: no cover
        return await _service.wait_for_choice(request_id, timeout=timeout)

    watcher = None
    try:
        watcher = _start_run_stop_watch(plugin, pipeline_id)
        stopped, response = await _race_with_stop(
            _service.wait_for_choice(request_id, timeout=timeout), watcher
        )
    finally:
        if watcher is not None:
            watcher.stop()

    if stopped:
        # 交互卡收口：卡片不消失会让用户对着一个已被抛弃的请求作答。
        try:
            await _service.cancel_request(request_id, reason="run_stopped")
        except Exception:  # noqa: BLE001 —— 收口失败不得掩盖停止结果
            logger.warning(
                "[human_interaction] 停止收口时取消交互请求失败（卡片可能残留）| rid=%s",
                request_id,
                exc_info=True,
            )
        raise _RunStoppedDuringWait(request_id, pipeline_id)

    return response


async def _do_conversation(
    kwargs: dict[str, Any], session_id: str, pipeline_id: str, timeout: int,
    blocking: bool = True,
) -> dict[str, Any]:
    """统一交互卡——创建请求 + 阻塞等待用户作答（点选/自由回复/进入对话）。

    2026-10-19 用户裁定「选择=对话，没有两种交互态」：对话模式即超集
    （options + 文本 + 进入对话），LLM 工具的选择分支删除，收敛到本函数。

    审批非阻塞 M1（设计 §三 ①）：``blocking=False``（mode=async）= 创建后
    立即返回 ``{status: submitted, ticket_id}``，不等待用户——决定经审批页
    写回 state，下一轮 approval.fold 注入（G1/G3）；ticket/category/blocking
    语义与 choice 路径同契约。
    """
    assert _service is not None, "human_interaction 入口已保证 _service 初始化"
    rid = await _service.create_conversation_request(
        session_id=session_id,
        thread_id=session_id,
        tab_id=pipeline_id,
        title=kwargs.get("title", ""),
        description=kwargs.get("description", ""),
        initial_message=kwargs.get("initial_message"),
        suggestions=kwargs.get("suggestions"),
        options=_normalize_options(kwargs.get("options")),
        timeout_seconds=timeout,
        priority=Priority(kwargs.get("priority", "normal")),
        agent_id=pipeline_id,
        pipeline_id=pipeline_id,
        file_paths=_resolved_file_paths(kwargs),
        user_id=kwargs.get("user_id") or None,
        agent_level=_agent_level_from_kwargs(kwargs),
        category=kwargs.get("category"),
        blocking=blocking,
    )
    if not blocking:
        return {"status": "submitted", "ticket_id": rid, "request_id": rid}
    response = await _await_choice_or_run_stop(rid, timeout, pipeline_id)
    resp_type = response.get("response_type", "")
    # 进入对话跳转，或在管道里直接发消息解除挂起（approved 且未选任何选项）
    # → user_arrived：agent 结束当前轮，等待用户下一条消息推进；选选项/自由
    # 回复走 completed（选项点选恒为 answered；approved+selected_option 属
    # 协议外畸形，按 completed 原样透出，不做语义猜测）。
    # conversation_mode=True（2026-11-06 用户裁定恢复，10-19 双态化删除时丢失）：
    # conversation_mode 输出插件据它写 suspended=true——工具迭代 post 后当轮
    # 直接挂起，零空转 LLM 轮（与 task_evaluate 投影键收束同机制）；键属
    # per-run 挥发，用户下一条消息的新 run 从非对话态开始。
    if resp_type == "approved" and not response.get("selected_option"):
        return {
            "status": "user_arrived",
            "conversation_mode": True,
            "message": "用户已回到对话界面，当前执行已自动挂起，等待新消息。",
        }
    result: dict[str, Any] = {"status": "completed", "response_type": resp_type}
    if response.get("selected_option"):
        result["selected_option"] = response["selected_option"]
    if response.get("answers"):
        result["answers"] = response["answers"]
    if response.get("feedback"):
        result["feedback"] = response["feedback"]
    return result


# ════════════════════════════════════════════════════════════════════
# 角色 1.5：approval.fold 聚合注入步（审批非阻塞 M1，设计 §三 ③）
# mode=async 提交的审批不阻塞工具——下一 LLM 轮由此步把待决项与已决结果
# 并入 prompt.dynamic_vars 消息尾部（todo_fold 同款降级契约：dynamic_vars
# 缺失零动作、能力缺席 warning 直通、任何异常不阻断管道）。
# ════════════════════════════════════════════════════════════════════

_TICKET_KEY_PREFIX = "approval.tickets."
_DECISION_WORDS = {
    "approved": "已批准",
    "denied": "已拒绝",
    "expired": "已过期",
    "withdrawn": "已撤回",
    "cancelled": "已取消",
}


async def _approval_fold_impl(state: dict[str, Any]) -> dict[str, Any]:
    """fold 实现：读 pipeline-state 收集本管道 ticket → 注入 + 两阶段清理。"""
    dvm = state.get("prompt.dynamic_vars")
    if not isinstance(dvm, dict) or "content" not in dvm:
        return {}  # dynamic_vars 缺失：零动作直通（todo_fold 同款降级契约）
    pipeline_id = str(state.get("pipeline_id") or "")
    try:
        state_cap = plugin.get_capability("pipeline-state")
        if state_cap is None:
            raise KeyError("pipeline-state")
        rows = await state_cap.call("list", {})
    except KeyError:
        logger.warning("[approval_fold] pipeline-state 未注入，零写入直通")
        return {}
    if not isinstance(rows, list):
        return {}

    pending: list[dict[str, Any]] = []
    decided: list[tuple[str, dict[str, Any]]] = []
    consumed_keys: list[str] = []
    consumed_markers: list[str] = []  # 兄弟标记键（fold 置位形态），随批删除
    for row in rows:
        if not isinstance(row, dict) or str(row.get("pipeline_id") or "") != pipeline_id:
            continue
        for key, ticket in row.items():
            if not key.startswith(_TICKET_KEY_PREFIX) or not isinstance(ticket, dict):
                continue  # 兄弟 consumed_at 标记键非 dict，自然跳过
            marker = f"{key}.consumed_at"
            if ticket.get("consumed_at") or row.get(marker):
                # 已消费双形态：ticket 字典内置位 / fold 兄弟键置位——
                # 只补删，绝不重复注入（G7）
                consumed_keys.append(key)
                if row.get(marker):
                    consumed_markers.append(marker)
            elif ticket.get("status") == "pending":
                pending.append(ticket)
            elif isinstance(ticket.get("decision"), dict):
                decided.append((key, ticket))
    if not pending and not decided and not consumed_keys:
        return {}  # 无审批 ticket：零写入

    lines: list[str] = []
    if pending:
        lines.append("待决策的审批项（用户稍后在审批页决定，决定结果下轮注入）：")
        for t in pending:
            cat = str(t.get("category") or "generic")
            blocking_mark = "【须当场处理】" if t.get("blocking") else ""
            title = str(t.get("title") or t.get("ticket_id") or "")
            lines.append(f"- {blocking_mark}{title}（{cat}）")
    if decided:
        lines.append("已收敛的审批决定：")
        for _, t in decided:
            status = str(t.get("status") or "")
            word = _DECISION_WORDS.get(status, status)
            sel = (t.get("decision") or {}).get("selected_option")
            title = str(t.get("title") or t.get("ticket_id") or "")
            lines.append(f"- {title}：{word}" + (f"（选项：{sel}）" if sel else ""))

    new_content = dvm["content"]
    if lines:
        new_content = f"{new_content}\n\n" + "\n".join(lines)

    # 两阶段清理（设计 §5.11）：先 consumed_at 置位（幂等闸，删除失败后
    # 下轮不重复注入）→ delete_keys 真删（内核 pipeline-state.update 支持）。
    # 置位 = 兄弟扁平键 ``<ticket键>.consumed_at``（state 行是扁平键列，
    # timestamp 单值键独立于 ticket 字典）；两段独立 update：置位成功即落定，
    # 删除失败只重试删除（G7 不丢决定）。已决键与标记键同批真删。
    now_iso = datetime.now(UTC).isoformat()
    mark_fields: dict[str, Any] = {f"{key}.consumed_at": now_iso for key, _ in decided}
    delete_keys = (
        [key for key, _ in decided]
        + [f"{key}.consumed_at" for key, _ in decided]
        + consumed_keys
        + consumed_markers
    )
    if mark_fields:
        await state_cap.call("update", {"pipeline_id": pipeline_id, "fields": mark_fields})
    if delete_keys:
        try:
            await state_cap.call(
                "update", {"pipeline_id": pipeline_id, "fields": {}, "delete_keys": delete_keys}
            )
        except Exception as exc:  # noqa: BLE001 — 删除失败下轮重试，不阻断注入
            logger.warning(
                "[approval_fold] delete_keys 失败（下轮仅重试删除，不重复注入）| err=%s", exc
            )
    return {"prompt.dynamic_vars": {"content": new_content}}


@plugin.step("approval.fold")
async def approval_fold(state: dict, config: dict | None = None) -> dict:
    """输入阶段：审批 ticket 聚合注入动态变量消息尾部 + 已决条目两阶段清理。"""
    try:
        return {"state_updates": await _approval_fold_impl(state or {})}
    except Exception as exc:  # noqa: BLE001 — 降级契约：失败不阻断管道
        logger.warning("[approval_fold] 执行失败，零写入直通 | err=%s", exc)
        return {"state_updates": {}}


# ════════════════════════════════════════════════════════════════════
# 角色 2：交互服务工具（interaction.*）
# 其他插件经 capability 调这些工具，或前端响应回路经内核转发到此。
# ════════════════════════════════════════════════════════════════════

@plugin.tool(
    name="interaction.send_notification",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "title": {"type": "string"},
            "message": {"type": "string", "default": ""},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
        },
        "required": ["session_id", "thread_id", "title"],
    },
    description="发送非阻塞通知（服务能力，供其他插件经 capability 调用）",
)
async def interaction_send_notification(
    session_id: str, thread_id: str, title: str, message: str = "", priority: str = "normal",
) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    rid = await _service.send_notification(
        session_id=session_id, thread_id=thread_id, title=title,
        message=message, priority=Priority(priority),
    )
    return {"request_id": rid, "status": "sent"}


@plugin.tool(
    name="interaction.create_choice",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"}, "thread_id": {"type": "string"},
            "tab_id": {"type": "string"}, "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "options": {"type": "array"},
            "timeout_seconds": {"type": "integer", "default": 86400},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
            "user_id": {
                "type": "string",
                "description": "创建者用户（param_inject/调用方服务端注入，用于审批归属归因）",
            },
            "agent_id": {
                "type": "string",
                "description": "发起 Agent 坐标（state agent.id / config_id，卡片来源展示）",
            },
            "agent_level": {
                "type": "string",
                "description": "发起 Agent 层级（L1/L2/L3，卡片来源展示）",
            },
            "pipeline_id": {
                "type": "string",
                "description": "所属管道 ID（卡片来源展示与路由）",
            },
            "agent_name": {
                "type": "string",
                "description": "发起 Agent 显示名（卡片来源展示，缺省由前端缓存解析）",
            },
            "memory_key": {
                "type": "string",
                "description": "拒绝记忆作用域键（创建方声明，推荐 工具+指纹 精确键；缺省回退标题）",
            },
            "category": {
                "type": "string",
                "enum": ["decision", "permission", "generic"],
                "default": "generic",
                "description": "审批分类（decision=决策审批 / permission=权限审批 / generic=其余交互缺省；由发起方显式声明，不由后端按标题猜测，落 ticket 供审批收件箱分栏）",
            },
        },
        "required": ["session_id", "thread_id", "tab_id", "title"],
    },
    description="创建选择模式交互请求（服务能力）",
)
async def interaction_create_choice(
    session_id: str, thread_id: str, tab_id: str, title: str, description: str = "",
    options: list[dict[str, Any]] | None = None,
    timeout_seconds: int = 86400, priority: str = "normal", user_id: str = "",
    agent_id: str = "", agent_level: str = "", pipeline_id: str = "", agent_name: str = "",
    memory_key: str = "", category: str = "",
) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    rid = await _service.create_choice_request(
        session_id=session_id, thread_id=thread_id, tab_id=tab_id, title=title,
        description=description, options=options,
        timeout_seconds=timeout_seconds, priority=Priority(priority),
        user_id=user_id or None,
        agent_id=agent_id or None, agent_level=agent_level or None,
        pipeline_id=pipeline_id or None, agent_name=agent_name or None,
        memory_key=memory_key or None,
        category=category or None,
    )
    return {"request_id": rid, "status": "pending"}


@plugin.tool(
    name="interaction.wait_for_choice",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "timeout": {"type": "number", "default": 86400},
        },
        "required": ["request_id"],
    },
    description="等待用户选择响应（服务能力，阻塞直到用户操作或超时）",
)
async def interaction_wait_for_choice(request_id: str, timeout: float = 86400) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    try:
        return await _service.wait_for_choice(request_id, timeout)
    except InteractionTimeoutError as e:
        # 错误分类统一 error_code（ADR 2026-10-01 决策 8，词源 SDK
        # InteractionErrorCode）：消费方按码分类，禁止对 error 消息做子串嗅探。
        return {
            "error": f"审批等待超时，已按拒绝裁决 (interaction timeout): {e}",
            "error_code": InteractionErrorCode.INTERACTION_TIMEOUT.value,
            "request_id": request_id,
        }
    except InteractionDeniedError as e:
        return {
            "error": f"denied: {e}",
            "error_code": InteractionErrorCode.INTERACTION_DENIED.value,
            "request_id": request_id,
        }
    except InteractionCancelledError as e:
        return {
            "error": f"cancelled: {e}",
            "error_code": InteractionErrorCode.INTERACTION_CANCELLED.value,
            "request_id": request_id,
        }


@plugin.tool(
    name="interaction.respond",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "response": {
                "type": "object",
                "description": "前端应答载荷原样透传（response_type/selected_option/answers/feedback 契约由本插件自持）",
            },
        },
        "required": ["request_id", "response"],
    },
    description="提交交互响应（前端用户操作经内核转发到此，唤醒 wait_for_choice）",
)
async def interaction_respond(request_id: str, response: dict[str, Any]) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    success = await _service.submit_response(
        request_id=request_id,
        response_type=response.get("response_type", "answered"),
        selected_option=response.get("selected_option"),
        answers=response.get("answers"),
        feedback=response.get("feedback"),
    )
    return {"ok": success, "request_id": request_id, "status": "submitted" if success else "not_found"}


@plugin.tool(
    name="interaction.cancel",
    schema={
        "type": "object",
        "properties": {"request_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["request_id"],
    },
    description="取消交互请求（服务能力）",
)
async def interaction_cancel(request_id: str, reason: str | None = None) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    success = await _service.cancel_request(request_id, reason)
    return {"ok": success, "request_id": request_id, "status": "cancelled" if success else "not_found"}


@plugin.tool(
    name="interaction.get_pending",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
        },
    },
    description="获取待处理请求列表（服务能力）",
)
async def interaction_get_pending(session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    requests = await _service.get_pending_requests(session_id=session_id, limit=limit)
    return {"requests": requests, "count": len(requests)}


if __name__ == "__main__":
    plugin.run()
