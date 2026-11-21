# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""web_app 官方 Web 应用监督器：前置条件、profile 链接、patch 拼装、生命周期。

契约要点（可观察行为）：
- Node 版本闸：< 22 如实报 ``node_too_old``（带已发现版本），不静默用旧 Node 起
  （DSH 官方 Web 应用要求 >= 22）；
- ``profiles/web`` 必须是运行时工作区的链接：已存在且指向别处 → ``profile_conflict``，
  绝不静默退化成第二个 profile（那会让两侧插件列表分叉）；
- ``--patch`` 清单取自各 bundle 自己的 ``dsh.bundle.patch`` 声明，顺序 base→web-app；
- 监听端口固定（``WEB_PORT_DEFAULT``，CSP 精确放行的前提）+ URL 从
  ``dsh web: <url>`` 行解析；
- 启动失败如实 ``start_failed`` 并带进程残留输出；已在跑则 start 幂等。

mock 边界：只替外部过程（subprocess.Popen/run = 外部 Node 运行时）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

pytestmark = pytest.mark.unit

_MODULE = "dsh_adapter_web_app_test"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE, PLUGIN_DIR / "web_app.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wa() -> Any:
    return _load()


def _write_pkg(root: Path, name: str, version: str, patch: Any) -> None:
    pkg = root / "node_modules" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": name, "version": version, "dsh": {"bundle": {"patch": patch}}}),
        encoding="utf-8",
    )
    files = [patch] if isinstance(patch, str) else list(patch)
    for rel in files:
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[]\n", encoding="utf-8")


class TestNodeGate:
    def test_accepts_explicit_node_when_new_enough(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        assert sup.resolve_node() == "/opt/node22/bin/node"

    def test_rejects_old_explicit_node(self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (20, 19, 6))
        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node20/bin/node")
        with pytest.raises(wa.WebAppError) as exc:
            sup.resolve_node()
        assert exc.value.code == "node_too_old"
        assert "20.19.6" in exc.value.message

    def test_reports_missing_binary(self, wa: Any, tmp_path: Path) -> None:
        """显式指定的 node 不存在 → node_unusable（先于版本判定，不留空指向）。"""
        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/nonexistent/node")
        with pytest.raises(wa.WebAppError) as exc:
            sup.resolve_node()
        assert exc.value.code == "node_unusable"

    def test_path_scan_reports_found_versions(self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PATH 里只有旧 Node 时：如实列出发现版本（可判定，不含糊）。"""
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr(wa, "_node_candidates", lambda: ["/usr/bin/node"])
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (20, 19, 6))
        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        with pytest.raises(wa.WebAppError) as exc:
            sup.resolve_node()
        assert exc.value.code == "node_too_old"
        assert exc.value.detail["found"] == [{"path": "/usr/bin/node", "version": "20.19.6"}]

    def test_path_scan_picks_first_compliant(self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wa, "_node_candidates", lambda: ["/usr/bin/node", "/home/u/node22/bin/node"])
        monkeypatch.setattr(
            wa, "node_version_of",
            lambda b: (20, 0, 0) if b == "/usr/bin/node" else (22, 20, 0),
        )
        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        assert sup.resolve_node() == "/home/u/node22/bin/node"


def _make_profile_link(link: Path, target: Path) -> None:
    """测试布置用目录链接：非特权 Windows 走 junction（mklink /J 免特权），
    POSIX 走符号链接。断言面统一用 resolve 等价（两种形态同语义）。"""
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - 固定 cmd 内建，参数自持
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False, errors="replace",
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def _popen_with_mklink(sup_factory: Any) -> Any:
    """Popen 替身路由：``cmd /c mklink``（profile 链接回退）走专用成功件，
    不与监督器进程件共享实例——subprocess.run 的整数退出码契约与 supervisor
    的存活轮询语义在同一个假件上互斥（共享时 mklink 被 run 判异常退出）。"""

    def _popen(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        if argv and str(argv[0]).lower() == "cmd":
            return _FakeProc([], exit_code=0)
        return sup_factory(cmd, *args, **kwargs)

    return _popen


class TestProfileLink:
    def test_links_when_absent(self, wa: Any, tmp_path: Path) -> None:
        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        home = sup.ensure_home()
        link = home / "profiles" / "web"
        # 链接形态随平台特权而变（symlink 或 junction），契约 = 指向运行时工作区
        assert link.resolve() == tmp_path.resolve()
        # profile 骨架补齐（launcher 会覆写 cordis.yml，先保证存在）
        assert (tmp_path / "cordis.yml").is_file()
        assert (tmp_path / "cordis.patch.yml").is_file()

    def test_idempotent_when_already_linked(self, wa: Any, tmp_path: Path) -> None:
        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        sup.ensure_home()
        sup.ensure_home()  # 二次调用不抛

    def test_conflict_when_pointing_elsewhere(self, wa: Any, tmp_path: Path) -> None:
        """已存在且指向别处 → 拒绝（禁静默制造第二个 profile）。"""
        other = tmp_path / "other-profile"
        other.mkdir()
        home = tmp_path / "root"
        home.mkdir()
        (home / wa.WEB_HOME_DIR / "profiles").mkdir(parents=True)
        _make_profile_link(home / wa.WEB_HOME_DIR / "profiles" / "web", other)

        sup = wa.WebAppSupervisor(runtime_root=home)
        with pytest.raises(wa.WebAppError) as exc:
            sup.ensure_home()
        assert exc.value.code == "profile_conflict"

    def test_falls_back_to_junction_when_symlink_denied(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows 无建链接特权时必须退目录联接，而不是报错放弃。

        对照组：符号链接不可用时若直接抛 ``profile_link_failed``，官方 Web 应用
        永远起不来（本机实测 WinError 1314）。故断言"回退被调用且链接落位"。
        """
        calls: list[tuple[Path, Path]] = []

        def deny_then_junction(link: Path, target: Path) -> None:
            calls.append((link, target))
            _make_profile_link(link, target)  # 模拟回退链接生效（junction/符号链接随平台）

        monkeypatch.setattr(wa, "_link_profile_dir", deny_then_junction)

        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        home = sup.ensure_home()

        assert len(calls) == 1
        assert calls[0][1] == tmp_path.resolve()
        assert (home / "profiles" / "web").resolve() == tmp_path.resolve()

    def test_link_profile_dir_uses_junction_cmd_on_windows(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_link_profile_dir``：符号链接失败 → 走 ``cmd /c mklink /J``。

        用"父目录不存在"触发真实的 ``OSError``（不 patch ``Path`` 类方法，避免
        污染同进程其它用例），再断言 Windows 分支改走 mklink。
        """
        link = tmp_path / "absent-dir" / "link"
        target = tmp_path / "target"
        target.mkdir()
        recorded: list[list[str]] = []

        class _Done:
            returncode = 0
            stdout = "Junction created"
            stderr = ""

        monkeypatch.setattr(wa, "os", type("StubOs", (), {"name": "nt"})())
        monkeypatch.setattr(
            wa.subprocess, "run",
            lambda cmd, **_k: (recorded.append(cmd), _Done())[1],
        )

        wa._link_profile_dir(link, target)

        assert len(recorded) == 1
        cmd = recorded[0]
        assert cmd[:4] == ["cmd", "/c", "mklink", "/J"]
        assert cmd[4] == str(link) and cmd[5] == str(target)

    def test_link_profile_dir_propagates_on_non_windows(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """非 Windows 平台符号链接失败照实抛，不做 junction 回退。"""
        monkeypatch.setattr(wa, "os", type("StubOs", (), {"name": "posix"})())

        with pytest.raises(OSError):
            wa._link_profile_dir(tmp_path / "absent-dir" / "link", tmp_path)


class TestNpmResolution:
    """``_resolve_npm`` 必须给 ``CreateProcess`` 可直调的入口。

    Windows 上 Node 目录并列 ``npm``（POSIX shell 脚本）/``npm.cmd``/``npm.ps1``；
    裸 ``npm`` 交给 ``subprocess`` 会以 WinError 193 失败（本机实测）。
    """

    def test_windows_picks_cmd_shim_not_extensionless_script(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bindir = tmp_path / "nodejs"
        bindir.mkdir()
        (bindir / "node.exe").write_text("", encoding="utf-8")
        (bindir / "npm").write_text("#!/bin/sh\n", encoding="utf-8")      # POSIX 脚本
        (bindir / "npm.cmd").write_text("@echo off\n", encoding="utf-8")  # 真入口
        # POSIX 依赖 X_OK，Windows 无该位语义（os.access 恒真）；两侧都置可执行，
        # 判据才只剩"最终选了哪个入口名"这一条。
        (bindir / "npm").chmod(0o755)
        (bindir / "npm.cmd").chmod(0o755)

        monkeypatch.setattr(wa, "_NPM_SHIM_NAMES", ("npm.cmd", "npm.exe"))

        got = wa._resolve_npm(str(bindir / "node.exe"))

        assert got is not None
        assert Path(got).name == "npm.cmd"
        assert Path(got).name != "npm"

    def test_posix_picks_plain_npm(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bindir = tmp_path / "nodejs"
        bindir.mkdir()
        (bindir / "node").write_text("", encoding="utf-8")
        (bindir / "npm").write_text("#!/bin/sh\n", encoding="utf-8")
        (bindir / "npm").chmod(0o755)

        monkeypatch.setattr(wa, "_NPM_SHIM_NAMES", ("npm",))

        got = wa._resolve_npm(str(bindir / "node"))

        assert got is not None
        assert Path(got).name == "npm"

    def test_falls_through_to_path_when_sibling_missing(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Node 目录没有 npm 时回落到 PATH 查找，不返回不存在的兄弟路径。"""
        bindir = tmp_path / "nodejs"
        bindir.mkdir()
        (bindir / "node.exe").write_text("", encoding="utf-8")

        monkeypatch.setattr(wa, "_NPM_SHIM_NAMES", ("npm.cmd", "npm.exe"))
        monkeypatch.setattr(wa.shutil, "which", lambda _n: "/path/npm.cmd")

        assert wa._resolve_npm(str(bindir / "node.exe")) == "/path/npm.cmd"


class TestBundlePatches:
    def test_reads_declared_patch_lists_in_bundle_order(self, wa: Any, tmp_path: Path) -> None:
        _write_pkg(tmp_path, wa.BASE_BUNDLE, "0.2.1-rc.5", "./cordis.patch.yml")
        _write_pkg(tmp_path, wa.WEB_APP_BUNDLE, "0.2.1-rc.5",
                   ["./cordis.patch.yml", "./presets/standard.patch.yml"])

        args = wa.WebAppSupervisor(runtime_root=tmp_path).bundle_patch_args()
        assert args.count("--patch") == 3
        base_idx = args.index(str(tmp_path / "node_modules" / wa.BASE_BUNDLE / "cordis.patch.yml"))
        web_idx = args.index(str(tmp_path / "node_modules" / wa.WEB_APP_BUNDLE / "cordis.patch.yml"))
        preset_idx = args.index(
            str(tmp_path / "node_modules" / wa.WEB_APP_BUNDLE / "presets" / "standard.patch.yml"))
        # 顺序：base 层先于 web-app 层（官方模板顺序）
        assert base_idx < web_idx < preset_idx

    def test_missing_bundle_is_actionable(self, wa: Any, tmp_path: Path) -> None:
        with pytest.raises(wa.WebAppError) as exc:
            wa.WebAppSupervisor(runtime_root=tmp_path).bundle_patch_args()
        assert exc.value.code == "stack_incomplete"
        assert wa.BASE_BUNDLE in exc.value.message

    def test_missing_patch_file_is_actionable(self, wa: Any, tmp_path: Path) -> None:
        _write_pkg(tmp_path, wa.BASE_BUNDLE, "1", "./cordis.patch.yml")
        pkg = tmp_path / "node_modules" / wa.WEB_APP_BUNDLE
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(
            json.dumps({"name": wa.WEB_APP_BUNDLE, "dsh": {"bundle": {"patch": "./absent.yml"}}}),
            encoding="utf-8")
        with pytest.raises(wa.WebAppError) as exc:
            wa.WebAppSupervisor(runtime_root=tmp_path).bundle_patch_args()
        assert exc.value.code == "stack_incomplete"
        assert "absent.yml" in exc.value.message


class TestInstallCommand:
    """安装命令必须跟随 profile 的既有包管理形态（否则 pnpm 后续变更被拒）。"""

    def test_prefers_pnpm_when_profile_has_it(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wa, "_resolve_pnpm", lambda _root: ("/pnpm/bin/pnpm", []))
        cmd = wa.WebAppSupervisor(runtime_root=tmp_path).install_command()
        assert cmd[0] == "/pnpm/bin/pnpm"
        assert "add" in cmd
        assert "--save-exact" in cmd
        assert any(spec.startswith(f"{wa.BASE_BUNDLE}@") for spec in cmd)
        assert any(spec.startswith(f"{wa.WEB_APP_BUNDLE}@") for spec in cmd)
        assert any(spec.startswith(f"{wa.CLI_PACKAGE}@") for spec in cmd)

    def test_falls_back_to_npm_without_pnpm(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wa, "_resolve_pnpm", lambda _root: (None, []))
        monkeypatch.setattr(wa, "_resolve_npm", lambda _node: "/usr/bin/npm")
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        cmd = sup.install_command()
        assert cmd[0] == "/usr/bin/npm"
        assert "install" in cmd
        # npm 路径不写 lockfile（避免与 pnpm 的 lockfile 混用）
        assert "--package-lock=false" in cmd

    def test_reports_when_neither_available(self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wa, "_resolve_pnpm", lambda _root: (None, []))
        monkeypatch.setattr(wa, "_resolve_npm", lambda _node: None)
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        with pytest.raises(wa.WebAppError) as exc:
            sup.install_command()
        assert exc.value.code == "npm_missing"


class _FakeProc:
    """launcher 替身：按行吐输出，poll() 表示存活。"""

    def __init__(self, lines: list[str], exit_code: int | None = None) -> None:
        self._lines = list(lines)
        self._exit = exit_code
        self.pid = 4321
        self.stdout = self
        self.terminated = False
        self.killed = False
        # subprocess.run/Popen 契约面：args 回填、communicate 后 returncode 可读
        self.args: tuple[str, ...] = ("node",)
        self.returncode = exit_code if exit_code is not None else 0

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def poll(self) -> int | None:
        return self._exit if not self._lines else None

    def communicate(
        self, input: bytes | str | None = None, timeout: float | None = None  # noqa: A002
    ) -> tuple[str, str]:
        return "", ""

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True

    # 生产面经 subprocess.run/Popen 上下文管理器形态持有进程（cc94732fda
    # Windows npm 入口修复起），替身需实现同协议（__exit__ 对齐真实 Popen：
    # 等待收尾，不主动 terminate——终止语义由 supervisor 显式调用）
    def __enter__(self) -> "_FakeProc":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _ready_stack(root: Path, wa: Any) -> None:
    """铺一个"全栈已装"的最小工作区（launcher 文件在位）。"""
    _write_pkg(root, wa.BASE_BUNDLE, "1", "./cordis.patch.yml")
    _write_pkg(root, wa.WEB_APP_BUNDLE, "1", "./cordis.patch.yml")
    _write_pkg(root, wa.CLI_PACKAGE, "1", "./whatever.yml")
    launcher = root / "node_modules" / wa.CLI_PACKAGE / "lib" / "bin.js"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("// launcher\n", encoding="utf-8")


class TestLifecycle:
    def test_start_parses_url_and_uses_port_zero(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ready_stack(tmp_path, wa)
        captured: dict[str, Any] = {}

        def _fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            return _FakeProc(["starting…\n",
                              "dsh web: http://127.0.0.1:41234/?token=tok\n"])

        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        monkeypatch.setattr(wa.subprocess, "Popen", _popen_with_mklink(_fake_popen))

        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        status = sup.start()

        assert status.available is True
        assert status.url == "http://127.0.0.1:41234/?token=tok"
        assert status.origin == "http://127.0.0.1:41234"
        # 端口固定：前端以跨源 iframe 内嵌，宿主 CSP 只能精确放行固定端口
        assert captured["cmd"][-2:] == ["--port", str(wa.WEB_PORT_DEFAULT)]
        assert "--no-open" in captured["cmd"]
        assert "--profile" in captured["cmd"]
        assert "web" in captured["cmd"]
        assert captured["cmd"].count("--patch") == 2
        # DSH_HOME 指向监督器自建 home（profile 目录经它解析到运行时工作区）
        assert captured["env"]["DSH_HOME"] == str(tmp_path / wa.WEB_HOME_DIR)
        # 已在跑：start 幂等复用同一 URL，不再起新进程
        assert sup.start().url == "http://127.0.0.1:41234/?token=tok"

    def test_start_failure_reports_exit_and_tail(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ready_stack(tmp_path, wa)
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        monkeypatch.setattr(
            wa.subprocess, "Popen",
            _popen_with_mklink(lambda *_a, **_k: _FakeProc(["boom: profile broken\n"], exit_code=2)),
        )

        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        with pytest.raises(wa.WebAppError) as exc:
            sup.start()
        assert exc.value.code == "start_failed"
        assert "code=2" in exc.value.message
        # 失败后状态如实（不返回假的 available）
        status = sup.status()
        assert status.available is False
        assert status.reason is not None
        assert "code=2" in status.reason

    def test_status_reports_starting_while_spawning(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sup = wa.WebAppSupervisor(runtime_root=tmp_path)
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        sup.node_bin = "/opt/node22/bin/node"

        def _slow_popen(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            # 首个 readline 前把状态改成 starting（模拟冷启动窗口）
            return _FakeProc(["", "dsh web: http://127.0.0.1:1/?token=x\n"])

        monkeypatch.setattr(wa.subprocess, "Popen", _popen_with_mklink(_slow_popen))
        assert sup.status().available is False

    def test_stop_terminates_process(
        self, wa: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ready_stack(tmp_path, wa)
        proc = _FakeProc(["dsh web: http://127.0.0.1:1/?token=x\n"])
        monkeypatch.setattr(wa, "node_version_of", lambda _b: (22, 20, 0))
        monkeypatch.setattr(wa.subprocess, "Popen", _popen_with_mklink(lambda *_a, **_k: proc))

        sup = wa.WebAppSupervisor(runtime_root=tmp_path, node_bin="/opt/node22/bin/node")
        sup.start()
        sup.stop()
        assert proc.terminated is True
        assert sup.status().available is False


class TestUrlParsing:
    @pytest.mark.parametrize(
        ("url", "origin"),
        [
            ("http://127.0.0.1:3080/?token=abc", "http://127.0.0.1:3080"),
            ("http://127.0.0.1:41234/", "http://127.0.0.1:41234"),
            ("https://app.example/ui/?token=t", "https://app.example"),
        ],
    )
    def test_origin_of(self, wa: Any, url: str, origin: str) -> None:
        assert wa._origin_of(url) == origin

    def test_origin_of_garbage(self, wa: Any) -> None:
        assert wa._origin_of("not-a-url") is None

    def test_wait_for_url_ignores_noise_lines(self, wa: Any) -> None:
        proc = _FakeProc(["[warn] something\n", "dsh web: http://127.0.0.1:9/?token=z\n"])
        assert wa._wait_for_url(proc, timeout_s=1.0) == "http://127.0.0.1:9/?token=z"
