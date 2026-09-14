# @feature: FP-T07 llm api | @ci: python-coverage
"""routes_thinking_mode 缺口补测：项目根探测回退分支（routes_thinking_mode.py 49）。

契约（``_resolve_project_root`` docstring）：按 ``config/`` + ``config/kernel/``
双特征向上探测项目根；全部祖先均不含该特征时，回退 ``__file__`` 的固定上溯
（sidecar 形态下的最后兜底，不做硬编码 parent×N）。

不可达说明（逐条）：
- 无——49 行可达：把 ``__file__`` 指向一个祖先链不含 ``config/kernel`` 的
  真实临时目录（真实文件系统，非 mock），探测循环全部落空后即走回退。

llm.yaml 路径经模块属性重定向（``_LLM_YAML`` 是模块级常量，指向探测结果；
重定向属测试注入惯例，同目录 test_llm_http.py 先例）。不触网。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)


def _load() -> Any:
    """按唯一模块名加载 routes_thinking_mode（防裸名跨插件串扰）。"""
    mod_name = "llm_gaps_routes_thinking_mode_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "routes_thinking_mode.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_resolve_project_root_falls_back_when_no_config_kernel_in_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """祖先链无 config/kernel → 回退 __file__ 上溯 4 级（真实临时目录）。

    回退值性质断言：必定是「锚定目录的上溯结果」且不等于探测命中值——
    锁定语义（侧车形态兜底）而非钉死平台路径拼接细节。
    """
    rtm = _load()
    deep = tmp_path / "no-proj" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    fake_file = deep / "routes_thinking_mode.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(rtm, "__file__", str(fake_file))

    root = rtm._resolve_project_root()

    expected = fake_file.resolve().parent.parent.parent.parent
    assert root == expected
    assert not (root / "config" / "kernel").is_dir()


def test_resolve_project_root_finds_ancestor_with_config_and_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区分度输入（正例）：祖先含 config/ + config/kernel/ → 命中该祖先。

    与上一条仅「特征目录是否存在」一面不同，共同锁定探测优先于回退。
    """
    rtm = _load()
    (tmp_path / "config" / "kernel").mkdir(parents=True)
    deep = tmp_path / "plugins" / "shared" / "system" / "llm"
    deep.mkdir(parents=True)
    fake_file = deep / "routes_thinking_mode.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(rtm, "__file__", str(fake_file))

    assert rtm._resolve_project_root() == tmp_path


def test_get_llm_data_raises_config_missing_for_absent_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llm.yaml 缺失 → ThinkingModeAPIError(500, LLM_CONFIG_MISSING)（fail-closed）。

    配置缺失必须显式报错，禁止伪造空 models 让 health 假 ok。
    """
    rtm = _load()
    monkeypatch.setattr(rtm, "_LLM_YAML", tmp_path / "nope.yaml")

    with pytest.raises(rtm.ThinkingModeAPIError) as ei:
        rtm.health()

    assert ei.value.status_code == 500
    assert ei.value.error_code == "LLM_CONFIG_MISSING"


def test_get_llm_data_raises_config_malformed_for_non_mapping_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llm.yaml 内容非映射（空/列表）→ ThinkingModeAPIError(500, MALFORMED)。

    区分度输入：文件存在但内容形态非法（区别于上一条的「不存在」）。
    """
    rtm = _load()
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setattr(rtm, "_LLM_YAML", yaml_path)

    with pytest.raises(rtm.ThinkingModeAPIError) as ei:
        rtm.health()

    assert ei.value.status_code == 500
    assert ei.value.error_code == "LLM_CONFIG_MALFORMED"


_REAL_YAML = """\
defaults:
  chat: reason-m1
models:
  reason-m1:
    display_name: Reason M1
    reasoning_model: true
    default_params:
      max_tokens: 4096
  plain-m2:
    display_name: Plain M2
    reasoning_model: false
    default_params:
      max_tokens: 2048
"""


@pytest.fixture
def rtm_with_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """真实 llm.yaml 文件 + 模块属性重定向（真实数据驱动，非 mock）。"""
    rtm = _load()
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text(_REAL_YAML, encoding="utf-8")
    monkeypatch.setattr(rtm, "_LLM_YAML", yaml_path)
    return rtm


def test_health_counts_reasoning_models_only(rtm_with_yaml: Any) -> None:
    """health 只计 reasoning_model=true 的模型（1 个），形态含 service 名。"""
    assert rtm_with_yaml.health() == {
        "status": "ok",
        "available_models": 1,
        "service": "thinking-mode",
    }


def test_list_models_returns_only_reasoning_models(rtm_with_yaml: Any) -> None:
    """list_models 仅返回思考模型；同一模型名在 check 面 supports_thinking=true。"""
    listed = rtm_with_yaml.list_models()
    assert [m["model_name"] for m in listed] == ["reason-m1"]
    assert listed[0]["thinking_type"] == "parameter_switch"
    assert rtm_with_yaml.check_support("reason-m1")["supports_thinking"] is True
    assert rtm_with_yaml.check_support("plain-m2")["supports_thinking"] is False


def test_switch_mode_unknown_model_and_flags(rtm_with_yaml: Any) -> None:
    """switch_mode：未知模型 → switch_type=none 且 params 空；启用态注入
    reasoning_effort=99、关闭态保留 default_params（真实 YAML 驱动，≥2 组输入）。"""
    unknown = rtm_with_yaml.switch_mode({"current_model": "ghost", "enable_thinking": True})
    assert unknown["switch_type"] == "none"
    assert unknown["params"] == {}

    enabled = rtm_with_yaml.switch_mode({"current_model": "reason-m1", "enable_thinking": True})
    assert enabled["switch_type"] == "parameter_switch"
    assert enabled["params"]["reasoning_effort"] == 99
    assert enabled["params"]["max_tokens"] == 4096

    disabled = rtm_with_yaml.switch_mode({"current_model": "reason-m1", "enable_thinking": False})
    assert "reasoning_effort" not in disabled["params"]


def test_recommendations_ranks_default_model_highest(rtm_with_yaml: Any) -> None:
    """recommendations：defaults.chat 命中的模型 suitability_score 最高并排首位。"""
    recs = rtm_with_yaml.recommendations()
    assert [r["model_name"] for r in recs] == ["reason-m1"]
    assert recs[0]["suitability_score"] > 0.7
    assert recs[0]["optimal_params"]["reasoning_effort"] == 99
