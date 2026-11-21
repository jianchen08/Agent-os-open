"""远端文件面（SFTP）——sync_agent 的能力切片：列目录 / stat / 读 / 写。

定位：连接插件自带「看得到对端文件」的最小面，供前端文件浏览器与后续自动化
取远端目录内容。传输是 SFTP（paramiko），目标表来自配置段 ``remote_fs``
（``config/plugins/sync_agent/remote_fs.yaml``），空表 = 面关闭。

边界（与挂载的区别，务必按此理解）：
- 这是端点级浏览，不是文件系统挂载：没有统一路径命名空间，第三方应用不可见；
  远端路径只在 ``target.root`` 之下解释。
- 目标表只存 ``key_path`` 引用，私钥本体在设备私有目录（0600），不进配置面。
- hostkey 首连 pin 进 ``known_hosts_path``；之后不符即拒连（不做无条件信任）。

端点（plugin.json 声明，auth=user；``/ext/sync_agent/fs/**``）：
    GET  /ext/sync_agent/fs/targets
    GET  /ext/sync_agent/fs/probe?target=<id>
    GET  /ext/sync_agent/fs/list?target=<id>&path=<dir>
    GET  /ext/sync_agent/fs/stat?target=<id>&path=<path>
    GET  /ext/sync_agent/fs/read?target=<id>&path=<file>&offset=&length=
    POST /ext/sync_agent/fs/write?target=<id>&path=<file>  body={"content_base64":...}
"""

from __future__ import annotations

import base64
import logging
import os
import posixpath
import socket
import stat as stat_module
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from _root_api import decode_body

logger = logging.getLogger(__name__)

#: 单次 read 返回上限（字节）；更大文件由调用方按 offset 分页。
MAX_READ_BYTES = 1024 * 1024

#: SFTP 传输窗口（默认 64KiB 窗口/32KiB 包在公网 RTT 下吞吐偏低）。
_WINDOW_BYTES = 4 * 1024 * 1024
_PACKET_BYTES = 32768

_TARGET_KEYS = ("id", "label", "host", "port", "user", "key_path", "root", "known_hosts_path")


class RemoteFsError(Exception):
    """远端文件面错误（``status`` 供端点层透出 4xx/5xx）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _SftpLike(Protocol):
    """本模块用到的 SFTPClient 子集（测试以假对象替换 paramiko）。"""

    def close(self) -> None: ...

    def listdir_attr(self, path: str) -> list[Any]: ...
    def stat(self, path: str) -> Any: ...
    def open(self, path: str, mode: str) -> Any: ...
    def normalize(self, path: str) -> str: ...


class _ConnectionLike(Protocol):
    """一次远端连接：SFTP 通道 + 活性判定 + 关闭。"""

    sftp: _SftpLike

    def is_active(self) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RemoteTarget:
    """一个远端目标（SFTP 连接参数 + 路径根）。"""

    id: str
    host: str
    user: str
    key_path: str
    root: str
    port: int = 22
    label: str = ""
    known_hosts_path: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], index: int) -> RemoteTarget:
        """配置项 → 目标；缺必填字段即 400（fail-closed，不猜默认 host）。"""
        unknown = sorted(set(raw) - set(_TARGET_KEYS))
        if unknown:
            # 拼错键静默忽略会让"配了却没生效"变成排障黑洞，直接拒。
            raise RemoteFsError(f"targets[{index}] 含未知字段：{','.join(unknown)}")
        target_id = str(raw.get("id") or "").strip()
        host = str(raw.get("host") or "").strip()
        user = str(raw.get("user") or "").strip()
        key_path = str(raw.get("key_path") or "").strip()
        missing = [
            name
            for name, value in (("id", target_id), ("host", host), ("user", user), ("key_path", key_path))
            if not value
        ]
        if missing:
            raise RemoteFsError(f"targets[{index}] 缺必填字段：{','.join(missing)}")
        raw_port = raw.get("port")
        try:
            # port=0 是 falsy，`or 22` 会把它静默吞成默认值——显式判 None/空串
            port = 22 if raw_port in (None, "") else int(str(raw_port))
        except (TypeError, ValueError) as exc:
            raise RemoteFsError(f"targets[{index}].port 非法：{raw.get('port')!r}") from exc
        if not 0 < port < 65536:
            raise RemoteFsError(f"targets[{index}].port 超范围：{port}")
        root = str(raw.get("root") or "/").strip() or "/"
        if not root.startswith("/"):
            raise RemoteFsError(f"targets[{index}].root 必须是绝对路径：{root!r}")
        return cls(
            id=target_id,
            host=host,
            user=user,
            key_path=key_path,
            root=root.rstrip("/") or "/",
            port=port,
            label=str(raw.get("label") or "").strip(),
            known_hosts_path=str(raw.get("known_hosts_path") or "").strip() or (key_path + "_known_hosts"),
        )

    def public(self) -> dict[str, Any]:
        """对外视图：不含 key_path / known_hosts_path（凭据路径不出端点）。"""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "root": self.root,
            "transport": "sftp",
        }


def _entry_payload(name: str, attrs: Any) -> dict[str, Any]:
    """SFTPAttributes → 端点条目（type/size/mtime/mode）。"""
    mode = int(getattr(attrs, "st_mode", 0) or 0)
    if stat_module.S_ISDIR(mode):
        kind = "dir"
    elif stat_module.S_ISLNK(mode):
        kind = "link"
    else:
        kind = "file"
    return {
        "name": name,
        "type": kind,
        "size": int(getattr(attrs, "st_size", 0) or 0),
        "mtime": int(getattr(attrs, "st_mtime", 0) or 0),
        "mode": oct(mode & 0o777),
    }


def resolve_remote_path(target: RemoteTarget, rel: str) -> str:
    """相对路径 → target.root 之下的绝对远端路径；越界（含 ``..``）一律拒。

    归一一律走 ``posixpath.normpath``（远端恒是 POSIX 路径语义），再用根前缀
    判定兜住 ``..`` 逃逸——单一闸口，不留"先过滤再拼接"的双规则。
    """
    rel = (rel or "").strip()
    root = target.root
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts:
        return root
    joined = posixpath.normpath("/".join([root.rstrip("/"), *parts]))
    if root != "/" and not (joined == root or joined.startswith(root + "/")):
        raise RemoteFsError(f"path 越出 target.root={root}")
    return joined


def _connect_sftp(target: RemoteTarget) -> _ConnectionLike:
    """建立真实 SFTP 连接（paramiko 延迟导入：未走远端文件面就不付 import 成本）。"""
    # 延迟导入：未走远端文件面就不付 import 成本；mypy 环境（根 env 无 paramiko）
    # 不装该依赖，忽略未解析导入（类型面按 Any 走）
    import paramiko  # noqa: PLC0415  # type: ignore[import-not-found]

    sock = socket.create_connection((target.host, target.port), timeout=20)
    transport = paramiko.Transport(sock)
    transport.default_window_size = _WINDOW_BYTES
    transport.default_max_packet_size = _PACKET_BYTES
    try:
        transport.start_client(timeout=30)
        hostkey = transport.get_remote_server_key()
        known = paramiko.HostKeys()
        if target.known_hosts_path and os.path.exists(target.known_hosts_path):
            known.load(target.known_hosts_path)
        entry = known.lookup(target.host)
        if entry is None:
            known.add(target.host, hostkey.get_name(), hostkey)
            if target.known_hosts_path:
                known.save(target.known_hosts_path)
        elif entry.get(hostkey.get_name()) != hostkey:
            raise RemoteFsError(
                f"hostkey 与已 pin 记录不符（{hostkey.get_name()}），拒绝连接；确认后删除 {target.known_hosts_path}",
                502,
            )
        transport.auth_publickey(target.user, paramiko.ECDSAKey(filename=target.key_path))
        sftp = paramiko.SFTPClient.from_transport(transport)
    except Exception:
        transport.close()
        raise
    return _ParamikoConnection(transport, sftp)


class _ParamikoConnection:
    """paramiko (Transport, SFTPClient) 对 ``_ConnectionLike`` 的适配。"""

    def __init__(self, transport: Any, sftp: _SftpLike) -> None:
        self._transport = transport
        self.sftp = sftp

    def is_active(self) -> bool:
        return bool(self._transport.is_active())

    def close(self) -> None:
        for obj in (self.sftp, self._transport):
            try:
                obj.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不阻断重建
                logger.debug("remote_fs: 关闭连接失败（忽略）", exc_info=True)


class _Session:
    """单目标连接单元：一把锁串行化 + 探活失败即重建。"""

    def __init__(self, target: RemoteTarget, connect: Callable[[RemoteTarget], _ConnectionLike]) -> None:
        self.target = target
        self._connect = connect
        self._conn: _ConnectionLike | None = None
        self.lock = threading.Lock()

    def ensure(self) -> tuple[_ConnectionLike, bool, dict[str, Any] | None]:
        """确保连接可用；返回 (连接, 是否复用, 新建连接耗时表)。调用方须持锁。"""
        if self._conn is not None and self._conn.is_active():
            try:
                self._conn.sftp.listdir_attr(self.target.root)
                return self._conn, True, None
            except Exception:  # noqa: BLE001 —— 探活失败按断链处理
                logger.info("remote_fs: target=%s 复用探活失败，重建连接", self.target.id)
                self.close()
        marks: dict[str, Any] = {}
        t0 = time.time()
        self._conn = self._connect(self.target)
        marks["connect_ms"] = round((time.time() - t0) * 1000, 1)
        marks["host"] = self.target.host
        return self._conn, False, marks

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class RemoteFsService:
    """远端文件面服务（纯同步实现；端点层负责 ``asyncio.to_thread``）。"""

    def __init__(
        self,
        targets: Mapping[str, RemoteTarget],
        *,
        connect: Callable[[RemoteTarget], _ConnectionLike] = _connect_sftp,
    ) -> None:
        self._targets = dict(targets)
        self._connect = connect
        self._sessions: dict[str, _Session] = {}
        self._guard = threading.Lock()

    # ── 构造 ──

    @classmethod
    def from_config(cls, section: Mapping[str, Any] | None) -> RemoteFsService | None:
        """配置段 → 服务；未启用/无目标返回 None（面关闭，端点诚实报未配置）。"""
        if not section or not bool(section.get("enabled", True)):
            return None
        raw_targets = section.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            return None
        targets: dict[str, RemoteTarget] = {}
        for index, raw in enumerate(raw_targets):
            if not isinstance(raw, Mapping):
                raise RemoteFsError(f"targets[{index}] 必须是映射")
            target = RemoteTarget.from_mapping(raw, index)
            if target.id in targets:
                raise RemoteFsError(f"targets 重复 id：{target.id}")
            targets[target.id] = target
        return cls(targets)

    @property
    def target_ids(self) -> list[str]:
        return sorted(self._targets)

    # ── 会话 ──

    def _session(self, target_id: str) -> _Session:
        target = self._targets.get(target_id)
        if target is None:
            raise RemoteFsError(f"未知 target={target_id!r}（已配置：{','.join(sorted(self._targets))}）")
        with self._guard:
            session = self._sessions.get(target_id)
            if session is None:
                session = _Session(target, self._connect)
                self._sessions[target_id] = session
            return session

    def _run(self, target_id: str, fn: Callable[[_SftpLike, RemoteTarget], dict[str, Any]]) -> dict[str, Any]:
        """在目标会话锁内执行；失败即丢弃连接（避免毒化复用）。"""
        session = self._session(target_id)
        with session.lock:
            conn, reused, marks = session.ensure()
            try:
                payload = fn(conn.sftp, session.target)
            except RemoteFsError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 传输层失败丢弃连接后上抛
                logger.info("remote_fs: target=%s 操作失败，丢弃连接：%s", target_id, exc)
                session.close()
                raise RemoteFsError(f"remote_fs: {exc}", 502) from exc
        payload["reused"] = reused
        if marks:
            payload["connect_ms"] = marks.get("connect_ms")
        return payload

    def close_all(self) -> None:
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    # ── 操作 ──

    def targets_payload(self) -> dict[str, Any]:
        items = [self._targets[tid].public() for tid in sorted(self._targets)]
        return {"targets": items, "count": len(items)}

    def probe(self, target_id: str) -> dict[str, Any]:
        session = self._session(target_id)
        with session.lock:
            conn, reused, marks = session.ensure()
            home = conn.sftp.normalize(".")
        payload: dict[str, Any] = {
            "target": target_id,
            "reused": reused,
            "root": session.target.root,
            "home": home,
            "alive": True,
        }
        if marks:
            payload["connect_ms"] = marks.get("connect_ms")
        return payload

    def list_dir(self, target_id: str, path: str) -> dict[str, Any]:
        def run(sftp: _SftpLike, target: RemoteTarget) -> dict[str, Any]:
            remote = resolve_remote_path(target, path)
            t0 = time.time()
            entries = [_entry_payload(item.filename, item) for item in sftp.listdir_attr(remote)]
            entries.sort(key=lambda item: (item["type"] != "dir", str(item["name"]).lower()))
            return {
                "path": remote,
                "entries": entries,
                "count": len(entries),
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

        return self._run(target_id, run)

    def stat_path(self, target_id: str, path: str) -> dict[str, Any]:
        def run(sftp: _SftpLike, target: RemoteTarget) -> dict[str, Any]:
            remote = resolve_remote_path(target, path)
            payload = _entry_payload(remote.rsplit("/", 1)[-1] or remote, sftp.stat(remote))
            payload["path"] = remote
            return payload

        return self._run(target_id, run)

    def read_file(self, target_id: str, path: str, offset: int = 0, length: int = MAX_READ_BYTES) -> dict[str, Any]:
        offset = max(0, int(offset))
        length = max(1, min(int(length), MAX_READ_BYTES))

        def run(sftp: _SftpLike, target: RemoteTarget) -> dict[str, Any]:
            remote = resolve_remote_path(target, path)
            t0 = time.time()
            with sftp.open(remote, "rb") as handle:
                if offset:
                    handle.seek(offset)
                # 预取整段：默认 32KiB 分块在公网 RTT 下会把 1MiB 拖成几十个往返
                handle.prefetch(length)
                data = handle.read(length)
            size = int(getattr(sftp.stat(remote), "st_size", 0) or 0)
            return {
                "path": remote,
                "offset": offset,
                "length": len(data),
                "size": size,
                "eof": offset + len(data) >= size,
                "content_base64": base64.b64encode(data).decode("ascii"),
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

        return self._run(target_id, run)

    def write_file(self, target_id: str, path: str, data: bytes, append: bool = False) -> dict[str, Any]:
        def run(sftp: _SftpLike, target: RemoteTarget) -> dict[str, Any]:
            remote = resolve_remote_path(target, path)
            t0 = time.time()
            with sftp.open(remote, "ab" if append else "wb") as handle:
                handle.write(data)
            size = int(getattr(sftp.stat(remote), "st_size", 0) or 0)
            return {
                "path": remote,
                "written": len(data),
                "size": size,
                "append": append,
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

        return self._run(target_id, run)

    # ── 端点分发 ──

    def handle(
        self, rest: list[str], method: str, query: Mapping[str, str], raw_body: str
    ) -> tuple[int, dict[str, Any]]:
        """``/ext/sync_agent/fs/<rest>`` 分发；返回 (HTTP 状态, 载荷)。"""
        target_id = str(query.get("target") or "").strip()
        if rest == ["targets"] and method == "GET":
            return 200, self.targets_payload()
        if rest == ["probe"] and method == "GET":
            return 200, self.probe(target_id)
        if rest == ["list"] and method == "GET":
            return 200, self.list_dir(target_id, str(query.get("path") or ""))
        if rest == ["stat"] and method == "GET":
            return 200, self.stat_path(target_id, str(query.get("path") or ""))
        if rest == ["read"] and method == "GET":
            return 200, self.read_file(
                target_id,
                str(query.get("path") or ""),
                offset=_query_int(query, "offset", 0),
                length=_query_int(query, "length", MAX_READ_BYTES),
            )
        if rest == ["write"] and method == "POST":
            return 200, self.write_file(target_id, str(query.get("path") or ""), *_decode_write_body(raw_body))
        raise RemoteFsError(f"remote_fs: no route for {method} /fs/{'/'.join(rest)}", 404)


def _query_int(query: Mapping[str, str], key: str, default: int) -> int:
    """query 数字参数（缺省/空串取默认；非法是调用方错误 400）。"""
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RemoteFsError(f"query.{key} 非法：{raw!r}") from exc


def _decode_write_body(raw_body: str) -> tuple[bytes, bool]:
    """写端点 body → (字节, append)；非法体是调用方错误（400）。

    ``raw_body`` 由内核透传，可能是 base64 包裹的 JSON 或明文 JSON——统一走
    ``http_json.decode_body``（shared 单点，不在这里重造双形态判定）。
    """
    try:
        body = decode_body(raw_body)
    except ValueError as exc:
        raise RemoteFsError(f"body 非法 JSON：{exc}") from exc
    if "content_base64" not in body:
        raise RemoteFsError("body 需含 content_base64")
    try:
        data = base64.b64decode(str(body["content_base64"]), validate=True)
    except Exception as exc:  # noqa: BLE001 —— 非法 base64 是调用方错误
        raise RemoteFsError(f"content_base64 解码失败：{exc}") from exc
    return data, bool(body.get("append"))
