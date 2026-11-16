# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""db-read 内核库读契约桥（plugins/shared/db_read.py，C2 刀2）单测。

覆盖插件侧装配契约（spec §9）：信封解包（成功/业务失败/异形）、能力调用
归一（KeyError/通道异常 → DbReadQueryError）、身份参数穿线（_authorization
原头透传；无凭证时不携带该键）。收窄执法本体由 Rust 契约测试承担
（kernel/crates/db-admin/src/read_faces/tests.rs），此处不重复。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from db_read import DbReadQueryError, db_read_query, unwrap_db_read_envelope  # noqa: E402


class _FakeHandle:
    """CapabilityHandle 替身：记录调用参数并按预设返回/抛出。"""

    def __init__(
        self, result: Any = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: Any = None) -> Any:
        self.calls.append((method, params))
        if self._error is not None:
            raise self._error
        return self._result


class _FakePlugin:
    def __init__(self, handle: _FakeHandle | None = None, missing: bool = False) -> None:
        self._handle = handle
        self._missing = missing

    def get_capability(self, name: str) -> Any:
        if self._missing or name != "db-read":
            raise KeyError(f"capability not injected: {name}")
        return self._handle


def test_unwrap_success_returns_rows() -> None:
    rows = unwrap_db_read_envelope(
        {"status": 200, "body": {"columns": ["tenant_id"], "rows": [["t-a"], ["t-b"]]}}
    )
    assert rows == [["t-a"], ["t-b"]]


def test_unwrap_error_envelope_raises_with_status() -> None:
    with pytest.raises(DbReadQueryError) as exc_info:
        unwrap_db_read_envelope(
            {"status": 403, "error": {"code": "DB_READ_ROLE_DENIED", "message": "档位拒绝"}}
        )
    assert exc_info.value.status == 403
    assert "档位拒绝" in str(exc_info.value)


def test_unwrap_malformed_envelope_raises() -> None:
    with pytest.raises(DbReadQueryError) as malformed:
        unwrap_db_read_envelope(["not", "an", "envelope"])
    assert malformed.value.status is None
    with pytest.raises(DbReadQueryError):
        unwrap_db_read_envelope({"status": 200, "body": {"columns": []}})


def test_query_success_threads_identity_and_params() -> None:
    handle = _FakeHandle(result={"status": 200, "body": {"columns": [], "rows": [[1]]}})
    rows = _run(
        db_read_query(
            _FakePlugin(handle),
            "traces",
            "SELECT tenant_id FROM traces GROUP BY tenant_id",
            {"limit": 5},
            "tok-1",
        )
    )
    assert rows == [[1]]
    method, params = handle.calls[0]
    assert method == "query"
    assert params["face"] == "traces"
    assert params["params"] == {"limit": 5}
    assert params["_authorization"] == "tok-1"


def test_query_without_authorization_omits_identity_key() -> None:
    # 无凭证调用照发（内核侧 401 裁决）——插件不伪造也不省略 face/sql
    handle = _FakeHandle(result={"status": 200, "body": {"columns": [], "rows": []}})
    _run(db_read_query(_FakePlugin(handle), "pipeline_state", "SELECT 1"))
    params = handle.calls[0][1]
    assert "_authorization" not in params


def test_query_capability_missing_normalized() -> None:
    with pytest.raises(DbReadQueryError) as exc_info:
        _run(db_read_query(_FakePlugin(missing=True), "traces", "SELECT 1"))
    assert exc_info.value.status is None


def test_query_contract_error_reraised_as_is() -> None:
    # 通道内已归一的契约错误原样透传（不二次包装，status 不丢）
    handle = _FakeHandle(error=DbReadQueryError("内核 403", 403))
    with pytest.raises(DbReadQueryError) as exc_info:
        _run(db_read_query(_FakePlugin(handle), "traces", "SELECT 1"))
    assert exc_info.value is handle._error


def test_query_channel_exception_normalized() -> None:
    handle = _FakeHandle(error=RuntimeError("bridge closed"))
    with pytest.raises(DbReadQueryError) as exc_info:
        _run(db_read_query(_FakePlugin(handle), "traces", "SELECT 1"))
    assert "bridge closed" in str(exc_info.value)


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
