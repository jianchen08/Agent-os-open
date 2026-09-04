"""godot_run 工具——Godot 编辑器执行面（godot-mcp-go serve 按需驱动）。

执行面路由（2026-09-03 用户裁定）：目标工程由当前任务 workspace 指向决定，
不读 GODOT_PROJECT_DIR（机器单值 env 已否）。解析顺序：project 参数 >
workspace 及其祖先的 project.godot > git worktree 还原主工程（编辑器开的
是主工程路径）。每个工程按需 spawn 一个
`godot-mcp-go serve --typed=false --project <dir>` 子进程（MCP over stdio，
addon WebSocket 端口经工程内发现文件自动发现），进程按工程缓存复用，
死进程下次调用自动重建。机器级 env 只剩 GODOT_MCP_BIN（二进制位置）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 共享层自举（plugins/shared/ —— 与 project_create 同模式）
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from agentos_plugin_sdk.builtin_tool import BuiltinTool  # noqa: E402
from agentos_plugin_sdk.results import ToolExecutionResult  # noqa: E402
from agentos_plugin_sdk.tool_types import (  # noqa: E402
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

_INIT_TIMEOUT_SECONDS = 20.0
_CALL_TIMEOUT_SECONDS = 90.0  # serve 自身单次调用默认 60s，客户端留余量
_AUTOSTART_WAIT_SECONDS = 120.0  # 编辑器冷启动（含首次导入工程）探活预算
_AUTOSTART_POLL_INTERVAL = 4.0

# 描述三段式：这是啥 / 命令怎么写（带实例）/ 常用命令清单。
# 纪律性内容（自动拉起细节、3D bounds 锚定、引用语义、接入自愈）在
# godot_domain_rules 与 code-godot 技能中，schema 不复读。
_GODOT_RUN_DESCRIPTION = """\
对运行中的 Godot 编辑器执行命令：建场景、写脚本、运行游戏、调试、导出。

命令写法：method = "组.命令"，params = 参数对象（可省略）。例：
- scene.tree {} —— 查看场景节点树
- node.add {"type": "Sprite2D", "name": "Player"} —— 加节点
- script.create {"path": "res://player.gd"} —— 新建脚本
- scene.play / scene.stop —— 运行 / 停止游戏
- editor.errors {} —— 看编辑器报错

常用命令：
- 场景：scene.tree、scene.open、scene.save、scene.play、scene.stop
- 节点：node.add、node.set、node.get、node.delete、node.connect
- 脚本：script.create、script.edit、script.read、script.attach
- 工程与 API：project.info、engine.search、engine.commands、engine.class_info
- 编辑器与运行：editor.errors、editor.screenshot、runtime.screenshot（需先 scene.play）

完整清单：engine.commands 按组列出；调未知 method 也返回命令清单，不必盲猜。
"""

_GODOT_RUN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "description": "<group>.<command>, e.g. node.add or engine.search",
        },
        "params": {
            "type": "object",
            "additionalProperties": True,
            "description": "command parameters",
        },
        "game": {
            "type": "boolean",
            "description": (
                "Route to a standalone debug-build game's direct server "
                "instead of the editor (requires godot_mcp/runtime/direct_server)."
            ),
        },
        "project": {
            "type": "string",
            "description": (
                "目标 Godot 工程目录（可选）。缺省按任务 workspace 自动解析："
                "workspace 或其祖先含 project.godot 即为工程；workspace 是 git "
                "worktree 时还原主工程（编辑器开的是主工程路径）。"
            ),
        },
    },
    "required": ["method"],
}

_GODOT_RUN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "命令结果对象：成功时含命令各自的数据字段；编辑器不可达时含 editor_unreachable/verdict/status.message 恢复指引",
}


def _serve_argv(bin_path: str, project_dir: Path) -> list[str]:
    """serve 子进程 argv（测试接缝：假 serve 替换本函数）。"""
    return [bin_path, "serve", "--typed=false", "--project", str(project_dir)]


class _EditorUnreachable(Exception):
    """编辑器不可达（addon 无响应）——触发自动启动流程。"""


# 本进程内已尝试拉起过的工程（配合 status 前置检查，防重复拉起）
_LAUNCHED_PROJECTS: set[str] = set()


def _editor_status(bin_path: str, project_dir: Path) -> str | None:
    """godot-mcp-go status 前置检查：running/starting/crashed/closed；未知返回 None。

    测试接缝：单测替换本函数脚本化状态序列。
    """
    try:
        completed = subprocess.run(
            [bin_path, "status", "--project", str(project_dir)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\b(running|starting|crashed|closed)\b", completed.stdout or "")
    return m.group(1) if m else None


def _launch_editor_process(argv: list[str]) -> None:
    """拉起编辑器进程（分离式，不随工具进程退出）。测试接缝：单测替换记录 argv。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _find_editor_bin() -> str:
    """定位 Godot 编辑器可执行文件：GODOT_EDITOR_BIN 优先，回退 PATH 的 godot/godot4。"""
    raw = os.environ.get("GODOT_EDITOR_BIN", "").strip()
    if raw:
        if Path(raw).is_file():
            return raw
        raise ValueError(f"GODOT_EDITOR_BIN 指向的文件不存在: {raw}")
    for name in ("godot", "godot4"):
        found = shutil.which(name)
        if found:
            return found
    raise ValueError(
        "编辑器未运行且无法自动启动：未设置 GODOT_EDITOR_BIN（Godot 可执行文件路径，"
        "见 .env.example），PATH 中也未找到 godot/godot4。设置后重试，或手动打开工程编辑器。"
    )


def _project_godot_in(dir_path: Path) -> bool:
    return (dir_path / "project.godot").is_file()


def _worktree_main_repo(dir_path: Path) -> Path | None:
    """dir_path 在 git worktree 内时还原主工程仓库根；否则 None。

    worktree 的 .git 是文件（gitdir: <主仓>/.git/worktrees/<name>），据此还原
    主仓根——编辑器打开的是主工程路径，godot_run 必须路由到主仓而非 worktree 副本。
    """
    for ancestor in [dir_path, *dir_path.parents]:
        git_marker = ancestor / ".git"
        if not git_marker.is_file():
            continue
        raw = git_marker.read_text(encoding="utf-8", errors="replace").strip()
        if not raw.startswith("gitdir:"):
            continue
        gitdir = Path(raw.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = ancestor / gitdir
        parts = gitdir.parts
        if ".git" in parts:
            dot_git_index = len(parts) - parts[::-1].index(".git")
            main_root = Path(*parts[: dot_git_index - 1])
            if main_root.is_dir():
                return main_root
        return None
    return None


def resolve_project_dir(explicit: str, workspace: str) -> Path:
    """解析目标 Godot 工程：project 参数 > workspace/祖先 project.godot >
    worktree 主工程。解析不到抛 ValueError（带可执行指引）。"""
    start = Path(explicit or workspace or "").resolve() if (explicit or workspace) else None
    if start is None or not start.is_dir():
        raise ValueError(
            "无法解析目标 Godot 工程：workspace 为空或不存在。"
            "可传 project=<工程绝对路径>，或让任务挂靠到工程工作区后再调用。"
        )
    if _project_godot_in(start):
        return start
    for ancestor in start.parents:
        if _project_godot_in(ancestor):
            return ancestor
    main_repo = _worktree_main_repo(start)
    if main_repo is not None and _project_godot_in(main_repo):
        return main_repo
    if main_repo is not None and not _project_godot_in(main_repo):
        raise ValueError(
            f"workspace 是 git worktree（主仓 {main_repo}），但主仓缺 project.godot，"
            "不是 Godot 工程。确认目标工程或显式传 project=<工程路径>。"
        )
    raise ValueError(
        f"workspace（{start}）及其祖先未发现 project.godot，未指向 Godot 工程。"
        "可传 project=<工程绝对路径>，或先用 project_create 初始化 Godot 工程。"
    )


class _ServeProxy:
    """单工程 godot-mcp-go serve 子进程代理（MCP over stdio，串行请求）。"""

    def __init__(self, bin_path: str, project_dir: Path) -> None:
        self._bin_path = bin_path
        self._project_dir = project_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._tool_name = "godot_run"

    async def _start(self) -> None:
        argv = _serve_argv(self._bin_path, self._project_dir)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self._project_dir),
        )
        await self._handshake()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_response(self, request_id: int, timeout: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"等待 serve 响应超时（id={request_id}）")
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise OSError("serve 进程 stdout 已关闭（进程可能退出）")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.debug("[godot_run] 忽略非 JSON 行: %s", line[:120])
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    async def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await self._read_response(request_id, timeout)

    async def _handshake(self) -> None:
        init = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentos-godot-run", "version": "1.0.0"},
            },
            _INIT_TIMEOUT_SECONDS,
        )
        if "error" in init:
            raise OSError(f"serve initialize 失败: {init['error']}")
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = await self._request("tools/list", {}, _INIT_TIMEOUT_SECONDS)
        names = [
            t.get("name", "")
            for t in (listing.get("result", {}).get("tools") or [])
            if isinstance(t, dict)
        ]
        if "godot_run" in names:
            self._tool_name = "godot_run"
        elif len(names) == 1:
            self._tool_name = names[0]
        else:
            raise OSError(f"serve 未暴露 godot_run 工具（实际: {names or '无'}）")

    async def _call_once(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """单次 tools/call（调用方须持锁）；编辑器不可达抛 _EditorUnreachable。"""
        if not self._alive():
            await self._start()
        assert self._proc is not None
        response = await self._request(
            "tools/call",
            {"name": self._tool_name, "arguments": arguments},
            _CALL_TIMEOUT_SECONDS,
        )
        if "error" in response:
            raise OSError(f"serve tools/call 错误: {response['error']}")
        result = response.get("result") or {}
        content = result.get("content") or []
        text = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        ).strip()
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        # 不可达判定只认结构化标记 editor_unreachable（二进制的 verdict 键名），
        # 禁用裸 "unreachable" 子串——工程路径含该词会误判（单测实证）
        unreachable = (
            isinstance(parsed, dict) and "editor_unreachable" in parsed
        ) or ("editor_unreachable" in text.lower())
        if unreachable:
            raise _EditorUnreachable(text or "editor unreachable")
        if result.get("isError"):
            raise OSError(f"godot 命令执行失败: {text or result}")
        if not text:
            return {}
        return parsed if isinstance(parsed, dict) else {"text": text}

    async def _autostart_and_wait(self, first_error: str) -> None:
        """编辑器不可达时的自动启动：status 确认 closed/崩溃恢复才拉起（严禁第二实例），
        探活等待后由调用方重试原命令。无法定位编辑器二进制抛 ValueError。"""
        editor_bin = _find_editor_bin()
        key = str(self._project_dir)
        status = _editor_status(self._bin_path, self._project_dir)
        if status in ("closed", "crashed") and key not in _LAUNCHED_PROJECTS:
            _launch_editor_process([editor_bin, "--path", str(self._project_dir), "--editor"])
            _LAUNCHED_PROJECTS.add(key)
            logger.info(
                "[godot_run] 自动拉起编辑器（status=%s）| project=%s | bin=%s",
                status,
                key,
                editor_bin,
            )
        elif status in ("running", "starting"):
            logger.info("[godot_run] 编辑器已在运行，等待其就绪 | project=%s", key)
        else:
            logger.info(
                "[godot_run] status=%s 不可判定，不拉起（防第二实例），仅探活等待 | project=%s",
                status,
                key,
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _AUTOSTART_WAIT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(_AUTOSTART_POLL_INTERVAL)
            try:
                async with self._lock:
                    await self._call_once({"method": "engine.version", "params": {}})
                return
            except (_EditorUnreachable, OSError, TimeoutError, asyncio.TimeoutError):
                continue
        raise TimeoutError(
            f"自动启动编辑器后探活超时（{_AUTOSTART_WAIT_SECONDS}s）。"
            f"首次不可达原因: {first_error}"
        )

    async def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """tools/call 透传；serve 进程死亡自动重建，编辑器未开自动拉起后重试一次。"""
        try:
            async with self._lock:
                return await self._call_once(arguments)
        except _EditorUnreachable as exc:
            await self._autostart_and_wait(str(exc))
        async with self._lock:
            try:
                return await self._call_once(arguments)
            except _EditorUnreachable as exc:
                raise OSError(f"自动启动后编辑器仍不可达: {exc}") from exc


_PROXIES: dict[tuple[str, str], _ServeProxy] = {}


def _get_proxy(bin_path: str, project_dir: Path) -> _ServeProxy:
    key = (bin_path, str(project_dir))
    proxy = _PROXIES.get(key)
    if proxy is None:
        proxy = _ServeProxy(bin_path, project_dir)
        _PROXIES[key] = proxy
    return proxy


class GodotRunTool(BuiltinTool):
    """godot_run——按 workspace 路由的 Godot 编辑器执行面。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="godot_run",
            description=_GODOT_RUN_DESCRIPTION,
            input_schema=_GODOT_RUN_INPUT_SCHEMA,
            output_schema=_GODOT_RUN_OUTPUT_SCHEMA,
            source=ToolSource.CODE,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.ALL,
            tags=["godot", "execution", "mcp"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """解析目标工程 → 复用/按需启动 serve 代理 → tools/call 透传。"""
        method = str(inputs.get("method") or "").strip()
        if not method:
            return create_failure_result(
                error="必须指定 method（'<group>.<command>'，如 node.add；不确定时先 engine.search 查证）",
                error_code="MISSING_METHOD",
            )
        bin_path = os.environ.get("GODOT_MCP_BIN", "").strip()
        if not bin_path:
            return create_failure_result(
                error="缺少环境变量 GODOT_MCP_BIN（指向 godot-mcp-go 可执行文件，见 .env.example）",
                error_code="MISSING_GODOT_MCP_BIN",
            )
        if not Path(bin_path).is_file():
            return create_failure_result(
                error=f"GODOT_MCP_BIN 指向的文件不存在: {bin_path}",
                error_code="GODOT_MCP_BIN_NOT_FOUND",
            )
        try:
            project_dir = resolve_project_dir(
                str(inputs.get("project") or ""), str(inputs.get("workspace") or "")
            )
        except ValueError as exc:
            return create_failure_result(error=str(exc), error_code="PROJECT_RESOLVE_FAILED")

        proxy = _get_proxy(bin_path, project_dir)
        arguments: dict[str, Any] = {"method": method, "params": inputs.get("params") or {}}
        if inputs.get("game"):
            arguments["game"] = True
        try:
            data = await proxy.call(arguments)
        except (ValueError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            logger.error(
                "[godot_run] 调用失败 | project=%s | method=%s | err=%s",
                project_dir,
                method,
                exc,
            )
            return create_failure_result(
                error=f"godot_run 调用失败（工程 {project_dir}）: {exc}",
                error_code="GODOT_RUN_FAILED",
            )
        logger.info(
            "[godot_run] %s | project=%s | method=%s", "ok", project_dir, method
        )
        return create_success_result(data=data)
