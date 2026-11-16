# @feature: FP-0.2.〇 管道引擎与插件执行模型（位置闸管道层执法） | @ci: python-coverage
"""用户空间根 ≠ 仓库根——repo_anchor 解析契约的装机形态回归测试。

锁定契约（解析规则：AGENTOS_CONFIG_ROOT 优先 → __file__ 向上找 config/kernel
祖先；**候选含用户空间根一律不采纳**——用户根是运行时面，user_space 契约
= 用户可写资产整体位于仓库之外，其 config/kernel 由装机播种（kernel-manager
钉 AGENTOS_CONFIG_ROOT=<user_root>/config + 出厂树补齐），目录指纹无法区分
仓库根与用户根，按范畴排除）：

1. 装机布局（AGENTOS_CONFIG_ROOT=<user_root>/config 且 config/kernel 已播种）：
   resolve_repo_root() 绝不返回用户空间根——否则用户运行时面
   （.ai_workspaces 等）被拖进仓库恒拒集，写区白名单链在其下全部失效；
2. 同布局下 repo_write_denied 对用户空间 .ai_workspaces/<非 sessions> 路径
   返回 None（不拒，落白名单链）；zone_policy.write_verdict 携名单 entries
   放行（2026-10-18 装机哨兵 observations.md 写回归的场景还原）；
3. dev 布局（假仓库 ≠ 用户根）：解析与自保拒绝面零变化——仓库根照常采纳、
   仓库 .ai_workspaces/<task_id> 对写恒拒、sessions 子树豁免保留；
4. 候选判定（__file__ 向上探测逐父目录消费的同一直查出口）：用户根形态
   （config/kernel 命中且 == 用户根）不可作仓库根；非用户根同指纹可作。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import pytest  # noqa: E402
import repo_anchor  # noqa: E402
from repo_anchor import _is_repo_root_candidate, repo_write_denied, resolve_repo_root  # noqa: E402
from zone_policy import write_verdict  # noqa: E402

pytestmark = pytest.mark.unit


def _normcase(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


@pytest.fixture
def installed_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """装机布局：用户根携带播种的 config/kernel，AGENTOS_CONFIG_ROOT 钉其 config。"""
    user_root = tmp_path / "usroot"
    (user_root / "config" / "kernel").mkdir(parents=True)
    (user_root / ".ai_workspaces" / "maintenance_sentinel").mkdir(parents=True)
    (user_root / ".ai_workspaces" / "sessions" / "e45484480619").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(user_root / "config"))
    repo_anchor.reset_cache()
    yield user_root
    repo_anchor.reset_cache()


@pytest.fixture
def installed_layout_root_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """同布局、env 第二合法形态（值 = 根本身，kernel-manager 变体皆可能）。"""
    user_root = tmp_path / "usroot"
    (user_root / "config" / "kernel").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(user_root))
    repo_anchor.reset_cache()
    yield user_root
    repo_anchor.reset_cache()


@pytest.fixture
def dev_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """dev 布局：假仓库（config/kernel 指纹）与用户根分离，env 钉仓库 config。"""
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    (repo / ".ai_workspaces" / "task-1").mkdir(parents=True)
    (repo / ".ai_workspaces" / "sessions" / "thread-a").mkdir(parents=True)
    user_root = tmp_path / "usroot"
    (user_root / "config" / "kernel").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    repo_anchor.reset_cache()
    yield repo
    repo_anchor.reset_cache()


class TestInstalledLayout:
    """装机形态：用户根不得被解析为仓库根（写区白名单链保持可达）。"""

    def test_resolve_repo_root_never_user_root(self, installed_layout: Path) -> None:
        root = resolve_repo_root()
        assert root is None or _normcase(root) != _normcase(installed_layout)

    def test_user_space_ai_workspaces_write_falls_to_whitelist(
        self, installed_layout: Path
    ) -> None:
        observations = installed_layout / ".ai_workspaces" / "maintenance_sentinel" / "observations.md"
        assert repo_write_denied(observations) is None
        allow, reason = write_verdict(
            observations,
            [],
            write_zones=[str(installed_layout / ".ai_workspaces" / "maintenance_sentinel")],
        )
        assert allow is True
        assert reason is None

    @pytest.mark.parametrize("subdir", ["maintenance_sentinel", "task-abc123"])
    def test_sessions_subtree_exemption_unchanged(self, installed_layout: Path, subdir: str) -> None:
        """会话子树豁免与任务运行时面恒拒是仓库根语义——用户根不采纳后两者都不触发。"""
        probe = installed_layout / ".ai_workspaces" / subdir / "x.txt"
        assert repo_write_denied(probe) is None

    def test_root_form_env_never_user_root(
        self, installed_layout_root_form: Path
    ) -> None:
        """env 第二形态（值 = 根本身）同契约：用户根不被采纳。"""
        root = resolve_repo_root()
        assert root is None or _normcase(root) != _normcase(installed_layout_root_form)


class TestDevLayoutUnchanged:
    """dev 形态：仓库根采纳与自保拒绝面零变化（防修过头）。"""

    def test_repo_root_resolved(self, dev_layout: Path) -> None:
        assert resolve_repo_root() is not None
        assert _normcase(resolve_repo_root()) == _normcase(dev_layout)  # type: ignore[union-attr]

    def test_repo_task_workspace_write_still_denied(self, dev_layout: Path) -> None:
        deny = repo_write_denied(dev_layout / ".ai_workspaces" / "task-1" / "out.txt")
        assert deny is not None
        assert ".ai_workspaces" in deny

    def test_repo_sessions_subtree_still_exempt(self, dev_layout: Path) -> None:
        assert repo_write_denied(dev_layout / ".ai_workspaces" / "sessions" / "thread-a" / "a.md") is None


class TestCandidateCheck:
    """_is_repo_root_candidate：指纹 + 用户根范畴排除（__file__ 探测逐父消费）。"""

    def test_user_root_form_rejected(self, installed_layout: Path) -> None:
        assert _is_repo_root_candidate(installed_layout) is False

    def test_non_user_root_with_fingerprint_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        (repo / "config" / "kernel").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
        assert _is_repo_root_candidate(repo) is True

    def test_without_fingerprint_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
        assert _is_repo_root_candidate(plain) is False

    def test_user_root_unavailable_keeps_fingerprint_adoption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """极端环境（用户根不可得）不扩大排除面：指纹命中照常采纳。"""
        repo = tmp_path / "repo"
        (repo / "config" / "kernel").mkdir(parents=True)
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        assert _is_repo_root_candidate(repo) is True
