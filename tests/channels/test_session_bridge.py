"""跨通道会话桥接测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path


from channels.gateway.session_bridge import SessionBridge


class TestSessionBridge:
    """SessionBridge 测试。"""

    def setup_method(self) -> None:
        """每个测试方法前创建 SessionBridge 实例。"""
        self.tmpdir = tempfile.mkdtemp()
        self.bridge = SessionBridge(storage_path=Path(self.tmpdir))

    def teardown_method(self) -> None:
        """清理临时目录。"""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_or_create_session_new_user(self) -> None:
        """新用户首次获取会话，应创建新会话。"""
        session_id = self.bridge.get_or_create_session(
            unified_user_id="feishu:ou_001",
            channel_type="feishu",
        )
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    def test_get_or_create_session_same_user_same_channel(self) -> None:
        """同一用户同一渠道再次获取，应返回相同 session_id。"""
        sid1 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid2 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        assert sid1 == sid2

    def test_get_or_create_session_cross_channel(self) -> None:
        """同一用户不同渠道应返回相同 session_id（跨通道共享）。"""
        sid_feishu = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid_dingtalk = self.bridge.get_or_create_session("feishu:ou_001", "dingtalk")
        assert sid_feishu == sid_dingtalk

    def test_get_active_channel_default(self) -> None:
        """新用户活跃通道应为注册时的通道。"""
        self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        active = self.bridge.get_active_channel("feishu:ou_001")
        assert active == "feishu"

    def test_get_active_channel_unknown_user(self) -> None:
        """未知用户应返回空字符串。"""
        active = self.bridge.get_active_channel("unknown:user")
        assert active == ""

    def test_switch_channel(self) -> None:
        """测试切换活跃通道。"""
        self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        self.bridge.switch_channel("feishu:ou_001", "dingtalk")
        active = self.bridge.get_active_channel("feishu:ou_001")
        assert active == "dingtalk"

    def test_switch_channel_unknown_user(self) -> None:
        """未知用户切换通道应优雅处理。"""
        # 不应抛出异常
        self.bridge.switch_channel("unknown:user", "dingtalk")

    def test_persistence_and_restore(self) -> None:
        """测试持久化和恢复。"""
        sid = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        self.bridge.switch_channel("feishu:ou_001", "dingtalk")

        # 创建新 bridge 实例从同一目录恢复
        bridge2 = SessionBridge(storage_path=Path(self.tmpdir))
        sid2 = bridge2.get_or_create_session("feishu:ou_001", "feishu")
        assert sid2 == sid
        assert bridge2.get_active_channel("feishu:ou_001") == "dingtalk"

    def test_different_users_separate_sessions(self) -> None:
        """不同用户应有不同的会话。"""
        sid1 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid2 = self.bridge.get_or_create_session("feishu:ou_002", "feishu")
        assert sid1 != sid2

    def test_bridge_without_storage(self) -> None:
        """无存储路径时也能正常工作。"""
        bridge = SessionBridge()
        sid = bridge.get_or_create_session("feishu:ou_001", "feishu")
        assert isinstance(sid, str)
        assert bridge.get_active_channel("feishu:ou_001") == "feishu"
