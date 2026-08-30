# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""project_create 工具测试（项目创建与任务解耦：无执行者，独立入口）。

覆盖：创建成功（文件夹 + git init + 登记）；同路径幂等复用（created=false）；
缺 goal 拒绝；显式目录指定；登记带 session_id/user_id 署名。
"""

from __future__ import annotations

import importlib.util
import os
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
