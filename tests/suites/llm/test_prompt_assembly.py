"""提示词组装链路集成测试。

验证重构后的消息构建链路与旧代码 layer_order 完全一致：
    Input 插件链:
        message_inject → context_build → memory_read → tool_schema → prompt_build
    Core 插件:
        LLMCore._build_messages() 从 system_message + compression_messages + messages + dynamic_vars 组装
        (dynamic_vars 作为独立 system 消息追加在末尾，不合并进 system_message)

测试覆盖：
    1. prompt_build 只产出 state["system_message"]（纯 prompt），不含历史和动态变量
    2. prompt_build 按 layer_order 顺序组装（system_prompt → tools_description → static_vars）
       记忆/知识不再自动拼入：仅 static_vars 配 retrieval/tags 时 opt-in（_retrieve_by_tags）
    3. prompt_build 默认不拼入 tools_description（走 function calling）
    4. tool_schema 默认不写 prompt.tool_descriptions
    5. LLMCore._build_messages 从多来源组装（system_message + compression + history + dynamic_vars）
    6. LLMCore._build_messages 动态变量作为独立 system 消息追加在末尾，不合并进 system_message
    7. LLMCore 只将 assistant 回复追加到 state["messages"]（不包含 system/dynamic）
    8. 完整链路：message_inject → prompt_build → LLMCore._build_messages
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.types import ChunkData
from pipeline.plugin import PluginContext
from pipeline.types import create_initial_state
from plugins.core.llm_core import LLMCore
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.input.tool_schema import ToolSchemaPlugin


# ── 测试辅助 ──


def make_ctx(state: dict, **services: Any) -> PluginContext:
    """创建带服务的插件上下文。"""
    ctx = PluginContext(state=state)
    for name, svc in services.items():
        ctx._services[name] = svc
    return ctx


def make_base_state(**overrides: Any) -> dict[str, Any]:
    """创建包含 system_prompt 的基础 state。"""
    state = create_initial_state(**overrides)
    state["context.system_prompt"] = "你是一个有用的 AI 助手。"
    state["context.session_id"] = "test-session-001"
    state["messages"] = []
    return state


# ══════════════════════════════════════════════════
# 1. prompt_build 基础：只产出 system_message
# ══════════════════════════════════════════════════


class TestPromptBuildBasic:
    """prompt_build 基础行为测试。"""

    @pytest.mark.asyncio
    async def test_output_only_system_message(self) -> None:
        """prompt_build 只产出 state['system_message']，不包含历史消息。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "你好"}]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        assert "system_message" in result.state_updates
        assert result.state_updates["system_message"]["role"] == "system"
        assert "你是一个有用的 AI 助手。" in result.state_updates["system_message"]["content"]
        assert "你好" not in result.state_updates["system_message"]["content"]

    @pytest.mark.asyncio
    async def test_output_dynamic_vars_separately(self) -> None:
        """prompt_build 将 dynamic_vars 单独输出（role=system），不拼入 system_message。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        system_content = result.state_updates["system_message"]["content"]
        dynamic_vars = result.state_updates.get("prompt.dynamic_vars", "")

        assert "日期" in dynamic_vars["content"]
        assert "时间" in dynamic_vars["content"]
        assert dynamic_vars["role"] == "system"  # 改为 system role
        assert dynamic_vars["content"] not in system_content

    @pytest.mark.asyncio
    async def test_no_messages_in_output(self) -> None:
        """prompt_build 产出不包含 messages 字段。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "test"}]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        assert "messages" not in result.state_updates


# ══════════════════════════════════════════════════
# 2. prompt_build layer_order 顺序
# ══════════════════════════════════════════════════


class TestPromptBuildLayerOrder:
    """prompt_build 按 layer_order 顺序组装各层内容。"""

    @pytest.mark.asyncio
    async def test_system_prompt_first(self) -> None:
        """system_prompt 是第一层。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert content.startswith("你是一个有用的 AI 助手。")

    @pytest.mark.asyncio
    async def test_tools_description_excluded_by_default(self) -> None:
        """默认不拼入 tools_description（走 function calling）。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["prompt.tool_descriptions"] = "## 可用工具\n- tool1: desc"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "可用工具" not in content

    @pytest.mark.asyncio
    async def test_tools_description_included_when_enabled(self) -> None:
        """配置 include_tools_description_in_prompt=true 时拼入 tools_description。"""
        plugin = PromptBuildPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()
        state["prompt.tool_descriptions"] = "## 可用工具\n- tool1: desc"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "可用工具" in content

    @pytest.mark.asyncio
    async def test_knowledge_context_not_auto_injected(self) -> None:
        """knowledge.context 不再无条件拼入 system_message（仅 static_vars opt-in）。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        state = make_base_state()
        state["knowledge.context"] = "## 知识库\nPython 异常处理最佳实践"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "知识库" not in content
        assert "Python 异常处理最佳实践" not in content

    @pytest.mark.asyncio
    async def test_memory_retrieved_not_auto_injected(self) -> None:
        """memory.retrieved 不再无条件拼入 system_message。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        state = make_base_state()
        state["knowledge.context"] = "知识内容"
        state["memory.retrieved"] = "## 记忆检索\n相关经验: 上次讨论了异常处理"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "知识内容" not in content
        assert "记忆检索" not in content

    @pytest.mark.asyncio
    async def test_memory_injected_via_retrieval_static_var(self) -> None:
        """static_vars 配 type: retrieval + tags 时，记忆经 _retrieve_by_tags opt-in 进 prompt。"""
        mock_memory_service = MagicMock()
        mock_memory_service.retrieve = AsyncMock(
            return_value=[MagicMock(content="用户偏好用 Python 处理数据")],
        )
        plugin = PromptBuildPlugin(config={"include_compressed_layers": False})
        state = make_base_state()
        state["user_id"] = "user-1"
        state["context.static_vars"] = [
            {"type": "retrieval", "name": "相关记忆", "tags": ["preference"], "top_k": 3},
        ]
        ctx = make_ctx(state, memory_service=mock_memory_service)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "用户偏好用 Python 处理数据" in content
        mock_memory_service.retrieve.assert_called()

    @pytest.mark.asyncio
    async def test_compressed_layers_order_keywords_l2_l1(self) -> None:
        """压缩层按 关键词 → L2 → L1 顺序排列（预算溢出降级）。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False})
        state = make_base_state()
        state["pipeline_id"] = "test-pipeline-001"
        # 小窗口 → L1预算=6, L2预算=2
        state["context_window"] = 40

        from datetime import datetime, UTC
        # 4 个 L1 chunks: 最新 1 个 fit L1，1 个 fit L2，1 个 fit L2，
        # 最老的溢出到关键词
        chunks_l1 = [
            ChunkData(
                content="L1最新", l2_content="L2",
                keywords=["kw4"], created_at=datetime(2024, 4, 1, tzinfo=UTC),
                sequence_start=301, sequence_end=400,
            ),
            ChunkData(
                content="L" * 20, l2_content="y",
                keywords=["kw3"], created_at=datetime(2024, 3, 1, tzinfo=UTC),
                sequence_start=201, sequence_end=300,
            ),
            ChunkData(
                content="L" * 20, l2_content="z",
                keywords=["kw2"], created_at=datetime(2024, 2, 1, tzinfo=UTC),
                sequence_start=101, sequence_end=200,
            ),
            ChunkData(
                content="L" * 20, l2_content="w",
                keywords=["kw1_old"], created_at=datetime(2024, 1, 1, tzinfo=UTC),
                sequence_start=1, sequence_end=100,
            ),
        ]

        chunk_service = MagicMock()
        async def mock_find(pid, layer):
            if layer == "L1":
                return chunks_l1
            return []
        chunk_service.find_by_pipeline = AsyncMock(side_effect=mock_find)
        ctx = make_ctx(state, chunk_service=chunk_service)

        result = await plugin.execute(ctx)

        # 压缩层作为独立消息输出在 compression_messages（而非 system_message.content）
        compression_msgs = result.state_updates.get("compression_messages", [])
        assert compression_msgs, "compression_messages 不应为空"

        # 验证存在的层级按正确顺序排列：keywords → L2 → L1
        # 预算充足时某些层级可能不出现，只验证存在的层级顺序
        def _find_level(msgs: list, marker: str) -> int:
            """查找包含 marker 的消息索引，不存在返回 -1。"""
            for i, m in enumerate(msgs):
                if marker in m.get("content", ""):
                    return i
            return -1

        kw_pos = _find_level(compression_msgs, 'level="KEYWORDS"')
        l2_pos = _find_level(compression_msgs, 'level="L2"')
        l1_pos = _find_level(compression_msgs, 'level="L1"')
        assert l1_pos >= 0, "至少应有 L1 压缩块"

        if kw_pos >= 0 and l2_pos >= 0:
            assert kw_pos < l2_pos, "keywords 应在 L2 之前"
        if l2_pos >= 0 and l1_pos >= 0:
            assert l2_pos < l1_pos, "L2 应在 L1 之前"
        if kw_pos >= 0 and l1_pos >= 0:
            assert kw_pos < l1_pos, "keywords 应在 L1 之前"


# ══════════════════════════════════════════════════
# 3. prompt_build static_vars
# ══════════════════════════════════════════════════


class TestPromptBuildStaticVars:
    """prompt_build 静态变量加载测试。"""

    @pytest.mark.asyncio
    async def test_timestamp_type(self) -> None:
        """timestamp 类型静态变量生成当前时间。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "timestamp", "name": "当前时间", "format": "%Y-%m-%d"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "当前时间" in content
        assert "静态变量" in content

    @pytest.mark.asyncio
    async def test_content_type(self) -> None:
        """content 类型静态变量直接使用文本值。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "项目说明", "value": "这是一个 Agent OS 项目"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "项目说明" in content
        assert "Agent OS" in content

    @pytest.mark.asyncio
    async def test_disabled_var_skipped(self) -> None:
        """enabled=false 的静态变量被跳过。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "跳过", "value": "不应出现", "enabled": False}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "不应出现" not in content

    @pytest.mark.asyncio
    async def test_static_vars_disabled_by_config(self) -> None:
        """include_static_vars=false 时跳过所有静态变量。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False})
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "项目说明", "value": "不应出现"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "不应出现" not in content


# ══════════════════════════════════════════════════
# 3.5 prompt_build folder 类型（相对路径拼接 workspace）
# ══════════════════════════════════════════════════


class TestPromptBuildFolderInjection:
    """folder 类型静态变量注入：path 必填，相对路径拼接 ws_meta.path（worktree 路径）。

    实际数据形态（参考 2eee5194e20f.yaml）::

        ws_meta:
          mode: worktree
          path: D:\\myproject\\myproject__wt_2eee5194
          branch: task/2eee5194e20f
          project_root: D:\\myproject

    即: 任务 YAML 中 ``workspace`` 字段是 ``.`` 之类的相对值, 真正的 worktree 路径
    落在 ``ws_meta.path``, 必须用它作为 folder 相对路径的拼接 base。

    路径解析规则（**不允许 CWD 回退**）：
        1. 相对路径优先拼 ``state["ws_meta"]["path"]``
        2. 缺失 ws_meta 时回退到 ``state["workspace"]``
        3. 二者都缺失 → 返回空，**绝不**用 CWD 解析
        4. 绝对路径直接使用
    """

    @pytest.mark.asyncio
    async def test_folder_empty_path_skipped(self, tmp_path) -> None:
        """folder 未指定 path 时直接跳过，不允许默认注入整个工作空间。"""
        # workspace 下放一个文件，确认它不会被默认吸入
        (tmp_path / "stray.md").write_text("不应出现", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["ws_meta"] = {"path": str(tmp_path), "mode": "worktree", "branch": "task/test"}
        state["workspace"] = "."  # 模拟实际场景：workspace 是相对值, 真实路径在 ws_meta
        state["context.static_vars"] = [
            {"type": "path", "name": "空路径目录", "path": ""}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        # 既不应有 section 标题，也不应有任何文件内容
        assert "空路径目录" not in content
        assert "不应出现" not in content

    @pytest.mark.asyncio
    async def test_folder_relative_path_joined_with_workspace(self, tmp_path) -> None:
        """文件夹相对路径与 state["workspace"] 拼接，读取目录下文件。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("文档A", encoding="utf-8")
        (docs / "b.md").write_text("文档B", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["workspace"] = str(tmp_path)  # engine.run(workspace=ws_meta.path) 注入
        state["context.static_vars"] = [
            {"type": "path", "name": "项目文档", "path": "docs"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "项目文档" in content
        assert "文档A" in content
        assert "文档B" in content
        # 应使用 `--- filename ---` 分隔
        assert "--- a.md ---" in content
        assert "--- b.md ---" in content

    @pytest.mark.asyncio
    async def test_folder_no_ws_meta_and_no_workspace_returns_empty(self, tmp_path) -> None:
        """无 ws_meta.path 且无 workspace 时返回空（不读 CWD）。"""
        # 在 CWD 下放同名目录, 验证不会去 CWD 找
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("不应出现", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state.pop("workspace", None)
        state.pop("ws_meta", None)
        state["context.static_vars"] = [
            {"type": "path", "name": "无base", "path": "docs"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "无base" not in content
        assert "不应出现" not in content

    @pytest.mark.asyncio
    async def test_folder_workspace_fallback(self, tmp_path) -> None:
        """ws_meta.path 缺失时回退到 state["workspace"]（engine.run 传入）。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "readme.md").write_text("workspace 回退成功", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        # 无 ws_meta，但有 workspace
        state.pop("ws_meta", None)
        state["workspace"] = str(tmp_path)
        state["context.static_vars"] = [
            {"type": "path", "name": "回退测试", "path": "docs"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "回退测试" in content
        assert "workspace 回退成功" in content

    @pytest.mark.asyncio
    async def test_folder_absolute_path_used_as_is(self, tmp_path) -> None:
        """绝对路径直接使用，不与 workspace / ws_meta 拼接。"""
        docs = tmp_path / "external"
        docs.mkdir()
        (docs / "info.md").write_text("外部信息", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        # 故意把 workspace / ws_meta 指到无关目录，确认不影响绝对路径
        state["ws_meta"] = {"path": str(tmp_path / "unrelated"), "mode": "worktree"}
        (tmp_path / "unrelated").mkdir()
        state["workspace"] = str(tmp_path / "unrelated")
        state["context.static_vars"] = [
            {"type": "path", "name": "外部文档", "path": str(docs)}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "外部信息" in content
        assert "--- info.md ---" in content

    @pytest.mark.asyncio
    async def test_folder_extensions_filter(self, tmp_path) -> None:
        """extensions 按白名单过滤文件扩展名。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("Markdown", encoding="utf-8")
        (docs / "b.txt").write_text("应被过滤", encoding="utf-8")
        (docs / "c.json").write_text("{\"k\":1}", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["ws_meta"] = {"path": str(tmp_path), "mode": "worktree"}
        state["workspace"] = "."
        state["context.static_vars"] = [
            {
                "type": "path",
                "name": "仅Markdown",
                "path": "docs",
                "extensions": [".md"],
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "Markdown" in content
        assert "应被过滤" not in content
        assert '"k"' not in content

    @pytest.mark.asyncio
    async def test_folder_no_cwd_fallback_returns_empty(
        self, tmp_path, monkeypatch,
    ) -> None:
        """无 ws_meta 也无 workspace 时，相对路径绝不回退到 CWD，应返回空。"""
        # 在 CWD 下放一个同名目录, 验证实现不会去 CWD 找
        cwd_dir = tmp_path / "cwd_fake"
        cwd_dir.mkdir()
        (cwd_dir / "a.md").write_text("CWD 内容, 不应被注入", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state.pop("workspace", None)
        state.pop("ws_meta", None)
        state["context.static_vars"] = [
            {"type": "path", "name": "无base", "path": "cwd_fake"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        # 既不应有 section 标题，也不应注入任何 CWD 中的文件
        assert "无base" not in content
        assert "CWD 内容" not in content
        assert "不应被注入" not in content

    @pytest.mark.asyncio
    async def test_folder_directory_not_found_returns_empty(self, tmp_path) -> None:
        """目标目录不存在时静默返回空（不抛错）。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["ws_meta"] = {"path": str(tmp_path), "mode": "worktree"}
        state["workspace"] = "."
        state["context.static_vars"] = [
            {"type": "path", "name": "不存在的目录", "path": "missing"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        # 目录不存在 → content 为空 → 不应渲染该 section
        assert "不存在的目录" not in content


# ══════════════════════════════════════════════════
# 3.6 prompt_build path 类型（文件用 project_root 解析）
# ══════════════════════════════════════════════════


class TestPromptBuildPathInjection:
    """path 类型静态变量注入：文件（相对路径）用 project_root 解析。

    路径解析规则（**互斥，无回退**）：
        1. 文件相对路径 → 拼接 ``project_root``（state 或 services）
        2. 文件夹相对路径 → 拼接 ``ws_meta.path``（由 TestPromptBuildFolderInjection 覆盖）
        3. 两者互斥：文件不读 ws_meta.path，文件夹不读 project_root
        4. 缺失 project_root → 返回空，**绝不**用 CWD 解析
        5. 绝对路径直接使用
    """

    @pytest.mark.asyncio
    async def test_path_no_project_root_returns_empty(
        self, tmp_path, monkeypatch,
    ) -> None:
        """无 project_root（state 和 services 均无）时，相对路径绝不回退到 CWD。"""
        cwd_file = tmp_path / "rules.md"
        cwd_file.write_text("CWD 内容, 不应被注入", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state.pop("workspace", None)
        state.pop("ws_meta", None)
        state["context.static_vars"] = [
            {"type": "path", "name": "无base", "path": "rules.md"}
        ]
        ctx = make_ctx(state)  # 不传 services → project_root 为空

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "无base" not in content
        assert "CWD 内容" not in content
        assert "不应被注入" not in content

    @pytest.mark.asyncio
    async def test_path_relative_joined_with_project_root(self, tmp_path) -> None:
        """文件相对路径与 project_root（services）拼接，读取单个文件。"""
        (tmp_path / "rules.md").write_text("## 项目规范\n1. 必须有类型注解", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "path", "name": "项目规则", "path": "rules.md"}
        ]
        # 通过 services 提供 project_root（模拟实际运行时 Application 注入）
        ctx = make_ctx(state, project_root=str(tmp_path))

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "项目规则" in content
        assert "类型注解" in content

    @pytest.mark.asyncio
    async def test_path_nested_relative_joined_with_project_root(self, tmp_path) -> None:
        """多层相对路径（如 docs/sub/spec.md）也能正确拼接到 project_root。"""
        nested = tmp_path / "docs" / "sub"
        nested.mkdir(parents=True)
        (nested / "spec.md").write_text("嵌套规范", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "path", "name": "嵌套规则", "path": "docs/sub/spec.md"}
        ]
        ctx = make_ctx(state, project_root=str(tmp_path))

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "嵌套规范" in content

    @pytest.mark.asyncio
    async def test_path_absolute_used_as_is(self, tmp_path) -> None:
        """绝对路径直接使用，不与 project_root 拼接。"""
        external = tmp_path / "external"
        external.mkdir()
        (external / "info.md").write_text("外部信息", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        # 故意把 project_root / ws_meta 指到无关目录，确认不影响绝对路径
        state["ws_meta"] = {"path": str(tmp_path / "unrelated"), "mode": "worktree"}
        (tmp_path / "unrelated").mkdir()
        state["context.static_vars"] = [
            {"type": "path", "name": "外部文档", "path": str(external / "info.md")}
        ]
        ctx = make_ctx(state, project_root=str(tmp_path / "unrelated"))

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "外部信息" in content

    @pytest.mark.asyncio
    async def test_path_file_not_via_ws_meta_path(self, tmp_path) -> None:
        """文件即使在 ws_meta.path 下存在，也不通过 ws_meta.path 解析（互斥规则：文件只用 project_root）。"""
        (tmp_path / "rules.md").write_text("不应出现", encoding="utf-8")

        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["ws_meta"] = {"path": str(tmp_path), "mode": "worktree"}
        state["workspace"] = "."
        state["context.static_vars"] = [
            {"type": "path", "name": "仅ws_meta", "path": "rules.md"}
        ]
        # 不传 services → project_root 为空，且 ws_meta.path 不用于文件解析
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "仅ws_meta" not in content
        assert "不应出现" not in content

    @pytest.mark.asyncio
    async def test_path_empty_path_skipped(self) -> None:
        """path 未指定 path 时跳过，不允许默认注入。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["ws_meta"] = {"path": "D:\\some\\path", "mode": "worktree"}
        state["context.static_vars"] = [
            {"type": "path", "name": "空路径文件", "path": ""}
        ]
        ctx = make_ctx(state, project_root="D:\\project")

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "空路径文件" not in content

    @pytest.mark.asyncio
    async def test_path_file_not_found_returns_empty(self, tmp_path) -> None:
        """目标文件在 project_root 下不存在时静默返回空（不抛错）。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "path", "name": "不存在的文件", "path": "missing.md"}
        ]
        ctx = make_ctx(state, project_root=str(tmp_path))

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "不存在的文件" not in content


# ══════════════════════════════════════════════════
# 4. tool_schema 默认行为
# ══════════════════════════════════════════════════


class TestToolSchemaDefault:
    """tool_schema 默认不写 prompt.tool_descriptions。"""

    @pytest.mark.asyncio
    async def test_no_tool_descriptions_by_default(self) -> None:
        """默认不生成 prompt.tool_descriptions。"""
        plugin = ToolSchemaPlugin()
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "echo"
        mock_tool.description = "回显工具"
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)
        result = await plugin.execute(ctx)

        assert "tool_schemas" in result.state_updates
        assert "prompt.tool_descriptions" not in result.state_updates

    @pytest.mark.asyncio
    async def test_tool_descriptions_when_enabled(self) -> None:
        """include_tools_description_in_prompt=true 时生成 prompt.tool_descriptions。"""
        plugin = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "echo"
        mock_tool.description = "回显工具"
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)
        result = await plugin.execute(ctx)

        assert "tool_schemas" in result.state_updates
        assert "prompt.tool_descriptions" in result.state_updates
        assert "可用工具" in result.state_updates["prompt.tool_descriptions"]

    @pytest.mark.asyncio
    async def test_tool_schemas_always_written(self) -> None:
        """tool_schemas 始终写入（不受 include_tools_description_in_prompt 影响）。"""
        plugin_default = ToolSchemaPlugin()
        plugin_enabled = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx1 = make_ctx(dict(state), tool_registry=mock_registry)
        ctx2 = make_ctx(dict(state), tool_registry=mock_registry)

        result_default = await plugin_default.execute(ctx1)
        result_enabled = await plugin_enabled.execute(ctx2)

        assert len(result_default.state_updates["tool_schemas"]) == 1
        assert len(result_enabled.state_updates["tool_schemas"]) == 1


# ══════════════════════════════════════════════════
# 5. LLMCore._build_messages 三来源组装
# ══════════════════════════════════════════════════


class TestLLMCoreBuildMessages:
    """LLMCore._build_messages 从三来源组装 messages。"""

    def test_system_message_first(self) -> None:
        """system_message 是 messages 的第一条。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "你是一个助手。"},
            "messages": [{"role": "user", "content": "你好"}],
        }

        messages = core._build_messages(state)

        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "你是一个助手。"

    def test_history_in_middle(self) -> None:
        """历史消息在 system_message 之后；dynamic_vars 作为独立 system 消息追加在末尾。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [
                {"role": "user", "content": "问题1"},
                {"role": "assistant", "content": "回答1"},
                {"role": "user", "content": "问题2"},
            ],
            "prompt.dynamic_vars": {"role": "system", "name": "dynamic_context", "content": "动态变量内容"},
        }

        messages = core._build_messages(state)

        # system_message 保持纯净，dynamic_vars 不污染它
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "系统提示词"
        assert "动态变量内容" not in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
        assert messages[3]["role"] == "user"
        # dynamic_vars 作为独立 system 消息追加在末尾
        assert messages[4]["role"] == "system"
        assert messages[4]["name"] == "dynamic_context"
        assert "动态变量内容" in messages[4]["content"]

    def test_dynamic_vars_last(self) -> None:
        """动态变量作为独立 system 消息追加在末尾（不合并进 system_message）。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [{"role": "user", "content": "你好"}],
            "prompt.dynamic_vars": {"role": "system", "name": "dynamic_context", "content": "- 日期: 2025-01-01\n- 时间: 12:00:00"},
        }

        messages = core._build_messages(state)

        # dynamic_vars 作为独立 system 消息追加在末尾
        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "系统提示词"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "system"
        assert messages[2]["name"] == "dynamic_context"
        assert "日期" in messages[2]["content"]

    def test_no_dynamic_vars(self) -> None:
        """没有 dynamic_vars 时不追加额外的 SystemMessage。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [{"role": "user", "content": "你好"}],
        }

        messages = core._build_messages(state)

        assert len(messages) == 2
        assert messages[-1]["role"] == "user"

    def test_empty_state(self) -> None:
        """state 为空时返回空列表。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        messages = core._build_messages({})
        assert messages == []

    def test_only_system_message(self) -> None:
        """只有 system_message 时返回单元素列表。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "提示词"},
        }

        messages = core._build_messages(state)

        assert len(messages) == 1
        assert messages[0]["role"] == "system"


# ══════════════════════════════════════════════════
# 6. LLMCore messages 不累积 system/dynamic
# ══════════════════════════════════════════════════


class TestLLMCoreNoAccumulation:
    """验证 LLMCore 只将 assistant 回复追加到 state['messages']，不包含 system/dynamic。"""

    @pytest.mark.asyncio
    async def test_assistant_reply_appended_only(self) -> None:
        """LLM 普通文本回复只追加 assistant 消息到 messages。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["prompt.dynamic_vars"] = {"role": "user", "name": "dynamic_context", "content": "动态变量"}
        state["messages"] = [{"role": "user", "content": "你好"}]

        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="你好！有什么可以帮你？", tool_calls=[], thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        updated_messages = result["messages"]
        assert len(updated_messages) == 2
        assert updated_messages[0]["role"] == "user"
        assert updated_messages[1]["role"] == "assistant"
        assert updated_messages[1]["content"] == "你好！有什么可以帮你？"

    @pytest.mark.asyncio
    async def test_tool_calls_appended_only(self) -> None:
        """LLM 工具调用只追加 assistant 消息（含 tool_calls）到 messages。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["messages"] = [{"role": "user", "content": "搜索天气"}]

        tool_calls = [{"id": "call_123", "name": "search", "args": '{"query": "天气"}'}]
        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="", tool_calls=tool_calls, thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        updated_messages = result["messages"]
        assert len(updated_messages) == 2
        assert updated_messages[0]["role"] == "user"
        assert updated_messages[1]["role"] == "assistant"
        assert "tool_calls" in updated_messages[1]
        assert updated_messages[1]["tool_calls"][0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_no_system_message_in_messages_after_llm(self) -> None:
        """LLM 执行后 state['messages'] 不包含 system_message。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["prompt.dynamic_vars"] = {"role": "user", "name": "dynamic_context", "content": "动态变量"}
        state["messages"] = [{"role": "user", "content": "你好"}]

        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="回复", tool_calls=[], thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        for msg in result["messages"]:
            assert msg["role"] != "system", "state['messages'] 不应包含 system 消息"


# ══════════════════════════════════════════════════
# 7. 完整链路集成测试
# ══════════════════════════════════════════════════


class TestFullAssemblyPipeline:
    """完整链路：prompt_build → LLMCore._build_messages。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_system_memory_history_dynamic(self) -> None:
        """完整链路：dynamic_vars 作为独立 system 消息追加在末尾，history 在中间。"""
        prompt_plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["messages"] = [
            {"role": "user", "content": "帮我设计架构"},
            {"role": "assistant", "content": "好的，让我分析一下..."},
        ]

        # Step 1: prompt_build 产出 system_message + dynamic_vars
        ctx = make_ctx(state)
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        # Step 2: LLMCore._build_messages 组装最终 messages
        final_messages = llm_core._build_messages(state)

        # 验证: dynamic_vars 作为独立 system 消息追加在末尾，不合并进 system_message
        assert final_messages[0]["role"] == "system"
        assert "你是一个有用的 AI 助手" in final_messages[0]["content"]
        assert "日期" not in final_messages[0]["content"]  # dynamic_vars 不进 system_message

        assert final_messages[1]["role"] == "user"
        assert final_messages[2]["role"] == "assistant"

        # dynamic_vars 作为独立 system 消息追加在末尾
        assert len(final_messages) == 4
        assert final_messages[3]["role"] == "system"
        assert final_messages[3]["name"] == "dynamic_context"
        assert "日期" in final_messages[3]["content"]

    @pytest.mark.asyncio
    async def test_full_pipeline_with_static_vars_and_compressed(self) -> None:
        """完整链路包含 static_vars 和压缩层。"""
        prompt_plugin = PromptBuildPlugin()
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["pipeline_id"] = "test-pipeline-001"
        state["context.static_vars"] = [
            {"type": "content", "name": "项目名", "value": "Agent OS"}
        ]

        chunk_service = MagicMock()
        async def mock_find(pid, layer):
            if layer == "L1":
                return [ChunkData(
                    content="L1摘要内容",
                    l2_content="L2摘要内容",
                    keywords=["kw_test"],
                )]
            return []
        chunk_service.find_by_pipeline = AsyncMock(side_effect=mock_find)

        ctx = make_ctx(state, chunk_service=chunk_service)
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        final_messages = llm_core._build_messages(state)
        system_content = final_messages[0]["content"]

        assert "Agent OS" in system_content
        # 压缩层作为独立消息在 final_messages 中（非 system_message）
        all_content = "\n".join(m.get("content", "") for m in final_messages)
        assert "过程摘要" in all_content

    @pytest.mark.asyncio
    async def test_full_pipeline_tools_in_function_calling_mode(self) -> None:
        """完整链路：tools 走 function calling，不拼入 system_message。"""
        prompt_plugin = PromptBuildPlugin()
        tool_schema_plugin = ToolSchemaPlugin()
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "搜索天气"}]

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "搜索工具"
        mock_tool.to_llm_format.return_value = {
            "type": "function",
            "function": {"name": "search", "parameters": {}},
        }
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)

        # tool_schema 写入 tool_schemas
        schema_result = await tool_schema_plugin.execute(ctx)
        state.update(schema_result.state_updates)

        # prompt_build 产出 system_message
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        # LLMCore._build_messages 组装
        final_messages = llm_core._build_messages(state)

        # system_message 不包含工具描述
        assert "可用工具" not in final_messages[0]["content"]
        assert "搜索工具" not in final_messages[0]["content"]

        # tool_schemas 在 state 中（供 LLM API 的 tools 参数使用）
        assert "tool_schemas" in state
        assert len(state["tool_schemas"]) == 1

    @pytest.mark.asyncio
    async def test_full_pipeline_tools_in_prompt_mode(self) -> None:
        """完整链路：配置开启时 tools 拼入 system_message。"""
        prompt_plugin = PromptBuildPlugin(config={"include_tools_description_in_prompt": True})
        tool_schema_plugin = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "搜索工具"
        mock_tool.to_llm_format.return_value = {
            "type": "function",
            "function": {"name": "search", "parameters": {}},
        }
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)

        schema_result = await tool_schema_plugin.execute(ctx)
        state.update(schema_result.state_updates)

        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        final_messages = llm_core._build_messages(state)

        # system_message 包含工具描述
        assert "可用工具" in final_messages[0]["content"]
        assert "搜索工具" in final_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_layer_order_matches_old_code(self) -> None:
        """验证 layer_order: system_message 层内顺序 + 压缩层独立消息追加。"""
        from datetime import datetime, UTC

        prompt_plugin = PromptBuildPlugin(config={
            "include_tools_description_in_prompt": True,
            "include_static_vars": True,
            "include_compressed_layers": True,
        })
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["pipeline_id"] = "test-pipeline-001"
        state["context_window"] = 500
        state["prompt.tool_descriptions"] = "## 工具\n- search: 搜索"
        state["context.static_vars"] = [{"type": "content", "name": "项目", "value": "AgentOS"}]

        chunk_service = MagicMock()
        async def mock_find(pid, layer):
            if layer != "L1":
                return []
            return [
                ChunkData(
                    content="最新L1摘要",
                    l2_content="最新L2摘要",
                    keywords=["kw_new"],
                    created_at=datetime(2024, 3, 1, tzinfo=UTC),
                    sequence_start=200, sequence_end=300,
                ),
                ChunkData(
                    content="中间L1摘要",
                    l2_content="中间L2摘要",
                    keywords=["kw_mid"],
                    created_at=datetime(2024, 2, 1, tzinfo=UTC),
                    sequence_start=100, sequence_end=200,
                ),
                ChunkData(
                    content="最老L1摘要",
                    l2_content="最老L2摘要",
                    keywords=["old_kw"],
                    created_at=datetime(2024, 1, 1, tzinfo=UTC),
                    sequence_start=1, sequence_end=100,
                ),
            ]
        chunk_service.find_by_pipeline = AsyncMock(side_effect=mock_find)
        ctx = make_ctx(state, chunk_service=chunk_service)

        result = await prompt_plugin.execute(ctx)
        state.update(result.state_updates)

        # 1. system_message 内部顺序验证
        system_content = state["system_message"]["content"]
        sys_positions = {
            "system_prompt": system_content.index("AI 助手"),
            "tools": system_content.index("工具"),
            "static_vars": system_content.index("静态变量"),
        }
        sys_order = list(sys_positions.values())
        assert sys_order == sorted(sys_order), (
            f"system_message 层内顺序不一致: {list(sys_positions.keys())}"
        )

        # 2. 压缩层独立消息顺序验证（keywords → L2 → L1）
        compression_msgs = state.get("compression_messages", [])
        assert compression_msgs, "compression_messages 不应为空"

        def _find_level(msgs: list, marker: str) -> int:
            for i, m in enumerate(msgs):
                if marker in m.get("content", ""):
                    return i
            return -1

        kw_pos = _find_level(compression_msgs, 'level="KEYWORDS"')
        l2_pos = _find_level(compression_msgs, 'level="L2"')
        l1_pos = _find_level(compression_msgs, 'level="L1"')
        assert l1_pos >= 0, "至少应有 L1 压缩块"
        if kw_pos >= 0 and l2_pos >= 0:
            assert kw_pos < l2_pos, "keywords 应在 L2 之前"
        if l2_pos >= 0 and l1_pos >= 0:
            assert l2_pos < l1_pos, "L2 应在 L1 之前"

        # 3. 最终消息列表顺序：system → compression → history
        final_messages = llm_core._build_messages(state)
        assert final_messages[0]["role"] == "system"
        assert "过程摘要" in "\n".join(m.get("content", "") for m in final_messages)


class TestRoutedVars:
    """routed 类型变量路由测试。"""

    @pytest.mark.asyncio
    async def test_routed_static_string_match(self) -> None:
        """routed 变量根据 state 中的 key 值路由到字符串内容。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["scene"] = "coding"
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "场景人格",
                "route_key": "scene",
                "routes": {
                    "coding": "你是专业的编程助手",
                    "chatting": "你是温暖的聊天伙伴",
                    "_default": "你是全能助手",
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "编程助手" in content
        assert "聊天伙伴" not in content

    @pytest.mark.asyncio
    async def test_routed_static_default(self) -> None:
        """routed 变量 state 中无匹配值时走 _default。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["scene"] = "unknown_mode"
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "场景人格",
                "route_key": "scene",
                "routes": {
                    "coding": "你是专业的编程助手",
                    "_default": "你是全能助手",
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "全能助手" in content
        assert "编程助手" not in content

    @pytest.mark.asyncio
    async def test_routed_static_no_key_in_state(self) -> None:
        """routed 变量 state 中无 route_key 时走 _default。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "场景人格",
                "route_key": "scene",
                "routes": {
                    "coding": "你是专业的编程助手",
                    "_default": "你是全能助手",
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "全能助手" in content

    @pytest.mark.asyncio
    async def test_routed_static_empty_routes(self) -> None:
        """routed 变量 routes 为空时不注入任何内容。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["scene"] = "coding"
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "场景人格",
                "route_key": "scene",
                "routes": {},
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "场景人格" not in content

    @pytest.mark.asyncio
    async def test_routed_static_nested_content(self) -> None:
        """routed 变量路由到嵌套 content 类型变量。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["scene"] = "chatting"
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "场景知识",
                "route_key": "scene",
                "routes": {
                    "chatting": {
                        "type": "content",
                        "content": "生活百科知识",
                    },
                    "_default": {
                        "type": "content",
                        "content": "通用知识",
                    },
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "生活百科知识" in content
        assert "通用知识" not in content

    @pytest.mark.asyncio
    async def test_routed_dynamic_var(self) -> None:
        """routed 变量作为动态变量使用。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["time_period"] = "morning"
        state["context.dynamic_vars"] = [
            {
                "type": "routed",
                "name": "时间段问候",
                "route_key": "time_period",
                "routes": {
                    "morning": "早上好，新的一天开始了",
                    "evening": "晚上好，辛苦了",
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        dynamic_vars = result.state_updates.get("prompt.dynamic_vars", "")

        assert "早上好" in dynamic_vars["content"]
        assert "晚上好" not in dynamic_vars["content"]

    @pytest.mark.asyncio
    async def test_routed_non_string_state_value(self) -> None:
        """routed 变量 state 中的值为非字符串时也能正确路由。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["user_level"] = 1
        state["context.static_vars"] = [
            {
                "type": "routed",
                "name": "用户指引",
                "route_key": "user_level",
                "routes": {
                    "1": "新手引导模式",
                    "2": "进阶模式",
                    "_default": "标准模式",
                },
            }
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "新手引导模式" in content
