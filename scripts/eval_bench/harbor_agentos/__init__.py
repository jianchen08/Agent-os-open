"""Harbor BaseAgent 适配器包（Terminal-Bench 参赛：AgentOS 整系统形态）。

harbor run 挂载（Ubuntu 侧，harbor ≥0.26）：
    PYTHONPATH=<repo root> harbor run ... \
        -a scripts.eval_bench.harbor_agentos.adapter:AgentOSHarborAgent

模块布局：
- adapter.py：AgentOSHarborAgent（BaseAgent 子类；硬依赖 harbor）
- kernel_bridge.py：内核桥（登录/建会话/绑定外部容器/派发/轮询终态）
- container_probe.py：任务容器解析（harbor-free，鸭子式 environment.exec）

导入形态：包内裸名平铺 import（仓内插件同款约定），本包目录在任何导入
上下文（harbor 包形态 / pytest 直指文件形态）先自推 sys.path。
adapter 硬依赖 harbor——本包 __init__ 对它惰性导出（harbor 缺席时
kernel_bridge/container_probe 面照常可用，harbor 侧测试 importorskip）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PKG_DIR = str(Path(__file__).resolve().parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from container_probe import ContainerProbeError, ContainerRef  # noqa: E402
from kernel_bridge import KernelBridge, KernelBridgeError, RunOutcome  # noqa: E402

__all__ = [
    "AgentOSAgentOptions",
    "AgentOSHarborAgent",
    "ContainerRef",
    "ContainerProbeError",
    "KernelBridge",
    "KernelBridgeError",
    "RunOutcome",
]

_HARBOR_GATED = ("AgentOSAgentOptions", "AgentOSHarborAgent")


def __getattr__(name: str) -> Any:
    """adapter 面（harbor 硬依赖）惰性导出：harbor 缺席时给出明确指引。"""
    if name in _HARBOR_GATED:
        try:
            from adapter import AgentOSAgentOptions, AgentOSHarborAgent
        except ImportError as exc:
            raise ImportError(
                "adapter 面需要 harbor 包（pip install harbor，开发机 0.26.0）"
                f"：{exc}"
            ) from exc
        return {"AgentOSAgentOptions": AgentOSAgentOptions, "AgentOSHarborAgent": AgentOSHarborAgent}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
