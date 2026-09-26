#!/usr/bin/env python3
# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""第三批迁移模块（scene + workspace）导入验证测试。connectors 树已随 5037bd01a 整体退役，其迁移验证类同批移除。

验证：
1. 老代码文件已复制到插件目录（平铺）
2. 导入路径已适配（from .xxx → from xxx）
3. server.py 的 MCP 工具注册结构正确
4. plugin.json 格式有效

[来源: docs/working/module_migration_plan.md §六 P2 迁移验收标准]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---- 路径常量 ----
# 被测插件树：plugins/shared/system（scene / workspace 所在）
SYSTEM_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "system"
SCENE_DIR = SYSTEM_DIR / "scene"
WORKSPACE_DIR = SYSTEM_DIR / "workspace"


def _purge_modules(prefix: str) -> None:
    """清理 sys.modules 中指定前缀的模块缓存，防止同名文件跨目录污染。"""
    to_remove = [
        mod for mod in sys.modules
        if mod == prefix or mod.startswith(prefix + ".")
    ]
    for mod in to_remove:
        del sys.modules[mod]


# ============================================================
# scene 模块
# ============================================================

class TestSceneMigration:
    """scene 模块迁移验证。"""

    def test_models_copied(self) -> None:
        """models.py 已复制。"""
        assert (SCENE_DIR / "models.py").exists()

    def test_manager_copied(self) -> None:
        """manager.py 已复制。"""
        assert (SCENE_DIR / "manager.py").exists()

    def test_persistence_copied(self) -> None:
        """persistence.py 已复制。"""
        assert (SCENE_DIR / "persistence.py").exists()

    def test_templates_copied(self) -> None:
        """templates.py 已复制。"""
        assert (SCENE_DIR / "templates.py").exists()

    def test_plugin_json_exists_and_valid(self) -> None:
        """plugin.json 存在且格式有效。"""
        json_path = SCENE_DIR / "plugin.json"
        assert json_path.exists()
        data: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["id"] == "scene_service"
        assert data["plugin_type"] == "system"
        assert data["entry"] == "python server.py"  # 仓库统一约定为 python（85 个 plugin.json 一致）
        # D.6 槽位拆分：服务方法声明在 services
        assert len(data["capabilities"]["services"]) >= 5

    def test_server_py_exists(self) -> None:
        """server.py 存在。"""
        assert (SCENE_DIR / "server.py").exists()

    def test_models_imports(self) -> None:
        """models.py 可导入。"""
        _purge_modules("models")
        sys.path.insert(0, str(SCENE_DIR))
        try:
            from models import Scene, SceneLayoutType  # noqa: F811

            assert SceneLayoutType.GRID.value == "grid"
            scene = Scene(name="test")
            assert scene.name == "test"
        finally:
            if str(SCENE_DIR) in sys.path:
                sys.path.remove(str(SCENE_DIR))
            _purge_modules("models")
            _purge_modules("persistence")
            _purge_modules("templates")
            _purge_modules("manager")

    def test_templates_imports(self) -> None:
        """templates.py 可导入，预设模板存在。"""
        _purge_modules("models")
        _purge_modules("templates")
        sys.path.insert(0, str(SCENE_DIR))
        try:
            from templates import list_templates  # noqa: F811

            templates = list_templates()
            assert len(templates) >= 3
        finally:
            if str(SCENE_DIR) in sys.path:
                sys.path.remove(str(SCENE_DIR))
            _purge_modules("models")
            _purge_modules("templates")

    def test_manager_imports(self) -> None:
        """SceneManager 可导入。"""
        _purge_modules("models")
        _purge_modules("persistence")
        _purge_modules("templates")
        _purge_modules("manager")
        sys.path.insert(0, str(SCENE_DIR))
        try:
            from manager import SceneManager  # noqa: F811

            assert SceneManager is not None
        finally:
            if str(SCENE_DIR) in sys.path:
                sys.path.remove(str(SCENE_DIR))
            _purge_modules("models")
            _purge_modules("persistence")
            _purge_modules("templates")
            _purge_modules("manager")


# ============================================================
# workspace 模块
# ============================================================

class TestWorkspaceMigration:
    """workspace 模块迁移验证。"""

    def test_models_copied(self) -> None:
        """models.py 已复制。"""
        assert (WORKSPACE_DIR / "models.py").exists()

    def test_workspace_service_copied(self) -> None:
        """workspace_service.py 已复制。"""
        assert (WORKSPACE_DIR / "workspace_service.py").exists()

    def test_plugin_json_exists_and_valid(self) -> None:
        """plugin.json 存在且格式有效。"""
        json_path = WORKSPACE_DIR / "plugin.json"
        assert json_path.exists()
        data: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["id"] == "workspace_service"
        assert data["plugin_type"] == "system"
        assert data["entry"] == "python server.py"  # 仓库统一约定为 python（85 个 plugin.json 一致）
        assert len(data["capabilities"]["services"]) >= 3

    def test_server_py_exists(self) -> None:
        """server.py 存在。"""
        assert (WORKSPACE_DIR / "server.py").exists()

    def test_models_imports(self) -> None:
        """models.py 可导入。"""
        _purge_modules("models")
        sys.path.insert(0, str(WORKSPACE_DIR))
        try:
            from models import FileTreeNode, Workspace  # noqa: F811

            node = FileTreeNode(name="test.py", type="file", path="test.py")
            assert node.name == "test.py"
            ws = Workspace(container_task_id="task-1")
            assert ws.container_task_id == "task-1"
        finally:
            if str(WORKSPACE_DIR) in sys.path:
                sys.path.remove(str(WORKSPACE_DIR))
            _purge_modules("models")

    def test_workspace_service_imports(self) -> None:
        """WorkspaceService 可导入且可实例化。"""
        _purge_modules("models")
        _purge_modules("workspace_service")
        _purge_modules("workspace")
        # 平铺串扰：tasks/isolation 目录的 workspace.py 裸模块会压制本目录的
        # workspace 命名空间包（PathFinder 普通模块优先于 namespace portion）；
        # system/ 在路径上才能把 workspace/ 解析为命名空间包供 workspace.models 用。
        _conflicts = [
            d for d in (SYSTEM_DIR / "tasks", SYSTEM_DIR / "isolation")
            if str(d) in sys.path
        ]
        # 去重移除：并发/串行守卫可能重复插入同一目录（如 suites/core 与
        # cascade 的 tasks 路径守卫），单个 remove 只删第一个副本。
        for d in _conflicts:
            while str(d) in sys.path:
                sys.path.remove(str(d))
        sys.path.insert(0, str(WORKSPACE_DIR))
        sys.path.insert(0, str(SYSTEM_DIR))
        try:
            from workspace_service import WorkspaceService  # noqa: F811

            svc = WorkspaceService()
            assert svc is not None
        finally:
            for p in (str(WORKSPACE_DIR), str(SYSTEM_DIR)):
                if p in sys.path:
                    sys.path.remove(p)
            for d in _conflicts:
                sys.path.insert(0, str(d))
            _purge_modules("models")
            _purge_modules("workspace_service")
            _purge_modules("workspace")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
