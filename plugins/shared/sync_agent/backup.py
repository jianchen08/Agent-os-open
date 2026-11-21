"""手动备份服务 —— DB 逻辑记录 + 指定工作空间 → 本地 git 仓库（T6，拍板③a）。

语义（台账 R7）：**commit 即备份点**——增量存储（git 对象去重，零变更零
commit）、任意时点回溯（git log 清单）、可恢复（从备份点检出到干净目标）。
一次触发两件事各产一个 commit（单线历史 ``refs/heads/backup``）：①DB 会话/
任务逻辑记录导出（JSONL，T1 逻辑记录形态 ``{table, pk, row}``）→ db commit；
②指定工作空间目录 → workspace commit（删除=墓碑即提交）。

git 原语与 file 通道（T2）同源（``git_cli`` 公共件：裸仓库+分离 work-tree，
抽公共件不整段复制）。备份仓库落 settings 配置的根路径（data/ 语义层——
永不同步，D4）。

内核读面为注入端口（:class:`KernelReads`；server.py 以 db-admin table_query /
service-registry messages.list 装配）——本模块零内核知识、零网络依赖（离线
第一：备份不要求配对/relay 可达）。

诚实语义（任务书第 3 条，全部登记在台账）：
- git 可执行缺失 = 备份能力停用，显式报错禁静默；
- 空记录/空工作空间 = 诚实跳过（首备份无内容可提交；已备后清空 = 墓碑提交，
  与 R7 墓碑语义一致）；
- 导出失败 = 零 commit（导出先于任何提交，半备份不存在）+ 现场清理
  （暂存区复位到 HEAD）；
- 恢复目标目录非空 = 显式拒绝（禁覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from git_cli import (
    RUNTIME_EXCLUDES,
    GitCommandError,
    ensure_bare_repo,
    run_git,
    stderr_of,
)

logger = logging.getLogger(__name__)

#: 备份分支（单线历史；备份点 = 该分支上的 commit）。
BACKUP_REF = "refs/heads/backup"

#: 工作空间登记名（git 路径安全：禁空名/路径分隔/穿越）。
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 工作空间复制面跳过的运行时产物（与仓库 info/exclude 同源，少拷无用 I/O）。
_COPY_IGNORES = shutil.ignore_patterns(
    *(pattern.rstrip("/") for pattern in RUNTIME_EXCLUDES)
)

#: db-admin table_query 单页上限（内核 clamp 1..500）。
_TABLE_PAGE = 500


class BackupError(RuntimeError):
    """备份操作失败（显式报错禁静默；status 供 HTTP 信封映射）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BackupConfig:
    """备份装配参数（server.py 从 settings/env 解析）。"""

    repo_root: Path
    git_exe: str = "git"
    timeout_secs: float = 120.0


class KernelReads(Protocol):  # pragma: no cover —— Protocol 方法体为样板声明，无运行时面
    """内核读端口（db-admin table_query / service-registry messages.list 供给）。"""

    async def list_table_rows(self, table: str) -> list[dict[str, Any]]:
        """整表行（分页拉平；db-admin 租户收窄随身份链）。"""
        ...

    async def list_pipeline_messages(self, pipeline_id: str) -> list[dict[str, Any]]:
        """单管道消息全文行（内核读时重建形态，MessageRecord 字典）。"""
        ...


@dataclass(frozen=True)
class _WorkspaceSpec:
    """校验后的工作空间登记（名称 = 备份仓库内 ``workspace/<name>`` 子树）。"""

    name: str
    path: Path


class BackupService:
    """备份仓库原语 + 导出/提交/清单/检出编排（端点触发，非 tick 循环面）。

    ``reads`` 为装配期缺省端口；每次触发可经 ``run_backup(reads=…)`` 换入
    携带当次身份链的端口实现（db-admin 契约：身份经 _authorization 由内核
    复权，不能装配期定死）。
    """

    def __init__(self, config: BackupConfig, *, reads: KernelReads | None = None) -> None:
        self._cfg = config
        self._reads = reads
        self._lock = asyncio.Lock()

    # ── 仓库坐标 ─────────────────────────────────────────────

    @property
    def repo_root(self) -> Path:
        return self._cfg.repo_root

    @property
    def git_dir(self) -> Path:
        return self._cfg.repo_root / "repo.git"

    @property
    def staging(self) -> Path:
        """分离 work-tree 落点（data/ 私有暂存区；每次备份刷新到当前真值）。"""
        return self._cfg.repo_root / "staging"

    # ── 探测与状态面 ─────────────────────────────────────────

    def probe(self) -> bool:
        """git 可执行可用性（缺失 = 备份能力停用，T2 先例同律）。"""
        try:
            return run_git(self._cfg.git_exe, None, None, "--version").returncode == 0
        except GitCommandError:
            return False

    def status(self) -> dict[str, Any]:
        """状态面（GET /backup/status 消费）。"""
        head = self._head_sha()
        return {
            "git_available": self.probe(),
            "repo_root": str(self.repo_root),
            "head": (head or "")[:12],
            "commits": self._commit_count(),
        }

    def points(self, limit: int = 200) -> list[dict[str, Any]]:
        """备份点清单（git log，新→旧）：sha/时刻/主题。空历史 = 空表。"""
        if self._head_sha() is None:
            return []
        proc = self._gx(
            self.git_dir,
            None,
            "log",
            f"--max-count={int(limit)}",
            "--format=%H%x1f%aI%x1f%s",
            BACKUP_REF,
        )
        if proc.returncode != 0:  # pragma: no cover —— head 在场时 log 失败=仓库损坏瞬态（防御面）
            raise BackupError(f"备份点清单读取失败: {stderr_of(proc)[:200]}", 500)
        result: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                result.append({"sha": parts[0], "time": parts[1], "subject": parts[2]})
        return result

    # ── 备份执行 ─────────────────────────────────────────────

    async def run_backup(
        self,
        workspaces: list[tuple[str, str | Path]],
        *,
        reads: KernelReads | None = None,
    ) -> dict[str, Any]:
        """一次触发 = db commit + workspace commit（各部分独立可跳过）。

        导出先于任何提交：内核读失败 = 零 commit + 暂存区复位（半备份不存在）。
        git 子进程与整树复制是同步阻塞体，一律 ``asyncio.to_thread`` 出事件循环
        （宿主 watchdog 判心跳停滞即整组退出）；``self._lock`` 是协程侧
        ``asyncio.Lock``，只包 await，不随同步体进线程。
        """
        effective_reads = reads if reads is not None else self._reads
        if effective_reads is None:
            raise BackupError("内核读端口未注入（备份导出面不可用）", 500)
        specs = self._validate_workspaces(workspaces)
        async with self._lock:
            # 仓库探测/初始化与 commit 段同锁：同步体下放线程后多触发会真并发，
            # 裸仓初始化须串行（同实例串行是既有语义）。
            if not await asyncio.to_thread(self.probe):
                raise BackupError(
                    f"备份能力停用：git 可执行 {self._cfg.git_exe!r} 不可用（装机 git 依赖，"
                    "T2 先例同律）",
                    503,
                )
            try:
                await asyncio.to_thread(
                    ensure_bare_repo, self.git_dir, BACKUP_REF, "agentos", RUNTIME_EXCLUDES
                )
            except GitCommandError as exc:
                raise BackupError(f"备份仓库初始化失败: {exc}", 500) from exc
            try:
                await asyncio.to_thread(self.staging.mkdir, parents=True, exist_ok=True)
                records = await self._export_records(effective_reads)
                db_result = await asyncio.to_thread(self._commit_db_part, records)
                ws_result = await asyncio.to_thread(self._commit_workspace_part, specs)
            except BackupError:
                await asyncio.to_thread(self._cleanup_staging)
                raise
            except Exception as exc:
                await asyncio.to_thread(self._cleanup_staging)
                raise BackupError(
                    f"备份失败（导出/暂存面）: {type(exc).__name__}: {exc}", 500
                ) from exc
        head = await asyncio.to_thread(self._head_sha)
        return {
            "ok": True,
            "db": db_result,
            "workspace": ws_result,
            "head": (head or "")[:12],
            "repo_root": str(self._cfg.repo_root),
        }

    # ── 恢复（从备份点检出）────────────────────────────────────

    async def restore(
        self,
        *,
        commit: str | None,
        part: str,
        dest: str | Path,
        name: str = "",
    ) -> dict[str, Any]:
        """备份点内容检出（workspace 部分 = 真恢复路径；db 部分 = JSONL 物化，
        回灌内核的导入面为后续刀候选，本刀登记）。目标目录必须不存在或为空。

        检出全链（git read-tree/checkout-index + 全树计数）是同步阻塞体，
        ``asyncio.to_thread`` 出事件循环，返回/异常语义不变。
        """
        return await asyncio.to_thread(
            self._restore_sync, commit=commit, part=part, dest=dest, name=name
        )

    def _restore_sync(
        self,
        *,
        commit: str | None,
        part: str,
        dest: str | Path,
        name: str = "",
    ) -> dict[str, Any]:
        if not self.probe():
            raise BackupError(
                f"备份能力停用：git 可执行 {self._cfg.git_exe!r} 不可用", 503
            )
        if not (self.git_dir / "HEAD").exists():
            raise BackupError("备份仓库尚无备份点", 404)
        sha = self._resolve_commit(commit)
        subpath = self._resolve_subtree(sha, part, name)
        target = self._validate_dest(dest)
        target.mkdir(parents=True, exist_ok=True)
        index_file = self._cfg.repo_root / ".restore-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
        try:
            read_tree = self._gx(self.git_dir, None, "read-tree", f"{sha}:{subpath}", env=env)
            if read_tree.returncode != 0:  # pragma: no cover —— 子树已 rev-parse 验证在场，read-tree 失败=瞬态损坏（防御面）
                raise BackupError(
                    f"备份点 {sha[:12]} 无 {part} 内容可检出: {stderr_of(read_tree)[:200]}", 404
                )
            checkout = self._gx(
                self.git_dir,
                target,
                "checkout-index",
                "-a",
                "-f",
                f"--prefix={target.as_posix()}/",
                env=env,
            )
            if checkout.returncode != 0:  # pragma: no cover —— read-tree 成功后专用空目录检出不失败（防御分支）
                raise BackupError(f"检出到 {target} 失败: {stderr_of(checkout)[:200]}", 500)
        finally:
            index_file.unlink(missing_ok=True)
        files = sum(1 for p in target.rglob("*") if p.is_file())
        return {"commit": sha[:12], "part": part, "dest": str(target), "files": files}

    # ── 导出（T1 逻辑记录形态）─────────────────────────────────

    async def _export_records(self, reads: KernelReads) -> list[dict[str, Any]]:
        """内核 DB → 逻辑记录流：sessions/pipeline_sessions/messages/tasks/
        future_tasks（会话数据 = 会话行 + 归属映射 + 消息全文；任务 = task.*
        投影 + 任务队列）。失败上抛（零 commit 保证的前提）。"""
        sessions = await reads.list_table_rows("sessions")
        memberships = await reads.list_table_rows("pipeline_sessions")
        state_rows = await reads.list_table_rows("pipeline_state")
        future_tasks = await reads.list_table_rows("future_tasks")
        records: list[dict[str, Any]] = []
        records += self._table_records("sessions", sessions, "thread_id")
        records += self._table_records("pipeline_sessions", memberships, "pipeline_id")
        pipeline_ids = sorted(
            {str(row.get("pipeline_id") or "") for row in memberships} - {""}
        )
        for pipeline_id in pipeline_ids:
            for row in await reads.list_pipeline_messages(pipeline_id):
                records.append(
                    {
                        "table": "messages",
                        "pk": str(row.get("message_id") or ""),
                        "row": row,
                    }
                )
        records += self._task_records(state_rows)
        records += self._table_records("future_tasks", future_tasks, "task_id")
        return records

    @staticmethod
    def _table_records(
        table: str, rows: list[dict[str, Any]], pk_field: str
    ) -> list[dict[str, Any]]:
        return [
            {"table": table, "pk": str(row.get(pk_field) or ""), "row": row} for row in rows
        ]

    @staticmethod
    def _task_records(state_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """pipeline_state 行 → T1 任务记录形态（每管道一行 task.* 键集投影；
        非任务键不入备份）。"""
        by_pipeline: dict[str, dict[str, Any]] = {}
        for row in state_rows:
            key = str(row.get("key") or "")
            if not key.startswith("task."):
                continue
            pipeline_id = str(row.get("pipeline_id") or "")
            by_pipeline.setdefault(pipeline_id, {})[key] = row.get("value")
        return [
            {"table": "tasks", "pk": pipeline_id, "row": snapshot}
            for pipeline_id, snapshot in sorted(by_pipeline.items())
        ]

    # ── commit 两臂 ──────────────────────────────────────────

    def _commit_db_part(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """① DB 记录 commit：全量导出覆写暂存（git 对象去重即增量）。"""
        jsonl_path = self.staging / "db" / "records.jsonl"
        if records:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            lines = "".join(
                json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records
            )
            _atomic_write_text(jsonl_path, lines)
        else:
            jsonl_path.unlink(missing_ok=True)  # 已备后清空 = 墓碑提交；首备 = 状态空跳过
        sha = self._commit_path("db", f"backup: db_records {len(records)} record(s)")
        return self._part_result(sha, empty=len(records) == 0, records=len(records))

    def _commit_workspace_part(self, specs: list[_WorkspaceSpec]) -> dict[str, Any]:
        """② 工作空间 commit：暂存刷新（删除传播=墓碑）→ 单 commit 覆全部登记。"""
        total_files = 0
        for spec in specs:
            target = self.staging / "workspace" / spec.name
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(spec.path, target, ignore=_COPY_IGNORES)
            total_files += sum(1 for p in target.rglob("*") if p.is_file())
        names = " ".join(spec.name for spec in specs)
        sha = self._commit_path("workspace", f"backup: workspace {names}")
        return self._part_result(sha, empty=total_files == 0, files=total_files, names=names)

    def _commit_path(self, prefix: str, subject: str) -> str | None:
        """路径限定的 add+status+commit（零变更返回 None；失败显式翻译）。

        前置守卫：暂存与 HEAD 都无该前缀内容时跳过（首备空部分连 pathspec
        都不存在，``git add`` 会以 fatal 拒绝——那不是故障是无物可提交）。
        """
        if not self._prefix_has_content(prefix):
            return None
        add = self._gx(self.git_dir, self.staging, "add", "-A", "--", prefix)
        if add.returncode != 0:
            raise BackupError(f"备份暂存失败 ({prefix}): {stderr_of(add)[:200]}", 500)
        status = self._gx(self.git_dir, self.staging, "status", "--porcelain", "--", prefix)
        if status.returncode != 0:
            raise BackupError(f"备份状态检查失败 ({prefix}): {stderr_of(status)[:200]}", 500)
        if not status.stdout.strip():
            return None
        commit = self._gx(self.git_dir, self.staging, "commit", "-q", "-m", subject)
        if commit.returncode != 0:
            raise BackupError(f"备份提交失败 ({prefix}): {stderr_of(commit)[:200]}", 500)
        head = self._head_sha()
        if head is None:  # pragma: no cover —— commit 成功后必有分支头（git 后置不变量）
            raise BackupError(f"备份提交后无分支头 ({prefix}，仓库状态异常)", 500)
        return head

    def _prefix_has_content(self, prefix: str) -> bool:
        """暂存区该前缀下有文件，或 HEAD 历史有该子树（墓碑提交的前提）。"""
        base = self.staging / prefix
        if any(p.is_file() for p in base.rglob("*")):
            return True
        check = self._gx(
            self.git_dir, None, "rev-parse", "--verify", "--quiet", f"{BACKUP_REF}:{prefix}"
        )
        return check.returncode == 0

    @staticmethod
    def _part_result(
        sha: str | None, *, empty: bool, **extra: Any
    ) -> dict[str, Any]:
        """部分结果收形：committed=False 时附诚实原因（empty=首备无内容）。"""
        result: dict[str, Any] = {"committed": sha is not None}
        if sha is not None:
            result["commit"] = sha[:12]
        elif empty:
            result["reason"] = "empty"
        else:
            result["reason"] = "unchanged"
        result.update(extra)
        return result

    # ── 恢复辅助 ─────────────────────────────────────────────

    def _resolve_commit(self, commit: str | None) -> str:
        if commit is None or not commit.strip():
            sha = self._head_sha()
            if sha is None:
                raise BackupError("备份仓库尚无备份点", 404)
            return sha
        verify = self._gx(
            self.git_dir, None, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"
        )
        if verify.returncode != 0:
            raise BackupError(f"备份点不存在: {commit}", 404)
        return verify.stdout.strip()

    def _resolve_subtree(self, sha: str, part: str, name: str) -> str:
        """备份点内的检出子树：db → ``db``；workspace → ``workspace/<name>``。"""
        if part == "db":
            subpath = "db"
        elif part == "workspace":
            if not _WORKSPACE_NAME_RE.fullmatch(name or ""):
                raise BackupError(f"工作空间名违约: {name!r}（空名/路径分隔/穿越拒绝）", 400)
            subpath = f"workspace/{name}"
        else:
            raise BackupError(f"part 必须为 db|workspace，得到 {part!r}", 400)
        check = self._gx(
            self.git_dir, None, "rev-parse", "--verify", "--quiet", f"{sha}:{subpath}"
        )
        if check.returncode != 0:
            raise BackupError(f"备份点 {sha[:12]} 无 {subpath} 子树", 404)
        return subpath

    @staticmethod
    def _validate_dest(dest: str | Path) -> Path:
        target = Path(dest)
        if not target.is_absolute():
            raise BackupError("恢复目标必须是绝对路径", 400)
        if target.exists():
            if not target.is_dir():
                raise BackupError(f"恢复目标已存在且非目录: {target}", 400)
            if any(target.iterdir()):
                raise BackupError(f"恢复目标非空，拒绝覆盖: {target}", 400)
        return target

    # ── 校验与内部 ───────────────────────────────────────────

    @staticmethod
    def _validate_workspaces(
        workspaces: list[tuple[str, str | Path]],
    ) -> list[_WorkspaceSpec]:
        specs: list[_WorkspaceSpec] = []
        seen: set[str] = set()
        for name, raw in workspaces:
            if not _WORKSPACE_NAME_RE.fullmatch(name or ""):
                raise BackupError(f"工作空间名违约: {name!r}（空名/路径分隔/穿越拒绝）", 400)
            if name in seen:
                raise BackupError(f"工作空间名重复: {name}", 400)
            seen.add(name)
            path = Path(raw)
            if not path.is_absolute():
                raise BackupError(f"工作空间路径必须是绝对路径: {name} → {raw!r}", 400)
            if not path.is_dir():
                raise BackupError(f"工作空间目录不存在: {name} → {path}", 400)
            specs.append(_WorkspaceSpec(name=name, path=path))
        return specs

    def _head_sha(self) -> str | None:
        """备份分支头 sha；零历史 / git 不可执行（观测面诚实降级）返回 None。"""
        try:
            proc = run_git(
                self._cfg.git_exe,
                self.git_dir,
                None,
                "rev-parse",
                "--verify",
                "--quiet",
                BACKUP_REF,
                timeout_secs=self._cfg.timeout_secs,
            )
        except GitCommandError:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _commit_count(self) -> int:
        if self._head_sha() is None:
            return 0
        proc = self._gx(self.git_dir, None, "rev-list", "--count", BACKUP_REF)
        return int(proc.stdout.strip() or 0)

    def _gx(self, git_dir: Path | None, work_tree: Path | None, *args: str, env: dict[str, str] | None = None) -> Any:
        """run_git 翻译 wrapper：进程级故障 → BackupError（返回码留给调用方裁决）。"""
        try:
            return run_git(
                self._cfg.git_exe,
                git_dir,
                work_tree,
                *args,
                timeout_secs=self._cfg.timeout_secs,
                env=env,
            )
        except GitCommandError as exc:  # pragma: no cover —— 进程级故障面（exe 消失/超时），调用方均已裁决返回码（防御面）
            raise BackupError(str(exc), 503) from exc

    def _cleanup_staging(self) -> None:
        """失败现场清理：暂存区复位到 HEAD（无历史则整体清除），防幽灵变更。"""
        try:
            if self._head_sha() is not None:
                self._gx(self.git_dir, self.staging, "reset", "--quiet", "--hard", "HEAD")
                self._gx(self.git_dir, self.staging, "clean", "-qfd")
            else:
                shutil.rmtree(self.staging, ignore_errors=True)
        except BackupError:  # pragma: no cover —— 清理是善后面，原始异常优先
            logger.warning("[backup] 暂存区复位失败（不影响本次错误上报）", exc_info=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写（临时文件 + replace，JSONL 全量覆写面）。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
