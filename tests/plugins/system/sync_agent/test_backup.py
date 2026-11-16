# @feature: FP-0.2.多端同步 T6 手动备份（拍板③a） | @ci: python-coverage
"""手动备份功能测试（T6，台账 R7 拍板③a）：DB 逻辑记录 + 指定工作空间 → 本地
git 仓库，commit 即备份点。

车道口径（任务书：轻量=单实例真实 git 子进程，备份→回溯→恢复链路断言）：
git 侧全走真实子进程与真实仓库（备份→清单→恢复闭环，零 mock 内部依赖）；
内核读面（db-admin table_query / service-registry messages.list）是插件外的
内核能力边界，按外部依赖 fake 端口注入。

语义断言面：
- 一次触发两件事各产一个 commit（db 记录先、工作空间后，单线历史）；
- 增量：零变更零 commit（git status 内置检测）；删除=墓碑提交；
- 空记录/空工作空间=诚实跳过（选跳过方案，台账登记）；
- 备份点清单=git log；恢复=从备份点检出到干净目标（拒覆盖非空目录）；
- git 缺失=备份能力停用诚实报错（禁静默）；名称/路径违约显式拒绝。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from backup import BackupConfig, BackupError, BackupService, KernelReads
from git_cli import RUNTIME_EXCLUDES, GitCommandError, ensure_bare_repo, run_git

pytestmark = pytest.mark.unit

# ── 内核读面 fake（外部依赖边界：db-admin / service-registry）──────────


class FakeKernelReads:
    """内核读端口替身：表行与管道消息字典，模拟 db-admin/service-registry 数据。"""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        messages: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.tables = tables or {}
        self.messages = messages or {}
        self.table_calls: list[str] = []
        self.message_calls: list[str] = []

    async def list_table_rows(self, table: str) -> list[dict[str, Any]]:
        self.table_calls.append(table)
        return list(self.tables.get(table, []))

    async def list_pipeline_messages(self, pipeline_id: str) -> list[dict[str, Any]]:
        self.message_calls.append(pipeline_id)
        return list(self.messages.get(pipeline_id, []))


def _service(tmp_path: Path, reads: KernelReads) -> BackupService:
    return BackupService(BackupConfig(repo_root=tmp_path / "backup"), reads=reads)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _git_log(git_dir: Path) -> list[str]:
    """测试观察面：直接以 git CLI 读仓库可观察状态（空历史返回空表）。"""
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), "log", "--format=%H %s", "refs/heads/backup"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _ls_tree(git_dir: Path, rev: str) -> list[str]:
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), "ls-tree", "-r", "--name-only", rev],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.splitlines()


# ── git_cli 公共件 ────────────────────────────────────────────────


class TestGitCli:
    def test_run_git_missing_executable_raises(self) -> None:
        with pytest.raises(GitCommandError):
            run_git("git-executable-missing-xyz", None, None, "--version")

    def test_ensure_bare_repo_idempotent_and_head_ref(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "r.git"
        ensure_bare_repo(git_dir, "refs/heads/backup", "dev-x", RUNTIME_EXCLUDES)
        ensure_bare_repo(git_dir, "refs/heads/backup", "dev-x", RUNTIME_EXCLUDES)
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        assert head == "ref: refs/heads/backup"
        exclude = (git_dir / "info" / "exclude").read_text(encoding="utf-8")
        assert ".venv/" in exclude

    def test_run_git_returncode_is_result_surface(self, tmp_path: Path) -> None:
        """非零返回码不抛（返回码即结果面，调用方逐点裁决——file_channel 同律）。"""
        git_dir = tmp_path / "r.git"
        ensure_bare_repo(git_dir, "refs/heads/backup", "dev-x", RUNTIME_EXCLUDES)
        proc = run_git("git", git_dir, None, "rev-parse", "--verify", "--quiet", "nope")
        assert proc.returncode != 0


# ── 备份执行：两件事各产一个 commit ─────────────────────────────────


class TestRunBackup:
    def test_first_run_produces_db_then_workspace_commits(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        reads = FakeKernelReads(
            tables={
                "sessions": [
                    {
                        "thread_id": "t1",
                        "title": "会话一",
                        "tenant_id": "default",
                        "pipeline_ids": '["p1"]',
                    }
                ],
                "pipeline_sessions": [
                    {"pipeline_id": "p1", "thread_id": "t1", "tenant_id": "default"}
                ],
                "pipeline_state": [
                    {"pipeline_id": "p1", "key": "task.status", "value": "completed"},
                    {"pipeline_id": "p1", "key": "track.total_tokens", "value": "10"},
                ],
                "future_tasks": [{"task_id": "task-1", "title": "排队任务"}],
            },
            messages={
                "p1": [
                    {"message_id": "m1", "role": "user", "content": "你好"},
                    {"message_id": "m2", "role": "assistant", "content": "在"},
                ]
            },
        )
        svc = _service(tmp_path, reads)

        result = asyncio.run(svc.run_backup([("alpha", ws)]))

        assert result["ok"] is True
        assert result["db"]["committed"] is True  # 1 session + 1 映射 + 2 消息 + 1 任务 + 1 队列任务
        assert result["workspace"]["committed"] is True
        git_dir = tmp_path / "backup" / "repo.git"
        log = _git_log(git_dir)
        assert len(log) == 2
        assert "workspace" in log[0]
        assert "alpha" in log[0]
        assert "db_records" in log[1]

    def test_records_jsonl_shape_is_t1_logical_records(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("x", encoding="utf-8")
        reads = FakeKernelReads(
            tables={
                "sessions": [{"thread_id": "t1", "title": "T"}],
                "pipeline_sessions": [{"pipeline_id": "p1", "thread_id": "t1"}],
                "pipeline_state": [
                    {"pipeline_id": "p1", "key": "task.id", "value": "p1"},
                    {"pipeline_id": "p1", "key": "subtask.x", "value": "noise"},
                ],
                "future_tasks": [{"task_id": "task-1", "title": "排队"}],
            },
            messages={"p1": [{"message_id": "m1", "role": "user", "content": "全文"}]},
        )
        svc = _service(tmp_path, reads)
        asyncio.run(svc.run_backup([("alpha", ws)]))

        jsonl = _read_jsonl(tmp_path / "backup" / "staging" / "db" / "records.jsonl")
        by_table: dict[str, list[dict[str, Any]]] = {}
        for record in jsonl:
            assert set(record) == {"table", "pk", "row"}, record
            by_table.setdefault(record["table"], []).append(record)
        assert {r["pk"] for r in by_table["sessions"]} == {"t1"}
        assert {r["pk"] for r in by_table["pipeline_sessions"]} == {"p1"}
        # 消息：每消息一对象，pk=message_id，行含全文内容（内核读时重建形态）。
        assert {r["pk"] for r in by_table["messages"]} == {"m1"}
        assert by_table["messages"][0]["row"]["content"] == "全文"
        # 任务：只投影 task.* 前缀键（subtask.* 不入备份），pk=pipeline_id。
        assert {r["pk"] for r in by_table["tasks"]} == {"p1"}
        assert by_table["tasks"][0]["row"] == {"task.id": "p1"}
        assert {r["pk"] for r in by_table["future_tasks"]} == {"task-1"}

    def test_rerun_unchanged_skips_commits(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("x", encoding="utf-8")
        reads = FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]})
        svc = _service(tmp_path, reads)
        first = asyncio.run(svc.run_backup([("alpha", ws)]))
        assert first["ok"] is True

        second = asyncio.run(svc.run_backup([("alpha", ws)]))

        assert second["ok"] is True
        assert second["db"]["committed"] is False
        assert second["db"]["reason"] == "unchanged"
        assert second["workspace"]["committed"] is False
        assert second["workspace"]["reason"] == "unchanged"
        assert len(_git_log(tmp_path / "backup" / "repo.git")) == 2

    def test_increment_and_tombstone_across_runs(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "keep.txt").write_text("k", encoding="utf-8")
        (ws / "gone.txt").write_text("g", encoding="utf-8")
        reads = FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]})
        svc = _service(tmp_path, reads)
        asyncio.run(svc.run_backup([("alpha", ws)]))

        # DB 新增一行 + 工作空间改写与删除（墓碑即提交）。
        reads.tables["sessions"].append({"thread_id": "t2"})
        (ws / "gone.txt").unlink()
        (ws / "keep.txt").write_text("k2", encoding="utf-8")
        second = asyncio.run(svc.run_backup([("alpha", ws)]))

        assert second["db"]["committed"] is True
        assert second["workspace"]["committed"] is True
        git_dir = tmp_path / "backup" / "repo.git"
        log = _git_log(git_dir)
        assert len(log) == 4
        names = _ls_tree(git_dir, log[0].split()[0])
        assert "workspace/alpha/keep.txt" in names
        assert "workspace/alpha/gone.txt" not in names  # 删除已提交（墓碑）

    def test_empty_records_and_empty_workspace_skip(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()  # 空工作空间
        svc = _service(tmp_path, FakeKernelReads(tables={}))  # 空记录
        result = asyncio.run(svc.run_backup([("alpha", ws)]))

        assert result["ok"] is True
        assert result["db"]["committed"] is False
        assert result["db"]["reason"] == "empty"
        assert result["workspace"]["committed"] is False
        assert result["workspace"]["reason"] == "empty"
        assert _git_log(tmp_path / "backup" / "repo.git") == []

    def test_export_failure_means_zero_commits_and_recovery(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("x", encoding="utf-8")

        class ExplodingReads(FakeKernelReads):
            async def list_pipeline_messages(self, pipeline_id: str) -> list[dict[str, Any]]:
                raise RuntimeError("messages.list 内核不可用")

        svc = _service(
            tmp_path,
            ExplodingReads(
                tables={
                    "sessions": [{"thread_id": "t1"}],
                    "pipeline_sessions": [{"pipeline_id": "p1", "thread_id": "t1"}],
                }
            ),
        )
        with pytest.raises(BackupError, match="messages.list"):
            asyncio.run(svc.run_backup([("alpha", ws)]))
        # 失败零 commit（导出先于任何提交，半备份不存在）。
        assert _git_log(tmp_path / "backup" / "repo.git") == []
        # 恢复后重跑不产生幽灵变更（失败现场已清理，暂存区不残留）。
        healthy = _service(tmp_path, FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]}))
        result = asyncio.run(healthy.run_backup([("alpha", ws)]))
        assert result["ok"] is True
        assert result["db"]["committed"] is True
        assert len(_git_log(tmp_path / "backup" / "repo.git")) == 2

    def test_workspace_copy_excludes_runtime_artifacts(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        (ws / ".venv" / "lib").mkdir(parents=True)
        (ws / ".venv" / "lib" / "x.py").write_text("1", encoding="utf-8")
        (ws / "real.txt").write_text("r", encoding="utf-8")
        svc = _service(tmp_path, FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]}))
        asyncio.run(svc.run_backup([("alpha", ws)]))

        git_dir = tmp_path / "backup" / "repo.git"
        names = _ls_tree(git_dir, "refs/heads/backup")
        assert "workspace/alpha/real.txt" in names
        assert not any(n.startswith("workspace/alpha/.venv") for n in names)


# ── 名称与路径校验（显式拒绝，禁静默）──────────────────────────────


class TestWorkspaceValidation:
    @pytest.mark.parametrize(
        ("name", "raw"),
        [
            ("", "/tmp/anywhere"),
            ("../escape", "/tmp/anywhere"),
            ("a/b", "/tmp/anywhere"),
            ("ok-name", "   "),
            ("ok-name", "relative/path"),
        ],
    )
    def test_invalid_specs_rejected_before_git(self, tmp_path: Path, name: str, raw: str) -> None:
        svc = _service(tmp_path, FakeKernelReads())
        with pytest.raises(BackupError):
            asyncio.run(svc.run_backup([(name, Path(raw))]))
        assert not (tmp_path / "backup" / "repo.git" / "HEAD").exists()

    def test_missing_workspace_dir_rejected(self, tmp_path: Path) -> None:
        svc = _service(tmp_path, FakeKernelReads())
        with pytest.raises(BackupError, match="不存在"):
            asyncio.run(svc.run_backup([("alpha", tmp_path / "no-such-dir")]))

    def test_duplicate_workspace_names_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        svc = _service(tmp_path, FakeKernelReads())
        with pytest.raises(BackupError, match="alpha"):
            asyncio.run(svc.run_backup([("alpha", ws), ("alpha", ws)]))


# ── git 缺失 = 备份能力停用诚实报错 ─────────────────────────────────


class TestGitMissing:
    def test_probe_false_when_git_missing(self, tmp_path: Path) -> None:
        svc = BackupService(
            BackupConfig(repo_root=tmp_path / "backup", git_exe="git-missing-xyz"),
            reads=FakeKernelReads(),
        )
        assert svc.probe() is False

    def test_run_backup_raises_honest_error_when_git_missing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        svc = BackupService(
            BackupConfig(repo_root=tmp_path / "backup", git_exe="git-missing-xyz"),
            reads=FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]}),
        )
        with pytest.raises(BackupError, match="git"):
            asyncio.run(svc.run_backup([("alpha", ws)]))


# ── 备份点清单（git log）与恢复（检出）──────────────────────────────


class TestPointsAndRestore:
    def _seed_two_points(self, tmp_path: Path) -> tuple[BackupService, Path]:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("v1", encoding="utf-8")
        reads = FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]})
        svc = _service(tmp_path, reads)
        asyncio.run(svc.run_backup([("alpha", ws)]))
        points = svc.points()
        assert len(points) == 2
        (ws / "a.txt").write_text("v2", encoding="utf-8")
        asyncio.run(svc.run_backup([("alpha", ws)]))
        return svc, ws

    def test_points_lists_log_newest_first(self, tmp_path: Path) -> None:
        svc, _ = self._seed_two_points(tmp_path)
        points = svc.points()
        assert len(points) == 3  # 第一跑 2 commit + 第二跑仅工作空间增量 1 commit
        for point in points:
            assert len(point["sha"]) == 40
            assert point["time"]
            assert point["subject"]
        assert "workspace" in points[0]["subject"]

    def test_points_empty_repo_returns_empty_list(self, tmp_path: Path) -> None:
        svc = _service(tmp_path, FakeKernelReads())
        assert svc.points() == []

    def test_restore_on_empty_repo_raises(self, tmp_path: Path) -> None:
        svc = _service(tmp_path, FakeKernelReads())
        with pytest.raises(BackupError, match="备份点"):
            asyncio.run(svc.restore(commit=None, part="db", dest=tmp_path / "d"))

    def test_restore_workspace_to_older_point(self, tmp_path: Path) -> None:
        svc, ws = self._seed_two_points(tmp_path)
        points = svc.points()
        older = [p for p in points if "workspace" in p["subject"]][-1]["sha"]  # 首跑工作空间点 = v1
        dest = tmp_path / "restored-ws"

        result = asyncio.run(
            svc.restore(commit=older, part="workspace", name="alpha", dest=dest)
        )

        assert result["files"] >= 1
        assert (dest / "a.txt").read_text(encoding="utf-8") == "v1"
        assert (ws / "a.txt").read_text(encoding="utf-8") == "v2"  # 源不受影响

    def test_restore_db_records_materializes_jsonl(self, tmp_path: Path) -> None:
        svc, _ = self._seed_two_points(tmp_path)
        dest = tmp_path / "restored-db"

        result = asyncio.run(svc.restore(commit=None, part="db", dest=dest))

        assert result["files"] == 1
        records = _read_jsonl(dest / "records.jsonl")
        assert {r["pk"] for r in records if r["table"] == "sessions"} == {"t1"}

    def test_restore_refuses_nonempty_dest(self, tmp_path: Path) -> None:
        svc, _ = self._seed_two_points(tmp_path)
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "some.txt").write_text("x", encoding="utf-8")
        with pytest.raises(BackupError, match="非空"):
            asyncio.run(svc.restore(commit=None, part="db", dest=dest))

    def test_restore_unknown_commit_and_workspace(self, tmp_path: Path) -> None:
        svc, _ = self._seed_two_points(tmp_path)
        with pytest.raises(BackupError):
            asyncio.run(
                svc.restore(commit="0" * 40, part="workspace", name="alpha", dest=tmp_path / "d1")
            )
        with pytest.raises(BackupError):
            asyncio.run(
                svc.restore(commit=None, part="workspace", name="no-such", dest=tmp_path / "d2")
            )

    def test_restore_workspace_creates_dest_parents(self, tmp_path: Path) -> None:
        svc, _ = self._seed_two_points(tmp_path)
        dest = tmp_path / "deep" / "nested" / "dest"
        asyncio.run(svc.restore(commit=None, part="workspace", name="alpha", dest=dest))
        assert (dest / "a.txt").read_text(encoding="utf-8") == "v2"


# ── 状态面 ────────────────────────────────────────────────────────


class TestStatus:
    def test_status_reflects_configured_and_head(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("x", encoding="utf-8")
        svc = _service(tmp_path, FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]}))
        before = svc.status()
        assert before["git_available"] is True
        assert before["head"] == ""
        asyncio.run(svc.run_backup([("alpha", ws)]))
        after = svc.status()
        assert len(after["head"]) == 12
        assert after["commits"] == 2
        assert after["repo_root"]


# ── server 路由面（端点分发 + 未配置诚实态 + 端口身份链接线）────────────


class FakeBackupService:
    """路由测试替身：记录调用与 reads 注入（路由接线是被测面）。"""

    def __init__(self) -> None:
        self.run_calls: list[Any] = []
        self.run_reads: Any = None
        self.restore_calls: list[Any] = []

    async def run_backup(self, specs: Any, *, reads: Any = None) -> dict[str, Any]:
        self.run_calls.append(specs)
        self.run_reads = reads
        return {"ok": True, "db": {"committed": True, "commit": "a" * 12}, "head": "a" * 12}

    def status(self) -> dict[str, Any]:
        return {"git_available": True, "repo_root": "/x", "head": "a" * 12, "commits": 1}

    def points(self) -> list[dict[str, Any]]:
        return [{"sha": "a" * 40, "time": "2026-10-17T00:00:00+00:00", "subject": "s"}]

    async def restore(self, **kwargs: Any) -> dict[str, Any]:
        self.restore_calls.append(kwargs)
        return {"commit": "a" * 12, "part": kwargs["part"], "dest": kwargs["dest"], "files": 1}


class TestServerRoutes:
    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Any:
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import server as agent_server

        return importlib.reload(agent_server)

    def _unwrap(self, resp: dict[str, Any]) -> dict[str, Any]:
        data = resp["data"]
        body = json.loads(base64.b64decode(data["body"]))
        return {"status": data["status"], "body": body}

    def test_unconfigured_backup_routes_are_honest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(monkeypatch, tmp_path)
        asyncio.run(module._on_load({}))
        assert module._assembly is not None
        assert module._assembly.backup is None
        run = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/run",
                    method="POST",
                    raw_body='{"workspaces": []}',
                )
            )
        )
        assert run["status"] == 503
        status = self._unwrap(
            asyncio.run(module.http_handle(path="/ext/sync_agent/backup/status", method="GET"))
        )
        assert status["body"] == {"configured": False}
        points = self._unwrap(
            asyncio.run(module.http_handle(path="/ext/sync_agent/backup/points", method="GET"))
        )
        assert points["status"] == 503
        restore = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/restore",
                    method="POST",
                    raw_body='{"part": "db", "dest": "/tmp/out"}',
                )
            )
        )
        assert restore["status"] == 503

    def test_configured_routes_reach_service_with_request_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(
            monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT=str(tmp_path / "backup")
        )
        asyncio.run(module._on_load({}))
        assert module._assembly is not None
        assert module._assembly.backup is not None
        fake = FakeBackupService()
        module._assembly.backup = fake

        run = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/run",
                    method="POST",
                    raw_body='{"workspaces": [{"name": "alpha", "path": "' + str(tmp_path / "ws").replace("\\", "\\\\") + '"}]}',
                    headers={"authorization": "Bearer admin-jwt"},
                )
            )
        )
        assert run["status"] == 200
        assert run["body"]["ok"] is True
        assert fake.run_calls == [[("alpha", str(tmp_path / "ws"))]]
        assert fake.run_reads is not None  # 每请求身份链端口已注入

        status = self._unwrap(
            asyncio.run(module.http_handle(path="/ext/sync_agent/backup/status", method="GET"))
        )
        assert status["body"]["configured"] is True
        assert status["body"]["commits"] == 1
        points = self._unwrap(
            asyncio.run(module.http_handle(path="/ext/sync_agent/backup/points", method="GET"))
        )
        assert len(points["body"]["points"]) == 1
        restore = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/restore",
                    method="POST",
                    raw_body='{"part": "db", "dest": "' + str(tmp_path / "out").replace("\\", "\\\\") + '"}',
                )
            )
        )
        assert restore["status"] == 200
        assert restore["body"]["files"] == 1

    def test_run_body_validation_rejects_malformed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(
            monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT=str(tmp_path / "backup")
        )
        asyncio.run(module._on_load({}))
        bad = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/run",
                    method="POST",
                    raw_body='{"workspaces": [{"name": "alpha"}]}',
                )
            )
        )
        assert bad["status"] == 400
        not_object = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/run", method="POST", raw_body="[1]"
                )
            )
        )
        assert not_object["status"] == 400


# ── 真实故障形态与诚实降级（对齐 file_channel 三故障形态先例）────────────


class TestFaultForms:
    def _seed(self, tmp_path: Path) -> tuple[BackupService, Path, FakeKernelReads]:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "a.txt").write_text("v1", encoding="utf-8")
        reads = FakeKernelReads(tables={"sessions": [{"thread_id": "t1"}]})
        svc = _service(tmp_path, reads)
        asyncio.run(svc.run_backup([("alpha", ws)]))
        return svc, ws, reads

    def test_run_without_reads_port_raises(self, tmp_path: Path) -> None:
        svc = BackupService(BackupConfig(repo_root=tmp_path / "backup"))
        with pytest.raises(BackupError, match="端口未注入"):
            asyncio.run(svc.run_backup([("alpha", tmp_path)]))

    def test_repo_root_under_file_reports_init_failure(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")
        svc = BackupService(BackupConfig(repo_root=blocker / "backup"), reads=FakeKernelReads())
        with pytest.raises(BackupError, match="初始化失败"):
            asyncio.run(svc.run_backup([("alpha", tmp_path)]))

    def test_add_failure_lock_residue_propagates(self, tmp_path: Path) -> None:
        svc, ws, _ = self._seed(tmp_path)
        git_dir = tmp_path / "backup" / "repo.git"
        (git_dir / "index.lock").write_bytes(b"")
        (ws / "a.txt").write_text("v2", encoding="utf-8")
        with pytest.raises(BackupError, match="暂存失败"):
            asyncio.run(svc.run_backup([("alpha", ws)]))

    def test_status_check_failure_config_corruption_propagates(self, tmp_path: Path) -> None:
        svc, ws, _ = self._seed(tmp_path)
        git_dir = tmp_path / "backup" / "repo.git"
        subprocess.run(
            ["git", "--git-dir", str(git_dir), "config", "status.showUntrackedFiles", "bogus"],
            capture_output=True,
            check=True,
        )
        (ws / "a.txt").write_text("v2", encoding="utf-8")
        with pytest.raises(BackupError, match="状态检查失败"):
            asyncio.run(svc.run_backup([("alpha", ws)]))

    def test_commit_failure_propagates_and_staging_resets(self, tmp_path: Path) -> None:
        """pre-commit 拒绝（真实故障形态）→ 显式报错 + 现场复位，无幽灵变更。"""
        svc, ws, _ = self._seed(tmp_path)
        git_dir = tmp_path / "backup" / "repo.git"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        # POSIX git 只执行带执行位的钩子（Windows 无此语义）：不 chmod 则
        # Linux 上钩子被静默跳过 → commit 成功 → 故障形态无法复现
        os.chmod(hooks / "pre-commit", 0o755)
        subprocess.run(
            ["git", "--git-dir", str(git_dir), "config", "core.hooksPath", str(hooks)],
            capture_output=True,
            check=True,
        )
        (ws / "a.txt").write_text("v2", encoding="utf-8")
        with pytest.raises(BackupError, match="备份提交失败"):
            asyncio.run(svc.run_backup([("alpha", ws)]))
        # 现场已复位：摘钩后重跑，提交数 = 原有 2 + 新鲜 1（无幽灵第三态）。
        subprocess.run(
            ["git", "--git-dir", str(git_dir), "config", "--unset", "core.hooksPath"],
            capture_output=True,
            check=True,
        )
        result = asyncio.run(svc.run_backup([("alpha", ws)]))
        assert result["db"]["reason"] == "unchanged"
        assert result["workspace"]["committed"] is True
        assert len(_git_log(git_dir)) == 3

    def test_restore_validation_branches(self, tmp_path: Path) -> None:
        svc, _, _ = self._seed(tmp_path)
        with pytest.raises(BackupError, match="part"):
            asyncio.run(svc.restore(commit=None, part="bogus", dest=tmp_path / "d1"))
        with pytest.raises(BackupError, match="名违约"):
            asyncio.run(
                svc.restore(commit=None, part="workspace", name="../x", dest=tmp_path / "d2")
            )
        with pytest.raises(BackupError, match="绝对路径"):
            asyncio.run(svc.restore(commit=None, part="db", dest="relative/dest"))
        dest_file = tmp_path / "afile.txt"
        dest_file.write_text("x", encoding="utf-8")
        with pytest.raises(BackupError, match="非目录"):
            asyncio.run(svc.restore(commit=None, part="db", dest=dest_file))

    def test_restore_on_zero_commit_repo_raises(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        svc = _service(tmp_path, FakeKernelReads(tables={}))
        asyncio.run(svc.run_backup([("alpha", ws)]))  # 全空 → 仓库已建但零 commit
        with pytest.raises(BackupError, match="尚无备份点"):
            asyncio.run(svc.restore(commit=None, part="db", dest=tmp_path / "d"))

    def test_restore_honest_with_git_missing(self, tmp_path: Path) -> None:
        svc = BackupService(
            BackupConfig(repo_root=tmp_path / "backup", git_exe="git-missing-xyz"),
            reads=FakeKernelReads(),
        )
        with pytest.raises(BackupError, match="停用"):
            asyncio.run(svc.restore(commit=None, part="db", dest=tmp_path / "d"))

    def test_status_and_points_honest_with_git_missing(self, tmp_path: Path) -> None:
        svc = BackupService(
            BackupConfig(repo_root=tmp_path / "backup", git_exe="git-missing-xyz"),
            reads=FakeKernelReads(),
        )
        status = svc.status()
        assert status["git_available"] is False
        assert status["head"] == ""
        assert status["commits"] == 0
        assert svc.points() == []

    def test_stderr_of_decodes_binary_form(self, tmp_path: Path) -> None:
        from git_cli import stderr_of

        git_dir = tmp_path / "r.git"
        ensure_bare_repo(git_dir, "refs/heads/backup", "dev-x", RUNTIME_EXCLUDES)
        proc = run_git(
            "git", git_dir, None, "rev-parse", "--verify", "--quiet", "nope", binary=True
        )
        assert isinstance(stderr_of(proc), str)


# ── server 端口实现（每请求身份链 + 信封裁决）────────────────────────


class FakeCapabilityHandle:
    """capability 句柄替身：method → 信封/结果脚本（外部内核能力边界）。"""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        result = self.script[method]
        # 信封序列 = 分页脚本（逐调用弹出下一页）；其余原样回传（如消息数组）。
        if (
            isinstance(result, list)
            and bool(result)
            and all(isinstance(x, dict) and "status" in x for x in result)
        ):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestBackupKernelReads:
    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        import server as agent_server

        return importlib.reload(agent_server)

    def test_table_rows_pagination_and_identity_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(monkeypatch, tmp_path)
        handle = FakeCapabilityHandle(
            {
                "table_query": [
                    {"status": 200, "body": {"rows": [{"pk": "a"}, {"pk": "b"}], "total": 3}},
                    {"status": 200, "body": {"rows": [{"pk": "c"}], "total": 3}},
                ]
            }
        )
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle)
        reads = module._BackupKernelReads("Bearer jwt-x")

        rows = asyncio.run(reads.list_table_rows("sessions"))

        assert [r["pk"] for r in rows] == ["a", "b", "c"]
        assert len(handle.calls) == 2
        assert handle.calls[0][1]["offset"] == 0
        assert handle.calls[1][1]["offset"] == 2
        assert handle.calls[0][1]["_authorization"] == "Bearer jwt-x"

    def test_table_rows_error_and_malformed_envelopes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(monkeypatch, tmp_path)
        handle = FakeCapabilityHandle(
            {"table_query": [{"status": 500, "error": {"code": "x", "message": "内核炸了"}}]}
        )
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle)
        reads = module._BackupKernelReads("")
        with pytest.raises(RuntimeError, match="内核炸了"):
            asyncio.run(reads.list_table_rows("sessions"))

        handle2 = FakeCapabilityHandle({"table_query": {"status": 200, "body": {"nope": 1}}})
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle2)
        with pytest.raises(RuntimeError, match="rows"):
            asyncio.run(reads.list_table_rows("sessions"))

        handle3 = FakeCapabilityHandle({"table_query": "junk"})
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle3)
        with pytest.raises(RuntimeError, match="异常信封"):
            asyncio.run(reads.list_table_rows("sessions"))

    def test_messages_list_filters_and_rejects_non_array(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(monkeypatch, tmp_path)
        handle = FakeCapabilityHandle({"messages.list": [{"message_id": "m1"}, "junk", 3]})
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle)
        reads = module._BackupKernelReads("")
        rows = asyncio.run(reads.list_pipeline_messages("p1"))
        assert rows == [{"message_id": "m1"}]

        handle2 = FakeCapabilityHandle({"messages.list": {"unexpected": True}})
        monkeypatch.setattr(module.plugin, "get_capability", lambda _: handle2)
        with pytest.raises(RuntimeError, match="非数组"):
            asyncio.run(reads.list_pipeline_messages("p1"))


class TestServerAssemblyBranches:
    def _load(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Any:
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import server as agent_server

        return importlib.reload(agent_server)

    def _unwrap(self, resp: dict[str, Any]) -> dict[str, Any]:
        data = resp["data"]
        body = json.loads(base64.b64decode(data["body"]))
        return {"status": data["status"], "body": body}

    def test_relative_backup_root_resolves_against_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        module = self._load(monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT="rel/backup")
        service = module._build_backup({})
        assert service is not None
        assert Path(service.repo_root).is_absolute()

    def test_unbuildable_root_disables_backup_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")
        module = self._load(
            monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT=str(blocker / "backup")
        )
        assert module._build_backup({}) is None

    def test_restore_route_error_maps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(
            monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT=str(tmp_path / "backup")
        )
        asyncio.run(module._on_load({}))
        assert module._assembly is not None

        for raw, expect in [
            ("not-json", 400),
            ('{"part": "bogus", "dest": "/tmp/x"}', 400),
            ('{"part": "db", "dest": ""}', 400),
        ]:
            resp = self._unwrap(
                asyncio.run(
                    module.http_handle(
                        path="/ext/sync_agent/backup/restore", method="POST", raw_body=raw
                    )
                )
            )
            assert resp["status"] == expect, raw

    def test_run_and_points_error_maps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = self._load(
            monkeypatch, tmp_path, AGENTOS_SYNC_BACKUP_ROOT=str(tmp_path / "backup")
        )
        asyncio.run(module._on_load({}))
        assert module._assembly is not None
        module._assembly.backup = FakeBackupService()

        # workspaces 非数组 / 项畸形 → 400（_parse_workspace_specs 拒绝面）
        for raw in ('{"workspaces": "nope"}', '{"workspaces": [1]}'):
            resp = self._unwrap(
                asyncio.run(
                    module.http_handle(
                        path="/ext/sync_agent/backup/run", method="POST", raw_body=raw
                    )
                )
            )
            assert resp["status"] == 400, raw

        class BoomService(FakeBackupService):
            async def run_backup(self, specs: Any, *, reads: Any = None) -> dict[str, Any]:
                raise BackupError("域错误", 422)

            def points(self) -> list[dict[str, Any]]:
                raise BackupError("清单错误", 500)

            async def restore(self, **kwargs: Any) -> dict[str, Any]:
                raise BackupError("检出错误", 409)

        module._assembly.backup = BoomService()
        run = self._unwrap(
            asyncio.run(
                module.http_handle(path="/ext/sync_agent/backup/run", method="POST", raw_body="{}")
            )
        )
        assert run["status"] == 422
        points = self._unwrap(
            asyncio.run(module.http_handle(path="/ext/sync_agent/backup/points", method="GET"))
        )
        assert points["status"] == 500
        restore = self._unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/backup/restore",
                    method="POST",
                    raw_body='{"part": "db", "dest": "/tmp/out2"}',
                )
            )
        )
        assert restore["status"] == 409
