# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""kernel_token 公共模块契约测试 — 内核 0.2 开发期 token 自解析单点。

覆盖 ``plugins/shared/kernel_token.py`` decode_kernel_token 的四态行为契约
（自 e5df14240 四站点回归测试收编）：

- 旧式 4 段 token（``access:{user_id}:{username}:{exp}``）→ 三元组；
- 新式带点两段式（``base64(payload).base64(sig)``，payload 内 exp 后带 HMAC
  尾巴）→ 三元组（点号切签名单向污染是 e5df14240 修复的根因，锁死）；
- 过期 exp → 解码仍返回三元组（过期判定是调用方职责，模块只还原文档化值）；
- 垃圾输入（非 base64 / 缺段 / 非数字 exp / 空串）→ None。

安全前提：自解析不验签，鉴权权威在内核 dispatcher（见模块 docstring）——
本测试只锁解码行为，不构造任何"验签通过"语义。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

_SHARED_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import kernel_token  # noqa: E402, I001  (需先推 sys.path 再导入；isort 不识别平铺裸模块——per-file-ignores 先例)


def _b64(payload: str) -> str:
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _signed_token(payload: str, signature: bytes = b"fake-signature-bytes") -> str:
    """新式形态：base64(payload).base64(sig)（30d1b0959 后实测带点两段式）。"""
    return _b64(payload) + "." + base64.b64encode(signature).decode("ascii")


# ── 可解析形态：旧 4 段 / 新带点（两组有区分度输入，防拟合单一格式）───────


@pytest.mark.parametrize(
    "token",
    [
        _b64("access:user-1:admin:9999999999"),  # 旧式 4 段
        _signed_token("access:user-1:admin:9999999999:8a7228c9b9d7484bb1ecf53682af04d0:6d0f0536"),
        _b64("access:u-é中:名:123") + "==",  # 非 ASCII 身份 + 非规范 padding
        _signed_token("access:bob:alice:7", b""),  # 空签名字节
    ],
)
def test_valid_tokens_resolve_to_identity_triple(token: str) -> None:
    decoded = kernel_token.decode_kernel_token(token)
    assert decoded is not None
    user_id, username, exp = decoded
    assert user_id == "user-1" or user_id in {"u-é中", "bob"}
    assert username in {"admin", "名", "alice"}
    assert exp in {9999999999, 123, 7}
    # 性质：exp 恒为非负 int（模块保证 int 化成功才返回）
    assert isinstance(exp, int) and exp >= 0


def test_hmac_tail_preserved_verbatim_in_fifth_segment() -> None:
    """exp 后的签名尾巴（第 5 段起）不影响身份三段还原——split(":", 4) 契约。"""
    payload = "access:uid:alice:42:aa:bb:cc"
    decoded = kernel_token.decode_kernel_token(_signed_token(payload))
    assert decoded == ("uid", "alice", 42)


# ── 过期：模块不做时间判定，exp 原样返回（过期处置属调用方）──────────────


def test_expired_exp_still_decodes_caller_decides() -> None:
    decoded = kernel_token.decode_kernel_token(_b64("access:user-1:admin:1"))
    assert decoded == ("user-1", "admin", 1)


# ── 垃圾输入：一律 None（fail-closed）───────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        "",  # 空串
        "!!!not-base64!!!",  # 非 base64
        _b64("x"),  # 解出但不足 4 段
        _b64("access:user:n"),  # 缺段（3 段）
        _b64("access:user:n:notnum"),  # exp 非数字
        _signed_token("access:user:n:notnum"),  # 带点形态同样缺段/非数字
        "===.====",  # padding 大杂烩，解出非 UTF-8 语义
    ],
)
def test_garbage_tokens_return_none(token: str) -> None:
    assert kernel_token.decode_kernel_token(token) is None


def test_dot_separates_signature_even_if_payload_b64_invalid_utf8() -> None:
    """点号必须先切：payload 段后的签名字节混入解码必炸 UTF-8（回归根因锁死）。

    构造：整串（payload+签名不切点）无法解码出合法身份；带点切分后正常。
    """
    raw_payload = _b64("access:uid:admin:5")
    sig = base64.b64encode(bytes(range(256))).decode("ascii")  # 非文本字节
    assert kernel_token.decode_kernel_token(f"{raw_payload}.{sig}") == ("uid", "admin", 5)
