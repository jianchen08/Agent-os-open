# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""community_ops GitHubOpsTool 单元测试。

覆盖 plugins/shared/tools/community_ops/tool.py 的 GitHubOpsTool：
1. get_tool_definition 工具契约（名称/必填/枚举 action/output schema）
2. 令牌解析：AGENTOS_GITHUB_TOKEN / GITHUB_TOKEN 回退 / 双缺省
3. execute 分发：非法 repo / 各 action / 非法 action
4. _request：成功 / 超时 / 传输错误 / HTTP>=400（401/403 提示分支与其余分支）
5. 各 action 成功路径解析（list 解析字段映射、get_issue 评论失败降级、
   create_comment 令牌缺失 fail-closed）

外部依赖打桩：httpx.AsyncClient 用路由化伪客户端替换（网络）；令牌用
monkeypatch 环境变量注入。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_tool() -> Any:
    """动态加载 tool.py（唯一模块名，避免与裸名 tool 冲突）。"""
    mod_name = "community_ops_github_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None, "Cannot load tool.py"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_tool()
GitHubOpsTool = _MOD.GitHubOpsTool


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    """路由化伪客户端：按 URL 后缀返回预设响应，可抛异常。"""

    routes: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        for suffix, resp in type(self).routes.items():
            if url.endswith(suffix):
                item = resp(kwargs) if callable(resp) else resp
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unexpected url: {method} {url}")


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _install_routes(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> None:
    _FakeAsyncClient.routes = routes
    monkeypatch.setattr(_MOD.httpx, "AsyncClient", _FakeAsyncClient)


_ISSUE = {
    "number": 7,
    "title": "安装失败",
    "state": "open",
    "user": {"login": "someone"},
    "created_at": "2026-09-14T00:00:00Z",
    "html_url": "https://github.com/o/r/issues/7",
    "labels": [{"name": "bug"}],
    "comments": 1,
    "body": "启动报错" * 200,
}


def test_definition_contract() -> None:
    tool = GitHubOpsTool.get_tool_definition()
    assert tool.name == "github_ops"
    assert tool.input_schema["required"] == ["action"]
    actions = tool.input_schema["properties"]["action"]["enum"]
    assert set(actions) == {
        "list_issues",
        "get_issue",
        "list_prs",
        "repo_stats",
        "create_comment",
    }
    assert "success" in tool.output_schema["required"]


def test_token_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert _MOD._github_token() == ""
    monkeypatch.setenv("GITHUB_TOKEN", "fallback")
    assert _MOD._github_token() == "fallback"
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "primary")
    assert _MOD._github_token() == "primary"
    headers = _MOD._github_headers()
    assert headers["Authorization"] == "Bearer primary"
    monkeypatch.delenv("AGENTOS_GITHUB_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN")
    assert "Authorization" not in _MOD._github_headers()


def test_invalid_repo_and_action(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(GitHubOpsTool().execute({"action": "repo_stats", "repo": "no-slash"}))
    assert result.success is False
    assert result.error_code == "INVALID_REPO"
    result = _run(GitHubOpsTool().execute({"action": "nuke"}))
    assert result.success is False
    assert result.error_code == "INVALID_ACTION"


def test_list_issues_parses_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def capture(kwargs: dict[str, Any]) -> _FakeResponse:
        seen.update(kwargs)
        return _FakeResponse(payload=[_ISSUE])

    _install_routes(monkeypatch, {"/repos/o/r/issues": capture})
    result = _run(
        GitHubOpsTool().execute(
            {"action": "list_issues", "repo": "o/r", "state": "all", "per_page": 5}
        )
    )
    assert result.success is True
    item = result.output["items"][0]
    assert item["number"] == 7
    assert item["author"] == "someone"
    assert item["labels"] == ["bug"]
    assert item["comments"] == 1
    assert len(item["excerpt"]) == _MOD._EXCERPT_LEN
    assert seen["params"]["state"] == "all"
    assert seen["params"]["per_page"] == 5
    assert seen["params"]["direction"] == "desc"


def test_list_prs_failure_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_routes(monkeypatch, {"/repos/o/r/pulls": _FakeResponse(500, {}, "boom")})
    result = _run(GitHubOpsTool().execute({"action": "list_prs", "repo": "o/r"}))
    assert result.success is False
    assert result.error_code == "HTTP_500"


def test_get_issue_with_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_routes(
        monkeypatch,
        {
            "/repos/o/r/issues/7/comments": _FakeResponse(
                payload=[
                    {"user": {"login": "dev"}, "created_at": "t1", "body": "已收到"}
                ]
            ),
            "/repos/o/r/issues/7": _FakeResponse(payload=_ISSUE),
        },
    )
    result = _run(
        GitHubOpsTool().execute({"action": "get_issue", "repo": "o/r", "number": 7})
    )
    assert result.success is True
    assert result.output["issue"]["body"] == _ISSUE["body"]
    assert result.output["comments"][0]["author"] == "dev"
    result = _run(GitHubOpsTool().execute({"action": "get_issue", "repo": "o/r"}))
    assert result.success is False
    assert result.error_code == "MISSING_NUMBER"


def test_get_issue_comments_failure_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_routes(
        monkeypatch,
        {
            "/repos/o/r/issues/7/comments": _FakeResponse(500, {}, "boom"),
            "/repos/o/r/issues/7": _FakeResponse(payload=_ISSUE),
        },
    )
    result = _run(
        GitHubOpsTool().execute({"action": "get_issue", "repo": "o/r", "number": 7})
    )
    assert result.success is True
    assert result.output["comments"] == []


def test_repo_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_routes(
        monkeypatch,
        {
            "/repos/o/r": _FakeResponse(
                payload={
                    "stargazers_count": 5,
                    "forks_count": 0,
                    "open_issues_count": 2,
                    "subscribers_count": 3,
                    "pushed_at": "2026-09-14T00:00:00Z",
                }
            )
        },
    )
    result = _run(GitHubOpsTool().execute({"action": "repo_stats", "repo": "o/r"}))
    assert result.success is True
    stats = result.output["stats"]
    assert stats["stars"] == 5
    assert stats["open_issues"] == 2
    assert stats["pushed_at"] == "2026-09-14T00:00:00Z"


def test_create_comment_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = _run(
        GitHubOpsTool().execute(
            {"action": "create_comment", "repo": "o/r", "number": 7, "body": "hi"}
        )
    )
    assert result.success is False
    assert result.error_code == "TOKEN_MISSING"


def test_create_comment_param_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    result = _run(GitHubOpsTool().execute({"action": "create_comment", "repo": "o/r"}))
    assert result.error_code == "MISSING_NUMBER"
    result = _run(
        GitHubOpsTool().execute({"action": "create_comment", "repo": "o/r", "number": 7})
    )
    assert result.error_code == "MISSING_BODY"


def test_create_comment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    seen: dict[str, Any] = {}

    def capture(kwargs: dict[str, Any]) -> _FakeResponse:
        seen.update(kwargs)
        return _FakeResponse(payload={"html_url": "https://github.com/o/r/issues/7#c1"})

    _install_routes(monkeypatch, {"/repos/o/r/issues/7/comments": capture})
    result = _run(
        GitHubOpsTool().execute(
            {"action": "create_comment", "repo": "o/r", "number": 7, "body": "回复"}
        )
    )
    assert result.success is True
    assert result.output["comment_url"].endswith("#c1")
    assert seen["json"] == {"body": "回复"}
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_get_issue_failure_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_routes(
        monkeypatch,
        {"/repos/o/r/issues/9": _FakeResponse(404, {"message": "Not Found"}, "nope")},
    )
    result = _run(
        GitHubOpsTool().execute({"action": "get_issue", "repo": "o/r", "number": 9})
    )
    assert result.success is False
    assert result.error_code == "HTTP_404"


def test_create_comment_failure_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    _install_routes(
        monkeypatch,
        {"/repos/o/r/issues/7/comments": _FakeResponse(422, {}, "invalid")},
    )
    result = _run(
        GitHubOpsTool().execute(
            {"action": "create_comment", "repo": "o/r", "number": 7, "body": "x"}
        )
    )
    assert result.success is False
    assert result.error_code == "HTTP_422"


def test_request_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    _install_routes(
        monkeypatch,
        {"/repos/o/r": httpx.TimeoutException("slow")},
    )
    result = _run(GitHubOpsTool().execute({"action": "repo_stats", "repo": "o/r"}))
    assert result.error_code == "TIMEOUT"
    _install_routes(
        monkeypatch,
        {"/repos/o/r": httpx.ConnectError("refused")},
    )
    result = _run(GitHubOpsTool().execute({"action": "repo_stats", "repo": "o/r"}))
    assert result.error_code == "GITHUB_API_ERROR"


def test_request_http_403_carries_token_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    _install_routes(
        monkeypatch,
        {"/repos/o/r": _FakeResponse(403, {}, "rate limited")},
    )
    result = _run(GitHubOpsTool().execute({"action": "repo_stats", "repo": "o/r"}))
    assert result.success is False
    assert result.error_code == "HTTP_403"
    assert "AGENTOS_GITHUB_TOKEN" in result.error
