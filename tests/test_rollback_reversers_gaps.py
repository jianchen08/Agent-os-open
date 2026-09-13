# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""rollback 逆操作器缺口补测（根级，供插桩基集登记）。

覆盖缺口（reversers.py 官方车道 miss 行）：
- FileReverser：不支持的操作类型（EXECUTE）、reverse 异常兜底、
  DELETE 逆操作缺原始内容失败
- GitReverser：git_branch / git_stash / 不支持工具分发、异常兜底、
  reset 失败、分支删除成功/失败、stash 恢复成功/失败
- APIReverser：缺逆操作定义、不支持逆操作类型、HTTP 逆操作
  （成功/4xx/缺 URL/请求异常）

git 路径关键分支走真实临时仓库（git 不可用整组 skip）；
仅「git 命令层失败」与「HTTP 传输」两类外部依赖用替身脚本化。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLLBACK_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "rollback"

# 平铺 import 命名空间抢位：目标目录置于 sys.path 最前并逐出同名裸模块缓存
sys.path.insert(0, str(_ROLLBACK_DIR))
for _m in ("models", "reversers", "manager"):
    sys.modules.pop(_m, None)

from models import OperationLog, OperationType  # noqa: E402
from reversers import APIReverser, FileReverser, GitReverser  # noqa: E402

# 导入完成即恢复现场：裸名缓存与 sys.path 抢位若驻留，后收集的 plugins/**
# 测试（conftest 逐出钩子只覆盖 tests/ 子树）的 `from models import` 会命中
# rollback 同名模块。自身引用在上方导入期已绑定，逐出不伤本文件。
for _m in ("models", "reversers", "manager"):
    sys.modules.pop(_m, None)
sys.path.remove(str(_ROLLBACK_DIR))


# ============================================================
# git 环境辅助（与 tests/plugins/system/rollback 同款夹具）
# ============================================================


def _run(repo: Any, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在 repo 内执行 git 命令（数组参数，无 shell）。"""
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=check, capture_output=True, text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Any) -> Any:
    """真实临时 git 仓库；git 不可用则整组 skip。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用，跳过 GitReverser 测试")
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "TDD Test")
    _run(repo, "config", "core.autocrlf", "false")
    return repo


def _commit(repo: Any, filename: str, content: str) -> str:
    """提交一个文件，返回新 commit hash。"""
    (repo / filename).write_text(content, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", f"commit {filename}")
    return _head(repo)


def _head(repo: Any) -> str:
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


def _branches(repo: Any) -> list[str]:
    out = _run(repo, "branch", "--format=%(refname:short)").stdout.strip()
    return out.splitlines() if out else []


def _stashes(repo: Any) -> list[str]:
    out = _run(repo, "stash", "list").stdout.strip()
    return out.splitlines() if out else []


# ============================================================
# FileReverser
# ============================================================


class TestFileReverserGaps:
    @pytest.mark.asyncio
    async def test_unsupported_operation_type_rejected(self, tmp_path: Any) -> None:
        """EXECUTE 型操作不属于文件逆操作语义 → 明确失败而非静默。"""
        results = []
        for tool in ("file_read", "shell_exec"):
            op = OperationLog(
                tool_name=tool,
                operation_type=OperationType.EXECUTE,
                target=str(tmp_path / "x.txt"),
                before_state={"content": "x"},
            )
            results.append(await FileReverser().reverse(op))

        for r in results:
            assert r["success"] is False
            assert "不支持的操作类型" in r["message"]

    @pytest.mark.asyncio
    async def test_reverse_exception_caught_and_reported(self, tmp_path: Any) -> None:
        """逆操作内部异常不外溢：统一失败信封 + 错误明细，原对象不损坏。"""
        # 输入 A：UPDATE 原始内容非字符串（write_text 拒绝）→ TypeError
        target_a = tmp_path / "a.txt"
        target_a.write_text("orig", encoding="utf-8")
        op_a = OperationLog(
            tool_name="file_update",
            operation_type=OperationType.UPDATE,
            target=str(target_a),
            before_state={"content": 123},
        )
        r_a = await FileReverser().reverse(op_a)

        # 输入 B：DELETE 恢复目标是一个目录（write_text 拒绝目录）→ OSError
        target_b = tmp_path / "b_dir"
        target_b.mkdir()
        op_b = OperationLog(
            tool_name="file_delete",
            operation_type=OperationType.DELETE,
            target=str(target_b),
            before_state={"content": "x"},
        )
        r_b = await FileReverser().reverse(op_b)

        for r in (r_a, r_b):
            assert r["success"] is False
            assert "逆操作执行失败" in r["message"]
            assert r["details"]["error"]  # 错误明细可观测
        # 失败不留半成品：原对象保持原样
        assert target_a.read_text(encoding="utf-8") == "orig"
        assert target_b.is_dir()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before_state", [None, {}])
    async def test_reverse_delete_without_content_fails(
        self, tmp_path: Any, before_state: dict[str, Any] | None,
    ) -> None:
        """无原始内容快照 → 拒绝恢复（不静默造空文件）。"""
        target = tmp_path / "gone.txt"
        op = OperationLog(
            tool_name="file_delete",
            operation_type=OperationType.DELETE,
            target=str(target),
            before_state=before_state,
        )

        result = await FileReverser().reverse(op)

        assert result["success"] is False
        assert "缺少原始内容" in result["message"]
        assert not target.exists()


# ============================================================
# GitReverser
# ============================================================


class TestGitReverserDispatch:
    """reverse 按 tool_name 分发到对应逆操作与失败分支。"""

    @pytest.mark.asyncio
    async def test_branch_without_name_fails(self, git_repo: Any) -> None:
        op = OperationLog(tool_name="git_branch", operation_type=OperationType.EXECUTE, params={})

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is False
        assert "分支名称" in result["message"]

    @pytest.mark.asyncio
    async def test_unsupported_git_tool_rejected(self, git_repo: Any) -> None:
        op = OperationLog(tool_name="git_checkout", operation_type=OperationType.EXECUTE)

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is False
        assert "不支持的 Git 操作" in result["message"]
        assert "git_checkout" in result["message"]

    @pytest.mark.asyncio
    async def test_git_command_exception_caught(self, git_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """git 命令层崩溃 → 异常兜底失败信封，不向调用方外溢。"""
        rev = GitReverser(repo_path=str(git_repo))

        async def _crash(*args: str) -> tuple[int, str, str]:
            raise RuntimeError("git binary vanished")

        monkeypatch.setattr(rev, "_run_git_command", _crash)
        op = OperationLog(
            tool_name="git_commit",
            operation_type=OperationType.EXECUTE,
            before_state={"commit_hash": "a" * 40},
        )

        result = await rev.reverse(op)

        assert result["success"] is False
        assert "Git 逆操作执行失败" in result["message"]
        assert result["details"]["error"] == "git binary vanished"


class TestGitReverserBranch:
    @pytest.mark.asyncio
    async def test_branch_delete_success(self, git_repo: Any) -> None:
        """git_branch 逆操作 = 删除创建的分支：真实仓库内分支消失。"""
        _commit(git_repo, "a.txt", "A")
        _run(git_repo, "branch", "feature-x")
        assert "feature-x" in _branches(git_repo)

        op = OperationLog(
            tool_name="git_branch",
            operation_type=OperationType.EXECUTE,
            params={"branch_name": "feature-x"},
        )

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is True
        assert result["details"] == {"action": "delete_branch", "branch": "feature-x"}
        assert "feature-x" not in _branches(git_repo)  # 性质断言：分支确已不存在

    @pytest.mark.asyncio
    async def test_branch_delete_failure_reports_stderr(self, git_repo: Any) -> None:
        """删除不存在的分支 → git 非零退出 → 失败信封携带 stderr。"""
        _commit(git_repo, "a.txt", "A")

        op = OperationLog(
            tool_name="git_branch",
            operation_type=OperationType.EXECUTE,
            params={"branch_name": "no-such-branch"},
        )

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is False
        assert "删除分支失败" in result["message"]
        assert result["details"]["stderr"]


class TestGitReverserStash:
    @pytest.mark.asyncio
    async def test_stash_restore_success(self, git_repo: Any) -> None:
        """git_stash 逆操作 = pop 指定 stash：工作区恢复、stash 栈清空。"""
        _commit(git_repo, "a.txt", "committed")
        (git_repo / "a.txt").write_text("working change", encoding="utf-8")
        _run(git_repo, "stash")
        assert len(_stashes(git_repo)) == 1

        op = OperationLog(
            tool_name="git_stash",
            operation_type=OperationType.EXECUTE,
            before_state={"stash_index": 0},
        )

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is True
        assert result["details"] == {"action": "stash_pop", "index": 0}
        # stash 内容回到工作区，栈清空
        assert (git_repo / "a.txt").read_text(encoding="utf-8") == "working change"
        assert _stashes(git_repo) == []

    @pytest.mark.asyncio
    async def test_stash_restore_failure_when_no_stash(self, git_repo: Any) -> None:
        """空 stash 栈 → pop 非零退出 → 失败信封。"""
        _commit(git_repo, "a.txt", "A")
        assert _stashes(git_repo) == []

        op = OperationLog(
            tool_name="git_stash",
            operation_type=OperationType.EXECUTE,
            before_state={"stash_index": 0},
        )

        result = await GitReverser(repo_path=str(git_repo)).reverse(op)

        assert result["success"] is False
        assert "恢复 stash 失败" in result["message"]
        assert result["details"]["stderr"]

    @pytest.mark.asyncio
    async def test_reset_failure_reports_stderr(
        self, git_repo: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """前置校验通过但 reset 本身失败 → 失败信封携带 stderr，不假装成功。"""
        rev = GitReverser(repo_path=str(git_repo))

        async def _scripted(*args: str) -> tuple[int, str, str]:
            if args[0] in ("rev-parse", "merge-base"):
                return (0, "", "")  # 记录点存在且 HEAD 未早于记录点
            return (128, "", "fatal: cannot reset")

        monkeypatch.setattr(rev, "_run_git_command", _scripted)
        op = OperationLog(
            tool_name="git_commit",
            operation_type=OperationType.EXECUTE,
            before_state={"commit_hash": "a" * 40},
        )

        result = await rev.reverse(op)

        assert result["success"] is False
        assert "Git reset 失败" in result["message"]
        assert result["details"] == {"stderr": "fatal: cannot reset"}


# ============================================================
# APIReverser
# ============================================================


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _install_fake_aiohttp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    exc: Exception | None = None,
) -> list[dict[str, Any]]:
    """把 aiohttp.ClientSession 换成脚本化替身（网络为外部依赖），返回请求记录。"""
    import aiohttp

    requests: list[dict[str, Any]] = []

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

        def request(
            self, method: str, url: str,
            headers: dict[str, str] | None = None,
            json: Any = None,
        ) -> _FakeResponse:
            requests.append({"method": method, "url": url, "headers": headers, "json": json})
            if exc is not None:
                raise exc
            return _FakeResponse(status)

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    return requests


class TestAPIReverserGaps:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["api_create", "api_delete"])
    async def test_missing_reverse_action_fails(self, tool_name: str) -> None:
        op = OperationLog(tool_name=tool_name, operation_type=OperationType.EXECUTE)

        result = await APIReverser().reverse(op)

        assert result["success"] is False
        assert "缺少逆操作定义" in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reverse_action", [{"type": None}, {"type": "sql"}])
    async def test_unsupported_action_type_fails(
        self, reverse_action: dict[str, Any],
    ) -> None:
        op = OperationLog(
            tool_name="api_delete",
            operation_type=OperationType.EXECUTE,
            reverse_action=reverse_action,
        )

        result = await APIReverser().reverse(op)

        assert result["success"] is False
        assert "不支持的逆操作类型" in result["message"]

    @pytest.mark.asyncio
    async def test_http_reverse_success_sends_configured_request(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """http 型逆操作：method/url/headers/body 按定义发出，2xx 判成功。"""
        for status in (200, 204):
            requests = _install_fake_aiohttp(monkeypatch, status=status)
            op = OperationLog(
                tool_name="api_delete",
                operation_type=OperationType.EXECUTE,
                reverse_action={
                    "type": "http",
                    "method": "DELETE",
                    "url": "https://api.example.com/items/7",
                    "headers": {"X-Token": "t1"},
                    "body": {"reason": "rollback"},
                },
            )

            result = await APIReverser().reverse(op)

            assert result["success"] is True
            assert result["details"]["status"] == status
            sent = requests[0]
            assert sent == {
                "method": "DELETE",
                "url": "https://api.example.com/items/7",
                "headers": {"X-Token": "t1"},
                "json": {"reason": "rollback"},
            }

    @pytest.mark.asyncio
    async def test_http_reverse_client_error_status_fails(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """4xx/5xx 响应 → 逆操作失败，状态码可观测。"""
        requests = _install_fake_aiohttp(monkeypatch, status=404)
        op = OperationLog(
            tool_name="api_delete",
            operation_type=OperationType.EXECUTE,
            reverse_action={"type": "http", "method": "DELETE", "url": "https://x/y"},
        )

        result = await APIReverser().reverse(op)

        assert result["success"] is False
        assert "404" in result["message"]
        assert result["details"] == {"status": 404}
        assert len(requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", [None, ""])
    async def test_http_reverse_without_url_fails(self, url: str | None) -> None:
        op = OperationLog(
            tool_name="api_delete",
            operation_type=OperationType.EXECUTE,
            reverse_action={"type": "http", "method": "DELETE", "url": url},
        )

        result = await APIReverser().reverse(op)

        assert result["success"] is False
        assert "缺少 URL" in result["message"]

    @pytest.mark.asyncio
    async def test_http_reverse_transport_exception_fails(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """传输层异常（连接拒绝等）→ 失败信封携带错误明细。"""
        _install_fake_aiohttp(monkeypatch, exc=OSError("connection refused"))
        op = OperationLog(
            tool_name="api_delete",
            operation_type=OperationType.EXECUTE,
            reverse_action={"type": "http", "method": "DELETE", "url": "https://x/y"},
        )

        result = await APIReverser().reverse(op)

        assert result["success"] is False
        assert "HTTP 请求失败" in result["message"]
        assert result["details"]["error"] == "connection refused"
