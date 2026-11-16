# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""``flow`` 授权流程编排 + 回调闸门测试（真 HTTP / 真 PKCE / 真 .env）。

验收面：
- 全链路：授权 URL → 真 302 → 回调 handler → 换 token → 用户根 .env 落库；
- 等待有界：回调不来时返回「待授权」+ URL（不挂死），且晚到的回调仍能完成落库；
- 幂等：有效 token 存在时不重走流程、不重复注册；
- 刷新：过期走 refresh 轮换；refresh 被拒回落全量授权（并用既有 client_id）；
- 预注册机密客户端：client_secret 落 .env 并在换 token/刷新时随请求提交；
- CIMD：operator 传 HTTPS 元数据文档 URL 当 client_id 时原样透传、跳过注册；
- 引导分支：自助注册不可行（元数据不可得/无注册端点/注册被拒）→ 自动开注册页或
  PAT 页（best-effort 预填）+ 逐条列出要手填的值；
- 回调闸门 fail-closed：state 不符 / 缺 state / iss 不符 / AS 声明支持 iss 却缺 iss
  → 一律拒且**零 token 请求**；方法不符 → 405；
- 凭据零泄漏：token/client_secret/code/verifier 不进工具返回值、不进日志、
  不进 .env 之外的文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import flow
import oauth_protocol
import pytest
from env_store import key_name, merge_env_updates, read_env
from fake_auth_server import fetch_redirect_target, parse_query
from oauth_fixtures import load_server_module
from oauth_protocol import OAuthError

pytestmark = pytest.mark.unit


# ────────────────────────────────────────────────────────────
# 助手：用户代理（opener 替身 + 真 302 + 回调）
# ────────────────────────────────────────────────────────────


def _opener_capturing(captured: list[str], driver: Any = None) -> Any:
    """opener 替身：记录授权 URL（给了 driver 则调度「用户代理」任务），声明浏览器已打开。"""

    def opener(url: str) -> bool:
        captured.append(url)
        if driver is not None:
            asyncio.get_running_loop().create_task(driver(url))
        return True

    return opener


def _opener_unavailable(captured: list[str]) -> Any:
    def opener(url: str) -> bool:
        captured.append(url)
        return False

    return opener


async def _wait_for_capture(captured: list[str], timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while not captured and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert captured, "授权 URL 未被捕获（流程未走到浏览器打开步骤）"
    return captured[0]


async def _drive_via_callback(url: str, callback_endpoint, *, tamper=None) -> tuple:
    """用户代理：授权 URL → 302 → 回调 query（可篡改）→ 插件回调 handler。"""
    query = await asyncio.to_thread(lambda: parse_query(fetch_redirect_target(url)))
    if tamper is not None:
        query = tamper(query)
    return await callback_endpoint(query)


async def _run_full_flow(fake_as, callback_endpoint, *, wait_seconds: float = 10, **kwargs) -> dict:
    captured: list[str] = []
    opener = _opener_capturing(captured, lambda url: _drive_via_callback(url, callback_endpoint))
    return await flow.authorize(server_url=fake_as.resource_url, opener=opener, wait_seconds=wait_seconds, **kwargs)


async def _start_pending_flow(fake_as, *, wait_seconds: float = 5) -> tuple[str, asyncio.Task[dict]]:
    """起一个「等回调」的授权流程，返回（授权 URL, 任务）。"""
    captured: list[str] = []
    opener = _opener_capturing(captured)  # 只捕获，不驱动 302
    task = asyncio.create_task(
        flow.authorize(server_url=fake_as.resource_url, opener=opener, wait_seconds=wait_seconds)
    )
    url = await _wait_for_capture(captured)
    return url, task


# ────────────────────────────────────────────────────────────
# 1. 全链路与落库
# ────────────────────────────────────────────────────────────


async def test_full_flow_writes_user_root_env_and_preserves_foreign_keys(
    fake_as, env_path, callback_endpoint, user_root_env
) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("# 既有\nOPENAI_API_KEY=sk-other\n", encoding="utf-8")

    result = await _run_full_flow(fake_as, callback_endpoint)

    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"
    assert result["browser_opened"] is True
    assert result["server_url"] == fake_as.resource_url
    assert result["resource"] == fake_as.resource_url
    assert result["authorization_url"] is None, "已授权后不再回授权 URL（state 无残留面）"
    vars_after = read_env(env_path)
    prefix = result["env_prefix"]
    assert vars_after["OPENAI_API_KEY"] == "sk-other", "别人的键一个不动"
    assert re.fullmatch(r"AT-\d+", vars_after[key_name(prefix, "ACCESS_TOKEN")])
    assert re.fullmatch(r"RT-\d+", vars_after[key_name(prefix, "REFRESH_TOKEN")])
    assert vars_after[key_name(prefix, "TOKEN_ENDPOINT")] == f"{fake_as.base_url}/token"
    assert vars_after[key_name(prefix, "CLIENT_ID")].startswith("client-")
    assert vars_after[key_name(prefix, "RESOURCE")] == fake_as.resource_url
    assert datetime.fromisoformat(vars_after[key_name(prefix, "EXPIRES_AT")]) > datetime.now(UTC)
    assert set(result["env_keys"]) == {key_name(prefix, suffix) for suffix in flow.ENV_KEY_SUFFIXES}
    # 性质：落库凭据就是对面端签发的那一对——换 refresh 成功即证明（真状态机闭环）
    refreshed = await asyncio.to_thread(
        oauth_protocol.refresh_access_token,
        f"{fake_as.base_url}/token",
        refresh_token=vars_after[key_name(prefix, "REFRESH_TOKEN")],
        client_id=vars_after[key_name(prefix, "CLIENT_ID")],
        resource=fake_as.resource_url,
    )
    assert refreshed["access_token"] != vars_after[key_name(prefix, "ACCESS_TOKEN")]


async def test_full_flow_never_leaks_secrets_to_result_logs_or_other_files(
    fake_as, env_path, callback_endpoint, user_root_env, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = await _run_full_flow(fake_as, callback_endpoint)

    vars_after = read_env(env_path)
    prefix = result["env_prefix"]
    secrets = [
        vars_after[key_name(prefix, "ACCESS_TOKEN")],
        vars_after[key_name(prefix, "REFRESH_TOKEN")],
        vars_after[key_name(prefix, "CLIENT_ID")],
        fake_as.authorize_requests[0]["state"],
        fake_as.token_requests[0]["code"],
        fake_as.token_requests[0]["code_verifier"],
    ]
    payload = json.dumps(result, ensure_ascii=False)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in payload, f"凭据不得进工具返回值：{secret}"
        assert secret not in logs, f"凭据不得进日志：{secret}"
    assert [p.name for p in user_root_env.iterdir()] == [".env"], "用户根只新增 .env（无 tmp/缓存残骸）"


async def test_pending_on_timeout_then_late_callback_still_lands(fake_as, env_path, callback_endpoint) -> None:
    """回调未到 → 有界等待后返回待授权 + URL；晚到的回调仍完成换 token 落库。"""
    url, task = await _start_pending_flow(fake_as, wait_seconds=1)

    started = time.monotonic()
    result = await task
    elapsed = time.monotonic() - started

    assert result["status"] == "pending"
    assert result["method"] is None
    assert result["expires_at"] is None
    assert result["authorization_url"] == url
    assert elapsed < 10, f"等待必须有界：{elapsed}s"
    assert not env_path.exists(), "未授权不得写 .env"

    status_code, body, _ = await _drive_via_callback(url, callback_endpoint)

    assert status_code == 200
    assert "授权成功" in body
    vars_after = read_env(env_path)
    assert re.fullmatch(r"AT-\d+", vars_after[key_name(result["env_prefix"], "ACCESS_TOKEN")])


async def test_pending_without_wait_when_browser_unavailable(fake_as, env_path) -> None:
    captured: list[str] = []
    started = time.monotonic()

    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_opener_unavailable(captured), wait_seconds=30
    )

    assert result["status"] == "pending"
    assert result["browser_opened"] is False
    assert result["authorization_url"] == captured[0]
    assert time.monotonic() - started < 5, "浏览器打不开时不得干等"
    assert result["notes"], "必须给可操作提示（人工点击后重新调用）"
    assert not env_path.exists()


async def test_wait_seconds_zero_returns_url_immediately(fake_as, env_path) -> None:
    """wait_seconds=0：即便浏览器已打开也立即返回授权 URL（不阻塞调用方）。"""
    captured: list[str] = []
    started = time.monotonic()

    result = await flow.authorize(server_url=fake_as.resource_url, opener=_opener_capturing(captured), wait_seconds=0)

    assert result["status"] == "pending"
    assert result["browser_opened"] is True
    assert result["authorization_url"] == captured[0]
    assert time.monotonic() - started < 5
    assert any("wait_seconds=0" in note for note in result["notes"])


async def test_stale_pending_pruned_on_next_flow_start(fake_as, env_path, callback_endpoint, monkeypatch) -> None:
    """挂起表保活上限：超期条目在新流程启动时清理 → 其回调按「state 无效」拒。"""
    monkeypatch.setattr(flow, "PENDING_TTL_SECONDS", 0.0)
    stale_url, stale_task = await _start_pending_flow(fake_as, wait_seconds=0.1)
    assert (await stale_task)["status"] == "pending"

    fresh_url, fresh_task = await _start_pending_flow(fake_as, wait_seconds=0.1)
    assert (await fresh_task)["status"] == "pending"

    stale_status, stale_body, _ = await _drive_via_callback(stale_url, callback_endpoint)
    fresh_status, _, _ = await _drive_via_callback(fresh_url, callback_endpoint)

    assert stale_status == 400
    assert "state" in stale_body
    assert fresh_status == 200, "新流程不受旧条目清理影响"


# ────────────────────────────────────────────────────────────
# 2. 预注册客户端（client_secret / CIMD）与幂等、刷新
# ────────────────────────────────────────────────────────────


async def test_preregistered_client_with_secret_lands_in_env(auth_server_factory, env_path, callback_endpoint) -> None:
    """预注册机密客户端：DCR 被跳过；secret 落 .env 且换 token/刷新真的带上它。"""
    server = auth_server_factory(require_client_secret="s3cr3t")

    result = await _run_full_flow(server, callback_endpoint, client_id="preregistered-client", client_secret="s3cr3t")

    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"
    assert server.registrations == [], "预注册 client_id 在时不得再走动态注册"
    vars_after = read_env(env_path)
    prefix = result["env_prefix"]
    assert vars_after[key_name(prefix, "CLIENT_ID")] == "preregistered-client"
    assert vars_after[key_name(prefix, "CLIENT_SECRET")] == "s3cr3t"
    assert server.token_requests[-1]["client_secret"] == "s3cr3t"
    payload = json.dumps(result, ensure_ascii=False)
    assert "s3cr3t" not in payload, "client_secret 绝不进返回值"


async def test_preregistered_client_stored_secret_used_on_refresh(
    auth_server_factory, env_path, callback_endpoint
) -> None:
    """secret 落库后，过期刷新从 .env 取 secret 继续用（不用重新传参）。"""
    server = auth_server_factory(require_client_secret="s3cr3t")
    first = await _run_full_flow(server, callback_endpoint, client_id="preregistered-client", client_secret="s3cr3t")
    prefix = first["env_prefix"]
    stale = read_env(env_path)
    stale[key_name(prefix, "EXPIRES_AT")] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    env_path.write_text("\n".join(f"{k}={v}" for k, v in stale.items()) + "\n", encoding="utf-8")

    result = await flow.authorize(server_url=server.resource_url, opener=_opener_unavailable([]), wait_seconds=0)

    assert result["status"] == "authorized"
    assert result["method"] == "refresh"
    assert server.token_requests[-1]["client_secret"] == "s3cr3t"


async def test_cimd_client_id_url_passed_through(fake_as, callback_endpoint) -> None:
    """CIMD 形态：operator 把自托管 HTTPS 元数据文档 URL 当 client_id —— 原样透传、跳过注册。"""
    cimd_url = "https://client.example/oauth-client-metadata.json"

    result = await _run_full_flow(fake_as, callback_endpoint, client_id=cimd_url)

    assert result["status"] == "authorized"
    assert fake_as.registrations == [], "CIMD 形态不走动态注册"
    assert fake_as.authorize_requests[-1]["client_id"] == cimd_url, "client_id 原样进授权请求"
    assert fake_as.token_requests[-1]["client_id"] == cimd_url


async def test_dcr_returned_client_secret_not_persisted(auth_server_factory, env_path, callback_endpoint) -> None:
    """DCR 回 client_secret（公共客户端形态不应出现）→ 不落库、不带进 token 请求、不进返回值。"""
    server = auth_server_factory(dcr_client_secret="dcr-secret")

    result = await _run_full_flow(server, callback_endpoint)

    assert result["status"] == "authorized"
    prefix = result["env_prefix"]
    assert key_name(prefix, "CLIENT_SECRET") not in read_env(env_path)
    assert "client_secret" not in server.token_requests[-1], "公共客户端不得回带 DCR secret"
    assert "dcr-secret" not in json.dumps(result, ensure_ascii=False)


async def test_authorize_reuses_client_id_and_valid_token_across_calls(fake_as, env_path, callback_endpoint) -> None:
    await _run_full_flow(fake_as, callback_endpoint)
    assert len(fake_as.registrations) == 1

    second = await flow.authorize(server_url=fake_as.resource_url, opener=_opener_unavailable([]), wait_seconds=0)

    assert second["status"] == "authorized"
    assert second["method"] == "existing"
    assert second["authorization_url"] is None
    assert len(fake_as.registrations) == 1, "有效 token 在时不得再注册客户端"
    assert len(fake_as.authorize_requests) == 1, "有效 token 在时不得再开授权请求"


async def test_authorize_skips_dcr_when_client_id_already_stored(fake_as, env_path, callback_endpoint) -> None:
    """注册结果缓存面：.env 已有 _CLIENT_ID → 新一轮授权复用，不再 POST /register。"""
    prefix = flow.derive_env_prefix(fake_as.resource_url)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"{key_name(prefix, 'CLIENT_ID')}=client-preseeded\n", encoding="utf-8")

    result = await _run_full_flow(fake_as, callback_endpoint)

    assert result["status"] == "authorized"
    assert fake_as.registrations == []
    assert read_env(env_path)[key_name(prefix, "CLIENT_ID")] == "client-preseeded"


async def test_expired_token_refreshes_and_rotates(fake_as, env_path, callback_endpoint) -> None:
    first = await _run_full_flow(fake_as, callback_endpoint)
    prefix = first["env_prefix"]
    vars_before = read_env(env_path)
    expired = dict(vars_before)
    expired[key_name(prefix, "EXPIRES_AT")] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    env_path.write_text("\n".join(f"{k}={v}" for k, v in expired.items()) + "\n", encoding="utf-8")

    result = await flow.authorize(server_url=fake_as.resource_url, opener=_opener_unavailable([]), wait_seconds=0)

    assert result["status"] == "authorized"
    assert result["method"] == "refresh"
    vars_after = read_env(env_path)
    assert vars_after[key_name(prefix, "ACCESS_TOKEN")] != vars_before[key_name(prefix, "ACCESS_TOKEN")]
    assert vars_after[key_name(prefix, "REFRESH_TOKEN")] != vars_before[key_name(prefix, "REFRESH_TOKEN")], (
        "公共客户端 refresh 轮换：新 refresh 必须落库"
    )
    assert len(fake_as.registrations) == 1, "刷新不得重注册客户端"


async def test_refresh_rejected_falls_back_to_full_authorization(fake_as, env_path, callback_endpoint) -> None:
    first = await _run_full_flow(fake_as, callback_endpoint)
    prefix = first["env_prefix"]
    stale = read_env(env_path)
    stale[key_name(prefix, "EXPIRES_AT")] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    stale[key_name(prefix, "REFRESH_TOKEN")] = "RT-revoked"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in stale.items()) + "\n", encoding="utf-8")

    result = await _run_full_flow(fake_as, callback_endpoint)

    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"
    assert result["refresh_error"] == "TOKEN_REQUEST_FAILED", "刷新失败如实上报后再回落"
    assert len(fake_as.registrations) == 1, "回落全量授权复用既有 client_id"


async def test_refresh_context_missing_reported_before_full_flow(fake_as, env_path, callback_endpoint) -> None:
    """有过期 token 与 refresh token 但缺 token_endpoint → 刷新不可行，如实上报后回落全量授权。"""
    prefix = flow.derive_env_prefix(fake_as.resource_url)
    merge_env_updates(
        env_path,
        [
            (key_name(prefix, "ACCESS_TOKEN"), "AT-stale"),
            (key_name(prefix, "EXPIRES_AT"), (datetime.now(UTC) - timedelta(minutes=1)).isoformat()),
            (key_name(prefix, "REFRESH_TOKEN"), "RT-x"),
        ],
    )

    result = await _run_full_flow(fake_as, callback_endpoint)

    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"
    assert result["refresh_error"] == "REFRESH_CONTEXT_MISSING"


# ────────────────────────────────────────────────────────────
# 3. 回调闸门（fail-closed）
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda q: {**q, "state": "tampered-state"}, id="wrong-state"),
        pytest.param(lambda q: {k: v for k, v in q.items() if k != "state"}, id="missing-state"),
        pytest.param(lambda q: {**q, "iss": "https://evil.example"}, id="iss-mismatch"),
        pytest.param(lambda q: {k: v for k, v in q.items() if k != "iss"}, id="iss-missing-but-advertised"),
    ],
)
async def test_callback_rejects_fail_closed(fake_as, env_path, callback_endpoint, tamper) -> None:
    url, task = await _start_pending_flow(fake_as)

    status_code, body, _ = await _drive_via_callback(url, callback_endpoint, tamper=tamper)
    result = await task

    assert status_code == 400, f"闸门必须拒：{body}"
    assert fake_as.token_requests == [], "被拒的回调一个 token 请求都不许发"
    assert not env_path.exists(), "被拒的回调不得落库"
    assert result["status"] == "pending", "回调被拒 ≠ 流程完成"


async def test_callback_accepts_absent_iss_when_as_does_not_advertise(auth_server_factory, callback_endpoint) -> None:
    """对照面：AS 未声明支持 iss 且响应无 iss → 放行（闸门不得过宽）。"""
    server = auth_server_factory(omit_iss_supported=True, iss_in_response="")

    result = await _run_full_flow(server, callback_endpoint)

    assert result["status"] == "authorized"


async def test_callback_missing_code_reports_and_fails_waiting_flow(fake_as, env_path, callback_endpoint) -> None:
    """回调带对了 state/iss 但没 code → 400 且等待方收到失败（流程终态化）。"""
    url, task = await _start_pending_flow(fake_as)

    def tamper(query: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in query.items() if k != "code"}

    status_code, body, _ = await _drive_via_callback(url, callback_endpoint, tamper=tamper)

    assert status_code == 400
    assert "code" in body
    with pytest.raises(OAuthError) as excinfo:
        await task
    assert excinfo.value.code == "AUTHORIZATION_FAILED"
    assert not env_path.exists()


async def test_callback_exchange_failure_reports_local_error_code(fake_as, env_path, callback_endpoint) -> None:
    """换 token 失败（授权码被换过/无效）→ 400 且等待方收到 TOKEN_REQUEST_FAILED（本地面错误码直通）。"""
    url, task = await _start_pending_flow(fake_as)

    def tamper(query: dict[str, str]) -> dict[str, str]:
        return {**query, "code": "code-bogus"}

    status_code, body, _ = await _drive_via_callback(url, callback_endpoint, tamper=tamper)

    assert status_code == 400
    assert "TOKEN_REQUEST_FAILED" in body
    with pytest.raises(OAuthError) as excinfo:
        await task
    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert not env_path.exists()


async def test_callback_reports_as_error_and_fails_waiting_flow(fake_as, env_path, callback_endpoint) -> None:
    url, task = await _start_pending_flow(fake_as)

    def tamper(query: dict[str, str]) -> dict[str, str]:
        return {"state": query["state"], "iss": query["iss"], "error": "access_denied"}

    status_code, body, _ = await _drive_via_callback(url, callback_endpoint, tamper=tamper)

    assert status_code == 400
    assert "access_denied" in body
    with pytest.raises(OAuthError) as excinfo:
        await task
    assert excinfo.value.code == "AUTHORIZATION_FAILED"
    assert not env_path.exists()


async def test_callback_rejects_unknown_state_without_touching_env(env_path, callback_endpoint) -> None:
    status_code, body, envelope = await callback_endpoint({"code": "code-x", "state": "never-registered"})

    assert status_code == 400
    assert "重新" in body, "必须给可操作提示（重新调用 oauth_authorize）"
    assert envelope["success"] is True, "回调是浏览器面 400，不是插件失败信封"
    assert not env_path.exists()


async def test_http_handle_guards_method(callback_endpoint) -> None:
    server_module = load_server_module()
    wrong_method = await server_module.http_handle(
        path=flow.CALLBACK_PATH, method="POST", plugin_id="oauth_broker", raw_body="", headers={}, query={}
    )

    assert int(wrong_method["data"]["status"]) == 405


# ────────────────────────────────────────────────────────────
# 3.5 人工引导分支（自助注册不可行：自动开注册页/PAT 页 + 逐条列值）
# ────────────────────────────────────────────────────────────

#: 引导配置夹具（形态与出厂 guidance 配置面同构：providers → host → 两个 URL + 预填参数）
_GUIDANCE_HOST = "127.0.0.1"
_GUIDANCE_CONFIG = {
    "providers": {
        _GUIDANCE_HOST: {
            "app_name": "Lingxi AgentOS",
            "registration_url": "https://example.test/settings/applications/new",
            "registration_params": {
                "oauth_application[name]": "{app_name}",
                "oauth_application[url]": "{homepage_url}",
                "oauth_application[callback_url]": "{callback_url}",
            },
            "token_url": "https://example.test/settings/tokens/new",
            "token_params": {"scopes": "{scopes_comma}", "description": "{description}"},
        }
    }
}


def _guidance_opener(captured: list[str]) -> Any:
    """引导分支的 opener 替身：只记录被打开的 URL（不开真浏览器）。"""

    def opener(url: str) -> bool:
        captured.append(url)
        return True

    return opener


def _guidance_query(url: str) -> dict[str, str]:
    return parse_query(url)


async def test_guidance_when_registration_endpoint_missing(fake_as, env_path) -> None:
    """AS 元数据无 registration_endpoint 且未传 client_id → 引导分支：开注册页 + 逐条列值。"""
    fake_as.no_registration_endpoint = True
    fake_as.prm_scopes = ["org:read", "project:write"]
    captured: list[str] = []

    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )

    assert result["status"] == "registration_required"
    assert result["browser_opened"] is True
    guide = result["guidance"]
    assert guide["provider"] == _GUIDANCE_HOST
    opened = _guidance_query(captured[0])
    assert captured[0].startswith("https://example.test/settings/applications/new?")
    assert opened["oauth_application[callback_url]"] == guide["callback_url"]
    assert guide["callback_url"].endswith(flow.CALLBACK_PATH), "回调地址须是真实回调 URL"
    assert opened["oauth_application[name]"] == "Lingxi AgentOS"
    assert opened["oauth_application[url]"] == guide["homepage_url"]
    assert guide["scopes"] == ["org:read", "project:write"], "scope 清单取自 PRM 的 scopes_supported"
    assert guide["pat_env_key"] == key_name(result["env_prefix"], "ACCESS_TOKEN")
    assert guide["auth_config_snippet"] == '{"type":"bearer","value":"${' + guide["pat_env_key"] + '}"}'
    assert any(guide["callback_url"] in note for note in result["notes"]), "文案必须给出可操作下一步"
    assert not env_path.exists(), "引导分支不落任何凭据"


async def test_guidance_when_authorization_server_metadata_unavailable(fake_as, env_path) -> None:
    """连 AS 元数据都没有（老式端点，如 GitHub）→ 仍然进引导分支（别只报「不支持」）。"""
    fake_as.as_metadata_form = "none"
    fake_as.prm_scopes = ["repo", "read:org"]
    captured: list[str] = []

    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )

    assert result["status"] == "registration_required"
    assert result["guidance"]["scopes"] == ["repo", "read:org"]
    assert "元数据不可得" in result["notes"][0]
    assert captured, "引导分支必须自动开页"
    assert captured[0].startswith("https://example.test/settings/applications/new?")


async def test_guidance_create_pat_action_opens_token_page(fake_as, env_path, user_root_env) -> None:
    """guidance_action=create_pat → 打开 PAT 创建页（带 scope/描述预填），并指明落库键名。"""
    fake_as.no_registration_endpoint = True
    fake_as.prm_scopes = ["repo", "gist"]
    captured: list[str] = []

    result = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=_guidance_opener(captured),
        guidance=_GUIDANCE_CONFIG,
        guidance_action=flow.GUIDANCE_ACTION_CREATE_PAT,
    )

    assert result["status"] == "registration_required"
    assert result["guidance"]["opened_action"] == "create_pat"
    opened = _guidance_query(captured[0])
    assert captured[0].startswith("https://example.test/settings/tokens/new?")
    assert opened["scopes"] == "repo,gist"
    assert "description" in opened
    # PAT 路径闭环：token 写进指引的键名后，复调用即视为已授权（无需再走 OAuth）
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"{result['guidance']['pat_env_key']}=ghp_demo_token\n", encoding="utf-8")
    again = await flow.authorize(server_url=fake_as.resource_url, opener=_guidance_opener([]))
    assert again["status"] == "authorized"
    assert again["method"] == "existing"


async def test_guidance_then_preregistered_client_completes_flow(fake_as, env_path, callback_endpoint) -> None:
    """引导 → 用户贴回 client_id → 标准授权流程自动走完（1 注册 + 1 粘贴 + 1 授权点击）。"""
    fake_as.no_registration_endpoint = True
    fake_as.prm_scopes = ["files:read"]
    captured: list[str] = []
    guided = await flow.authorize(
        server_url=fake_as.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )
    assert guided["status"] == "registration_required"

    result = await _run_full_flow(fake_as, callback_endpoint, client_id="user-owned-app")

    assert result["status"] == "authorized"
    assert fake_as.registrations == []
    assert fake_as.authorize_requests[-1]["client_id"] == "user-owned-app"


async def test_guidance_on_registration_rejected(auth_server_factory, env_path) -> None:
    """注册端点被拒（REGISTRATION_FAILED）→ 也转引导分支（错误要可操作，不只是报错）。"""
    server = auth_server_factory(dcr_error=(400, {"error": "invalid_redirect_uri"}))
    server.prm_scopes = ["files:read"]
    captured: list[str] = []

    result = await flow.authorize(
        server_url=server.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )

    assert result["status"] == "registration_required"
    assert "注册被拒" in result["notes"][0]


async def test_guidance_when_protected_resource_metadata_missing(auth_server_factory) -> None:
    """连 PRM 都不可得（两种形态全 404）且未传 client_id → 引导分支（scope 清单为空）。"""
    server = auth_server_factory(omit_prm_subpath=True, omit_prm_root=True)
    captured: list[str] = []

    result = await flow.authorize(
        server_url=server.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )

    assert result["status"] == "registration_required"
    assert "受保护资源元数据不可得" in result["notes"][0]
    assert result["guidance"]["scopes"] == []


async def test_prm_missing_with_client_id_fails_closed(auth_server_factory) -> None:
    """PRM 全 404 但传了 client_id → 硬失败（不静默直连 AS，也不吞成引导）。"""
    server = auth_server_factory(omit_prm_subpath=True, omit_prm_root=True)

    with pytest.raises(OAuthError) as excinfo:
        await flow.authorize(server_url=server.resource_url, client_id="user-owned", opener=_opener_unavailable([]))

    assert excinfo.value.code == "DISCOVERY_FAILED"


async def test_non_http_registration_endpoint_fails_closed(auth_server_factory) -> None:
    """AS 元数据给非 http/https 的注册端点 → URL_INVALID 硬失败（不静默引导、不放行）。"""
    server = auth_server_factory()
    server.as_metadata_override = {
        "issuer": server.issuer,
        "authorization_endpoint": f"{server.base_url}/authorize",
        "token_endpoint": f"{server.base_url}/token",
        "registration_endpoint": "ftp://registry.example/register",
        "code_challenge_methods_supported": ["S256"],
    }

    with pytest.raises(OAuthError) as excinfo:
        await flow.authorize(server_url=server.resource_url, opener=_opener_unavailable([]))

    assert excinfo.value.code == "URL_INVALID"


async def test_guidance_notes_mention_cimd_when_advertised(auth_server_factory) -> None:
    """AS 声明支持客户端元数据文档（CIMD）→ 引导文案给出该可选路径，默认行为不变。"""
    server = auth_server_factory(no_registration_endpoint=True)
    server.as_metadata_override = {
        "issuer": server.issuer,
        "authorization_endpoint": f"{server.base_url}/authorize",
        "token_endpoint": f"{server.base_url}/token",
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    }
    captured: list[str] = []

    result = await flow.authorize(
        server_url=server.resource_url, opener=_guidance_opener(captured), guidance=_GUIDANCE_CONFIG
    )

    assert result["status"] == "registration_required"
    assert any("CIMD" in note for note in result["notes"])


async def test_guidance_not_taken_when_client_id_given(auth_server_factory, callback_endpoint) -> None:
    """对照面：无注册端点但传了 client_id → 直走授权流程，不进引导分支。"""
    server = auth_server_factory(no_registration_endpoint=True)

    result = await _run_full_flow(server, callback_endpoint, client_id="user-owned-app")

    assert result["status"] == "authorized"
    assert "guidance" not in result


async def test_guidance_without_provider_row_still_lists_values(fake_as, env_path) -> None:
    """未配置该服务（无 URL 行）→ 不打开页面，但值照旧逐条列出（复制粘贴兜底）。"""
    fake_as.as_metadata_form = "none"
    fake_as.prm_scopes = []

    result = await flow.authorize(server_url=fake_as.resource_url, opener=_guidance_opener([]), guidance=None)

    assert result["status"] == "registration_required"
    assert result["browser_opened"] is False
    guide = result["guidance"]
    assert guide["provider"] is None
    assert guide["registration_url"] is None
    assert guide["pat_page_url"] is None
    assert guide["app_name"] == "Lingxi AgentOS"
    assert guide["callback_url"].endswith(flow.CALLBACK_PATH)
    assert guide["scopes"] == []


# ────────────────────────────────────────────────────────────
# 4. 地址与元数据安全门
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:9100", "http://127.0.0.1:9100"),
        ("http://127.0.0.1:9100/", "http://127.0.0.1:9100"),
        ("http://localhost:9100", "http://localhost:9100"),
        ("https://127.0.0.1:9443", "https://127.0.0.1:9443"),
    ],
)
def test_resolve_callback_base_accepts_loopback(raw, expected) -> None:
    assert flow.resolve_callback_base(raw) == expected


@pytest.mark.parametrize("raw", ["http://example.com", "https://10.0.0.5", "ftp://127.0.0.1", "not-a-url"])
def test_resolve_callback_base_rejects_non_loopback(raw) -> None:
    with pytest.raises(OAuthError) as excinfo:
        flow.resolve_callback_base(raw)

    assert excinfo.value.code == "CALLBACK_BASE_INVALID"


def test_resolve_callback_base_defaults_to_kernel_port(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_KERNEL_PORT", raising=False)
    assert flow.resolve_callback_base(None) == "http://127.0.0.1:9100"

    monkeypatch.setenv("AGENTOS_KERNEL_PORT", "19100")
    assert flow.resolve_callback_base(None) == "http://127.0.0.1:19100"

    monkeypatch.setenv("AGENTOS_KERNEL_PORT", "not-a-port")
    assert flow.resolve_callback_base(None) == "http://127.0.0.1:9100"


def test_callback_url_is_declared_ext_namespace() -> None:
    url = flow.callback_url("http://127.0.0.1:9100")

    assert url == "http://127.0.0.1:9100/ext/oauth_broker/callback"
    assert flow.CALLBACK_PATH == "/ext/oauth_broker/callback"


def test_default_open_browser_reports_success_and_swallows_backend_errors(monkeypatch) -> None:
    """默认浏览器打开面：正常返回布尔；后端缺失（抛异常）→ False（不致命）。"""
    monkeypatch.setattr(flow.webbrowser, "open", lambda _url: True)
    assert flow.open_browser("http://127.0.0.1:9100/x") is True

    monkeypatch.setattr(flow.webbrowser, "open", lambda _url: False)
    assert flow.open_browser("http://127.0.0.1:9100/x") is False

    def boom(url: str) -> bool:
        raise OSError("no browser backend")

    monkeypatch.setattr(flow.webbrowser, "open", boom)
    assert flow.open_browser("http://127.0.0.1:9100/x") is False


@pytest.mark.parametrize("raw", ["ftp://mcp.example.com/mcp", "mcp.example.com/mcp", "https://x/mcp#fragment", ""])
def test_normalize_server_url_rejects_non_http(raw) -> None:
    with pytest.raises(OAuthError) as excinfo:
        flow.normalize_server_url(raw)

    assert excinfo.value.code == "URL_INVALID"


def test_normalize_server_url_strips_trailing_slash_only() -> None:
    assert flow.normalize_server_url("https://mcp.example.com/mcp/") == "https://mcp.example.com/mcp"
    assert flow.normalize_server_url("HTTPS://MCP.Example.com/mcp") == "HTTPS://MCP.Example.com/mcp"


async def test_authorize_rejects_non_loopback_callback_base(fake_as) -> None:
    with pytest.raises(OAuthError) as excinfo:
        await flow.authorize(
            server_url=fake_as.resource_url, callback_base_url="http://example.com", opener=_opener_unavailable([])
        )

    assert excinfo.value.code == "CALLBACK_BASE_INVALID"
    assert fake_as.authorize_requests == [], "非法回调基址在校验期就拒（未触达对面端）"


async def test_authorize_rejects_metadata_missing_token_endpoint(auth_server_factory) -> None:
    server = auth_server_factory(omit_token_endpoint=True)

    with pytest.raises(OAuthError) as excinfo:
        await flow.authorize(server_url=server.resource_url, opener=_opener_unavailable([]))

    assert excinfo.value.code == "METADATA_INVALID"


# ────────────────────────────────────────────────────────────
# 5. 状态查询（只回键名与状态，绝不回 token 值）
# ────────────────────────────────────────────────────────────


async def test_status_reports_state_without_secret_values(fake_as, env_path, callback_endpoint) -> None:
    result = await _run_full_flow(fake_as, callback_endpoint)
    prefix = result["env_prefix"]

    report = flow.status(server_url=fake_as.resource_url)

    (entry,) = report["servers"]
    assert entry["env_prefix"] == prefix
    assert entry["has_access_token"] is True
    assert entry["has_refresh_token"] is True
    assert entry["expired"] is False
    assert entry["needs_refresh"] is False
    payload = json.dumps(report, ensure_ascii=False)
    stored = read_env(env_path)
    for suffix in ("ACCESS_TOKEN", "REFRESH_TOKEN", "CLIENT_ID"):
        assert stored[key_name(prefix, suffix)] not in payload, f"状态面绝不回凭据值：{suffix}"


def test_status_lists_all_servers_and_unknown_server_is_absent(env_path) -> None:
    prefix_a = flow.derive_env_prefix("https://a.example.com/mcp")
    prefix_b = flow.derive_env_prefix("https://b.example.com/mcp")
    merge_env_updates(
        env_path,
        [
            (key_name(prefix_a, "ACCESS_TOKEN"), "AT-a"),
            (key_name(prefix_a, "EXPIRES_AT"), (datetime.now(UTC) - timedelta(hours=1)).isoformat()),
            (key_name(prefix_b, "ACCESS_TOKEN"), "AT-b"),
            (key_name(prefix_b, "EXPIRES_AT"), (datetime.now(UTC) + timedelta(hours=1)).isoformat()),
        ],
    )

    report = flow.status()
    entries = {entry["env_prefix"]: entry for entry in report["servers"]}

    assert set(entries) == {prefix_a, prefix_b}
    assert entries[prefix_a]["expired"] is True
    assert entries[prefix_a]["needs_refresh"] is False, "无 refresh token 时 needs_refresh 恒 False"
    assert entries[prefix_b]["expired"] is False
    assert flow.status(server_url="https://unknown.example.com/mcp")["servers"] == []
