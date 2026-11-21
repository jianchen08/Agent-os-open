# @feature: FP-0.2.多端同步 远端文件面 SFTP | @ci: python-coverage
"""sync_agent 远端文件面（remote_fs）单测。

paramiko / socket 属外部依赖，按测试纪律打桩（stub 模块 + 假 SFTP 对象）；
行为断言（路径闸、状态码、连接复用/丢弃、端点分发）不依赖实现细节。
"""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("sync_agent")

import remote_fs  # noqa: E402

# ══ 假 SFTP 面 ══


class _Attrs:
    def __init__(self, filename: str, mode: int, size: int = 0, mtime: int = 1_700_000_000) -> None:
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _Handle:
    def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
        self._store = store
        self._path = path
        self._mode = mode
        self._pos = 0
        self.prefetch_calls: list[int] = []

    def __enter__(self) -> _Handle:
        if self._mode.startswith("w") or self._mode.startswith("a"):
            self._store.setdefault(self._path, b"")
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def seek(self, pos: int) -> None:
        self._pos = pos

    def prefetch(self, size: int) -> None:
        self.prefetch_calls.append(size)

    def read(self, size: int) -> bytes:
        data = self._store.get(self._path, b"")
        chunk = data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> None:
        if self._mode.startswith("a"):
            self._store[self._path] = self._store.get(self._path, b"") + data
        else:
            self._store[self._path] = data
        self._pos = len(self._store[self._path])


class _Sftp:
    def __init__(self, *, dirs: dict[str, list[_Attrs]] | None = None, files: dict[str, bytes] | None = None) -> None:
        self.dirs = dirs or {}
        self.files = files or {}
        self.handles: list[_Handle] = []
        self.fail_listdir = False

    def listdir_attr(self, path: str) -> list[_Attrs]:
        if self.fail_listdir:
            raise OSError("transport down")
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return self.dirs[path]

    def stat(self, path: str) -> _Attrs:
        if path in self.files:
            return _Attrs(path.rsplit("/", 1)[-1], 0o100644, len(self.files[path]))
        if path in self.dirs:
            return _Attrs(path.rsplit("/", 1)[-1], 0o040755, 4096)
        raise FileNotFoundError(path)

    def open(self, path: str, mode: str) -> _Handle:
        handle = _Handle(self.files, path, mode)
        self.handles.append(handle)
        return handle

    def normalize(self, path: str) -> str:
        del path
        return "/home/tester"

    def close(self) -> None:
        return None


class _Conn:
    def __init__(self, sftp: _Sftp, active: bool = True) -> None:
        self.sftp = sftp
        self._active = active
        self.closed = 0

    def is_active(self) -> bool:
        return self._active

    def close(self) -> None:
        self.closed += 1
        self._active = False


def _service(
    monkeypatch: pytest.MonkeyPatch, sftp: _Sftp | None = None
) -> tuple[remote_fs.RemoteFsService, list[_Sftp]]:
    """构造服务；connect 走到假连接，返回服务与「每次新建连接用的 sftp」序列。"""
    created: list[_Sftp] = []

    def connect(target: remote_fs.RemoteTarget) -> _Conn:
        del target
        sftp_obj = _Sftp() if sftp is None else (created and created[-1] or sftp)
        if sftp is None:
            created.append(sftp_obj)
        return _Conn(sftp_obj)

    target = remote_fs.RemoteTarget.from_mapping(
        {"id": "vps", "host": "203.0.113.10", "user": "agentos", "key_path": "/k", "root": "/srv/data"},
        0,
    )
    return remote_fs.RemoteFsService({"vps": target}, connect=connect), created


# ══ 配置解析 ══


def test_from_config_disabled_or_empty_returns_none() -> None:
    assert remote_fs.RemoteFsService.from_config(None) is None
    assert remote_fs.RemoteFsService.from_config({}) is None
    assert remote_fs.RemoteFsService.from_config({"enabled": False, "targets": [{"id": "a"}]}) is None
    assert remote_fs.RemoteFsService.from_config({"enabled": True, "targets": []}) is None


def test_from_config_rejects_bad_targets() -> None:
    base = {"id": "a", "host": "h", "user": "u", "key_path": "/k"}
    with pytest.raises(remote_fs.RemoteFsError) as unknown:
        remote_fs.RemoteFsService.from_config({"targets": [{**base, "hops": 2}]})
    assert "未知字段" in str(unknown.value)

    with pytest.raises(remote_fs.RemoteFsError) as missing:
        remote_fs.RemoteFsService.from_config({"targets": [{"id": "a", "host": "h"}]})
    assert "缺必填字段" in str(missing.value)

    with pytest.raises(remote_fs.RemoteFsError) as dup:
        remote_fs.RemoteFsService.from_config({"targets": [base, dict(base)]})
    assert "重复 id" in str(dup.value)

    with pytest.raises(remote_fs.RemoteFsError) as port:
        remote_fs.RemoteFsService.from_config({"targets": [{**base, "port": 0}]})
    assert "超范围" in str(port.value)

    with pytest.raises(remote_fs.RemoteFsError) as root:
        remote_fs.RemoteFsService.from_config({"targets": [{**base, "root": "rel/path"}]})
    assert "绝对路径" in str(root.value)

    with pytest.raises(remote_fs.RemoteFsError) as not_map:
        remote_fs.RemoteFsService.from_config({"targets": ["nope"]})
    assert "必须是映射" in str(not_map.value)


def test_targets_payload_hides_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch)
    payload = service.targets_payload()
    assert payload["count"] == 1
    item = payload["targets"][0]
    assert item["id"] == "vps"
    assert item["transport"] == "sftp"
    assert "key_path" not in item
    assert "known_hosts_path" not in item


# ══ 路径闸 ══


def test_resolve_remote_path_guards_root() -> None:
    target = remote_fs.RemoteTarget.from_mapping(
        {"id": "a", "host": "h", "user": "u", "key_path": "/k", "root": "/srv/data/"}, 0
    )
    assert remote_fs.resolve_remote_path(target, "") == "/srv/data"
    assert remote_fs.resolve_remote_path(target, "a//b/./c") == "/srv/data/a/b/c"
    assert remote_fs.resolve_remote_path(target, "sub/../b") == "/srv/data/b"
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        remote_fs.resolve_remote_path(target, "../etc/passwd")
    assert exc.value.status == 400
    assert "越出" in str(exc.value)


# ══ 操作面 ══


def test_list_dir_sorts_dirs_first_and_reuses_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(
        dirs={
            "/srv/data": [
                _Attrs("b.txt", 0o100644, 3),
                _Attrs("zdir", 0o040755, 4096),
                _Attrs("Alink", 0o120777),
                _Attrs("adir", 0o040755, 4096),
            ]
        }
    )
    service, _ = _service(monkeypatch, sftp)

    first = service.list_dir("vps", "")
    assert [entry["name"] for entry in first["entries"]] == ["adir", "zdir", "Alink", "b.txt"]
    assert first["count"] == 4
    assert first["reused"] is False
    assert first["connect_ms"] is not None
    assert {entry["type"] for entry in first["entries"]} == {"dir", "file", "link"}
    assert first["entries"][3]["size"] == 3
    assert first["entries"][3]["mode"] == "0o644"

    second = service.list_dir("vps", "")
    assert second["reused"] is True
    assert "connect_ms" not in second


def test_stat_and_read_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []}, files={"/srv/data/a.txt": b"0123456789"})
    service, _ = _service(monkeypatch, sftp)

    stat = service.stat_path("vps", "a.txt")
    assert stat["name"] == "a.txt"
    assert stat["type"] == "file"
    assert stat["path"] == "/srv/data/a.txt"

    chunk = service.read_file("vps", "a.txt", offset=2, length=4)
    assert base64.b64decode(chunk["content_base64"]) == b"2345"
    assert chunk["offset"] == 2
    assert chunk["length"] == 4
    assert chunk["size"] == 10
    assert chunk["eof"] is False
    assert sftp.handles[-1].prefetch_calls == [4]

    tail = service.read_file("vps", "a.txt", offset=8, length=99)
    assert base64.b64decode(tail["content_base64"]) == b"89"
    assert tail["eof"] is True

    written = service.write_file("vps", "new.bin", b"abc")
    assert written["written"] == 3
    assert written["size"] == 3
    appended = service.write_file("vps", "new.bin", b"de", append=True)
    assert appended["size"] == 5
    assert sftp.files["/srv/data/new.bin"] == b"abcde"


def test_read_length_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []}, files={"/srv/data/a.txt": b"x" * 8})
    service, _ = _service(monkeypatch, sftp)
    payload = service.read_file("vps", "a.txt", offset=0, length=10**9)
    assert sftp.handles[-1].prefetch_calls == [remote_fs.MAX_READ_BYTES]
    assert payload["length"] == 8


def test_unknown_target_lists_configured_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch)
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        service.list_dir("nope", "")
    assert exc.value.status == 400
    assert "vps" in str(exc.value)


def test_transport_failure_drops_connection_then_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []})
    service, _ = _service(monkeypatch, sftp)

    assert service.list_dir("vps", "")["reused"] is False
    session = service._sessions["vps"]
    conn = session._conn
    assert conn is not None

    sftp.fail_listdir = True
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        service.list_dir("vps", "")
    assert exc.value.status == 502
    assert conn.closed == 1
    assert session._conn is None

    sftp.fail_listdir = False
    assert service.list_dir("vps", "")["reused"] is False


def test_probe_reports_home_and_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch, _Sftp(dirs={"/srv/data": []}))
    first = service.probe("vps")
    assert first["home"] == "/home/tester"
    assert first["alive"] is True
    assert first["reused"] is False
    assert first["root"] == "/srv/data"
    assert service.probe("vps")["reused"] is True


def test_close_all_drops_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch, _Sftp(dirs={"/srv/data": []}))
    service.list_dir("vps", "")
    session = service._sessions["vps"]
    conn = session._conn
    service.close_all()
    assert conn is not None
    assert conn.closed == 1
    assert service._sessions == {}


# ══ 端点分发 ══


def test_handle_dispatch_and_body_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []}, files={"/srv/data/a.txt": b"hi"})
    service, _ = _service(monkeypatch, sftp)

    status, payload = service.handle(["targets"], "GET", {}, "")
    assert status == 200
    assert payload["count"] == 1

    status, payload = service.handle(["list"], "GET", {"target": "vps", "path": ""}, "")
    assert status == 200
    assert payload["count"] == 0

    status, payload = service.handle(
        ["write"],
        "POST",
        {"target": "vps", "path": "b.txt"},
        json.dumps({"content_base64": base64.b64encode(b"ok").decode()}),
    )
    assert status == 200
    assert payload["size"] == 2

    with pytest.raises(remote_fs.RemoteFsError) as not_found:
        service.handle(["nope"], "GET", {}, "")
    assert not_found.value.status == 404

    with pytest.raises(remote_fs.RemoteFsError) as bad_json:
        service.handle(["write"], "POST", {"target": "vps", "path": "c"}, "{")
    assert bad_json.value.status == 400

    with pytest.raises(remote_fs.RemoteFsError) as no_field:
        service.handle(["write"], "POST", {"target": "vps", "path": "c"}, json.dumps({}))
    assert "content_base64" in str(no_field.value)

    with pytest.raises(remote_fs.RemoteFsError) as bad_int:
        service.handle(["read"], "GET", {"target": "vps", "path": "a.txt", "offset": "x"}, "")
    assert "query.offset" in str(bad_int.value)

    with pytest.raises(remote_fs.RemoteFsError) as bad_b64:
        service.handle(["write"], "POST", {"target": "vps", "path": "c"}, json.dumps({"content_base64": "***"}))
    assert "解码失败" in str(bad_b64.value)


# ══ 真实 paramiko 连接路径（stub 模块 + 假 socket）══


class _HostKey:
    def __init__(self, base64_key: str) -> None:
        self._base64 = base64_key

    def get_name(self) -> str:
        return "ssh-ed25519"

    def get_base64(self) -> str:
        return self._base64

    def __eq__(self, other: object) -> bool:
        # 真实 paramiko PKey.__eq__ 比 key 字节；桩保持同语义
        return isinstance(other, _HostKey) and other.get_base64() == self._base64

    def __hash__(self) -> int:
        return hash(self._base64)


def _install_paramiko_stub(
    monkeypatch: pytest.MonkeyPatch, *, saved: dict[str, str] | None = None
) -> types.SimpleNamespace:
    """在 sys.modules 装 paramiko 桩：known_hosts 以 JSON 落盘，便于断言 pin 行为。"""
    state: dict[str, object] = {"auth": [], "closed": 0, "saved": saved}

    class _Transport:
        def __init__(self, sock: object) -> None:
            self.sock = sock
            self.default_window_size = 0
            self.default_max_packet_size = 0
            state["transport"] = self

        def start_client(self, timeout: int | None = None) -> None:
            del timeout

        def get_remote_server_key(self) -> _HostKey:
            return _HostKey("SERVERKEY")

        def auth_publickey(self, user: str, key: object) -> None:
            cast = state["auth"]
            assert isinstance(cast, list)
            cast.append((user, type(key).__name__))

        def is_active(self) -> bool:
            return True

        def close(self) -> None:
            state["closed"] = int(state["closed"]) + 1  # type: ignore[call-overload]

    class _HostKeys:
        def __init__(self) -> None:
            self._data: dict[str, dict[str, str]] = {}

        def load(self, path: str) -> None:
            with open(path, encoding="utf-8") as fh:
                self._data = json.load(fh)

        def lookup(self, host: str) -> dict[str, _HostKey] | None:
            entry = self._data.get(host)
            return {name: _HostKey(value) for name, value in entry.items()} if entry else None

        def add(self, host: str, keytype: str, key: _HostKey) -> None:
            self._data.setdefault(host, {})[keytype] = key.get_base64()

        def save(self, path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            saved_dict = state["saved"]
            if isinstance(saved_dict, dict):
                saved_dict.update({host: dict(keys) for host, keys in self._data.items()})

    class _ECDSAKey:
        def __init__(self, filename: str) -> None:
            self.filename = filename

    class _SftpClient:
        @staticmethod
        def from_transport(transport: object) -> _Sftp:
            state["sftp_transport"] = transport
            return _Sftp(dirs={"/srv/data": []})

    stub = types.SimpleNamespace(
        Transport=_Transport,
        HostKeys=_HostKeys,
        ECDSAKey=_ECDSAKey,
        SFTPClient=_SftpClient,
    )
    monkeypatch.setitem(sys.modules, "paramiko", stub)

    def _fake_create_connection(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    monkeypatch.setattr(remote_fs.socket, "create_connection", _fake_create_connection)
    return types.SimpleNamespace(state=state)


def test_connect_sftp_pins_hostkey_then_authenticates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stub = _install_paramiko_stub(monkeypatch)
    known = tmp_path / "known_hosts.json"
    target = remote_fs.RemoteTarget.from_mapping(
        {
            "id": "vps",
            "host": "203.0.113.10",
            "user": "agentos",
            "key_path": str(tmp_path / "id_key"),
            "known_hosts_path": str(known),
            "root": "/srv/data",
        },
        0,
    )

    conn = remote_fs._connect_sftp(target)
    assert conn.is_active() is True
    assert stub.state["auth"] == [("agentos", "_ECDSAKey")]
    assert known.exists(), "首连必须把 hostkey pin 到 known_hosts"
    transport = stub.state["transport"]
    assert transport.default_window_size == remote_fs._WINDOW_BYTES

    # 第二次连接：pin 记录匹配，不再改写文件
    before = known.read_text(encoding="utf-8")
    remote_fs._connect_sftp(target)
    assert known.read_text(encoding="utf-8") == before

    conn.close()
    assert stub.state["closed"] == 1


def test_connect_sftp_rejects_changed_hostkey(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _install_paramiko_stub(monkeypatch)
    known = tmp_path / "known_hosts.json"
    known.write_text(json.dumps({"203.0.113.10": {"ssh-ed25519": "OTHERKEY"}}), encoding="utf-8")
    target = remote_fs.RemoteTarget.from_mapping(
        {
            "id": "vps",
            "host": "203.0.113.10",
            "user": "agentos",
            "key_path": str(tmp_path / "id_key"),
            "known_hosts_path": str(known),
            "root": "/srv/data",
        },
        0,
    )

    with pytest.raises(remote_fs.RemoteFsError) as exc:
        remote_fs._connect_sftp(target)
    assert exc.value.status == 502
    assert "hostkey" in str(exc.value)


def test_from_config_accepts_valid_and_reports_ids() -> None:
    service = remote_fs.RemoteFsService.from_config(
        {
            "targets": [
                {"id": "b", "host": "h", "user": "u", "key_path": "/k"},
                {"id": "a", "host": "h2", "user": "u2", "key_path": "/k2", "port": "2222"},
            ]
        }
    )
    assert service is not None
    assert service.target_ids == ["a", "b"]


def test_port_non_numeric_is_rejected() -> None:
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        remote_fs.RemoteFsService.from_config(
            {"targets": [{"id": "a", "host": "h", "user": "u", "key_path": "/k", "port": "ssh"}]}
        )
    assert "port 非法" in str(exc.value)


def test_resolve_remote_path_normalizes_and_keeps_prefix() -> None:
    target = remote_fs.RemoteTarget.from_mapping(
        {"id": "a", "host": "h", "user": "u", "key_path": "/k", "root": "/srv/data"}, 0
    )
    assert remote_fs.resolve_remote_path(target, "  ") == "/srv/data"
    assert remote_fs.resolve_remote_path(target, "x/../y/z") == "/srv/data/y/z"
    # /srv/database 不是 /srv/data 的子路径（前缀必须带分隔符）
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        remote_fs.resolve_remote_path(target, "../database/leak")
    assert "越出" in str(exc.value)

    def _connect(target_obj: remote_fs.RemoteTarget) -> _Conn:
        del target_obj
        return _Conn(_Sftp(dirs={"/srv/data": [], "/srv/data/x": []}))

    service = remote_fs.RemoteFsService({"a": target}, connect=_connect)
    assert service.list_dir("a", "x")["path"] == "/srv/data/x"


def test_remote_fs_error_passthrough_does_not_drop_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []})
    service, _ = _service(monkeypatch, sftp)
    service.list_dir("vps", "")
    session = service._sessions["vps"]
    conn = session._conn

    # 远端路径闸抛 RemoteFsError：连接应保留（不是传输故障）
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        service.list_dir("vps", "../x")
    assert exc.value.status == 400
    assert conn is not None
    assert conn.closed == 0
    assert session._conn is conn


def test_paramiko_connection_close_swallows_errors() -> None:
    class _BadSftp(_Sftp):
        def close(self) -> None:
            raise RuntimeError("close boom")

    class _BadTransport:
        def is_active(self) -> bool:
            return False

        def close(self) -> None:
            raise RuntimeError("close boom")

    conn = remote_fs._ParamikoConnection(_BadTransport(), _BadSftp())
    assert conn.is_active() is False
    conn.close()  # 不抛（关闭失败只记 debug）


def test_handle_routes_probe_stat_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp = _Sftp(dirs={"/srv/data": []}, files={"/srv/data/a.txt": b"xy"})
    service, _ = _service(monkeypatch, sftp)

    status, payload = service.handle(["probe"], "GET", {"target": "vps"}, "")
    assert status == 200
    assert payload["target"] == "vps"

    status, payload = service.handle(["stat"], "GET", {"target": "vps", "path": "a.txt"}, "")
    assert status == 200
    assert payload["type"] == "file"

    status, payload = service.handle(["read"], "GET", {"target": "vps", "path": "a.txt"}, "")
    assert status == 200
    assert payload["length"] == 2
    # 缺省 offset/length 走 _query_int 默认值分支
    assert payload["offset"] == 0


def test_empty_body_write_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service(monkeypatch, _Sftp(dirs={"/srv/data": []}))
    with pytest.raises(remote_fs.RemoteFsError) as exc:
        service.handle(["write"], "POST", {"target": "vps", "path": "z"}, "")
    assert "content_base64" in str(exc.value)
