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
# routes_llm_config 依赖 plugins/shared 根的共享模块（atomic_io 等），一并入 path
_SHARED_DIR = _DIR.parents[1]
for _p in (str(_DIR), str(_SHARED_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


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


# ─────────── 拉取模型上限事实（litellm 注册表，厂商 /models 不返回） ───────────

def test_lookup_model_limits_returns_registry_facts(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """litellm 注册表有该模型 → 返回真实 context_window 与 max_output_tokens。

    厂商 /models 端点只返回 id/object/owned_by，上限事实只能来自注册表；
    这些数字随后写进模型的 context_window / max_tokens（不再发明 4096）。

    注册表条目显式桩定：同进程先前装载 llm server.py 时会置
    LITELLM_LOCAL_MODEL_COST_MAP=true，litellm 由此改用本地备份地图——
    新模型（如 zai/glm-5.2）是否收录随 litellm 版本/地图形态漂移，断言
    真实数据会时序性翻车；本用例锁的是查表接线语义，不是 litellm 数据。
    """
    import litellm

    monkeypatch.setitem(litellm.model_cost, "zai/glm-5.2", {
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
    })
    limits = rlc._lookup_model_limits("zai", "glm-5.2")

    assert limits["context_window"] == 1_000_000
    assert limits["max_output_tokens"] == 128_000


def test_lookup_model_limits_unknown_model_returns_empty(rlc: Any) -> None:
    """注册表无该模型 → 空 dict（调用方走保守默认，不发明数字）。"""
    assert rlc._lookup_model_limits("openai", "no-such-model-xyz") == {}
    assert rlc._lookup_model_limits("", "glm-5.2") == {}
    assert rlc._lookup_model_limits("zai", "") == {}


def test_lookup_model_limits_no_cross_vendor_suffix_match(rlc: Any) -> None:
    """不做后缀模糊匹配：同名模型在不同厂商上限不同，套用他人上限即失真。

    ollama 的 glm-5.2 在注册表里没有 openai/ 前缀条目——不得退化成命中
    dashscope/glm-5.2 或 zai/glm-5.2（那是别的厂商的事实）。
    """
    assert rlc._lookup_model_limits("openai", "glm-5.2") == {}


def test_lookup_model_limits_registry_failure_degrades_to_empty(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册表读取异常 → 空 dict 且不抛出（拉取列表不因查上限失败而整体失败）。"""
    import builtins

    real_import = builtins.__import__

    def _boom(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "litellm":
            raise RuntimeError("registry unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)

    assert rlc._lookup_model_limits("zai", "glm-5.2") == {}


def test_get_remote_models_attaches_registry_limits(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点接线闸：拉取列表逐条附上限事实（删掉接线即红）。

    厂商 /models 只返回 id/object/owned_by；端点须把 litellm 注册表的
    context_window / max_output_tokens 一并下发，否则前端添加模型时只能
    发明 4096（本函数存在的唯一理由）。

    注册表条目显式桩定（同 test_lookup_model_limits_returns_registry_facts）：
    进程内地图形态随 llm server 装载与否漂移，接线闸只锁端点→查表→下发链路。
    """
    import httpx
    import litellm

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {"id": "glm-5.2", "object": "model", "owned_by": "z-ai"},
                    {"id": "unknown-model-xyz", "object": "model", "owned_by": "z-ai"},
                ]
            }

    monkeypatch.setattr(rlc, "_read_yaml", lambda _p: {
        "providers": {
            "zhipu_coding": {
                "type": "zai",
                "api_base": "https://example.invalid/v4",
                "keys": [{"api_key": "sk-test"}],
            }
        }
    })
    monkeypatch.setattr(rlc, "_resolve_env_value", lambda v: v)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setitem(litellm.model_cost, "zai/glm-5.2", {
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
    })

    payload = rlc.get_remote_models("zhipu_coding")
    by_id = {m["id"]: m for m in payload["models"]}

    # 注册表有该模型 → 事实随条目下发
    assert by_id["glm-5.2"]["context_window"] == 1_000_000
    assert by_id["glm-5.2"]["max_output_tokens"] == 128_000
    # 注册表无该模型 → 无事实键（前端走保守兜底，不发明数字）
    assert "context_window" not in by_id["unknown-model-xyz"]
    assert "max_output_tokens" not in by_id["unknown-model-xyz"]
