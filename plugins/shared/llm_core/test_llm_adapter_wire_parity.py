# @feature: FP-T07 llm api | @ci: python-coverage
"""行为回归基线：同模型同输入，wire 复制改造前后请求 payload 字节级一致。

判据（主方案 §7.1 批 3 行 / ADR 2026-11-18-llm-two-tier-plugin-convergence）：
llm_core 适配服务面（``llm_wire``/``llm_dialects``/``llm_adapter``，复制改造件）
与 llm_service 源实现（``plugins/shared/llm/adapter.py`` +
``plugins/shared/llm/wire_client.py`` @ c0fe6922fc）在同一模型、同一输入
（messages / tools / 采样参数 / 超时）下产出的 HTTP 请求（URL + 鉴权 +
body 字节）必须完全一致——复制改造零行为差的机器证明。

方法：两侧各自走真实代码路径到 wire 发送点（唯一桩 = httpx 发送函数，
外部依赖替身，不触网）；捕获的 body 经 httpx 真实序列化器编码为字节后
逐字节比较。覆盖两条有区分度的消息适配路径（deepseek 采样保留 /
minimax 非首位 system 角色修复）与无工具纯正文路径。
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"
# llm_core 目录必须压过 llm 目录（两处 adapter.py 同名）；源实现按路径显式装载。
for _d in (_LLM_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_LLM_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

import llm_adapter  # noqa: E402
import llm_dialects  # noqa: E402
import router_factory  # noqa: E402  源路径路由表（bare，来自 llm 目录）
import wire_client  # noqa: E402  源 wire（bare，来自 llm 目录）


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"cannot load {path}"
    assert spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


src_adapter = _load("llm_src_adapter_parity", _LLM_DIR / "adapter.py")

_LLM_DATA: dict[str, Any] = {
    "providers": {
        "deepseek": {
            "type": "deepseek",
            "api_base": "https://api.deepseek.test/v1",
            "keys": [{"id": "ds_main", "api_key": "sk-deepseek", "api_base": "https://api.deepseek.test/v1"}],
        },
        "minimax": {
            "type": "minimax",
            "api_base": "https://api.minimax.test/v1",
            "api_key": "sk-minimax",
        },
        "zhipu_coding": {
            "type": "zai",
            "api_base": "https://open.bigmodel.test/api/coding/paas/v4/",
            "api_key": "sk-zai",
        },
    },
    "models": {
        "deepseek-v4-pro": {"provider": "deepseek", "model_name": "deepseek-v4-pro"},
        "MiniMax-M3": {"provider": "minimax", "model_name": "MiniMax-M3"},
        "gw-glm-5.3-flash": {"provider": "zhipu_coding", "model_name": "glm-5.3-flash"},
    },
    "defaults": {},
}

with_tools: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run shell",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }
]


class _FakeLoader:
    def load_llm_data(self) -> dict[str, Any]:
        return _LLM_DATA


def _text_chunks() -> list[Any]:
    return [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="你", reasoning_content=None, tool_calls=None), finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="好", reasoning_content=None, tool_calls=None), finish_reason="stop")],
            usage=None,
        ),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7, prompt_tokens_details=None)),
    ]


class _StubStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self._idx = 0
        self.closed = False

    def __aiter__(self) -> _StubStream:
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class _Capture:
    """wire 发送点替身：记录请求要素，返回脚本化流。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_chat(
        self, base_url: str, api_key: str, body: dict[str, Any], *, timeout: float, model_label: str = ""
    ) -> _StubStream:
        self.calls.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "body": copy.deepcopy(body),
                "timeout": timeout,
                "model_label": model_label,
            }
        )
        return _StubStream(_text_chunks())


@dataclass
class _Case:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    params: dict[str, Any]


_CASES = [
    pytest.param(
        _Case(
            model="deepseek-v4-pro",
            messages=[
                {"role": "system", "content": "系统提示"},
                {
                    "role": "assistant",
                    "content": "查一下",
                    "reasoning_content": "先想",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"},
                {"role": "user", "content": "总结🌊"},
            ],
            tools=with_tools,
            params={"temperature": 0.3, "max_tokens": 512, "reasoning_effort": "high"},
        ),
        id="deepseek-tools-thinking-cjk",
    ),
    pytest.param(
        _Case(
            model="MiniMax-M3",
            messages=[
                {"role": "system", "content": "s1"},
                {"role": "assistant", "content": "答", "reasoning_content": "想"},
                {"role": "system", "content": "运行时注入", "name": "inject"},
                {"role": "user", "content": "继续"},
            ],
            tools=None,
            params={"temperature": 0.7},
        ),
        id="minimax-role-fix",
    ),
    pytest.param(
        _Case(
            model="gw-glm-5.3-flash",
            messages=[
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好，介绍一下你自己 😀"},
            ],
            tools=None,
            params={},
        ),
        id="zai-plain-cjk",
    ),
]


def _request_bytes(call: dict[str, Any]) -> bytes:
    """按源 wire 发送形态（_auth_headers + json body）求真实 httpx 编码字节。"""
    headers = {"Authorization": f"Bearer {call['api_key']}", "Content-Type": "application/json"}
    request = httpx.Request("POST", "http://wire.parity.test/chat/completions", json=call["body"], headers=headers)
    return request.content


def _run_source(case: _Case, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Any]:
    capture = _Capture()
    monkeypatch.setattr(wire_client, "stream_chat", capture.stream_chat)
    adapter = src_adapter.KeyPoolAdapter(None)
    response = asyncio.run(
        adapter.completion(
            model=case.model,
            messages=copy.deepcopy(case.messages),
            tools=copy.deepcopy(case.tools),
            stream=True,
            first_chunk_timeout=30,
            inter_chunk_timeout=30,
            **case.params,
        )
    )
    assert len(capture.calls) == 1, "源路径应恰好发送一次（异常/重试会暴露）"
    return capture.calls[0], response


def _run_new(case: _Case, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    capture = _Capture()
    original = llm_dialects.get_dialect(llm_dialects.OPENAI_COMPAT)
    patched = llm_dialects.Dialect(
        key=original.key,
        type_keys=original.type_keys,
        build_payload=original.build_payload,
        stream=capture.stream_chat,
        post=original.post,
    )
    llm_dialects.register_dialect(patched, replace=True)
    try:
        face = llm_adapter.LLMAdapterFace(config_provider=lambda: {"llm": _LLM_DATA})
        result = asyncio.run(
            face.execute(
                model=case.model,
                messages=copy.deepcopy(case.messages),
                tools=copy.deepcopy(case.tools),
                stream=True,
                params={**case.params, "first_chunk_timeout": 30, "inter_chunk_timeout": 30},
            )
        )
    finally:
        llm_dialects.register_dialect(original, replace=True)
    assert len(capture.calls) == 1, "适配服务面应恰好发送一次"
    return capture.calls[0], result


@pytest.fixture(autouse=True)
def _router(monkeypatch: pytest.MonkeyPatch) -> None:
    """源路径路由表按同一份 llm 数据重建（与适配服务面同源配置）。

    两处会话级卫生（缺一即基线假红，实测 plugins/shared/llm_core 先于
    plugins/shared/llm 收集时复发）：
    - ``router_factory`` 在**夹具期**从 ``sys.modules`` 现解析——同会话他测
      会 pop 后重导裸名模块，本文件模块级绑定会指向陈旧实例（表建在旧对象上，
      源实现懒导入读到的是新实例的空表 → provider 名解析为空）；
    - 配置注入视图钉死同一份 ``_LLM_DATA``——源实现（``llm/adapter.py``）的
      model→api_base 解析读进程级 ``_config_models`` 视图，不钉会被他测
      ``set_config`` 泄漏污染。
    """
    import _config_models
    import router_factory as current_router_factory  # 运行期解析，防陈旧绑定

    monkeypatch.setattr(_config_models, "_config", {"llm": _LLM_DATA})
    current_router_factory.build_router(_FakeLoader())


@pytest.mark.parametrize("case", _CASES)
def test_request_payload_byte_identical_before_and_after(case: _Case, monkeypatch: pytest.MonkeyPatch) -> None:
    """改造前后：URL / 鉴权 / 超时 / body 字节逐项一致（零行为差机器证明）。"""
    src_call, src_response = _run_source(case, monkeypatch)
    new_call, new_result = _run_new(case, monkeypatch)

    assert src_call["base_url"] == new_call["base_url"]
    assert src_call["api_key"] == new_call["api_key"]
    assert src_call["timeout"] == new_call["timeout"]
    assert _request_bytes(src_call) == _request_bytes(new_call)
    assert len(_request_bytes(new_call)) > 0
    # 响应侧行为同源：同一输入同一 chunk 脚本下聚合结果一致
    assert src_response.text == new_result["text"]
    assert new_result["status"] == "streamed"
    assert new_result["dialect"] == "openai-compat"
