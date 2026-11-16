"""db-read capability 测试替身（C2 刀2 共享测试基建）。

仿真范围 = 内核 db-read 契约服务（C2 刀1，read_faces.rs）的调用方可见面：
``{status, body: {columns, rows}}`` 成功信封、tenant 档包装收窄（token→租户
映射仿身份链 `_authorization` 复权）、缺租户身份 403 信封、`fail` 注入通道
异常。收窄执法本体由 Rust 契约测试承担（read_faces/tests.rs 29 例），本替身
只让插件侧装配/降级/SQL 形状测试有可信外部依赖——真 SQL 落测试自有 sqlite
文件执行（不是内核库），包装语义与 spec §3.5 同式（一行 WHERE 追加）。

用法（插件根或 tests 车道需把 plugins/shared 置于 sys.path）::

    from _libs.testing.db_read_fake import FakeDbRead

    fake = FakeDbRead(db_path, token_tenants={"Bearer tok": "tenant-a"})
    kernel_reads.set_provider("db-read", fake.provider)   # monitoring 缝
    fake.calls  # 断言 face/params/_authorization 穿线
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# read_faces.yaml 初版四面档位（C2 刀1）；自定义注册表可经 scopes 覆盖
DEFAULT_FACE_SCOPES: dict[str, str] = {
    "pipeline_state": "tenant",
    "pipeline_state_admin": "global",
    "sessions": "tenant",
    "traces": "tenant",
}

_OK = 200


class FakeDbRead:
    """真 SQL 落测试库 + 契约信封/收窄仿真的 db-read 替身。"""

    def __init__(
        self,
        db_path: Path,
        token_tenants: dict[str, str] | None = None,
        fail: Exception | None = None,
        scopes: dict[str, str] | None = None,
    ) -> None:
        self._db_path = db_path
        self._token_tenants = token_tenants or {}
        self._fail = fail
        self._scopes = scopes or DEFAULT_FACE_SCOPES
        self.calls: list[dict[str, Any]] = []

    async def provider(
        self,
        face: str,
        sql: str,
        params: dict[str, Any] | None = None,
        authorization: str = "",
    ) -> Any:
        """kernel_reads.db_read_query / 共享桥 provider 形态（信封原样回传）。"""
        return await self.call(
            "query",
            {"face": face, "sql": sql, "params": params, "_authorization": authorization},
        )

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        del method, timeout
        self.calls.append(params)
        if self._fail is not None:
            raise self._fail
        face = str(params.get("face", ""))
        sql = str(params.get("sql", "")).strip()
        binds: dict[str, Any] = dict(params.get("params") or {})
        if self._scopes.get(face) == "tenant":
            tenant = self._token_tenants.get(str(params.get("_authorization") or ""), "")
            if not tenant:
                return {
                    "status": 403,
                    "error": {"code": "DB_READ_ROLE_DENIED", "message": "缺租户身份"},
                }
            sql = f"SELECT * FROM ({sql}) AS _scoped WHERE tenant_id = :__tenant__"
            binds["__tenant__"] = tenant
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(sql, binds)
            rows = [list(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return {"status": _OK, "body": {"columns": [], "rows": rows}}
