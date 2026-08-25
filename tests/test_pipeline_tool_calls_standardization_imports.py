"""P0-1 契约测试：tool_calls 标准化 / JSON 修复链路必须真正执行。

契约：这些代码路径不得因 import 失败而静默跳过——
``context_window_guard._standardize_tool_calls`` 的 ImportError 若被空泛
``except Exception`` 吞掉，标准化会永不执行。

本测试断言行为（WHY）：标准化/修复必须真正生效，而非 import 语句本身。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_PIPELINE_DIR = _SHARED_DIR / "pipeline"

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
    # 插件源目录也加入 sys.path，使平铺 from pipeline.types / from _message_normalizer 解析
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


# ---------------------------------------------------------------------------
# Site 1: context_window_guard._standardize_tool_calls（标准化必须真实执行）
# ---------------------------------------------------------------------------
def test_context_window_guard_standardize_tool_calls_actually_runs() -> None:
    """压缩写回前 tool_calls 必须被标准化为 OpenAI 结构。

    WHY：``_standardize_tool_calls`` 的 import 失败不得被外层 ``except
    Exception`` 吞成 warning 后静默跳过——畸形 tool_calls 须被真正归一化，
    否则原样写回会破坏后续 provider 调用。
    """
    mod = _load_plugin_module("input", "context_window_guard", "cwg_plugin_p0_test")
    plugin = mod.ContextWindowGuardPlugin()

    # 畸形 tool_calls：扁平 {name, args} 而非 OpenAI 的 {type:function, function:{...}}
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "search", "args": '{"q": "hi"}'}],
        },
    ]

    plugin._standardize_tool_calls(messages)

    tc = messages[0]["tool_calls"][0]
    # 归一契约：扁平 {name, args} → OpenAI 结构 {type, function:{...}}
    assert tc["type"] == "function"
    assert isinstance(tc["function"], dict)
    assert tc["function"]["name"] == "search"
    assert tc["function"]["arguments"] == '{"q": "hi"}'
    # id 被补齐为标准 call_ 前缀格式
    assert isinstance(tc.get("id"), str) and tc["id"].startswith("call_")


# ---------------------------------------------------------------------------
# Site 2: tool_schema_validator._detect_truncation（截断检测必须可用）
# ---------------------------------------------------------------------------
def test_tool_schema_validator_detect_truncation_runs() -> None:
    """截断检测必须能调用 repair_json_string 并返回丢失字段。

    WHY：``_check_args_truncation`` 依赖 repair_json_string；该依赖不可用时
    整个截断检测不可用。契约：截断必须被修复并报告丢失的顶层字段。
    """
    mod = _load_plugin_module("input", "tool_schema_validator", "tsv_plugin_p0_test")
    validator = mod.ToolSchemaValidator()

    # 结构性截断：steps 数组未闭合 → repair 会丢弃 steps
    truncated_args = '{"goal": "do x", "steps": ["a",'
    result = validator._check_args_truncation(truncated_args, "my_tool")

    assert result is not None, "截断的 arguments 应被识别"
    assert result["truncated"] is True
    assert "steps" in result["lost_keys"]


# ---------------------------------------------------------------------------
# Site 3: param_inject._do_work 兜底修复路径（兜底修复必须保住可用字段）
# ---------------------------------------------------------------------------
def test_param_inject_repair_path_runs() -> None:
    """参数注入对畸形 arguments 的兜底修复必须真正执行。

    WHY：json.loads 失败的兜底分支必须能调用 repair_json_string——可修复的
    半截 arguments 不得直接变成 {} 使下游拿不到内容。
    """
    from pipeline.plugin import PluginContext  # noqa: PLC0415

    mod = _load_plugin_module("input", "param_inject", "pi_plugin_p0_test")
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

    import asyncio  # noqa: PLC0415

    updates = asyncio.new_event_loop().run_until_complete(plugin._do_work(ctx))

    injected = updates.get("raw_tool_calls")
    assert injected, "应回写注入后的 tool_calls"
    # 修复成功后 arguments 被解析成 dict（goal 字段保住），不再被空泛置空
    fn_args = injected[0].get("args") or injected[0].get("arguments")
    assert isinstance(fn_args, dict)
    assert fn_args.get("goal") == "x"


# ---------------------------------------------------------------------------
# Site 4: core/llm_core error-recovery 路径的 reset_pairing_cache 可解析
# ---------------------------------------------------------------------------
def test_core_llm_core_reset_pairing_cache_import_resolves() -> None:
    """core/llm_core 错误恢复路径用的 reset_pairing_cache 必须可解析。

    WHY：``core/llm_core/plugin.py`` 在 LLM 调用失败的 error-recovery 分支用
    ``from plugins.core.llm_core._message_normalizer import reset_pairing_cache``
    （断裂），须为同目录平铺 import（与该文件 line 27 既有写法一致）。
    本测试用平铺形态断言符号可达——与生产代码使用的 import 形态一致。
    """
    core_dir = str(_PIPELINE_DIR / "core" / "llm_core")
    if core_dir not in sys.path:
        sys.path.insert(0, core_dir)
    # 与 core/llm_core/plugin.py 一致的平铺 import
    from _message_normalizer import reset_pairing_cache  # noqa: PLC0415

    assert callable(reset_pairing_cache)
