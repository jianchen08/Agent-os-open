# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""human 工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR，F-MIGR-2）：旧 human/tool.py 是 0.1 工具壳（import
tools.builtin.base / tools.types / core.results / human_interaction 服务包，
均已删除）。迁移 = 工具壳接到 V2 自包含服务（本插件目录 models / service
平铺模块，HumanInteractionService 纯内存版）。

验证：
1. 模块可加载——顶层类型走 agentos_plugin_sdk；服务走 V2 平铺模块；
   WorkspaceAwareMixin 来自工具共享层；format_size 就地重建。
2. get_tool_definition() 返回合法 SDK Tool。
3. 核心行为（服务可注入）：choice / conversation / notification 三种模式 +
   超时/取消/拒绝异常映射 + file_paths 校验。

装配：conftest.py 注入 sdk / tools 共享层；本文件把 human 目录加入 sys.path
（与 human/server.py 的 0.2 装配语义一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_HUMAN_DIR = Path(__file__).resolve().parent.parent / "human"

if str(_HUMAN_DIR) not in sys.path:
    sys.path.insert(0, str(_HUMAN_DIR))


def _load_module() -> Any:
    """加载 human/tool.py（唯一模块名，进程内缓存）。

    裸名治理：tool.py 内 `from models import ...` / `from service import ...` 是
    human 目录的平铺模块。同一 pytest 进程里其它插件（如 channel_api）可能已把
    同名裸模块缓存进 sys.modules（sys.modules 优先于 sys.path），导致解析到错误
    模块；其它测试的 sys.path.insert(0) 也可能把 human 目录挤出最前位。因此：
    1. exec 前弹出相关裸名缓存；
    2. 把 human 目录重排到 sys.path 最前；
    3. exec 失败（半初始化）时弹出坏模块，避免后续测试拿到 AttributeError 缓存。
    """
    mod_name = "human_tool_under_test"
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
        if hasattr(module, "HumanInteractionTool"):  # 半初始化保护
            return module
        sys.modules.pop(mod_name, None)
    module_path = _HUMAN_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    for _colliding in ("models", "service", "interfaces"):
        sys.modules.pop(_colliding, None)
    if str(_HUMAN_DIR) in sys.path:
        sys.path.remove(str(_HUMAN_DIR))
    sys.path.insert(0, str(_HUMAN_DIR))
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "cannot load human tool.py"
    assert spec.loader is not None, "cannot load human tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)  # 半初始化模块不缓存
        raise
    return module


@pytest.fixture
def mod() -> Any:
    """human 工具模块（加载后可 monkeypatch 模块级依赖）。"""
    return _load_module()


def _make_service() -> MagicMock:
    """构造 HumanInteractionService mock（async 方法为 AsyncMock）。"""
    service = MagicMock()
    service.create_choice_request = AsyncMock(return_value="req-choice-1")
    service.create_conversation_request = AsyncMock(return_value="req-conv-1")
    service.send_notification = AsyncMock(return_value="req-notif-1")
    service.wait_for_choice = AsyncMock()
    service.cancel_request = AsyncMock(return_value=True)
    return service


_BASE_INPUTS = {
    "pipeline_id": "pipe-1",
    "session_id": "sess-1",
    "title": "请确认",
    "user_id": "u-1",
    "parent_agent_level": 1,
}


# ── 迁移验证：可加载 + 0.2 类型面 ──────────────────────────


class TestHumanMigration:
    """迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.HumanInteractionTool is not None
        assert callable(mod.HumanInteractionTool.get_tool_definition)

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.HumanInteractionTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "human_interaction"
        assert tool.category.value == "system"

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.HumanInteractionTool(), mod.BuiltinTool)

    def test_format_size_rebuilt_locally(self, mod):
        """0.1 formatters.format_size 就地重建（显示语义对齐）。"""
        assert mod.format_size(512) == "512B"
        assert mod.format_size(1536) == "1.5KB"
        assert mod.format_size(1048576) == "1.0MB"
        assert mod.format_size(2 * 1024 * 1024 * 1024) == "2.0GB"


# ── 核心行为：三种交互模式 ─────────────────────────────────


class TestHumanInteractionModes:
    """choice / conversation / notification 均接到 V2 service。"""

    @pytest.mark.asyncio
    async def test_choice_mode_returns_selected_option(self, mod):
        """选择模式：创建请求 → 等待用户选择 → 返回所选选项。"""
        service = _make_service()
        service.wait_for_choice.return_value = {
            "request_id": "req-choice-1",
            "response_type": "answered",
            "selected_option": "选项A",
            "feedback": "同意",
        }
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice", "options": [{"id": "a", "label": "选项A"}]})
        assert result.success is True
        assert result.output["status"] == "completed"
        assert result.output["selected_option"] == "选项A"
        assert result.output["feedback"] == "同意"
        service.create_choice_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_conversation_mode_approved_hangs_pipeline(self, mod):
        """对话模式：用户已进入对话页 → user_arrived（管道挂起语义）。"""
        service = _make_service()
        service.wait_for_choice.return_value = {
            "request_id": "req-conv-1",
            "response_type": "approved",
        }
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "conversation", "initial_message": "你好"})
        assert result.success is True
        assert result.output["status"] == "user_arrived"
        assert result.output["conversation_mode"] is True
        service.create_conversation_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notification_mode_non_blocking(self, mod):
        """通知模式：非阻塞发送后立即返回 request_id。"""
        service = _make_service()
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute(
            {**_BASE_INPUTS, "mode": "notification", "initial_message": "进度 50%", "progress": 50}
        )
        assert result.success is True
        assert result.output["status"] == "sent"
        assert result.output["request_id"] == "req-notif-1"
        # 通知模式不等待用户响应
        service.wait_for_choice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pipeline_id_rejected(self, mod):
        """缺 pipeline_id → 失败（服务端注入上下文缺失）。"""
        tool = mod.HumanInteractionTool(service=_make_service())
        result = await tool.execute({"mode": "choice", "title": "t"})
        assert result.success is False
        assert "pipeline_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_invalid_mode_rejected(self, mod):
        """非法 mode → 失败。"""
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=_make_service())
        result = await tool.execute({**_BASE_INPUTS, "mode": "bogus"})
        assert result.success is False
        assert "不支持的交互模式" in (result.error or "")


# ── 核心行为：异常映射（超时/取消/拒绝） ────────────────────


class TestHumanInteractionErrors:
    """V2 service 异常 → 结构化错误码（不向上冒泡）。"""

    @pytest.mark.asyncio
    async def test_timeout_maps_to_error_code(self, mod):
        """等待超时 → INTERACTION_TIMEOUT（Agent 可据上下文继续）。"""
        from service import InteractionTimeoutError

        service = _make_service()
        service.wait_for_choice.side_effect = InteractionTimeoutError("req-choice-1", 30)
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice"})
        assert result.success is False
        assert result.error_code == "INTERACTION_TIMEOUT"

    @pytest.mark.asyncio
    async def test_cancelled_maps_to_error_code(self, mod):
        """用户取消 → INTERACTION_CANCELLED。"""
        from service import InteractionCancelledError

        service = _make_service()
        service.wait_for_choice.side_effect = InteractionCancelledError("req-choice-1", "用户取消")
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice"})
        assert result.success is False
        assert result.error_code == "INTERACTION_CANCELLED"

    @pytest.mark.asyncio
    async def test_denied_returns_denied_result(self, mod):
        """用户拒绝 → 成功结果（status=denied），非错误（拒绝是合法结果）。"""
        from service import InteractionDeniedError

        service = _make_service()
        service.wait_for_choice.side_effect = InteractionDeniedError("req-choice-1", "我不批准")
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice"})
        assert result.success is True
        assert result.output["status"] == "denied"
        assert result.output["selected_option"] == "用户拒绝"


# ── 核心行为：file_paths 校验 ──────────────────────────────


class TestHumanFilePathsValidation:
    """file_paths 参数合法性校验（数量上限/类型/存在性）。"""

    @pytest.mark.asyncio
    async def test_too_many_file_paths_rejected(self, mod):
        """超过 MAX_FILE_PATHS 上限 → INVALID_FILE_PATHS。"""
        service = _make_service()
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        many_paths = [f"file_{i}.txt" for i in range(11)]
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice", "file_paths": many_paths})
        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"
        assert "超过最大限制" in (result.error or "")
        # 校验失败时不创建请求
        service.create_choice_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_list_file_paths_rejected(self, mod):
        """file_paths 非列表 → INVALID_FILE_PATHS。"""
        service = _make_service()
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice", "file_paths": "src/main.py"})
        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"

    @pytest.mark.asyncio
    async def test_nonexistent_file_path_rejected(self, mod, tmp_path, monkeypatch):
        """文件不存在 → INVALID_FILE_PATHS（不把坏路径透传给用户面板）。"""
        service = _make_service()
        tool = mod.HumanInteractionTool(pipeline_id="pipe-1", service=service)
        missing = str(tmp_path / "no_such_file.txt")
        result = await tool.execute({**_BASE_INPUTS, "mode": "choice", "file_paths": [missing]})
        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"
        assert "文件不存在" in (result.error or "")
