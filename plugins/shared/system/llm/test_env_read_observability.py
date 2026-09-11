# @feature: FP-T12 前端适配 | @ci: python-coverage
""".env 读取失败可观测性测试（C3）。

锁定契约：stat/read_text 的 OSError（文件被占/权限等）→ warn 带 path 与
异常摘要 + 返回空表（key 缺失根因可观测，不再无声吞掉）；FileNotFoundError
（无 .env）保持首启正常语义静默返回。

open/stat 属文件系统外部边界，失败注入用 monkeypatch（按路径后缀命中，
不误伤其他 Path 调用）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _DIR.parents[1]
for _p in (str(_DIR), str(_SHARED_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, filename: str) -> Any:
    """按显式路径加载模块（裸名防劫持）。"""
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_DIR / filename))
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ccm() -> Any:
    return _load("env_obs_config_models_test", "_config_models.py")


@pytest.fixture()
def rlc() -> Any:
    m = _load("env_obs_routes_llm_config_test", "routes_llm_config.py")
    m.invalidate_all_llm_caches = None
    m.get_config_center = None
    return m


def _patch_path_call(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    target_suffix: str,
    exc: Exception,
) -> None:
    """让指定 Path 方法对路径后缀命中的调用抛异常，其余走真实现。"""
    import pathlib

    real = getattr(pathlib.Path, method_name)

    def wrapper(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        if str(self).endswith(target_suffix):
            raise exc
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, method_name, wrapper)


class TestConfigModelsEnvReadWarn:
    def test_stat_permission_error_warns_and_returns_empty(
        self, ccm: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """mtime 探测遇 PermissionError → warn 含 path 与异常摘要 + 空表。"""
        ccm._env_cache = None
        env_file = str(ccm._resolve_project_root() / ".env")
        _patch_path_call(monkeypatch, "stat", ".env", PermissionError("env locked"))
        with caplog.at_level(logging.WARNING, logger=ccm.__name__):
            assert ccm._env_file_vars() == {}
        msgs = [r.getMessage() for r in caplog.records if ".env 读取失败" in r.getMessage()]
        assert msgs and env_file in msgs[0] and "env locked" in msgs[0]

    def test_read_permission_error_warns_and_returns_empty(
        self, ccm: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """read_text 遇 PermissionError → warn + 空表（两处 except 均覆盖）。"""
        ccm._env_cache = None
        env_file = str(ccm._resolve_project_root() / ".env")
        Path(env_file).write_text("K=v\n", encoding="utf-8")  # 让 stat 成功、read 失败
        _patch_path_call(monkeypatch, "read_text", ".env", PermissionError("read denied"))
        with caplog.at_level(logging.WARNING, logger=ccm.__name__):
            assert ccm._env_file_vars() == {}
        msgs = [r.getMessage() for r in caplog.records if ".env 读取失败" in r.getMessage()]
        assert msgs and "read denied" in msgs[0]
        assert ccm._env_cache is None  # 失败结果不入缓存（下次重读）

    def test_missing_env_file_stays_silent(
        self, ccm: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """无 .env（FileNotFoundError）→ 静默空表、无告警（首启正常语义不变）。"""
        ccm._env_cache = None
        monkeypatch.setattr(ccm, "_resolve_project_root", lambda: tmp_path)
        with caplog.at_level(logging.WARNING, logger=ccm.__name__):
            assert ccm._env_file_vars() == {}
        assert not [r for r in caplog.records if ".env 读取失败" in r.getMessage()]


class TestRoutesLlmConfigEnvReadWarn:
    def test_stat_permission_error_warns_and_returns_empty(
        self, rlc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """mtime 探测遇 PermissionError → warn 含 path 与异常摘要 + 空表。"""
        env_file = tmp_path / "project.env"
        monkeypatch.setattr(rlc, "_ENV_FILE", env_file)
        rlc._env_file_cache = None
        _patch_path_call(monkeypatch, "stat", "project.env", PermissionError("env locked"))
        with caplog.at_level(logging.WARNING, logger=rlc.__name__):
            assert rlc._env_file_vars() == {}
        msgs = [r.getMessage() for r in caplog.records if ".env 读取失败" in r.getMessage()]
        assert msgs and str(env_file) in msgs[0] and "env locked" in msgs[0]

    def test_missing_env_file_stays_silent(
        self, rlc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """无 .env（FileNotFoundError）→ 静默空表、无告警（首启正常语义不变）。"""
        monkeypatch.setattr(rlc, "_ENV_FILE", tmp_path / "absent.env")
        rlc._env_file_cache = None
        with caplog.at_level(logging.WARNING, logger=rlc.__name__):
            assert rlc._env_file_vars() == {}
        assert not [r for r in caplog.records if ".env 读取失败" in r.getMessage()]
