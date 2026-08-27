# @feature: scan批B 安全三修复 | @vision: V2 可观测 | @ci: python-coverage
"""godot_mcp 外部 MCP 插件 manifest 契约测试（scan批B #3）。

锁定两件事：
1. 机器本地路径不得入库——manifest 全文不允许出现盘符绝对路径；
   二进制与 demo 工程一律经 endpoint.env 的 ${VAR} 占位注入
   （内核 resolve_env_placeholders 解析：进程 env → .env overlay，
   缺失时 spawn 早失败并指名变量）。
2. 接线形态：command 为 PATH 可解析的 "python" 引导（外部 stdio 分支
   不设 working_dir，不能依赖插件目录相对路径），bootstrap 显式消费
   GODOT_MCP_BIN / GODOT_PROJECT_DIR。

[来源: docs/working/规则驱动全仓扫描报告_20260827.md tools Should Fix #11]
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
def manifest_text() -> str:
    # 解析后重新序列化：契约针对数据本身，不锁排版/转写细节。
    return json.dumps(json.loads(_MANIFEST.read_text(encoding="utf-8")), ensure_ascii=False)


@pytest.fixture(scope="module")
def endpoint(manifest_text: str) -> dict:
    mcp = json.loads(manifest_text).get("mcp") or {}
    assert mcp.get("transport") == "stdio"
    return mcp.get("endpoint") or {}


def test_no_machine_absolute_paths(manifest_text: str) -> None:
    """反模式红线回归：全文不得含盘符绝对路径（URL 的 https:// 不受影响）。"""
    drives = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s\"']*", manifest_text)
    assert not drives, f"发现机器本地绝对路径入库: {drives}"


def test_command_is_path_resolvable_python(endpoint: dict) -> None:
    """外部 stdio 分支无 working_dir → command 必须是 PATH 可解析的 python。"""
    assert endpoint["command"] == "python"
    args = endpoint["args"]
    assert args[:1] == ["-c"], "引导脚本应内联于 -c（无工作目录锚点可用）"


def test_endpoint_env_placeholders_declared(endpoint: dict) -> None:
    """两个机器本地值必须经 ${VAR} 声明，由内核解析与缺失早失败。"""
    env = endpoint.get("env") or {}
    assert env.get("GODOT_MCP_BIN") == "${GODOT_MCP_BIN}"
    assert env.get("GODOT_PROJECT_DIR") == "${GODOT_PROJECT_DIR}"


def test_bootstrap_consumes_injected_env(endpoint: dict) -> None:
    """引导脚本显式消费两个注入变量并透传 serve 参数给二进制。"""
    script = next(a for a in endpoint["args"] if a != "-c")
    assert "GODOT_MCP_BIN" in script
    assert "GODOT_PROJECT_DIR" in script
    assert "serve" in script
    assert "--typed=false" in script
    assert "--project" in script


@pytest.mark.parametrize("missing", ["GODOT_MCP_BIN", "GODOT_PROJECT_DIR"])
def test_bootstrap_requires_both_envs(monkeypatch, missing) -> None:
    """攻击面输入对照：任一注入缺失，引导立即失败（不静默降级半配置启动）。"""
    endpoint = json.loads(_MANIFEST.read_text(encoding="utf-8"))["mcp"]["endpoint"]
    script = next(a for a in endpoint["args"] if a != "-c")
    monkeypatch.setenv("GODOT_MCP_BIN", "/fake/godot-mcp")
    monkeypatch.setenv("GODOT_PROJECT_DIR", "/fake/project")
    monkeypatch.delenv(missing, raising=False)

    import subprocess
    import sys as _sys

    proc = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0, f"缺失 {missing} 时必须失败"
    assert missing in (proc.stderr + proc.stdout)
