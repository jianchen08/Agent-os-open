#!/usr/bin/env python3
"""验证 pipeline 包导入链路完整性。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "shared"))

try:
    from pipeline.plugin import IInputPlugin, IOutputPlugin, ICorePlugin, PluginContext, PluginResult, OutputResult
    from pipeline.types import ErrorPolicy, StateKeys, RouteSignal, TargetType, create_initial_state
    from pipeline.plugin_types import PluginTypeSlot
    print("pipeline package imports: OK")
    print("  IInputPlugin:", IInputPlugin)
    print("  ErrorPolicy.ABORT:", ErrorPolicy.ABORT)
    print("  StateKeys.ITERATION:", StateKeys.ITERATION)
    print("  PluginTypeSlot:", PluginTypeSlot)
    print("  create_initial_state:", type(create_initial_state))
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)
