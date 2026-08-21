# @feature: FP-0.2.二 内部模块manifest | @ci: python-coverage
"""task_evaluate server.py 进程内导入冒烟（mypy 收紧批配套）。

意图（WHY）：
- 2026-08-21 治理批次为 server.py 补 `from typing import Any`（原 Name not defined）
  后该行进入 diff-coverage 度量面；server.py 为 sidecar-only 文件，此前无任何
  进程内测试 → 覆盖面缺失（diff 门禁 fail-loud 的压力点即指此）。
- 本测试以 importlib 进程内装载 server.py：锁 import 期回归（sys.path 注入、
  AgentOSPlugin 实例化、@plugin.tool 注册）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "tools" / "task_evaluate"


def _load_server():
    mod_name = "task_evaluate_server_import_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load task_evaluate/server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_server_module_loads_and_registers() -> None:
    server = _load_server()
    assert server.plugin is not None, "AgentOSPlugin 单例应在 import 期构造"
    assert server.plugin.name == "task_evaluate_tool"


def test_server_declares_tool_schema() -> None:
    server = _load_server()
    # @plugin.tool 装饰在 import 期注册工具声明；tools 注册表非空即契约在位。
    tools = getattr(server.plugin, "tools", None)
    if tools is None:
        pytest.skip("SDK 版本无 plugin.tools 属性（注册表形态差异，非回归）")
    assert tools, "task_evaluate 工具声明应非空"
