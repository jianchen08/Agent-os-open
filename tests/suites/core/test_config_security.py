"""config.py 安全与合并逻辑单元测试。

覆盖：
  - P9: _import_class 白名单检查，拒绝不在白名单中的模块
  - M6: build_plugin_registry 中 loader 配置合并而非替换

所有测试使用 Mock，不依赖真实服务。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.config import PipelineConfig, _import_class, build_plugin_registry
from pipeline.route import InputRouteTable, OutputRouteTable


# ---------------------------------------------------------------------------
# P9: _import_class 白名单检查
# ---------------------------------------------------------------------------


class TestImportClassWhitelist:
    """P9: _import_class 白名单安全检查测试。"""

    @pytest.mark.unit
    def test_reject_os_module(self) -> None:
        """P9: 拒绝 os 模块（不在白名单中）。"""
        with pytest.raises(ImportError, match="Security"):
            _import_class("os.path")

    @pytest.mark.unit
    def test_reject_sys_module(self) -> None:
        """P9: 拒绝 sys 模块（不在白名单中）。"""
        with pytest.raises(ImportError, match="Security"):
            _import_class("sys.exit")

    @pytest.mark.unit
    def test_reject_arbitrary_module(self) -> None:
        """P9: 拒绝任意非白名单前缀模块（如 evil.module.Class）。"""
        with pytest.raises(ImportError, match="Security"):
            _import_class("evil.module.MyClass")

    @pytest.mark.unit
    def test_reject_subprocess_module(self) -> None:
        """P9: 拒绝 subprocess 模块（不在白名单中）。"""
        with pytest.raises(ImportError, match="Security"):
            _import_class("subprocess.Popen")

    @pytest.mark.unit
    def test_reject_importlib_module(self) -> None:
        """P9: 拒绝 importlib 模块（不在白名单中）。"""
        with pytest.raises(ImportError, match="Security"):
            _import_class("importlib.import_module")

    @pytest.mark.unit
    @patch("importlib.import_module")
    def test_accept_plugins_prefix(self, mock_import: MagicMock) -> None:
        """P9: 接受 plugins. 前缀的模块。"""
        mock_module = MagicMock()
        mock_class = type("MyPlugin", (), {})
        mock_module.MyPlugin = mock_class
        mock_import.return_value = mock_module

        result = _import_class("plugins.my_plugin.MyPlugin")
        assert result is mock_class

    @pytest.mark.unit
    @patch("importlib.import_module")
    def test_accept_pipeline_prefix(self, mock_import: MagicMock) -> None:
        """P9: 接受 pipeline. 前缀的模块。"""
        mock_module = MagicMock()
        mock_class = type("MyPlugin", (), {})
        mock_module.MyPlugin = mock_class
        mock_import.return_value = mock_module

        result = _import_class("pipeline.core.MyPlugin")
        assert result is mock_class

    @pytest.mark.unit
    @patch("importlib.import_module")
    def test_accept_agents_prefix(self, mock_import: MagicMock) -> None:
        """P9: 接受 agents. 前缀的模块。"""
        mock_module = MagicMock()
        mock_class = type("MyAgent", (), {})
        mock_module.MyAgent = mock_class
        mock_import.return_value = mock_module

        result = _import_class("agents.specialized.MyAgent")
        assert result is mock_class

    @pytest.mark.unit
    @patch("importlib.import_module")
    def test_accept_tools_prefix(self, mock_import: MagicMock) -> None:
        """P9: 接受 tools. 前缀的模块。"""
        mock_module = MagicMock()
        mock_class = type("MyTool", (), {})
        mock_module.MyTool = mock_class
        mock_import.return_value = mock_module

        result = _import_class("plugins.tools.MyTool")
        assert result is mock_class

    @pytest.mark.unit
    @patch("importlib.import_module")
    def test_reject_non_class_attribute(self, mock_import: MagicMock) -> None:
        """P9: 导入目标不是类时抛出 ImportError。"""
        mock_module = MagicMock()
        mock_module.not_a_class = "just a string"
        mock_import.return_value = mock_module

        with pytest.raises(ImportError, match="not a class"):
            _import_class("plugins.my_plugin.not_a_class")

    @pytest.mark.unit
    def test_reject_no_dot_in_path(self) -> None:
        """P9: 无点号的路径（无法分离模块和类名）应抛出异常。"""
        with pytest.raises((ImportError, ValueError)):
            _import_class("justanidentifier")


# ---------------------------------------------------------------------------
# M6: build_plugin_registry 配置合并
# ---------------------------------------------------------------------------


class TestBuildPluginRegistryConfigMerge:
    """M6: build_plugin_registry 中 loader 配置合并而非替换。"""

    @pytest.mark.unit
    @patch("pipeline.config._import_class")
    def test_llm_config_merged_not_replaced(self, mock_import: MagicMock) -> None:
        """M6: llm_call 插件配置应与 model_loader 配置合并，而非替换。

        场景：
          core_plugins 中 llm_call 配置了 temperature=0.7
          model_loader 返回 model_name="gpt-4" 和 api_key="sk-xxx"
          最终插件配置应同时包含 temperature 和 model_name
        """
        # Setup mock 插件类
        mock_plugin_instance = MagicMock()
        mock_plugin_class = MagicMock(return_value=mock_plugin_instance)
        mock_import.return_value = mock_plugin_class

        # Setup model_loader mock
        model_loader = MagicMock()
        model_loader.get_default_model.return_value = {
            "_id": "gpt-4",
            "display_name": "GPT-4",
            "model_name": "gpt-4",
        }
        model_loader.get_llm_core_config.return_value = {
            "model_name": "gpt-4",
            "api_key": "sk-xxx",
            "api_base": "https://api.openai.com/v1",
        }

        # 创建配置：llm_call 带 temperature 但不带 model_name
        config = PipelineConfig(
            name="test_merge",
            input_route_table=InputRouteTable([]),
            output_route_table=OutputRouteTable([]),
            core_plugins={
                "llm_call": {
                    "class": "plugins.llm.LLMPlugin",
                    "config": {"temperature": 0.7},
                },
            },
        )

        build_plugin_registry(config, model_loader)

        # 验证插件被创建
        mock_plugin_class.assert_called_once()
        call_kwargs = mock_plugin_class.call_args[1]
        merged_config = call_kwargs["config"]

        # M6: 原始配置保留
        assert "temperature" in merged_config
        assert merged_config["temperature"] == 0.7

        # M6: model_loader 配置合入
        assert "model_name" in merged_config
        assert merged_config["model_name"] == "gpt-4"
        assert "api_key" in merged_config
        assert "api_base" in merged_config

    @pytest.mark.unit
    @patch("pipeline.config._import_class")
    def test_llm_config_not_merged_when_model_name_set(
        self, mock_import: MagicMock
    ) -> None:
        """M6: 当 plugin_config 已指定 model_name 时不合并 loader 配置。

        场景：
          core_plugins 中 llm_call 配置了 model_name="local-model"
          即使 model_loader 可用，也不应覆盖已有 model_name
        """
        mock_plugin_instance = MagicMock()
        mock_plugin_class = MagicMock(return_value=mock_plugin_instance)
        mock_import.return_value = mock_plugin_class

        model_loader = MagicMock()

        config = PipelineConfig(
            name="test_no_merge",
            input_route_table=InputRouteTable([]),
            output_route_table=OutputRouteTable([]),
            core_plugins={
                "llm_call": {
                    "class": "plugins.llm.LLMPlugin",
                    "config": {"model_name": "local-model", "temperature": 0.5},
                },
            },
        )

        build_plugin_registry(config, model_loader)

        # model_loader.get_default_model 不应被调用
        model_loader.get_default_model.assert_not_called()

        # 验证插件配置保持原样
        call_kwargs = mock_plugin_class.call_args[1]
        original_config = call_kwargs["config"]
        assert original_config["model_name"] == "local-model"
        assert original_config["temperature"] == 0.5

    @pytest.mark.unit
    @patch("pipeline.config._import_class")
    def test_llm_config_no_loader(self, mock_import: MagicMock) -> None:
        """M6: 无 model_loader 时使用原始 plugin_config，不合并。"""
        mock_plugin_instance = MagicMock()
        mock_plugin_class = MagicMock(return_value=mock_plugin_instance)
        mock_import.return_value = mock_plugin_class

        config = PipelineConfig(
            name="test_no_loader",
            input_route_table=InputRouteTable([]),
            output_route_table=OutputRouteTable([]),
            core_plugins={
                "llm_call": {
                    "class": "plugins.llm.LLMPlugin",
                    "config": {"temperature": 0.9},
                },
            },
        )

        build_plugin_registry(config, model_loader=None)

        call_kwargs = mock_plugin_class.call_args[1]
        assert call_kwargs["config"] == {"temperature": 0.9}
