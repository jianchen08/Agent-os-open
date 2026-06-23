"""Round 3 测试审查：工作空间参数解析矩阵 + 多通道消息边界测试。

聚焦 Round1/Round2 未覆盖的深度边界场景。

| 维度 | 覆盖内容 |
|------|----------|
| 工作空间参数矩阵 | 文档§5.6 任务类型×isolation_level×workspace参数 → ws_meta.mode/path |
| inherit 继承模式边界 | _inherit_workspace_resolved 各分支 |
| 多层嵌套 workspace chain | resolve_workspace 递归（nested/shared 双模式） |
| 通道消息边界 | 空消息、超长消息、特殊字符、None payload |
| 通道适配器接口一致性 | channel_type / is_connected / get_status 三件套 |

[来源:
- docs/requirements/各模块需求文档/10_工作空间与隔离模块需求文档.md §5.6
- docs/requirements/各模块需求文档/09_多通道接入模块需求文档.md §2.1
- src/isolation/workspace_lifecycle.py
- src/isolation/workspace.py
- src/channels/gateway/
]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))


# ════════════════════════════════════════════════════════════════
# 辅助工厂
# ════════════════════════════════════════════════════════════════


def _make_lifecycle(tmp_path: Path, ws_meta_store: dict | None = None) -> Any:
    """构造一个 WorkspaceLifecycleManager 实例。"""
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager

    ws_root = tmp_path / "ws"
    ws_root.mkdir(exist_ok=True)
    return WorkspaceLifecycleManager(
        resource_merge=MagicMock(),
        config={"workspace": {"root": str(ws_root)}},
        task_tree=MagicMock(),
        ws_meta_store=ws_meta_store if ws_meta_store is not None else {},
        base_path=str(tmp_path),
    )


# ════════════════════════════════════════════════════════════════
# Section 1: 工作空间参数矩阵（文档§5.6）
# ════════════════════════════════════════════════════════════════


class TestWorkspaceParamMatrix:
    """文档§5.6 完整参数矩阵验证。

    意图：用户提交不同类型的任务时，ws_meta 的 mode/path 必须按矩阵确定，
    否则隔离层无法正确建立 git worktree 或 Docker mount 路径。
    """

    def test_container_host_with_workspace_reuses_original(self, tmp_path):
        """容器任务 + host + 指定 workspace → 直接复用原空间路径。

        矩阵行: container/host/指定 → mode=project_root, path=原 workspace
        """
        lc = _make_lifecycle(tmp_path)
        source = tmp_path / "my_proj"
        source.mkdir()
        (source / "main.py").write_text("# test", encoding="utf-8")

        meta = lc.init_container_workspace(
            container_task_id="ct_host_explicit",
            workspace=str(source),
            task_data={"isolation_mode": "host"},
        )

        assert meta["mode"] == "project_root"
        assert meta["path"] == str(source), "host 模式应直接复用原 workspace 路径"
        assert meta.get("is_container_workspace") is True

    def test_container_host_without_workspace_creates_container_dir(self, tmp_path):
        """容器任务 + host + 未指定 workspace → 创建 ws_root/container_{id}。"""
        lc = _make_lifecycle(tmp_path)
        meta = lc.init_container_workspace(
            container_task_id="ct_host_none",
            workspace=None,
            task_data={"isolation_mode": "host"},
        )
        assert meta["mode"] == "project_root"
        assert "container_ct_host_none" in meta["path"]
        assert Path(meta["path"]).exists()

    def test_container_isolated_with_workspace_copies_files(self, tmp_path):
        """容器任务 + isolated + 指定 workspace → 复制文件到容器空间。"""
        lc = _make_lifecycle(tmp_path)
        source = tmp_path / "src_proj"
        source.mkdir()
        (source / "code.py").write_text("# code", encoding="utf-8")

        meta = lc.init_container_workspace(
            container_task_id="ct_iso_explicit",
            workspace=str(source),
            task_data={"isolation_mode": ""},  # 非 host
        )
        assert meta["mode"] == "project_root"
        assert "container_ct_iso_explicit" in meta["path"]
        # 文件应被复制到新空间
        assert (Path(meta["path"]) / "code.py").exists()

    def test_container_isolated_without_workspace_empty_space(self, tmp_path):
        """容器任务 + isolated + 未指定 workspace → 空容器空间。"""
        lc = _make_lifecycle(tmp_path)
        meta = lc.init_container_workspace(
            container_task_id="ct_iso_none",
            workspace=None,
            task_data={"isolation_mode": "isolated"},
        )
        assert meta["mode"] == "project_root"
        assert Path(meta["path"]).exists()
        assert "container_ct_iso_none" in meta["path"]

    def test_non_container_subtask_shares_parent_workspace(self, tmp_path):
        """非容器子任务 → mode=shared，path 等于父任务 path。

        矩阵行: 非容器子任务/任意/不指定 → shared | 父任务工作空间路径
        """
        parent_path = "/some/parent/__wt_parent01"
        ws_meta_store = {
            "p1": {
                "mode": "worktree",
                "path": parent_path,
                "branch": "task/p1",
                "project_root": "/some/parent",
            }
        }
        lc = _make_lifecycle(tmp_path, ws_meta_store=ws_meta_store)

        mock_task = MagicMock()
        mock_task.parent_task_id = "p1"
        lc._task_tree.get_task.return_value = mock_task

        meta = lc._start_subtask("c1", "", {"is_root": False})

        assert meta["mode"] == "shared"
        assert meta["path"] == parent_path
        assert meta["project_root"] == "/some/parent"

    def test_container_workspace_meta_has_branch_main(self, tmp_path):
        """容器任务 ws_meta 包含 branch='main' 和 project_root=path。"""
        lc = _make_lifecycle(tmp_path)
        meta = lc.init_container_workspace(
            container_task_id="ct_branch",
            workspace=None,
            task_data={"isolation_mode": ""},
        )
        assert meta.get("branch") == "main"
        assert meta.get("project_root") == meta["path"]


# ════════════════════════════════════════════════════════════════
# Section 2: inherit 继承模式边界条件
# ════════════════════════════════════════════════════════════════


class TestInheritWorkspaceMode:
    """inherit_workspace_from 继承模式边界。

    意图：当任务通过 inherit_workspace_from 复用旧任务空间时，
    必须继承旧 ws_meta 的 mode / branch / project_root，
    而不是误判为"新项目"重新初始化。
    """

    def test_inherit_preserves_worktree_mode(self, tmp_path):
        """inherit + source mode=worktree → 复用 worktree 模式 + branch。"""
        lc = _make_lifecycle(tmp_path)
        task_data = {
            "is_root": True,
            "_inherit_workspace_resolved": True,
            "_source_ws_meta": {
                "mode": "worktree",
                "branch": "task/old_task_99",
                "project_root": "/some/old/proj",
            },
        }
        meta = lc._start_root_task("new_task_01", "/new/ws/path", task_data)

        assert meta["mode"] == "worktree"
        assert meta["path"] == "/new/ws/path", "path 应使用新传入的 workspace"
        assert meta["branch"] == "task/old_task_99", "branch 继承自源 ws_meta"
        assert meta["project_root"] == "/some/old/proj"

    def test_inherit_without_source_meta_defaults_to_shared(self, tmp_path):
        """inherit + 无 _source_ws_meta → 默认 mode=shared。"""
        lc = _make_lifecycle(tmp_path)
        task_data = {
            "is_root": True,
            "_inherit_workspace_resolved": True,
        }
        meta = lc._start_root_task("new_task_02", "/some/path", task_data)

        assert meta["mode"] == "shared"
        assert meta["path"] == "/some/path"

    def test_inherit_empty_source_meta_defaults_to_shared(self, tmp_path):
        """inherit + _source_ws_meta={} → mode=shared。"""
        lc = _make_lifecycle(tmp_path)
        task_data = {
            "is_root": True,
            "_inherit_workspace_resolved": True,
            "_source_ws_meta": {},
        }
        meta = lc._start_root_task("new_task_03", "/x/y", task_data)

        assert meta["mode"] == "shared"
        assert meta["path"] == "/x/y"

    def test_inherit_writes_to_ws_meta_store(self, tmp_path):
        """inherit 后 ws_meta 应写入 ws_meta_store。"""
        store: dict = {}
        lc = _make_lifecycle(tmp_path, ws_meta_store=store)
        task_data = {
            "is_root": True,
            "_inherit_workspace_resolved": True,
            "_source_ws_meta": {"mode": "worktree", "branch": "task/x"},
        }
        lc._start_root_task("inh_store", "/p", task_data)

        assert "inh_store" in store
        assert store["inh_store"]["mode"] == "worktree"

    def test_inherit_skips_container_workspace_search(self, tmp_path):
        """inherit 模式不查找容器空间（_inherit_workspace_resolved=True 时跳过）。"""
        lc = _make_lifecycle(tmp_path)
        task_data = {
            "is_root": True,
            "_inherit_workspace_resolved": True,
            "_source_ws_meta": {"mode": "shared"},
        }
        # 不应因查找容器空间而抛异常
        meta = lc._start_root_task("inh_skip", "/skip/path", task_data)
        assert meta["mode"] == "shared"


# ════════════════════════════════════════════════════════════════
# Section 3: 多层嵌套子任务 workspace chain（resolve_workspace）
# ════════════════════════════════════════════════════════════════


class TestWorkspaceChainResolution:
    """resolve_workspace 的多层嵌套解析逻辑。

    意图：三层以上嵌套子任务必须逐层拼接路径，不允许把孙任务当作根任务。
    [来源: src/isolation/workspace.py resolve_workspace L69-147]
    """

    def test_root_task_no_workspace(self):
        """根任务无 workspace → root/{task_id}。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace("t1", None, config_root="/ws_root")
        assert result == "/ws_root/t1"

    def test_root_task_with_relative_workspace(self):
        """根任务 + 相对路径 workspace → root/{workspace}。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace("t2", "myproj", config_root="/ws_root")
        assert result == "/ws_root/myproj"

    def test_root_task_absolute_workspace(self):
        """根任务 + 绝对路径 workspace → 直接使用。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace("t3", "/abs/path", config_root="/ws_root")
        assert result == "/abs/path"

    def test_child_nested_default_appends_task_id(self):
        """子任务（nested 模式）无 workspace → parent/{child_id}。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "child1", None,
            parent_resolved_workspace="/ws_root/parent1",
        )
        assert result == "/ws_root/parent1/child1"

    def test_child_nested_with_workspace(self):
        """子任务（nested 模式）+ workspace → parent/{workspace}。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "child2", "subdir",
            parent_resolved_workspace="/ws_root/parent2",
        )
        assert result == "/ws_root/parent2/subdir"

    def test_child_shared_mode_reuses_parent(self):
        """子任务（shared 模式）→ 直接复用父路径。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "child3", "ignored_ws",
            parent_resolved_workspace="/ws_root/parent3",
            nesting_mode="shared",
        )
        assert result == "/ws_root/parent3", "shared 模式忽略 workspace 参数"

    def test_three_level_nested_chain(self):
        """三层嵌套 nested 模式逐层拼接。"""
        from isolation.workspace import resolve_workspace

        root = resolve_workspace("root_t", None, config_root="/base")
        level1 = resolve_workspace("l1", None, parent_resolved_workspace=root)
        level2 = resolve_workspace("l2", None, parent_resolved_workspace=level1)

        assert level2 == "/base/root_t/l1/l2"

    def test_three_level_shared_chain(self):
        """三层嵌套 shared 模式全部复用根路径。"""
        from isolation.workspace import resolve_workspace

        root = resolve_workspace("root_s", "proj", config_root="/base")
        level1 = resolve_workspace(
            "l1s", None,
            parent_resolved_workspace=root,
            nesting_mode="shared",
        )
        level2 = resolve_workspace(
            "l2s", None,
            parent_resolved_workspace=level1,
            nesting_mode="shared",
        )
        assert level1 == root
        assert level2 == root

    def test_child_absolute_workspace_ignored_in_shared(self):
        """shared 模式下绝对路径 workspace 也被忽略。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "c_abs", "/override/path",
            parent_resolved_workspace="/parent/path",
            nesting_mode="shared",
        )
        assert result == "/parent/path"

    def test_child_absolute_workspace_in_nested(self):
        """nested 模式下绝对路径 workspace 直接使用。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "c_nest", "/abs/nested",
            parent_resolved_workspace="/parent/path",
        )
        assert result == "/abs/nested"

    def test_root_workspace_already_has_root_prefix(self):
        """workspace 已包含 root 前缀时不重复拼接。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "t_dup", "/ws_root/existing",
            config_root="/ws_root",
        )
        assert result == "/ws_root/existing"

    def test_child_workspace_already_has_parent_prefix(self):
        """子任务 workspace 已含父路径时不重复拼接。"""
        from isolation.workspace import resolve_workspace

        result = resolve_workspace(
            "c_dup", "/parent/path/sub",
            parent_resolved_workspace="/parent/path",
        )
        assert result == "/parent/path/sub"


# ════════════════════════════════════════════════════════════════
# Section 4: 多通道消息格式转换边界
# ════════════════════════════════════════════════════════════════


class TestChannelMessageBoundary:
    """多通道消息格式转换的边界场景。

    意图：消息标准化器（MessageNormalizer）面对空消息、超长消息、
    特殊字符时不能崩溃，且必须保持字段完整性。
    """

    @pytest.fixture
    def normalizer(self):
        from channels.gateway.message_normalizer import MessageNormalizer
        return MessageNormalizer()

    # ── 空消息边界 ──

    def test_feishu_empty_raw_yields_unknown_user(self, normalizer):
        """飞书空消息体 → unified_user_id=feishu:unknown。"""
        msg = normalizer.normalize("feishu", {})
        assert msg.unified_user_id == "feishu:unknown"
        assert msg.channel_user_id == ""

    def test_dingtalk_empty_raw_yields_unknown_user(self, normalizer):
        """钉钉空消息体 → unified_user_id=dingtalk:unknown。"""
        msg = normalizer.normalize("dingtalk", {})
        assert msg.unified_user_id == "dingtalk:unknown"
        assert msg.channel_type == "dingtalk"

    def test_wecom_empty_raw_yields_unknown_user(self, normalizer):
        """企业微信空消息体 → unified_user_id=wecom:unknown。"""
        msg = normalizer.normalize("wecom", {})
        assert msg.unified_user_id == "wecom:unknown"
        assert msg.content == ""

    def test_qq_empty_raw_yields_unknown_user(self, normalizer):
        """QQ 空消息体 → unified_user_id=qq:unknown。"""
        msg = normalizer.normalize("qq", {})
        assert msg.unified_user_id == "qq:unknown"

    # ── 超长消息 ──

    def test_feishu_very_long_message_preserved(self, normalizer):
        """飞书 10 万字符超长消息 → content 完整保留。"""
        long_text = "x" * 100_000
        raw = {
            "event": {
                "message": {
                    "message_type": "text",
                    "content": '{"text":"' + long_text + '"}',
                }
            }
        }
        msg = normalizer.normalize("feishu", raw)
        assert len(msg.content) == 100_000
        assert msg.content == long_text

    def test_dingtalk_very_long_message_preserved(self, normalizer):
        """钉钉 5 万字符消息 → content 完整保留。"""
        long_text = "y" * 50_000
        raw = {
            "msgtype": "text",
            "text": {"content": long_text},
        }
        msg = normalizer.normalize("dingtalk", raw)
        assert msg.content == long_text

    # ── 特殊字符 ──

    def test_feishu_html_tags_preserved_as_content(self, normalizer):
        """飞书含 HTML 标签的消息 → 原样保留（不做转义/过滤）。"""
        raw = {
            "event": {
                "message": {
                    "message_type": "text",
                    "content": '{"text":"<script>alert(1)</script>"}',
                }
            }
        }
        msg = normalizer.normalize("feishu", raw)
        assert "<script>" in msg.content
        assert msg.content == "<script>alert(1)</script>"

    def test_feishu_unicode_emoji_preserved(self, normalizer):
        """飞书含 emoji/unicode → content 原样保留。"""
        raw = {
            "event": {
                "message": {
                    "message_type": "text",
                    "content": '{"text":"你好🌍🚀\\n世界"}',
                }
            }
        }
        msg = normalizer.normalize("feishu", raw)
        assert "🌍" in msg.content
        assert "🚀" in msg.content
        assert "你好" in msg.content

    def test_qq_cq_code_image_returns_empty_text(self, normalizer):
        """QQ 纯图片 CQ 码消息 → content 为 [图片] 或空字符串。"""
        raw = {
            "user_id": 12345,
            "message": "[CQ:image,file=abc.jpg]",
        }
        msg = normalizer.normalize("qq", raw)
        # CQ 码被移除后文本为空
        assert msg.content_type == "image"

    def test_qq_cq_code_mixed_text_and_at(self, normalizer):
        """QQ 混合文本和 @ 的 CQ 码 → 提取文本和 @。"""
        raw = {
            "user_id": 67890,
            "message": "[CQ:at,qq=111]你好啊",
        }
        msg = normalizer.normalize("qq", raw)
        # 文本中的 CQ:at 被移除
        assert "你好啊" in msg.content

    # ── 反标准化边界 ──

    def test_denormalize_feishu_empty_content(self, normalizer):
        """飞书空 content 反标准化 → text 字段为空。"""
        from channels.gateway.unified_types import UnifiedResponse

        resp = UnifiedResponse(
            message_id="r1",
            channel_type="feishu",
            content="",
            content_type="text",
        )
        result = normalizer.denormalize("feishu", resp)
        assert result["msg_type"] == "text"
        assert result["content"]["text"] == ""

    def test_denormalize_dingtalk_long_content(self, normalizer):
        """钉钉超长 content → text.content 完整保留。"""
        from channels.gateway.unified_types import UnifiedResponse

        long_text = "z" * 10_000
        resp = UnifiedResponse(
            message_id="r2",
            channel_type="dingtalk",
            content=long_text,
            content_type="text",
        )
        result = normalizer.denormalize("dingtalk", resp)
        assert result["msgtype"] == "text"
        assert result["text"]["content"] == long_text

    def test_denormalize_qq_special_chars(self, normalizer):
        """QQ 特殊字符 → message 段原样保留。"""
        from channels.gateway.unified_types import UnifiedResponse

        resp = UnifiedResponse(
            message_id="r3",
            channel_type="qq",
            content='特殊"字符<>&',
            content_type="text",
        )
        result = normalizer.denormalize("qq", resp)
        assert result["message"][0]["data"]["text"] == '特殊"字符<>&'


# ════════════════════════════════════════════════════════════════
# Section 5: 通道适配器接口一致性
# ════════════════════════════════════════════════════════════════


class TestAdapterInterfaceConsistency:
    """各通道适配器接口一致性验证。

    意图：所有通道适配器必须提供统一的 channel_type / is_connected /
    get_status 接口，否则 ChannelGateway 无法统一管理生命周期。
    """

    def test_base_combo_adapter_get_status_has_required_keys(self):
        """BaseComboAdapter.get_status 返回包含 type/connected/healthy 的字典。"""
        from channels.base_combo_adapter import BaseComboAdapter

        class _FakeAdapter(BaseComboAdapter):
            @property
            def channel_type(self) -> str:
                return "fake"

            class stream_client:
                is_connected = True

        adapter = _FakeAdapter()
        status = adapter.get_status()
        assert "type" in status
        assert "connected" in status
        assert "healthy" in status
        assert status["type"] == "fake"

    def test_base_combo_adapter_health_check_returns_bool(self):
        """BaseComboAdapter.health_check 返回 bool。"""
        from channels.base_combo_adapter import BaseComboAdapter

        class _FakeAdapter2(BaseComboAdapter):
            @property
            def channel_type(self) -> str:
                return "fake2"

            class stream_client:
                is_connected = True

        adapter = _FakeAdapter2()
        result = adapter.health_check()
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            value = loop.run_until_complete(result)
            assert isinstance(value, bool)
            assert value is True
        finally:
            loop.close()

    def test_input_adapter_subclass_has_required_interface(self):
        """IInputAdapter 子类必须实现 receive() 方法。"""
        from channels.input_adapter import IInputAdapter

        # 定义子类时如果没有实现 receive 应报 TypeError
        with pytest.raises(TypeError):
            class _Incomplete(IInputAdapter):
                pass
            _Incomplete()

    def test_output_adapter_subclass_has_required_interface(self):
        """IOutputAdapter 子类必须实现 send() 和 send_stream() 方法。"""
        from channels.output_adapter import IOutputAdapter

        with pytest.raises(TypeError):
            class _Incomplete2(IOutputAdapter):
                pass
            _Incomplete2()

    def test_normalizer_register_custom_channel_bidirectional(self):
        """自定义通道注册后可双向转换。"""
        from channels.gateway.message_normalizer import MessageNormalizer
        from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse

        normalizer = MessageNormalizer()

        def custom_normalize(raw: dict) -> UnifiedMessage:
            return UnifiedMessage(
                message_id=raw.get("id", "x"),
                channel_type="custom",
                channel_user_id=raw.get("user", ""),
                unified_user_id=f"custom:{raw.get('user', '')}",
                content=raw.get("text", ""),
                content_type="text",
                raw_message=raw,
                timestamp=0.0,
            )

        def custom_denormalize(resp: UnifiedResponse) -> dict:
            return {"custom_text": resp.content}

        normalizer.register("custom", custom_normalize, custom_denormalize)

        # normalize
        msg = normalizer.normalize("custom", {"id": "c1", "user": "u1", "text": "hello"})
        assert msg.unified_user_id == "custom:u1"
        assert msg.content == "hello"

        # denormalize
        resp = UnifiedResponse(
            message_id="c1",
            channel_type="custom",
            content="reply",
            content_type="text",
        )
        out = normalizer.denormalize("custom", resp)
        assert out["custom_text"] == "reply"

    def test_normalize_unsupported_channel_raises(self):
        """不支持通道的 normalize 抛出 ValueError。"""
        from channels.gateway.message_normalizer import MessageNormalizer

        normalizer = MessageNormalizer()
        with pytest.raises(ValueError, match="Unsupported channel type"):
            normalizer.normalize("nonexistent", {})

    def test_denormalize_unsupported_channel_raises(self):
        """不支持通道的 denormalize 抛出 ValueError。"""
        from channels.gateway.message_normalizer import MessageNormalizer
        from channels.gateway.unified_types import UnifiedResponse

        normalizer = MessageNormalizer()
        resp = UnifiedResponse(
            message_id="x",
            channel_type="nonexistent",
            content="x",
            content_type="text",
        )
        with pytest.raises(ValueError, match="Unsupported channel type"):
            normalizer.denormalize("nonexistent", resp)


# ════════════════════════════════════════════════════════════════
# Section 6: SessionBridge 跨通道会话边界
# ════════════════════════════════════════════════════════════════


class TestSessionBridgeBoundary:
    """SessionBridge 跨通道会话桥接边界。"""

    def test_get_or_create_session_returns_12_char_id(self):
        """首次创建会话 → session_id 长度为 12。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        sid = bridge.get_or_create_session("feishu:ou_1", "feishu")
        assert len(sid) == 12

    def test_same_user_returns_same_session(self):
        """同一用户多次调用返回相同 session_id。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        sid1 = bridge.get_or_create_session("dingtalk:s1", "dingtalk")
        sid2 = bridge.get_or_create_session("dingtalk:s1", "dingtalk")
        assert sid1 == sid2

    def test_different_users_different_sessions(self):
        """不同用户产生不同 session_id。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        sid1 = bridge.get_or_create_session("feishu:u1", "feishu")
        sid2 = bridge.get_or_create_session("feishu:u2", "feishu")
        assert sid1 != sid2

    def test_switch_channel_unknown_user_no_crash(self):
        """切换未知用户的通道不崩溃。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        # 未知用户切换通道，不抛异常
        bridge.switch_channel("unknown:user", "feishu")
        # get_active_channel 返回空字符串
        assert bridge.get_active_channel("unknown:user") == ""

    def test_get_active_channel_unknown_user_empty(self):
        """未知用户 active_channel 返回空字符串。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        assert bridge.get_active_channel("nobody") == ""

    def test_cross_channel_same_session(self):
        """跨通道复用同一 session（先飞书后钉钉）。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()
        sid_feishu = bridge.get_or_create_session("user:cross", "feishu")
        sid_dingtalk = bridge.get_or_create_session("user:cross", "dingtalk")
        assert sid_feishu == sid_dingtalk

        # active_channel 应切换到最新
        bridge.switch_channel("user:cross", "dingtalk")
        assert bridge.get_active_channel("user:cross") == "dingtalk"
