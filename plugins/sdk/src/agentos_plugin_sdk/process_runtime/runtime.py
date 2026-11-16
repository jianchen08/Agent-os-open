"""EnvProcessRuntime：隔离环境进程运行时的 Tasks 状态机 API。

对外形状对齐官方 MCP Tasks（SEP-1686 Final，随 2025-11-25 规范发布）：

- ``start`` 即 call-now：句柄立即返回不阻塞（fetch-later）；
- ``status`` 对应 tasks/get：working / input_required / completed / failed /
  cancelled 五态，终态不可逆；
- ``result`` 对应 tasks/result：仅 completed 可取，否则 TaskStateError；
- ``send_input`` 对应 input_required + elicitation 通路（向等待输入的进程
  发文本：yes/no、密码、菜单选项）；
- ``cancel`` 对应 notifications/cancelled：终态 cancelled，幂等（SEP §4.8.3
  终态后取消通知被忽略）；
- ``wait`` 是 start + 轮询至终态的便利封装（有限命令阻塞等完的形态），不是
  独立通路——句柄跨轮存活，模型下一轮仍可 status/result/input/cancel。

与 Tasks 的有意差异（内部实现载体决定）：
- taskId = 宿主句柄 pid（ProcessManager 注册键，OS 层唯一不冲突）；
- 状态机不含 submitted（start 同步拿到句柄即 working）；
- input_required 不做主动探测（子进程是否在等 stdin 无可靠通用判据），
  send_input 随时可发、由进程自行消费——与 bash 工具 input action 同语义。

隔离上下文解析：``from_inputs`` 凭 isolation_guard 服务端注入的
``_container_id``/``_exec_backend`` 构造三后端分派（wsl_native/linux_native/
docker）；无注入（非隔离上下文）返回 None，调用方自定宿主分支——
「插件工具按情况选择是否使用隔离」的落点。LLM 侧伪造无效：
tool_args_inject 剥离下划线键，注入面是服务端信任链。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from agentos_plugin_sdk.process_runtime.manager import ProcessManager

# wait 轮询间隔（秒）；对齐 bash 工具 execute 路径的 0.5s 轮询节奏
_WAIT_POLL_INTERVAL = 0.5


class TaskStatus(StrEnum):
    """任务状态（MCP Tasks 词汇；不含 submitted——start 同步返回即 working）。"""

    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集（SEP-1686 §4.3：终态不可逆）
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class TaskStateError(RuntimeError):
    """状态不允许该操作（如 result 在非 completed 态；SEP §4.6.2 错误同形）。

    未知任务（无登记且无磁盘日志）同报此错——对应 tasks/get 查无此 taskId。
    """

    def __init__(self, pid: int, status: TaskStatus, action: str) -> None:
        self.pid = pid
        self.status = status
        super().__init__(
            f"task {pid} 状态为 {status.value}，不允许 {action}（对齐 MCP Tasks 语义：该操作要求特定状态）"
        )


class EnvProcessRuntime:
    """隔离环境内进程运行时（bash 工具 ProcessManager 的 Tasks 形状门面）。

    manager 由调用方持有（通常每插件进程一个单例，日志目录随插件工作区）；
    本类只做 Tasks 词汇映射，不持进程状态。
    """

    def __init__(
        self,
        manager: ProcessManager,
        container_id: str | None = None,
        exec_backend: dict[str, Any] | None = None,
    ) -> None:
        self._manager = manager
        self._container_id = container_id
        self._exec_backend = exec_backend

    @classmethod
    def from_inputs(
        cls,
        inputs: dict[str, Any],
        manager: ProcessManager | None = None,
    ) -> EnvProcessRuntime | None:
        """从工具 inputs 解析隔离上下文；非隔离上下文返回 None。

        - ``_container_id`` 缺失 = 未隔离（guard 未注入）→ None，调用方走
          宿主分支；
        - ``_exec_backend`` 可缺失（docker 环境）→ manager 按 docker exec
          兜底通路执行（与 bash 工具同一行为）。
        """
        container_id = inputs.get("_container_id")
        if not container_id:
            return None
        exec_backend = inputs.get("_exec_backend")
        return cls(
            manager=manager or ProcessManager(),
            container_id=str(container_id),
            exec_backend=exec_backend if isinstance(exec_backend, dict) else None,
        )

    # ── call-now：句柄立即返回 ──────────────────────────────────

    async def start(
        self,
        command: str,
        working_dir: str | None = None,
        *,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        container_id: str | None = None,
        exec_backend: dict[str, Any] | None = None,
        owner: str | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> int:
        """启动进程，立即返回句柄 pid（taskId），不等待完成。

        container_id/exec_backend 缺省用构造值（from_inputs 解析的隔离上下
        文）；显式传参可覆盖（同插件操作多环境时）。超时（timeout 秒）不杀
        进程：命令仍在跑，句柄照常 status/input/cancel——与 bash 工具
        「超时返回 running+pid」同语义。
        """
        cid = container_id if container_id is not None else self._container_id
        backend = exec_backend if exec_backend is not None else self._exec_backend
        pid, _log_file = await self._manager.start_process(
            command,
            working_dir=working_dir,
            env=env,
            container_id=cid,
            owner=owner,
            on_output=on_output,
            exec_backend=backend if cid else None,
        )
        return pid

    # ── tasks/get：状态查询 ─────────────────────────────────────

    def status(self, pid: int) -> TaskStatus:
        """查任务状态（五态；终态不可逆）。未知任务抛 TaskStateError。"""
        return self._snapshot_status(pid)

    # ── tasks/result：结果获取 ──────────────────────────────────

    async def result(self, pid: int) -> dict[str, Any]:
        """获取完成任务的结果（仅 completed；否则 TaskStateError）。

        返回 {"summary": 压缩摘要, "output": 全量输出, "exit_code": 退出码}。
        """
        st = self._snapshot_status(pid)
        if st is not TaskStatus.COMPLETED:
            raise TaskStateError(pid, st, "result")
        # 等输出读取任务收尾再读（快命令竞态防护，manager 契约）
        await self._manager.wait_output_settled(pid)
        info = self._manager.get_process_info(pid)
        return {
            "summary": self._manager.get_summary(pid),
            "output": self._manager.get_output(pid),
            "exit_code": info.exit_code if info else None,
        }

    # ── input_required：交互输入 ────────────────────────────────

    async def send_input(self, pid: int, input_text: str) -> tuple[bool, str | None]:
        """向运行中进程发文本（yes/no、密码、菜单选项）。"""
        return await self._manager.send_input(pid, input_text)

    # ── notifications/cancelled：取消 ───────────────────────────

    async def cancel(self, pid: int, force: bool = True) -> tuple[bool, str | None]:
        """终止进程整树；终态后幂等忽略（SEP-1686 §4.8.3），未知任务报错。"""
        if pid not in self._manager.active_processes:
            disk = self._manager.read_log_by_pid(pid)
            if disk is None:
                return False, f"任务 {pid} 不存在"
            return False, f"任务 {pid} 已终态，取消忽略（SEP-1686 §4.8.3）"
        if self._snapshot_status(pid) in TERMINAL_TASK_STATUSES:
            return False, f"任务 {pid} 已终态，取消忽略（SEP-1686 §4.8.3）"
        return await self._manager.terminate_process(pid, force=force)

    # ── 日志（bash read_log 同源；Tasks 无对应，属运行时增量能力）──

    def read_log(self, pid: int) -> str:
        """读全量命令日志（任何时候可用，进程已结束也能读）。"""
        return self._manager.get_output(pid)

    # ── 便利封装：阻塞等待至终态（有限命令形态）────────────────

    async def wait(self, pid: int, timeout: float = 290.0) -> TaskStatus:
        """轮询至终态或超时；超时返回当前状态（进程不被杀，句柄照常可用）。"""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            st = self._snapshot_status(pid)
            if st in TERMINAL_TASK_STATUSES:
                return st
            remaining = deadline - loop.time()
            if remaining <= 0:
                return st
            await asyncio.sleep(min(_WAIT_POLL_INTERVAL, remaining))

    # ── 内部 ────────────────────────────────────────────────────

    def _snapshot_status(self, pid: int) -> TaskStatus:
        """内部状态 → Tasks 五态映射（终态不可逆由内部 status 单向迁移保证）。"""
        info = self._manager.get_process_info(pid)
        if info is None:
            # 进程已清（即时清理）：磁盘降级路径判断终态——exit_code 从日志
            # 尾部解析（manager 契约）；无日志 = 任务不存在；无退出码 =
            # 非正常收尾（被杀/日志丢失）→ cancelled。
            disk = self._manager.read_log_by_pid(pid)
            if disk is None:
                raise TaskStateError(pid, TaskStatus.FAILED, "status（任务不存在且无日志）")
            exit_code = disk.get("exit_code")
            if exit_code == 0:
                return TaskStatus.COMPLETED
            if exit_code is None:
                return TaskStatus.CANCELLED
            return TaskStatus.FAILED
        if info.status == "running":
            return TaskStatus.WORKING
        if info.status == "terminated":
            return TaskStatus.CANCELLED
        # completed / error：按退出码分态
        if info.exit_code == 0:
            return TaskStatus.COMPLETED
        return TaskStatus.FAILED
