# @feature: FP-T12 前端适配 | @ci: python-coverage
"""routes_llm_config.get_llm_presets 预置声明下发测试（解耦方案 P2-5）。

覆盖：
1. 载荷形状契约：provider_groups（label + providers [id, display_name] 二元组）、
   common_provider_types 非空、thinking_strength（levels + allowed_keys）——
   前端设置页唯一数据来源，形状漂移即红；
2. 预置 provider id 全局唯一（分组间不得重复，重复会让卡片渲染两行）；
3. 对账闸：声明文件 thinking_strength.allowed_keys 必须与 llm_core 的
   ``_THINKING_STRENGTH_ALLOWED`` 白名单一致（跨插件契约，插件声明与执行端
   过滤器漂移即红）；
4. 声明文件缺失 → ConfigAPIError（fail-closed，不回退前端硬编码时代）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
# routes_llm_config 依赖 plugins/shared 根的共享模块（atomic_io 等），一并入 path
_SHARED_DIR = _DIR.parents[1]
for _p in (str(_DIR), str(_SHARED_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LLM_CORE_PLUGIN = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core" / "plugin.py"


@pytest.fixture
def rlc() -> Any:
    """按显式路径加载 routes_llm_config（裸名防劫持）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_routes_llm_config_presets_test", str(_DIR / "routes_llm_config.py")
    )
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_routes_llm_config_presets_test"] = m
    spec.loader.exec_module(m)
    return m


def _llm_core_thinking_allowed_keys() -> set[str]:
    """从 llm_core 插件源码提取 _THINKING_STRENGTH_ALLOWED 字面量（对账真值）。"""
    assert _LLM_CORE_PLUGIN.exists(), f"llm_core plugin.py 不存在: {_LLM_CORE_PLUGIN}"
    source = _LLM_CORE_PLUGIN.read_text(encoding="utf-8")
    marker = "_THINKING_STRENGTH_ALLOWED ="
    line = next((ln for ln in source.splitlines() if ln.startswith(marker)), None)
    assert line is not None, "llm_core 未声明 _THINKING_STRENGTH_ALLOWED"
    parsed = ast.literal_eval(line[len(marker):].strip())
    return {str(k) for k in parsed}


def test_presets_payload_shape(rlc: Any) -> None:
    """载荷形状契约：三段结构齐备且类型正确（前端消费面唯一来源）。"""
    payload = rlc.get_llm_presets()

    assert isinstance(payload["provider_groups"], list) and payload["provider_groups"]
    for group in payload["provider_groups"]:
        assert isinstance(group["label"], str) and group["label"]
        assert isinstance(group["providers"], list) and group["providers"]
        for pair in group["providers"]:
            assert isinstance(pair, list) and len(pair) == 2
            assert all(isinstance(x, str) and x for x in pair)

    types_ = payload["common_provider_types"]
    assert isinstance(types_, list) and len(types_) >= 3

    strength = payload["thinking_strength"]
    assert isinstance(strength["levels"], list) and set(strength["levels"]) == {
        "high",
        "medium",
        "low",
        "off",
    }
    assert isinstance(strength["allowed_keys"], list) and strength["allowed_keys"]


def test_preset_provider_ids_unique_across_groups(rlc: Any) -> None:
    """预置 provider id 跨分组唯一（重复 id 会让前端卡片渲染两行）。"""
    payload = rlc.get_llm_presets()
    ids = [
        pair[0]
        for group in payload["provider_groups"]
        for pair in group["providers"]
    ]
    assert len(ids) == len(set(ids))


def test_allowed_keys_matches_llm_core_whitelist(rlc: Any) -> None:
    """对账闸：声明 allowed_keys 与 llm_core 执行端过滤白名单一致（防漂移）。"""
    payload = rlc.get_llm_presets()
    declared = set(payload["thinking_strength"]["allowed_keys"])
    assert declared == _llm_core_thinking_allowed_keys()


def test_levels_covers_chat_input_gears(rlc: Any) -> None:
    """对账闸：声明 levels 必须覆盖聊天页全部档位（含 off）。

    设置页按本清单重写模型的 thinking_strength_params——清单缺某档位时，
    该档映射会在保存时被静默删除（off 缺失曾使"关闭"档配置无法持久）。
    """
    payload = rlc.get_llm_presets()
    declared = set(payload["thinking_strength"]["levels"])
    chat_gears = _chat_input_thinking_gears()
    missing = chat_gears - declared
    assert not missing, (
        f"声明 levels 缺聊天页档位 {sorted(missing)}——设置页保存会静默删除这些档的映射"
    )


def _chat_input_thinking_gears() -> set[str]:
    """聊天输入框思考强度档位（llm_core 插件 widget 声明的 select options）。"""
    manifest = json.loads(
        (
            _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core" / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    for widget in manifest["ui_schema"]["widgets"]:
        if widget.get("id") != "thinking_strength":
            continue
        for field in widget["props"]["fields"]:
            if field.get("name") == "strength":
                return {str(opt["value"]) for opt in field["options"]}
    raise AssertionError("llm_core manifest 未声明 thinking_strength widget 档位")


def test_missing_declaration_fails_closed(rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """声明文件缺失 → ConfigAPIError 500（不静默回退空清单）。"""
    rlc._PRESETS_FILE = tmp_path / "not-exists.yaml"
    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc.get_llm_presets()
    assert exc_info.value.status_code == 500
