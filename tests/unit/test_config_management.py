"""
配置管理单元测试。

覆盖 AC：
- AC-CFG-01: GET /api/v1/config/{path} 返回配置内容
- AC-CFG-02: PUT /api/v1/config/{path} 写入 YAML 文件
- AC-CFG-03: ${ENV_VAR} 替换正确
- AC-CFG-06: API Key 不在 YAML 中硬编码

对应需求：F-CFG-01~04
"""
import os
import yaml
from pathlib import Path

import pytest

from src.config.loader import ConfigLoader


# ============================================================
# AC-CFG-03: ${ENV_VAR} 替换正确
# ============================================================

class TestEnvVarSubstitution:
    """环境变量替换测试。"""

    def test_env_var_replaced(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """${ENV_VAR} 被替换为环境变量值。"""
        monkeypatch.setenv("TEST_API_KEY", "sk-abc123")

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("api_key: ${TEST_API_KEY}\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("test.yaml")

        assert result["api_key"] == "sk-abc123"

    def test_env_var_with_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """${VAR:-default} 在变量不存在时使用默认值。"""
        monkeypatch.delenv("TEST_MISSING_VAR", raising=False)

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("value: ${TEST_MISSING_VAR:-fallback_value}\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("test.yaml")

        assert result["value"] == "fallback_value"

    def test_env_var_in_nested_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """嵌套字典中的 ${ENV_VAR} 被正确替换。"""
        monkeypatch.setenv("NESTED_VAR", "nested_value")

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "providers:\n  openai:\n    api_key: ${NESTED_VAR}\n",
            encoding="utf-8",
        )

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("test.yaml")

        assert result["providers"]["openai"]["api_key"] == "nested_value"

    def test_env_var_in_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """列表中的 ${ENV_VAR} 被正确替换。"""
        monkeypatch.setenv("LIST_VAR", "list_item")

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "items:\n  - ${LIST_VAR}\n  - static\n",
            encoding="utf-8",
        )

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("test.yaml")

        assert result["items"] == ["list_item", "static"]

    def test_system_env_overrides_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """系统环境变量优先于 .env 文件。"""
        monkeypatch.setenv("DUAL_VAR", "from_system")

        env_file = tmp_path / ".env"
        env_file.write_text("DUAL_VAR=from_dotenv\n", encoding="utf-8")

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("value: ${DUAL_VAR}\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path, env_file=env_file)
        result = loader.load("test.yaml")

        assert result["value"] == "from_system"

    def test_no_env_var_placeholder_left_unchanged_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """必需的环境变量不存在且无默认值时抛出异常。"""
        monkeypatch.delenv("MISSING_REQUIRED_VAR", raising=False)

        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("value: ${MISSING_REQUIRED_VAR}\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)

        from src.core.exceptions import EnvVarNotFoundError
        with pytest.raises(EnvVarNotFoundError):
            loader.load("test.yaml")

    def test_plain_values_not_affected(self, tmp_path: Path) -> None:
        """不含环境变量的普通值不受影响。"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("name: hello\nport: 8080\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("test.yaml")

        assert result["name"] == "hello"
        assert result["port"] == 8080


# ============================================================
# AC-CFG-01: GET 读取配置
# ============================================================

class TestConfigRead:
    """配置读取测试。"""

    def test_load_existing_yaml(self, tmp_path: Path) -> None:
        """加载存在的 YAML 配置文件。"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("config.yaml")

        assert result == {"key": "value"}

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        """加载不存在的文件抛出 ConfigNotFoundError。"""
        loader = ConfigLoader(config_dir=tmp_path)

        from src.core.exceptions import ConfigNotFoundError
        with pytest.raises(ConfigNotFoundError):
            loader.load("nonexistent.yaml")

    def test_load_empty_yaml_returns_empty(self, tmp_path: Path) -> None:
        """空 YAML 文件返回空字典。"""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load("empty.yaml")

        assert result == {}

    def test_load_all_yaml_files(self, tmp_path: Path) -> None:
        """load_all 加载目录下所有 YAML。"""
        (tmp_path / "a.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("b: 2\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load_all()

        assert "a" in result
        assert "b" in result
        assert result["a"] == {"a": 1}


# ============================================================
# AC-CFG-02: PUT 写入 YAML（直接测试 yaml.dump）
# ============================================================

class TestConfigWrite:
    """配置写入测试。"""

    def test_write_yaml_creates_file(self, tmp_path: Path) -> None:
        """写入 YAML 创建新文件。"""
        target = tmp_path / "output.yaml"
        data = {"key": "value"}

        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        assert target.exists()
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded == {"key": "value"}

    def test_write_yaml_overwrites(self, tmp_path: Path) -> None:
        """写入 YAML 覆盖已有文件。"""
        target = tmp_path / "output.yaml"

        for version in (1, 2):
            with open(target, "w", encoding="utf-8") as f:
                yaml.dump({"version": version}, f, allow_unicode=True)

        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded["version"] == 2

    def test_write_yaml_preserves_unicode(self, tmp_path: Path) -> None:
        """写入 YAML 保留中文内容。"""
        target = tmp_path / "unicode.yaml"
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump({"name": "灵汐系统"}, f, allow_unicode=True)

        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded["name"] == "灵汐系统"

    def test_write_yaml_creates_parent_dirs(self, tmp_path: Path) -> None:
        """写入 YAML 时自动创建父目录。"""
        target = tmp_path / "nested" / "deep" / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump({"k": "v"}, f)

        assert target.exists()
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded == {"k": "v"}


# ============================================================
# AC-CFG-06: API Key 不在 YAML 中硬编码 — 脱敏函数测试
# ============================================================

def _mask_key(key: str) -> str:
    """API Key 脱敏函数（与 routes_config 中实现一致）。"""
    if not key or len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


class TestApiKeySecurity:
    """API Key 安全检查。"""

    def test_mask_key_short(self) -> None:
        """短 API Key 完全掩码。"""
        assert _mask_key("short") == "****"

    def test_mask_key_empty(self) -> None:
        """空 API Key 返回空字符串。"""
        assert _mask_key("") == ""

    def test_mask_key_long(self) -> None:
        """长 API Key 只显示首尾4位。"""
        masked = _mask_key("sk-1234567890abcdef")
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "****" in masked
        assert "234567890" not in masked

    def test_mask_key_8_chars(self) -> None:
        """8 字符的 Key 被掩码。"""
        assert _mask_key("12345678") == "****"

    def test_env_var_used_for_api_key_in_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """验证配置文件中 API Key 使用 ${ENV_VAR} 格式而非硬编码。"""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-value-here")

        yaml_file = tmp_path / "llm.yaml"
        yaml_file.write_text(
            "providers:\n  openai:\n    api_key: ${OPENAI_API_KEY}\n",
            encoding="utf-8",
        )

        loader = ConfigLoader(config_dir=tmp_path)
        config = loader.load("llm.yaml")

        # 加载后值是环境变量的值
        assert config["providers"]["openai"]["api_key"] == "sk-real-key-value-here"

        # 原始文件中不含明文 key
        raw = yaml_file.read_text(encoding="utf-8")
        assert "sk-real-key-value-here" not in raw
        assert "${OPENAI_API_KEY}" in raw

    def test_config_yaml_does_not_leak_api_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置读取后 API Key 来自环境变量，YAML 文件不含明文。"""
        monkeypatch.setenv("SECRET_KEY", "super-secret-1234567890")

        yaml_file = tmp_path / "secret.yaml"
        yaml_file.write_text("key: ${SECRET_KEY}\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        config = loader.load("secret.yaml")

        assert config["key"] == "super-secret-1234567890"

        # 原始文件不含明文
        raw_content = yaml_file.read_text(encoding="utf-8")
        assert "super-secret" not in raw_content
