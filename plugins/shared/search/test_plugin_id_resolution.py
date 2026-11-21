# @feature: FP-0.2.二 动态工具面插件 id 解析 | @vision: V2 全能闭环 | @ci: python-coverage
"""动态工具面的**路由目标解析**测试：目录名 ≠ manifest id 的插件必须能经 call_tool 调用。

锁的可观察契约（缺陷取证 = ``docs/working/c2fb3f357120_批次B验证报告.md`` §3）：

- **插件 id 的唯一来源 = 内核 manifests 键面（``manifest.id``）**。活注册表
  ``GET /api/v1/tools`` 条目的 ``plugin_id`` 就是该键（``ToolDescriptor.plugin_id``
  注册自 ``manifest.id``，``plugin-loader/src/loader.rs``）；``resource_search``
  产出的 ``tool_defs[*].plugin_id`` 必须是它，**不得是插件目录名**——目录名与
  manifest id 无契约关系，按目录名查插件必然内核 ``plugin not found``。
- **样本取自仓内真实插件**（``plugins/shared/builtin_tools`` 的 id 是
  ``agentos-builtin-tools``、``plugins/shared/memory`` 是 ``memory_tool``、
  ``plugins/shared/bash`` 是 ``bash_tool``、``plugins/shared/hello_pack`` 是
  ``hello_pack``）：沙箱里放的是**这些真实 plugin.json 的原字节**，目录名沿用真身，
  不手写样例、不改写 id。沙箱只为避开全仓扫描的磁盘开销，取值仍以真身文件为准
  （``test_real_manifest_bytes_match_expectation`` 直读真身复核）。
- **不回归**：目录名 == manifest id 的既有成功场景（``hello_pack``）解析结果不变。
- **回落真值同源**：活条目缺 ``plugin_id`` 时回落本条 manifest 的 ``id``，仍不猜目录名。

外部依赖（内核 ``GET /api/v1/tools`` 端点）按门禁口径打桩：桩的**取值来自真实
manifest 文件**，与内核注册面同构（``plugin_id = manifest.id``），不手写期望值。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
_SERVER_MOD = "search_plugin_id_test_server"

# 真身样本：仓内 ``plugins/shared/<目录>``，真值 = 该目录 plugin.json 的 ``id``
_BUILTIN_TOOLS = ("builtin_tools", "agentos-builtin-tools", "file_read")
_MEMORY = ("memory", "memory_tool", "memory")
_BASH = ("bash", "bash_tool", "bash")
_SAME_DIR = ("hello_pack", "hello_pack", "hello_pack")  # 目录名 == id（活体通过场景；snippet_box 插件已删，替身取同形真身 hello_pack）
_MISMATCH_SAMPLES = [_BUILTIN_TOOLS, _MEMORY, _BASH]
_ALL_SAMPLES = [*_MISMATCH_SAMPLES, _SAME_DIR]


def _load_server() -> Any:
    """装载本插件 server.py（继承其 sys.path / loader 接线）。"""
    if _SERVER_MOD in sys.modules:
        del sys.modules[_SERVER_MOD]
    spec = importlib.util.spec_from_file_location(_SERVER_MOD, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SERVER_MOD] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _real_manifest(dir_name: str) -> dict[str, Any]:
    """直读仓内真身 ``plugins/shared/<dir_name>/plugin.json``。"""
    path = _SHARED_DIR / dir_name / "plugin.json"
    assert path.is_file(), f"真身 manifest 不在位: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _live_items(manifests: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """按内核注册面同构生成活条目：``plugin_id`` = ``manifest.id``（非目录名）。"""
    items: list[dict[str, Any]] = []
    for _dir_name, manifest in manifests:
        plugin_id = str(manifest.get("id") or "")
        for tool_decl in (manifest.get("capabilities") or {}).get("tools") or []:
            items.append(
                {
                    "name": tool_decl.get("name"),
                    "description": tool_decl.get("description", ""),
                    "plugin_id": plugin_id,
                    "category": "search",
                    "source": "builtin",
                }
            )
    return items


def _install_surface(items: list[dict[str, Any]]) -> list[int]:
    """在注入缝装活注册表桩（外部依赖 = 内核 HTTP 端点），返回取数次数记录。"""
    calls: list[int] = []

    async def stub() -> dict[str, Any]:
        calls.append(1)
        return {"items": items, "total": len(items)}

    sys.modules["resource_search_tool_impl"].set_tool_surface_caller(stub)
    return calls


def _defs(rs: Any, query: str) -> list[dict[str, Any]]:
    """跑一次 resource_search（detailed），取 tool_defs。"""
    result = _run(rs.execute({"resource_type": "tool", "query": query, "mode": "detailed"}))
    assert result.error is None, f"resource_search 意外失败: {result.error}"
    return list(result.output.get("tool_defs") or [])


def _def_of(defs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in defs:
        if item.get("name") == name:
            return item
    raise AssertionError(f"tool_defs 缺目标工具 {name}: {[d.get('name') for d in defs]}")


@pytest.fixture(scope="module")
def server_mod() -> Any:
    return _load_server()


@pytest.fixture
def sandbox(server_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """沙箱插件面：真身 plugin.json **原字节**复制到 ``tmp/plugins/shared/<同名目录>``。

    目录名与文件字节都取自真身（关系是「目录名 ≠ manifest id」这一真实事实），
    live 活注册表桩按内核注册面同构生成。
    """
    manifests: list[tuple[str, dict[str, Any]]] = []
    for dir_name, _expected_id, _tool in _ALL_SAMPLES:
        source = _SHARED_DIR / dir_name / "plugin.json"
        target_dir = tmp_path / "plugins" / "shared" / dir_name
        target_dir.mkdir(parents=True)
        shutil.copyfile(source, target_dir / "plugin.json")
        manifests.append((dir_name, _real_manifest(dir_name)))

    tool = server_mod._load_resource_search_tool()()  # 先装载：impl 模块入 sys.modules
    impl = sys.modules["resource_search_tool_impl"]
    monkeypatch.setattr(impl, "_PROJECT_ROOT", tmp_path)
    _install_surface(_live_items(manifests))
    yield tool, manifests
    impl.set_tool_surface_caller(None)


class TestRealManifestSamples:
    """样本真值复核：结论锚在真身文件上，不靠测试内字面值。"""

    @pytest.mark.parametrize(("dir_name", "expected_id", "tool_name"), _ALL_SAMPLES)
    def test_real_manifest_bytes_match_expectation(
        self, dir_name: str, expected_id: str, tool_name: str
    ) -> None:
        manifest = _real_manifest(dir_name)
        assert str(manifest.get("id")) == expected_id
        declared = [t.get("name") for t in (manifest.get("capabilities") or {}).get("tools") or []]
        assert tool_name in declared
        assert manifest.get("plugin_type") == "tool"

    def test_repo_has_both_kinds(self) -> None:
        """仓内两类插件并存（目录名 == id 与 ≠ id），本缺陷面真实存在。"""
        assert _BUILTIN_TOOLS[0] != _BUILTIN_TOOLS[1]
        assert _SAME_DIR[0] == _SAME_DIR[1]


class TestManifestIdResolution:
    """``tool_defs[*].plugin_id`` 取自内核 manifests 键面，绝不取目录名。"""

    @pytest.mark.parametrize(("dir_name", "expected_id", "tool_name"), _MISMATCH_SAMPLES)
    def test_mismatch_plugin_resolves_to_manifest_id(
        self, sandbox: Any, dir_name: str, expected_id: str, tool_name: str
    ) -> None:
        """目录名 ≠ id 的真实插件：定义里 plugin_id = manifest id，且不等于目录名。"""
        rs, _manifests = sandbox
        definition = _def_of(_defs(rs, tool_name), tool_name)
        assert definition["plugin_id"] == expected_id
        assert definition["plugin_id"] != dir_name

    def test_same_dir_plugin_not_regressed(self, sandbox: Any) -> None:
        """目录名 == id 的既有成功场景（hello_pack）解析结果不变。"""
        rs, _manifests = sandbox
        definition = _def_of(_defs(rs, _SAME_DIR[2]), _SAME_DIR[2])
        assert definition["plugin_id"] == _SAME_DIR[1]

    def test_every_definition_plugin_id_is_kernel_resolvable(self, sandbox: Any) -> None:
        """性质断言：产出的每个 plugin_id 都在真实 manifest id 集内（否则内核必 miss）。"""
        rs, manifests = sandbox
        ids = {str(m.get("id") or "") for _d, m in manifests}
        dirs = {d for d, _m in manifests}
        defs = _defs(rs, "*")
        assert len(defs) >= len(_ALL_SAMPLES), f"混合面样本不足: {[d.get('name') for d in defs]}"
        for definition in defs:
            plugin_id = definition["plugin_id"]
            assert plugin_id in ids, (
                f"{definition['name']} 的 plugin_id={plugin_id} 不在内核可解析 id 集"
            )
            assert plugin_id != "", f"{definition['name']} 的 plugin_id 为空（无路由目标）"
            assert not (plugin_id in dirs and plugin_id not in ids), (
                f"{definition['name']} 的 plugin_id={plugin_id} 是目录名，内核查插件必 miss"
            )


class TestFallbackWithoutLivePluginId:
    """活条目缺 ``plugin_id`` 字段 → 回落本条 manifest 的 ``id``，仍不猜目录名。"""

    def test_falls_back_to_manifest_id(self, server_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tool = server_mod._load_resource_search_tool()()
        impl = sys.modules["resource_search_tool_impl"]
        plugin_dir = tmp_path / "plugins" / "shared" / _BUILTIN_TOOLS[0]
        plugin_dir.mkdir(parents=True)
        shutil.copyfile(_SHARED_DIR / _BUILTIN_TOOLS[0] / "plugin.json", plugin_dir / "plugin.json")
        monkeypatch.setattr(impl, "_PROJECT_ROOT", tmp_path)
        # 活条目缺 plugin_id（老内核/坏件形态）——不是正常形态，只为覆盖回落分支
        _install_surface([{"name": _BUILTIN_TOOLS[2], "description": "", "category": "search", "source": "builtin"}])
        try:
            definition = _def_of(_defs(tool, _BUILTIN_TOOLS[2]), _BUILTIN_TOOLS[2])
        finally:
            impl.set_tool_surface_caller(None)
        assert definition["plugin_id"] == _BUILTIN_TOOLS[1]
        assert definition["plugin_id"] != _BUILTIN_TOOLS[0]


class TestCallToolForwardingWithRealPlugins:
    """端到端：真实插件的定义经 ``call_tool`` 转发时携带的 plugin_id 是内核可解析 id。"""

    @staticmethod
    def _forward(server_mod: Any, defs: list[dict[str, Any]], tool_name: str, arguments: dict[str, Any]) -> Any:
        """用真实定义驱动 call_tool，记录转发到执行面的 (tool, args, plugin_id)。"""
        call_tool_cls = server_mod._load_call_tool()
        ct_mod = sys.modules["call_tool_impl"]
        calls: list[tuple[str, dict[str, Any], str]] = []

        async def recorder(tool: str, args: dict[str, Any], plugin_id: str) -> Any:
            calls.append((tool, args, plugin_id))
            return {"success": True, "output": {"ok": True}}

        ct_mod.set_tool_invoker(recorder)
        ct_mod.set_state_reader(None)
        try:
            result = _run(call_tool_cls().execute({"tool": tool_name, "arguments": arguments, "_dynamic_tools": defs}))
        finally:
            ct_mod.set_tool_invoker(None)
        assert result.error is None, f"call_tool 意外失败: {result.error}"
        assert calls, "未发生转发——用例前提失效"
        return calls[0]

    def test_mismatch_plugin_forwarded_with_manifest_id(self, sandbox: Any, server_mod: Any) -> None:
        """``file_read``（目录 ``builtin_tools``）转发时 plugin_id = ``agentos-builtin-tools``。"""
        rs, _manifests = sandbox
        defs = _defs(rs, _BUILTIN_TOOLS[2])
        tool, _args, plugin_id = self._forward(server_mod, defs, _BUILTIN_TOOLS[2], {"path": "README.md"})
        assert tool == _BUILTIN_TOOLS[2]
        assert plugin_id == _BUILTIN_TOOLS[1]
        assert plugin_id != _BUILTIN_TOOLS[0], "按目录名路由 = 内核 plugin not found"

    def test_same_dir_plugin_forwarding_not_regressed(self, sandbox: Any, server_mod: Any) -> None:
        """``hello_pack``（目录名 == id）转发 plugin_id 不变。"""
        rs, _manifests = sandbox
        defs = _defs(rs, _SAME_DIR[2])
        _tool, _args, plugin_id = self._forward(server_mod, defs, _SAME_DIR[2], {"action": "list"})
        assert plugin_id == _SAME_DIR[1]
