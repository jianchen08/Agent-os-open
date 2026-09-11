"""P0-1 契约测试：tool_calls 标准化 / JSON 修复链路必须真正执行。

契约：这些代码路径不得因依赖缺失而静默跳过——

- tool_schema_validator 截断检测 / param_inject 兜底修复依赖 llm_service 的
  ``llm.repair_json`` 能力（repair_json_string 单一真值源在
  llm_service._message_normalizer，2026-09-06 T7 起跨插件共享经能力面收敛）。
  本测试用桩替身模拟 capability 传输层（外部进程边界），修复逻辑走真实实现，
  断言「修复必须真正生效并应用」而非 import 语句本身。
- context_window_guard 的压缩写回标准化已上移 llm_service 标准化唯一关卡，
  不再有调用方本地实现（行为覆盖见
  plugins/shared/system/llm/test_complete_stream_normalize.py）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_PIPELINE_DIR = _SHARED_DIR / "pipeline"
_LLM_SERVICE_DIR = _SHARED_DIR / "system" / "llm"

# 与各 server.py 自身的 sys.path 注入对齐：plugins/shared/ 让 ``pipeline`` 包可解析，
# 各插件源目录让平铺 ``from plugin import X`` 可解析。
for _p in (str(_SHARED_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_plugin_module(category: str, name: str, mod_name: str) -> Any:
    """按文件路径加载 pipeline 插件 plugin.py，返回全新模块实例。

    用唯一 mod_name 避免多个同名 ``plugin.py`` 互相污染 sys.modules 缓存。

    Args:
        category: ``input`` / ``output`` / ``core``。
        name: 插件目录名。
        mod_name: 注册进 sys.modules 的唯一模块名。

    Returns:
        已 exec 的模块对象。
    """
    module_path = _PIPELINE_DIR / category / name / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    # 插件源目录也加入 sys.path，使平铺 from pipeline.types 可解析
    src_dir = str(module_path.parent)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_service_normalizer() -> Any:
    """加载 llm_service 的 _message_normalizer（唯一模块名，供桩能力调用真实实现）。"""
    mod_name = "llm_service_normalizer_p0_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _LLM_SERVICE_DIR / "_message_normalizer.py"
    assert module_path.exists(), f"module missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _RepairCapabilityStub:
    """llm.repair_json 能力桩：模拟 tool-executor 信封传输，修复走真实实现。

    只替身跨进程传输（外部依赖）；repair_json_string 用 llm_service 真实模块，
    保证契约断言针对真实修复逻辑而非桩的臆造行为。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG002
        self.calls.append(params)
        assert params.get("tool_name") == "llm.repair_json", params
        assert params.get("plugin_id") == "llm_service", params
        normalizer = _load_service_normalizer()
        text = (params.get("args") or {}).get("text", "")
        return {
            "success": True,
            "data": {"repaired": normalizer.repair_json_string(text)},
            "error": None,
        }


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Site 1: tool_schema_validator._check_args_truncation（截断检测必须可用）
# ---------------------------------------------------------------------------
def test_tool_schema_validator_detect_truncation_runs(monkeypatch: Any) -> None:
    """截断检测必须能经 llm.repair_json 能力修复并返回丢失字段。

    WHY：``_check_args_truncation`` 依赖该能力；能力桩不可用时整个截断检测
    降级为"不可修复"。契约：截断必须被修复并报告丢失的顶层字段。
    """
    stub = _RepairCapabilityStub()
    mod = _load_plugin_module("input", "tool_schema_validator", "tsv_plugin_p0_test")
    monkeypatch.setattr(mod, "_capability_caller", stub)
    validator = mod.ToolSchemaValidator()

    # 结构性截断：steps 数组未闭合 → repair 会丢弃 steps
    truncated_args = '{"goal": "do x", "steps": ["a",'
    result = _run(validator._check_args_truncation(truncated_args, "my_tool"))

    assert stub.calls, "截断检测未调用 llm.repair_json 能力"
    assert result is not None, "截断的 arguments 应被识别"
    assert result["truncated"] is True
    assert "steps" in result["lost_keys"]


# ---------------------------------------------------------------------------
# Site 2: param_inject._do_work 兜底修复路径（兜底修复必须保住可用字段）
# ---------------------------------------------------------------------------
def test_param_inject_repair_path_runs(monkeypatch: Any) -> None:
    """参数注入对畸形 arguments 的兜底修复必须真正执行。

    WHY：json.loads 失败的兜底分支必须能经 llm.repair_json 修复——可修复的
    半截 arguments 不得直接变成 {} 使下游拿不到内容。
    """
    from pipeline.plugin import PluginContext  # noqa: PLC0415

    stub = _RepairCapabilityStub()
    mod = _load_plugin_module("input", "param_inject", "pi_plugin_p0_test")
    monkeypatch.setattr(mod, "_capability_caller", stub)
    plugin = mod.ParamInjectPlugin()

    # tool_execute 路径 + 一个 arguments 为畸形 JSON 字符串的 tool_call
    ctx = PluginContext(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [
                {"id": "call_1", "name": "do_stuff", "args": '{"goal": "x"'}
            ],
        },
    )

    updates = _run(plugin._do_work(ctx))

    assert stub.calls, "兜底修复未调用 llm.repair_json 能力"
    injected = updates.get("raw_tool_calls")
    assert injected, "应回写注入后的 tool_calls"
    # 修复成功后 arguments 被解析成 dict（goal 字段保住），不再被空泛置空
    fn_args = injected[0].get("args") or injected[0].get("arguments")
    assert isinstance(fn_args, dict)
    assert fn_args.get("goal") == "x"
