"""
社区运营工具：GitHub 开源仓操作（github_ops）+ 反馈台账（feedback_ledger）。

github_ops：对开源仓 issue/PR 做只读巡检（list_issues/list_prs/get_issue/
repo_stats）与评论发布（create_comment）。评论属对外发布，经
config/plugins/security_check/security_rules.yaml 的 needs_approval 规则强制人工审批；
鉴权令牌读环境变量 AGENTOS_GITHUB_TOKEN（回退 GITHUB_TOKEN），未配置时
只读动作可用（GitHub 匿名限额 60 次/小时），写动作 fail-closed 拒绝。

feedback_ledger：社区反馈登记册（JSON 单文件，缺 entries 键或损坏即报错，
不静默重置丢数据），动作 append/update_status/list。路径解析顺序：
参数 ledger_path > 环境变量 AGENTOS_COMMUNITY_LEDGER > <cwd>/data/community/。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

DEFAULT_REPO = "jianchen08/Agent-os-open"
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_API_BASE = "https://api.github.com"
_EXCERPT_LEN = 300
_DEFAULT_LEDGER = Path("data") / "community" / "feedback_ledger.json"
_LEDGER_STATUSES = ("待处置", "已回复", "已入候选池", "已入 ROADMAP", "已否决", "挂起")


def _github_token() -> str:
    for env in ("AGENTOS_GITHUB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return ""


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _excerpt(body: str | None) -> str:
    return (body or "")[:_EXCERPT_LEN]


def _parse_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item["number"],
        "title": item.get("title", ""),
        "state": item.get("state", ""),
        "author": (item.get("user") or {}).get("login", ""),
        "created_at": item.get("created_at", ""),
        "html_url": item.get("html_url", ""),
        "labels": [label.get("name", "") for label in item.get("labels", [])],
        "comments": item.get("comments", 0),
        "excerpt": _excerpt(item.get("body")),
    }


def _parse_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "author": (item.get("user") or {}).get("login", ""),
        "created_at": item.get("created_at", ""),
        "body": item.get("body", ""),
    }


class GitHubOpsTool(BuiltinTool):
    """GitHub 开源仓巡检与评论发布。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="github_ops",
            description=(
                "GitHub 开源仓操作：巡检 issue/PR、查看仓库状态、发布 issue 评论。"
                "create_comment 属对外发布，会触发人工审批；未配置"
                " AGENTOS_GITHUB_TOKEN 时只读可用、评论被拒绝。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_issues",
                            "get_issue",
                            "list_prs",
                            "repo_stats",
                            "create_comment",
                        ],
                        "description": "操作类型",
                    },
                    "repo": {
                        "type": "string",
                        "pattern": r"^[\w.-]+/[\w.-]+$",
                        "description": "仓库（owner/repo），默认开源仓",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "default": "open",
                        "description": "list_issues/list_prs 的状态过滤",
                    },
                    "number": {
                        "type": "integer",
                        "description": "issue 编号（get_issue/create_comment 必填）",
                    },
                    "body": {
                        "type": "string",
                        "minLength": 1,
                        "description": "评论内容（create_comment 必填，Markdown）",
                    },
                    "per_page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "列表每页条数",
                    },
                },
                "required": ["action"],
            },
            output_schema={
                "type": "object",
                "required": ["success"],
                "properties": {
                    "success": {"type": "boolean"},
                    "action": {"type": "string"},
                    "items": {
                        "type": "array",
                        "description": "list_issues/list_prs 结果",
                        "items": {"type": "object"},
                    },
                    "issue": {
                        "type": "object",
                        "description": "get_issue 的 issue 本体",
                    },
                    "comments": {
                        "type": "array",
                        "description": "get_issue 的评论列表",
                        "items": {"type": "object"},
                    },
                    "stats": {"type": "object", "description": "repo_stats 结果"},
                    "comment_url": {
                        "type": "string",
                        "description": "create_comment 发布的评论链接",
                    },
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.WEB,
            tags=["github", "community", "issues"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        repo = inputs.get("repo") or DEFAULT_REPO
        if not _REPO_RE.match(str(repo)):
            return create_failure_result(
                error=f"仓库格式非法（应为 owner/repo）: {repo}",
                error_code="INVALID_REPO",
            )
        action = inputs.get("action")
        if action == "list_issues":
            return await self._list_items(repo, "issues", inputs)
        if action == "list_prs":
            return await self._list_items(repo, "pulls", inputs)
        if action == "get_issue":
            return await self._get_issue(repo, inputs)
        if action == "repo_stats":
            return await self._repo_stats(repo)
        if action == "create_comment":
            return await self._create_comment(repo, inputs)
        return create_failure_result(
            error=f"不支持的操作: {action}",
            error_code="INVALID_ACTION",
        )

    async def _request(
        self,
        method: str,
        path: str,
        inputs: dict[str, Any],
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    f"{_API_BASE}{path}",
                    headers=_github_headers(),
                    json=json_body,
                    params=params,
                    timeout=httpx.Timeout(inputs.get("timeout", 30)),
                )
        except httpx.TimeoutException:
            return create_failure_result(error="请求超时", error_code="TIMEOUT")
        except httpx.HTTPError as exc:
            return create_failure_result(
                error=f"GitHub 请求失败: {exc}",
                error_code="GITHUB_API_ERROR",
            )
        if response.status_code >= 400:
            hint = ""
            if response.status_code in (401, 403):
                hint = "（检查 AGENTOS_GITHUB_TOKEN 是否有效/未超匿名限额）"
            return create_failure_result(
                error=f"HTTP {response.status_code}: {response.text[:300]}{hint}",
                error_code=f"HTTP_{response.status_code}",
            )
        return create_success_result(data={"payload": response.json()})

    async def _list_items(
        self, repo: str, kind: str, inputs: dict[str, Any]
    ) -> ToolExecutionResult:
        result = await self._request(
            "GET",
            f"/repos/{repo}/{kind}",
            inputs,
            params={
                "state": inputs.get("state", "open"),
                "per_page": inputs.get("per_page", 20),
                "sort": "created",
                "direction": "desc",
            },
        )
        if not result.success:
            return result
        assert result.output is not None
        items = [_parse_issue(item) for item in result.output["payload"]]
        return create_success_result(
            data={"action": kind, "items": items},
            metadata={"repo": repo},
        )

    async def _get_issue(self, repo: str, inputs: dict[str, Any]) -> ToolExecutionResult:
        number = inputs.get("number")
        if number is None:
            return create_failure_result(
                error="get_issue 需要 number 参数",
                error_code="MISSING_NUMBER",
            )
        result = await self._request("GET", f"/repos/{repo}/issues/{number}", inputs)
        if not result.success:
            return result
        assert result.output is not None
        issue = result.output["payload"]
        comments_result = await self._request(
            "GET", f"/repos/{repo}/issues/{number}/comments", inputs
        )
        comments = (
            [
                _parse_comment(item)
                for item in (
                    comments_result.output["payload"]
                    if comments_result.output is not None
                    else []
                )
            ]
            if comments_result.success
            else []
        )
        return create_success_result(
            data={
                "action": "get_issue",
                "issue": {
                    **_parse_issue(issue),
                    "body": issue.get("body", ""),
                },
                "comments": comments,
            },
            metadata={"repo": repo},
        )

    async def _repo_stats(self, repo: str) -> ToolExecutionResult:
        result = await self._request("GET", f"/repos/{repo}", {})
        if not result.success:
            return result
        assert result.output is not None
        payload = result.output["payload"]
        return create_success_result(
            data={
                "action": "repo_stats",
                "stats": {
                    "stars": payload.get("stargazers_count", 0),
                    "forks": payload.get("forks_count", 0),
                    "open_issues": payload.get("open_issues_count", 0),
                    "watchers": payload.get("subscribers_count", 0),
                    "pushed_at": payload.get("pushed_at", ""),
                },
            },
            metadata={"repo": repo},
        )

    async def _create_comment(
        self, repo: str, inputs: dict[str, Any]
    ) -> ToolExecutionResult:
        if not _github_token():
            return create_failure_result(
                error=(
                    "未配置 GitHub 令牌，评论发布被拒绝。"
                    "请设置环境变量 AGENTOS_GITHUB_TOKEN（需 repo 权限）。"
                ),
                error_code="TOKEN_MISSING",
            )
        number = inputs.get("number")
        if number is None:
            return create_failure_result(
                error="create_comment 需要 number 参数",
                error_code="MISSING_NUMBER",
            )
        body = inputs.get("body")
        if not body:
            return create_failure_result(
                error="create_comment 需要 body 参数",
                error_code="MISSING_BODY",
            )
        result = await self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            inputs,
            json_body={"body": body},
        )
        if not result.success:
            return result
        assert result.output is not None
        return create_success_result(
            data={
                "action": "create_comment",
                "comment_url": result.output["payload"].get("html_url", ""),
            },
            metadata={"repo": repo, "issue": number},
        )


class FeedbackLedgerTool(BuiltinTool):
    """社区反馈登记册（JSON 单文件）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="feedback_ledger",
            description=(
                "社区反馈登记册：登记来源（GitHub/群聊等）的反馈条目、推进状态、"
                "按状态查询。状态取值：待处置/已回复/已入候选池/已入 ROADMAP/已否决/挂起。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["append", "update_status", "list"],
                        "description": "操作类型",
                    },
                    "source": {
                        "type": "string",
                        "description": "反馈来源（append 必填），如 github_issue/B站/群聊",
                    },
                    "summary": {
                        "type": "string",
                        "description": "反馈内容摘要（append 必填）",
                    },
                    "category": {
                        "type": "string",
                        "description": "分类（append 可选）：bug_report/feature/咨询/合作/其他",
                    },
                    "link": {
                        "type": "string",
                        "description": "原文链接（append 可选）",
                    },
                    "id": {
                        "type": "string",
                        "pattern": "^FB-\\d{3,}$",
                        "description": "条目编号（update_status 必填）",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(_LEDGER_STATUSES),
                        "description": "状态（update_status 必填）",
                    },
                    "note": {
                        "type": "string",
                        "description": "处置备注（update_status 可选，覆盖写）",
                    },
                    "filter_status": {
                        "type": "string",
                        "description": "list 按状态过滤（可选）",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "list 返回条数上限（可选，默认全部）",
                    },
                    "ledger_path": {
                        "type": "string",
                        "description": "登记册文件路径（可选，覆盖默认与环境变量）",
                    },
                },
                "required": ["action"],
            },
            output_schema={
                "type": "object",
                "required": ["success"],
                "properties": {
                    "success": {"type": "boolean"},
                    "action": {"type": "string"},
                    "entry": {"type": "object", "description": "append/update_status 结果条目"},
                    "entries": {
                        "type": "array",
                        "description": "list 结果",
                        "items": {"type": "object"},
                    },
                    "total": {"type": "integer", "description": "list 过滤后总数"},
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            tags=["community", "feedback", "ledger"],
        )

    def _ledger_path(self, inputs: dict[str, Any]) -> Path:
        explicit = inputs.get("ledger_path")
        if explicit:
            return Path(str(explicit))
        env_path = os.environ.get("AGENTOS_COMMUNITY_LEDGER", "").strip()
        if env_path:
            return Path(env_path)
        return _DEFAULT_LEDGER

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"entries": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError("登记册结构非法（缺少 entries 数组）")
        return data

    @staticmethod
    def _save(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        path = self._ledger_path(inputs)
        action = inputs.get("action")
        try:
            if action == "append":
                return self._append(path, inputs)
            if action == "update_status":
                return self._update_status(path, inputs)
            if action == "list":
                return self._list(path, inputs)
        except json.JSONDecodeError as exc:
            return create_failure_result(
                error=f"登记册文件损坏（不自动重置，请人工修复）: {path} | {exc}",
                error_code="LEDGER_CORRUPT",
            )
        except (OSError, ValueError) as exc:
            return create_failure_result(
                error=f"登记册操作失败: {exc}",
                error_code="LEDGER_ERROR",
            )
        return create_failure_result(
            error=f"不支持的操作: {action}",
            error_code="INVALID_ACTION",
        )

    def _append(self, path: Path, inputs: dict[str, Any]) -> ToolExecutionResult:
        source = inputs.get("source")
        summary = inputs.get("summary")
        if not source or not summary:
            return create_failure_result(
                error="append 需要 source 与 summary 参数",
                error_code="MISSING_FIELDS",
            )
        data = self._load(path)
        max_seq = 0
        for entry in data["entries"]:
            match = re.match(r"^FB-(\d+)$", str(entry.get("id", "")))
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        entry = {
            "id": f"FB-{max_seq + 1:03d}",
            "date": date.today().isoformat(),
            "source": str(source),
            "summary": str(summary),
            "category": str(inputs.get("category", "")),
            "status": str(inputs.get("status") or "待处置"),
            "link": str(inputs.get("link", "")),
            "note": "",
        }
        data["entries"].append(entry)
        self._save(path, data)
        return create_success_result(data={"action": "append", "entry": entry})

    def _update_status(self, path: Path, inputs: dict[str, Any]) -> ToolExecutionResult:
        entry_id = inputs.get("id")
        status = inputs.get("status")
        if not entry_id or not status:
            return create_failure_result(
                error="update_status 需要 id 与 status 参数",
                error_code="MISSING_FIELDS",
            )
        data = self._load(path)
        for entry in data["entries"]:
            if entry.get("id") == entry_id:
                entry["status"] = str(status)
                if inputs.get("note"):
                    entry["note"] = str(inputs["note"])
                self._save(path, data)
                return create_success_result(
                    data={"action": "update_status", "entry": entry}
                )
        return create_failure_result(
            error=f"条目不存在: {entry_id}",
            error_code="ENTRY_NOT_FOUND",
        )

    def _list(self, path: Path, inputs: dict[str, Any]) -> ToolExecutionResult:
        data = self._load(path)
        filter_status = inputs.get("filter_status")
        entries = [
            entry
            for entry in data["entries"]
            if not filter_status or entry.get("status") == filter_status
        ]
        limit = inputs.get("limit")
        total = len(entries)
        if limit is not None:
            entries = entries[: int(limit)]
        return create_success_result(
            data={"action": "list", "entries": entries, "total": total}
        )
