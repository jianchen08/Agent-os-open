# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""project_manager 项目创建/删除事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12 异步阻塞审计 A-5）：``ProjectCreateTool.execute``（async）
直调 ``project_registry.ensure_project_registered``（``git init`` timeout 60s）与
``_apply_init_recipe``（``git init``/``_git`` ×4 timeout 60s + addon ``copytree``）
以及 ``server.project_remove_folder`` / ``project_purge_legacy_dirs`` 的
``rmtree``——全部同步阻塞体跑在 light 合宿宿主事件循环上，最坏 240s+，超
watchdog 阈值即整组 ``os._exit(1)``。

锁面（断行为）：
- 出生初始化（addon 整树复制 + git init/commit）出事件循环；
- 项目登记前的 ``git init``（registry 侧）出事件循环；
- 删项目文件夹 / 清遗留容器目录的 ``rmtree`` 出事件循环；
- 返回语义不变：登记成功信封、init 报告（installed/committed）、removed 布尔。

subprocess/copytree/rmtree 只注入延迟（真实实现仍执行），git 走真实子进程。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker

pytestmark = pytest.mark.unit

# 共享根自举（plugins/shared/ —— project_manager 包所在；包限定名导入免串扰）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from project_manager import project_create_tool  # noqa: E402

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_server() -> Any:
    """按唯一模块名装载插件 server.py（端点分发面：rmtree 调用点所在）。"""
    spec = importlib.util.spec_from_file_location(
        "project_manager_loop_liveness_server", str(_PLUGIN_DIR / "server.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr(
        "project_manager.project_registry.workspace_base_dir", lambda: ws_base
    )
    # 白名单隔离（与 test_project_create 先例同款）：不隔离会读开发者本机
    # 用户空间 config/users/{tenant}/project_whitelist.yaml——显式 target 在
    # tmp_path（ws_base 兄弟目录）不在本机名单/被 patch 的 ws 根下即被拒，
    # 测试随机器而红。隔离后 tmp_path 全域恒过闸。
    monkeypatch.setattr(
        "project_manager.project_registry.load_zone_sections",
        lambda **k: {"entries": [str(tmp_path)], "read_deny": [], "read_allow": []},
    )
    return {"tasks_root": tasks_root, "ws_base": ws_base}


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：创建链（git init / addon copytree / 出生提交）出事件循环
# ============================================================


async def test_create_init_recipe_offloads_addon_copy_and_git(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出生初始化整段（addon 整树复制 0.3s + 真实 git init/commit）期间心跳
    持续打点；init 报告照常（addon 装好、出生提交落地）。"""
    addons_src = tmp_path / "addons_src" / "addon1"
    (addons_src / "plugin.cfg").parent.mkdir(parents=True)
    (addons_src / "plugin.cfg").write_text("[plugin]\n", encoding="utf-8")
    recipe = {
        "project_file": "demo.txt",
        "scaffold": "{title}\n",
        "addons_source": str(tmp_path / "addons_src"),
        "addons": ["addon1"],
    }
    monkeypatch.setattr(
        project_create_tool, "_load_type_config", lambda: {"t1": recipe}
    )
    real_copytree = shutil.copytree

    def _slow_copytree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", _slow_copytree)
    target = tmp_path / "proj"
    target.mkdir()
    tool = project_create_tool.ProjectCreateTool()

    async with LoopTicker() as ticker:
        result = await tool.execute(
            {"goal": "带配方项目", "path": str(target), "project_type": "t1"}
        )

    assert ticker.max_gap < MAX_TICK_GAP
    assert result.success, result.error
    init = result.output["init"]
    assert init["addons_installed"] == ["addon1"]
    assert init["committed"] is True
    assert (target / "addons" / "addon1" / "plugin.cfg").is_file()


async def test_create_registry_git_init_off_loop(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记前的 ``git init``（registry 侧 timeout 60s）出事件循环；登记仍成功。"""
    calls: list[list[str]] = []

    def _slow_run(*args: Any, **kwargs: Any) -> Any:
        calls.append(list(args[0]))
        time.sleep(0.25)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _slow_run)
    target = tmp_path / "plain_proj"
    target.mkdir()
    tool = project_create_tool.ProjectCreateTool()

    async with LoopTicker() as ticker:
        result = await tool.execute({"goal": "普通项目", "path": str(target)})

    assert ticker.max_gap < MAX_TICK_GAP
    assert result.success, result.error
    assert result.output["created"] is True
    assert calls, "git init 必须被调用（延迟注入不改变调用面）"


# ============================================================
# 主锁：删除链（rmtree）出事件循环
# ============================================================


async def test_remove_folder_offloads_rmtree(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删项目文件夹（rmtree 0.3s）期间心跳持续打点，removed 语义不变。"""
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "sub" / "a.txt").write_text("x", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def _slow_rmtree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", _slow_rmtree)
    server = _load_server()

    async with LoopTicker() as ticker:
        out = await server.project_remove_folder(str(victim))

    assert ticker.max_gap < MAX_TICK_GAP
    assert out == {"removed": True}
    assert not victim.exists()


async def test_purge_legacy_dirs_offloads_rmtree(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """清 ``container_*`` 遗留目录（rmtree 0.3s）期间心跳持续打点，计数照常。"""
    ws_base = env["ws_base"]
    (ws_base / "container_task1" / "sub").mkdir(parents=True)
    (ws_base / "container_task2").mkdir(parents=True)
    real_rmtree = shutil.rmtree

    def _slow_rmtree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", _slow_rmtree)
    server = _load_server()

    async with LoopTicker() as ticker:
        out = await server.project_purge_legacy_dirs()

    assert ticker.max_gap < MAX_TICK_GAP
    assert out == {"removed_dirs": 2}


async def test_create_service_offloads_registration(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务面 ``project.create``（capability 直连）同链下放：登记前 git init
    0.25s 期间心跳持续打点。"""

    def _slow_run(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.25)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _slow_run)
    target = tmp_path / "svc_proj"
    target.mkdir()
    server = _load_server()

    async with LoopTicker() as ticker:
        out = await server.project_create_service(title="服务面项目", path=str(target))

    assert ticker.max_gap < MAX_TICK_GAP
    assert out["created"] is True
    assert out["project"]["title"] == "服务面项目"
