# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""工具面（``oauth``：authorize / status 二合一）与声明面一致性测试。

走 ``server.py`` 注册的真实 handler（SDK dispatcher 调用的同一函数对象）：
- 输入校验（空/非 http server_url、wait_seconds 非法与超上限）→ 失败信封带 error_code；
- 「待授权」态：浏览器不可用时立即返回 URL；随后回调到达 → 复调用返回已授权；
- 状态面：只回键名与状态，绝不回 token 值（硬约束机械断言）；
- 落库失败 → ENV_WRITE_FAILED（错误是值，不吞）；
- 声明面：plugin.json 的合并工具与 server.py 注册函数同名同面（单一真值源）。
"""

from __future__ import annotations

import asyncio
import json

import flow
import pytest
from env_store import key_name, read_env
from oauth_fixtures import load_server_module

pytestmark = pytest.mark.unit


@pytest.fixture
def server_module():
    return load_server_module()


def _capturing_opener(captured: list[str]) -> object:
    def opener(url: str) -> bool:
        captured.append(url)
        return False  # 声明浏览器不可用：工具不等待、直接回「待授权」

    return opener


async def test_authorize_tool_returns_pending_then_status_after_callback(
    server_module, fake_as, env_path, callback_endpoint, monkeypatch
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(flow, "open_browser", _capturing_opener(captured))

    pending = await server_module.oauth(action="authorize", server_url=fake_as.resource_url)

    assert pending["status"] == "pending"
    assert pending["authorization_url"] == captured[0]
    assert pending["browser_opened"] is False
    assert env_path.exists() is False

    # 人工在浏览器点击后（用户代理 → 回调），复调用即见已授权
    from test_oauth_flow import _drive_via_callback

    status_code, _, _ = await _drive_via_callback(captured[0], callback_endpoint)
    assert status_code == 200
    again = await server_module.oauth(action="authorize", server_url=fake_as.resource_url)
    assert again["status"] == "authorized"
    assert again["method"] == "existing"

    report = await server_module.oauth(action="status", server_url=fake_as.resource_url)
    (entry,) = report["servers"]
    assert entry["has_access_token"] is True
    assert entry["has_refresh_token"] is True
    payload = json.dumps(report, ensure_ascii=False)
    stored = read_env(env_path)
    for suffix in ("ACCESS_TOKEN", "REFRESH_TOKEN", "CLIENT_ID"):
        assert stored[key_name(entry["env_prefix"], suffix)] not in payload, "status 绝不回凭据值"


async def test_authorize_tool_opens_browser_when_available(
    server_module, fake_as, callback_endpoint, monkeypatch
) -> None:
    captured: list[str] = []

    def opener(url: str) -> bool:
        captured.append(url)
        asyncio.get_running_loop().create_task(_drive(url, callback_endpoint))
        return True

    monkeypatch.setattr(flow, "open_browser", opener)

    result = await server_module.oauth(action="authorize", server_url=fake_as.resource_url, wait_seconds=10)

    assert result["status"] == "authorized"
    assert result["browser_opened"] is True


async def _drive(url: str, callback_endpoint) -> None:
    from test_oauth_flow import _drive_via_callback

    await _drive_via_callback(url, callback_endpoint)


@pytest.mark.parametrize(
    ("args", "expected_code"),
    [
        pytest.param({}, "URL_INVALID", id="missing-url"),
        pytest.param({"server_url": "  "}, "URL_INVALID", id="blank-url"),
        pytest.param({"server_url": "ftp://mcp.example.com/mcp"}, "URL_INVALID", id="non-http-url"),
        pytest.param(
            {"server_url": "https://mcp.example.com/mcp", "wait_seconds": "abc"},
            "OPTION_INVALID",
            id="non-numeric-wait",
        ),
        pytest.param(
            {"server_url": "https://mcp.example.com/mcp", "wait_seconds": -5}, "OPTION_INVALID", id="negative-wait"
        ),
        pytest.param(
            {"server_url": "https://mcp.example.com/mcp", "wait_seconds": 99999}, "OPTION_INVALID", id="over-max-wait"
        ),
        pytest.param(
            {"server_url": "https://mcp.example.com/mcp", "callback_base_url": "http://example.com"},
            "CALLBACK_BASE_INVALID",
            id="bad-callback-base",
        ),
        pytest.param(
            {"server_url": "https://mcp.example.com/mcp", "guidance_action": "bogus"},
            "OPTION_INVALID",
            id="bad-guidance-action",
        ),
    ],
)
async def test_authorize_tool_rejects_invalid_args(server_module, args, expected_code) -> None:
    result = await server_module.oauth(action="authorize", **args)

    assert result["success"] is False
    assert result["error_code"] == expected_code


async def test_authorize_tool_reports_env_write_failure(
    server_module, fake_as, monkeypatch, tmp_path, callback_endpoint
) -> None:
    """用户根不可写 → ENV_WRITE_FAILED（不静默当成功）。

    写失败注入在 flow 的 env 写接缝（merge_env_updates）：用户根不可写在两
    平台无同构文件系统构造——文件占位路径在 POSIX 上先炸读侧（ENOTDIR →
    ENV_READ_FAILED，Windows 上读侧报 FileNotFoundError 放行到写侧），POSIX
    chmod 又不约束 Windows ACL。按测试纪律 mock 限外部依赖（用户空间 FS 写
    面），flow 侧 OSError→ENV_WRITE_FAILED 错误翻译走真实路径。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))

    def _unwritable(path: object, updates: object) -> None:
        raise OSError("注入：用户根不可写")

    monkeypatch.setattr(flow, "merge_env_updates", _unwritable)
    captured: list[str] = []

    def opener(url: str) -> bool:
        captured.append(url)
        asyncio.get_running_loop().create_task(_drive(url, callback_endpoint))
        return True

    monkeypatch.setattr(flow, "open_browser", opener)

    result = await server_module.oauth(action="authorize", server_url=fake_as.resource_url, wait_seconds=10)

    assert result["success"] is False
    assert result["error_code"] == "ENV_WRITE_FAILED"


async def test_status_tool_reports_env_read_failure(server_module, monkeypatch, tmp_path) -> None:
    """`.env` 位置被目录占位（真实读错误）→ ENV_READ_FAILED（不把读失败当「无授权」静默吞）。"""
    blocked_root = tmp_path / "blocked-root"
    (blocked_root / ".env").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(blocked_root))

    result = await server_module.oauth(action="status", )

    assert result["success"] is False
    assert result["error_code"] == "ENV_READ_FAILED"


async def test_callback_handler_rejects_undeclared_path(server_module) -> None:
    envelope = await server_module.http_handle(
        path="/ext/oauth_broker/not-the-callback",
        method="GET",
        plugin_id="oauth_broker",
        raw_body="",
        headers={},
        query={},
    )

    assert int(envelope["data"]["status"]) == 404


async def test_status_tool_reports_all_and_absent(server_module, env_path, user_root_env) -> None:
    prefix = flow.derive_env_prefix("https://a.example.com/mcp")
    from env_store import merge_env_updates

    merge_env_updates(env_path, [(key_name(prefix, "ACCESS_TOKEN"), "AT-secret")])

    all_report = await server_module.oauth(action="status", )
    absent = await server_module.oauth(action="status", server_url="https://never.example.com/mcp")
    bad_url = await server_module.oauth(action="status", server_url="ftp://x")

    assert [e["env_prefix"] for e in all_report["servers"]] == [prefix]
    assert absent == {"servers": []}
    assert bad_url["success"] is False
    assert bad_url["error_code"] == "URL_INVALID"
    assert "AT-secret" not in json.dumps(all_report, ensure_ascii=False)


async def test_authorize_tool_uses_injected_guidance_config(server_module, fake_as, monkeypatch) -> None:
    """工具面：guidance_action=create_pat 透传，装载期注入的引导表被真正用上（自动开 PAT 页）。"""
    from test_oauth_flow import _GUIDANCE_CONFIG, _guidance_opener, _guidance_query

    fake_as.no_registration_endpoint = True
    fake_as.prm_scopes = ["repo", "gist"]
    captured: list[str] = []
    monkeypatch.setattr(server_module.plugin, "get_config", lambda: {"guidance": _GUIDANCE_CONFIG})
    monkeypatch.setattr(flow, "open_browser", _guidance_opener(captured))

    result = await server_module.oauth(action="authorize", server_url=fake_as.resource_url, guidance_action="create_pat")

    assert result["status"] == "registration_required"
    assert result["guidance"]["opened_action"] == "create_pat"
    assert captured, "有服务方行时必须自动开页"
    assert _guidance_query(captured[0])["scopes"] == "repo,gist"


def test_manifest_declares_exactly_the_registered_tools(server_module) -> None:
    """声明面（plugin.json）与实现面（server.py handler 名）单源一致。"""
    import json as _json
    from pathlib import Path

    manifest = _json.loads((Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8"))
    declared = [tool["name"] for tool in manifest["capabilities"]["tools"]]

    assert set(declared) == {"oauth"}
    for name in declared:
        assert callable(getattr(server_module, name)), f"server.py 必须暴露与声明同名的 handler：{name}"
    routes = {(ep["path"], ep["method"], ep["auth"]) for ep in manifest["http_endpoints"]}
    assert routes == {(flow.CALLBACK_PATH, "GET", "none")}
    for tool in manifest["capabilities"]["tools"]:
        assert tool.get("output_schema"), f"{tool['name']} 缺 output_schema"
        assert tool.get("render", {}).get("card"), f"{tool['name']} 缺 render.card"


async def test_oauth_rejects_unknown_action_with_guidance(server_module) -> None:
    """action 非法 → OPTION_INVALID 教改法（允许集 + 两向最小示例）。"""
    result = await server_module.oauth(action="revoke")

    assert result["success"] is False
    assert result["error_code"] == "OPTION_INVALID"
    assert "'authorize' | 'status'" in result["error"]


async def test_oauth_status_rejects_authorize_only_params(server_module) -> None:
    """status 混入 authorize 专属参数 → OPTION_INVALID 指出参数归属 action。"""
    result = await server_module.oauth(action="status", wait_seconds=10)

    assert result["success"] is False
    assert result["error_code"] == "OPTION_INVALID"
    assert "wait_seconds 仅 action='authorize' 可用" in result["error"]


def test_oauth_manifest_lockstep(server_module) -> None:
    """声明面锁步：plugin.json input_schema 与 server.py 上报同源（_MANIFEST_TOOLS）。"""
    import json as _json
    from pathlib import Path

    manifest = _json.loads(
        (Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8")
    )
    declared = manifest["capabilities"]["tools"][0]
    assert declared["name"] == "oauth"
    assert server_module._MANIFEST_TOOLS["oauth"]["input_schema"] == declared["input_schema"]
    one_of = declared["input_schema"]["oneOf"]
    assert [b["properties"]["action"]["const"] for b in one_of] == ["authorize", "paste_callback", "status"]
    # N3 双模式（CA-P0 件 2）：mode 枚举定义在顶层 properties（分支内为空占位），
    # paste_callback 分支要求 callback_url 必填
    assert declared["input_schema"]["properties"]["mode"]["enum"] == ["browser", "paste_callback"]
    paste_branch = one_of[1]
    assert paste_branch["required"] == ["action", "callback_url"]
    assert "mode" in one_of[0]["properties"], "mode 归 authorize 分支"
    assert "callback_url" not in one_of[0]["properties"], "callback_url 不归 authorize 分支"


# ────────────────────────────────────────────────────────────
# N3 粘贴回调双模式（CA-P0 件 2）：paste action 经 server.py 同一 handler 的分发面
# ────────────────────────────────────────────────────────────


async def test_paste_callback_tool_completes_authorization(server_module, fake_as, env_path) -> None:
    pending = await server_module.oauth(
        action="authorize", server_url=fake_as.resource_url, mode="paste_callback"
    )
    assert pending["status"] == "pending"
    assert pending["browser_opened"] is False
    pasted = await asyncio.to_thread(_fetch_redirect_target, pending["authorization_url"])

    done = await server_module.oauth(action="paste_callback", callback_url=pasted)

    assert done["status"] == "authorized"
    assert done["method"] == "authorization_code"
    assert done["browser_opened"] is False
    stored = read_env(env_path)
    assert stored[key_name(done["env_prefix"], "ACCESS_TOKEN")]


async def test_paste_callback_tool_requires_callback_url(server_module, fake_as) -> None:
    await server_module.oauth(action="authorize", server_url=fake_as.resource_url, mode="paste_callback")

    result = await server_module.oauth(action="paste_callback")

    assert result["success"] is False
    assert result["error_code"] == "OPTION_INVALID"
    assert "callback_url 必填" in result["error"]


async def test_paste_callback_tool_reports_rejection_as_failure_envelope(server_module, fake_as, env_path) -> None:
    await server_module.oauth(action="authorize", server_url=fake_as.resource_url, mode="paste_callback")
    bogus = f"{flow.callback_url('http://127.0.0.1:9100')}?code=x&state=never-registered"

    result = await server_module.oauth(action="paste_callback", callback_url=bogus)

    assert result["success"] is False
    assert result["error_code"] == "AUTHORIZATION_FAILED", "闸门被拒 = 授权失败（错误是值）"
    assert not env_path.exists()


async def test_authorize_tool_rejects_unknown_mode(server_module, fake_as) -> None:
    result = await server_module.oauth(action="authorize", server_url=fake_as.resource_url, mode="clipboard")

    assert result["success"] is False
    assert result["error_code"] == "OPTION_INVALID"
    assert "mode 只接受 browser/paste_callback" in result["error"]


def _fetch_redirect_target(url: str) -> str:
    from fake_auth_server import fetch_redirect_target

    return fetch_redirect_target(url)
