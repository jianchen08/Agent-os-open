# @feature: FP-0.2.四 模式体系 P3-④ agent_manager 聚合注册表 | @vision: V1 可进化 | @ci: none-local
"""agent_manager 聚合注册表测试——§4.4 双来源读面 + 模式包写面（用户副本）。

覆盖（docs/working/模式体系落地设计_20260915.md §4.4）：
1. schema 取数：真实本地 HTTP 解析 mode_agents / 字段缺席=空注册表不算降级 /
   连接失败降级 / 无 token 不发请求
2. 读面聚合：system|mode:<plugin_id> 来源标注、用户副本内容赢、降级仅系统表并注明
   （mode_registry.available/error）、agent_type/search 过滤同样作用于模式条目、
   caller token 透传
3. get 双面：用户副本可写（source=user_copy）、出厂种子只读回落
   （readonly=true, source=factory_seed）、未知/非法模式键 404
4. put 双面：首写以出厂种子 etag 为乐观锁基线建用户副本、二写 .bak 备份副本、
   409/400 拒写、写目标越界 fail-closed、用户根不可得 500
5. git 提交尝试：有仓真提交 / 无仓跳过 / 坏仓与 subprocess 失败不阻断落盘 /
   副本不在用户根内跳过提交
6. http 分发：模式路由 GET/PUT（用户门控）与系统路由 admin 闸并存

mock 仅用于外部依赖（内核 HTTP、subprocess、用户根解析不可得注入），文件 IO
一律真实临时目录。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "agent_manager_mode_registry"

_SYSTEM_YAML = "config_id: agentos\nname: 灵汐\nagent_type: main\nlevel: L1\n"
_SEED_YAML = (
    "config_id: code_writer\nname: 代码编写\ndescription: 模式包编码代理\n"
    "agent_type: specialized\nlevel: L2\napi_key: sk-seed-secret\n"
)


def _load_module() -> Any:
    """动态加载 server.py（独立模块名，避免与其他测试文件装载互相覆盖）。"""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """临时三根：系统 config/agents + 出厂模式根 + 用户根（AGENTOS_USER_ROOT）。"""
    agents = tmp_path / "config" / "agents" / "main"
    agents.mkdir(parents=True)
    (agents / "agentos.yaml").write_text(_SYSTEM_YAML, encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
    mod = _load_module()
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path / "factory_modes")
    return mod


def _seed_factory_agent(
    tmp_path: Path, mode_pkg: str = "mode_coding", stem: str = "code_writer",
    content: str = _SEED_YAML,
) -> Path:
    """出厂种子 agent yaml（真实落盘到临时出厂模式根）。"""
    agents = tmp_path / "factory_modes" / mode_pkg / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    seed = agents / f"{stem}.yaml"
    seed.write_text(content, encoding="utf-8")
    return seed


def _user_copy(
    tmp_path: Path, mode_pkg: str = "mode_coding", stem: str = "code_writer"
) -> Path:
    """用户副本文件路径（<USER_ROOT>/plugins/modes/<pkg>/agents/<stem>.yaml）。"""
    return (
        tmp_path / "userroot" / "plugins" / "modes" / mode_pkg / "agents" / f"{stem}.yaml"
    )


def _mode_entries(seed: Path, key: str = "mode_coding/code_writer") -> list[dict[str, Any]]:
    return [{"key": key, "plugin_id": "mode_coding", "path": str(seed)}]


def _file_etag(path: Path) -> str:
    """磁盘原文 sha256（read_text 口径——compute_etag 以解码后文本编码，Windows
    write_text 落盘 \r\n 会被 read_text 还原为 \n，与 read_bytes 口径不同）。"""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()


def _seed_etag(server: Any, tmp_path: Path) -> str:
    """经 GET 取出厂种子的只读回落 etag（首写乐观锁基线的真实取法）。"""
    status, payload = server.get_agent_config("mode_coding/code_writer")
    assert status == 200 and payload["source"] == "factory_seed"
    return payload["etag"]


def _b64_token(username: str, exp: int | None = None) -> str:
    payload = f"access:u-1:{username}:{exp or 4102444800}"
    return base64.b64encode(payload.encode()).decode().rstrip("=")


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _run_handle(
    server: Any,
    method: str,
    path: str,
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        server.http_handle(
            path=path, method=method, raw_body=raw_body, headers=headers, query=query
        )
    )


# ══ 1. schema 取数（真实本地 HTTP）══


class _SchemaHandler(http.server.BaseHTTPRequestHandler):
    """可编程 /api/v1/schema 假内核：按 path 返回预设 JSON。"""

    payload: bytes = b"{}"

    def do_GET(self) -> None:  # noqa: N802（BaseHTTPRequestHandler 命名）
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: Any) -> None:
        pass


def _start_schema_server(payload: dict[str, Any]) -> tuple[Any, int]:
    handler = type("Handler", (_SchemaHandler,), {"payload": json.dumps(payload).encode()})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class TestSchemaFetch:
    def test_fetch_parses_kernel_schema_entries(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "mode_agents": [
                {"key": "mode_coding/code_writer", "plugin_id": "mode_coding", "path": "x.yaml"},
                "junk-non-dict-entry",  # 结构漂移防御：非 dict 条目被滤除
            ]
        }
        srv, port = _start_schema_server(payload)
        try:
            monkeypatch.setenv("AGENTOS_KERNEL_PORT", str(port))
            entries, error = server.fetch_mode_agents("tok")
        finally:
            srv.shutdown()
        assert error is None
        assert entries == [
            {"key": "mode_coding/code_writer", "plugin_id": "mode_coding", "path": "x.yaml"}
        ]

    def test_fetch_missing_field_is_empty_registry_not_degraded(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        srv, port = _start_schema_server({"agents": []})
        try:
            monkeypatch.setenv("AGENTOS_KERNEL_PORT", str(port))
            entries, error = server.fetch_mode_agents("tok")
        finally:
            srv.shutdown()
        assert (entries, error) == ([], None), "旧内核 additive 缺席 ≠ 降级"

    def test_fetch_connection_refused_reports_degradation(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", str(closed_port))
        entries, error = server.fetch_mode_agents("tok")
        assert entries == []
        assert error is not None and "schema fetch failed" in error

    def test_fetch_without_token_skips_request(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 端口 1 无监听：若误发请求必以 "schema fetch failed" 失败，据此断言未发。
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "1")
        entries, error = server.fetch_mode_agents(None)
        assert entries == []
        assert error is not None and "no caller token" in error

    def test_kernel_base_url_defaults_to_9100(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTOS_KERNEL_PORT", raising=False)
        assert server._kernel_base_url() == "http://localhost:9100"
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9200")
        assert server._kernel_base_url() == "http://localhost:9200"


# ══ 2. 读面聚合 ══


class TestListAggregation:
    def test_list_aggregates_two_sources_with_source_labels(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        monkeypatch.setattr(
            server, "fetch_mode_agents", lambda token: (_mode_entries(seed), None)
        )
        out = server.aggregate_agents(token="tok")
        assert out["mode_registry"] == {"available": True, "error": None}
        assert out["total"] == 2
        by_id = {i["id"]: i for i in out["items"]}
        assert by_id["agentos"]["source"] == "system"
        mode_item = by_id["mode_coding/code_writer"]
        assert mode_item["source"] == "mode:mode_coding"
        assert mode_item["config_id"] == "code_writer"
        assert mode_item["name"] == "代码编写"
        assert mode_item["agent_type"] == "specialized"
        assert mode_item["level"] == "L2"

    def test_mode_user_copy_content_wins_in_list(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        copy = _user_copy(tmp_path)
        copy.parent.mkdir(parents=True)
        copy.write_text("config_id: code_writer\nname: 用户改名版\n", encoding="utf-8")
        monkeypatch.setattr(
            server, "fetch_mode_agents", lambda token: (_mode_entries(seed), None)
        )
        out = server.aggregate_agents(token="tok")
        mode_item = next(i for i in out["items"] if i["id"] == "mode_coding/code_writer")
        assert mode_item["name"] == "用户改名版", "列表读数应用户副本优先于出厂种子"

    def test_degrades_to_system_only_with_note_when_schema_unavailable(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        monkeypatch.setattr(
            server,
            "fetch_mode_agents",
            lambda token: ([], "schema fetch failed: connection refused"),
        )
        out = server.aggregate_agents(token="tok")
        assert out["total"] == 1, "降级仅系统表"
        assert {i["id"] for i in out["items"]} == {"agentos"}
        assert out["mode_registry"]["available"] is False
        assert "schema fetch failed" in out["mode_registry"]["error"]

    def test_service_list_without_token_notes_degradation(self, server: Any) -> None:
        out = asyncio.run(server.agent_list(agent_type=""))
        assert out["total"] == 1
        assert out["mode_registry"]["available"] is False
        assert "no caller token" in out["mode_registry"]["error"]

    def test_http_list_passes_caller_token_to_schema(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str | None] = []

        def fake_fetch(token: str | None) -> tuple[list[dict[str, Any]], str | None]:
            seen.append(token)
            return [], None

        monkeypatch.setattr(server, "fetch_mode_agents", fake_fetch)
        _run_handle(
            server, "GET", "/ext/agent_manager/agents",
            headers={"Authorization": f"Bearer {_b64_token('admin')}"},
        )
        _run_handle(server, "GET", "/ext/agent_manager/agents")
        assert seen[0] is not None and len(seen[0]) > 10, "带 caller 头时应透传其 bearer token"
        assert seen[1] is None, "无头调用 token 为 None（降级路径）"

    def test_filters_apply_to_mode_items(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        monkeypatch.setattr(
            server, "fetch_mode_agents", lambda token: (_mode_entries(seed), None)
        )
        by_type = server.aggregate_agents(agent_type="main", token="tok")
        assert {i["id"] for i in by_type["items"]} == {"agentos"}
        by_type_mode = server.aggregate_agents(agent_type="specialized", token="tok")
        assert {i["id"] for i in by_type_mode["items"]} == {"mode_coding/code_writer"}
        by_search = server.aggregate_agents(search="代码编写", token="tok")
        assert {i["id"] for i in by_search["items"]} == {"mode_coding/code_writer"}
        no_hit = server.aggregate_agents(search="zzz-nonexistent", token="tok")
        assert no_hit["items"] == [] and no_hit["total"] == 0

    def test_malformed_registry_entries_are_skipped(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_factory_agent(tmp_path)
        entries = [
            {"plugin_id": "mode_coding", "path": "x"},  # 缺 key
            {"key": "bad-key-no-slash", "plugin_id": "m", "path": "x"},  # 键形非法
            {"key": "mode_coding/code_writer"},  # 缺 plugin_id
            {"key": "mode_coding/ghost", "plugin_id": "mode_coding"},  # 文件不存在
            {"key": "mode_coding/code_writer", "plugin_id": "mode_coding",
             "path": str(tmp_path / "factory_modes" / "mode_coding" / "agents" / "broken.yaml")},
        ]
        (tmp_path / "factory_modes" / "mode_coding" / "agents" / "broken.yaml").write_text(
            'a: "未闭合\n\tb: 1\n', encoding="utf-8"
        )
        monkeypatch.setattr(server, "fetch_mode_agents", lambda token: (entries, None))
        out = server.aggregate_agents(token="tok")
        assert [i["id"] for i in out["items"] if i["source"] != "system"] == []

    @pytest.mark.parametrize(
        ("bad_yaml", "label"),
        [
            ("- a\n- b\n", "top-level-list"),
            ("just-a-string\n", "top-level-scalar"),
        ],
    )
    def test_mode_entry_with_non_dict_yaml_is_skipped(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        bad_yaml: str, label: str,
    ) -> None:
        """合法 YAML 但顶层非 dict 的模式条目被跳过（靶行 server.py 370），
        其余正常条目照常收录、聚合不报错。

        yaml 解析成功（非 YAMLError）→ 走 `isinstance(parsed, dict)` 守卫：
        该文件不产生条目，同批正常条目不受影响。
        """
        good = _seed_factory_agent(tmp_path)
        bad = tmp_path / "factory_modes" / "mode_coding" / "agents" / "weird.yaml"
        bad.write_text(bad_yaml, encoding="utf-8")
        entries = [
            {"key": "mode_coding/code_writer", "plugin_id": "mode_coding", "path": str(good)},
            {"key": "mode_coding/weird", "plugin_id": "mode_coding", "path": str(bad)},
        ]
        monkeypatch.setattr(server, "fetch_mode_agents", lambda token: (entries, None))

        out = server.aggregate_agents(token="tok")

        mode_ids = [i["id"] for i in out["items"] if i["source"] == "mode:mode_coding"]
        assert mode_ids == ["mode_coding/code_writer"], (
            f"顶层非 dict（{label}）的条目必须被静默跳过，正常条目照常收录"
        )
        assert out["mode_registry"] == {"available": True, "error": None}


# ══ 3. get 双面 ══


class TestGetModeFace:
    def test_get_user_copy_is_writable(self, server: Any, tmp_path: Path) -> None:
        copy = _user_copy(tmp_path)
        copy.parent.mkdir(parents=True)
        copy.write_text("config_id: code_writer\nname: 副本版\napi_key: sk-copy-secret\n", encoding="utf-8")
        status, payload = server.get_agent_config("mode_coding/code_writer")
        assert status == 200
        assert payload["source"] == "user_copy"
        assert payload["readonly"] is False
        assert "sk-copy-secret" not in payload["yaml"] and "****" in payload["yaml"]
        assert payload["etag"] == _file_etag(copy)

    def test_get_falls_back_to_factory_seed_readonly(
        self, server: Any, tmp_path: Path
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        status, payload = server.get_agent_config("mode_coding/code_writer")
        assert status == 200
        assert payload["source"] == "factory_seed"
        assert payload["readonly"] is True
        assert payload["etag"] == _file_etag(seed)
        assert "代码编写" in payload["yaml"]

    @pytest.mark.parametrize(
        "bad_key", ["mode_coding/ghost", "noslash", "mode_coding/", "mode_A/x", "mode_coding/a/b"]
    )
    def test_get_unknown_or_malformed_mode_key_404(
        self, server: Any, tmp_path: Path, bad_key: str
    ) -> None:
        _seed_factory_agent(tmp_path)
        status, payload = server.get_agent_config(bad_key)
        assert status == 404
        assert "not found" in payload["error"]

    def test_service_agent_get_resolves_mode_key(
        self, server: Any, tmp_path: Path
    ) -> None:
        _seed_factory_agent(tmp_path)
        out = asyncio.run(server.agent_get(agent_id="mode_coding/code_writer"))
        assert out["found"] is True
        assert out["config"]["name"] == "代码编写"
        miss = asyncio.run(server.agent_get(agent_id="mode_coding/ghost"))
        assert miss == {"found": False, "config": None}


# ══ 4. put 双面（用户副本写面）══


class TestPutModeFace:
    def test_first_write_creates_user_copy_with_seed_etag_baseline(
        self, server: Any, tmp_path: Path
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        etag = _seed_etag(server, tmp_path)
        new_yaml = "config_id: code_writer\nname: 用户定制版\nlevel: L2\n"
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": new_yaml, "if_match": etag}
        )
        assert status == 200, payload
        assert payload["success"] is True
        assert payload["backup"] is None, "首写建副本无备份"
        assert payload["source"] == "user_copy" and payload["readonly"] is False
        copy = _user_copy(tmp_path)
        assert copy.read_text(encoding="utf-8") == new_yaml
        assert seed.read_text(encoding="utf-8") == _SEED_YAML, "出厂种子必须原样保留"
        assert payload["etag"] == hashlib.sha256(new_yaml.encode()).hexdigest()
        # GET 翻转为可写副本
        status2, payload2 = server.get_agent_config("mode_coding/code_writer")
        assert status2 == 200 and payload2["source"] == "user_copy"

    def test_second_write_backs_up_copy(self, server: Any, tmp_path: Path) -> None:
        _seed_factory_agent(tmp_path)
        etag = _seed_etag(server, tmp_path)
        v2 = "config_id: code_writer\nname: v2\n"
        status, _ = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": v2, "if_match": etag}
        )
        assert status == 200
        etag2 = hashlib.sha256(v2.encode()).hexdigest()
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\nname: v3\n", "if_match": etag2}
        )
        assert status == 200
        assert payload["backup"] == "code_writer.yaml.bak"
        bak = _user_copy(tmp_path).with_suffix(".yaml.bak")
        assert bak.read_text(encoding="utf-8") == v2, "备份内容为副本上一版"

    def test_put_stale_etag_409_and_invalid_yaml_400(
        self, server: Any, tmp_path: Path
    ) -> None:
        _seed_factory_agent(tmp_path)
        key = "mode_coding/code_writer"
        status, payload = server.put_agent_config(key, {"yaml": "config_id: x\n"})
        assert status == 409 and "ETag mismatch" in payload["error"]
        etag = _seed_etag(server, tmp_path)
        broken = 'config_id: code_writer\nname: "未闭合\n\tb: 1\n'
        status, payload = server.put_agent_config(key, {"yaml": broken, "if_match": etag})
        assert status == 400 and "invalid" in payload["error"]
        assert not _user_copy(tmp_path).exists(), "400 拒写不得落副本"

    def test_put_unknown_mode_agent_404(self, server: Any, tmp_path: Path) -> None:
        _seed_factory_agent(tmp_path)
        status, payload = server.put_agent_config(
            "mode_coding/ghost", {"yaml": "config_id: ghost\n", "if_match": "x"}
        )
        assert status == 404

    def test_put_write_target_escape_fail_closed(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解析器回归防御：写目标逃出模式包用户副本 agents/ 必须拒绝。"""
        _seed_factory_agent(tmp_path)
        etag = _seed_etag(server, tmp_path)
        escaping = tmp_path / "userroot" / "plugins" / "evil.yaml"
        monkeypatch.setattr(
            server, "_mode_user_copy_path",
            lambda mode, stem: (
                tmp_path / "userroot" / "plugins" / "modes" / "mode_coding"
                / "agents" / ".." / ".." / f"{stem}.yaml"
            ),
        )
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\n", "if_match": etag}
        )
        assert status == 400
        assert "escapes mode user copy dir" in payload["error"]
        assert not escaping.exists(), "越界目标不得落盘"

    def test_put_user_root_unavailable_500(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_factory_agent(tmp_path)
        etag = _seed_etag(server, tmp_path)
        monkeypatch.setattr(server, "_user_plugins_dir", lambda: None)
        monkeypatch.setattr(server, "_user_root", lambda: None)
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: x\n", "if_match": etag}
        )
        assert status == 500
        assert "user root unavailable" in payload["error"]
        assert not _user_copy(tmp_path).exists()

    def test_put_unreadable_seed_500_precedes_etag(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """首写基线（出厂种子）不可读 → 500，先于 etag 判定。"""
        seed = _seed_factory_agent(tmp_path)
        real_read = Path.read_text

        def fake_read(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == seed:
                raise PermissionError(13, "denied")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read)
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: x\n", "if_match": "stale"}
        )
        assert status == 500
        assert "read agent config" in payload["error"]
        assert not _user_copy(tmp_path).exists()

    def test_put_atomic_write_failure_500_disk_untouched(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        etag = _seed_etag(server, tmp_path)

        def boom(*args: Any, **kwargs: Any) -> None:
            raise OSError(28, "disk full")

        monkeypatch.setattr(server, "_atomic_write_text", boom)
        status, payload = server.put_agent_config(
            "mode_coding/code_writer",
            {"yaml": "config_id: code_writer\nname: v2\n", "if_match": etag},
        )
        assert status == 500
        assert "write agent config" in payload["error"]
        assert not _user_copy(tmp_path).exists(), "原子写失败不得留下半成品副本"
        assert seed.read_text(encoding="utf-8") == _SEED_YAML


# ══ 5. git 提交尝试（有仓 / 无仓 / 坏仓）══


class TestGitCommit:
    def _git(self, *args: str, cwd: Path) -> str:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def test_put_commits_into_user_repo(self, server: Any, tmp_path: Path) -> None:
        _seed_factory_agent(tmp_path)
        userroot = tmp_path / "userroot"
        userroot.mkdir(parents=True)
        (userroot / ".git").mkdir()  # git init 对已存在空 .git 目录直接复用
        self._git("init", "-q", cwd=userroot)
        self._git("config", "user.name", "测试用户", cwd=userroot)
        self._git("config", "user.email", "test@agentos.dev", cwd=userroot)
        etag = _seed_etag(server, tmp_path)
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\nname: 入仓版\n", "if_match": etag}
        )
        assert status == 200, payload
        subject = self._git("log", "-1", "--pretty=%s", cwd=userroot)
        assert "mode_coding/code_writer" in subject
        assert self._git("status", "--porcelain", cwd=userroot) == "", "副本文件应已提交干净"

    def test_put_without_repo_skips_git(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_factory_agent(tmp_path)
        calls: list[list[str]] = []

        def spy(cmd: list[str], **kwargs: Any) -> Any:
            calls.append(cmd)
            raise AssertionError("无 .git 时不得调用 git")

        monkeypatch.setattr(server.subprocess, "run", spy)
        etag = _seed_etag(server, tmp_path)
        status, _ = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\n", "if_match": etag}
        )
        assert status == 200
        assert calls == []
        assert _user_copy(tmp_path).exists()

    def test_put_broken_repo_does_not_block_write(
        self, server: Any, tmp_path: Path
    ) -> None:
        _seed_factory_agent(tmp_path)
        userroot = tmp_path / "userroot"
        (userroot / ".git").mkdir(parents=True)  # 空目录：.git 在场但非仓库
        etag = _seed_etag(server, tmp_path)
        status, payload = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\nname: v2\n", "if_match": etag}
        )
        assert status == 200, "git 失败不得阻断写面"
        assert _user_copy(tmp_path).read_text(encoding="utf-8").endswith("v2\n")

    def test_put_git_subprocess_crash_does_not_block_write(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_factory_agent(tmp_path)
        userroot = tmp_path / "userroot"
        userroot.mkdir(parents=True)
        (userroot / ".git").mkdir()

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError(13, "git not executable")

        monkeypatch.setattr(server.subprocess, "run", boom)
        etag = _seed_etag(server, tmp_path)
        status, _ = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\n", "if_match": etag}
        )
        assert status == 200
        assert _user_copy(tmp_path).exists()

    def test_put_copy_outside_user_root_skips_commit(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENTOS_USER_PLUGINS_DIR 独立指向用户根之外 → 副本不入仓，跳过提交。"""
        _seed_factory_agent(tmp_path)
        other_root = tmp_path / "otherroot"
        monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(other_root / "plugins"))
        userroot = tmp_path / "userroot"
        userroot.mkdir(parents=True)
        (userroot / ".git").mkdir()
        self._git("init", "-q", cwd=userroot)
        self._git("config", "user.name", "测试用户", cwd=userroot)
        self._git("config", "user.email", "test@agentos.dev", cwd=userroot)
        etag = _seed_etag(server, tmp_path)
        status, _ = server.put_agent_config(
            "mode_coding/code_writer", {"yaml": "config_id: code_writer\n", "if_match": etag}
        )
        assert status == 200
        copy = other_root / "plugins" / "modes" / "mode_coding" / "agents" / "code_writer.yaml"
        assert copy.exists(), "副本写往独立插件根"
        head = subprocess.run(
            ["git", "-C", str(userroot), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True,
        )
        assert head.returncode != 0, "副本不在用户根内：不得产生提交"


# ══ 6. http.handle 模式路由分发 ══


class TestHttpModeRoutes:
    def test_get_mode_config_route(self, server: Any, tmp_path: Path) -> None:
        _seed_factory_agent(tmp_path)
        status, body = _decode_http(
            _run_handle(server, "GET", "/ext/agent_manager/agents/mode_coding/code_writer/config")
        )
        assert status == 200
        assert body["readonly"] is True
        assert body["source"] == "factory_seed"

    def test_put_mode_route_is_user_gated_not_admin(
        self, server: Any, tmp_path: Path
    ) -> None:
        seed = _seed_factory_agent(tmp_path)
        etag = _file_etag(seed)
        body = base64.b64encode(
            json.dumps({"yaml": "config_id: code_writer\nname: 用户改\n", "if_match": etag}).encode()
        ).decode()
        status, payload = _decode_http(
            _run_handle(
                server, "PUT", "/ext/agent_manager/agents/mode_coding/code_writer/config",
                raw_body=body, headers={"Authorization": f"Bearer {_b64_token('user1')}"},
            )
        )
        assert status == 200, "模式面用户门控：非 admin 已认证用户可写用户副本"
        assert payload["success"] is True

    def test_put_mode_route_invalid_body_400(
        self, server: Any, tmp_path: Path
    ) -> None:
        _seed_factory_agent(tmp_path)
        status, payload = _decode_http(
            _run_handle(
                server, "PUT", "/ext/agent_manager/agents/mode_coding/code_writer/config",
                raw_body="not json {{{",
                headers={"Authorization": f"Bearer {_b64_token('user1')}"},
            )
        )
        assert status == 400
        assert "invalid JSON body" in payload["error"]

    def test_system_route_admin_gate_unchanged(
        self, server: Any, tmp_path: Path
    ) -> None:
        status, body = _decode_http(
            _run_handle(
                server, "PUT", "/ext/agent_manager/agents/agentos/config",
                raw_body=json.dumps({"yaml": "config_id: agentos\n", "if_match": "x"}),
                headers={"Authorization": f"Bearer {_b64_token('user1')}"},
            )
        )
        assert status == 403, "系统面 admin 门控不变（插件自持检查）"
        assert "admin role required" in body["error"]

    def test_system_config_route_still_works(self, server: Any) -> None:
        status, body = _decode_http(
            _run_handle(server, "GET", "/ext/agent_manager/agents/agentos/config")
        )
        assert status == 200
        assert body["source"] == "system"
        assert body["readonly"] is False
