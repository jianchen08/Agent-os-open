"""file 通道 git 仓库化原语 —— T2 可同步层文件类资产（拍板③b，台账 R16）。

架构依据：ADR ``docs/decisions/2026-10-17-sync-two-channel-routing.md`` 裁定
补充——文件类同步通道选型 = git 仓库管理方式，四件能力全 git 内置、禁止手写：

- **变更检测 = commit**（``add -A`` + ``status --porcelain`` 非空即 commit，
  删除即**墓碑提交**）；零手写指纹（config yaml 面的快照对比是 git 化前
  过渡形态，本模块不复制它）。
- **版本历史 = log**（本原语暴露 head/commit_count 观测面）。
- **增量传输 = git bundle**（``bundle create <f> <ref> ^<basis>``，bundle 头
  自带前置依赖声明；读者首拉 = 全量）。bundle 经 FileTransport 端口走
  relay/执行通道（端口注入，post_json/get_json 同律）。
- **拉取恒 fast-forward**：``git fetch <bundle> refs/heads/<scope>:
  refs/heads/<scope>``（refspec 无 ``+`` 前缀，git 原生拒绝非 ff）→
  ``reset --hard`` 物化镜像（删除落地 = 墓碑镜像语义）。**本模块无任何
  merge/rebase 调用面**——零合并是结构性执法，不是纪律约定。

属主模型（连接性互斥，R5/R7）：scope 属主 = 配置声明（owner_roots）；属主端
work-tree = 真实内容源（分离 work-tree，源树零内嵌 .git）；他端只读，镜像落
data/（**不接管**现役装配面，T1 镜像哲学同构）。属主写他端只读 ⇒ 读者 head
恒 ⊆ 属主历史 ⇒ bundle 增量前置恒可得 ⇒ 拉取恒 ff。

已知约束（台账 R16 ⑤登记）：装机 git 可执行依赖（缺失 = 本通道停用，
config/db 面不受影响）；大二进制全量存储（文本为主可接受，lfs 后评估）；
bundle 来源 = 自有 relay 拓扑（私有人单租户信任模型，git 原生拒 ``..`` 路径）。
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from git_cli import (
    DEFAULT_TIMEOUT_SECS,
    RUNTIME_EXCLUDES,
    GitCommandError,
    ensure_bare_repo,
    run_git,
)

logger = logging.getLogger(__name__)

#: T2 文件类 scope 词表（asset_type 同名注册进 sync_event.ASSET_CHANNELS → file）。
FILE_CHANNEL_SCOPES: tuple[str, ...] = ("plugins", "skills", "artifact_index")

#: 运行时/依赖产物排除（仓库 info/exclude，git 内置机制；零源树污染）。
_SCOPE_EXCLUDES: tuple[str, ...] = RUNTIME_EXCLUDES

_GIT_TIMEOUT_SECS = DEFAULT_TIMEOUT_SECS

#: git 通道操作失败（含非 ff 拒绝——零合并结构性执法的失败面）。
#: 与公共件同一类型（子进程机制自 git_cli 单源供给，T6 备份同源复用）。
GitChannelError = GitCommandError


#: 传输端口：拉取侧按 (scope, 对端 basis) 供 bundle；None = 对端无新提交。
FileTransport = Callable[[str, str | None], Awaitable[bytes | None]]


@dataclass(frozen=True)
class FileChannelConfig:
    """file 通道装配参数（server.py 从 settings/env 解析）。"""

    repo_root: Path
    owner_roots: dict[str, Path] = field(default_factory=dict)
    reader_scopes: frozenset[str] = frozenset()
    device_id: str = ""
    git_exe: str = "git"
    timeout_secs: float = _GIT_TIMEOUT_SECS

    @property
    def owner_scopes(self) -> frozenset[str]:
        """本端为属主（写者）的域（词表外交织静默剔除）。"""
        return frozenset(self.owner_roots) & set(FILE_CHANNEL_SCOPES)

    @property
    def reader_scope_set(self) -> frozenset[str]:
        """本端拉取（镜像）的域。"""
        return self.reader_scopes & set(FILE_CHANNEL_SCOPES)


class GitFileChannel:
    """file 通道 git 原语（每 scope 一个裸仓库 + 分离 work-tree）。

    目录形态：``<repo_root>/repos/<scope>.git``（裸仓库，分支
    ``refs/heads/<scope>``）；属主 work-tree = 配置的内容源；读者 work-tree =
    ``<repo_root>/mirror/<scope>``（镜像落点，data/ 私有）。
    """

    def __init__(self, config: FileChannelConfig) -> None:
        self._cfg = config
        self._repo_root = Path(config.repo_root)

    # ── 观测面 ───────────────────────────────────────────────

    @property
    def owner_scopes(self) -> frozenset[str]:
        return self._cfg.owner_scopes

    @property
    def reader_scopes(self) -> frozenset[str]:
        return self._cfg.reader_scope_set

    def repo_git_dir(self, scope: str) -> Path:
        return self._repo_root / "repos" / f"{scope}.git"

    def mirror_root(self, scope: str) -> Path:
        return self._repo_root / "mirror" / scope

    def probe(self) -> bool:
        """git 可执行可用性（装机依赖探测；缺失 = 引擎侧停用本通道）。"""
        try:
            return self._run(None, None, "--version").returncode == 0
        except GitChannelError:
            return False

    def head(self, scope: str) -> str | None:
        """scope 分支头 commit；零历史返回 None。"""
        proc = self._run(
            self.repo_git_dir(scope),
            None,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{scope}",
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def commit_count(self, scope: str) -> int:
        """版本历史深度（git rev-list 内置计数）。"""
        if self.head(scope) is None:
            return 0
        proc = self._run(
            self.repo_git_dir(scope), None, "rev-list", "--count", f"refs/heads/{scope}"
        )
        return int(proc.stdout.strip() or 0)

    def status(self) -> dict[str, Any]:
        """状态面：scope → role/head/commits（get_status 工具消费）。"""
        result: dict[str, Any] = {}
        for scope in sorted(self.owner_scopes | self.reader_scopes):
            result[scope] = {
                "role": "owner" if scope in self.owner_scopes else "reader",
                "head": (self.head(scope) or "")[:12],
                "commits": self.commit_count(scope),
            }
        return result

    # ── 属主臂：变更检测 = commit（含墓碑）────────────────────

    def capture_commit(self, scope: str) -> str | None:
        """源树变更 → commit；返回新头 sha，零变更返回 None。

        删除 = 墓碑即提交（git 原生 D 状态）。读者域调用 = 结构性违约
        （属主写他端只读——他端无本地提交是拉取恒 ff 的前提）。
        """
        if scope not in self.owner_scopes:
            raise GitChannelError(f"scope {scope} 非本端属主域（他端只读，无 commit 入口）")
        source = self._cfg.owner_roots[scope]
        if not source.is_dir():
            logger.warning("[file_channel] scope %s 源目录缺失，跳过捕获: %s", scope, source)
            return None
        self._ensure_repo(scope)
        git_dir = self.repo_git_dir(scope)
        add = self._run(git_dir, source, "add", "-A")
        if add.returncode != 0:
            raise GitChannelError(f"scope {scope} add 失败: {add.stderr.strip()[:200]}")
        status = self._run(git_dir, source, "status", "--porcelain")
        if status.returncode != 0:
            raise GitChannelError(f"scope {scope} status 失败: {status.stderr.strip()[:200]}")
        if not status.stdout.strip():
            return None
        changed = sum(1 for line in status.stdout.splitlines() if line.strip())
        commit = self._run(
            git_dir, source, "commit", "-q", "-m", f"file_channel: {scope} {changed} path(s)"
        )
        if commit.returncode != 0:
            raise GitChannelError(
                f"scope {scope} commit 失败: {commit.stderr.strip()[:200]}"
            )
        head = self.head(scope)
        if head is None:  # pragma: no cover —— commit 成功后必有分支头（git 后置不变量）
            raise GitChannelError(f"scope {scope} commit 后无分支头（git 仓库状态异常）")
        return head

    # ── 传输原语：bundle 增量 ────────────────────────────────

    def create_bundle(self, scope: str, basis: str | None) -> bytes | None:
        """对端 basis 之后的提交打包（git 内置增量语义）。

        零历史 / 对端已最新 → None；basis 不在本端历史（异常对端）→
        GitChannelError（fail-closed，拒绝供给）。
        """
        head = self.head(scope)
        if head is None or (basis is not None and basis == head):
            return None
        bundle_path = self._repo_root / f".bundle-{scope}.tmp"
        if not self._repo_root.is_dir():  # pragma: no cover —— repo_root 存在性随 head 存在性保证（防御守卫）
            self._repo_root.mkdir(parents=True, exist_ok=True)
        bundle_path = self._repo_root / f".bundle-{scope}.tmp"
        args = ["bundle", "create", str(bundle_path), f"refs/heads/{scope}"]
        if basis is not None:
            args.append(f"^{basis}")
        try:
            proc = self._run_bytes(self.repo_git_dir(scope), None, *args)
            if proc.returncode != 0:
                raise GitChannelError(
                    f"scope {scope} bundle 生成失败（basis 不在本端历史？）: "
                    + proc.stderr.decode("utf-8", errors="replace").strip()[:200]
                )
            return bundle_path.read_bytes()
        finally:
            bundle_path.unlink(missing_ok=True)

    def apply_bundle(self, scope: str, bundle: bytes) -> None:
        """读者落地：fetch（原生拒绝非 ff）→ reset --hard 物化镜像。

        非 ff / bundle 前置缺失 → GitChannelError，仓库与镜像零副作用
        （fetch 先于 worktree 物化失败，ref 不动）。
        """
        if scope not in self.reader_scopes:
            raise GitChannelError(f"scope {scope} 不在本端拉取域（属主域不走 apply 路径）")
        self._ensure_repo(scope)
        git_dir = self.repo_git_dir(scope)
        incoming = self._repo_root / f".incoming-{scope}.bundle"
        incoming.write_bytes(bundle)
        try:
            fetch = self._run_bytes(
                git_dir,
                None,
                "fetch",
                "--quiet",
                str(incoming),
                f"refs/heads/{scope}:refs/heads/{scope}",
            )
            if fetch.returncode != 0:
                raise GitChannelError(
                    f"scope {scope} fetch 拒绝（非 ff / bundle 前置缺失）: "
                    + fetch.stderr.decode("utf-8", errors="replace").strip()[:200]
                )
        finally:
            incoming.unlink(missing_ok=True)
        mirror = self.mirror_root(scope)
        try:
            mirror.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitChannelError(f"scope {scope} 镜像目录创建失败: {exc}") from exc
        reset = self._run(git_dir, mirror, "reset", "--quiet", "--hard", f"refs/heads/{scope}")
        if reset.returncode != 0:  # pragma: no cover —— fetch 成功后 reset --hard 于专用镜像目录不失败（防御分支）
            raise GitChannelError(
                f"scope {scope} 镜像物化失败: {reset.stderr.strip()[:200]}"
            )

    # ── 内部 ─────────────────────────────────────────────────

    def _ensure_repo(self, scope: str) -> None:
        """裸仓库初始化（幂等）：分支对齐 scope + 本端提交身份 + 排除面。"""
        try:
            ensure_bare_repo(
                self.repo_git_dir(scope),
                f"refs/heads/{scope}",
                self._cfg.device_id or "agentos",
                _SCOPE_EXCLUDES,
            )
        except GitCommandError as exc:
            raise GitChannelError(f"scope {scope} {exc}") from exc

    def _run(
        self, git_dir: Path | None, work_tree: Path | None, *args: str
    ) -> subprocess.CompletedProcess[str]:
        return run_git(
            self._cfg.git_exe, git_dir, work_tree, *args, timeout_secs=self._cfg.timeout_secs
        )

    def _run_bytes(
        self, git_dir: Path | None, work_tree: Path | None, *args: str
    ) -> subprocess.CompletedProcess[bytes]:
        return run_git(
            self._cfg.git_exe,
            git_dir,
            work_tree,
            *args,
            timeout_secs=self._cfg.timeout_secs,
            binary=True,
        )
