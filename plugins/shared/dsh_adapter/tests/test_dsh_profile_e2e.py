# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""真机 lane：官方 plugin-manager 在真实 profile 工作区挂载并 list。

默认 skip；设置 ``AGENTOS_DSH_E2E=1`` 且默认用户空间工作区已 bootstrap
（含 manager 栈 + pnpm）时运行。不做安装/卸载（避免污染用户数据），只验证
桥能挂 management context 并拿到 ``listBundles``。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import bridge as bridge_mod  # noqa: E402
import runtime_workspace as rw  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.skipif(os.environ.get("AGENTOS_DSH_E2E") != "1",
                    reason="real DSH profile lane (set AGENTOS_DSH_E2E=1)")
def test_manager_mounts_and_lists_real_profile() -> None:
    root = str(rw.resolve_runtime_root())
    manager = rw.read_package_json(Path(root) / "node_modules" / rw.MANAGER_PACKAGE)
    if not manager or str(manager.get("version") or "") != rw.MANAGER_VERSION:
        pytest.skip("default runtime workspace not bootstrapped with official manager")
    pnpm, pnpm_args = rw.resolve_pnpm_invocation(root)
    if pnpm is None:
        pytest.skip("pnpm runtime not bootstrapped")

    async def scenario() -> None:
        b = bridge_mod.DshRuntimeBridge(
            runtime_root=root,
            boot_plan={"source": "profile", "chain": False,
                       "fallback": {"chain": False, "packages": []}},
            manager_config={"pnpmCommand": pnpm, "pnpmArgs": pnpm_args,
                            "registry": rw.NPM_REGISTRY_DEFAULT,
                            "fallbackRegistries": []},
            boot_timeout_s=120,
        )
        try:
            await b.initialize()
            assert b.manager_ready is True, b.manager_error
            listed = await b.manager_list()
            assert listed.get("ok") is True, listed.get("error")
            assert isinstance((listed.get("data") or {}).get("bundles"), list)
        finally:
            await b.shutdown()

    asyncio.run(scenario())
