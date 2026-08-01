"""环境变量热重载测试。

覆盖两个核心改动：
1. config/models.py 的 reload_env_file() —— 基于 mtime 的 .env 重载
2. config/config_center.py 的 ConfigCenter._handle_env_change() ——
   .env 变更后触发 invalidate_all_llm_caches 重建 Router

用 tmp_path 隔离，绝不触碰真实 .env。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from config import models as models_mod
from config.config_center import ConfigCenter


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _write_env(path: Path, lines: list[str]) -> None:
    """写入 .env 文件（KEY=VALUE 格式）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    """强制推进文件 mtime（部分文件系统 mtime 精度低，确保 > 上次值）。"""
    # 写一次保证内容变化；再 utime 推进到未来，避免同秒判定未变
    future = time.time() + 5
    os.utime(path, (future, future))


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """将 models.py 的 _ENV_FILE_PATH 重定向到 tmp_path，并重置 mtime 缓存。

    用 monkeypatch.setenv 写入的测试 key 会在用例结束时自动还原，
    避免 os.environ 污染其他用例。
    """
    env_path = tmp_path / ".env"
    monkeypatch.setattr(models_mod, "_ENV_FILE_PATH", env_path)
    monkeypatch.setattr(models_mod, "_dotenv_mtime", None)
    return env_path


# ---------------------------------------------------------------------------
# reload_env_file() —— mtime 驱动重载
# ---------------------------------------------------------------------------


class TestReloadEnvFile:
    """config/models.py reload_env_file() 行为。"""

    def test_first_load_writes_environ(self, isolated_env):
        """首次加载应把 .env 内容写入 os.environ。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_HOTRELOAD_KEY=alpha"])

        changed = models_mod.reload_env_file()

        assert changed is True
        assert os.environ.get("TESTENV_HOTRELOAD_KEY") == "alpha"

    def test_unchanged_mtime_skips_reload(self, isolated_env):
        """mtime 未变时二次调用应跳过（返回 False）。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_HOTRELOAD_SKIP=keep"])

        assert models_mod.reload_env_file() is True
        # 不改 mtime，立即再调一次
        assert models_mod.reload_env_file() is False

    def test_mtime_change_triggers_reload(self, isolated_env):
        """mtime 变化 + 内容变化时应重载并返回 True。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_HOTRELOAD_V1=one"])
        assert models_mod.reload_env_file() is True
        assert os.environ.get("TESTENV_HOTRELOAD_V1") == "one"

        # 改内容 + 推进 mtime
        _write_env(env_path, ["TESTENV_HOTRELOAD_V1=two"])
        _bump_mtime(env_path)

        changed = models_mod.reload_env_file()
        assert changed is True
        assert os.environ.get("TESTENV_HOTRELOAD_V1") == "two"

    def test_missing_file_returns_false(self, isolated_env, monkeypatch):
        """文件不存在时应返回 False，不抛异常。"""
        env_path = isolated_env
        # 文件不存在（从未 _write_env）
        monkeypatch.setattr(models_mod, "_dotenv_mtime", None)
        assert models_mod.reload_env_file() is False

    def test_comments_and_blank_lines_ignored(self, isolated_env):
        """注释行和空行应被忽略。"""
        env_path = isolated_env
        _write_env(
            env_path,
            ["# a comment", "", "TESTENV_HOTRELOAD_REAL=yes", "=NOKEY"],
        )
        _bump_mtime(env_path)

        models_mod.reload_env_file()
        assert os.environ.get("TESTENV_HOTRELOAD_REAL") == "yes"

    def test_load_dotenv_once_still_works(self, isolated_env):
        """_load_dotenv_once() 保留向后兼容（内部转调 reload_env_file）。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_HOTRELOAD_COMPAT=ok"])

        # 不应抛异常
        models_mod._load_dotenv_once()
        assert os.environ.get("TESTENV_HOTRELOAD_COMPAT") == "ok"


# ---------------------------------------------------------------------------
# ConfigCenter._handle_env_change() —— 触发 LLM 缓存重建
# ---------------------------------------------------------------------------


class TestConfigCenterEnvReload:
    """ConfigCenter 对 .env 变更的处理。"""

    def test_env_change_triggers_invalidate_llm_caches(
        self, isolated_env, monkeypatch
    ):
        """_handle_env_change 在环境变量变化时应调用 invalidate_all_llm_caches。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_CC_TRIGGER=newvalue"])
        _bump_mtime(env_path)

        # 让 models.reload_env_file 能找到 tmp .env（已被 isolated_env monkeypatch）
        center = ConfigCenter(env_file_path=env_path, debounce_seconds=0.0)

        invalidate_calls: list[bool] = []
        # reload_env_file 内部读 models_mod._ENV_FILE_PATH（已重定向）
        with patch(
            "config.models.invalidate_all_llm_caches",
            side_effect=lambda: invalidate_calls.append(True),
        ) as mock_inv:
            center._handle_env_change("modified")

        assert len(invalidate_calls) == 1
        mock_inv.assert_called_once()

    def test_env_unchanged_skips_llm_rebuild(self, isolated_env):
        """.env 内容哈希未变（同内容二次触发）不应重复重建 Router。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_CC_STABLE=same"])
        _bump_mtime(env_path)

        center = ConfigCenter(env_file_path=env_path, debounce_seconds=0.0)

        with patch("config.models.invalidate_all_llm_caches") as mock_inv:
            center._handle_env_change("modified")  # 首次
            center._handle_env_change("modified")  # 哈希相同

        # 内容未变 → 第二次不应触发 invalidate（哈希去重）
        assert mock_inv.call_count == 1

    def test_deleted_env_does_not_crash(self, isolated_env):
        """.env 删除事件不应抛异常。"""
        env_path = isolated_env
        _write_env(env_path, ["TESTENV_CC_DEL=bye"])
        center = ConfigCenter(env_file_path=env_path, debounce_seconds=0.0)

        with patch("config.models.invalidate_all_llm_caches") as mock_inv:
            # 模拟删除：传 deleted 事件
            center._handle_env_change("deleted")

        # 删除不应触发重建
        mock_inv.assert_not_called()

    def test_env_file_path_default_resolution(self):
        """未传 env_file_path 时应默认指向项目根 .env。"""
        center = ConfigCenter()
        # 应是 models.py 同款推导的根 .env
        expected = (
            Path(models_mod.__file__).resolve().parent.parent.parent / ".env"
        )
        assert center._env_file_path == expected

    def test_determine_config_type_recognizes_env(self):
        """_determine_config_type 应识别 .env 系列文件为 env 类型。"""
        assert ConfigCenter._determine_config_type("/proj/.env") == "env"
        assert ConfigCenter._determine_config_type("/proj/.env.local") == "env"
        # 不影响原有判定
        assert ConfigCenter._determine_config_type("/x/agents/a.yaml") == "agent"
        assert (
            ConfigCenter._determine_config_type("/x/models/llm.yaml") == "model"
        )


# ---------------------------------------------------------------------------
# _format_key_fingerprint —— router 构建诊断日志
# ---------------------------------------------------------------------------


class TestKeyFingerprint:
    """router_factory._format_key_fingerprint 安全指纹。"""

    def test_empty_key(self):
        from llm.router_factory import _format_key_fingerprint

        assert _format_key_fingerprint("") == "EMPTY"
        assert _format_key_fingerprint(None) == "EMPTY"

    def test_unresolved_placeholder(self):
        from llm.router_factory import _format_key_fingerprint

        assert (
            _format_key_fingerprint("${OPENCODE_API_KEY}")
            == "UNRESOLVED:${OPENCODE_API_KEY}"
        )

    def test_normal_key_redacted(self):
        from llm.router_factory import _format_key_fingerprint

        key = "sk-abcd1234567890"
        fp = _format_key_fingerprint(key)
        assert fp.startswith("sk-abc")
        assert f"len={len(key)}" in fp
        # 完整 key 不应出现在指纹里
        assert key not in fp
