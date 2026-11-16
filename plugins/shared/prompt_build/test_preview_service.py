# @feature: FP-0.2.提示词预览 | @ci: python-coverage
"""prompt.preview 服务测试——context 片段 → 组装结果（与管道步同代码路径）。

服务是离线组装面（管理面预览端点的取数单源），断言四点契约：
context 片段全量进 system_message、动态变量独立成消息、session_id 可注入、
空/非 dict 输入零注出。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent


def _load_server() -> Any:
    """动态加载 server.py（每次新建，隔离模块级状态；同 agent_manager 测试范式）。

    全车道共跑防御（fd8998101 同款）：先逐出裸名缓存（96 插件同名 plugin.py
    经 sys.path 抢先入 sys.modules 时，server.py 内 `from plugin import ...`
    会拿到他插件模块），再把本插件目录与共享根钉在 sys.path 最前，保证
    server.py 的裸名导入（plugin/_libs/_root_api）解析到本插件真身。
    """
    import sys as _sys

    for stale in [k for k in _sys.modules if k == "plugin" or k.startswith("_libs") or k == "_root_api"]:
        del _sys.modules[stale]
    for p in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
        while p in _sys.path:
            _sys.path.remove(p)
        _sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "prompt_build_server_preview_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prompt_build_server_preview_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPromptPreview:
    def test_context_pieces_assembled_into_system_message(self, server: Any) -> None:
        """system_prompt/static_vars/constraints_text 三片段全量进 system_message。"""
        result = _run(
            server.prompt_preview(
                context={
                    "system_prompt": "你是代码审查助手",
                    "static_vars": [
                        {"type": "content", "name": "档案", "content": "审查口径 XYZ"}
                    ],
                    "constraints_text": "- [必须] 只审不改",
                }
            )
        )
        msg = result["system_message"]
        assert "你是代码审查助手" in msg
        assert "审查口径 XYZ" in msg
        assert "## 约束" in msg
        assert "- [必须] 只审不改" in msg

    def test_dynamic_vars_become_separate_message(self, server: Any) -> None:
        """动态变量不进 system_message，独立成 prompt.dynamic_vars 消息。"""
        result = _run(
            server.prompt_preview(
                context={
                    "system_prompt": "P",
                    "dynamic_vars": [{"type": "session", "name": "会话"}],
                }
            )
        )
        assert result["system_message"] == "P"
        dyn = result["dynamic_vars_message"]
        assert isinstance(dyn, dict)
        assert "- 会话: preview" in dyn["content"]

    def test_session_id_param_renders(self, server: Any) -> None:
        """session_id 参数注入 {{session}} 渲染。"""
        result = _run(
            server.prompt_preview(
                context={
                    "system_prompt": "P",
                    "dynamic_vars": [{"type": "session", "name": "会话"}],
                },
                session_id="s-9",
            )
        )
        assert "- 会话: s-9" in result["dynamic_vars_message"]["content"]

    def test_empty_context_yields_empty_assembly(self, server: Any) -> None:
        """空 context：system_message 空串、无动态变量消息（零注出）。"""
        result = _run(server.prompt_preview(context={}))
        assert result["system_message"] == ""
        assert result["dynamic_vars_message"] is None

    def test_non_dict_context_treated_as_empty(self, server: Any) -> None:
        """非 dict context 按空处理，不抛异常（边界防御）。"""
        result = _run(server.prompt_preview(context=None))
        assert result["system_message"] == ""
        assert result["dynamic_vars_message"] is None
