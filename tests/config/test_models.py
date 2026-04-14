"""ModelConfigLoader 单元测试。

覆盖：
- ModelConfigLoader 加载 llm.yaml / embedding.yaml
- get_model_config 精确查找
- get_default_model 返回默认模型
- 环境变量替换逻辑
- get_provider_config 返回提供商配置
- get_llm_core_config 返回 LLMCore 格式配置
- ConfigSchemaValidator.validate_model_config 校验
- resolve_env_or_model 环境变量回退逻辑
- load_pipeline_config 环境变量替换与回退
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from config.models import ModelConfigLoader, _substitute_env_vars
from config.schema import ConfigSchemaValidator


# ── Fixture ────────────────────────────────────────────────────

@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """创建临时配置目录，写入 llm.yaml 和 embedding.yaml。"""
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    llm_data = {
        "models": {
            "minimax-m2.7": {
                "provider": "minimax",
                "model_name": "MiniMax-M2.7",
                "display_name": "MiniMax-M2.7",
                "api_base": "https://api.minimax.chat/v1",
                "api_key": "sk-minimax-test-key",
                "context_window": 204800,
                "reasoning_model": True,
                "default_params": {"temperature": 0.7, "max_tokens": 8192},
            },
            "deepseek-chat": {
                "provider": "deepseek",
                "model_name": "deepseek-chat",
                "display_name": "DeepSeek Chat",
                "api_base": "https://api.deepseek.com/v1",
                "api_key": "sk-deepseek-test-key",
                "context_window": 6000,
                "default_params": {"temperature": 0.7, "max_tokens": 4096},
            },
            "glm-5-turbo": {
                "provider": "zhipu_coding",
                "model_name": "glm-5-turbo",
                "display_name": "GLM-5-Turbo",
                "context_window": 200000,
                "reasoning_model": True,
                "default_params": {"temperature": 0.7, "max_tokens": 16384},
            },
        },
        "defaults": {
            "chat": "minimax-m2.7",
            "reasoning": "minimax-m2.7",
        },
        "providers": {
            "minimax": {
                "api_key": "sk-minimax-test-key",
                "api_base": "https://api.minimax.chat/v1",
            },
            "deepseek": {
                "api_key": "sk-deepseek-test-key",
                "api_base": "https://api.deepseek.com/v1",
            },
            "zhipu_coding": {
                "api_key": "sk-zhipu-test-key",
                "api_base": "https://open.bigmodel.cn/api/coding/paas/v4/",
            },
        },
    }
    with open(models_dir / "llm.yaml", "w", encoding="utf-8") as f:
        yaml.dump(llm_data, f, allow_unicode=True)

    emb_data = {
        "embeddings": {
            "zhipu": {
                "provider": "zhipu",
                "model_name": "embedding-3",
                "display_name": "智谱 Embedding-3",
                "dimension": 1024,
                "api_base": "https://open.bigmodel.cn/api/paas/v4/",
                "max_batch_size": 100,
            },
        },
        "default_embedding": "zhipu",
        "providers": {
            "zhipu": {
                "api_key": "${APP_ZHIPU_API_KEY}",
                "api_base": "https://open.bigmodel.cn/api/paas/v4/",
            },
        },
    }
    with open(models_dir / "embedding.yaml", "w", encoding="utf-8") as f:
        yaml.dump(emb_data, f, allow_unicode=True)

    return models_dir


@pytest.fixture
def loader(config_dir: Path) -> ModelConfigLoader:
    """创建基于临时配置目录的 ModelConfigLoader。"""
    return ModelConfigLoader(config_dir=config_dir)


# ── 加载测试 ────────────────────────────────────────────────────


class TestModelConfigLoaderLoad:
    """测试 ModelConfigLoader 的加载能力。"""

    def test_load_llm_data(self, loader: ModelConfigLoader) -> None:
        """LLM 配置能正确加载。"""
        data = loader._load_llm_data()
        assert "models" in data
        assert "minimax-m2.7" in data["models"]
        assert "defaults" in data
        assert "providers" in data

    def test_load_embedding_data(self, loader: ModelConfigLoader) -> None:
        """嵌入模型配置能正确加载。"""
        data = loader._load_embedding_data()
        assert "embeddings" in data
        assert "zhipu" in data["embeddings"]

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """配置文件不存在时抛出 FileNotFoundError。"""
        loader = ModelConfigLoader(config_dir=tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError, match="模型配置文件不存在"):
            loader._load_llm_data()


# ── get_model_config 测试 ────────────────────────────────────────


class TestGetModelConfig:
    """测试 get_model_config 方法。"""

    def test_get_existing_llm_model(self, loader: ModelConfigLoader) -> None:
        """能获取 LLM 模型配置。"""
        config = loader.get_model_config("minimax-m2.7")
        assert config is not None
        assert config["provider"] == "minimax"
        assert config["model_name"] == "MiniMax-M2.7"
        assert config["api_key"] == "sk-minimax-test-key"

    def test_get_existing_deepseek_model(self, loader: ModelConfigLoader) -> None:
        """能获取 DeepSeek 模型配置。"""
        config = loader.get_model_config("deepseek-chat")
        assert config is not None
        assert config["provider"] == "deepseek"

    def test_get_embedding_model(self, loader: ModelConfigLoader) -> None:
        """能获取嵌入模型配置。"""
        config = loader.get_model_config("zhipu")
        assert config is not None
        assert config["model_name"] == "embedding-3"

    def test_get_nonexistent_model(self, loader: ModelConfigLoader) -> None:
        """不存在的模型返回 None。"""
        config = loader.get_model_config("nonexistent-model")
        assert config is None

    def test_model_config_is_copy(self, loader: ModelConfigLoader) -> None:
        """返回的配置是副本，修改不影响内部数据。"""
        config1 = loader.get_model_config("minimax-m2.7")
        assert config1 is not None
        config1["api_key"] = "modified"
        config2 = loader.get_model_config("minimax-m2.7")
        assert config2 is not None
        assert config2["api_key"] == "sk-minimax-test-key"


# ── get_default_model 测试 ────────────────────────────────────────


class TestGetDefaultModel:
    """测试 get_default_model 方法。"""

    def test_default_chat_model(self, loader: ModelConfigLoader) -> None:
        """默认 chat 模型是 minimax-m2.7。"""
        config = loader.get_default_model("chat")
        assert config is not None
        assert config["model_name"] == "MiniMax-M2.7"

    def test_default_reasoning_model(self, loader: ModelConfigLoader) -> None:
        """默认 reasoning 模型是 minimax-m2.7。"""
        config = loader.get_default_model("reasoning")
        assert config is not None
        assert config["model_name"] == "MiniMax-M2.7"

    def test_default_embedding_model(self, loader: ModelConfigLoader) -> None:
        """默认嵌入模型是 zhipu。"""
        config = loader.get_default_model("embedding")
        assert config is not None
        assert config["model_name"] == "embedding-3"

    def test_default_unknown_type(self, loader: ModelConfigLoader) -> None:
        """未知类型返回 None。"""
        config = loader.get_default_model("unknown_type")
        assert config is None


# ── get_provider_config 测试 ────────────────────────────────────


class TestGetProviderConfig:
    """测试 get_provider_config 方法。"""

    def test_get_minimax_provider(self, loader: ModelConfigLoader) -> None:
        """能获取 minimax 提供商配置。"""
        config = loader.get_provider_config("minimax")
        assert config is not None
        assert "api_key" in config
        assert "api_base" in config
        assert config["api_key"] == "sk-minimax-test-key"
        assert "minimax.chat" in config["api_base"]

    def test_get_deepseek_provider(self, loader: ModelConfigLoader) -> None:
        """能获取 deepseek 提供商配置。"""
        config = loader.get_provider_config("deepseek")
        assert config is not None
        assert config["api_key"] == "sk-deepseek-test-key"

    def test_get_nonexistent_provider(self, loader: ModelConfigLoader) -> None:
        """不存在的提供商返回 None。"""
        config = loader.get_provider_config("nonexistent")
        assert config is None

    def test_provider_config_is_copy(self, loader: ModelConfigLoader) -> None:
        """返回的配置是副本。"""
        config1 = loader.get_provider_config("minimax")
        assert config1 is not None
        config1["api_key"] = "modified"
        config2 = loader.get_provider_config("minimax")
        assert config2 is not None
        assert config2["api_key"] == "sk-minimax-test-key"


# ── get_llm_core_config 测试 ────────────────────────────────────


class TestGetLLMCoreConfig:
    """测试 get_llm_core_config 方法。"""

    def test_llm_core_config_minimax(self, loader: ModelConfigLoader) -> None:
        """MiniMax 模型的 LLMCore 格式配置正确。"""
        config = loader.get_llm_core_config("minimax-m2.7")
        assert config is not None
        assert config["provider"] == "minimax"
        assert config["model_name"] == "MiniMax-M2.7"
        assert config["api_base"] == "https://api.minimax.chat/v1"
        assert config["api_key"] == "sk-minimax-test-key"
        assert "default_params" in config
        assert config["default_params"]["temperature"] == 0.7

    def test_llm_core_config_deepseek(self, loader: ModelConfigLoader) -> None:
        """DeepSeek 模型的 LLMCore 格式配置正确。"""
        config = loader.get_llm_core_config("deepseek-chat")
        assert config is not None
        assert config["provider"] == "deepseek"
        assert config["api_key"] == "sk-deepseek-test-key"

    def test_llm_core_config_model_without_api_key(
        self, loader: ModelConfigLoader
    ) -> None:
        """模型自身无 api_key 时回退到提供商配置。"""
        config = loader.get_llm_core_config("glm-5-turbo")
        assert config is not None
        assert config["provider"] == "zhipu_coding"
        # 模型自身没有 api_key，应回退到 zhipu_coding 提供商的 api_key
        assert config["api_key"] == "sk-zhipu-test-key"

    def test_llm_core_config_nonexistent(self, loader: ModelConfigLoader) -> None:
        """不存在的模型返回 None。"""
        config = loader.get_llm_core_config("nonexistent")
        assert config is None

    def test_llm_core_config_default_params(
        self, loader: ModelConfigLoader
    ) -> None:
        """default_params 正确传递。"""
        config = loader.get_llm_core_config("minimax-m2.7")
        assert config is not None
        assert config["default_params"]["max_tokens"] == 8192


# ── 环境变量替换测试 ──────────────────────────────────────────


class TestEnvVarSubstitution:
    """测试环境变量替换逻辑。"""

    def test_substitute_simple(self) -> None:
        """简单环境变量替换。"""
        with patch.dict(os.environ, {"MY_KEY": "abc123"}, clear=False):
            result = _substitute_env_vars("${MY_KEY}")
            assert result == "abc123"

    def test_substitute_missing_env(self) -> None:
        """环境变量不存在时替换为空字符串。"""
        with patch.dict(os.environ, {}, clear=False):
            # 确保变量不存在
            os.environ.pop("NONEXISTENT_VAR_12345", None)
            result = _substitute_env_vars("${NONEXISTENT_VAR_12345}")
            assert result == ""

    def test_substitute_embedded(self) -> None:
        """环境变量嵌入在字符串中。"""
        with patch.dict(os.environ, {"HOST": "localhost"}, clear=False):
            result = _substitute_env_vars("http://${HOST}:8080")
            assert result == "http://localhost:8080"

    def test_substitute_dict(self) -> None:
        """字典中的环境变量递归替换。"""
        with patch.dict(os.environ, {"API_KEY": "secret"}, clear=False):
            data = {"key": "${API_KEY}", "nested": {"inner": "${API_KEY}"}}
            result = _substitute_env_vars(data)
            assert result["key"] == "secret"
            assert result["nested"]["inner"] == "secret"

    def test_substitute_list(self) -> None:
        """列表中的环境变量递归替换。"""
        with patch.dict(os.environ, {"VAL": "42"}, clear=False):
            data = ["${VAL}", "static", {"k": "${VAL}"}]
            result = _substitute_env_vars(data)
            assert result[0] == "42"
            assert result[1] == "static"
            assert result[2]["k"] == "42"

    def test_substitute_non_string_passthrough(self) -> None:
        """非字符串类型原样返回。"""
        assert _substitute_env_vars(42) == 42
        assert _substitute_env_vars(True) is True
        assert _substitute_env_vars(None) is None

    def test_embedding_env_var_substitution(
        self, loader: ModelConfigLoader
    ) -> None:
        """嵌入模型配置中的 ${APP_ZHIPU_API_KEY} 被替换。"""
        with patch.dict(os.environ, {"APP_ZHIPU_API_KEY": "zhipu-real-key"}, clear=False):
            # 需要重新加载以触发环境变量替换
            loader._embedding_data = None
            provider = loader.get_provider_config("zhipu")
            assert provider is not None
            assert provider["api_key"] == "zhipu-real-key"

    def test_embedding_env_var_empty(
        self, loader: ModelConfigLoader
    ) -> None:
        """嵌入模型配置中的环境变量不存在时替换为空字符串。"""
        # 确保环境变量不存在
        os.environ.pop("APP_ZHIPU_API_KEY", None)
        loader._embedding_data = None
        provider = loader.get_provider_config("zhipu")
        assert provider is not None
        assert provider["api_key"] == ""


# ── resolve_env_or_model 测试 ──────────────────────────────────


class TestResolveEnvOrModel:
    """测试 resolve_env_or_model 方法。"""

    def test_resolve_with_env_set(self, loader: ModelConfigLoader) -> None:
        """环境变量已设置时使用环境变量值。"""
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "env-value"}, clear=False):
            result = loader.resolve_env_or_model("${MINIMAX_API_KEY}", "minimax")
            assert result == "env-value"

    def test_resolve_with_env_missing_fallback(
        self, loader: ModelConfigLoader
    ) -> None:
        """环境变量未设置时回退到提供商配置。"""
        os.environ.pop("MINIMAX_API_KEY", None)
        result = loader.resolve_env_or_model("${MINIMAX_API_KEY}", "minimax")
        assert result == "sk-minimax-test-key"

    def test_resolve_no_provider(self, loader: ModelConfigLoader) -> None:
        """无提供商名称且环境变量不存在时返回空字符串。"""
        os.environ.pop("NONEXISTENT_KEY_99999", None)
        result = loader.resolve_env_or_model("${NONEXISTENT_KEY_99999}")
        assert result == ""

    def test_resolve_non_env_string(self, loader: ModelConfigLoader) -> None:
        """非环境变量字符串原样返回。"""
        result = loader.resolve_env_or_model("static-value", "minimax")
        assert result == "static-value"


# ── ConfigSchemaValidator.validate_model_config 测试 ──────────────


class TestValidateModelConfig:
    """测试 ConfigSchemaValidator.validate_model_config 方法。"""

    def test_valid_model_config(self) -> None:
        """合法模型配置通过校验。"""
        data = {
            "models": {
                "test-model": {
                    "provider": "test",
                    "model_name": "test-model-v1",
                }
            },
            "defaults": {"chat": "test-model"},
            "providers": {"test": {"api_key": "key", "api_base": "http://test"}},
        }
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert errors == []

    def test_missing_models(self) -> None:
        """缺少 models 字段报错。"""
        data = {"providers": {}}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("缺少必填字段: models" in e for e in errors)

    def test_models_not_dict(self) -> None:
        """models 不是字典报错。"""
        data = {"models": "invalid"}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("models 必须为字典" in e for e in errors)

    def test_model_missing_provider(self) -> None:
        """模型缺少 provider 报错。"""
        data = {"models": {"test": {"model_name": "test"}}}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("缺少必填字段: provider" in e for e in errors)

    def test_model_missing_model_name(self) -> None:
        """模型缺少 model_name 报错。"""
        data = {"models": {"test": {"provider": "test"}}}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("缺少必填字段: model_name" in e for e in errors)

    def test_model_empty_provider(self) -> None:
        """provider 为空字符串报错。"""
        data = {"models": {"test": {"provider": "", "model_name": "test"}}}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("provider 必须为非空字符串" in e for e in errors)

    def test_defaults_not_dict(self) -> None:
        """defaults 不是字典报错。"""
        data = {"models": {}, "defaults": "invalid"}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("defaults 必须为字典" in e for e in errors)

    def test_providers_not_dict(self) -> None:
        """providers 不是字典报错。"""
        data = {"models": {}, "providers": "invalid"}
        validator = ConfigSchemaValidator()
        errors = validator.validate_model_config(data)
        assert any("providers 必须为字典" in e for e in errors)

    def test_validate_yaml_file_model_type(
        self, tmp_path: Path
    ) -> None:
        """validate_yaml_file 自动检测 model 类型。"""
        model_data = {
            "models": {
                "test": {"provider": "test", "model_name": "test"},
            },
            "providers": {"test": {"api_key": "k"}},
        }
        yaml_path = tmp_path / "models" / "test.yaml"
        yaml_path.parent.mkdir(parents=True)
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(model_data, f)

        validator = ConfigSchemaValidator()
        errors = validator.validate_yaml_file(yaml_path)
        assert errors == []


# ── load_pipeline_config 环境变量替换测试 ──────────────────────


class TestPipelineConfigEnvVars:
    """测试 load_pipeline_config 中的环境变量替换。"""

    def test_env_var_substitution_in_pipeline(
        self, tmp_path: Path
    ) -> None:
        """管道配置中的 ${ENV_VAR} 被正确替换。"""
        from pipeline.config import load_pipeline_config

        pipeline_yaml = textwrap.dedent("""\
            name: test_pipeline
            input_routes:
              - name: default
                condition: ""
                target: core
                plugins: []
                priority: 0
            output_routes:
              - route_type: end
                condition: ""
                priority: 0
            core_plugins:
              llm_call:
                class: agent_os.plugins.core.llm_core.LLMCore
                config:
                  provider: minimax
                  model_name: MiniMax-M2.7
                  api_key: "${MINIMAX_API_KEY}"
                  api_base: "https://api.minimax.chat/v1"
        """)
        pipe_path = tmp_path / "test_pipeline.yaml"
        pipe_path.write_text(pipeline_yaml, encoding="utf-8")

        with patch.dict(os.environ, {"MINIMAX_API_KEY": "env-minimax-key"}, clear=False):
            config = load_pipeline_config(pipe_path)
            assert config.core_plugins["llm_call"]["config"]["api_key"] == "env-minimax-key"

    def test_env_var_fallback_to_model_loader(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        """环境变量不存在时回退到 ModelConfigLoader 的提供商配置。"""
        from pipeline.config import load_pipeline_config

        pipeline_yaml = textwrap.dedent("""\
            name: test_pipeline
            input_routes:
              - name: default
                condition: ""
                target: core
                plugins: []
                priority: 0
            output_routes:
              - route_type: end
                condition: ""
                priority: 0
            core_plugins:
              llm_call:
                class: agent_os.plugins.core.llm_core.LLMCore
                config:
                  provider: minimax
                  model_name: MiniMax-M2.7
                  api_key: "${MINIMAX_API_KEY}"
                  api_base: "https://api.minimax.chat/v1"
        """)
        pipe_path = tmp_path / "test_pipeline.yaml"
        pipe_path.write_text(pipeline_yaml, encoding="utf-8")

        # 确保环境变量不存在
        os.environ.pop("MINIMAX_API_KEY", None)

        loader = ModelConfigLoader(config_dir=config_dir)
        config = load_pipeline_config(pipe_path, model_loader=loader)
        # 应回退到模型配置中的 api_key
        assert config.core_plugins["llm_call"]["config"]["api_key"] == "sk-minimax-test-key"

    def test_no_fallback_without_model_loader(
        self, tmp_path: Path
    ) -> None:
        """无 ModelConfigLoader 时环境变量不存在替换为空字符串。"""
        from pipeline.config import load_pipeline_config

        pipeline_yaml = textwrap.dedent("""\
            name: test_pipeline
            input_routes:
              - name: default
                condition: ""
                target: core
                plugins: []
                priority: 0
            output_routes:
              - route_type: end
                condition: ""
                priority: 0
            core_plugins:
              llm_call:
                class: agent_os.plugins.core.llm_core.LLMCore
                config:
                  api_key: "${NONEXISTENT_KEY_XYZ}"
        """)
        pipe_path = tmp_path / "test_pipeline.yaml"
        pipe_path.write_text(pipeline_yaml, encoding="utf-8")

        os.environ.pop("NONEXISTENT_KEY_XYZ", None)
        config = load_pipeline_config(pipe_path)
        assert config.core_plugins["llm_call"]["config"]["api_key"] == ""
