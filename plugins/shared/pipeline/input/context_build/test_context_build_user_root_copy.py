# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""context_build user_root 扁平副本回归（BUG-56 / K10 配置断链）。

user_root 扁平迁移后 sidecar 副本（<USER_ROOT>/plugins/<名>/，祖先无 0.2
共享根指纹）曾因共享根解析落空在引导期崩溃（No module named 'mode_keys'）
→ state 无 tool_ids → 工具面置空 → 聊天空回复。本文件对**副本本体**断言：

- 副本 server.py 按真实 sidecar 形态启动（bootstrap_plugin 经 SDK 位置锚
  解析回仓库共享根），模块级导入不再断裂；
- 副本 plugin.py 在副本 sys.path 形态下注入 tool_ids（agent yaml 自持配置）。

副本为运行时数据（gitignored）——缺席环境跳过，不造假现场。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DIR.parents[4]
_SHARED = _REPO_ROOT / "plugins" / "shared"
_USER_ROOT = Path(
    __import__("os").environ.get("AGENTOS_USER_ROOT", str(_REPO_ROOT / "user_root"))
)
_COPY_DIR = _USER_ROOT / "plugins" / "context_build"

_copy_missing = pytest.mark.skipif(
    not (_COPY_DIR / "plugin.py").is_file(),
    reason="user_root 扁平副本不在位（无用户空间环境）",
)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@_copy_missing
def test_copy_server_boot_resolves_repo_shared_root() -> None:
    """副本 server.py 按 sidecar 形态启动：引导解析仓库共享根，模块级导入不崩。

    runpy 执行模块级代码（bootstrap + 全链导入）；main 守卫不触发（无服务
    循环挂起）。共享根断裂时在 ``from mode_keys import``（plugin.py）即崩。
    """
    server = _COPY_DIR / "server.py"
    driver = (
        "import runpy, sys\n"
        f"runpy.run_path(r'{server}')\n"
        "ok = any(str(p).replace(chr(92), '/').rstrip('/').endswith('plugins/shared')\n"
        "        for p in sys.path)\n"
        "assert ok, sys.path\n"
        "print('BOOTSTRAP_OK')\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"副本引导断裂：\n{proc.stderr[-2000:]}"
    assert "BOOTSTRAP_OK" in proc.stdout


@_copy_missing
def test_copy_injects_tool_ids_from_agent_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """副本 plugin.py 注入 tool_ids（K10 回归）：agent yaml 自持配置随执行态写出。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    factory = tmp_path / "factory"
    agents = factory / "agents"
    agents.mkdir(parents=True)
    (agents / "l2coder.yaml").write_text(
        "display_name: 测试执行者\nlevel: L2\ntool_ids: [file_read, file_write]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))

    # 副本 sidecar 引导形态：bootstrap_plugin 先于 plugin.py 导入（与 server.py
    # 同序），终态 [shared_root, plugin_dir]——副本本体的导入全部经此解析。
    from agentos_plugin_sdk.bootstrap import bootstrap_plugin

    sys_path_snapshot = list(sys.path)
    try:
        bootstrap_plugin(_COPY_DIR / "server.py")
        mod_name = "context_build_user_root_copy_test"
        spec = importlib.util.spec_from_file_location(mod_name, str(_COPY_DIR / "plugin.py"))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

        from pipeline.plugin import PluginContext

        plugin = mod.ContextBuildPlugin(config={})
        res = _run(plugin.execute(PluginContext(state={"agent.id": "l2coder"}, config={})))
    finally:
        sys.path[:] = sys_path_snapshot

    assert res.state_updates["tool_ids"] == ["file_read", "file_write"]
