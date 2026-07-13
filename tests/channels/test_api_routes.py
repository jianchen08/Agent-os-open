"""API 路由基础功能测试。

覆盖 src/channels/api/ 下核心路由模块的基础功能：
- routes_config: YAML 读写工具函数
- routes_agents: Agent 列表/详情查询
- routes_tools: 工具列表/详情查询

使用 FastAPI TestClient 进行端点测试，Mock 外部依赖。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))


# ═══════════════════════════════════════════════════════════
# routes_config: YAML 工具函数单元测试
# ═══════════════════════════════════════════════════════════


class TestConfigYamlUtils:
    """routes_config 内部 YAML 工具函数测试。"""

    def test_mask_key_short_key(self) -> None:
        """短密钥（≤8字符）完全遮蔽。"""
        from channels.api.routes_config import _mask_key

        assert _mask_key("abc") == "****"
        assert _mask_key("12345678") == "****"

    def test_mask_key_empty(self) -> None:
        """空密钥返回空字符串。"""
        from channels.api.routes_config import _mask_key

        assert _mask_key("") == ""

    def test_mask_key_long_key(self) -> None:
        """长密钥保留前4后4，中间遮蔽。"""
        from channels.api.routes_config import _mask_key

        result = _mask_key("sk-abcdefghijklmnop")
        assert result.startswith("sk-a")
        assert result.endswith("mnop")
        assert "********" in result

    def test_read_yaml_valid_file(self) -> None:
        """读取有效 YAML 文件。"""
        from channels.api.routes_config import _read_yaml

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump({"key": "value", "nested": {"a": 1}}, f)
            tmp_path = Path(f.name)

        try:
            data = _read_yaml(tmp_path)
            assert data["key"] == "value"
            assert data["nested"]["a"] == 1
        finally:
            tmp_path.unlink()

    def test_read_yaml_nonexistent_file_raises_404(self) -> None:
        """读取不存在的文件抛出 HTTPException(404)。"""
        from fastapi import HTTPException

        from channels.api.routes_config import _read_yaml

        with pytest.raises(HTTPException) as exc_info:
            _read_yaml(Path("/nonexistent/path/config.yaml"))
        assert exc_info.value.status_code == 404
        assert "配置文件不存在" in exc_info.value.detail

    def test_read_yaml_empty_file(self) -> None:
        """空 YAML 文件返回空字典。"""
        from channels.api.routes_config import _read_yaml

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write("")
            tmp_path = Path(f.name)

        try:
            data = _read_yaml(tmp_path)
            assert data == {}
        finally:
            tmp_path.unlink()

    def test_write_yaml_creates_file(self) -> None:
        """写入 YAML 数据到文件。"""
        from channels.api.routes_config import _write_yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_output.yaml"
            data = {"name": "test", "enabled": True, "count": 42}
            _write_yaml(path, data)

            assert path.exists()
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            assert loaded["name"] == "test"
            assert loaded["enabled"] is True
            assert loaded["count"] == 42

    def test_write_yaml_creates_parent_dirs(self) -> None:
        """写入时自动创建父目录。"""
        from channels.api.routes_config import _write_yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "dir" / "config.yaml"
            _write_yaml(path, {"test": True})
            assert path.exists()

    def test_write_yaml_preserves_unicode(self) -> None:
        """写入 YAML 保留中文字符（不转义）。"""
        from channels.api.routes_config import _write_yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unicode.yaml"
            _write_yaml(path, {"描述": "中文内容", "名称": "灵汐"})

            content = path.read_text(encoding="utf-8")
            assert "描述" in content
            assert "灵汐" in content

    def test_read_write_roundtrip(self) -> None:
        """读写往返：写入后读回数据一致。"""
        from channels.api.routes_config import _read_yaml, _write_yaml

        original = {
            "string_val": "hello",
            "int_val": 100,
            "float_val": 3.14,
            "bool_val": False,
            "list_val": [1, 2, 3],
            "nested": {"key": "value"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "roundtrip.yaml"
            _write_yaml(path, original)
            loaded = _read_yaml(path)

        assert loaded == original


# ═══════════════════════════════════════════════════════════
# routes_agents: Agent 响应模型测试
# ═══════════════════════════════════════════════════════════


class TestAgentResponseModels:
    """Agent API 响应模型验证。"""

    def test_agent_response_model_fields(self) -> None:
        """AgentResponse 包含所有必要字段。"""
        from channels.api.models import AgentResponse

        resp = AgentResponse(
            config_id="main",
            name="灵汐",
            display_name="灵汐",
            description="主Agent",
            agent_type="MAIN",
            category="general",
            level="L1",
            system_prompt="你是灵汐",
            tool_ids=["file_read"],
            tags=["core"],
            is_active=True,
            version="1.0.0",
        )
        assert resp.config_id == "main"
        assert resp.level == "L1"
        assert resp.is_active is True

    def test_agent_list_response(self) -> None:
        """AgentListResponse 包含 total 和 agents。"""
        from channels.api.models import AgentListResponse, AgentResponse

        agents = [
            AgentResponse(
                config_id="a1", name="Agent1", display_name="A1",
                description="", agent_type="SPECIALIZED", category="code",
                level="L2", system_prompt="", tool_ids=[], tags=[],
                is_active=True, version="1.0.0",
            ),
        ]
        list_resp = AgentListResponse(total=1, items=agents)
        assert list_resp.total == 1
        assert len(list_resp.items) == 1


class TestConfigToResponseModel:
    """_config_to_response 模型解析测试。

    验证 model 字段解析与运行时 apply_agent_model_override 逻辑一致：
    model_tier 解析优先，model_name 兜底。
    """

    def test_model_tier_resolved(self) -> None:
        """model_tier 配置时，通过 resolve_tier 解析为真实模型标识。"""
        from agents.types import AgentConfig, AgentLevel, AgentType
        from channels.api.routes_agents import _config_to_response

        cfg = AgentConfig(
            config_id="lingxi",
            name="灵汐",
            display_name="灵汐",
            agent_type=AgentType.MAIN,
            level=AgentLevel.L1_MAIN,
            model_name="",
            model_tier="large",
        )
        with patch(
            "pipeline.plugin_resolver.resolve_tier",
            return_value="glm-5.2",
        ):
            resp = _config_to_response(cfg)
        assert resp.model == "glm-5.2"

    def test_model_name_fallback(self) -> None:
        """无 model_tier 时，直接使用 model_name。"""
        from agents.types import AgentConfig, AgentLevel, AgentType
        from channels.api.routes_agents import _config_to_response

        cfg = AgentConfig(
            config_id="custom",
            name="custom",
            model_name="minimax-m3",
            model_tier="",
            agent_type=AgentType.SPECIALIZED,
            level=AgentLevel.L3_ATOMIC,
        )
        resp = _config_to_response(cfg)
        assert resp.model == "minimax-m3"

    def test_empty_model_when_both_absent(self) -> None:
        """model_tier 和 model_name 均空时，model 为空字符串。"""
        from agents.types import AgentConfig, AgentLevel, AgentType
        from channels.api.routes_agents import _config_to_response

        cfg = AgentConfig(
            config_id="plain",
            name="plain",
            model_name="",
            model_tier="",
            agent_type=AgentType.SPECIALIZED,
            level=AgentLevel.L3_ATOMIC,
        )
        resp = _config_to_response(cfg)
        assert resp.model == ""



# ═══════════════════════════════════════════════════════════
# routes_tools: Tool 响应模型测试
# ═══════════════════════════════════════════════════════════


class TestToolResponseModels:
    """Tool API 响应模型验证。"""

    def test_tool_response_model_fields(self) -> None:
        """ToolResponse 包含所有必要字段。"""
        from channels.api.models import ToolResponse

        resp = ToolResponse(
            name="file_read",
            description="读取文件内容",
            category="file",
            source="builtin",
            level="all",
            status="active",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            when_to_use=["需要读取文件时"],
            when_not_to_use=["文件不存在时"],
            caveats=["大文件可能超时"],
            version="1.0.0",
            tags=["file", "core"],
            requires_approval=False,
        )
        assert resp.name == "file_read"
        assert resp.category == "file"
        assert resp.requires_approval is False

    def test_tool_list_response(self) -> None:
        """ToolListResponse 包含 total 和 tools。"""
        from channels.api.models import ToolListResponse, ToolResponse

        tools = [
            ToolResponse(
                name="t1", description="", category="file", source="builtin",
                level="all", status="active", parameters={},
                when_to_use=[], when_not_to_use=[], caveats=[],
                version="1.0.0", tags=[], requires_approval=False,
            ),
        ]
        list_resp = ToolListResponse(total=1, items=tools)
        assert list_resp.total == 1
        assert len(list_resp.items) == 1


# ═══════════════════════════════════════════════════════════
# routes_config: API 端点集成测试（Mock 文件系统）
# ═══════════════════════════════════════════════════════════


class TestConfigEndpoints:
    """配置 API 端点测试（使用 FastAPI TestClient）。"""

    def setup_method(self) -> None:
        """创建临时配置目录和 FastAPI 测试应用。"""
        from fastapi import FastAPI

        from channels.api.routes_config import router

        self.tmpdir = tempfile.mkdtemp()
        self.app = FastAPI()
        self.app.include_router(router)

    def _write_config(self, filename: str, data: dict) -> Path:
        """在临时目录写入配置文件。"""
        path = Path(self.tmpdir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
        return path

    def test_read_yaml_function_directly(self) -> None:
        """直接测试 _read_yaml 函数正常路径。"""
        from channels.api.routes_config import _read_yaml

        path = self._write_config("test.yaml", {"enabled": True, "max": 10})
        data = _read_yaml(path)
        assert data["enabled"] is True
        assert data["max"] == 10

    def test_write_yaml_function_directly(self) -> None:
        """直接测试 _write_yaml 函数正常路径。"""
        from channels.api.routes_config import _read_yaml, _write_yaml

        path = Path(self.tmpdir) / "new_config.yaml"
        _write_yaml(path, {"new_key": "new_value"})
        data = _read_yaml(path)
        assert data["new_key"] == "new_value"

    def test_mask_key_various_lengths(self) -> None:
        """密钥遮蔽函数边界测试。"""
        from channels.api.routes_config import _mask_key

        # 空字符串
        assert _mask_key("") == ""
        # 1字符
        assert _mask_key("a") == "****"
        # 恰好8字符
        assert _mask_key("12345678") == "****"
        # 9字符（首次保留头尾）
        result = _mask_key("123456789")
        assert result.startswith("1234")
        assert result.endswith("6789")
