# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: python-plugins-test
"""Hindsight 记忆 sidecar 插件 TDD 测试。

验证内容（与任务规格 9 个用例对齐）：
1. plugin.json manifest 必备字段
2. 5 个工具全部注册到 plugin 对象
3. _client 为 None 时 retain/recall 优雅降级（不崩溃）
4. mock client.retain 后调用工具，断言调用参数与返回形状
5. recall 按 memory_type 客户端过滤
6. import_document 将长文本切分为 ~3 块并 retain 3 次
7. import_document 拒绝非 txt/md 文件
8. bank_id 缺省时回落到默认值（多租户隔离 key）

测试不依赖真实 hindsight 包——通过 monkeypatch 模块级 _client 实现。

[来源: docs/tasks Step 2 Hindsight memory sidecar]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 源码加入 sys.path（与 plugins/test_system_plugins.py 同款 setup）
_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，不依赖 importlib.reload
    （file-location spec 不可被 reload 重新查找，会抛 ModuleNotFoundError）。
    """
    mod_name = "hindsight_memory_server_test"
    plugin_path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    """调用插件工具并 await 协程结果（新建事件循环，避免 pytest-asyncio 冲突）。"""
    td = module.plugin._tools[tool_name]
    result = td.handler(**kwargs)
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()
    return result


@pytest.fixture
def mod() -> Any:
    """加载 server 模块，每个测试独立（重置 _client）。"""
    module = _load_module()
    module._client = None
    return module


@pytest.fixture
def mock_client(mod: Any) -> MagicMock:
    """注入 mock hindsight client 到模块。"""
    client = MagicMock()
    mod._client = client
    return client


# ═══════════════════════════════════════════════════════════
# 1. plugin.json manifest 校验
# ═══════════════════════════════════════════════════════════


class TestPluginManifest:
    def test_plugin_manifest_valid(self) -> None:
        """plugin.json 必须包含 id/plugin_type/host_type/entry 等必备字段。"""
        manifest_path = _PLUGIN_DIR / "plugin.json"
        assert manifest_path.exists(), "plugin.json must exist"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        # 必备字段
        assert data["id"] == "hindsight_memory_service"
        assert data["plugin_type"] == "system"
        assert data["host_type"] == "sidecar"
        # invoke 入口（entry 字段，python server.py 形式）
        entry = data.get("entry") or data.get("invoke", {}).get("entry")
        assert entry and "server.py" in entry, "entry must reference server.py"

        # capabilities.tools 至少声明 5 个工具
        tool_names = [t["name"] for t in data.get("capabilities", {}).get("tools", [])]
        for name in (
            "hindsight.retain",
            "hindsight.recall",
            "hindsight.reflect",
            "hindsight.delete",
            "hindsight.import_document",
        ):
            assert name in tool_names, f"{name} missing in capabilities.tools"


# ═══════════════════════════════════════════════════════════
# 2. 工具注册
# ═══════════════════════════════════════════════════════════


class TestToolRegistration:
    def test_tools_registered(self, mod: Any) -> None:
        """5 个 hindsight 工具全部注册到 plugin._tools。"""
        for name in (
            "hindsight.retain",
            "hindsight.recall",
            "hindsight.reflect",
            "hindsight.delete",
            "hindsight.import_document",
        ):
            assert name in mod.plugin._tools, f"tool {name} not registered"


# ═══════════════════════════════════════════════════════════
# 3. 降级（_client is None）
# ═══════════════════════════════════════════════════════════


class TestDegradeWithoutClient:
    def test_retain_without_client_degrades(self, mod: Any) -> None:
        """_client 为 None 时 retain 返回降级字典而非崩溃。"""
        mod._client = None
        result = _call_tool(
            mod, "hindsight.retain", bank_id="b1", content="hello"
        )
        assert isinstance(result, dict)
        assert result.get("initialized") is False
        assert "error" in result

    def test_recall_without_client_degrades(self, mod: Any) -> None:
        """_client 为 None 时 recall 返回降级字典而非崩溃。"""
        mod._client = None
        result = _call_tool(
            mod, "hindsight.recall", bank_id="b1", query="hello"
        )
        assert isinstance(result, dict)
        assert result.get("initialized") is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════
# 4. retain + mock client
# ═══════════════════════════════════════════════════════════


class TestRetainWithMock:
    def test_retain_with_mock_client(self, mod: Any, mock_client: MagicMock) -> None:
        """mock client.retain 被正确调用，返回 {id, stored:true}。"""
        mock_client.retain.return_value = {"id": "mem_123", "content": "hello"}

        result = _call_tool(
            mod,
            "hindsight.retain",
            bank_id="bank_A",
            content="hello world",
            memory_type="semantic",
            metadata={"source": "test"},
        )

        # 调用断言
        mock_client.retain.assert_called_once()
        call_kwargs = mock_client.retain.call_args.kwargs
        assert call_kwargs["bank_id"] == "bank_A"
        assert call_kwargs["content"] == "hello world"

        # 返回形状
        assert result["stored"] is True
        assert result["id"] == "mem_123"


# ═══════════════════════════════════════════════════════════
# 5. recall + memory_type 过滤
# ═══════════════════════════════════════════════════════════


class TestRecallFilter:
    def test_recall_filters_by_memory_type(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """recall 返回混合类型，memory_type 过滤后只保留匹配项。"""
        mock_client.recall.return_value = [
            {"id": "1", "content": "a", "metadata": {"memory_type": "semantic"}},
            {"id": "2", "content": "b", "metadata": {"memory_type": "episode"}},
            {"id": "3", "content": "c", "metadata": {"memory_type": "semantic"}},
        ]

        result = _call_tool(
            mod,
            "hindsight.recall",
            bank_id="bank_A",
            query="abc",
            memory_type="semantic",
        )

        mock_client.recall.assert_called_once()
        assert result["total"] == 2
        contents = [r["content"] for r in result["results"]]
        assert contents == ["a", "c"]


# ═══════════════════════════════════════════════════════════
# 6. import_document 切分
# ═══════════════════════════════════════════════════════════


class TestImportDocument:
    def test_import_document_chunks_text(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """5000 字符文本被切分为 ~3 块，retain 调用 3 次。"""
        long_text = "x" * 5000
        mock_client.retain.return_value = {"id": "mem_x"}

        result = _call_tool(
            mod,
            "hindsight.import_document",
            bank_id="bank_A",
            text=long_text,
            knowledge_name="kb1",
        )

        # 2000 字符/块 → 5000/2000 ≈ 3 块
        assert result["chunks_imported"] == 3
        assert result["knowledge_name"] == "kb1"
        assert mock_client.retain.call_count == 3

    def test_import_document_rejects_non_txt(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """file_path 为 .pdf 时返回错误字典，不读文件不 retain。"""
        result = _call_tool(
            mod,
            "hindsight.import_document",
            bank_id="bank_A",
            file_path="secret.pdf",
        )

        assert isinstance(result, dict)
        assert "error" in result
        # 关键：未触发 retain
        mock_client.retain.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 7. bank_id 缺省
# ═══════════════════════════════════════════════════════════


class TestBankIdDefault:
    def test_bank_id_defaults_to_tenant(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """bank_id 未给定时回落到默认值（多租户隔离 key）。"""
        mock_client.retain.return_value = {"id": "mem_d"}

        result = _call_tool(
            mod,
            "hindsight.retain",
            content="some memory",  # 注意：未传 bank_id
        )

        call_kwargs = mock_client.retain.call_args.kwargs
        # bank_id 必须被填上默认值（非空字符串）
        assert call_kwargs["bank_id"]
        assert call_kwargs["bank_id"] != ""
        assert result["stored"] is True
