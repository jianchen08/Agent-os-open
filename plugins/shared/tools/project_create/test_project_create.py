# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""project_create 工具测试（项目创建与任务解耦：无执行者，独立入口）。

覆盖：创建成功（文件夹 + git init + 登记）；同路径幂等复用（created=false）；
缺 goal 拒绝；显式目录指定；登记带 session_id/user_id 署名。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 共享层自举（plugins/shared/ —— project_registry 所在）
_SHARED_ROOT = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)


def _load_module() -> Any:
    """动态加载 tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "project_create_tool_test"
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
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_registry.workspace_base_dir", lambda: ws_base)
    return {"tasks_root": tasks_root, "ws_base": ws_base}


def _tool() -> Any:
    mod = _load_module()
    return mod.ProjectCreateTool()


class TestProjectCreate:
    async def test_creates_project_and_registers(self, env: dict[str, Any]) -> None:
        """创建成功：返回 project_id/title/path/created=True，登记落盘。"""
        tool = _tool()
        r = await tool.execute({"goal": "新项目", "user_id": "user-1", "session_id": "sess-1"})
        assert r.success, r.error
        assert len(r.output["project_id"]) == 12
        assert r.output["title"] == "新项目"
        assert r.output["created"] is True
        assert r.output["path"].endswith("projects\\新项目") or r.output["path"].endswith(
            "projects/新项目"
        )
        # 登记落盘（独立实例可读）
        from project_registry import ProjectRegistry

        reg = ProjectRegistry()
        loaded = reg.get(r.output["project_id"])
        assert loaded is not None
        assert loaded.session_id == "sess-1"
        assert loaded.submitted_by == "user-1"

    async def test_same_path_reuses_existing(self, env: dict[str, Any]) -> None:
        """同路径幂等复用：created=False 且 id 不变。"""
        target = env["ws_base"] / "proj"
        target.mkdir(parents=True)
        tool = _tool()
        r1 = await tool.execute({"goal": "项目A", "path": str(target)})
        r2 = await tool.execute({"goal": "项目B", "path": str(target)})
        assert r1.success and r2.success
        assert r1.output["created"] is True
        assert r2.output["created"] is False
        assert r2.output["project_id"] == r1.output["project_id"]

    async def test_missing_goal_rejected(self, env: dict[str, Any]) -> None:
        """缺 goal → 失败信封（不建文件夹不登记）。"""
        tool = _tool()
        r = await tool.execute({})
        assert not r.success
        assert "goal" in r.error
        from project_registry import load_project_paths

        assert load_project_paths() == {}

    async def test_explicit_nongit_dir_auto_git_init(self, env: dict[str, Any]) -> None:
        """显式已有非 git 目录：自动 git init 后登记（不拒绝不删文件）。"""
        target = env["ws_base"] / "existing"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("data", encoding="utf-8")
        tool = _tool()
        r = await tool.execute({"goal": "已有目录", "path": str(target)})
        assert r.success, r.error
        assert (target / "keep.txt").read_text(encoding="utf-8") == "data"
        assert (target / ".git").is_dir()

    def test_definition_shape(self) -> None:
        """工具定义：TASK 分类、L1/L2 层级、注入参数声明、schema 必填 goal。"""
        mod = _load_module()
        definition = mod.ProjectCreateTool.get_tool_definition()
        assert definition.name == "project_create"
        assert definition.category == mod.ToolCategory.TASK
        assert definition.level == mod.ToolLevel.L1_L2_ONLY
        assert definition.injected_params == ["user_id", "session_id"]
        assert "goal" in definition.input_schema.get("required", [])


class TestManifestSchemaLockstep:
    """plugin.json 声明 ↔ 代码实现 schema 锁步（G2 一致性闸的测试面）。

    input_schema 真值 = config/tools/project_create.yaml（类型映射说明与枚举
    由配置生成），manifest 是生成结果的同步投影：配置改动不同步 manifest =
    本测试红 + G2 启动 SchemaMismatch 拒绝，双保险防漂移。
    """

    def test_declared_input_schema_matches_implementation(self) -> None:
        import json

        manifest = json.loads(
            (_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8")
        )
        declared = manifest["capabilities"]["tools"][0]["input_schema"]
        mod = _load_module()

        assert declared == mod._build_input_schema(mod._load_type_config())


def _godot_config(source_root: Path) -> str:
    """隔离测试用的类型配方表（addon 源指向传入目录）。"""
    return (
        "project_types:\n"
        "  godot:\n"
        "    schema_summary: Godot 4 测试配方\n"
        "    detect_file: project.godot\n"
        "    addons_source: " + source_root.as_posix() + "\n"
        "    addons:\n"
        "      - agentos\n"
        "      - godot_mcp\n"
        "    project_file: project.godot\n"
        '    scaffold: "config_version=5\\n\\n[application]\\n\\nconfig/name=\\\"{title}\\\"\\n"\n'
        "    enable_section: editor_plugins\n"
        "    enable_entries:\n"
        "      - res://addons/agentos/plugin.cfg\n"
        "      - res://addons/godot_mcp/plugin.cfg\n"
    )


@pytest.fixture
def godot_env(env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离类型配方：addon 源与配置文件均落 tmp，不触真实仓库 config/hosts。"""
    src_root = tmp_path / "addons_src"
    for name in ("agentos", "godot_mcp"):
        addon_dir = src_root / name
        addon_dir.mkdir(parents=True)
        (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
    cfg = tmp_path / "project_create.yaml"
    cfg.write_text(_godot_config(src_root), encoding="utf-8")
    monkeypatch.setattr(_load_module(), "_TYPE_CONFIG_PATH", cfg)
    return {**env, "cfg": cfg}


class TestProjectTypeInit:
    """项目类型出生初始化：路由（显式/auto）→ 装 addon → 清单脚手架 → 启用合并。"""

    async def test_explicit_godot_installs_addons_and_enables(
        self, godot_env: dict[str, Any]
    ) -> None:
        """显式 godot：双 addon 落位 + 清单脚手架 + enabled 双条目。"""
        target = godot_env["ws_base"] / "g1"
        r = await _tool().execute(
            {"goal": "G游戏", "path": str(target), "project_type": "godot"}
        )
        assert r.success, r.error
        assert r.output["project_type"] == "godot"
        init = r.output["init"]
        assert init["addons_installed"] == ["agentos", "godot_mcp"]
        assert init["project_file_created"] is True
        assert init["enabled_added"] == [
            "res://addons/agentos/plugin.cfg",
            "res://addons/godot_mcp/plugin.cfg",
        ]
        text = (target / "project.godot").read_text(encoding="utf-8")
        assert 'config/name="G游戏"' in text
        assert (
            'enabled=PackedStringArray("res://addons/agentos/plugin.cfg", '
            '"res://addons/godot_mcp/plugin.cfg")' in text
        )
        assert (target / "addons" / "agentos" / "plugin.cfg").is_file()

    async def test_auto_detects_existing_project_and_merges_enabled(
        self, godot_env: dict[str, Any]
    ) -> None:
        """auto：已有 project.godot 即路由；enabled 并集保序（既有在前新增在后）。"""
        target = godot_env["ws_base"] / "g2"
        target.mkdir(parents=True)
        (target / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="已存在"\n'
            '\n[editor_plugins]\n\nenabled=PackedStringArray("res://addons/mine/plugin.cfg")\n',
            encoding="utf-8",
        )
        r = await _tool().execute({"goal": "G2", "path": str(target)})
        assert r.success, r.error
        assert r.output["project_type"] == "godot"
        init = r.output["init"]
        assert init["addons_installed"] == ["agentos", "godot_mcp"]
        assert init["project_file_created"] is False
        assert init["enabled_added"] == [
            "res://addons/agentos/plugin.cfg",
            "res://addons/godot_mcp/plugin.cfg",
        ]
        text = (target / "project.godot").read_text(encoding="utf-8")
        m = re.search(r"enabled=PackedStringArray\(([^)]*)\)", text)
        assert m is not None
        entries = re.findall(r'"([^"]+)"', m.group(1))
        assert entries[0] == "res://addons/mine/plugin.cfg"
        assert len(entries) == len(set(entries)) == 3

    async def test_merges_into_existing_section_without_enabled_key(
        self, godot_env: dict[str, Any]
    ) -> None:
        """清单已有 editor_plugins 段但无 enabled 键：段内补行，不另起重复段。"""
        target = godot_env["ws_base"] / "g7"
        target.mkdir(parents=True)
        (target / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="S"\n\n[editor_plugins]\n\n',
            encoding="utf-8",
        )
        r = await _tool().execute({"goal": "G7", "path": str(target)})
        assert r.success, r.error
        text = (target / "project.godot").read_text(encoding="utf-8")
        assert text.count("[editor_plugins]") == 1
        assert (
            'enabled=PackedStringArray("res://addons/agentos/plugin.cfg", '
            '"res://addons/godot_mcp/plugin.cfg")' in text
        )

    async def test_reuse_rerun_is_idempotent(self, godot_env: dict[str, Any]) -> None:
        """同路径重跑（自愈口）：created=False、零重复安装、零新增启用。"""
        target = godot_env["ws_base"] / "g3"
        r1 = await _tool().execute(
            {"goal": "G3", "path": str(target), "project_type": "godot"}
        )
        r2 = await _tool().execute(
            {"goal": "G3", "path": str(target), "project_type": "godot"}
        )
        assert r1.success and r2.success, r2.error
        assert r2.output["created"] is False
        init2 = r2.output["init"]
        assert init2["addons_installed"] == []
        assert init2["addons_present"] == ["agentos", "godot_mcp"]
        assert init2["project_file_created"] is False
        assert init2["enabled_added"] == []
        assert init2["committed"] is False
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(target), capture_output=True, text=True
        )
        assert len(log.stdout.strip().splitlines()) == 1

    async def test_init_commits_born_state(self, godot_env: dict[str, Any]) -> None:
        """出生提交：初始化产物（addon/清单）入库——worktree 分叉从此有文件。"""
        target = godot_env["ws_base"] / "g8"
        r = await _tool().execute(
            {"goal": "G8", "path": str(target), "project_type": "godot"}
        )
        assert r.success, r.error
        assert r.output["init"]["committed"] is True
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(target), capture_output=True, text=True
        )
        assert len(log.stdout.strip().splitlines()) == 1
        ls = subprocess.run(
            ["git", "ls-files"], cwd=str(target), capture_output=True, text=True
        )
        tracked = ls.stdout.replace("\\", "/")
        assert "addons/agentos/plugin.cfg" in tracked
        assert "addons/godot_mcp/plugin.cfg" in tracked
        assert "project.godot" in tracked

    async def test_unknown_type_fails_closed(self, godot_env: dict[str, Any]) -> None:
        """未声明类型：整体报错且不落任何 addon（禁止静默半装）。"""
        target = godot_env["ws_base"] / "g4"
        r = await _tool().execute(
            {"goal": "G4", "path": str(target), "project_type": "unity"}
        )
        assert not r.success
        assert "未知项目类型" in r.error
        addons = target / "addons"
        assert not addons.is_dir() or not any(addons.iterdir())

    async def test_plain_project_no_routing(self, env: dict[str, Any]) -> None:
        """普通项目（无类型无清单文件）：行为与既有契约一致，init 为 null。"""
        target = env["ws_base"] / "p1"
        r = await _tool().execute({"goal": "普通", "path": str(target)})
        assert r.success, r.error
        assert r.output["project_type"] == ""
        assert r.output["init"] is None

    async def test_missing_addon_source_fails(self, godot_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """addon 安装源缺失：报错指名路径（fail-closed），不假成功。"""
        broken = tmp_path / "broken.yaml"
        broken.write_text(_godot_config(tmp_path / "nonexistent_src"), encoding="utf-8")
        monkeypatch.setattr(_load_module(), "_TYPE_CONFIG_PATH", broken)
        r = await _tool().execute(
            {"goal": "G5", "path": str(godot_env["ws_base"] / "g5"), "project_type": "godot"}
        )
        assert not r.success
        assert "addon 安装源缺失" in r.error

    async def test_missing_config_with_explicit_type_fails(
        self, env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置缺失 + 显式指定类型：报错（禁止静默忽略调用方意图）。"""
        monkeypatch.setattr(_load_module(), "_TYPE_CONFIG_PATH", tmp_path / "absent.yaml")
        r = await _tool().execute(
            {"goal": "G6", "path": str(env["ws_base"] / "g6"), "project_type": "godot"}
        )
        assert not r.success
        assert "未知项目类型" in r.error and "配置缺失" in r.error
