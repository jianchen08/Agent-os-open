# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""辅助 venv（.venv-hindsight）自愈行为测试。

装机链背景：打包 extraResources 排除一切 .venv-*，内核 venv 自愈只建 .venv
（uv sync 语义）——.venv-hindsight 在装机首启必然缺失且无人重建（9-22 手工
修复不落结构，重装即复发）。本套件覆盖插件侧自愈：

1. ``_needs_aux_provision``：venv 在位 / venv 缺 + requirements 在 / 双缺 三态；
2. ``_aux_provision_cmds``：两步命令形状（uv venv --python 3.12 + uv pip
   install --python <venv内解释器> -r requirements.txt）；
3. ``_aux_venv_python``：win 布局优先、unix 布局回退；
4. ``_provision_aux_venv``：真实 uv 走通 venv 创建；命令非零退出错误上抛；
5. ``_on_load_init``：缺 venv 时快速返回进降级（后台任务被调度、不 spawn api）；
6. ``_provision_then_connect``：供给失败保持降级留痕 / 成功后自连后端。

mock 仅限外部依赖（subprocess / 文件系统 / hindsight client）。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py（每用例新建，模块级状态不跨测试污染）。"""
    mod_name = "hindsight_memory_server_aux_provision_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def srv() -> Any:
    return _load_module()


# ---------- _needs_aux_provision 三态 ----------


def test_needs_provision_false_when_venv_present(srv: Any, tmp_path: Path) -> None:
    venv_py = tmp_path / ".venv-hindsight" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    assert not srv._needs_aux_provision(str(tmp_path))


def test_needs_provision_true_when_venv_missing_requirements_present(
    srv: Any, tmp_path: Path
) -> None:
    (tmp_path / "requirements.txt").write_text("# pinned stack\n", encoding="utf-8")
    assert srv._needs_aux_provision(str(tmp_path))


def test_needs_provision_false_when_requirements_missing(srv: Any, tmp_path: Path) -> None:
    """双缺（无 venv 也无清单）不供给——无清单的目录自愈无从谈起。"""
    assert not srv._needs_aux_provision(str(tmp_path))


# ---------- 命令形状 ----------


def test_provision_cmds_shape_and_venv_identity(srv: Any, tmp_path: Path) -> None:
    """两步命令：uv venv + uv pip install；--python 解释器必须落在被建 venv 内。"""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    cmds = srv._aux_provision_cmds(str(tmp_path))
    assert len(cmds) == 2
    venv_cmd, install_cmd = cmds
    assert venv_cmd[:3] == ["uv", "venv", str(tmp_path / ".venv-hindsight")]
    assert "--python" in venv_cmd and "3.12" in venv_cmd
    assert install_cmd[0] == "uv" and "pip" in install_cmd
    py_flag = install_cmd.index("--python")
    interp = install_cmd[py_flag + 1]
    assert ".venv-hindsight" in interp, "安装目标解释器必须是被建 venv 内的 python"
    assert "-r" in install_cmd


# ---------- 解释器探测双布局 ----------


def test_aux_venv_python_prefers_win_layout(srv: Any, tmp_path: Path) -> None:
    scripts = tmp_path / ".venv-hindsight" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    assert srv._aux_venv_python(str(tmp_path)).endswith("python.exe")


def test_aux_venv_python_falls_back_to_unix_layout(srv: Any, tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv-hindsight" / "bin"
    venv_bin.mkdir(parents=True)
    assert srv._aux_venv_python(str(tmp_path)).replace("\\", "/").endswith(
        ".venv-hindsight/bin/python"
    )


# ---------- 真实 uv 的执行面 ----------


def test_provision_aux_venv_real_uv_creates_venv(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键路径真实依赖：真实 uv venv 建出解释器（uv 缺席显式跳过，同内核测试口径）。"""
    if shutil.which("uv") is None:
        pytest.skip("uv 不在 PATH")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    venv_dir = tmp_path / ".venv-hindsight"
    monkeypatch.setattr(
        srv, "_aux_provision_cmds", lambda d: [["uv", "venv", str(venv_dir)]]
    )
    _run(srv._provision_aux_venv(str(tmp_path)))
    py = srv._aux_venv_python(str(tmp_path))
    assert Path(py).is_file(), f"真实 uv venv 后解释器应就位: {py}"


def test_provision_aux_venv_nonzero_exit_raises_with_stderr(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令非零退出 → RuntimeError 带退出码（stderr tail 附带）。"""
    marker_cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    monkeypatch.setattr(srv, "_aux_provision_cmds", lambda d: [marker_cmd])
    with pytest.raises(RuntimeError, match="exit 3"):
        _run(srv._provision_aux_venv(str(tmp_path)))


# ---------- on_load 分派 ----------


async def _noop_async(*a: Any, **k: Any) -> None:
    return None


def test_on_load_init_defers_to_background_provision(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺辅助 venv：on_load 不 spawn api、快速返回，自愈任务被调度。"""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(srv, "_THIS_DIR", str(tmp_path))
    monkeypatch.setattr(srv.plugin, "get_config", lambda: {"data_dir": str(tmp_path / "data")})
    scheduled = AsyncMock()
    monkeypatch.setattr(srv, "_provision_then_connect", scheduled)

    import asyncio

    async def drive() -> None:
        await srv._on_load_init()
        # 让被调度任务有机会执行（FastAPI 事件循环外的裸 loop：手动 tick）
        await asyncio.sleep(0)

    _run(drive())
    assert srv._client is None
    scheduled.assert_awaited_once()
    assert scheduled.await_args is not None
    assert scheduled.await_args.args[0] == str(tmp_path)


def test_provision_failure_keeps_degraded(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """供给失败：warn 留痕、_client 保持 None（诚实降级，不假成功）。"""
    monkeypatch.setattr(
        srv, "_provision_aux_venv", AsyncMock(side_effect=RuntimeError("uv 缺席"))
    )
    with caplog.at_level(logging.WARNING):
        _run(srv._provision_then_connect(str(tmp_path), "8420", str(tmp_path / "data")))
    assert srv._client is None
    assert any("自愈失败" in r.getMessage() for r in caplog.records)


def test_provision_success_connects_backend(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """供给成功即自连后端（_connect_backend 被以原 port/data_dir 调用）。"""
    monkeypatch.setattr(srv, "_provision_aux_venv", AsyncMock())
    connect = AsyncMock()
    monkeypatch.setattr(srv, "_connect_backend", connect)
    _run(srv._provision_then_connect(str(tmp_path), "8420", str(tmp_path / "data")))
    connect.assert_awaited_once_with("8420", str(tmp_path / "data"))


def test_connect_failure_after_provision_degrades(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """自愈后连接仍失败（api 起不来等）：降级留痕不崩。"""
    monkeypatch.setattr(srv, "_provision_aux_venv", AsyncMock())
    monkeypatch.setattr(srv, "_connect_backend", AsyncMock(side_effect=RuntimeError("spawn 灭")))
    with caplog.at_level(logging.WARNING):
        _run(srv._provision_then_connect(str(tmp_path), "8420", str(tmp_path / "data")))
    assert srv._client is None
    assert any("自愈后连接失败" in r.getMessage() for r in caplog.records)


# ---------- 补扫残余分支（diff-cov 100% 执法） ----------


def test_provision_aux_venv_uv_missing_raises(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 不在 PATH：可读错误上抛（同内核 autoprovision 前提缺失的降级口径）。"""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="uv 不在 PATH"):
        _run(srv._provision_aux_venv(str(tmp_path)))


def test_provision_aux_venv_timeout_kills_and_raises(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令挂死：按宽上限超时 kill 并上抛（上限注入为 0.2s，不真等 900s）。"""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "C:/fake/uv.exe")
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(srv, "_aux_provision_cmds", lambda d: [hang_cmd])
    monkeypatch.setattr(srv, "_AUX_PROVISION_TIMEOUT_S", 0.2)
    with pytest.raises(RuntimeError, match="供给超时"):
        _run(srv._provision_aux_venv(str(tmp_path)))


def test_connect_backend_spawns_when_api_down(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_connect_backend 的 spawn 路径：api 不在位 → 起 api + 建客户端 + 建 bank。"""
    proc = SimpleNamespace(pid=4242)
    spawned: dict[str, Any] = {}

    def fake_start(port: int, data_dir: str) -> tuple[Any, str]:
        spawned["port"] = port
        spawned["data_dir"] = data_dir
        return proc, str(tmp_path / "stderr.log")

    monkeypatch.setattr(srv, "_hindsight_api_up", lambda base_url: False)
    monkeypatch.setattr(srv, "_start_api_server", fake_start)
    monkeypatch.setattr(srv, "_wait_api_ready", AsyncMock())

    fake_client = MagicMock()
    fake_client.acreate_bank = AsyncMock()
    fake_module = SimpleNamespace(Hindsight=lambda base_url: fake_client)
    monkeypatch.setitem(sys.modules, "hindsight_client", fake_module)

    data_dir = str(tmp_path / "data")
    _run(srv._connect_backend("8421", data_dir))

    assert spawned == {"port": 8421, "data_dir": data_dir}
    srv._wait_api_ready.assert_awaited_once()
    assert srv._client is fake_client
    fake_client.acreate_bank.assert_awaited_once()
