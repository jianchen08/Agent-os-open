# @feature: FP-0.2.六 安全与审批 | @ci: python-coverage
"""zone_policy 单元测试——读链内建工作空间地界白名单（用户裁定 2026-09-29）。

装机实证（事故 2026-09-29）：任务工作空间常驻仓库 ``.ai_workspaces`` 下的
会话子目录（``.../.ai_workspaces/<项目>__wt_x/sessions/<thread>``），项目
工作树是 sessions 的**兄弟目录**——workspace/project_root 锚覆盖不到，读
兄弟工作树撞仓库拒绝集（``.ai_workspaces`` 在 REPO_READ_DENIED_DIRS）弹
读授权卡，无人值守停泊。

用户裁定："工作空间根必须在白名单内，不能在黑名单内"——白名单策略而非
代码特例：read_verdict 放行链内建追加派生前缀（从锚向上找最近的
``.ai_workspaces`` 祖先），与显式 read_allow 同位同权。

锁定契约：
1. workspace 在 .ai_workspaces 下：兄弟工作树读放行（真实 read_verdict
   路径，白名单命中）；同一调用不传锚参数则仍拒（delta 即修复面）；
2. workspace 不在 .ai_workspaces 下：行为零变化（黑名单路径照拒）；
3. 派生前缀纯逻辑：大小写不敏感、取最近祖先、两锚任一命中即追加、
   地界外路径不因锚在 .ai_workspaces 下而放行；
4. 写链回归：兄弟工作树写仍走 write_verdict——仓库自保恒拒，不因读链
   白名单放行（跨会话写污染防线保持）；
5. deny 优先级：现状 = 白名单先行（授权前缀命中即放行，先于 deny 扫描），
   内建前缀与显式 read_allow 同位同权——地界内 read_deny 条目被内建白名单
   压过（与用户显式 add read_allow 同效，语义不新增）；地界外 read_deny
   照拒（黑名单优先于无白名单覆盖的路径，语义保持）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import repo_anchor  # noqa: E402
import zone_policy  # noqa: E402
from zone_policy import (  # noqa: E402
    builtin_read_allow_prefixes,
    read_verdict,
    write_verdict,
)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假仓库根（config/kernel 标记 + .ai_workspaces 地界），名单钉空。

    repo_anchor 解析缓存钉桩（AGENTOS_CONFIG_ROOT 生效前置 + 用后复位，
    防污染同进程后续测试）。
    """
    root = tmp_path / "repo"
    (root / "config" / "kernel").mkdir(parents=True)
    users_dir = tmp_path / "users"
    (users_dir / "default").mkdir(parents=True)
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        "entries: []\nread_allow: []\nread_deny: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    repo_anchor.reset_cache()
    yield root
    repo_anchor.reset_cache()


@pytest.fixture
def worktree_layout(fake_repo: Path) -> dict[str, Path]:
    """装机同构布局：.ai_workspaces/<项目>__wt_x 下 sessions 与 docs 兄弟。"""
    ws = fake_repo / ".ai_workspaces" / "proj__wt_x" / "sessions" / "thread-1"
    ws.mkdir(parents=True)
    sibling = fake_repo / ".ai_workspaces" / "proj__wt_x" / "docs" / "working"
    sibling.mkdir(parents=True)
    (sibling / "note.md").write_text("sibling doc", encoding="utf-8")
    return {"workspace": ws, "sibling": sibling, "root": fake_repo}


class TestBuiltinWorkspaceZonePrefix:
    """契约1：兄弟工作树读放行（真实 read_verdict，白名单命中）。"""

    def test_sibling_worktree_read_allowed(
        self, worktree_layout: dict[str, Path]
    ) -> None:
        ws = str(worktree_layout["workspace"])
        sibling = worktree_layout["sibling"] / "note.md"

        allow, reason = read_verdict(sibling, workspace=ws, project_root=ws)

        assert allow, f"兄弟工作树读应放行，实际拒绝: {reason}"
        assert reason is None

    def test_same_call_without_anchor_args_still_denied(
        self, worktree_layout: dict[str, Path]
    ) -> None:
        """不传锚参数 = 修复前调用形态：仍撞仓库拒绝集（delta 即修复面）。"""
        sibling = worktree_layout["sibling"] / "note.md"

        allow, reason = read_verdict(sibling)

        assert not allow
        assert reason is not None
        assert ".ai_workspaces" in reason

    def test_anchors_only_hit_outside_blacklist_layout(
        self, tmp_path: Path, fake_repo: Path
    ) -> None:
        """锚不在 .ai_workspaces 下（dev 仓库布局）：内建前缀零追加。"""
        ws = tmp_path / "dev_ws" / "sessions" / "t1"
        ws.mkdir(parents=True)

        assert builtin_read_allow_prefixes(ws, ws) == []

    def test_path_outside_territory_not_allowed(
        self, worktree_layout: dict[str, Path], fake_repo: Path
    ) -> None:
        """锚在 .ai_workspaces 下不等于全仓库放行：地界外路径照走判定链。"""
        ws = str(worktree_layout["workspace"])
        stranger = fake_repo / "data" / "tenant" / "secret.txt"
        stranger.parent.mkdir(parents=True)
        stranger.write_text("x", encoding="utf-8")

        allow, reason = read_verdict(stranger, workspace=ws, project_root=ws)

        assert not allow
        assert reason is not None
        assert "data" in reason


class TestPrefixDerivation:
    """契约3：派生前缀纯逻辑（大小写 / 最近祖先 / 两锚并集）。"""

    def test_case_insensitive_match(self, tmp_path: Path) -> None:
        ws = tmp_path / "Repo" / ".AI_Workspaces" / "proj" / "sessions" / "t1"
        ws.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(ws, None)

        assert len(prefixes) == 1
        assert Path(prefixes[0]).name.lower() == ".ai_workspaces"

    def test_nearest_ancestor_wins(self, tmp_path: Path) -> None:
        """嵌套 .ai_workspaces 取最近祖先：外层地界不随内层锚并入。"""
        inner_root = tmp_path / "outer" / ".ai_workspaces" / "nested" / ".ai_workspaces"
        ws = inner_root / "b" / "sessions" / "t1"
        ws.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(ws, None)

        assert [os_norm(p) for p in prefixes] == [os_norm(inner_root)]

    def test_both_anchors_union(self, tmp_path: Path) -> None:
        """两锚分别向上探测，任一命中即追加（去重）。"""
        root_a = tmp_path / "wsA" / ".ai_workspaces" / "a" / "sessions" / "t1"
        root_b = tmp_path / "wsB" / ".ai_workspaces" / "b" / "sessions" / "t2"
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(str(root_b), str(root_a))

        assert [os_norm(p) for p in prefixes] == sorted(
            [os_norm(root_a.parents[2]), os_norm(root_b.parents[2])], key=os_norm
        )

    def test_drive_root_terminates(self, tmp_path: Path) -> None:
        """锚在盘根直下（无 .ai_workspaces 祖先）：向上探测正常终止不发散。"""
        ws = tmp_path / "plain" / "ws"
        ws.mkdir(parents=True)

        assert builtin_read_allow_prefixes(ws, None) == []


class TestWriteChainUntouched:
    """契约4：写链回归——兄弟工作树写仍走 write_verdict（仓库自保恒拒）。"""

    def test_sibling_worktree_write_still_hard_denied(
        self, worktree_layout: dict[str, Path]
    ) -> None:
        sibling = worktree_layout["sibling"]

        allow, reason = write_verdict(sibling)

        assert not allow, "兄弟工作树写不得因读链白名单放行"
        assert reason is not None
        assert "运行时/产物区" in reason

    def test_write_verdict_signature_has_no_anchor_params(
        self, worktree_layout: dict[str, Path]
    ) -> None:
        """写链判定不受读链锚参数影响：write_verdict 无 workspace/project_root
        形参（签名即契约，白名单扩展止步于读面）。"""
        import inspect

        params = inspect.signature(write_verdict).parameters
        assert "workspace" not in params
        assert "project_root" not in params


class TestDenyPriority:
    """契约5：deny 优先级——白名单先行为现状语义，内建前缀同位同权。"""

    @pytest.fixture
    def deny_entry(self, monkeypatch: pytest.MonkeyPatch):
        """read_deny 名单注入（名单文件加载为外部依赖，钉桩于模块缝）。"""

        def _install(prefix: Path) -> Path:
            monkeypatch.setattr(
                zone_policy, "_load_read_deny", lambda: [str(prefix)]
            )
            return prefix

        return _install

    def test_read_deny_inside_territory_overridden_by_builtin_prefix(
        self, worktree_layout: dict[str, Path], deny_entry
    ) -> None:
        """地界内 read_deny 被内建白名单压过——与显式 read_allow 同效
        （现状白名单先行，见 zone_policy.read_verdict 放行链序）。"""
        ws = str(worktree_layout["workspace"])
        denied_doc = worktree_layout["sibling"] / "note.md"
        deny_entry(worktree_layout["sibling"])

        allow, reason = read_verdict(denied_doc, workspace=ws, project_root=ws)

        assert allow, "内建白名单命中应先于 deny 扫描（同显式 read_allow 位序）"
        assert reason is None

    def test_read_deny_outside_territory_still_denied(
        self, worktree_layout: dict[str, Path], tmp_path: Path, deny_entry
    ) -> None:
        """白名单未覆盖的 read_deny 路径照拒：黑名单对无白名单路径优先。"""
        ws = str(worktree_layout["workspace"])
        outside = tmp_path / "vendor" / "secret"
        outside.mkdir(parents=True)
        deny_entry(outside)

        allow, reason = read_verdict(outside / "k.txt", workspace=ws, project_root=ws)

        assert not allow
        assert reason is not None
        assert "read_deny" in reason


def os_norm(p: Path | str) -> str:
    """前缀比较同规归一（_path_under_prefix 消费形态）。"""
    return os.path.normcase(os.path.normpath(str(p)))
