"""内核桥（KernelBridge）：Harbor 适配器 ↔ AgentOS 内核的胶水面。

职责（harbor-free，可独立单测）：
1. 登录（口令走 AGENTOS_ADMIN_PASSWORD / 显式参数，不硬编码任何密钥）；
2. 建会话（agent_id 绑定 → config/agents/**/<agent_id>.yaml 被管道加载）；
3. 绑定外部容器（POST /ext/isolation/external/bind，容器不可达显式失败）；
4. 派发 instruction 并等终态，双通道（R17 起，缺省任务接口）：
   - 任务接口（dispatch_mode="task"，产品原样派发）：POST /ext/task_service/
     tasks/root 建真实任务（显式 workspace + plain + isolated，出生协议同步
     准备工作区后即派发）→ 轮询 task.status 至终态。任务管道 task.id 在场，
     task_reminder 提醒 / 评估闸门 / 失败轮 route 8 全部原生参与——纯文本轮
     「无工具调用=完成」的会话管道语义洞不再可达（R17 改向根修）；
   - 会话通道（dispatch_mode="session"，向后兼容保留）：WS user_input 派发 +
     轮询管道 run_status（R6-R16 既有链路原样）；
5. R12 时间预算注记两通道同源（原题逐字在前注记在后）；超长原题走容器内
   指令文件指针（task_submit 描述 2000 字硬闸的 harness 侧补偿）。

复用约定：kernel_client.py 原样复用（不改动）；create_session 需要的 agent_id
扩展经 client._authed_json 直发同端点（会话建卡端点的原生可选字段）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# kernel_client 同目录（scripts/eval_bench/）：pytest 与 harbor import 两种
# 上下文都先把 eval_bench 目录推上 sys.path 再裸名导入
_EVAL_BENCH_DIR = Path(__file__).resolve().parent.parent
if str(_EVAL_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_BENCH_DIR))

import kernel_client  # noqa: E402
from kernel_client import KernelClient, KernelClientError  # noqa: E402

#: 管道终态集合（run_eval.py wait_settled 同口径；会话通道用）
RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: 任务域终态集合（TaskStatus 词汇：pending/running/evaluating/stopped/
#: completed/failed/timeout——stopped 是可唤醒暂存态不算终局，预算耗尽
#: 如实回报快照）。task.status 署名终局 = completed/failed/timeout。
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout"})

#: WS 收流超时缺省（秒）；比管道单轮默认上限宽裕，跑满即靠 run_status 轮询兜底
DEFAULT_STREAM_TIMEOUT_S = 1800.0

#: run_status / task_status 轮询间隔（秒）
DEFAULT_POLL_INTERVAL_S = 2.0

#: 轮询进度日志节奏（秒）：超长预算（如 task.toml 28800s = 8h）下每 60s 一条
#: 进度，避免长轮询静默（R19）
POLL_PROGRESS_LOG_INTERVAL_S = 60.0

logger = logging.getLogger(__name__)

#: task_submit 描述长度硬闸（plugins/shared/task_submit/tool.py
#: _gate_base_submission：2000 字超限 DESCRIPTION_TOO_LONG 拒绝）
TASK_DESCRIPTION_MAX_CHARS = 2000

#: 内联描述上限（适配器侧，低于产品闸留缓冲——kickoff 组装面不受此限，
#: 但描述本身须过闸；超限转容器内指令文件指针）
INLINE_DESCRIPTION_MAX_CHARS = 1800

#: 容器内指令文件路径（指针模式：原题 base64 直写容器，模型经 file_read/
#: bash 读取——fs/bash 双工具面都可达，容器 workdir=/workspace 既有锚）
CONTAINER_INSTRUCTION_PATH = "/workspace/.agentos_task_instruction.md"

#: 指针模式描述模板（{path} 由 CONTAINER_INSTRUCTION_PATH 填充）
CONTAINER_INSTRUCTION_POINTER_TEMPLATE = (
    "Your full task instruction is in the file {path} inside your execution "
    "environment. Read that file first (file_read or bash), then complete "
    "the task it describes. The file content is the authoritative task "
    "instruction; this message is only a pointer to it."
)

#: 时间预算注记模板（R12，防无限分析循环）。原题在前、注记在后；{minutes}
#: 由派发时解析出的轮询预算换算，任务无关、不写死任何路径。模型不知道
#: 总预算/verifier 只读交付物时会在分析阶段烧满全程（minimax 终跑实证：
#: 19 分钟分析零交付）。
HARNESS_TIME_BUDGET_TEMPLATE = (
    "Time budget: you have about {minutes} minutes in total for this task.\n"
    "When time runs out, the verifier reads your deliverable as-is — an "
    "incomplete deliverable scores the same as nothing.\n"
    "Budget accordingly: do a single focused analysis pass, avoid repeated "
    "exploratory loops and re-checking the same files, and write the final "
    "deliverable EARLY (a complete draft scores better than a perfect "
    "analysis). Refine only if time remains."
)


def harness_time_budget_note(budget_s: float) -> str:
    """按轮询预算（秒）生成时间预算注记（分钟换算，任务无关）。

    分钟数 = 预算秒 / 60 四舍五入，下限 1（极小预算不给「0 分钟」这种
    退化文案）。与派发用轮询预算同一数值源（R19 起该预算第一优先取自
    评测基准 task.toml [agent] timeout_sec，与 harbor trial 执法同源）。
    """
    minutes = max(1, round(budget_s / 60.0))
    return HARNESS_TIME_BUDGET_TEMPLATE.format(minutes=minutes)


def compose_dispatch_message(instruction: str, budget_s: float) -> str:
    """派发消息组合：原题逐字在前，时间预算注记在后（R12）。

    原题不被改动（逐字前缀）；注记经空行分隔追加。执行/产出纪律由内核侧
    注记承载，评测题原文保持纯净（trace 可证原题逐字）。
    """
    return f"{instruction}\n\n{harness_time_budget_note(budget_s)}"


def compose_task_description(instruction: str, budget_s: float) -> tuple[str, bool]:
    """任务接口描述组合（R17）：内联原题 or 容器内指令文件指针。

    - 内联（len ≤ INLINE_DESCRIPTION_MAX_CHARS）：原题逐字 + 预算注记，
      返回 (description, False)——调用方无需写指令文件；
    - 指针（超限，task_submit 2000 字硬闸）：描述 = 指针文案 + 预算注记，
      返回 (description, True)——调用方须先把原题 base64 直写容器
      （CONTAINER_INSTRUCTION_PATH），再带 instruction_file_path 派发。

    原题本身两种形态都逐字保真（内联在描述里；指针模式在容器文件里，
    base64 无损），trace 可证。
    """
    inline = compose_dispatch_message(instruction, budget_s)
    if len(inline) <= INLINE_DESCRIPTION_MAX_CHARS:
        return inline, False
    pointer = CONTAINER_INSTRUCTION_POINTER_TEMPLATE.format(
        path=CONTAINER_INSTRUCTION_PATH
    )
    return f"{pointer}\n\n{harness_time_budget_note(budget_s)}", True


class KernelBridgeError(RuntimeError):
    """桥接面失败（绑定被拒/会话建卡失败/派发超时等）。"""


@dataclass
class RunOutcome:
    """一次「绑定 → 派发 → 等终态」的结果快照（双通道共用）。

    session 通道填 session_id/stream_terminal/run_status/interactions；
    task 通道填 task_id/task_status/task_result/task_error（pipeline_id =
    task_id，任务即管道）。dispatch_mode 标记来源通道。
    """

    session_id: str = ""
    pipeline_id: str | None = None
    stream_terminal: str = "deadline"
    run_status: str = ""  # completed / failed / cancelled / 空（未观测到）
    interactions: list[dict[str, Any]] = field(default_factory=list)
    container_name: str = ""
    workspace: str = ""
    # R17 任务接口面（dispatch_mode="task" 时有效）
    dispatch_mode: str = "session"  # "task" | "session"
    task_id: str = ""
    task_status: str = ""  # completed / failed / timeout / stopped / running / ...
    task_result: dict[str, Any] | None = None
    task_error: str | None = None
    instruction_file_path: str = ""  # 指针模式的容器内指令文件路径（空=内联）

    @property
    def ok(self) -> bool:
        """链路成功判定（按通道分派）。

        - task 通道：task.status 署名终局 completed 即成功；failed/timeout
          如实判失败（评估闸门/熔断署名的产品裁决，透传不翻案——TB 真判分
          在 harbor verifier，这里只决定 trial 是否正常收敛）。
        - session 通道（既有语义）：流终态正常且 run_status 终态非
          failed/cancelled。
        """
        if self.dispatch_mode == "task":
            return self.task_status == "completed"
        if self.stream_terminal in ("stream_error", "error"):
            return False
        return self.run_status in ("completed", "") and self.stream_terminal in (
            "stream_end",
            "deadline",
        )


class KernelBridge:
    """AgentOS 内核桥（每 TB trial 一个实例）。"""

    def __init__(
        self,
        base_url: str,
        admin_password: str | None = None,
        username: str = "admin",
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        client_factory: Any = None,
    ) -> None:
        """初始化桥。

        Args:
            base_url: 内核 HTTP 基址（如 http://127.0.0.1:9101）。
            admin_password: admin 口令；缺省读 AGENTOS_ADMIN_PASSWORD 环境变量
                （spawn_test_kernel 既有机制，不硬编码任何密钥）。
            username: 登录用户名（内核缺省 admin）。
            poll_interval_s: run_status 轮询间隔。
            client_factory: KernelClient 构造器注入点（单测桩；缺省真类）。
        """
        password = admin_password or os.environ.get("AGENTOS_ADMIN_PASSWORD") or ""
        if not password:
            raise KernelBridgeError(
                "缺少内核口令：设置 AGENTOS_ADMIN_PASSWORD 环境变量或显式传入"
                "（口令恒读环境/参数，不落代码）"
            )
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._poll_interval_s = poll_interval_s
        client_cls = client_factory or KernelClient
        self._client: KernelClient = client_cls(self._base_url)
        self._logged_in = False

    # ── 登录与会话 ──────────────────────────────────────────────

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._client.login(self._username, self._password)
            self._logged_in = True

    def create_session(self, title: str, agent_id: str) -> str:
        """建会话并绑定 agent 身份（thread 注册 agent_id → 管道加载该 agent yaml）。

        agent_id 扩展是该端点原生可选字段（session_routes.rs 出生绑定），经
        client._authed_json 直发——kernel_client.create_session 只传 title，
        本方法不改动 glue 文件、按同一 REST 面扩展。
        """
        self._ensure_login()
        body = self._client._authed_json(
            "POST",
            f"{self._client.base_url}/api/v1/sessions",
            {"title": title, "agent_id": agent_id},
        )
        thread_id = body.get("thread_id")
        if not thread_id:
            raise KernelBridgeError(f"创建会话响应缺少 thread_id: {body}")
        return str(thread_id)

    # ── 任务接口（R17：产品原样派发通道）────────────────────────

    def create_task(
        self,
        *,
        title: str,
        description: str,
        agent_id: str,
        workspace: str,
    ) -> str:
        """经任务接口建真实任务（POST /ext/task_service/tasks/root）。

        产品原样派发：root 创建端点把表单字段映射为 task_submit 工具入参
        （parent_agent_level=1），出生协议四段式同步准备工作区后即派发任务
        管道（create 即派发，无独立 submit 步）。显式 workspace + plain +
        isolated：任务管道 state.workspace = 本工作空间（绑定键对齐——setup
        期已按同键绑定外部容器，guard 查询即命中，R7/R11/R15 路由链原样
        生效）；task.id 在场 → task_reminder/评估闸门/route 8 原生参与。

        Returns:
            task_id（= pipeline_id，引擎生成的 12 位短 id）。

        Raises:
            KernelBridgeError: 非 2xx（描述超长/工作空间不安全/agent 不存在
                等工具闸拒绝）或响应缺 id。
        """
        self._ensure_login()
        payload = {
            "title": title,
            "description": description,
            "target_id": agent_id,
            "workspace": workspace,
            "workspace_mode": "plain",
            "isolation_level": "isolated",
            # 无会话根任务（task_birth 缺省语义：独立自环，不挂用户会话）
            "thread_id": "",
        }
        try:
            body = self._client._authed_json(
                "POST",
                f"{self._client.base_url}/ext/task_service/tasks/root",
                payload,
                timeout=60,
            )
        except KernelClientError as exc:
            raise KernelBridgeError(f"任务创建被拒（fail-loud）: {exc}") from exc
        task_id = str(body.get("id") or body.get("task_id") or "")
        if not task_id:
            raise KernelBridgeError(f"任务创建响应缺少 id: {body}")
        return task_id

    def _read_task_status(self, task_id: str) -> tuple[str, dict[str, Any]]:
        """读单任务状态（GET /ext/task_service/tasks/{id}；读失败按空串继续轮询）。

        Returns:
            (status, 响应体)；读失败返回 ("", {})。
        """
        try:
            body = self._client._authed_json(
                "GET",
                f"{self._client.base_url}/ext/task_service/tasks/{task_id}",
                timeout=60,
            )
        except KernelClientError:
            return "", {}
        if not isinstance(body, dict):
            return "", {}
        return str(body.get("status") or ""), body

    async def poll_task_status(
        self, task_id: str, timeout_s: float
    ) -> tuple[str, dict[str, Any]]:
        """轮询 task.status 至任务域终态（completed/failed/timeout）。

        超时返回当前快照（status 可能为 running/stopped/空——调用方按未收敛
        判失败）。读失败按空串继续轮询（瞬时故障，_read_run_status 同口径）。
        每 POLL_PROGRESS_LOG_INTERVAL_S 一条进度日志（R19：超长预算下避免
        长轮询静默）。
        """
        loop = asyncio.get_event_loop()
        started = loop.time()
        deadline = started + timeout_s
        status: str = ""
        body: dict[str, Any] = {}
        last_progress = started
        while loop.time() < deadline:
            status, body = self._read_task_status(task_id)
            if status in TASK_TERMINAL_STATUSES:
                return status, body
            now = loop.time()
            if now - last_progress >= POLL_PROGRESS_LOG_INTERVAL_S:
                logger.info(
                    "[KernelBridge] task 轮询进行中 | task=%s | elapsed=%ds/%ds"
                    " | status=%s",
                    task_id,
                    int(now - started),
                    int(timeout_s),
                    status or "<unread>",
                )
                last_progress = now
            await asyncio.sleep(self._poll_interval_s)
        logger.warning(
            "[KernelBridge] task 轮询预算耗尽 | task=%s | budget=%ds"
            " | last_status=%s",
            task_id,
            int(timeout_s),
            status or "<unread>",
        )
        return status, body

    # ── 外部容器绑定 ────────────────────────────────────────────

    def bind_external_container(
        self,
        task_id: str,
        workspace: str,
        container: str,
        workdir: str = "/workspace",
    ) -> dict[str, Any]:
        """绑定外部容器（POST /ext/isolation/external/bind）。

        容器不可达时内核返回 422 → KernelClientError（带响应体摘要），
        本方法转 KernelBridgeError 显式失败（fail-loud，评测题直接判失败）。
        """
        self._ensure_login()
        payload = {
            "task_id": task_id,
            "workspace": workspace,
            "container": container,
            "workdir": workdir,
        }
        try:
            body = self._client._authed_json(
                "POST",
                f"{self._client.base_url}/ext/isolation_service/external/bind",
                payload,
                timeout=60,
            )
        except KernelClientError as exc:
            raise KernelBridgeError(f"外部容器绑定被拒（fail-loud）: {exc}") from exc
        # 4xx 语义已被上面转错；2xx 信封内 success=False 同样显式失败
        if isinstance(body, dict) and body.get("success") is False:
            raise KernelBridgeError(f"外部容器绑定失败: {body.get('error')}")
        return body if isinstance(body, dict) else {"raw": body}

    def unbind_external_container(self, task_id: str | None = None, workspace: str | None = None) -> None:
        """解除绑定（尽力而为：失败只留痕不阻断——trial 结束清理路径）。"""
        self._ensure_login()
        try:
            self._client._authed_json(
                "POST",
                f"{self._client.base_url}/ext/isolation_service/external/unbind",
                {"task_id": task_id or "", "workspace": workspace or ""},
                timeout=30,
            )
        except KernelClientError as exc:
            print(f"[harbor_agentos] unbind 失败（忽略）: {exc}")

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """取消内核任务（POST /ext/task_service/tasks/{id}/cancel，R22 补）。

        轮询预算耗尽/调用方放弃时止血：内核侧任务管道随即停跑，不再烧 LLM
        token（R22 实证：harbor 判超时收尾后孤儿任务继续 running 数小时）。
        调用方（_run_task_via_task_interface）对失败留痕不阻断。
        """
        self._ensure_login()
        body = self._client._authed_json(
            "POST",
            f"{self._client.base_url}/ext/task_service/tasks/{task_id}/cancel",
            {},
            timeout=30,
        )
        return body if isinstance(body, dict) else {"raw": body}

    # ── 派发与轮询 ──────────────────────────────────────────────

    async def run_task(
        self,
        instruction: str,
        *,
        workspace: str,
        container_name: str,
        agent_id: str,
        title: str,
        stream_timeout_s: float = DEFAULT_STREAM_TIMEOUT_S,
        poll_timeout_s: float = DEFAULT_STREAM_TIMEOUT_S,
        on_interaction: Any = None,
        dispatch_mode: str = "session",
        instruction_file_path: str = "",
    ) -> RunOutcome:
        """全链路：派发 instruction → 等终态（双通道，R17 起）。

        Args:
            instruction: TB 任务指令（原样派发；执行空间语义由绑定 + workspace 承载）。
            workspace: 宿主工作空间（绑定键 + 任务/会话管道的 state.workspace，
                两通道必须同键——R17 绑定键对齐判据）。
            container_name: 已绑定的容器名（写入 outcome 供审计）。
            agent_id: 执行身份（评测 agent 配置加载键；task 通道 = target_id）。
            title: 会话标题 / 任务标题。
            stream_timeout_s: WS 收流上限（仅 session 通道；task 通道无 WS 流）。
            poll_timeout_s: 终态轮询上限（超过即返回当前快照）；同一预算换算成
                分钟随描述下发给模型（R12 时间预算注记，两通道同源）。
            on_interaction: 审批回调（仅 session 通道）。
            dispatch_mode: "task"（任务接口，产品原样）| "session"（WS 会话，
                向后兼容保留）。
            instruction_file_path: 容器内指令文件路径（compose_task_description
                判定指针模式时由调用方传入；空 = 内联描述）。

        Returns:
            RunOutcome（ok=False 时调用方按失败题计）。
        """
        if dispatch_mode == "task":
            return await self._run_task_via_task_interface(
                instruction,
                workspace=workspace,
                container_name=container_name,
                agent_id=agent_id,
                title=title,
                poll_timeout_s=poll_timeout_s,
                instruction_file_path=instruction_file_path,
            )
        return await self._run_task_via_session(
            instruction,
            workspace=workspace,
            container_name=container_name,
            agent_id=agent_id,
            title=title,
            stream_timeout_s=stream_timeout_s,
            poll_timeout_s=poll_timeout_s,
            on_interaction=on_interaction,
        )

    async def _run_task_via_task_interface(
        self,
        instruction: str,
        *,
        workspace: str,
        container_name: str,
        agent_id: str,
        title: str,
        poll_timeout_s: float,
        instruction_file_path: str,
    ) -> RunOutcome:
        """任务接口通道：建真实任务 → 轮询 task.status 至终态（R17）。

        描述组合：指针模式（instruction_file_path 在场）用指针文案 + 预算
        注记（原题已在容器文件里，base64 无损）；否则内联原题 + 注记。终态
        completed/failed/timeout 均如实透传（评估闸门署名的产品裁决，TB 真
        判分在 harbor verifier）。
        """
        if instruction_file_path:
            pointer = CONTAINER_INSTRUCTION_POINTER_TEMPLATE.format(
                path=instruction_file_path
            )
            description = (
                f"{pointer}\n\n{harness_time_budget_note(poll_timeout_s)}"
            )
        else:
            description = compose_dispatch_message(instruction, poll_timeout_s)
        outcome = RunOutcome(
            container_name=container_name,
            workspace=workspace,
            dispatch_mode="task",
            instruction_file_path=instruction_file_path,
        )
        task_id = self.create_task(
            title=title, description=description, agent_id=agent_id, workspace=workspace
        )
        outcome.task_id = task_id
        # 任务即管道（GAP-1 统一）：引擎生成 pipeline_id = task.id
        outcome.pipeline_id = task_id
        status, body = await self.poll_task_status(task_id, timeout_s=poll_timeout_s)
        if status not in TASK_TERMINAL_STATUSES:
            # 轮询预算耗尽仍未终局：取消内核任务再返回快照（R22 实证缺口——
            # harbor 判超时后孤儿任务继续烧 LLM token 数小时）。取消失败只
            # 留痕不阻断（快照如实返回，调用方按未收敛判失败）。
            try:
                self.cancel_task(task_id)
                logger.info(
                    "[KernelBridge] 轮询预算耗尽，孤儿任务已取消 | task=%s | last_status=%s",
                    task_id,
                    status or "<unread>",
                )
            except KernelClientError as exc:
                logger.warning(
                    "[KernelBridge] 孤儿任务取消失败（快照照常返回）: %s", exc
                )
        outcome.task_status = status
        result = body.get("result")
        outcome.task_result = result if isinstance(result, dict) else None
        error = body.get("error")
        outcome.task_error = str(error) if error else None
        return outcome

    async def _run_task_via_session(
        self,
        instruction: str,
        *,
        workspace: str,
        container_name: str,
        agent_id: str,
        title: str,
        stream_timeout_s: float,
        poll_timeout_s: float,
        on_interaction: Any,
    ) -> RunOutcome:
        """会话通道（向后兼容，R6-R16 既有链路原样）：WS 派发 + 轮询管道态。"""
        session_id = self.create_session(title, agent_id)
        task_key = f"tb-{uuid.uuid4().hex[:12]}"
        ws_url = self._client.ws_chat_url()
        outcome = RunOutcome(
            session_id=session_id,
            container_name=container_name,
            workspace=workspace,
            dispatch_mode="session",
        )

        result = await kernel_client.dispatch_and_collect(
            ws_url,
            session_id,
            compose_dispatch_message(instruction, poll_timeout_s),
            client_message_id=f"{task_key}-msg-0",
            on_interaction=on_interaction,
            timeout_s=stream_timeout_s,
            execution_context={
                "workspace": {"source_path": workspace, "mode": "plain"},
                "isolation": {"level": "isolated"},
            },
        )
        outcome.stream_terminal = result.terminal
        outcome.pipeline_id = result.pipeline_id
        outcome.interactions = result.interactions
        if result.terminal in ("stream_error", "error"):
            # 管道异常收尾：run_status 以失败计（不再等轮询）
            outcome.run_status = "failed"
            return outcome

        # 轮询 run_status 至终态（流结束早于管道落库的竞态兜底）
        status = await self.poll_run_status(outcome.pipeline_id, timeout_s=poll_timeout_s)
        outcome.run_status = status
        return outcome

    async def poll_run_status(self, pipeline_id: str | None, timeout_s: float) -> str:
        """轮询指定管道 run_status 至终态；超时返回当前值（可能为空）。

        pipeline_id 缺失（事件未携带）：轮询全表找唯一活跃新管道不可靠，
        直接返回空串（调用方按 stream_end 语义判成）。进度日志节奏与
        poll_task_status 同口径（R19）。
        """
        if not pipeline_id:
            return ""
        loop = asyncio.get_event_loop()
        started = loop.time()
        deadline = started + timeout_s
        status = ""
        last_progress = started
        while loop.time() < deadline:
            status = self._read_run_status(pipeline_id)
            if status in RUN_TERMINAL_STATUSES:
                return status
            now = loop.time()
            if now - last_progress >= POLL_PROGRESS_LOG_INTERVAL_S:
                logger.info(
                    "[KernelBridge] 管道轮询进行中 | pipeline=%s | elapsed=%ds/%ds"
                    " | status=%s",
                    pipeline_id,
                    int(now - started),
                    int(timeout_s),
                    status or "<unread>",
                )
                last_progress = now
            await asyncio.sleep(self._poll_interval_s)
        return status

    def _read_run_status(self, pipeline_id: str) -> str:
        """读单管道 run_status（REST 轮询面；读失败按空串继续轮询）。"""
        try:
            body = self._client._authed_json(
                "GET",
                f"{self._client.base_url}/api/v1/pipelines/state",
            )
        except KernelClientError:
            return ""
        items = body.get("items") if isinstance(body, dict) else body
        for row in items or []:
            if not isinstance(row, dict):
                continue
            if row.get("pipeline_id") != pipeline_id:
                continue
            state = row.get("state")
            if isinstance(state, dict):
                return str(state.get("run_status") or "")
        return ""


def outcome_to_context_metadata(outcome: RunOutcome) -> dict[str, Any]:
    """RunOutcome → AgentContext.metadata（审计面：全部坐标一处留痕）。

    按通道分形（R17）：session 通道保持既有键面（向后兼容）+ dispatch_mode；
    task 通道记任务域坐标（task_id/task_status/result/error/指令文件路径）。
    """
    common = {
        "dispatch_mode": outcome.dispatch_mode,
        "container_name": outcome.container_name,
        "workspace": outcome.workspace,
    }
    if outcome.dispatch_mode == "task":
        return {
            "agentos": {
                **common,
                "task_id": outcome.task_id,
                "pipeline_id": outcome.pipeline_id,
                "task_status": outcome.task_status,
                "task_result": outcome.task_result,
                "task_error": outcome.task_error,
                "instruction_file_path": outcome.instruction_file_path,
            }
        }
    return {
        "agentos": {
            **common,
            "session_id": outcome.session_id,
            "pipeline_id": outcome.pipeline_id,
            "stream_terminal": outcome.stream_terminal,
            "run_status": outcome.run_status,
            "interaction_count": len(outcome.interactions),
        }
    }


def dumps_outcome(outcome: RunOutcome) -> str:
    """RunOutcome 紧凑 JSON（日志/trajectory 落盘用）。"""
    return json.dumps(outcome_to_context_metadata(outcome), ensure_ascii=False)
