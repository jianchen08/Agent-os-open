# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
# @feature: scan批B 安全三修复 | @vision: V2 可观测 | @ci: python-coverage
"""godot_mcp 插件 manifest 契约测试。

0919e823e 起 external MCP 改造为 Python 工具代理：manifest 无 mcp.endpoint
（stdio 会话由 tool.py 的 _ServeProxy 按需驱动），机器本地值不再走
endpoint.env ${VAR} 占位注入，机器级配置只剩 GODOT_MCP_BIN 环境变量名
（tool.py 运行时读取；缺失/非法时调用即失败并指名变量，该行为由插件自带
test_godot_run.py::TestGodotRunExecute 锁定，此处不重复）。旧契约中的
GODOT_PROJECT_DIR 注入已随 workspace 路由裁定（2026-09-03）从契约移除。

本文件锁定 manifest 自身契约：
1. 机器本地路径不得入库——manifest 全文不允许出现盘符绝对路径；
2. Python 工具代理接线形态——tool 型 manifest，sidecar + 相对 entry
   （spawn 以插件目录为锚，不依赖机器本地路径）；
3. 工具契约——capabilities.tools 声明 godot_run 且带 input/output schema；
4. 机器本地二进制只以环境变量名（GODOT_MCP_BIN）出现在 manifest，
   无 ${VAR} 占位残留。

[来源: docs/working/规则驱动全仓扫描报告_20260827.md tools Should Fix #11；
 契约形态随 0919e823e（Python 工具代理）更新]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MANIFEST = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "tools" / "external_mcp" / "godot_mcp" / "plugin.json"
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest_text(manifest: dict) -> str:
    # 重新序列化：契约针对数据本身，不锁排版/转写细节。
    return json.dumps(manifest, ensure_ascii=False)


def test_no_machine_absolute_paths(manifest_text: str) -> None:
    """反模式红线回归：全文不得含盘符绝对路径（URL 的 https:// 不受影响）。"""
    drives = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s\"']*", manifest_text)
    assert not drives, f"发现机器本地绝对路径入库: {drives}"


def test_declares_python_tool_proxy(manifest: dict) -> None:
    """接线形态：Python 工具代理（sidecar + 相对 entry）。

    entry 相对插件目录锚定，不依赖机器本地路径——承接旧契约"spawn 引导
    不得依赖插件目录之外的本机锚点"。
    """
    assert manifest["plugin_type"] == "tool"
    assert manifest["language"] == "python"
    assert manifest["host_type"] == "sidecar"
    parts = manifest["entry"].split()
    assert parts[0] == "python", "entry 必须以 python 解释器引导"
    assert parts[-1] == "server.py", "entry 必须指向插件内相对入口脚本"


def test_tool_declaration_contract(manifest: dict) -> None:
    """工具契约：声明 godot_run，input/output schema 齐备，method 必填。"""
    tools = manifest["capabilities"]["tools"]
    names = [t.get("name") for t in tools]
    assert "godot_run" in names, f"必须声明 godot_run 工具: {names}"
    tool = next(t for t in tools if t["name"] == "godot_run")
    assert isinstance(tool.get("input_schema"), dict)
    assert isinstance(tool.get("output_schema"), dict)
    assert "method" in tool["input_schema"].get("required", [])


def test_machine_local_bin_by_env_name_only(manifest_text: str) -> None:
    """机器本地二进制位置只以环境变量名出现（tool.py 运行时读取），
    不经 manifest 注入、不留 ${VAR} 占位残留。"""
    assert "GODOT_MCP_BIN" in manifest_text, "部署环境变量名必须在 manifest 可见"
    assert "${" not in manifest_text, "endpoint.env ${VAR} 注入已随 Python 代理形态移除"
