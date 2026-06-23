"""验证 .env 加载优先级：.env 文件强制覆盖系统环境变量。

本项目以 .env 作为本地环境配置的唯一真相源。即便系统环境变量已存在
同名变量（如容器宿主／IDE 启动配置注入），.env 中的值也会将其覆盖，
确保用户通过编辑 .env 文件配置的密钥和选项能够生效。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import models as models_module


@pytest.fixture
def isolated_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用临时 .env 文件 + 重置加载标记，隔离测试。"""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(models_module, "_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(models_module, "_dotenv_loaded", False)
    return env_file


def _write_env(env_file: Path, lines: list[str]) -> None:
    """写入 .env 内容。"""
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestEnvOverridesSystemVar:
    """.env 文件值必须强制覆盖已存在的系统环境变量。"""

    def test_env_overrides_preexisting_system_var(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """已存在的系统环境变量必须被 .env 覆盖（核心规则）。"""
        monkeypatch.setenv("DOTENV_TEST_KEY", "system_value")
        _write_env(isolated_dotenv, ["DOTENV_TEST_KEY=env_value"])

        models_module._load_dotenv_once()

        assert os.environ["DOTENV_TEST_KEY"] == "env_value"

    def test_env_sets_var_when_system_absent(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """系统未设置时，.env 正常注入。"""
        monkeypatch.delenv("DOTENV_NEW_KEY", raising=False)
        _write_env(isolated_dotenv, ["DOTENV_NEW_KEY=from_env"])

        models_module._load_dotenv_once()

        assert os.environ["DOTENV_NEW_KEY"] == "from_env"


class TestEnvFileParsing:
    """.env 文件解析健壮性。"""

    def test_skips_comments_and_blank_lines(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """注释行和空行被跳过。"""
        _write_env(isolated_dotenv, [
            "# 这是注释",
            "",
            "DOTENV_REAL_KEY=real_value",
            "   ",
        ])

        models_module._load_dotenv_once()

        assert os.environ["DOTENV_REAL_KEY"] == "real_value"


class TestIdempotency:
    """_load_dotenv_once 只加载一次。"""

    def test_repeated_call_does_not_reread_file(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """未重置标记时，第二次调用不重读 .env。"""
        monkeypatch.delenv("DOTENV_ONCE", raising=False)
        _write_env(isolated_dotenv, ["DOTENV_ONCE=first_env"])
        models_module._load_dotenv_once()
        assert os.environ["DOTENV_ONCE"] == "first_env"

        # 改文件但不重置标记 → 第二次调用应被跳过
        _write_env(isolated_dotenv, ["DOTENV_ONCE=second_env"])
        models_module._load_dotenv_once()
        assert os.environ["DOTENV_ONCE"] == "first_env"


class TestMissingEnvFile:
    """.env 文件不存在时的行为。"""

    def test_missing_env_file_is_noop(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """.env 不存在时不报错，已有环境变量保持不变。"""
        monkeypatch.setenv("DOTENV_KEEP", "keep_me")

        models_module._load_dotenv_once()  # 不应抛异常

        assert os.environ["DOTENV_KEEP"] == "keep_me"
