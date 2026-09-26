# @feature: FP-0.2.二 原生隔离后端 wsl_native 缺口补测 | @ci: python-coverage
"""wsl_native_provider.py 缺口补测（HEAD coverage.xml 缺行）。

靶行与语义（行号经源码逐行确认）：
- 51（``_SHARED_ROOT`` 显式入 sys.path）、143（发行版列表查询失败回显 stderr）、
  159（发行版缺 bash）；
- 238（metadata 档写入失败 → ERROR env，不落 READY）；
- 317-319（``get_environment_status``：有登记 READY / 无登记 STOPPED）；
- 337-340（``_exec`` 的 TimeoutError 与泛 Exception 两条错误归一）；
- 367/373-397（``_file_op`` 的 read 失败、write 成功与失败路径）、406-408
  （不支持的操作 + 文件操作异常兜底）；
- 479/482（``_bridge_env`` 的 URL / TOKEN 两键存在性）；
- 527（``_decode_wsl_list`` 空输入 → 空列表）；
- 579-581（``_write_metadata`` 的 OSError → False 且不抛）。

打桩边界：wsl.exe 属外部命令面（``_popen_run`` 用记录型替身，
shutil.which 替身）；metadata 与 workspace 走真实 tmp_path 文件系统。
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationLevel,
    OperationType,
    TaskType,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
_SHARED_ROOT = _PLUGIN_DIR.parents[1]  # plugins/shared

for _p in (str(_PLUGIN_DIR), str(_SHARED_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_mod() -> Any:
    """动态加载 providers/wsl_native_provider.py（唯一模块名，防裸名串扰）。"""
    mod_name = "isolation_wsl_native_gaps_ut"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _PLUGIN_DIR / "providers" / "wsl_native_provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
WslNativeProvider = _MOD.WslNativeProvider


class _Recorder:
    """``_popen_run`` 记录型替身：记调用参数，按脚本或异常回放。"""

    def __init__(self, script: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._script = script or (lambda args: (0, b"", b""))
        self._exc = exc

    async def __call__(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        self.calls.append(list(args))
        if self._exc is not None:
            raise self._exc
        return self._script(args)


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(workspace: str | None) -> IsolationContext:
    return IsolationContext(
        task_id="t-gap",
        task_type=TaskType.ATOMIC,
        operation_type=OperationType.CODE_EXECUTION,
        workspace=workspace,
        isolation_level=IsolationLevel.CONTAINER,
    )


def _make_provider(tmp_path: Path, **overrides: Any) -> Any:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "distro": "Ubuntu",
        "state_dir": str(tmp_path / "envs"),
        "wsl_exe": "wsl",
    }
    config.update(overrides)
    provider = WslNativeProvider(config)
    provider._popen_run = _Recorder()
    provider._workspace = str(ws)
    return provider


def _metadata_path(provider: Any, name: str) -> Path:
    return Path(provider._state_dir) / f"{name}.json"


# ═══════════════ 模块自举与可用性探测 ═══════════════


class TestModuleBootstrap:
    def test_fresh_import_reinserts_shared_root(self) -> None:
        """共享根不在 sys.path 时首次装载会自持插入（51 行）——proc_tree 等
        共享裸模块的解析前提；本用例显式制造「未注入」初态，与收集顺序无关。
        """
        root = str(_SHARED_ROOT)
        removed: list[int] = []
        while root in sys.path:
            removed.append(sys.path.index(root))
            sys.path.remove(root)
        spec = importlib.util.spec_from_file_location(
            "isolation_wsl_native_bootstrap_ut",
            _PLUGIN_DIR / "providers" / "wsl_native_provider.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["isolation_wsl_native_bootstrap_ut"] = mod
        try:
            spec.loader.exec_module(mod)
            # 自举的副作用：共享根被重新插回，且共享裸模块可解析
            assert root in sys.path, "装载必须把共享根重新入列"
            assert mod.kill_process_tree is not None
            import proc_tree

            assert (
                Path(proc_tree.__file__).resolve().parent == Path(root).resolve()
            ), "proc_tree 必须解析自共享根（不是同名的其它副本）"
        finally:
            sys.modules.pop("isolation_wsl_native_bootstrap_ut", None)
            if root not in sys.path:
                sys.path.insert(0, root)

    def test_shared_root_on_sys_path(self) -> None:
        """共享根在 sys.path 上（proc_tree 等共享裸模块的解析前提）。"""
        assert str(_SHARED_ROOT) in sys.path

    def test_proc_tree_symbol_resolved_from_shared_root(self) -> None:
        """共享裸模块真实解析成功（自举不是摆设）。"""
        assert _MOD.kill_process_tree is not None
        import proc_tree

        assert Path(proc_tree.__file__).resolve().parent == Path(str(_SHARED_ROOT)).resolve()


class TestIsAvailableRemainingBranches:
    """``is_available`` 的两条未覆盖失败环（143 / 159）。"""

    def test_distro_list_query_failure_reports_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """wsl -l -q 非零退出 → (False, 原因含 stderr 原文)（143）。"""
        import shutil

        provider = _make_provider(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"wsl: 0x80370102"))

        ok, reason = _run(provider.is_available())

        assert ok is False
        assert reason is not None
        assert "发行版列表查询失败" in reason
        assert "0x80370102" in reason, "原因必须带原始 stderr 供排障"

    def test_query_failure_falls_back_to_stdout_when_stderr_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """stderr 为空时回显 stdout（两条输出面都不可读才给空原因）。"""
        import shutil

        provider = _make_provider(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        provider._popen_run = _Recorder(script=lambda args: (1, b"out-side", b""))

        ok, reason = _run(provider.is_available())

        assert ok is False
        assert "out-side" in (reason or "")

    def test_missing_bash_reported_as_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """发行版在位但缺 bash → (False, 执行壳依赖)（159）。"""
        import shutil

        provider = _make_provider(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")

        def _script(args: list[str]) -> tuple[int, bytes, bytes]:
            if args[1:3] == ["-l", "-q"]:
                return (0, b"Ubuntu\n", b"")
            if "command -v bash" in " ".join(args):
                return (1, b"", b"")
            return (0, b"", b"")

        provider._popen_run = _Recorder(script=_script)

        ok, reason = _run(provider.is_available())

        assert ok is False
        assert "缺少 bash" in (reason or "")

    def test_all_rings_ok_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：各环通过 → (True, None)（上面不是恒假）。"""
        import shutil

        provider = _make_provider(tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        provider._popen_run = _Recorder(script=lambda args: (0, b"Ubuntu\n", b""))

        assert _run(provider.is_available()) == (True, None)


# ═══════════════ create_environment 的 metadata 写失败 ═══════════════


class TestCreateMetadataWriteFailure:
    def test_write_failure_returns_error_env_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """metadata 档写入失败 → ERROR env（238）：无档=底层无环境，不得谎报 READY。"""
        provider = _make_provider(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (0, b"", b""))

        def _boom(self: Path, *a: Any, **k: Any) -> int:
            raise OSError("disk on fire")

        monkeypatch.setattr(Path, "write_text", _boom)

        env = _run(provider.create_environment(_ctx(provider._workspace), "cua-ws1"))

        assert env.status == EnvironmentStatus.ERROR.value
        assert "metadata 档写入失败" in str(env.provider_info.get("error"))
        assert "cua-ws1" not in provider._environments, "失败不得登记为可用环境"


# ═══════════════ get_environment_status 两态 ═══════════════


class TestEnvironmentStatus:
    def test_registered_env_is_ready(self, tmp_path: Path) -> None:
        """有登记 → READY（317-318）：wsl_native 无运行态，登记即就绪。"""
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(provider._workspace), "cua-ws1"))

        assert _run(provider.get_environment_status(env.env_id)) == EnvironmentStatus.READY

    def test_unknown_env_is_stopped(self, tmp_path: Path) -> None:
        """无登记 → STOPPED（319），不是 ERROR（环境不存在≠环境坏）。"""
        provider = _make_provider(tmp_path)

        assert _run(provider.get_environment_status("ghost")) == EnvironmentStatus.STOPPED


# ═══════════════ _exec 的两条错误归一 ═══════════════


class TestExecErrorNormalization:
    def _ready(self, tmp_path: Path) -> tuple[Any, str]:
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(provider._workspace), "cua-ws1"))
        return provider, env.env_id

    def test_command_timeout_maps_to_error_result(
        self, tmp_path: Path,
    ) -> None:
        """命令执行超时（TimeoutError）→ success=False 带超时原文（337-338）。"""
        provider, env_id = self._ready(tmp_path)
        provider._popen_run = _Recorder(exc=TimeoutError("命令执行超时（>5s），已终止进程树"))

        result = _run(
            provider.execute_in_environment(
                env_id, {"type": "command", "command": "sleep 100", "timeout": 5}
            )
        )

        assert result.success is False
        assert "超时" in (result.error or "")

    def test_command_unexpected_exception_maps_to_error_result(
        self, tmp_path: Path,
    ) -> None:
        """其他异常（OSError 等）→ success=False 带「执行命令失败」前缀（339-340）。"""
        provider, env_id = self._ready(tmp_path)
        provider._popen_run = _Recorder(exc=OSError("wsl.exe 无法启动"))

        result = _run(
            provider.execute_in_environment(env_id, {"type": "command", "command": "ls"})
        )

        assert result.success is False
        assert "执行命令失败" in (result.error or "")
        assert "wsl.exe 无法启动" in (result.error or "")


# ═══════════════ _file_op 的 read/write/兜底 ═══════════════


class TestFileOpBranches:
    def _ready(self, tmp_path: Path) -> tuple[Any, str]:
        provider = _make_provider(tmp_path)
        env = _run(provider.create_environment(_ctx(provider._workspace), "cua-ws1"))
        return provider, env.env_id

    def test_read_failure_reports_stderr(self, tmp_path: Path) -> None:
        """read 非零退出 → 读取失败 + stderr（367-369）。"""
        provider, env_id = self._ready(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"cat: no such file"))

        result = _run(
            provider.execute_in_environment(
                env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/x"}
            )
        )

        assert result.success is False
        assert "读取失败" in (result.error or "")
        assert "no such file" in (result.error or "")

    def test_write_success_uses_json_payload_and_reports_ok(self, tmp_path: Path) -> None:
        """write 成功 → success 且 output 为 None；内容经 json 编码传参（373-397）。"""
        provider, env_id = self._ready(tmp_path)
        recorder = _Recorder(script=lambda args: (0, b"", b""))
        provider._popen_run = recorder

        result = _run(
            provider.execute_in_environment(
                env_id,
                {
                    "type": "file_operation",
                    "operation": "write",
                    "path": "/workspace/sub/a.txt",
                    "content": "line1\nline2",
                },
            )
        )

        assert result.success is True
        assert result.output is None
        argv = recorder.calls[-1]
        # 目录段由映射后的路径 rsplit 派生；内容经 json.dumps 编码（多行安全）
        dir_arg, file_arg, content_arg = argv[-3], argv[-2], argv[-1]
        assert dir_arg == WslNativeProvider._map_working_dir(
            provider, "/workspace/sub"
        ), "目录段应为主目录映射结果"
        assert file_arg.endswith("sub/a.txt") or file_arg.endswith("sub\\a.txt")
        assert content_arg == json.dumps("line1\nline2"), "多行内容经 json 编码传参"

    def test_write_failure_reports_stderr(self, tmp_path: Path) -> None:
        """write 非零退出 → 写入失败 + stderr（393-396）。"""
        provider, env_id = self._ready(tmp_path)
        provider._popen_run = _Recorder(script=lambda args: (1, b"", b"disk full"))

        result = _run(
            provider.execute_in_environment(
                env_id,
                {"type": "file_operation", "operation": "write", "path": "/workspace/a", "content": "x"},
            )
        )

        assert result.success is False
        assert "写入失败" in (result.error or "")
        assert "disk full" in (result.error or "")

    def test_unsupported_file_operation_rejected(self, tmp_path: Path) -> None:
        """未知 file_operation（chmod）→ 显式拒绝（406）。"""
        provider, env_id = self._ready(tmp_path)

        result = _run(
            provider.execute_in_environment(
                env_id, {"type": "file_operation", "operation": "chmod", "path": "/workspace/a"}
            )
        )

        assert result.success is False
        assert "不支持的文件操作" in (result.error or "")
        assert "chmod" in (result.error or "")

    def test_file_op_exception_wrapped_with_prefix(self, tmp_path: Path) -> None:
        """_popen_run 抛异常 → 「文件操作失败」前缀归一（407-408）。"""
        provider, env_id = self._ready(tmp_path)
        provider._popen_run = _Recorder(exc=OSError("wsl 通路断了"))

        result = _run(
            provider.execute_in_environment(
                env_id, {"type": "file_operation", "operation": "read", "path": "/workspace/a"}
            )
        )

        assert result.success is False
        assert "文件操作失败" in (result.error or "")
        assert "wsl 通路断了" in (result.error or "")

    def test_write_path_without_slash_uses_dot_dir(self, tmp_path: Path) -> None:
        """相对路径（无 /）→ 目录段取 "."（rsplit 兜底分支）。"""
        provider, env_id = self._ready(tmp_path)
        recorder = _Recorder(script=lambda args: (0, b"", b""))
        provider._popen_run = recorder

        result = _run(
            provider.execute_in_environment(
                env_id,
                {"type": "file_operation", "operation": "write", "path": "bare.txt", "content": "x"},
            )
        )

        assert result.success is True
        argv = recorder.calls[-1]
        idx = argv.index("--")
        assert argv[idx + 1] == ".", "无斜杠路径的目录段应为当前目录"
        assert argv[idx + 2] == "bare.txt"


# ═══════════════ _bridge_env 两键 ═══════════════


class TestBridgeEnvKeys:
    def test_url_from_config_wins(self, tmp_path: Path) -> None:
        """显式 bridge_url 优先于环境变量（479）。"""
        provider = _make_provider(tmp_path, bridge_url="http://bridge.local:9")
        env = provider._bridge_env()

        assert env["AGENTOS_BRIDGE_URL"] == "http://bridge.local:9"

    def test_url_and_token_read_from_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """无显式配置时从环境变量读 URL 与 TOKEN（479/482 两键都注入）。"""
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://env-bridge:8")
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "tok-123")
        provider = _make_provider(tmp_path)

        env = provider._bridge_env()

        assert env["AGENTOS_BRIDGE_URL"] == "http://env-bridge:8"
        assert env["AGENTOS_BRIDGE_TOKEN"] == "tok-123"

    def test_absent_values_omit_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """两者皆缺 → 空字典（不写空串占位，避免子进程拿到空 URL）。"""
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        provider = _make_provider(tmp_path)

        assert provider._bridge_env() == {}


# ═══════════════ _decode_wsl_list 空输入 ═══════════════


class TestDecodeWslListEmpty:
    def test_none_and_empty_bytes_return_empty_list(self) -> None:
        """None / 空 bytes → 空列表（527），不产生 [''] 假条目。"""
        assert WslNativeProvider._decode_wsl_list(None) == []
        assert WslNativeProvider._decode_wsl_list(b"") == []

    def test_whitespace_only_output_yields_no_distros(self) -> None:
        """仅空白的输出同样不产生发行版条目（strip 后过滤）。"""
        assert WslNativeProvider._decode_wsl_list(b"\r\n\n  \n") == []


# ═══════════════ _write_metadata 的 OSError ═══════════════


class TestWriteMetadataFailure:
    def test_oserror_returns_false_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """写档 OSError → False 且不抛（579-581）：调用方据此转 ERROR env。"""
        from datetime import datetime, timezone

        provider = _make_provider(tmp_path)

        def _boom(self: Path, *a: Any, **k: Any) -> int:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "write_text", _boom)

        ok = provider._write_metadata(
            "cua-x", "/host/ws", "/mnt/c/ws", datetime.now(UTC)
        )

        assert ok is False

    def test_success_writes_round_trippable_document(self, tmp_path: Path) -> None:
        """对照组：写入成功 → True 且档内容可反读为同值（不是恒假）。"""
        from datetime import datetime, timezone

        provider = _make_provider(tmp_path)
        now = datetime.now(UTC)

        ok = provider._write_metadata("cua-ok", "/host/ws", "/mnt/c/ws", now)

        assert ok is True
        stored = json.loads(_metadata_path(provider, "cua-ok").read_text(encoding="utf-8"))
        assert stored["name"] == "cua-ok"
        assert stored["workspace"] == "/host/ws"
        assert stored["workspace_wsl"] == "/mnt/c/ws"
        assert stored["created_at"] == now.isoformat()
