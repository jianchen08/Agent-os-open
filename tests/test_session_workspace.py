"""会话工作空间与隔离模式测试。

覆盖：
- validate_workspace_path：会话/任务共用的工作空间路径安全性校验
  （磁盘根 / 系统目录 / 工作空间根 / 正常路径）
- normalize_isolation_mode：会话隔离模式归一化（配置默认 non_isolated）
"""
import os

import pytest

from isolation.workspace import validate_workspace_path

# ============================================================================
# 1. 公共校验函数 validate_workspace_path
# ============================================================================


class TestValidateWorkspacePath:
    """工作空间路径安全性校验。"""

    def test_empty_returns_none(self):
        """空值/None 直接通过（调用方按未指定处理）。"""
        assert validate_workspace_path("") is None

    def test_normal_windows_path(self):
        """正常 Windows 项目路径校验通过。"""
        if os.name == "nt":
            assert validate_workspace_path(r"D:\myproject\demo-app") is None

    def test_normal_unix_path(self):
        """正常 Unix 路径校验通过。"""
        assert validate_workspace_path("/home/user/projects/demo") is None

    def test_disk_root_rejected(self):
        """磁盘根目录（Windows）被拒绝。"""
        if os.name == "nt":
            assert validate_workspace_path("D:\\") is not None

    def test_unix_root_rejected(self):
        """根目录（Unix）被拒绝。"""
        if os.name != "nt":
            assert validate_workspace_path("/") is not None

    @pytest.mark.parametrize(
        "path",
        [
            r"C:\Windows",
            r"C:\Windows\System32",
            r"C:\Program Files",
            r"C:\Users",
        ],
    )
    def test_windows_system_dirs_rejected(self, path):
        """Windows 系统关键目录被拒绝。"""
        if os.name == "nt":
            assert validate_workspace_path(path) is not None

    @pytest.mark.parametrize(
        "path",
        ["/etc", "/bin", "/usr", "/var", "/tmp", "/root", "/home", "/opt"],
    )
    def test_unix_system_dirs_rejected(self, path):
        """Unix 系统关键目录被拒绝。"""
        if os.name != "nt":
            assert validate_workspace_path(path) is not None

    def test_workspace_config_root_rejected(self):
        """配置的工作空间根目录被拒绝（.ai_workspaces）。"""
        from isolation.workspace import get_workspace_config_root

        ws_root = get_workspace_config_root()
        assert validate_workspace_path(ws_root) is not None


# ============================================================================
# 2. 会话隔离模式归一化
# ============================================================================


class TestNormalizeIsolationMode:
    """会话隔离模式归一化。"""

    def test_explicit_values_passthrough(self):
        from infrastructure.session.session_workspace import normalize_isolation_mode

        assert normalize_isolation_mode("isolated") == "isolated"
        assert normalize_isolation_mode("non_isolated") == "non_isolated"

    def test_none_falls_back_to_default(self):
        """None 回退到配置 session_default_level（默认 non_isolated）。"""
        from infrastructure.session.session_workspace import normalize_isolation_mode

        assert normalize_isolation_mode(None) == "non_isolated"

    def test_invalid_value_falls_back(self):
        """无效值回退到默认 non_isolated。"""
        from infrastructure.session.session_workspace import normalize_isolation_mode

        assert normalize_isolation_mode("container") == "non_isolated"

    def test_config_overrides_default(self):
        """配置 session_default_level=isolated 时，None 归一化为 isolated。"""
        from unittest.mock import patch

        from infrastructure.session.session_workspace import normalize_isolation_mode

        fake_cfg = {"coordinator": {"session_default_level": "isolated"}}
        with patch(
            "config.config_center.get_config_center",
            return_value=type("CC", (), {"get": lambda _self, _k: fake_cfg})(),
        ):
            assert normalize_isolation_mode(None) == "isolated"


# ============================================================================
# 3. SessionWorkspaceService
# ============================================================================


class TestSessionWorkspaceService:
    """会话工作空间服务。"""

    def test_validate_workspace(self):
        from infrastructure.session.session_workspace import SessionWorkspaceService

        assert SessionWorkspaceService.validate_workspace("") is not None
        assert SessionWorkspaceService.validate_workspace(r"D:\myproject\demo-app") is None or os.name != "nt"

    @pytest.mark.asyncio
    async def test_get_or_create_session_container_missing_workspace(self):
        """workspace 为空时返回 None（不创建容器）。"""
        from infrastructure.session.session_workspace import SessionWorkspaceService

        assert await SessionWorkspaceService.get_or_create_session_container("") is None

    @pytest.mark.asyncio
    async def test_get_or_create_session_container_delegates_to_manager(self):
        """容器获取委托 IsolationManager（幂等复用，挂载由 provider 完成）。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from infrastructure.session.session_workspace import SessionWorkspaceService

        fake_env = MagicMock()
        fake_env.env_id = "cua-my-project"
        fake_manager = AsyncMock()
        fake_manager.get_or_create_environment = AsyncMock(return_value=fake_env)

        with patch("isolation.manager.get_isolation_manager", AsyncMock(return_value=fake_manager)):
            env_id = await SessionWorkspaceService.get_or_create_session_container(r"D:\myproject\demo-app")

        assert env_id == "cua-my-project"
        fake_manager.get_or_create_environment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_destroy_session_container_idempotent(self):
        """销毁委托 IsolationManager.destroy_environment（幂等）。"""
        from unittest.mock import AsyncMock, patch

        from infrastructure.session.session_workspace import SessionWorkspaceService

        fake_manager = AsyncMock()
        fake_manager.destroy_environment = AsyncMock(return_value=True)
        fake_manager._workspace_to_container_name = lambda ws, _tid: f"cua-{ws.split('/')[-1]}"

        with patch("isolation.manager.get_isolation_manager", AsyncMock(return_value=fake_manager)):
            await SessionWorkspaceService.destroy_session_container(r"D:\myproject\demo-app")

        fake_manager.destroy_environment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_destroy_session_container_noop_without_workspace(self):
        """workspace 为空时不触发任何销毁。"""
        from unittest.mock import AsyncMock, patch

        from infrastructure.session.session_workspace import SessionWorkspaceService

        fake_manager = AsyncMock()
        with patch("isolation.manager.get_isolation_manager", AsyncMock(return_value=fake_manager)):
            await SessionWorkspaceService.destroy_session_container("")

        fake_manager.destroy_environment.assert_not_awaited()
