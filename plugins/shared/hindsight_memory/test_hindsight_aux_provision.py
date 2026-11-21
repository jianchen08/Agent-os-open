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

_SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
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


@pytest.fixture(autouse=True)
def _isolate_user_space(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离真实用户空间：默认无登记处（各用例自行 monkeypatch 需要的落点）。

    登记处落点来自 AGENTOS_USER_ROOT / OS data dir——不隔离则断言面依赖开发机
    是否装过 hindsight aux venv（外部状态不得进断言面）。
    """
    monkeypatch.setattr(srv, "_user_space_path", lambda *_parts: None)


def _make_healthy_venv(root: Path) -> Path:
    """造「解释器+hindsight_api 包双在位」的健康 venv 根，返回解释器路径。"""
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    pkg = root / "Lib" / "site-packages" / "hindsight_api"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    return scripts / "python.exe"


# ---------- _needs_aux_provision 判活（可用性=解释器+包双在位） ----------


def _make_venv(tmp_path: Path, *, skeleton: bool = False) -> None:
    """搭 win 布局 fake venv：healthy=解释器+hindsight_api 包；skeleton=仅解释器。"""
    venv_py = tmp_path / ".venv-hindsight" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    if not skeleton:
        pkg = tmp_path / ".venv-hindsight" / "Lib" / "site-packages" / "hindsight_api"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")


def test_needs_provision_false_when_venv_healthy(srv: Any, tmp_path: Path) -> None:
    _make_venv(tmp_path)
    assert not srv._needs_aux_provision(str(tmp_path))


def test_needs_provision_true_when_venv_is_skeleton(srv: Any, tmp_path: Path) -> None:
    """回归（装机 2026-10-21 14:33 实证）：同步残骸骨架（解释器在、site-packages
    全失）曾骗过 isfile 判活 → spawn 秒死 ModuleNotFoundError 进永久降级、自愈
    永不触发。骨架必须判为需供给。"""
    _make_venv(tmp_path, skeleton=True)
    (tmp_path / "requirements.txt").write_text("# pinned stack\n", encoding="utf-8")
    assert srv._needs_aux_provision(str(tmp_path))
    assert not srv._aux_venv_usable(str(tmp_path))


def test_aux_venv_usable_tristate(srv: Any, tmp_path: Path) -> None:
    _make_venv(tmp_path)
    assert srv._aux_venv_usable(str(tmp_path))
    assert not srv._aux_venv_usable(str(tmp_path / "nonexistent"))


def test_needs_provision_true_when_venv_missing_requirements_present(srv: Any, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("# pinned stack\n", encoding="utf-8")
    assert srv._needs_aux_provision(str(tmp_path))


def test_needs_provision_false_when_requirements_missing(srv: Any, tmp_path: Path) -> None:
    """双缺（无 venv 也无清单）不供给——无清单的目录自愈无从谈起。"""
    assert not srv._needs_aux_provision(str(tmp_path))


# ---------- 命令形状 ----------


def test_provision_cmds_shape_and_venv_identity(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两步命令：uv venv --clear + uv pip install；--python 解释器必须落在被建 venv 内。

    --clear 契约：uv venv 对已存在目录默认拒绝——残骸骨架重建（与判活修复
    配套）必需；对缺失目录 clear 为无害 no-op。
    """
    monkeypatch.setattr(srv, "_uv_executable", lambda: "uv")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    cmds = srv._aux_provision_cmds(str(tmp_path))
    assert len(cmds) == 2
    venv_cmd, install_cmd = cmds
    assert venv_cmd[:3] == ["uv", "venv", str(tmp_path / ".venv-hindsight")]
    assert "--python" in venv_cmd and "3.12" in venv_cmd
    assert "--clear" in venv_cmd, "uv venv 对已存在骨架默认拒绝，重建必须带 --clear"
    assert install_cmd[0] == "uv" and "pip" in install_cmd
    py_flag = install_cmd.index("--python")
    interp = install_cmd[py_flag + 1]
    assert Path(interp).parent.parent == tmp_path / ".venv-hindsight", "安装目标解释器必须落在被建 venv 内"
    assert "-r" in install_cmd


# ---------- 登记处落位（根治：装机同步打不到 aux venv）----------


def test_aux_provision_lands_in_user_space_registry(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """供给落点=用户空间登记处：装机同步镜像插件目录打不到它（R2 事故根因）。

    登记键与内核 aux_venv_registry_key 同源：<插件 id>--<目录名剥前导点>。
    """
    user_root = tmp_path / "usroot"
    monkeypatch.setattr(srv, "_user_space_path", lambda *parts: str(user_root.joinpath(*parts)))
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    cmds = srv._aux_provision_cmds(str(tmp_path))
    target = user_root / "plugin-venvs" / "hindsight_memory_service--venv-hindsight"
    assert cmds[0][2] == str(target), "供给落点必须是用户空间登记处"
    assert target.parent.name == "plugin-venvs", "落点须在用户空间登记处目录内"
    assert target.name == "hindsight_memory_service--venv-hindsight", "登记键须与内核 aux_venv_registry_key 同源"
    assert target != tmp_path / ".venv-hindsight", "落点不得是插件目录内原地（装机同步会打回）"


def test_aux_provision_install_target_points_into_new_venv(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安装目标解释器按被建 venv 的平台布局直推（供给期 venv 尚不存在，不能探测）。"""
    user_root = tmp_path / "usroot"
    monkeypatch.setattr(srv, "_user_space_path", lambda *parts: str(user_root.joinpath(*parts)))
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    install_cmd = srv._aux_provision_cmds(str(tmp_path))[1]
    interp = install_cmd[install_cmd.index("--python") + 1]
    target = user_root / "plugin-venvs" / "hindsight_memory_service--venv-hindsight"
    assert Path(interp).parent.parent == target
    # 布局按平台直推（win=Scripts/python.exe、unix=bin/python）：断言走 Path
    # 组件比较，避开 os.path.join 的平台分隔符差异（字面量 endswith 平台相关）。
    assert Path(interp).parent.name == ("Scripts" if sys.platform == "win32" else "bin")
    assert Path(interp).name == ("python.exe" if sys.platform == "win32" else "python")


def test_aux_venv_python_prefers_usable_registry_over_skeleton(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插件目录内只剩骨架（同步打回）时，解析必须落到登记处健康 venv。

    R2/R236 同型复发面：骨架解释器（解释器在、site-packages 全失）曾遮蔽
    登记处那份健康 venv → spawn 秒死 ModuleNotFoundError。
    """
    user_root = tmp_path / "usroot"
    registry = user_root / "plugin-venvs" / "hindsight_memory_service--venv-hindsight"
    healthy = _make_healthy_venv(registry)
    monkeypatch.setattr(srv, "_user_space_path", lambda *parts: str(user_root.joinpath(*parts)))
    scripts = tmp_path / ".venv-hindsight" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    assert srv._aux_venv_python(str(tmp_path)) == str(healthy), "骨架不得遮蔽登记处健康 venv"


def test_aux_provision_fallback_cmds_use_stdlib_venv_and_pip(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 缺席降级命令形状：标准库 venv + pip，落点与 uv 路径一致。"""
    user_root = tmp_path / "usroot"
    monkeypatch.setattr(srv, "_user_space_path", lambda *parts: str(user_root.joinpath(*parts)))
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    cmds = srv._aux_fallback_provision_cmds(str(tmp_path))
    assert len(cmds) == 2
    assert cmds[0][1:3] == ["-m", "venv"], "降级建环境走标准库 venv"
    assert "--clear" in cmds[0], "残骸骨架重建语义与 uv 路径一致"
    assert cmds[1][1:3] == ["-m", "pip"], "降级装依赖走 pip"
    assert "-r" in cmds[1]
    target = user_root / "plugin-venvs" / "hindsight_memory_service--venv-hindsight"
    assert cmds[0][-1] == str(target), "降级路径落点与 uv 路径一致"


def test_aux_provision_plan_falls_back_when_uv_absent(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 缺席时供给计划走标准库降级路径（不得直接放弃自愈）。

    探测缝打在 _uv_executable（而非 shutil.which）：宿主是否装了 uv 是开发机状态，
    断言面不得依赖它。
    """
    monkeypatch.setattr(srv, "_uv_executable", lambda: None)
    cmds = srv._aux_provision_plan(str(tmp_path))
    assert cmds[0][1:3] == ["-m", "venv"], "uv 缺席须走标准库 venv 降级路径"


# ---------- 解释器探测双布局 ----------


def test_aux_venv_python_prefers_win_layout(srv: Any, tmp_path: Path) -> None:
    scripts = tmp_path / ".venv-hindsight" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    assert srv._aux_venv_python(str(tmp_path)).endswith("python.exe")


def test_aux_venv_python_falls_back_to_unix_layout(srv: Any, tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv-hindsight" / "bin"
    venv_bin.mkdir(parents=True)
    assert srv._aux_venv_python(str(tmp_path)).replace("\\", "/").endswith(".venv-hindsight/bin/python")


def test_aux_venv_python_falls_back_to_user_space_registry(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插件目录缺 venv（装机只读重定向形态）→ 用户空间登记处按内核同键回读。

    键 = plugin-venvs/<插件 id>--<目录名剥前导点>，与内核
    select_missing_aux_venvs 的登记键同源（ADR 2026-09-28-aux-venv-autoprovision）。
    """
    user_root = tmp_path / "usroot"
    key_dir = user_root / "plugin-venvs" / "hindsight_memory_service--venv-hindsight"
    scripts = key_dir / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(srv, "_user_space_path", lambda *parts: str(user_root.joinpath(*parts)))

    resolved = srv._aux_venv_python(str(tmp_path / "plugin_dir"))

    assert resolved == str(scripts / "python.exe"), "登记处解释器必须按内核同键命中"


# ---------- 真实 uv 的执行面 ----------


def test_provision_aux_venv_real_uv_creates_venv(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """关键路径真实依赖：真实 uv venv 建出解释器（uv 缺席显式跳过，同内核测试口径）。"""
    if shutil.which("uv") is None:
        pytest.skip("uv 不在 PATH")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    venv_dir = tmp_path / ".venv-hindsight"
    monkeypatch.setattr(srv, "_aux_provision_plan", lambda d: [["uv", "venv", str(venv_dir)]])
    _run(srv._provision_aux_venv(str(tmp_path)))
    py = srv._aux_venv_python(str(tmp_path))
    assert Path(py).is_file(), f"真实 uv venv 后解释器应就位: {py}"


def test_provision_aux_venv_nonzero_exit_raises_with_stderr(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令非零退出 → RuntimeError 带退出码（stderr tail 附带）。"""
    marker_cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    monkeypatch.setattr(srv, "_aux_provision_plan", lambda d: [marker_cmd])
    with pytest.raises(RuntimeError, match="exit 3"):
        _run(srv._provision_aux_venv(str(tmp_path)))


# ---------- on_load 分派 ----------


async def _noop_async(*a: Any, **k: Any) -> None:
    return None


def test_on_load_init_defers_to_background_provision(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺辅助 venv：on_load 不 spawn api、快速返回，自愈任务被调度。"""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(srv, "_THIS_DIR", str(tmp_path))
    monkeypatch.setattr(srv.plugin, "get_config", lambda: {"data_dir": str(tmp_path / "data")})
    scheduled = AsyncMock()
    monkeypatch.setattr(srv, "_provision_then_connect", scheduled)

    import asyncio

    async def drive() -> None:
        await srv._on_load_init(asyncio.Event())  # 供给分支：事件所有权不移交（返回 False）
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
    monkeypatch.setattr(srv, "_provision_aux_venv", AsyncMock(side_effect=RuntimeError("uv 缺席")))
    with caplog.at_level(logging.WARNING):
        _run(srv._provision_then_connect(str(tmp_path), "8420", str(tmp_path / "data")))
    assert srv._client is None
    assert any("自愈失败" in r.getMessage() for r in caplog.records)


def test_provision_success_connects_backend(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_provision_aux_venv_uv_missing_falls_back(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 不在 PATH：走降级重建路径（标准库 venv+pip），不再直接放弃自愈。

    R2 装机实证：electron 拉起链无 uv 时旧实现直接 raise → on_load 永久降级、
    记忆功能全废。降级语义=功能完整性优先：供给面与 uv 路径等价（仅解析器不同）。
    """
    def _boom(_d: str) -> list[list[str]]:
        raise AssertionError("uv 缺席时不得走 uv 供给路径")

    monkeypatch.setattr(srv, "_uv_executable", lambda: None)
    monkeypatch.setattr(srv, "_aux_provision_cmds", _boom)
    marker_cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    monkeypatch.setattr(srv, "_aux_fallback_provision_cmds", lambda d: [marker_cmd])
    with pytest.raises(RuntimeError, match="exit 3"):
        _run(srv._provision_aux_venv(str(tmp_path)))


def test_provision_aux_venv_timeout_kills_and_raises(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令挂死：按宽上限超时 kill 并上抛（上限注入为 0.2s，不真等 900s）。"""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "C:/fake/uv.exe")
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(srv, "_aux_provision_plan", lambda d: [hang_cmd])
    monkeypatch.setattr(srv, "_AUX_PROVISION_TIMEOUT_S", 0.2)
    with pytest.raises(RuntimeError, match="供给超时"):
        _run(srv._provision_aux_venv(str(tmp_path)))


def test_connect_backend_spawns_when_api_down(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ---------- 端口清障探测（装机 2026-10-13 码页回归锚 + 2026-10-20 wmic 退役） ----------
#
# 中文 Windows 上 netstat 输出走 OEM 码页（GBK）；字节捕获 + 宽松解码前，
# text=True 严格 UTF-8 解码在 reader 线程崩掉 → stdout=None → splitlines
# AttributeError → _start_api_server 掐死在 _pre_clean_port → api 永不 spawn，
# 记忆服务永久降级（真错误又被 output_schema 掩蔽为 schema 错）。
# 命令行查询不走子进程：wmic 在 Win11 22H2+ 默认移除（缺席→空表→孤儿判定
# 恒 False→清障自愈失效，2026-10-20 普查 🔴-1），改 psutil 单点双平台。


def _completed(bytes_stdout: bytes | None) -> Any:
    # None = stdout 为空的事故形态（解码线程崩溃），测试契约显式覆盖
    return SimpleNamespace(stdout=bytes_stdout)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="netstat -ano 解析是 Windows 专属路径：_find_port_listener_pid 非 win32 直接返回 None（docstring 明载契约）",
)
def test_find_port_listener_pid_parses_gbk_netstat_output(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """GBK 表头 + ASCII 数据行：不抛异常且命中目标端口的 LISTEN pid。"""
    netstat_out = "活动连接".encode("gbk") + b"\n\n" + (
        b"  Proto  Local Address      Foreign Address    State          PID\n"
        b"  TCP    127.0.0.1:8420     0.0.0.0:0          LISTENING      4242\n"
        b"  TCP    127.0.0.1:9101     0.0.0.0:0          LISTENING      999\n"
    )

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        assert "encoding" not in kwargs, "bytes 捕获形态不得再传编码参数走严格文本解码"
        return _completed(netstat_out)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert srv._find_port_listener_pid(8420) == 4242


def test_find_port_listener_pid_none_port_returns_none(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """GBK 输出、目标端口无监听：正常返回 None（不抛、不误报他端口）。"""
    netstat_out = "活动连接".encode("gbk") + b"\n" + (
        b"  TCP    127.0.0.1:9101     0.0.0.0:0          LISTENING      999\n"
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(netstat_out))
    assert srv._find_port_listener_pid(8420) is None


def test_find_port_listener_pid_null_stdout_is_none(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout=None（解码线程崩溃的事故形态）也必须返回 None 而非 AttributeError。"""
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(None))
    assert srv._find_port_listener_pid(8420) is None


def test_process_cmdline_walks_psutil_cmdline(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmdline 必须走 psutil（wmic 已在 Win11 22H2+ 默认移除，缺席=清障失效）。"""
    import psutil

    seen: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            seen.append(pid)

        def cmdline(self) -> list[str]:
            return [r"D:\工具\hindsight_api.exe", "--port", "8420"]

    monkeypatch.setattr(psutil, "Process", _FakeProcess)
    assert srv._process_cmdline(4242) == [r"D:\工具\hindsight_api.exe", "--port", "8420"]
    assert seen == [4242]


def test_process_cmdline_psutil_error_returns_empty(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """NoSuchProcess（进程已退）同归「未知占用者」契约：空表，不抛。"""
    import psutil

    def _gone(pid: int) -> Any:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", _gone)
    assert srv._process_cmdline(4242) == []
