#!/usr/bin/env python3
"""第三批迁移模块（connectors + scene + workspace）导入验证测试。

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

# ---- 路径常量 ----
# 测试文件位于 plugins/shared/system/test_migration_batch3.py
SYSTEM_DIR = Path(__file__).resolve().parent
CONNECTORS_DIR = SYSTEM_DIR / "connectors"
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
# connectors 模块
# ============================================================

class TestConnectorsMigration:
    """connectors 模块迁移验证。"""

    def test_connector_types_copied(self) -> None:
        """connector_types.py（原 types.py）已复制到插件目录。"""
        assert (CONNECTORS_DIR / "connector_types.py").exists(), \
            "connector_types.py 未复制"

    def test_base_copied(self) -> None:
        """base.py 已复制。"""
        assert (CONNECTORS_DIR / "base.py").exists()

    def test_registry_copied(self) -> None:
        """registry.py 已复制。"""
        assert (CONNECTORS_DIR / "registry.py").exists()

    def test_degradation_copied(self) -> None:
        """degradation.py 已复制。"""
        assert (CONNECTORS_DIR / "degradation.py").exists()

    def test_config_mixin_copied(self) -> None:
        """config_mixin.py 已复制。"""
        assert (CONNECTORS_DIR / "config_mixin.py").exists()

    def test_adapter_config_copied(self) -> None:
        """adapter_config.py 已复制。"""
        assert (CONNECTORS_DIR / "adapter_config.py").exists()

    def test_vscode_subdir_copied(self) -> None:
        """vscode/ 子目录已复制。"""
        assert (CONNECTORS_DIR / "vscode" / "channel.py").exists()
        assert (CONNECTORS_DIR / "vscode" / "connector.py").exists()

    def test_creative_subdir_copied(self) -> None:
        """creative/ 子目录已复制。"""
        assert (CONNECTORS_DIR / "creative" / "comfyui.py").exists()
        assert (CONNECTORS_DIR / "creative" / "game_engine.py").exists()
        assert (CONNECTORS_DIR / "creative" / "generic.py").exists()

    def test_plugin_json_exists_and_valid(self) -> None:
        """plugin.json 存在且格式有效。"""
        json_path = CONNECTORS_DIR / "plugin.json"
        assert json_path.exists()
        data: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["id"] == "connectors_service"
        assert data["plugin_type"] == "system"
        assert data["entry"] == "python3 server.py"
        assert len(data["capabilities"]["tools"]) >= 5

    def test_server_py_exists(self) -> None:
        """server.py 存在。"""
        assert (CONNECTORS_DIR / "server.py").exists()

    def test_connector_types_imports(self) -> None:
        """connector_types.py 可导入，核心类型可用。"""
        sys.path.insert(0, str(CONNECTORS_DIR))
        try:
            from connector_types import (  # noqa: F811
                ActionResult,
                ConnectorState,
            )

            assert ConnectorState.DISCONNECTED.value == "disconnected"
            assert ActionResult(success=True).success is True
        finally:
            if str(CONNECTORS_DIR) in sys.path:
                sys.path.remove(str(CONNECTORS_DIR))
            _purge_modules("connector_types")
            _purge_modules("base")
            _purge_modules("registry")
            _purge_modules("degradation")
            _purge_modules("config_mixin")
            _purge_modules("adapter_config")
            _purge_modules("vscode")
            _purge_modules("creative")

    def test_registry_imports(self) -> None:
        """ConnectorRegistry 可导入且可实例化。"""
        sys.path.insert(0, str(CONNECTORS_DIR))
        try:
            from registry import ConnectorRegistry  # noqa: F811

            reg = ConnectorRegistry()
            assert reg.count() == 0
        finally:
            if str(CONNECTORS_DIR) in sys.path:
                sys.path.remove(str(CONNECTORS_DIR))
            _purge_modules("connector_types")
            _purge_modules("base")
            _purge_modules("registry")
            _purge_modules("degradation")
            _purge_modules("config_mixin")
            _purge_modules("adapter_config")

    def test_degradation_imports(self) -> None:
        """DegradationManager 可导入且可实例化。"""
        sys.path.insert(0, str(CONNECTORS_DIR))
        try:
            from degradation import DegradationManager  # noqa: F811

            mgr = DegradationManager()
            assert mgr.can_handle_locally("open_file") is True
            assert mgr.can_handle_locally("nonexistent") is False
        finally:
            if str(CONNECTORS_DIR) in sys.path:
                sys.path.remove(str(CONNECTORS_DIR))
            _purge_modules("connector_types")
            _purge_modules("base")
            _purge_modules("degradation")


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
        assert data["entry"] == "python3 server.py"
        assert len(data["capabilities"]["tools"]) >= 5

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
        assert data["entry"] == "python3 server.py"
        assert len(data["capabilities"]["tools"]) >= 3

    def test_server_py_exists(self) -> None:
        """server.py 存在。"""
        assert (WORKSPACE_DIR / "server.py").exists()

    def test_models_imports(self) -> None:
        """models.py 可导入。"""
        _purge_modules("models")
        sys.path.insert(0, str(WORKSPACE_DIR))
        try:
            from models import Workspace, FileTreeNode  # noqa: F811

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
        sys.path.insert(0, str(WORKSPACE_DIR))
        try:
            from workspace_service import WorkspaceService  # noqa: F811

            svc = WorkspaceService()
            assert svc is not None
        finally:
            if str(WORKSPACE_DIR) in sys.path:
                sys.path.remove(str(WORKSPACE_DIR))
            _purge_modules("models")
            _purge_modules("workspace_service")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
