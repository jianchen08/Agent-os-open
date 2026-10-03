# @feature: FP-T12 前端适配 | @ci: python-coverage
"""routes_llm_config.get_thinking_levels 思考参数组选项下发测试。

聊天页思考选择器真值源（前端零硬编码、零映射）：选项 = thinking_strength_params
配置的**参数组本身**（无档位词汇映射层）。
1. 厂商级参数组在前、模型级补位，按参数内容去重，配置顺序即选项顺序；
2. 标签 = 实际字段值直显（_render_params：嵌套取标量叶子，多键按配置序
   " / " 连接；不得是 JSON 信封）；
3. value = 参数组紧凑 JSON 串（sort_keys）= 消息 thinking_strength 线上形态；
4. current = 模型 default_params 思考参数（reasoning_effort 优先、其次
   thinking.type）命中的选项 value，未匹配 None；
5. 模型未命中 / 两侧均未配置 → options=[]（前端选择器隐藏）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent


@pytest.fixture
def rlc() -> Any:
    """按显式路径加载 routes_llm_config（裸名防劫持）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_routes_llm_config_levels_test", str(_DIR / "routes_llm_config.py")
    )
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_routes_llm_config_levels_test"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def llm_yaml(rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """写临时 llm.yaml 并让 _llm_yaml_path 指向它。"""

    def _write(content: dict[str, Any]) -> None:
        path = tmp_path / "llm.yaml"
        # sort_keys=False：保留配置书写序——参数组键序即选项标签叶子序（契约面）
        path.write_text(
            yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        monkeypatch.setattr(rlc, "_llm_yaml_path", lambda: path)

    return _write


def test_provider_options_first_model_fill_dedup(rlc: Any, llm_yaml: Any) -> None:
    """厂商参数组在前、模型补位；相同参数组按内容去重。"""
    llm_yaml(
        {
            "providers": {
                "ds": {
                    "thinking_strength_params": {
                        "high": {"reasoning_effort": "max"},
                        "low": {"reasoning_effort": "low"},
                    }
                },
            },
            "models": {
                "m-1": {
                    "provider": "ds",
                    "model_name": "ds-max",
                    "display_name": "DS Max",
                    "thinking_strength_params": {
                        "dup": {"reasoning_effort": "max"},
                        "off": {"thinking": {"type": "disabled"}},
                    },
                },
            },
        }
    )
    result = rlc.get_thinking_levels("ds-max")
    # dup 与厂商 high 参数相同 → 去重；顺序 = 厂商配置在前、模型独有补位
    assert [o["params"] for o in result["options"]] == [
        {"reasoning_effort": "max"},
        {"reasoning_effort": "low"},
        {"thinking": {"type": "disabled"}},
    ]
    # fields = 表单字段声明（fieldsUri 数据源契约同构），options 即参数组
    assert len(result["fields"]) == 1
    assert result["fields"][0]["name"] == "strength"
    assert result["fields"][0]["type"] == "select"
    assert result["fields"][0]["options"] == result["options"]
    # 标签 = 实际字段值直显（嵌套取叶子），非 JSON 信封
    assert result["options"][0]["label"] == "max"
    assert result["options"][2]["label"] == "disabled"
    # value = 紧凑 JSON 串（sort_keys）
    assert result["options"][2]["value"] == json.dumps(
        {"thinking": {"type": "disabled"}}, sort_keys=True, separators=(",", ":")
    )


def test_label_renders_actual_field_values(rlc: Any, llm_yaml: Any) -> None:
    """标签 = 字段实际值直显（用户裁定 2026-10-03）：嵌套取叶子、多键按
    配置序 " / " 连接；标签面不出现 JSON 结构字符。"""
    llm_yaml(
        {
            "models": {
                "m-1": {
                    "provider": "p",
                    "model_name": "m",
                    "display_name": "M",
                    "thinking_strength_params": {
                        "high": {"thinking": {"type": "adaptive"}},
                        "low": {"thinking": {"type": "disabled"}},
                        "off": {
                            "thinking": {"type": "disabled"},
                            "reasoning_effort": "none",
                        },
                    },
                },
            },
        }
    )
    options = rlc.get_thinking_levels("m")["options"]
    assert [o["label"] for o in options] == ["adaptive", "disabled", "disabled / none"]
    # 性质断言：任何标签都是裸值形态，不携带 JSON 信封（thinking={"type":...}）
    assert all("{" not in o["label"] and "=" not in o["label"] for o in options)


def test_current_from_default_params_effort(rlc: Any, llm_yaml: Any) -> None:
    """default_params.reasoning_effort 与参数组精确相等 → current=该组 value。"""
    llm_yaml(
        {
            "models": {
                "m-1": {
                    "provider": "p",
                    "model_name": "m",
                    "display_name": "M",
                    "default_params": {"reasoning_effort": "max", "temperature": 0.7},
                    "thinking_strength_params": {
                        "high": {"reasoning_effort": "max"},
                        "low": {"reasoning_effort": "low"},
                    },
                },
            },
        }
    )
    high_value = json.dumps({"reasoning_effort": "max"}, sort_keys=True, separators=(",", ":"))
    assert rlc.get_thinking_levels("m")["current"] == high_value


def test_current_from_thinking_type(rlc: Any, llm_yaml: Any) -> None:
    """effort 缺失时 thinking.type 匹配（关闭形态参数组）。"""
    llm_yaml(
        {
            "models": {
                "m-1": {
                    "provider": "p",
                    "model_name": "m",
                    "display_name": "M",
                    "default_params": {"thinking": {"type": "disabled"}},
                    "thinking_strength_params": {
                        "high": {"reasoning_effort": "high"},
                        "off": {"thinking": {"type": "disabled"}},
                    },
                },
            },
        }
    )
    result = rlc.get_thinking_levels("m")
    assert result["current"] == result["options"][1]["value"]


def test_current_effort_priority_over_thinking(rlc: Any, llm_yaml: Any) -> None:
    """effort 与 thinking.type 同时存在且指向不同参数组 → effort 优先。"""
    llm_yaml(
        {
            "models": {
                "m-1": {
                    "provider": "p",
                    "model_name": "m",
                    "display_name": "M",
                    "default_params": {
                        "reasoning_effort": "max",
                        "thinking": {"type": "disabled"},
                    },
                    "thinking_strength_params": {
                        "high": {"reasoning_effort": "max"},
                        "off": {"thinking": {"type": "disabled"}},
                    },
                },
            },
        }
    )
    result = rlc.get_thinking_levels("m")
    assert result["current"] == result["options"][0]["value"]


def test_current_no_match_none(rlc: Any, llm_yaml: Any) -> None:
    """default_params 思考参数与任何参数组都不匹配 → current=None（不回落首项）。"""
    llm_yaml(
        {
            "models": {
                "m-1": {
                    "provider": "p",
                    "model_name": "m",
                    "display_name": "M",
                    "default_params": {"reasoning_effort": "medium"},
                    "thinking_strength_params": {
                        "high": {"reasoning_effort": "max"},
                        "low": {"reasoning_effort": "low"},
                    },
                },
            },
        }
    )
    assert rlc.get_thinking_levels("m")["current"] is None


def test_no_mapping_or_missing_model_empty(rlc: Any, llm_yaml: Any) -> None:
    """未命中模型 / 两侧均未配置 / model 为空 → options=[] current=None。"""
    llm_yaml(
        {
            "providers": {"ds": {}},
            "models": {
                "m-1": {"provider": "ds", "model_name": "ds-max", "display_name": "DS Max"},
            },
        }
    )
    empty: dict[str, Any] = {"model": "ds-max", "fields": [], "options": [], "current": None}
    assert rlc.get_thinking_levels("ds-max") == empty
    assert rlc.get_thinking_levels("no-such") == {
        "model": "no-such",
        "fields": [],
        "options": [],
        "current": None,
    }
    assert rlc.get_thinking_levels("") == {
        "model": "",
        "fields": [],
        "options": [],
        "current": None,
    }
