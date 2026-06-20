"""验证 .env 加载优先级：系统环境变量优先，.env 不覆盖已有值。

遵循 python-dotenv 与主流开源项目标准约定：
- 系统环境变量是权威来源（适用于生产部署时由 Docker/K8s/CI 注入密钥）
- .env 仅作本地开发兜底，不覆盖已存在的系统环境变量

这保证开源用户可以通过环境变量注入密钥，而不被代码仓库里的 .env（不提交）
或默认值干扰。用户配置密钥的参考模板见仓库根目录的 .env.example。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import models as models_module


@pytest.fixture
def isolated_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用临时 .env 文件 + 重置加载标记，隔离测试。

    - 指向 tmp_path/.env，不碰真实 .env
    - 重置 _dotenv_loaded，让每次测试都重新加载
    """
    env_file = tmp_path / ".env"
    monkeypatch.setattr(models_module, "_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(models_module, "_dotenv_loaded", False)
    return env_file


def _write_env(env_file: Path, lines: list[str]) -> None:
    """写入 .env 内容。"""
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSystemVarPriority:
    """系统环境变量必须优先于 .env 文件（核心安全语义）。"""

    def test_existing_system_var_not_overwritten(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """已存在的系统环境变量不被 .env 覆盖（核心规则）。"""
        monkeypatch.setenv("DOTENV_TEST_KEY", "system_value")
        _write_env(isolated_dotenv, ["DOTENV_TEST_KEY=env_value"])

        models_module._load_dotenv_once()

        assert os.environ["DOTENV_TEST_KEY"] == "system_value"

    def test_env_sets_var_when_system_absent(
        self, isolated_dotenv: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """系统未设置时，.env 正常注入（基础功能）。"""
        monkeypatch.delenv("DOTENV_NEW_KEY", raising=False)
        _write_env(isolated_dotenv, ["DOTENV_NEW_KEY=from_env"])

        models_module._load_dotenv_once()

        assert os.environ["DOTENV_NEW_KEY"] == "from_env"


class TestEnvFileParsing:
    """.env 文件解析健壮性（注释/空行）。"""

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
