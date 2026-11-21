# @feature: FP-T07 llm api | @ci: python-coverage
"""接口方言注册表（LLM 两类收敛批 3；主方案 §6.4 / §6.2）。

方言键 = llm.yaml ``providers.<name>.type``（批 1 已对齐的键位空间：
``_plugin_provider_routes`` 路由键同源升格）。本模块是适配服务面的路由表：
配置侧的 type 词 → 方言实现（wire 执行 + payload 构建 + 非流式发送）。

首发方言 ``openai-compat``：wire 机器 = ``llm_wire``（复制改造自
``plugins/shared/llm/wire_client.py`` @ c0fe6922fc，锚点见该文件头）。
配置现状里 openai/deepseek/minimax/zai 四类 type 都走同一条 OpenAI
chat.completions wire（消息适配差异在 ``llm_provider_adapters``），注册表
如实承载该事实。**未登记的 type fail-closed**（不回退默认方言）——与批 1
「方言键缺失即构建期 ValueError」同一纪律；anthropic 原生协议当前无 wire
实现、也无配置条目在用，保持未注册可辨形态。

扩展点（加新方言两步）：
1. ``register_dialect(Dialect(...))`` 注册实现（type_keys 声明配置词表）；
2. 配置条目 ``providers.<name>.type`` 指向该词即接入。
新方言须评审契约形状（主方案 §6.4-b 扩展纪律）。

已注册方言：
- ``openai-compat``（首发，type 词 openai/deepseek/minimax/zai）；
- ``dsh``（批 4；实现与四证据契约见 ``llm_dsh`` 文件头，type 词 dsh）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import llm_dsh
import llm_wire

#: 首发方言实现键（OpenAI 兼容 wire）。
OPENAI_COMPAT = "openai-compat"

BuildPayload = Callable[..., dict[str, Any]]
StreamCall = Callable[..., Awaitable[Any]]
PostCall = Callable[..., Awaitable[Any]]


class DialectNotFoundError(ValueError):
    """方言键未注册（配置缺 type 或实现未落位）——fail-closed，不猜默认。"""


@dataclass(frozen=True)
class Dialect:
    """一个接口方言的实现装配（配置词表 + 三个 wire 动作）。

    Attributes:
        key: 方言实现键（注册表语法键，如 ``openai-compat``）。
        type_keys: llm.yaml ``providers.<name>.type`` 上映射到本实现的词表。
        build_payload: ``(model_name, messages, *, tools, stream, extra) -> body``。
        stream: ``async (base_url, api_key, body, *, timeout, model_label) -> 流``。
        post: ``async (base_url, api_key, body, *, timeout) -> 归一响应``。
    """

    key: str
    type_keys: tuple[str, ...]
    build_payload: BuildPayload
    stream: StreamCall
    post: PostCall


_DIALECTS: dict[str, Dialect] = {}
_TYPE_INDEX: dict[str, str] = {}


def register_dialect(dialect: Dialect, *, replace: bool = False) -> None:
    """注册方言实现（扩展点）。重复键/type 词冲突默认拒绝，``replace`` 显式换装。

    Raises:
        ValueError: 方言键为空、键已注册未声明 replace、或 type 词已被其他
            方言占用（同一 type 词不指向两个实现——路由歧义 fail-closed）。
    """
    if not dialect.key:
        raise ValueError("方言实现键不能为空")
    if dialect.key in _DIALECTS and not replace:
        raise ValueError(f"方言键 '{dialect.key}' 已注册（如需换装请显式 replace=True）")
    if replace and dialect.key in _DIALECTS:
        # 旧实现的 type 词先释放（避免残留指向被替换的实现）
        for type_key in [k for k, owner in _TYPE_INDEX.items() if owner == dialect.key]:
            del _TYPE_INDEX[type_key]
    for type_key in dialect.type_keys:
        owner = _TYPE_INDEX.get(type_key)
        if owner is not None and owner != dialect.key and not replace:
            raise ValueError(
                f"方言 type 词 '{type_key}' 已被方言 '{owner}' 占用（同名冲突 fail-closed）"
            )
    _DIALECTS[dialect.key] = dialect
    for type_key in dialect.type_keys:
        _TYPE_INDEX[type_key] = dialect.key


def unregister_dialect(key: str) -> None:
    """注销方言实现并释放其 type 词（注册表可扩展且可回退）。"""
    dialect = _DIALECTS.pop(key, None)
    if dialect is None:
        return
    for type_key in [k for k, owner in _TYPE_INDEX.items() if owner == key]:
        del _TYPE_INDEX[type_key]


def get_dialect(key: str) -> Dialect:
    """解析方言实现：先按实现键、再按配置 type 词；未命中 fail-closed。"""
    if not key:
        raise DialectNotFoundError("方言键为空（providers.<name>.type 未配置且未显式指定）")
    dialect = _DIALECTS.get(key)
    if dialect is not None:
        return dialect
    impl_key = _TYPE_INDEX.get(key)
    if impl_key is not None:
        return _DIALECTS[impl_key]
    raise DialectNotFoundError(
        f"未注册的接口方言键 '{key}'（providers.<name>.type 既非实现键也非已登记 type 词；"
        "不回退默认方言——按「先加方言再接入」纪律补注册）"
    )


def resolve_dialect_key(type_key: str) -> str:
    """配置 type 词 → 方言实现键（供诊断/前端展示）。"""
    return get_dialect(type_key).key


def dialect_keys() -> tuple[str, ...]:
    """已注册方言实现键（确定性排序）。"""
    return tuple(sorted(_DIALECTS))


def type_keys() -> tuple[str, ...]:
    """已登记的配置 type 词表（确定性排序）。"""
    return tuple(sorted(_TYPE_INDEX))


register_dialect(
    Dialect(
        key=OPENAI_COMPAT,
        type_keys=("openai", "deepseek", "minimax", "zai"),
        build_payload=llm_wire.build_body,
        stream=llm_wire.stream_chat,
        post=llm_wire.post_chat,
    )
)

#: dsh 方言（批 4）：只经注册点扩展，openai-compat 条目零变化。
register_dialect(
    Dialect(
        key=llm_dsh.DSH,
        type_keys=llm_dsh.TYPE_KEYS,
        build_payload=llm_dsh.build_payload,
        stream=llm_dsh.stream,
        post=llm_dsh.post,
    )
)
