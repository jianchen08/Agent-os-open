# @feature: FP-T12 前端适配 | @ci: python-coverage
"""routes_llm_config.add_model 冲突分派行为测试。

覆盖（输入条件驱动，直写临时 llm.yaml 后断言落盘结果）：
1. 新 ID 直接写入，added_ids 如实回报；
2. 同 ID 不同提供商（同名模型多提供商场景）→ 自动派生
   ``<id>-<provider>`` 新增，既有条目原样保留（防覆盖性质）；
3. 派生 ID 也被占用 → 序号递增 ``-2``；
4. 同 provider 同 model_name 的真重复 → 409；
5. 非 dict models 体 → 400。
"""
from __future__ import annotations

import importlib.util
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
        "llm_routes_llm_config_add_test", str(_DIR / "routes_llm_config.py")
    )
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_routes_llm_config_add_test"] = m
    spec.loader.exec_module(m)
    # 隔离副作用：缓存失效与 ConfigCenter reload 均指向测试外的全局态
    m.invalidate_all_llm_caches = None
    m.get_config_center = None
    return m


@pytest.fixture
def llm_yaml(rlc: Any, tmp_path: Path) -> Path:
    """临时 llm.yaml：预置一个 deepseek 提供商下的同名模型。"""
    path = tmp_path / "llm.yaml"
    path.write_text(
        yaml.dump(
            {
                "models": {
                    "deepseek-v4-flash": {
                        "provider": "deepseek",
                        "model_name": "deepseek-v4-flash",
                        "context_window": 1000000,
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    rlc._LLM_YAML = path
    return path


def _on_disk(llm_yaml: Path) -> dict[str, Any]:
    return yaml.safe_load(llm_yaml.read_text(encoding="utf-8"))


def test_new_model_written_with_added_ids(rlc: Any, llm_yaml: Path) -> None:
    body = {"models": {"glm-5.2": {"provider": "zhipu_coding", "model_name": "glm-5.2"}}}
    result = rlc.add_model(body)

    assert result["added_ids"] == ["glm-5.2"]
    disk = _on_disk(llm_yaml)["models"]
    assert disk["glm-5.2"]["provider"] == "zhipu_coding"
    assert disk["deepseek-v4-flash"]["provider"] == "deepseek"


def test_same_name_other_provider_auto_derived(rlc: Any, llm_yaml: Path) -> None:
    """同名模型挂到另一提供商 → 派生新 ID 新增，原条目逐字保留（防覆盖）。"""
    original = {"provider": "deepseek", "model_name": "deepseek-v4-flash", "context_window": 1000000}
    body = {
        "models": {
            "deepseek-v4-flash": {"provider": "siliconflow", "model_name": "deepseek-v4-flash"}
        }
    }
    result = rlc.add_model(body)

    assert result["added_ids"] == ["deepseek-v4-flash-siliconflow"]
    disk = _on_disk(llm_yaml)["models"]
    assert disk["deepseek-v4-flash"] == original
    assert disk["deepseek-v4-flash-siliconflow"]["provider"] == "siliconflow"


def test_derived_id_collision_appends_seq(rlc: Any, llm_yaml: Path) -> None:
    disk_models = _on_disk(llm_yaml)["models"]
    disk_models["deepseek-v4-flash-siliconflow"] = {"provider": "siliconflow", "model_name": "old"}
    llm_yaml.write_text(yaml.dump({"models": disk_models}), encoding="utf-8")

    body = {
        "models": {
            "deepseek-v4-flash": {"provider": "siliconflow", "model_name": "deepseek-v4-flash"}
        }
    }
    result = rlc.add_model(body)

    assert result["added_ids"] == ["deepseek-v4-flash-siliconflow-2"]
    assert _on_disk(llm_yaml)["models"]["deepseek-v4-flash-siliconflow"]["model_name"] == "old"


def test_true_duplicate_same_provider_conflicts(rlc: Any, llm_yaml: Path) -> None:
    body = {
        "models": {
            "deepseek-v4-flash": {"provider": "deepseek", "model_name": "deepseek-v4-flash"}
        }
    }
    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc.add_model(body)

    assert exc_info.value.status_code == 409
    # 真重复不落盘
    assert set(_on_disk(llm_yaml)["models"]) == {"deepseek-v4-flash"}


def test_non_dict_models_body_rejected(rlc: Any, llm_yaml: Path) -> None:
    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc.add_model({"models": ["glm-5.2"]})

    assert exc_info.value.status_code == 400
