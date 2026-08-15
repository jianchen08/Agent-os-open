# @feature: FP-0.2.CFG 配置注入 | @ci: python-coverage
"""_config_models._expand_env_vars 的 .env 文件兜底测试（填 Key 免重启链路）。

sidecar 继承内核环境，但内核只在启动时加载一次 .env——用户在设置页
填写 API Key 后，新值只在 .env 文件里。set_config 注入时对 os.environ
未命中的 ``${VAR}`` 回退读项目根 .env（向上探测定位），使 sidecar
热重启后即拿到新 key，无需重启内核。
"""

from __future__ import annotations

import _config_models as cm
import pytest


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例重置模块级配置与 .env 缓存，隔离相互影响。"""
    monkeypatch.setattr(cm, "_config", {})
    monkeypatch.setattr(cm, "_env_cache", None)
    for var in ("CM_TEST_ENV_VAR", "CM_TEST_FILE_VAR", "CM_TEST_EXAMPLE_VAR"):
        monkeypatch.delenv(var, raising=False)


def test_env_var_hit_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CM_TEST_ENV_VAR", "from-env")
    cm.set_config({"llm": {"api_key": "${CM_TEST_ENV_VAR}"}})
    assert cm.get_config()["llm"]["api_key"] == "from-env"


def test_undefined_var_kept_verbatim() -> None:
    """os.environ 与 .env 都没有 → 保持 ${VAR} 原样（UNRESOLVED 指纹可诊断）。"""
    cm.set_config({"llm": {"api_key": "${CM_TEST_MISSING_VAR}"}})
    assert cm.get_config()["llm"]["api_key"] == "${CM_TEST_MISSING_VAR}"


def test_env_file_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """os.environ 未命中 → 回退读项目根 .env 文件。"""
    (tmp_path / ".env").write_text("CM_TEST_FILE_VAR=sk-from-env-file\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resolve_project_root", lambda: tmp_path)
    cm.set_config({"llm": {"api_key": "${CM_TEST_FILE_VAR}"}})
    assert cm.get_config()["llm"]["api_key"] == "sk-from-env-file"


def test_env_file_example_value_kept_verbatim(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """.env 里是 .env.example 式示例值（your- 开头）→ 视为未配置，保持占位符。"""
    (tmp_path / ".env").write_text("CM_TEST_EXAMPLE_VAR=your-example-key\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resolve_project_root", lambda: tmp_path)
    cm.set_config({"llm": {"api_key": "${CM_TEST_EXAMPLE_VAR}"}})
    assert cm.get_config()["llm"]["api_key"] == "${CM_TEST_EXAMPLE_VAR}"


def test_env_var_takes_priority_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """系统/进程环境变量优先于 .env 文件（与内核启动加载语义一致）。"""
    monkeypatch.setenv("CM_TEST_FILE_VAR", "from-env")
    (tmp_path / ".env").write_text("CM_TEST_FILE_VAR=stale-from-file\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resolve_project_root", lambda: tmp_path)
    cm.set_config({"llm": {"api_key": "${CM_TEST_FILE_VAR}"}})
    assert cm.get_config()["llm"]["api_key"] == "from-env"


def test_recursive_structures(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """dict/list 递归展开；非占位符字符串走 expandvars 原路径。"""
    monkeypatch.setenv("CM_TEST_ENV_VAR", "v1")
    (tmp_path / ".env").write_text("CM_TEST_FILE_VAR=sk-file\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resolve_project_root", lambda: tmp_path)
    cm.set_config(
        {
            "llm": {
                "keys": [{"api_key": "${CM_TEST_FILE_VAR}"}],
                "nested": {"a": "${CM_TEST_ENV_VAR}", "b": 42, "c": "plain"},
            }
        }
    )
    llm = cm.get_config()["llm"]
    assert llm["keys"][0]["api_key"] == "sk-file"
    assert llm["nested"]["a"] == "v1"
    assert llm["nested"]["b"] == 42
    assert llm["nested"]["c"] == "plain"
