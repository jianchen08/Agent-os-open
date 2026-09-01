# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""跨语言 capability 清单一致性测试（M1）。

校验 SDK 的 STANDARD_CAPABILITIES 与内核 initialize 声明的能力清单对齐，
避免「内核实现了但 sidecar 拿不到句柄」的漂移（见 tool-executor / service-registry 历史 bug）。

分层：unit（纯导入 + 注入模拟，无 sidecar 子进程）。
"""
import pytest

pytestmark = pytest.mark.unit

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.capability import STANDARD_CAPABILITIES as SDK_CAPS


def test_standard_capabilities_matches_expected_nine():
    """SDK 清单应包含全部 9 个标准能力，且无重复。

    2026-08-19（fbf0a1316）pipeline-state 补入两端清单：router 静态路由存在
    但不在声明面时，sidecar initialize 拿不到句柄，state 聚合读桥永远
    KeyError（e2e 实测）——故 SDK/内核清单均为 8 项。
    2026-09-02 tool-surface 补入两端清单：LLM 工具面过滤服务（agent 配置
    解耦，tool_schema 插件经此取过滤面，ADR 2026-09-02）——9 项。
    """
    expected = {
        "pipeline-executor",
        # pipeline-state.list：state 聚合读桥（task_submit/task_manage 依赖）
        "pipeline-state",
        "tenant-context",
        "event-bus",
        "metrics",
        "tool-executor",
        # tool-surface.schemas：LLM 工具面过滤服务（内核零 agent 配置知识）
        "tool-surface",
        "service-registry",
        # task_observability：插件 → 内核 → 前端一次性事件出口（ADR §3.5）
        "frontend",
    }
    assert set(SDK_CAPS) == expected, f"SDK 清单漂移: {set(SDK_CAPS) ^ expected}"
    assert len(SDK_CAPS) == len(set(SDK_CAPS)), "SDK 清单有重复项"


def test_sdk_injects_all_standard_caps_when_kernel_declares_them():
    """当内核声明全部 9 项能力时，SDK 应能为每一项创建 CapabilityHandle。

    模拟内核 initialize 的行为：build_declared_capabilities(true) 返回
    全部 8 项。SDK 的 _on_initialize 必须全部注入，否则该能力对插件不可用。
    """
    plugin = AgentOSPlugin("test_alignment")
    declared = {name: {} for name in SDK_CAPS}
    plugin._on_initialize({"capabilities": declared, "config": {}})

    missing = [name for name in SDK_CAPS if plugin.get_capability(name) is None]
    assert not missing, f"SDK 未为以下能力创建句柄: {missing}"


def test_get_capability_raises_keyerror_for_undeclared():
    """内核未声明的能力，SDK 必须显式抛 KeyError，而非静默返回 None。"""
    plugin = AgentOSPlugin("test_missing")
    plugin._on_initialize({"capabilities": {}, "config": {}})
    with pytest.raises(KeyError, match="not injected"):
        plugin.get_capability("pipeline-executor")
