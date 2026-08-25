# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""Bash 插件 0.2 sidecar 生命周期/越权/日志安全测试。

覆盖评审 H-issue（仅 0.2 代码：plugins/shared/tools/bash + plugins/sdk）：
1. 状态生命周期：MCP 调用间复用同一 BashTool/ProcessManager 单例——
   execute → input → continue → terminate 全链路跨调用可用（原 bug：
   server.py 每次调用新建 BashTool 导致进程状态丢失）。
2. 真实调用方式：子进程 e2e（spawn `python server.py` + JSON-RPC 多轮），
   验证 sidecar 自包含（不再依赖 0.1 src 树——原 bug：No module named
   'src.config'）且状态跨调用保持。
3. 越权防护：跨会话 owner 的 terminate/continue/read_log 被拒
   （PROCESS_FORBIDDEN）；无身份调用方操作有主进程被拒。
4. 日志安全：命令/输入敏感信息掩码（Authorization/Bearer/API key/密码）。
5. 大日志摘要 tail 读取：summary 只喂尾部窗口，read_log 完整输出。

环境说明：命令经 ProcessManager 的 shell 选择（WSL > Git Bash > CMD），
长任务/交互测试依赖 bash 语法（echo/read/sleep/seq），无 bash/wsl 时跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "tools" / "bash"
_SDK_DIR = Path(__file__).resolve().parents[3] / "plugins" / "sdk" / "src"

_NEEDS_BASH = pytest.mark.skipif(
    not (shutil.which("bash") or shutil.which("wsl")),
    reason="需要 bash/wsl（命令经 ProcessManager 的 shell 选择执行）",
)


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def bash_server_module():
    """加载 server.py 模块（sys.path 注入插件目录 + SDK 目录）。

    返回模块后，其 `_get_tool()` 单例与已注册的 `plugin._tools` handler
    可直接用于进程内测试（等价于 sidecar 进程内的多次 MCP 调用）。

    2026-08-23 串扰修复：tests/suites/plugins/ 不在 tests/plugins/conftest.py
    的裸名逐出钩子覆盖范围，先于本套件运行的 tests/plugins/tools/ 测试
    （如 test_task_evaluate_server_import.py）加载 task_evaluate/server.py 时，
    其模块级 ``sys.path.insert(0, 本目录)`` 会把 task_evaluate 目录永久压在
    sys.path[0]，且 'tool' 槽位被缓存成 task_evaluate/tool.py——本套件
    server.py ``from tool import BashTool`` 命中错误缓存 → 11 ERROR 簇。
    加载前逐出平铺同名裸模块 + 把 bash 目录**提升**到 sys.path[0]
    （仅「不存在才插入」不够，必须摘除后重插到首位；与
    tests/plugins/system/tasks/test_tasks_http_api.py 的 fixture 同款纪律）。
    """
    for p in (str(_PLUGIN_DIR), str(_SDK_DIR)):
        while p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(_SDK_DIR))
    sys.path.insert(0, str(_PLUGIN_DIR))
    for m in ("tool", "server", "plugin", "process_manager"):
        sys.modules.pop(m, None)

    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "bash_tool_sidecar", _PLUGIN_DIR / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bash_tool_sidecar"] = module
    spec.loader.exec_module(module)
    yield module
    # 清理：终止测试期间启动的所有进程
    try:
        import asyncio  # noqa: PLC0415

        asyncio.run(module._get_tool().process_manager.shutdown_all())
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def bash_handler(bash_server_module):
    """server.py 注册的 MCP 工具 handler（进程内直调，等价一次 MCP 调用）。"""
    return bash_server_module.plugin._tools["bash_execute"].handler


def _call(handler, **kwargs):
    """调用 handler 并返回其 data（server.py 成功时返回 result.output）。"""
    return handler(**kwargs)


# ============================================================================
# 1. 状态生命周期：单例 + 跨调用全链路
# ============================================================================

class TestStateLifecycle:
    """MCP 调用间进程状态保持（评审核心 H-issue）。"""

    def test_get_tool_is_singleton(self, bash_server_module):
        """模块级单例：多次调用返回同一实例（ProcessManager 共享）。"""
        assert bash_server_module._get_tool() is bash_server_module._get_tool()

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_cross_call_execute_input_continue_terminate(self, bash_handler):
        """跨调用全链路：execute(长任务) → input → continue → terminate。

        每次调用都走 handler（等价独立的 MCP tools/call），进程必须
        由共享单例的 ProcessManager 命中——否则 input/continue 会报
        "进程不存在"。
        """
        result = await bash_handler(
            action="execute",
            command="echo START; read line; echo GOT:$line",
            timeout=3,
            _owner="sess-1",
        )
        assert result["status"] == "running", result
        pid = result["pid"]
        assert isinstance(pid, int) and pid > 0

        # 跨调用 input（MCP call #2）
        result = await bash_handler(action="input", pid=pid, input_text="hello", _owner="sess-1")
        assert result["status"] == "running", result

        # 跨调用 continue（MCP call #3）——必须拿到已输入的内容
        result = await bash_handler(action="continue", pid=pid, timeout=10, _owner="sess-1")
        assert result["status"] == "completed", result
        assert "GOT:hello" in result.get("output", ""), result

        # 跨调用 read_log（MCP call #4，进程已结束→磁盘降级路径）
        result = await bash_handler(action="read_log", pid=pid, _owner="sess-1")
        assert result["status"] == "completed", result
        assert "GOT:hello" in result.get("output", ""), result

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_execute_completed_short_command(self, bash_handler):
        """快命令直接 completed（不经过 running 分支）。"""
        result = await bash_handler(
            action="execute", command="echo quick-42", timeout=10, _owner="sess-2"
        )
        assert result["status"] == "completed", result
        assert "quick-42" in result.get("output", "")

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_unknown_pid_returns_not_found(self, bash_handler):
        """未知 pid 的 continue/terminate 报 PROCESS_NOT_FOUND。"""
        result = await bash_handler(action="continue", pid=999999, _owner="sess-1")
        assert result.get("error_code") == "PROCESS_NOT_FOUND", result
        result = await bash_handler(action="terminate", pid=999999, _owner="sess-1")
        assert result.get("error_code") == "PROCESS_NOT_FOUND", result


# ============================================================================
# 2. 越权防护（owner 校验）
# ============================================================================

class TestOwnerEnforcement:
    """跨会话 pid 级操作拒绝（评审：仅 PID 查找的越权风险）。"""

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_cross_session_terminate_rejected(self, bash_handler):
        """A 会话启动的进程，B 会话 terminate 必须被拒。"""
        result = await bash_handler(action="execute", command="sleep 30", timeout=2, _owner="sess-A")
        assert result["status"] == "running", result
        pid = result["pid"]

        try:
            # 跨会话 terminate → 拒绝
            result = await bash_handler(action="terminate", pid=pid, _owner="sess-B")
            assert "PROCESS_FORBIDDEN" in result.get("error", ""), result
            # 无身份调用方操作有主进程 → 拒绝（防劫持）
            result = await bash_handler(action="terminate", pid=pid)
            assert "PROCESS_FORBIDDEN" in result.get("error", ""), result
            # 跨会话 read_log → 拒绝
            result = await bash_handler(action="read_log", pid=pid, _owner="sess-B")
            assert "PROCESS_FORBIDDEN" in result.get("error", ""), result
            # 本会话 terminate → 成功
            result = await bash_handler(action="terminate", pid=pid, _owner="sess-A")
            assert result["status"] == "terminated", result
        finally:
            # 兜底清理（防失败残留）
            result = await bash_handler(action="terminate", pid=pid, _owner="sess-A", force=True)
            assert result.get("status") in ("terminated", None)

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_cross_session_continue_rejected_while_running(self, bash_handler):
        """运行中的进程，跨会话 continue 必须被拒。"""
        result = await bash_handler(action="execute", command="sleep 30", timeout=2, _owner="sess-A")
        assert result["status"] == "running", result
        pid = result["pid"]
        try:
            result = await bash_handler(action="continue", pid=pid, timeout=1, _owner="sess-B")
            assert "PROCESS_FORBIDDEN" in result.get("error", ""), result
        finally:
            await bash_handler(action="terminate", pid=pid, _owner="sess-A", force=True)

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_ownerless_process_disk_fallback(self, bash_handler):
        """无身份（owner=None）调用的进程，后续有身份调用方不可操作。"""
        result = await bash_handler(
            action="execute", command="echo ownerless", timeout=10
        )
        assert result["status"] == "completed", result
        pid = result["pid"]
        # 有身份调用方操作无主进程 → 拒绝（防劫持）
        result = await bash_handler(action="read_log", pid=pid, _owner="sess-X")
        assert "PROCESS_FORBIDDEN" in result.get("error", ""), result
        # 双无身份 → 放行（兼容路径）
        result = await bash_handler(action="read_log", pid=pid)
        assert result["status"] == "completed", result


# ============================================================================
# 3. 日志安全：命令/输入敏感信息掩码
# ============================================================================

class TestLogSecurity:
    """命令与输入落盘前的敏感信息掩码。"""

    def test_mask_secrets(self, bash_server_module):
        """_mask_secrets：Authorization/Bearer/API key/URL 凭据取值被掩。"""
        mask = bash_server_module._get_tool().process_manager._mask_secrets
        assert mask('curl -H "Authorization: Bearer tok123" https://api') == (
            'curl -H "Authorization: Bearer ***" https://api'
        )
        assert mask("API_KEY=sk-abc123 run.sh") == "API_KEY=*** run.sh"
        assert mask("git clone https://user:pass@github.com/r.git") == (
            "git clone https://user:***@github.com/r.git"
        )
        assert mask("python -m pip install requests") == "python -m pip install requests"

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_log_file_command_masked(self, bash_server_module, bash_handler):
        """日志头 # Command: 落盘的是掩码后的命令，输出不受影响。"""
        tool = bash_server_module._get_tool()
        secret = "sk-super-secret-token"
        result = await bash_handler(
            action="execute",
            command=f"echo API_KEY={secret}",
            timeout=10,
            _owner="sess-1",
        )
        assert result["status"] == "completed", result
        pid = result["pid"]

        log_file = tool.process_manager.log_dir / f"bash_{pid}.log"
        assert log_file.exists(), f"日志文件缺失: {log_file}"
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        # 只检查日志头部 # 开头的注释行（# Command: 等元信息行）。
        # 命令输出（含 echo 明文）和 WSL 注入的 stderr 翻译消息不属于日志头，
        # 不应参与掩码校验——只验 # 注释行里的命令记录被掩码。
        header_lines = [line for line in lines if line.startswith("#")]
        header = "\n".join(header_lines)
        assert secret not in header, "日志头部泄露命令中的密钥"
        assert "API_KEY=***" in header
        # 输出本身（echo 的明文）仍是完整结果——掩码只作用于命令记录
        assert secret in result.get("output", "")

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_input_masked_in_log(self, bash_server_module, bash_handler):
        """发送给进程的敏感输入（含 password）在日志中被掩码。"""
        tool = bash_server_module._get_tool()
        result = await bash_handler(
            action="execute",
            command="echo GO; read line; echo DONE",
            timeout=3,
            _owner="sess-1",
        )
        assert result["status"] == "running", result
        pid = result["pid"]
        await bash_handler(action="input", pid=pid, input_text="my-password-123", _owner="sess-1")
        # 等输入处理落盘
        time.sleep(0.5)
        log_file = tool.process_manager.log_dir / f"bash_{pid}.log"
        content = log_file.read_text(encoding="utf-8", errors="replace")
        assert "my-password-123" not in content, "敏感输入明文落盘"
        assert "# [INPUT]" in content
        # 清理
        await bash_handler(action="terminate", pid=pid, _owner="sess-1", force=True)


# ============================================================================
# 4. 大日志摘要 tail 读取
# ============================================================================

class TestTailSummary:
    """摘要压缩只读尾部窗口（评审：日志摘要可能读取整个大文件）。"""

    @_NEEDS_BASH
    @pytest.mark.asyncio
    async def test_summary_tail_window_and_full_read_log(self, bash_server_module, bash_handler):
        """>5000 行输出：get_summary 窗口封顶，read_log 完整。"""
        tool = bash_server_module._get_tool()
        result = await bash_handler(
            action="execute",
            command="seq 1 6000",
            timeout=30,
            _owner="sess-1",
        )
        assert result["status"] == "completed", result
        pid = result["pid"]
        assert result["exit_code"] == 0

        # 进程已结束被清理 → 磁盘降级路径
        file_data = tool.process_manager.read_log_by_pid(pid)
        assert file_data is not None
        # total_lines 是完整行数（seq 6000 + 可能的 WSL stderr 噪音行）
        assert file_data["total_lines"] >= 6000, file_data["total_lines"]
        # 完整输出可读（read_log 契约不变：至少含 6000 个 seq 行）
        full = await bash_handler(action="read_log", pid=pid, _owner="sess-1")
        assert full["status"] == "completed"
        assert len(full["output"].splitlines()) >= 6000
        assert full["output"].count("6000") >= 1  # seq 末行在输出中

        # 摘要窗口封顶：直接构造超过 TAIL_SUMMARY_LINES 的日志验证
        # _read_tail_lines 返回 ≤ 窗口行数
        tail_lines = tool.process_manager._read_tail_lines(
            tool.process_manager.log_dir / f"bash_{pid}.log",
            max_lines=5000,
        )
        assert len(tail_lines) <= 5000


# ============================================================================
# 5. 子进程 e2e：真实 MCP 调用方式（spawn server.py + JSON-RPC）
# ============================================================================

class _JsonRpcClient:
    """极简 JSON-RPC over stdio 客户端（测试用）。"""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 60.0) -> dict | None:
        assert self.proc.stdout is not None
        result: dict = {}

        def reader() -> None:
            line = self.proc.stdout.readline()
            result["line"] = line

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if "line" not in result or not result["line"]:
            return None
        return json.loads(result["line"])

    def call_tool(self, req_id: int, arguments: dict) -> dict | None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": "bash_execute", "arguments": arguments},
            }
        )
        resp = self.recv()
        assert resp is not None, "MCP 无响应（sidecar 可能崩溃）"
        assert "error" not in resp, f"MCP error: {resp.get('error')}"
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)


@pytest.fixture(scope="module")
def sidecar_proc():
    """spawn 真实 `python server.py`（0.2 sidecar 进程）。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SDK_DIR)
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(_PLUGIN_DIR),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    client = _JsonRpcClient(proc)
    # 握手（MCP SDK 要求 spec 形状的 initialize params——空 params 会被
    # 校验拒绝：-32602 Invalid request parameters，2026-08-23 修复）
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest-bash-sidecar", "version": "0"},
            },
        }
    )
    resp = client.recv()
    assert resp is not None and "result" in resp, "initialize 失败（sidecar 启动崩溃）"
    yield client
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@_NEEDS_BASH
def test_subprocess_e2e_lifecycle(sidecar_proc):
    """真实调用方式：MCP call #1 execute → #2 input → #3 continue → #4 terminate。

    原 bug 双验证：
    - sidecar 自包含（不再报 No module named 'src.config'）
    - 每次 tools/call 是独立 JSON-RPC 请求，状态必须跨调用保持
    """
    client = sidecar_proc

    # call #1: execute 长任务（等待 stdin 输入）
    r = client.call_tool(2, {"action": "execute", "command": "echo READY; read line; echo GOT:$line",
                             "timeout": 3, "_owner": "sess-1"})
    assert r["status"] == "running", r
    pid = r["pid"]

    # call #2: input
    r = client.call_tool(3, {"action": "input", "pid": pid, "input_text": "hello", "_owner": "sess-1"})
    assert r["status"] == "running", r

    # call #3: continue（跨调用必须命中同一进程）
    r = client.call_tool(4, {"action": "continue", "pid": pid, "timeout": 10, "_owner": "sess-1"})
    assert r["status"] == "completed", r
    assert "GOT:hello" in r.get("output", ""), r

    # call #4: terminate（进程已结束 → 报不存在；验证可正常响应不崩溃）
    r = client.call_tool(5, {"action": "terminate", "pid": pid, "_owner": "sess-1"})
    assert r.get("error_code") == "PROCESS_NOT_FOUND", r


@_NEEDS_BASH
def test_subprocess_e2e_cross_session_rejected(sidecar_proc):
    """真实调用方式下跨会话 terminate 被拒（PROCESS_FORBIDDEN）。"""
    client = sidecar_proc
    r = client.call_tool(6, {"action": "execute", "command": "sleep 30", "timeout": 2, "_owner": "sess-A"})
    assert r["status"] == "running", r
    pid = r["pid"]
    try:
        r = client.call_tool(7, {"action": "terminate", "pid": pid, "_owner": "sess-B"})
        assert "PROCESS_FORBIDDEN" in r.get("error", ""), r
    finally:
        r = client.call_tool(8, {"action": "terminate", "pid": pid, "_owner": "sess-A", "force": True})
        assert r.get("status") == "terminated", r
