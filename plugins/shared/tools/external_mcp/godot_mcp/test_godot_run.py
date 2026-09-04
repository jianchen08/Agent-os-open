# @feature: FP-0.2.〇 godot_run workspace 路由 | @ci: python-coverage
"""godot_run 工具测试（执行面按 workspace 指向路由，serve 进程按工程缓存复用）。

覆盖：工程解析（workspace 含 project.godot / 祖先回溯 / worktree 还原主工程 /
解析失败 fail-closed）；serve 代理（握手+tools/call 透传、同工程进程复用、
跨工程独立进程、显式 project 覆盖）；GODOT_MCP_BIN 缺失/路径无效报错。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 共享层自举（plugins/shared/）
_SHARED_ROOT = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

# 假 serve 独立成文件（fake_serve.py），避免内嵌字符串转义地狱
_FAKE_SERVE_PATH = _PLUGIN_DIR / "fake_serve.py"


def _load_module() -> Any:
    """动态加载 tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "godot_run_tool_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离：假 serve 替换 argv、GODOT_MCP_BIN 指向解释器、代理注册表清空。"""
    mod = _load_module()
    fake = _FAKE_SERVE_PATH
    spawn_file = tmp_path / "spawns.log"
    spawn_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GODOT_MCP_BIN", sys.executable)
    monkeypatch.setenv("FAKE_SPAWN_FILE", str(spawn_file))

    def _fake_argv(bin_path: str, project_dir: Path) -> list[str]:
        return [sys.executable, str(fake), "--project", str(project_dir)]

    monkeypatch.setattr(mod, "_serve_argv", _fake_argv)
    monkeypatch.setattr(mod, "_PROXIES", {})
    return {"tmp": tmp_path, "spawn_file": spawn_file}


def _tool() -> Any:
    return _load_module().GodotRunTool()


class TestResolveProjectDir:
    def test_workspace_with_project_godot(self, tmp_path: Path) -> None:
        ws = tmp_path / "proj"
        ws.mkdir()
        (ws / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        assert _load_module().resolve_project_dir("", str(ws)) == ws.resolve()

    def test_ancestor_walk_up(self, tmp_path: Path) -> None:
        ws = tmp_path / "proj"
        (ws / "scenes").mkdir(parents=True)
        (ws / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        assert (
            _load_module().resolve_project_dir("", str(ws / "scenes")) == ws.resolve()
        )

    def test_worktree_resolves_main_repo(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (main / ".git").mkdir()
        wt_gitdir = main / ".git" / "worktrees" / "w1"
        wt_gitdir.mkdir(parents=True)
        ws = tmp_path / "wt"
        (ws / "scenes").mkdir(parents=True)
        (ws / ".git").write_text(f"gitdir: {wt_gitdir.as_posix()}\n", encoding="utf-8")
        assert (
            _load_module().resolve_project_dir("", str(ws / "scenes")) == main.resolve()
        )

    def test_unresolvable_raises_with_guidance(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="project.godot"):
            _load_module().resolve_project_dir("", str(empty))


class TestGodotRunExecute:
    async def test_call_proxies_and_reuses_process(self, env: dict[str, Any]) -> None:
        """同工程两次调用：结果透传且 serve 进程只 spawn 一次（复用）。"""
        proj = env["tmp"] / "projA"
        (proj / "scenes").mkdir(parents=True)
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        tool = _tool()
        r1 = await tool.execute(
            {"method": "node.add", "params": {"name": "Player"}, "workspace": str(proj)}
        )
        r2 = await tool.execute(
            {"method": "scene.tree", "workspace": str(proj / "scenes")}
        )
        assert r1.success and r2.success, r1.error or r2.error
        assert r1.output["project"] == str(proj)
        assert r1.output["method"] == "node.add"
        assert r2.output["method"] == "scene.tree"
        spawns = env["spawn_file"].read_text(encoding="utf-8").strip().splitlines()
        assert len(spawns) == 1
        assert "--project" in spawns[0] and "projA" in spawns[0].replace("\\", "/")

    async def test_second_project_gets_own_process(self, env: dict[str, Any]) -> None:
        """跨工程调用：各自 spawn 独立 serve，路由互不串。"""
        mod = _load_module()
        for name in ("projB", "projC"):
            proj = env["tmp"] / name
            proj.mkdir()
            (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        tool = _tool()
        rb = await tool.execute({"method": "engine.commands", "workspace": str(env["tmp"] / "projB")})
        rc = await tool.execute({"method": "engine.commands", "workspace": str(env["tmp"] / "projC")})
        assert rb.success and rc.success
        assert rb.output["project"] == str(env["tmp"] / "projB")
        assert rc.output["project"] == str(env["tmp"] / "projC")
        spawns = env["spawn_file"].read_text(encoding="utf-8").strip().splitlines()
        assert len(spawns) == 2
        assert len(mod._PROXIES) == 2

    async def test_explicit_project_overrides_workspace(
        self, env: dict[str, Any]
    ) -> None:
        proj = env["tmp"] / "projD"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        elsewhere = env["tmp"] / "elsewhere"
        elsewhere.mkdir()
        r = await _tool().execute(
            {"method": "project.info", "workspace": str(elsewhere), "project": str(proj)}
        )
        assert r.success, r.error
        assert r.output["project"] == str(proj)

    async def test_missing_bin_fails_named_var(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GODOT_MCP_BIN")
        r = await _tool().execute({"method": "engine.commands", "workspace": str(env["tmp"])})
        assert not r.success
        assert "GODOT_MCP_BIN" in r.error

    async def test_bin_path_not_found_fails(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GODOT_MCP_BIN", str(env["tmp"] / "nope.exe"))
        r = await _tool().execute({"method": "engine.commands", "workspace": str(env["tmp"])})
        assert not r.success
        assert "不存在" in r.error

    async def test_unresolvable_workspace_fails_closed(
        self, env: dict[str, Any]
    ) -> None:
        empty = env["tmp"] / "empty_ws"
        empty.mkdir()
        r = await _tool().execute({"method": "engine.commands", "workspace": str(empty)})
        assert not r.success
        assert "project.godot" in r.error

    def test_definition_shape(self) -> None:
        """工具定义：EXECUTION 分类、全层级、workspace 为服务端注入参数。"""
        mod = _load_module()
        definition = mod.GodotRunTool.get_tool_definition()
        assert definition.name == "godot_run"
        assert definition.category == mod.ToolCategory.EXECUTION
        assert definition.level == mod.ToolLevel.ALL
        assert definition.injected_params == ["workspace"]
        assert "method" in definition.input_schema.get("required", [])
        # workspace 由 param_inject 服务端注入，不暴露给 LLM
        assert "workspace" not in definition.input_schema.get("properties", {})


class TestEditorAutostart:
    """编辑器未开自动拉起：status 确认 closed/崩溃恢复才拉、探活后重试、严禁第二实例。"""

    @pytest.fixture
    def autostart_env(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Any]:
        """自动拉起环境：假 serve 首调不可达、status 脚本化、拉起只记录不真启。"""
        mod = _load_module()
        calls_file = env["tmp"] / "calls.log"
        calls_file.write_text("", encoding="utf-8")
        monkeypatch.setenv("FAKE_UNREACHABLE_FIRST", "1")
        monkeypatch.setenv("FAKE_CALLS_FILE", str(calls_file))
        monkeypatch.setenv("GODOT_EDITOR_BIN", sys.executable)
        monkeypatch.setattr(mod, "_AUTOSTART_POLL_INTERVAL", 0.05)
        monkeypatch.setattr(mod, "_AUTOSTART_WAIT_SECONDS", 3.0)
        monkeypatch.setattr(mod, "_LAUNCHED_PROJECTS", set())
        launched: list[list[str]] = []

        def _fake_launch(argv: list[str]) -> None:
            launched.append(argv)

        monkeypatch.setattr(mod, "_launch_editor_process", _fake_launch)
        monkeypatch.setattr(mod, "_editor_status", lambda bin_path, project: "closed")
        return {**env, "calls_file": calls_file, "launched": launched}

    async def test_unreachable_autostarts_and_retries(
        self, autostart_env: dict[str, Any]
    ) -> None:
        """首调不可达 → 拉起（--path/--editor）→ 探活 → 重试成功。"""
        proj = autostart_env["tmp"] / "projE"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        r = await _tool().execute({"method": "scene.tree", "workspace": str(proj)})
        assert r.success, r.error
        assert r.output["method"] == "scene.tree"
        assert len(autostart_env["launched"]) == 1
        argv = autostart_env["launched"][0]
        assert "--path" in argv and "--editor" in argv and str(proj) in argv

    async def test_no_editor_bin_fails_with_guidance(
        self, autostart_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 GODOT_EDITOR_BIN 且 PATH 无 godot：失败并指名设置项。"""
        mod = _load_module()
        monkeypatch.delenv("GODOT_EDITOR_BIN")
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        proj = autostart_env["tmp"] / "projF"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        r = await _tool().execute({"method": "scene.tree", "workspace": str(proj)})
        assert not r.success
        assert "GODOT_EDITOR_BIN" in r.error

    async def test_editor_already_running_never_relaunches(
        self, autostart_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status=running（既有实例）：绝不拉起；持续不可达按超时失败。"""
        mod = _load_module()
        monkeypatch.setattr(mod, "_editor_status", lambda bin_path, project: "running")
        # 假 serve 恒不可达（首个调用之外也报不可达）
        monkeypatch.delenv("FAKE_UNREACHABLE_FIRST")
        monkeypatch.setenv("FAKE_ALWAYS_UNREACHABLE", "1")
        proj = autostart_env["tmp"] / "projG"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        r = await _tool().execute({"method": "scene.tree", "workspace": str(proj)})
        assert not r.success
        assert "超时" in r.error
        assert autostart_env["launched"] == []

    async def test_crashed_editor_relaunches_once(
        self, autostart_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status=crashed（discovery 残留、原进程已死）：允许崩溃恢复拉起一次并重试成功。"""
        mod = _load_module()
        monkeypatch.setattr(mod, "_editor_status", lambda bin_path, project: "crashed")
        proj = autostart_env["tmp"] / "projH"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        r = await _tool().execute({"method": "scene.tree", "workspace": str(proj)})
        assert r.success, r.error
        assert r.output["method"] == "scene.tree"
        assert len(autostart_env["launched"]) == 1
        argv = autostart_env["launched"][0]
        assert "--path" in argv and "--editor" in argv and str(proj) in argv

    async def test_crashed_relaunch_happens_only_once(
        self, autostart_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """崩溃恢复同进程只拉一次：已拉起过仍不可达时不重复拉起，按超时失败。"""
        mod = _load_module()
        monkeypatch.setattr(mod, "_editor_status", lambda bin_path, project: "crashed")
        monkeypatch.delenv("FAKE_UNREACHABLE_FIRST")
        monkeypatch.setenv("FAKE_ALWAYS_UNREACHABLE", "1")
        proj = autostart_env["tmp"] / "projI"
        proj.mkdir()
        (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        r = await _tool().execute({"method": "scene.tree", "workspace": str(proj)})
        assert not r.success
        assert "超时" in r.error
        assert len(autostart_env["launched"]) == 1
