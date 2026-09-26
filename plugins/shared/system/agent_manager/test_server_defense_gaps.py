# @feature: FP-0.2.二 agent_manager 插件服务 | @vision: V1 可进化 | @ci: none-local
"""agent_manager server.py 容错/防御分支补测（test_server.py 未触及的缺行定向覆盖）。

覆盖面（2026-09-13 coverage.xml 缺行）：
1. 目录定位：AGENTOS_CONFIG_ROOT 缺失 → 回退仓库 config/agents
2. 扫描防御：非目录 → 空表；坏 yaml/不可读文件在两轮匹配与列表扫描中被跳过
3. 掩码边界：非字符串 secret 值原样保留、list 递归掩码
4. 读面 500：文件不可读（OSError）、yaml 损坏、空文件（→ 空配置）
5. 写面 500：读当前文件失败（先于 etag 判定）、备份失败/原子写失败磁盘保持原值
6. 鉴权：garbled token（解码失败）一律 401；PUT body 非法 JSON → 400
7. http.handle 兜底：非 UTF-8 配置文件（UnicodeDecodeError 逃出 OSError 捕获）
   → 错误信封（success=false, 500）
8. 服务面：agent_get 读失败按 not-found 同形、非 dict yaml → 空配置
9. 生命周期：_on_load 钩子可启动

mock 仅用于文件系统不可达错误（PermissionError 注入），其余一律真实临时目录。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "agent_manager_server_defense_gaps"

# 模块导入时快照真实读取（测试内验证磁盘状态不受注入影响）
_REAL_READ_TEXT = Path.read_text


def _load_module() -> Any:
    """动态加载 server.py（独立模块名，避免与 test_server.py 的装载互相覆盖）。"""
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, str(_PLUGIN_DIR / "server.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """临时项目根（AGENTOS_CONFIG_ROOT=<tmp>/config）+ 两个 agent（含 config_id≠文件名样本）。"""
    agents_main = tmp_path / "config" / "agents" / "main"
    agents_exec = tmp_path / "config" / "agents" / "executor"
    for d in (tmp_path / "config" / "agents", agents_main, agents_exec):
        d.mkdir(parents=True, exist_ok=True)
    (agents_main / "agentos.yaml").write_text(
        "config_id: agentos\nname: 灵汐\nagent_type: main\nlevel: L1\n",
        encoding="utf-8",
    )
    (agents_exec / "general_agent.yaml").write_text(
        "config_id: general_agent_agent\nname: 通用执行\nagent_type: specialized\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    return _load_module()


def _b64_token(username: str, user_id: str = "u-1", exp: int | None = None) -> str:
    payload = f"access:{user_id}:{username}:{exp or int(time.time()) + 3600}"
    return base64.b64encode(payload.encode()).decode().rstrip("=")


def _raw_token(payload: str) -> str:
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


def _patch_read_failure(monkeypatch: pytest.MonkeyPatch, target: Path, exc: Exception) -> None:
    """仅对单个文件注入读取失败（文件系统外部依赖 mock），其余路径走真实读取。"""
    original = _REAL_READ_TEXT

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise exc
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _write_valid_second_agent(agents: Path) -> Path:
    """config_id ≠ 文件名的合法 agent（迫使两轮匹配走 config_id 回退轮）。"""
    sub = agents / "zzz_other"
    sub.mkdir(exist_ok=True)
    valid = sub / "persona_v9.yaml"
    valid.write_text("config_id: wanted\nname: 目标\n", encoding="utf-8")
    return valid


# ══ 1. 目录定位与扫描 ══


class TestDirResolution:
    def test_agents_dir_falls_back_to_repo_config_without_env(
        self, server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
        fallback = server._agents_dir()
        assert fallback.parts[-2:] == ("config", "agents")
        assert fallback.is_dir(), "回退目标应是仓库真实 config/agents"
        # 对照：环境变量在场时以环境变量为准
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(elsewhere))
        assert server._agents_dir() == elsewhere / "agents"

    @pytest.mark.parametrize("non_dir", ["missing_dir", "plain.txt"])
    def test_collect_yaml_files_non_dir_returns_empty(
        self, server: Any, tmp_path: Path, non_dir: str
    ) -> None:
        p = tmp_path / non_dir
        if p.suffix:
            p.write_text("x", encoding="utf-8")
        assert server.collect_yaml_files(p) == []


class TestScanTolerance:
    def test_find_agent_yaml_skips_broken_sibling_still_matches_config_id(
        self, server: Any, tmp_path: Path
    ) -> None:
        agents = tmp_path / "config" / "agents"
        # 排序在前（main < zzz_other）保证坏文件先于合法文件进入回退读取
        (agents / "main" / "aaa_broken.yaml").write_text(
            'a: "未闭合\n\tb: 1\n', encoding="utf-8"
        )
        valid = _write_valid_second_agent(agents)
        found = server.find_agent_yaml(agents, "wanted")
        assert found == valid, "坏 yaml 兄弟文件不得阻断 config_id 匹配"

    def test_find_agent_yaml_skips_unreadable_sibling(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agents = tmp_path / "config" / "agents"
        unreadable = agents / "main" / "aaa_unreadable.yaml"
        unreadable.write_text("config_id: nope\n", encoding="utf-8")
        valid = _write_valid_second_agent(agents)
        _patch_read_failure(monkeypatch, unreadable, PermissionError(13, "denied"))
        assert server.find_agent_yaml(agents, "wanted") == valid

    def test_list_skips_broken_yaml(
        self, server: Any, tmp_path: Path
    ) -> None:
        (tmp_path / "config" / "agents" / "aaa_broken.yaml").write_text(
            'a: "未闭合\n\tb: 1\n', encoding="utf-8"
        )
        out = server.list_agents()
        assert {i["id"] for i in out["items"]} == {"agentos", "general_agent_agent"}

    def test_list_skips_unreadable_yaml(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "config" / "agents" / "aaa_unreadable.yaml"
        bad.write_text("config_id: x\n", encoding="utf-8")
        _patch_read_failure(monkeypatch, bad, PermissionError(13, "denied"))
        out = server.list_agents()
        assert {i["id"] for i in out["items"]} == {"agentos", "general_agent_agent"}

    @pytest.mark.parametrize("content", ["just a scalar\n", "- a\n- b\n", "\n"])
    def test_list_skips_non_dict_yaml(
        self, server: Any, tmp_path: Path, content: str
    ) -> None:
        (tmp_path / "config" / "agents" / "aaa_non_dict.yaml").write_text(
            content, encoding="utf-8"
        )
        out = server.list_agents()
        assert out["total"] == 2
        assert all(i["id"] for i in out["items"])


# ══ 2. 掩码边界 ══


class TestMaskEdges:
    @pytest.mark.parametrize("raw", [12345, ["t1", "t2"], None])
    def test_non_string_secret_value_kept_as_is(self, server: Any, raw: Any) -> None:
        out = server.mask_secrets({"api_key": raw})
        assert out == {"api_key": raw}, "非字符串 secret 值按内核语义原样保留"
        assert "****" not in str(out)

    def test_mask_secrets_recurses_into_lists(self, server: Any) -> None:
        out = server.mask_secrets(
            ["plain", {"api_key": "sk-1"}, {"nested": [{"token": "t9"}]}]
        )
        assert out[0] == "plain", "列表非敏感项原样"
        assert out[1] == {"api_key": "****"}
        assert out[2]["nested"][0] == {"token": "****"}

    def test_masked_list_fields_via_get(self, server: Any, tmp_path: Path) -> None:
        (tmp_path / "config" / "agents" / "main" / "agentos.yaml").write_text(
            "config_id: agentos\ntool_ids:\n  - t1\n  - t2\napi_key: sk-real\n",
            encoding="utf-8",
        )
        status, payload = server.get_agent_config("agentos")
        assert status == 200
        y = payload["yaml"]
        assert "t1" in y and "t2" in y, "列表内非敏感值经 GET 原样可见"
        assert "sk-real" not in y and "****" in y


# ══ 3. 读面边界 ══


class TestGetEdges:
    def test_unreadable_file_returns_500(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        _patch_read_failure(monkeypatch, target, PermissionError(13, "denied"))
        status, payload = server.get_agent_config("agentos")
        assert status == 500
        assert "read agent config" in payload["error"]

    def test_broken_yaml_returns_500(self, server: Any, tmp_path: Path) -> None:
        (tmp_path / "config" / "agents" / "main" / "agentos.yaml").write_text(
            'config_id: agentos\nname: "未闭合\n\tbad: 1\n', encoding="utf-8"
        )
        status, payload = server.get_agent_config("agentos")
        assert status == 500
        assert "yaml parse error" in payload["error"]

    @pytest.mark.parametrize("content", ["", "null\n"])
    def test_empty_yaml_becomes_empty_config(
        self, server: Any, tmp_path: Path, content: str
    ) -> None:
        (tmp_path / "config" / "agents" / "blank.yaml").write_text(content, encoding="utf-8")
        status, payload = server.get_agent_config("blank")
        assert status == 200
        assert payload["config_id"] == "blank"
        assert yaml.safe_load(payload["yaml"]) == {}, "空/_null 配置对外呈空映射"
        assert payload["etag"] == hashlib.sha256(content.encode()).hexdigest()


# ══ 4. 写面边界 ══


class TestPutEdges:
    def test_unreadable_current_file_500_precedes_etag_check(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = _REAL_READ_TEXT(target, encoding="utf-8")
        _patch_read_failure(monkeypatch, target, PermissionError(13, "denied"))
        status, payload = server.put_agent_config(
            "agentos", {"yaml": "config_id: agentos\n", "if_match": "stale-etag"}
        )
        assert status == 500, "读失败必须先于 etag 判定返回 500，而非 409"
        assert "read agent config" in payload["error"]
        assert _REAL_READ_TEXT(target, encoding="utf-8") == original, "读失败不得动磁盘"

    def test_backup_write_failure_500_and_disk_untouched(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = _REAL_READ_TEXT(target, encoding="utf-8")
        etag = hashlib.sha256(original.encode()).hexdigest()
        real_write = Path.write_text

        def fake_write(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
            if self.suffix == ".bak":
                raise PermissionError(13, "denied")
            return real_write(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fake_write)
        status, payload = server.put_agent_config(
            "agentos", {"yaml": "config_id: agentos\nname: v2\n", "if_match": etag}
        )
        assert status == 500
        assert "write agent config" in payload["error"]
        assert _REAL_READ_TEXT(target, encoding="utf-8") == original, "备份失败不得写主文件"
        assert not target.with_suffix(".yaml.bak").exists()

    def test_atomic_write_failure_500_backup_still_kept(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = _REAL_READ_TEXT(target, encoding="utf-8")
        etag = hashlib.sha256(original.encode()).hexdigest()

        def boom(*args: Any, **kwargs: Any) -> None:
            raise OSError(28, "disk full")

        monkeypatch.setattr(server, "_atomic_write_text", boom)
        status, payload = server.put_agent_config(
            "agentos", {"yaml": "config_id: agentos\nname: v2\n", "if_match": etag}
        )
        assert status == 500
        assert "write agent config" in payload["error"]
        assert _REAL_READ_TEXT(target, encoding="utf-8") == original, "原子写失败主文件保持原值"
        bak = target.with_suffix(".yaml.bak")
        assert bak.exists(), "备份先于写入完成，失败后仍在"
        assert _REAL_READ_TEXT(bak, encoding="utf-8") == original


# ══ 5. 鉴权与 body 解码（http.handle PUT 路由）══

_GARBLED_TOKENS = [
    "!!!",  # base64 清洗后为空
    _raw_token("x:y"),  # 段数不足
    _raw_token("access:u1:admin:notanum"),  # exp 非整数
]

_BAD_BODIES = [
    "not json {{{",  # 明文非法 JSON
    base64.b64encode(b"hello world, not json").decode(),  # 合法 base64 但非 JSON
]


class TestPutGateAndBody:
    def _put(self, server: Any, raw_body: str, headers: dict[str, str]) -> tuple[int, Any]:
        return _decode_http(
            _run_handle(
                server,
                "PUT",
                "/ext/agent_manager/agents/agentos/config",
                raw_body=raw_body,
                headers=headers,
            )
        )

    @pytest.mark.parametrize("token", _GARBLED_TOKENS)
    def test_garbled_token_401(
        self, server: Any, tmp_path: Path, token: str
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = _REAL_READ_TEXT(target, encoding="utf-8")
        status, body = self._put(
            server, json.dumps({"yaml": "config_id: agentos\n"}), {"Authorization": f"Bearer {token}"}
        )
        assert status == 401
        assert body["error"] == "invalid or expired token"
        assert _REAL_READ_TEXT(target, encoding="utf-8") == original

    @pytest.mark.parametrize("raw_body", _BAD_BODIES)
    def test_invalid_body_400(
        self, server: Any, tmp_path: Path, raw_body: str
    ) -> None:
        target = tmp_path / "config" / "agents" / "main" / "agentos.yaml"
        original = _REAL_READ_TEXT(target, encoding="utf-8")
        status, body = self._put(
            server, raw_body, {"Authorization": f"Bearer {_b64_token('admin')}"}
        )
        assert status == 400
        assert "invalid JSON body" in body["error"]
        assert _REAL_READ_TEXT(target, encoding="utf-8") == original


# ══ 6. http.handle 兜底（UnicodeDecodeError 逃出 OSError 捕获）══


class TestHttpHandleFallback:
    def _write_non_utf8_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "config" / "agents" / "badenc.yaml"
        p.write_bytes(b"config_id: badenc\nname: \xff\xfe\n")
        return p

    def test_get_non_utf8_config_returns_error_envelope(
        self, server: Any, tmp_path: Path
    ) -> None:
        self._write_non_utf8_config(tmp_path)
        result = _run_handle(server, "GET", "/ext/agent_manager/agents/badenc/config")
        assert result["success"] is False, "未捕获异常必须走错误信封而非成功包裹"
        assert "agent_manager service error" in result["error"]
        assert result["data"]["status"] == 500

    def test_list_with_non_utf8_file_returns_error_envelope(
        self, server: Any, tmp_path: Path
    ) -> None:
        self._write_non_utf8_config(tmp_path)
        result = _run_handle(server, "GET", "/ext/agent_manager/agents")
        assert result["success"] is False
        assert result["data"]["status"] == 500


# ══ 7. 服务面 agent.get 边界 ══


class TestAgentGetEdges:
    def test_broken_yaml_reported_as_not_found(self, server: Any, tmp_path: Path) -> None:
        (tmp_path / "config" / "agents" / "main" / "agentos.yaml").write_text(
            'config_id: agentos\nname: "未闭合\n\tb: 1\n', encoding="utf-8"
        )
        out = asyncio.run(server.agent_get(agent_id="agentos"))
        assert out == {"found": False, "config": None}, "损坏配置与 not-found 同形（契约）"

    @pytest.mark.parametrize("content", ["", "- a\n- b\n"])
    def test_non_dict_yaml_becomes_empty_config(
        self, server: Any, tmp_path: Path, content: str
    ) -> None:
        (tmp_path / "config" / "agents" / "main" / "agentos.yaml").write_text(
            content, encoding="utf-8"
        )
        out = asyncio.run(server.agent_get(agent_id="agentos"))
        assert out["found"] is True
        assert out["config"] == {}, "非 dict 解析结果归一为空配置"


# ══ 8. 生命周期 ══


class TestLifecycle:
    def test_on_load_starts_and_reports_agents_dir(self, server: Any) -> None:
        assert asyncio.run(server._on_load({})) is None
