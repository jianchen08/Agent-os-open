# @feature: FP-0.2.〇 插件开发 | @ci: python-coverage
"""plugin_verifier 执行核测试：路径闸/脚本 pin/环境洗白/超时杀树/证据脱敏。

测试纪律：断外部可观察行为（拒绝原因、命令面、落盘证据、进程树清理），
不 mock 被测实现；子进程用真进程 + 假 pin 表。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
_REPO_ROOT = _PLUGIN_DIR.parents[2]
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import verifier as v  # noqa: E402


def _demo_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    plugin_dir = workspace / "plugins" / "shared" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": "demo", "version": "1.0.0"}), encoding="utf-8"
    )
    return workspace, plugin_dir


def _app_root(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    scripts = root / "skills" / "plugin-dev" / "scripts"
    scripts.mkdir(parents=True)
    for rel in v._PINNED_SCRIPTS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.name}\n", encoding="utf-8")
    (root / "config" / "kernel").mkdir(parents=True)
    (root / "config" / "kernel" / "plugin_tiers.yaml").write_text(
        "tier1:\n  safety_plugins: []\n", encoding="utf-8"
    )
    (root / "plugins" / "shared").mkdir(parents=True)
    (root / "kernel").mkdir()
    (root / "kernel" / "agentos-kernel.exe").write_text("bin", encoding="utf-8")
    return root


def _pin_table(root: Path) -> dict[str, str]:
    return {
        rel: v._normalized_sha256(root / rel)
        for rel in v._PINNED_SCRIPTS
    }


class TestPathGuards:
    def test_workspace_requires_absolute_existing_dir(self, tmp_path: Path) -> None:
        with pytest.raises(v.VerifierError, match="缺少 workspace"):
            v.resolve_workspace("")
        with pytest.raises(v.VerifierError, match="不是绝对路径"):
            v.resolve_workspace("relative/ws")
        with pytest.raises(v.VerifierError, match="目录不存在"):
            v.resolve_workspace(str(tmp_path / "missing"))
        workspace, _ = _demo_workspace(tmp_path)
        assert v.resolve_workspace(str(workspace)) == workspace.resolve()

    def test_plugin_path_rejects_absolute_and_parent_escape(self, tmp_path: Path) -> None:
        workspace, _ = _demo_workspace(tmp_path)
        with pytest.raises(v.VerifierError, match="缺少 plugin_path"):
            v.resolve_plugin_dir(workspace, "")
        with pytest.raises(v.VerifierError, match="必须是工作空间相对路径"):
            v.resolve_plugin_dir(workspace, str(workspace / "plugins"))
        with pytest.raises(v.VerifierError, match="不得含"):
            v.resolve_plugin_dir(workspace, "../outside")

    def test_plugin_path_rejects_outside_and_missing_manifest(self, tmp_path: Path) -> None:
        workspace, _ = _demo_workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "plugin.json").write_text("{}", encoding="utf-8")
        link = workspace / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows 无符号链接权限时用 junction（同属重解析点，免管理员）。
            rc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                check=False,
            ).returncode
            if rc != 0:
                pytest.skip("当前平台不允许创建目录链接")
        with pytest.raises(v.VerifierError, match="越出工作空间"):
            v.resolve_plugin_dir(workspace, "link")
        empty = workspace / "plugins" / "empty"
        empty.mkdir()
        with pytest.raises(v.VerifierError, match="缺 plugin.json"):
            v.resolve_plugin_dir(workspace, "plugins/empty")

    def test_manifest_requires_object_and_valid_id(self, tmp_path: Path) -> None:
        workspace, plugin_dir = _demo_workspace(tmp_path)
        assert v.read_plugin_manifest(plugin_dir)["id"] == "demo"
        (plugin_dir / "plugin.json").write_text("[]", encoding="utf-8")
        with pytest.raises(v.VerifierError, match="顶层必须是对象"):
            v.read_plugin_manifest(plugin_dir)
        (plugin_dir / "plugin.json").write_text('{"id": "../x"}', encoding="utf-8")
        with pytest.raises(v.VerifierError, match="id 缺失或形态非法"):
            v.read_plugin_manifest(plugin_dir)
        (plugin_dir / "plugin.json").write_text("{bad", encoding="utf-8")
        with pytest.raises(v.VerifierError, match="不可解析"):
            v.read_plugin_manifest(plugin_dir)


class TestScriptPins:
    def test_normalized_hash_ignores_crlf(self, tmp_path: Path) -> None:
        lf = tmp_path / "lf.py"
        crlf = tmp_path / "crlf.py"
        lf.write_bytes(b"a\nb\n")
        crlf.write_bytes(b"a\r\nb\r\n")
        assert v._normalized_sha256(lf) == v._normalized_sha256(crlf)

    def test_pin_table_shape_and_match(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        pins = _pin_table(root)
        assert v.check_script_pins(root, pins) == pins
        bad = dict(pins)
        bad[v._PINNED_SCRIPTS[0]] = "0" * 64

        with pytest.raises(v.VerifierError, match="指纹不符"):
            v.check_script_pins(root, bad)

    def test_pin_table_missing_script_or_coverage(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        pins = _pin_table(root)
        (root / v._PINNED_SCRIPTS[1]).unlink()
        with pytest.raises(v.VerifierError, match="固定脚本缺失"):
            v.check_script_pins(root, pins)
        root2 = _app_root(tmp_path / "second")
        partial = {v._PINNED_SCRIPTS[0]: v._normalized_sha256(root2 / v._PINNED_SCRIPTS[0])}
        with pytest.raises(v.VerifierError, match="未覆盖必需脚本"):
            v.check_script_pins(root2, partial)

    def test_load_pin_file_rejects_bad_shape(self, tmp_path: Path) -> None:
        pin_file = tmp_path / "pins.json"
        with pytest.raises(v.VerifierError, match="不可读"):
            v.load_script_pins(pin_file)
        pin_file.write_text("[]", encoding="utf-8")
        with pytest.raises(v.VerifierError, match="形态非法"):
            v.load_script_pins(pin_file)
        pin_file.write_text('{"files": {"a": "b"}}', encoding="utf-8")
        assert v.load_script_pins(pin_file) == {"a": "b"}

    def test_repo_scripts_match_shipped_pins(self) -> None:
        """脚本锁定同文闸：改 skills/plugin-dev/scripts 必须同步 script_pins.json。"""
        assert v.check_script_pins(_REPO_ROOT) == v.load_script_pins()


class TestEnvironmentAndResolution:
    def test_build_env_drops_secrets_and_forces_utf8(self) -> None:
        env = v.build_env(
            {
                "PATH": "/bin",
                "AGENTOS_ADMIN_PASSWORD": "pw",
                "OPENAI_API_KEY": "sk",
                "SYSTEMROOT": "C:\\Windows",
                "UNRELATED": "x",
            }
        )
        assert env["PATH"] == "/bin"
        assert env["SYSTEMROOT"] == "C:\\Windows"
        assert env["PYTHONUTF8"] == "1"
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert "AGENTOS_ADMIN_PASSWORD" not in env
        assert "OPENAI_API_KEY" not in env
        assert "UNRELATED" not in env

    def test_resolve_app_root_requires_fixed_script(self, tmp_path: Path) -> None:
        with pytest.raises(v.VerifierError, match="验证脚本不在安装位内"):
            v.resolve_app_root(tmp_path)
        root = _app_root(tmp_path)
        assert v.resolve_app_root(root) == root.resolve()

    def test_resolve_app_root_env_override_wins(self, tmp_path: Path, monkeypatch: Any) -> None:
        root = _app_root(tmp_path)
        monkeypatch.setenv("AGENTOS_APP_ROOT", str(root))
        assert v.resolve_app_root() == root.resolve()

    def test_resolve_app_root_prefers_kernel_ancestor(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Tier2 用户副本：按进程祖先内核 exe 推导安装根，不取插件位置用户根。"""
        import psutil

        ready = _app_root(tmp_path)
        exe = ready / "kernel" / "agentos-kernel.exe"

        class _Kernel:
            def exe(self) -> str:
                return str(exe)

            def parent(self) -> Any:
                return None

        class _Self:
            def parent(self) -> Any:
                return _Kernel()

        monkeypatch.setattr(psutil, "Process", _Self)
        assert v.resolve_app_root() == ready.resolve()

    def test_resolve_app_root_process_error_falls_back_to_plugin_root(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import psutil

        monkeypatch.delenv("AGENTOS_APP_ROOT", raising=False)

        class _Self:
            def parent(self) -> Any:
                raise psutil.NoSuchProcess(1)

        monkeypatch.setattr(psutil, "Process", _Self)
        assert v.resolve_app_root() == v._APP_ROOT.resolve()

    def test_resolve_app_root_without_psutil_falls_back(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("AGENTOS_APP_ROOT", raising=False)
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert v._app_roots_from_process_ancestry() == []
        assert v.resolve_app_root() == v._APP_ROOT.resolve()

    def test_app_roots_skips_ancestor_without_exe(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import psutil

        exe = tmp_path / "kernel" / "agentos-kernel.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("bin", encoding="utf-8")

        class _Node:
            def __init__(self, exe_value: str, parent: Any = None) -> None:
                self._exe = exe_value
                self._parent = parent

            def exe(self) -> str:
                return self._exe

            def parent(self) -> Any:
                return self._parent

        chain = _Node(str(exe), _Node("", None))

        class _Self:
            def parent(self) -> Any:
                return chain

        monkeypatch.setattr(psutil, "Process", _Self)
        roots = v._app_roots_from_process_ancestry()
        assert tmp_path in roots  # 空 exe 祖先跳过，内核祖先候选保留

    def test_resolve_app_root_skips_duplicate_candidates(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        no_script = tmp_path / "no-script"
        no_script.mkdir()
        monkeypatch.setenv("AGENTOS_APP_ROOT", str(no_script))
        monkeypatch.setattr(v, "_app_roots_from_process_ancestry", lambda: [no_script])
        assert v.resolve_app_root() == v._APP_ROOT.resolve()

    def test_resolve_app_root_all_candidates_missing_raises(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        no_script = tmp_path / "no-script"
        no_script.mkdir()
        monkeypatch.setenv("AGENTOS_APP_ROOT", str(no_script))
        monkeypatch.setattr(v, "_app_roots_from_process_ancestry", lambda: [])
        monkeypatch.setattr(v, "_APP_ROOT", no_script)
        with pytest.raises(v.VerifierError, match="不在安装位内"):
            v.resolve_app_root()

    def test_resolve_kernel_bin_prefers_packaged(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        assert v.resolve_kernel_bin(root) == root / "kernel" / "agentos-kernel.exe"

    def test_resolve_kernel_bin_falls_back_to_cargo_target(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        (root / "kernel" / "agentos-kernel.exe").unlink()
        target = root / "target" / "debug"
        target.mkdir(parents=True)
        binary = target / "agentos-kernel"
        binary.write_text("bin", encoding="utf-8")
        assert v.resolve_kernel_bin(root) == binary

    def test_resolve_kernel_bin_finds_kernel_workspace_target(self, tmp_path: Path) -> None:
        """仓内主布局：独立 cargo 工作区 kernel/target/<profile>/。"""
        root = _app_root(tmp_path)
        (root / "kernel" / "agentos-kernel.exe").unlink()
        target = root / "kernel" / "target" / "debug"
        target.mkdir(parents=True)
        binary = target / "agentos-kernel.exe"
        binary.write_text("bin", encoding="utf-8")
        assert v.resolve_kernel_bin(root) == binary

    def test_resolve_kernel_bin_missing_is_explicit(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        (root / "kernel" / "agentos-kernel.exe").unlink()
        with pytest.raises(v.VerifierError, match="找不到内核二进制"):
            v.resolve_kernel_bin(root)

    def test_resolve_config_and_plugins_and_frontend(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        assert v.resolve_config_root(root) == root / "config"
        assert v.resolve_plugins_root(root) == root / "plugins" / "shared"
        assert v.resolve_frontend_dist(root) is None
        dist = root / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("x", encoding="utf-8")
        assert v.resolve_frontend_dist(root) == dist

    def test_resolve_config_missing_is_explicit(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        (root / "config" / "kernel" / "plugin_tiers.yaml").unlink()
        with pytest.raises(v.VerifierError, match="plugin_tiers.yaml"):
            v.resolve_config_root(root)
        (root / "plugins" / "shared").rmdir()
        with pytest.raises(v.VerifierError, match="内置插件根不存在"):
            v.resolve_plugins_root(root)

    def test_scope_and_timeout_normalization(self) -> None:
        assert v.normalize_scope(None) == "backend"
        assert v.normalize_scope("Frontend") == "frontend"
        with pytest.raises(v.VerifierError, match="scope 非法"):
            v.normalize_scope("ui")
        assert v.normalize_timeout(None) == 240
        assert v.normalize_timeout("120") == 120
        with pytest.raises(v.VerifierError, match="timeout_s 非法"):
            v.normalize_timeout("abc")
        with pytest.raises(v.VerifierError, match="超出"):
            v.normalize_timeout(10)


class TestCommandAndRun:
    def test_build_command_backend_and_frontend(self, tmp_path: Path) -> None:
        root = _app_root(tmp_path)
        workspace, plugin_dir = _demo_workspace(tmp_path)
        common: dict[str, Any] = {
            "app_root": root,
            "plugin_dir": plugin_dir,
            "workspace": workspace,
            "scratch_root": workspace / ".plugin_verify" / "demo",
            "kernel_bin": root / "kernel" / "agentos-kernel.exe",
            "config_root": root / "config",
            "plugins_root": root / "plugins" / "shared",
        }
        backend = v.build_command(scope="backend", **common)
        assert backend[:2] == [sys.executable, str(root / v._VERIFY_HOST_REL)]
        assert "--frontend" not in backend
        assert backend[backend.index("--scratch-root") + 1] == str(common["scratch_root"])
        frontend = v.build_command(
            scope="frontend", frontend_dist=root / "frontend" / "dist", **common
        )
        assert "--frontend" in frontend
        assert frontend[frontend.index("--frontend-dist") + 1] == str(
            root / "frontend" / "dist"
        )

    def test_run_command_success(self) -> None:
        code = "print('hello')"
        rc, out, err = v.run_command(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            env=v.build_env(),
            timeout_s=30,
        )
        assert (rc, out.strip(), err) == (0, "hello", "")

    def test_run_command_timeout_kills_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import proc_tree

        calls: list[int] = []
        real = proc_tree.kill_process_tree

        def _kill(pid: int) -> list[str]:
            calls.append(pid)
            return real(pid)

        monkeypatch.setattr(proc_tree, "kill_process_tree", _kill)
        rc, _out, err = v.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=Path.cwd(),
            env=v.build_env(),
            timeout_s=1,
        )
        assert rc == 124
        assert calls
        assert "验证超时" in err

    def test_run_command_kills_process_when_drain_also_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """杀树后收尸仍超时：兜底 proc.kill() + 无超时 communicate，不挂死调用方。"""
        import proc_tree

        class _StuckProc:
            pid = 4321
            returncode = -9

            def __init__(self) -> None:
                self.calls = 0
                self.killed = False

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                self.calls += 1
                if self.calls <= 2:
                    raise subprocess.TimeoutExpired("cmd", timeout or 0)
                return "", ""

            def kill(self) -> None:
                self.killed = True

        proc = _StuckProc()
        monkeypatch.setattr(v.subprocess, "Popen", lambda *_a, **_k: proc)
        monkeypatch.setattr(proc_tree, "kill_process_tree", lambda _pid: ["pid=1 残留"])
        rc, _out, err = v.run_command(
            ["cmd"], cwd=Path.cwd(), env={}, timeout_s=1
        )
        assert rc == 124
        assert proc.killed is True
        assert "清理失败" in err


class TestEvidence:
    def test_scrub_evidence_redacts_nested_password(self) -> None:
        raw: dict[str, Any] = {
            "backend": {"instance": {"admin_password": "pw", "port": 1}},
            "items": [{"admin_password": None}, {"ok": True}],
        }
        cleaned = v.scrub_evidence(raw)
        assert cleaned["backend"]["instance"]["admin_password"] == "***"
        assert cleaned["backend"]["instance"]["port"] == 1
        assert cleaned["items"][0]["admin_password"] is None
        assert raw["backend"]["instance"]["admin_password"] == "pw"  # 不改原引用

    def test_read_evidence_missing_broken_and_non_object(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "p"
        plugin_dir.mkdir()
        assert v.read_evidence(plugin_dir) is None
        (plugin_dir / v._EVIDENCE_NAME).write_text("{bad", encoding="utf-8")
        assert v.read_evidence(plugin_dir) is None
        (plugin_dir / v._EVIDENCE_NAME).write_text("[]", encoding="utf-8")
        assert v.read_evidence(plugin_dir) is None
        (plugin_dir / v._EVIDENCE_NAME).write_text('{"all_pass": true}', encoding="utf-8")
        assert v.read_evidence(plugin_dir) == {"all_pass": True}

    def test_redact_text_masks_password_field(self) -> None:
        assert v._redact_text('{"admin_password": "abc"}') == '{"admin_password": "***"}'

    def test_cleanup_scratch_retries_until_unlocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        calls: list[int] = []
        real_rmtree = v.shutil.rmtree

        def _flaky(path: Path, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) >= 2:  # 第一次模拟文件锁，第二次成功
                real_rmtree(path, **kwargs)

        monkeypatch.setattr(v.shutil, "rmtree", _flaky)
        monkeypatch.setattr(v.time, "sleep", lambda _s: None)
        assert v._cleanup_scratch(scratch) == []
        assert len(calls) == 2

    def test_cleanup_scratch_scrubs_password_when_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        lane = scratch / "lane"
        lane.mkdir(parents=True)
        instance_file = lane / "instance.yaml"
        instance_file.write_text('{"admin_password": "pw"}', encoding="utf-8")
        monkeypatch.setattr(v.shutil, "rmtree", lambda *_a, **_k: None)
        monkeypatch.setattr(v.time, "sleep", lambda _s: None)
        failures = v._cleanup_scratch(scratch)
        assert failures
        assert "pw" not in instance_file.read_text(encoding="utf-8")
        assert "***" in instance_file.read_text(encoding="utf-8")

    def test_cleanup_scratch_redact_failure_still_reports_leftover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        lane = scratch / "lane"
        lane.mkdir(parents=True)
        (lane / "instance.yaml").write_text('{"admin_password": "pw"}', encoding="utf-8")
        monkeypatch.setattr(v.shutil, "rmtree", lambda *_a, **_k: None)
        monkeypatch.setattr(v.time, "sleep", lambda _s: None)

        def _locked(_text: str) -> str:
            raise OSError("locked")

        monkeypatch.setattr(v, "_redact_text", _locked)
        assert v._cleanup_scratch(scratch)

    def test_cleanup_scratch_unlinks_view_before_rmtree(
        self, tmp_path: Path
    ) -> None:
        """视图目录链接先摘除：rmtree 不得递归进 staging 插件原件。"""
        target = tmp_path / "staging" / "demo"
        target.mkdir(parents=True)
        (target / "plugin.json").write_text("{}", encoding="utf-8")
        scratch = tmp_path / "scratch"
        view = scratch / "lane" / ".verify_tier2_view"
        view.mkdir(parents=True)
        link = view / "demo"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            rc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            ).returncode
            if rc != 0:
                pytest.skip("当前平台不允许创建目录链接")
        assert v._cleanup_scratch(scratch) == []
        assert not scratch.exists()
        assert (target / "plugin.json").is_file()  # 原件完好

    def test_cleanup_scratch_view_unlink_failure_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        view = scratch / "lane" / ".verify_tier1_view"
        (view / "dir_child").mkdir(parents=True)
        (view / "file_child").write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            v.Path, "rmdir", lambda *_a, **_k: (_ for _ in ()).throw(OSError("busy"))
        )
        monkeypatch.setattr(v.shutil, "rmtree", lambda *_a, **_k: None)
        monkeypatch.setattr(v.time, "sleep", lambda _s: None)
        assert v._cleanup_scratch(scratch)


class TestVerificationFlow:
    def _prepare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path]:
        root = _app_root(tmp_path)
        workspace, plugin_dir = _demo_workspace(tmp_path)
        monkeypatch.setattr(v, "load_script_pins", lambda: _pin_table(root))
        return root, workspace, plugin_dir

    def test_success_redacts_and_cleans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, workspace, plugin_dir = self._prepare(tmp_path, monkeypatch)
        captured: dict[str, Any] = {}
        probe_dir = plugin_dir / "verify_frontend_evidence"
        probe_dir.mkdir()
        (probe_dir / "probe_config.json").write_text('{"admin_password": "pw"}', encoding="utf-8")

        def _fake_run(command: list[str], **kwargs: Any) -> tuple[int, str, str]:
            captured["command"] = command
            captured["kwargs"] = kwargs
            (plugin_dir / v._EVIDENCE_NAME).write_text(
                json.dumps(
                    {
                        "all_pass": True,
                        "checks": [{"name": "backend", "pass": True, "detail": "ok"}],
                        "frontend": {"screenshots": ["/tmp/a.png"]},
                        "backend": {"instance": {"admin_password": "secret", "port": 1}},
                    }
                ),
                encoding="utf-8",
            )
            return 0, "done", ""

        monkeypatch.setattr(v, "run_command", _fake_run)
        result = v.run_plugin_verification(
            str(workspace), "plugins/shared/demo", "frontend", 120, app_root=root
        )
        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["checks"] == [{"name": "backend", "pass": True, "detail": "ok"}]
        assert result["screenshots"] == ["/tmp/a.png"]
        assert captured["command"][captured["command"].index("--frontend") :][:1] == ["--frontend"]
        assert captured["kwargs"]["timeout_s"] == 120
        # 落盘证据二次脱敏 + 前端探针配置（含明文口令）已删
        on_disk = json.loads((plugin_dir / v._EVIDENCE_NAME).read_text(encoding="utf-8"))
        assert on_disk["backend"]["instance"]["admin_password"] == "***"
        assert not (probe_dir / "probe_config.json").exists()
        # scratch 已清（fake run 没建目录也不报错）
        assert not (workspace / ".plugin_verify").exists()

    def test_failure_without_evidence_reports_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, workspace, _plugin_dir = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(
            v, "run_command", lambda *_a, **_k: (1, "", "错误: 内核启动失败 admin_password: pw")
        )
        result = v.run_plugin_verification(
            str(workspace), "plugins/shared/demo", "backend", 120, app_root=root
        )
        assert result["success"] is False
        assert result["status"] == "failed"
        assert "内核启动失败" in result["error"]
        assert "admin_password: pw" not in result["error"]  # 文本形态口令同样脱敏
        assert "admin_password: ***" in result["error"]
        assert result["evidence_path"] is None

    def test_failure_with_bad_evidence_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, workspace, plugin_dir = self._prepare(tmp_path, monkeypatch)
        (plugin_dir / v._EVIDENCE_NAME).write_text("{bad", encoding="utf-8")
        monkeypatch.setattr(v, "run_command", lambda *_a, **_k: (0, "done", ""))
        result = v.run_plugin_verification(
            str(workspace), "plugins/shared/demo", "backend", 120, app_root=root
        )
        assert result["status"] == "completed"
        assert result["all_pass"] is False
        assert "未产出证据文件" in result["error"]

    def test_timeout_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, workspace, _plugin_dir = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(
            v, "run_command", lambda *_a, **_k: (124, "", "验证超时（>120s），已杀进程树")
        )
        result = v.run_plugin_verification(
            str(workspace), "plugins/shared/demo", "backend", 120, app_root=root
        )
        assert result["status"] == "timeout"
        assert result["success"] is False
        assert "已杀进程树" in result["error"]

    def test_cleanup_failure_is_visible_in_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, workspace, _plugin_dir = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(v, "run_command", lambda *_a, **_k: (0, "done", ""))
        monkeypatch.setattr(v, "_cleanup_scratch", lambda _p: ["leftover"])
        result = v.run_plugin_verification(
            str(workspace), "plugins/shared/demo", "backend", 120, app_root=root
        )
        assert "scratch 清理失败" in result["error"]

    def test_verification_rejects_absolute_plugin_path_before_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, workspace, _plugin_dir = self._prepare(tmp_path, monkeypatch)
        with pytest.raises(v.VerifierError, match="必须是工作空间相对路径"):
            v.run_plugin_verification(
                str(workspace), str(workspace / "plugins/shared/demo"), app_root=root
            )


class TestServerHandler:
    """工具边界：异常一律转结构化错误（不向 MCP 抛裸异常）。"""

    @staticmethod
    def _server() -> Any:
        """按文件路径加载本插件 server（裸 import server 会被邻插件同名模块遮蔽）。"""
        import importlib.util

        name = "plugin_verifier_server_test"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "server.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_missing_workspace_returns_structured_error(self) -> None:
        import asyncio

        server = self._server()

        result = asyncio.run(server.plugin_verify(plugin_path="plugins/shared/demo"))
        assert result["success"] is False
        assert result["status"] == "error"
        assert "workspace" in result["error"]

    def test_success_passthrough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        server = self._server()

        payload = {
            "success": True,
            "status": "completed",
            "plugin_id": "demo",
            "scope": "backend",
            "all_pass": True,
            "checks": [],
        }
        monkeypatch.setattr(server, "run_plugin_verification", lambda *_a, **_k: payload)
        result = asyncio.run(
            server.plugin_verify(
                plugin_path="plugins/shared/demo", workspace=str(Path.cwd())
            )
        )
        assert result == payload

    def test_unexpected_exception_is_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        server = self._server()

        def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(server, "run_plugin_verification", _boom)
        result = asyncio.run(
            server.plugin_verify(
                plugin_path="plugins/shared/demo", workspace=str(Path.cwd())
            )
        )
        assert result["status"] == "error"
        assert result["error"] == "RuntimeError: kaboom"
