"""db-read 内核库读契约桥 —— 插件侧读内核 SQLite 的唯一通道（C2 刀2）。

数据面声明式注册（内核 ``config/kernel/read_faces.yaml``）+ 自由只读 SELECT +
租户内建收窄 + 权限分档，执法核心在内核 db-admin crate read_faces 模块
（C2 刀1 落地，Rust 侧契约测试 29 例）。本模块只做两件事：

- **调用装配**：``plugin.get_capability("db-read").call("query", params)``，
  入参 ``{face, sql, params?, _authorization?}``——身份经 ``_authorization``
  （入站 Authorization 原头值）由内核 resolve_request_user 复权，调用方不可
  传 tenant/role 形参（传入即拒）；租户收窄值只能来自身份链（spec §4）。
- **信封解包**：成功 ``{status: 200, body: {columns, rows}}`` → 行列表；
  业务失败 ``{status, error: {code, message}}`` → :class:`DbReadQueryError`
  （携带 status 与内核错误码）。降级策略（返 None/空集/全 0）归各调用面
  自持——调用方 except :class:`DbReadQueryError` 按既有降级契约处理。

绑定参数为命名 dict（``:name`` 占位）；tenant 档面（收窄查询）只收命名
参数，位置占位不可混；``__tenant__`` 为契约保留名，占用即 422 拒。
聚合形状纪律：无 GROUP BY 的裸聚合把全租户聚成一行、包装无法正确收窄——
聚合必须 ``GROUP BY <租户列>`` 后交包装收窄（71dca0fff 实证教训）。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入（``from db_read import db_read_query, DbReadQueryError``）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["DbReadQueryError", "db_read_query", "unwrap_db_read_envelope"]

_ROW_ENVELOPE_OK = 200


class DbReadQueryError(Exception):
    """db-read 契约查询失败（能力未注入/通道异常/内核信封非 200）。

    携带内核侧 HTTP 语义状态码（401 未认证 / 403 档位拒绝 / 404 面未注册 /
    422 SQL 拒绝 / 503 存储故障）；能力未注入/通道异常时 status 为 None。
    调用方按降级契约捕获，不区分细码（细分码由内核侧观测与测试承担）。
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def unwrap_db_read_envelope(envelope: Any) -> list[list[Any]]:
    """db-read.query 信封解包：成功出行列表，失败/异形信封抛 DbReadQueryError。

    - ``{status: 200, body: {columns, rows}}`` → rows（行 = 列值数组，位置
      对齐 columns；调用方按 SELECT 投影序解包）；
    - ``{status, error: {code, message}}`` → DbReadQueryError(status, message)；
    - 其余异形 → DbReadQueryError(None)。
    """
    if isinstance(envelope, dict) and envelope.get("status") == _ROW_ENVELOPE_OK:
        body = envelope.get("body")
        rows = body.get("rows") if isinstance(body, dict) else None
        if isinstance(rows, list):
            return rows
        raise DbReadQueryError("db-read 信封 body 缺 rows 列表", _ROW_ENVELOPE_OK)
    if isinstance(envelope, dict) and "error" in envelope:
        err = envelope["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raw_status = envelope.get("status")
        status = int(raw_status) if isinstance(raw_status, (int, float)) else None
        raise DbReadQueryError(str(message or "db-read 查询失败"), status)
    raise DbReadQueryError(f"db-read 返回异常信封: {envelope!r}")


async def db_read_query(
    plugin: Any,
    face: str,
    sql: str,
    params: dict[str, Any] | None = None,
    authorization: str = "",
) -> list[list[Any]]:
    """在声明数据面上执行自由只读 SELECT（db-read.query capability 通道）。

    Args:
        plugin: AgentOSPlugin 实例（get_capability 句柄来源）。
        face: 注册数据面 id（read_faces.yaml；面外查询内核 404 拒）。
        sql: 单条只读 SELECT；tenant 档面须投影面表真实租户列（收窄包装
            引用该列，未投影 422 拒）。
        params: 命名绑定参数（``:name`` 占位）；无参传 None/``{}``。
        authorization: 入站 Authorization 原头值（内核复权身份锚）。空 =
            无凭证（内核 401 拒，tenant 档调用面应先行 fail-closed）。

    Returns:
        行列表（行 = 列值数组）。

    Raises:
        DbReadQueryError: 能力未注入（KeyError 归一）/通道异常/内核信封
            非 200——status 字段携带内核语义码。
    """
    call_params: dict[str, Any] = {"face": face, "sql": sql}
    if params:
        call_params["params"] = params
    if authorization:
        call_params["_authorization"] = authorization
    try:
        handle = plugin.get_capability("db-read")
        envelope = await handle.call("query", call_params)
    except DbReadQueryError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 通道失败归一为契约错误，降级归调用面
        raise DbReadQueryError(f"db-read 能力调用失败: {exc}") from exc
    return unwrap_db_read_envelope(envelope)
