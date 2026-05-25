"""保留区 + 长期记忆提取功能测试。

测试 PreservedZone / MemoryExtraction 数据模型、
ContextCompressor.extract_preserved / extract_long_term_memory 方法、
模块导出和硬约束验证。
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import fields as dataclass_fields
from unittest.mock import AsyncMock

import pytest

# -----------------------------------------------------------
# 修补缺失模块，使 import 链不在环境中报错
# src.db.connection / src.memory.context_repository 在此测试中不被使用，
# 但 src.memory.compressor.__init__ 间接导入了它们。
# -----------------------------------------------------------

# 需要注册为 package-like 的模块（带 __path__ 以支持 from X.Y import Z）
_pkg_modules = ["src.db", "src.core", "src.utils"]
for _mod_name in _pkg_modules:
    if _mod_name not in sys.modules:
        _m = types.ModuleType(_mod_name)
        _m.__path__ = []  # 使其看起来像一个 package
        _m.__package__ = _mod_name
        sys.modules[_mod_name] = _m

# 需要注册的叶子模块
_leaf_modules = [
    "src.db.connection",
    "src.db.models",
    "src.core.tokenizer",
    "src.utils.message_id_helper",
    "src.memory.context_repository",
]
for _mod_name in _leaf_modules:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)

# 为 mock 模块添加必要的属性，防止 from ... import Name 报错
_mock_attrs: dict[str, list[str]] = {
    "src.core.tokenizer": ["get_token_counter"],
    "src.memory.context_repository": ["ContextRepository"],
    "src.db.connection": ["get_session_context"],
    "src.db.models": ["ExecutionRecord"],
    "src.utils.message_id_helper": ["generate_execution_record_id"],
}
for _mod, _attrs in _mock_attrs.items():
    for _attr in _attrs:
        if not hasattr(sys.modules[_mod], _attr):
            setattr(sys.modules[_mod], _attr, None)

from memory.context_compressor import CompressionConfig, ContextCompressor

# 从 context_compressor 模块获取模型类引用
# context_compressor.py 内部通过 from src.memory.compressor.models import ... 导入，
# 我们使用同一份引用，确保 isinstance 检查一致
import memory.context_compressor as _cc_mod

PreservedZone = _cc_mod.PreservedZone
MemoryExtraction = _cc_mod.MemoryExtraction


# ============================================================
# 1. PreservedZone 数据模型测试
# ============================================================


class TestPreservedZone:
    """测试 PreservedZone 数据模型。"""

    def test_包含5个字段(self) -> None:
        """PreservedZone 应恰好包含 5 个字段。"""
        field_names = [f.name for f in dataclass_fields(PreservedZone)]
        expected = [
            "user_requirements",
            "key_decisions",
            "execution_plan",
            "constraints",
            "pending_tasks",
        ]
        assert field_names == expected

    def test_所有字段默认值为空字符串(self) -> None:
        """无参构造时所有字段应为空字符串。"""
        zone = PreservedZone()
        assert zone.user_requirements == ""
        assert zone.key_decisions == ""
        assert zone.execution_plan == ""
        assert zone.constraints == ""
        assert zone.pending_tasks == ""

    def test_可传参构造(self) -> None:
        """应支持传入参数构造。"""
        zone = PreservedZone(
            user_requirements="需求A",
            key_decisions="决策B",
            execution_plan="计划C",
            constraints="约束D",
            pending_tasks="任务E",
        )
        assert zone.user_requirements == "需求A"
        assert zone.key_decisions == "决策B"
        assert zone.execution_plan == "计划C"
        assert zone.constraints == "约束D"
        assert zone.pending_tasks == "任务E"


# ============================================================
# 2. MemoryExtraction 数据模型测试
# ============================================================


class TestMemoryExtraction:
    """测试 MemoryExtraction 数据模型。"""

    def test_包含3个字段(self) -> None:
        """MemoryExtraction 应恰好包含 3 个字段。"""
        field_names = [f.name for f in dataclass_fields(MemoryExtraction)]
        expected = [
            "user_profile_updates",
            "project_knowledge_updates",
            "experience_updates",
        ]
        assert field_names == expected

    def test_所有字段默认值为空字符串(self) -> None:
        """无参构造时所有字段应为空字符串。"""
        ext = MemoryExtraction()
        assert ext.user_profile_updates == ""
        assert ext.project_knowledge_updates == ""
        assert ext.experience_updates == ""

    def test_可传参构造(self) -> None:
        """应支持传入参数构造。"""
        ext = MemoryExtraction(
            user_profile_updates="偏好A",
            project_knowledge_updates="知识B",
            experience_updates="经验C",
        )
        assert ext.user_profile_updates == "偏好A"
        assert ext.project_knowledge_updates == "知识B"
        assert ext.experience_updates == "经验C"


# ============================================================
# 3. extract_preserved 方法测试
# ============================================================


class TestExtractPreserved:
    """测试 ContextCompressor.extract_preserved 方法。"""

    @pytest.mark.asyncio
    async def test_空消息列表返回空PreservedZone(self) -> None:
        """空消息列表应返回空 PreservedZone。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.extract_preserved([])
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == ""
        assert result.key_decisions == ""
        assert result.execution_plan == ""
        assert result.constraints == ""
        assert result.pending_tasks == ""

    @pytest.mark.asyncio
    async def test_正常提取调用LLM并解析JSON(self) -> None:
        """正常提取时应调用 LLM 并返回解析后的 PreservedZone。"""
        response_json = json.dumps({
            "user_requirements": "实现登录功能",
            "key_decisions": "使用JWT认证",
            "execution_plan": "1. 写接口 2. 测试",
            "constraints": "必须兼容旧版API",
            "pending_tasks": "单元测试未完成",
        })
        llm_fn = AsyncMock(return_value=response_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)

        messages = [
            {"role": "user", "content": "帮我实现登录"},
            {"role": "assistant", "content": "好的，开始实现"},
        ]
        result = await compressor.extract_preserved(messages)
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == "实现登录功能"
        assert result.key_decisions == "使用JWT认证"
        assert result.execution_plan == "1. 写接口 2. 测试"
        assert result.constraints == "必须兼容旧版API"
        assert result.pending_tasks == "单元测试未完成"
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_LLM返回空响应时返回空PreservedZone(self) -> None:
        """LLM 返回空响应时应返回空 PreservedZone。"""
        llm_fn = AsyncMock(return_value="")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == ""

    @pytest.mark.asyncio
    async def test_LLM返回仅空白时返回空PreservedZone(self) -> None:
        """LLM 返回仅空白字符时应返回空 PreservedZone。"""
        llm_fn = AsyncMock(return_value="   \n  ")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == ""

    @pytest.mark.asyncio
    async def test_JSON解析失败时返回空PreservedZone(self) -> None:
        """JSON 解析失败时应返回空 PreservedZone。"""
        llm_fn = AsyncMock(return_value="这不是有效的JSON内容")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == ""

    @pytest.mark.asyncio
    async def test_异常时返回空PreservedZone不抛出(self) -> None:
        """LLM 抛异常时应返回空 PreservedZone，不向上抛出。"""
        llm_fn = AsyncMock(side_effect=RuntimeError("连接失败"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, PreservedZone)
        assert result.user_requirements == ""

    @pytest.mark.asyncio
    async def test_old_preserved参数传入prompt(self) -> None:
        """old_preserved 参数应正确传入 prompt。"""
        captured_prompt = ""

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return '{"user_requirements":""}'

        compressor = ContextCompressor(llm_call_fn=capture_llm)
        old_pz = json.dumps({"user_requirements": "旧需求"})
        await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}],
            old_preserved=old_pz,
        )
        assert old_pz in captured_prompt

    @pytest.mark.asyncio
    async def test_previous_l1和user_message参数传入prompt(self) -> None:
        """previous_l1 和 user_message 参数应正确传入 prompt。"""
        captured_prompt = ""

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return '{"user_requirements":""}'

        compressor = ContextCompressor(llm_call_fn=capture_llm)
        await compressor.extract_preserved(
            [{"role": "user", "content": "消息内容"}],
            previous_l1="前次L1摘要",
            user_message="当前用户消息",
        )
        assert "前次L1摘要" in captured_prompt
        assert "当前用户消息" in captured_prompt

    @pytest.mark.asyncio
    async def test_空user_message时从messages中自动提取(self) -> None:
        """user_message 为空时应从 messages 中自动提取最后一条用户消息。"""
        captured_prompt = ""

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return '{"user_requirements":""}'

        compressor = ContextCompressor(llm_call_fn=capture_llm)
        await compressor.extract_preserved(
            [
                {"role": "user", "content": "第一条用户消息"},
                {"role": "assistant", "content": "回复"},
                {"role": "user", "content": "最后的用户消息"},
            ],
            user_message="",  # 空值，应自动提取
        )
        assert "最后的用户消息" in captured_prompt

    @pytest.mark.asyncio
    async def test_部分字段缺失时用空字符串填充(self) -> None:
        """LLM 返回的 JSON 缺少部分字段时，缺失字段用空字符串填充。"""
        response_json = json.dumps({
            "user_requirements": "只有需求",
            "execution_plan": "只有计划",
        })
        llm_fn = AsyncMock(return_value=response_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_preserved(
            [{"role": "user", "content": "测试"}]
        )
        assert result.user_requirements == "只有需求"
        assert result.key_decisions == ""
        assert result.execution_plan == "只有计划"
        assert result.constraints == ""
        assert result.pending_tasks == ""


# ============================================================
# 4. extract_long_term_memory 方法测试
# ============================================================


class TestExtractLongTermMemory:
    """测试 ContextCompressor.extract_long_term_memory 方法。"""

    @pytest.mark.asyncio
    async def test_空消息列表返回空MemoryExtraction(self) -> None:
        """空消息列表应返回空 MemoryExtraction。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.extract_long_term_memory([])
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == ""
        assert result.project_knowledge_updates == ""
        assert result.experience_updates == ""

    @pytest.mark.asyncio
    async def test_正常提取调用LLM并解析JSON(self) -> None:
        """正常提取时应调用 LLM 并返回解析后的 MemoryExtraction。"""
        response_json = json.dumps({
            "user_profile_updates": "用户偏好TypeScript",
            "project_knowledge_updates": "项目使用React架构",
            "experience_updates": "踩坑：async不配合await会返回Promise",
        })
        llm_fn = AsyncMock(return_value=response_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)

        messages = [
            {"role": "user", "content": "我用TypeScript"},
            {"role": "assistant", "content": "好的"},
        ]
        result = await compressor.extract_long_term_memory(messages)
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == "用户偏好TypeScript"
        assert result.project_knowledge_updates == "项目使用React架构"
        assert result.experience_updates == "踩坑：async不配合await会返回Promise"
        llm_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_LLM返回空响应时返回空MemoryExtraction(self) -> None:
        """LLM 返回空响应时应返回空 MemoryExtraction。"""
        llm_fn = AsyncMock(return_value="")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == ""

    @pytest.mark.asyncio
    async def test_LLM返回仅空白时返回空MemoryExtraction(self) -> None:
        """LLM 返回仅空白字符时应返回空 MemoryExtraction。"""
        llm_fn = AsyncMock(return_value="   \n  ")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == ""

    @pytest.mark.asyncio
    async def test_JSON解析失败时返回空MemoryExtraction(self) -> None:
        """JSON 解析失败时应返回空 MemoryExtraction。"""
        llm_fn = AsyncMock(return_value="这不是有效的JSON内容")
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == ""

    @pytest.mark.asyncio
    async def test_异常时返回空MemoryExtraction不抛出(self) -> None:
        """LLM 抛异常时应返回空 MemoryExtraction，不向上抛出。"""
        llm_fn = AsyncMock(side_effect=RuntimeError("连接失败"))
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}]
        )
        assert isinstance(result, MemoryExtraction)
        assert result.user_profile_updates == ""

    @pytest.mark.asyncio
    async def test_previous_l1和user_message参数传入prompt(self) -> None:
        """previous_l1 和 user_message 参数应正确传入 prompt。"""
        captured_prompt = ""

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return '{"user_profile_updates":""}'

        compressor = ContextCompressor(llm_call_fn=capture_llm)
        await compressor.extract_long_term_memory(
            [{"role": "user", "content": "消息内容"}],
            previous_l1="前次L1摘要",
            user_message="当前用户消息",
        )
        assert "前次L1摘要" in captured_prompt
        assert "当前用户消息" in captured_prompt

    @pytest.mark.asyncio
    async def test_空user_message时从messages中自动提取(self) -> None:
        """user_message 为空时应从 messages 中自动提取最后一条用户消息。"""
        captured_prompt = ""

        async def capture_llm(prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return '{"user_profile_updates":""}'

        compressor = ContextCompressor(llm_call_fn=capture_llm)
        await compressor.extract_long_term_memory(
            [
                {"role": "user", "content": "第一条用户消息"},
                {"role": "assistant", "content": "回复"},
                {"role": "user", "content": "最后的用户消息"},
            ],
            user_message="",  # 空值，应自动提取
        )
        assert "最后的用户消息" in captured_prompt

    @pytest.mark.asyncio
    async def test_部分字段缺失时用空字符串填充(self) -> None:
        """LLM 返回的 JSON 缺少部分字段时，缺失字段用空字符串填充。"""
        response_json = json.dumps({
            "user_profile_updates": "偏好信息",
        })
        llm_fn = AsyncMock(return_value=response_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.extract_long_term_memory(
            [{"role": "user", "content": "测试"}]
        )
        assert result.user_profile_updates == "偏好信息"
        assert result.project_knowledge_updates == ""
        assert result.experience_updates == ""


# ============================================================
# 5. 模块导出测试
# ============================================================


class TestModuleExports:
    """测试模块导出。"""

    def test_从compressor_models模块导入PreservedZone(self) -> None:
        """from memory.compressor.models import PreservedZone 应正常导入且可实例化。"""
        from memory.compressor.models import PreservedZone as PZ

        assert PZ.__name__ == "PreservedZone"
        zone = PZ()
        assert zone.user_requirements == ""

    def test_从compressor_models模块导入MemoryExtraction(self) -> None:
        """from memory.compressor.models import MemoryExtraction 应正常导入且可实例化。"""
        from memory.compressor.models import MemoryExtraction as ME

        assert ME.__name__ == "MemoryExtraction"
        ext = ME()
        assert ext.user_profile_updates == ""

    def test_PreservedZone可通过models模块访问(self) -> None:
        """PreservedZone 应可通过 models 模块正常访问。"""
        import memory.compressor.models as models

        assert hasattr(models, "PreservedZone")

    def test_MemoryExtraction可通过models模块访问(self) -> None:
        """MemoryExtraction 应可通过 models 模块正常访问。"""
        import memory.compressor.models as models

        assert hasattr(models, "MemoryExtraction")


# ============================================================
# 6. 硬约束验证测试
# ============================================================


class TestHardConstraints:
    """验证新增功能不破坏既有行为。"""

    @pytest.mark.asyncio
    async def test_compress_all返回值结构不变(self) -> None:
        """compress_all 返回值仍包含 l1/l2/keywords 三个键。"""
        response_json = json.dumps({
            "l1": {"session_title": "测试", "current_state": "进行中"},
            "l2": {"intent": "测试意图", "process": "步骤", "results": "结果"},
            "keywords": ["测试"],
        })
        llm_fn = AsyncMock(return_value=response_json)
        compressor = ContextCompressor(llm_call_fn=llm_fn)
        result = await compressor.compress_all(
            [{"role": "user", "content": "测试"}]
        )
        assert "l1" in result
        assert "l2" in result
        assert "keywords" in result
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_compress_all空消息返回结构不变(self) -> None:
        """compress_all 空消息时返回结构仍是 l1/l2/keywords。"""
        compressor = ContextCompressor(llm_call_fn=AsyncMock())
        result = await compressor.compress_all([])
        assert result == {"l1": "", "l2": "", "keywords": []}

    def test_COMPRESS_PROMPT未被修改(self) -> None:
        """COMPRESS_PROMPT 模板应保持原样（包含关键标识符）。"""
        prompt = ContextCompressor.COMPRESS_PROMPT
        assert "{previous_l1_section}" in prompt
        assert "{user_message}" in prompt
        assert "{messages}" in prompt
        assert "l1" in prompt
        assert "l2" in prompt
        assert "keywords" in prompt
        assert "session_title" in prompt
