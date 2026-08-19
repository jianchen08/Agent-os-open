# @feature: FP-0.2.二 agent_manager 插件服务 | @vision: V1 可进化 | @ci: none-local
"""agent_manager 服务端测试——原内核 /api/v1/agents* 4 路由行为契约的承接验证。

覆盖（对齐 kernel/crates/api/tests/agent_config_endpoint_test.rs 语义 + 方案 §五）：
1. 列表：扫描 config/agents/**/*.yaml、agent_type 过滤、{items,total}、空 config_id 跳过
2. schema：12 字段声明（string/textarea/number/select/multiselect 全覆盖）
3. 读：yaml 掩码（明文 → ****、${ENV} 保留）、etag=磁盘原文 sha256、404、顶层文件、
   config_id 两轮匹配（文件名 ≠ config_id）
4. 路径穿越防护：../、%2F、非法字符 id 一律 404
5. 写：If-Match 409（缺失/不匹配）、语法非法 400 磁盘不变、.bak 备份内容=原文件、
   round-trip、新 etag
6. PUT admin 闸：无 token 401 / 非 admin 403 / admin 200
7. 服务面：agent.get / agent.list / agent.config-validate
8. http.handle 分发：4 路由 + 未知 path 404

唯一外部依赖是临时 config 目录（AGENTOS_CONFIG_ROOT 指向），不接真实内核。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "agent_manager_server_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_manager_server_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """临时项目根（AGENTOS_CONFIG_ROOT=<tmp>/config）+ 常驻两个 agent。"""
    agents_main = tmp_path / "config" / "agents" / "main"
    agents_exec = tmp_path / "config" / "agents" / "executor"
    for d in (tmp_path / "config" / "agents", agents_main, agents_exec):
        d.mkdir(parents=True, exist_ok=True)
    (agents_main / "agentos.yaml").write_text(
        "config_id: agentos\nname: 灵汐\nagent_type: main\nlevel: L1\nmodel_tier: large\n",
        encoding="utf-8",
    )
    # 文件名 general_agent.yaml，config_id=general_agent_agent（两轮匹配常态）
    (agents_exec / "general_agent.yaml").write_text(
        "config_id: general_agent_agent\nname: 通用执行\ndescription: 执行任务\n"
        "agent_type: specialized\nlevel: L3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    return _load_module()


def _b64_token(username: str, user_id: str = "u-123", exp: int | None = None) -> str:
    """构造内核 0.2 开发期 token（base64_nopad("access:{user_id}:{username}:{exp}")）。"""
    payload = f"access:{user_id}:{username}:{exp or int(time.time()) + 3600}"
    return base64.b64encode(payload.encode()).decode().rstrip("=")


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


# ══ 1. 列表 ══


class TestList:
    def test_list_returns_items_with_kernel_shape(self, server: Any) -> None:
        out = server.list_agents()
        assert out["total"] == 2
        ids = {i["id"] for i in out["items"]}
        # 文件名 ≠ config_id 时以 config_id 为准（general_agent.yaml → general_agent_agent）
        assert ids == {"agentos", "general_agent_agent"}
        item = next(i for i in out["items"] if i["id"] == "agentos")
        assert item["config_id"] == "agentos"
        assert item["name"] == "灵汐"
        assert item["agent_type"] == "main"
        assert item["status"] == "active"
        assert item["model"] == "large"
        assert item["level"] == "L1"

    def test_list_filters_by_agent_type(self, server: Any) -> None:
        out = server.list_agents("main")
        assert out["total"] == 1
        assert out["items"][0]["id"] == "agentos"

    def test_list_skips_empty_config_id(self, server: Any, tmp_path: Path) -> None:
        (tmp_path / "config" / "agents" / "main" / "no_id.yaml").write_text(
            "name: 无ID\n", encoding="utf-8"
        )
        out = server.list_agents()
        assert all(i["id"] for i in out["items"])
        assert out["total"] == 2


# ══ 2. schema ══


class TestSchema:
    def test_schema_fields_match_kernel_declaration(self, server: Any) -> None:
        status, body = _decode_http(
            _run_handle(server, "GET", "/ext/agent_manager/agents/schema")
        )
        assert status == 200
        fields = body["fields"]
        names = [f["name"] for f in fields]
        assert names == [
            "config_id", "name", "display_name", "description", "agent_type",
            "level", "model_tier", "system_prompt", "tool_ids",
            "max_iterations", "timeout_seconds", "tags",
        ]
        types = {f["type"] for f in fields}
        assert {"string", "textarea", "number", "select", "multiselect"} <= types
        type_field = next(f for f in fields if f["name"] == "agent_type")
        assert {o["value"] for o in type_field["options"]} == {
            "main", "orchestrator", "specialized", "atomic", "system",
        }


# ══ 3. 读（掩码 / etag / 404 / 两轮匹配）══


class TestGetConfig:
    def test_get_returns_masked_yaml_and_raw_etag(self, server: Any, tmp_path: Path) -> None:
        raw = (
            "config_id: agentos\nname: 灵汐\napi_key: sk-real-secret-123\n"
            "token: ${SOME_TOKEN}\npassword: plainpw\nnested:\n  secret_field: inner-secret\n"
            "keep: value\n"
        )
        (tmp_path / "config" / "agents" / "main" / "agentos.yaml").write_text(
            raw, encoding="utf-8"
        )
        status, payload = server.get_agent_config("agentos")
        assert status == 200
        assert payload["config_id"] == "agentos"
        # etag = 磁盘原文 sha256（掩码前）
        assert payload["etag"] == hashlib.sha256(raw.encode()).hexdigest()
        y = payload["yaml"]
        assert "sk-real-secret-123" not in y, "明文 secret 不得泄漏"
        assert "plainpw" not in y
        assert "inner-secret" not in y
        assert "****" in y
        assert "${SOME_TOKEN}" in y, "ENV 占位符应原样保留"
        assert "value" in y, "非敏感字段原样"

    def test_get_missing_returns_404(self, server: Any) -> None:
        status, payload = server.get_agent_config("ghost_agent")
        assert status == 404
        assert "not found" in payload["error"]

    def test_get_top_level_file(self, server: Any, tmp_path: Path) -> None:
        (tmp_path / "config" / "agents" / "top_level.yaml").write_text(
            "config_id: top_level\nname: 顶层\n", encoding="utf-8"
        )
        status, _ = server.get_agent_config("top_level")
        assert status == 200

    def test_get_by_config_id_second_round(self, server: Any) -> None:
        # general_agent.yaml 内 config_id=general_agent_agent → 按 config_id 命中
        status, payload = server.get_agent_config("general_agent_agent")
        assert status == 200
        assert "通用执行" in payload["yaml"]

    @pytest.mark.parametrize("bad_id", ["../agentos", "a/b", "a%2Fb", "a\\b", "..", "a b", ""])
    def test_unsafe_agent_id_rejected(self, server: Any, bad_id: str) -> None:
        assert server.resolve_agent_yaml_path(bad_id) is None
        status, _ = server.get_agent_config(bad_id)
        assert status == 404


# ══ 4. 写（409 / 400 / .bak / round-trip）══


class TestPutConfig:
    def _get_etag(self, server: Any, agent_id: str) -> str:
        status, payload = server.get_agent_config(agent_id)
        assert status == 200
        return payload["etag"]

    def test_put_round_trip_with_backup(self, server: Any, tmp_path: Path) -> None:
        original = (tmp_path / "config" / "agents" / "main" / "agentos.yaml").read_text(
            encoding="utf-8"
        )
        etag = self._get_etag(server, "agentos")
        new_yaml = "config_id: agentos\nname: 灵汐v2\nlevel: L1\n"
        status, payload = server.put_agent_config(
            "agentos", {"yaml": new_yaml, "if_match": etag}
        )
        assert status == 200, payload
        assert payload["success"] is True
        assert payload["backup"] == "agentos.yaml.bak"
        assert payload["etag"] == hashlib.sha256(new_yaml.encode()).hexdigest()
        # 磁盘已更新 + 备份内容为原文件
        assert (
            tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        ).read_text(encoding="utf-8") == new_yaml
        assert (
            tmp_path / "config" / "agents" / "main" / "agentos.yaml.bak"
        ).read_text(encoding="utf-8") == original
        # GET 读回新内容 + 新 etag
        status2, payload2 = server.get_agent_config("agentos")
        assert status2 == 200
        assert "灵汐v2" in payload2["yaml"]

    def test_put_missing_if_match_returns_409(self, server: Any) -> None:
        status, payload = server.put_agent_config("agentos", {"yaml": "config_id: agentos\n"})
        assert status == 409
        assert "ETag mismatch" in payload["error"]

    def test_put_stale_if_match_returns_409(self, server: Any) -> None:
        status, payload = server.put_agent_config(
            "agentos", {"yaml": "config_id: agentos\n", "if_match": "stale-etag"}
        )
        assert status == 409

    def test_put_invalid_yaml_400_keeps_disk(self, server: Any, tmp_path: Path) -> None:
        path = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = path.read_text(encoding="utf-8")
        etag = self._get_etag(server, "agentos")
        broken = 'config_id: agentos\nname: "未闭合\n\tbad: tab\n'
        status, payload = server.put_agent_config(
            "agentos", {"yaml": broken, "if_match": etag}
        )
        assert status == 400
        assert "invalid" in payload["error"]
        assert path.read_text(encoding="utf-8") == original, "400 时磁盘应保持原值"
        assert not (tmp_path / "config" / "agents" / "main" / "agentos.yaml.bak").exists()

    def test_put_missing_agent_404(self, server: Any) -> None:
        status, _ = server.put_agent_config(
            "ghost", {"yaml": "config_id: ghost\n", "if_match": "x"}
        )
        assert status == 404

    def test_put_missing_yaml_field_400(self, server: Any) -> None:
        status, _ = server.put_agent_config("agentos", {"if_match": "x"})
        assert status == 400


# ══ 5. PUT admin 闸（http.handle 层）══


class TestPutAuth:
    def _put_via_http(
        self, server: Any, yaml_text: str, etag: str, headers: dict[str, str] | None
    ) -> tuple[int, Any]:
        body = base64.b64encode(
            json.dumps({"yaml": yaml_text, "if_match": etag}).encode()
        ).decode()
        result = _run_handle(
            server,
            "PUT",
            "/ext/agent_manager/agents/agentos/config",
            raw_body=body,
            headers=headers,
        )
        return _decode_http(result)

    def test_put_without_token_401(self, server: Any) -> None:
        _, payload = server.get_agent_config("agentos")
        status, body = self._put_via_http(
            server, "config_id: agentos\nname: n\n", payload["etag"], headers=None
        )
        assert status == 401
        assert (server._agents_dir() / "main" / "agentos.yaml").read_text(
            encoding="utf-8"
        ).startswith("config_id: agentos\nname:")

    def test_put_non_admin_403(self, server: Any) -> None:
        _, payload = server.get_agent_config("agentos")
        status, _ = self._put_via_http(
            server,
            "config_id: agentos\n",
            payload["etag"],
            headers={"authorization": f"Bearer {_b64_token('user1')}"},
        )
        assert status == 403

    def test_put_admin_200(self, server: Any) -> None:
        _, payload = server.get_agent_config("agentos")
        status, body = self._put_via_http(
            server,
            "config_id: agentos\nname: 管理员改\n",
            payload["etag"],
            headers={"Authorization": f"Bearer {_b64_token('admin')}"},
        )
        assert status == 200, body
        assert body["success"] is True

    def test_put_expired_token_401(self, server: Any) -> None:
        _, payload = server.get_agent_config("agentos")
        expired = _b64_token("admin", exp=int(time.time()) - 10)
        status, _ = self._put_via_http(
            server,
            "config_id: agentos\n",
            payload["etag"],
            headers={"authorization": f"Bearer {expired}"},
        )
        assert status == 401


# ══ 6. 服务面 ══


class TestServices:
    @pytest.mark.asyncio
    async def test_agent_get_found(self, server: Any) -> None:
        out = await server.agent_get(agent_id="general_agent_agent")
        assert out["found"] is True
        assert out["config"]["config_id"] == "general_agent_agent"
        assert out["config"]["level"] == "L3"  # task_submit 级别校验消费的字段
        assert out["config"].get("is_active", True) is True

    @pytest.mark.asyncio
    async def test_agent_get_not_found(self, server: Any) -> None:
        out = await server.agent_get(agent_id="ghost")
        assert out["found"] is False
        assert out["config"] is None

    @pytest.mark.asyncio
    async def test_agent_list_service(self, server: Any) -> None:
        out = await server.agent_list(agent_type="main")
        assert out["total"] == 1
        assert out["items"][0]["id"] == "agentos"

    @pytest.mark.asyncio
    async def test_agent_config_validate(self, server: Any) -> None:
        ok = await server.agent_config_validate(yaml="config_id: x\nname: y\n")
        assert ok == {"valid": True, "error": None}
        bad = await server.agent_config_validate(yaml='a: "未闭合\n\tb: tab\n')
        assert bad["valid"] is False
        assert "invalid" in bad["error"]
        missing = await server.agent_config_validate()
        assert missing["valid"] is False


# ══ 7. http.handle 分发 ══


class TestHttpDispatch:
    def test_unknown_path_404(self, server: Any) -> None:
        status, body = _decode_http(_run_handle(server, "GET", "/ext/agent_manager/nope"))
        assert status == 404

    def test_list_route_with_query(self, server: Any) -> None:
        result = _run_handle(
            server, "GET", "/ext/agent_manager/agents", query={"agent_type": "main"}
        )
        status, body = _decode_http(result)
        assert status == 200
        assert body["total"] == 1

    def test_get_config_route(self, server: Any) -> None:
        status, body = _decode_http(
            _run_handle(server, "GET", "/ext/agent_manager/agents/agentos/config")
        )
        assert status == 200
        assert body["config_id"] == "agentos"

    def test_plain_json_body_decoded(self, server: Any) -> None:
        """raw_body 明文 JSON（非 base64）也应可解（channel_api 同款防御）。"""
        _, payload = server.get_agent_config("agentos")
        result = _run_handle(
            server,
            "PUT",
            "/ext/agent_manager/agents/agentos/config",
            raw_body=json.dumps({"yaml": "config_id: agentos\n", "if_match": payload["etag"]}),
            headers={"Authorization": f"Bearer {_b64_token('admin')}"},
        )
        status, body = _decode_http(result)
        assert status == 200


def _run_handle(
    server: Any,
    method: str,
    path: str,
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """同步驱动 async http.handle（测试全部为同步用例）。"""
    import asyncio

    return asyncio.run(
        server.http_handle(
            path=path, method=method, raw_body=raw_body, headers=headers, query=query
        )
    )
