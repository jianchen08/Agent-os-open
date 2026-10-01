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


# ── 用户根形态地界（ADR 2026-10-01-workspace-root-user-root-anchor）──────────


@pytest.fixture
def user_root(tmp_path: Path, fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """用户根钉桩（AGENTOS_USER_ROOT → tmp 仓外独立子树，装机形态同构）。

    fake_repo 依赖提供名单/env 钉桩与仓库锚缓存复位；用户根放 tmp_path 下
    与 repo 平级的独立子树（复现 %APPDATA%\\agentos 不在任何仓库内）。
    """
    (tmp_path / "empty_appdata").mkdir()  # OS 目录推导兜底钉空（env 缺失分支用）
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty_appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty_appdata"))
    ur = tmp_path / "userroot" / "agentos"
    ur.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(ur))
    return ur


class TestUserRootFormBoundary:
    """用户根形态地界识别（装机工作空间根迁 ``<user_root>/workspaces``）。

    锁定契约：
    1. 锚在 ``<user_root>/workspaces/<task>`` 下（任务根/会话子目录/项目树
       三种布局）：派生 ``<user_root>/workspaces`` 地界前缀（用户根锚定命中，
       大小写不敏感与旧形态同规）；
    2. 地界内（锚外兄弟树）读放行——read_deny 命中被内建白名单压过（与旧
       形态同位同权），不携锚则照拒（delta 即修复面）；
    3. 地界外（``<user_root>/config``）：不派生、写面仍在写区外（位置闸弹
       授权卡 = 拦截待授权，不误判为地界内）；
    4. 任意项目下同名 ``workspaces``（非用户根直下）：不误判为地界；
    5. 旧形态 ``.ai_workspaces`` 不回退；用户根不可得时仅用户根形态收缩
       （fail-closed，范围收缩不发散）。
    """

    @pytest.mark.parametrize(
        "anchor_tail",
        [
            "01ed89f3952e",  # 任务根直锚（装机形态 workspace = workspaces/<task>）
            "01ed89f3952e/sessions/thread-1",  # 会话子目录锚
            "d8f8c75463c6/projects/demo",  # 会话登记项目树（sessions 兄弟形态）
        ],
    )
    def test_anchor_under_user_root_derives_boundary(
        self, user_root: Path, anchor_tail: str
    ) -> None:
        anchor = user_root / "workspaces" / anchor_tail
        anchor.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(str(anchor), str(anchor))

        assert [os_norm(p) for p in prefixes] == [os_norm(user_root / "workspaces")]
        # 性质断言：锚在地界前缀子树内，且地界不等于锚自身
        p, pre = os_norm(anchor), os_norm(prefixes[0])
        assert p.startswith(pre + os.sep)
        assert Path(prefixes[0]) != anchor

    def test_read_inside_boundary_overrides_deny(
        self, user_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """地界内兄弟树读：read_deny 命中被内建白名单压过；不携锚照拒。"""
        ws = user_root / "workspaces" / "01ed89f3952e" / "sessions" / "thread-1"
        sibling = user_root / "workspaces" / "01ed89f3952e" / "docs"
        sibling.mkdir(parents=True)
        ws.mkdir(parents=True)
        note = sibling / "note.md"
        note.write_text("sibling doc", encoding="utf-8")
        monkeypatch.setattr(zone_policy, "_load_read_deny", lambda: [str(sibling)])

        allow, reason = read_verdict(note, workspace=str(ws), project_root=str(ws))
        assert allow, f"地界内兄弟树读应被内建白名单放行，实际拒绝: {reason}"
        assert reason is None

        allow_no_anchor, reason_no_anchor = read_verdict(note)
        assert not allow_no_anchor
        assert reason_no_anchor is not None

    def test_write_in_own_task_workspace_via_root_anchor(
        self, user_root: Path
    ) -> None:
        """自己任务工作区内写：根锚覆盖（位置闸锚内先行放行的判定输入）。"""
        ws = user_root / "workspaces" / "01ed89f3952e"
        ws.mkdir(parents=True)
        target = zone_policy.resolve_anchored("tests/a.txt", str(ws), str(ws))

        assert zone_policy.within_root_anchors(target, str(ws), str(ws))

    def test_write_outside_boundary_still_gated(self, user_root: Path) -> None:
        """地界外（<user_root>/config）：根锚不覆盖、不在任何写区（= 位置闸
        弹授权卡的拦截路径），且不因新集合误判为地界内。"""
        ws = user_root / "workspaces" / "01ed89f3952e"
        ws.mkdir(parents=True)
        config_file = user_root / "config" / "kernel.yaml"
        config_file.parent.mkdir(parents=True)
        resolved = zone_policy.resolve_anchored(str(config_file), str(ws), str(ws))

        assert not zone_policy.within_root_anchors(resolved, str(ws), str(ws))
        assert write_verdict(resolved) == (False, None)
        # 地界前缀覆盖边界性质：派生前缀不覆盖 config 子树
        prefixes = builtin_read_allow_prefixes(str(ws), str(ws))
        assert len(prefixes) == 1
        assert os_norm(resolved) != os_norm(prefixes[0])
        assert not os_norm(resolved).startswith(os_norm(prefixes[0]) + os.sep)

    @pytest.mark.parametrize("layout", ["in_repo", "random_project"])
    def test_same_named_workspaces_outside_user_root_not_boundary(
        self, tmp_path: Path, fake_repo: Path, user_root: Path, layout: str
    ) -> None:
        """任意项目下同名 ``workspaces``（非用户根直下）不是地界。"""
        base = fake_repo if layout == "in_repo" else tmp_path / "randomproj"
        anchor = base / "workspaces" / "t1"
        anchor.mkdir(parents=True)

        assert builtin_read_allow_prefixes(str(anchor), None) == []

    def test_name_case_insensitive_same_as_legacy(self, user_root: Path) -> None:
        """目录名大小写不敏感（与旧形态 .lower() 同规）。"""
        anchor = user_root / "WORKSPACES" / "t1"
        anchor.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(str(anchor), None)

        assert len(prefixes) == 1
        assert Path(prefixes[0]).name.lower() == "workspaces"

    def test_env_user_root_pointing_elsewhere_no_hit(
        self, tmp_path: Path, user_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENTOS_USER_ROOT 指向别处：同名目录不命中（锚定条件生效）。"""
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "elsewhere"))
        anchor = user_root / "workspaces" / "t1"
        anchor.mkdir(parents=True)

        assert builtin_read_allow_prefixes(str(anchor), None) == []

    def test_user_root_missing_degrades_closed_but_legacy_still_hits(
        self, tmp_path: Path, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户根不可得：workspaces 形态不命中（范围收缩），旧形态照常命中
        （fail-closed 不扩大化为全局失明）。"""
        ws = tmp_path / "orphan" / "agentos" / "workspaces" / "t1"
        ws.mkdir(parents=True)
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)

        assert builtin_read_allow_prefixes(str(ws), None) == []
        legacy = fake_repo / ".ai_workspaces" / "s1"
        legacy.mkdir(parents=True)
        assert builtin_read_allow_prefixes(str(legacy), None) != []

    def test_user_root_unresolvable_none_degrades_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user_root() 返回 None（OS 目录推导不可得）：workspaces 形态不命中
        （跨平台钉桩模块缝，覆盖 Windows 专属的 None 分支）。"""
        ws = tmp_path / "nohost" / "agentos" / "workspaces" / "t1"
        ws.mkdir(parents=True)
        monkeypatch.setattr(zone_policy, "user_root", lambda: None)

        assert builtin_read_allow_prefixes(str(ws), None) == []

    def test_legacy_form_unregressed_with_user_root_set(
        self, fake_repo: Path, user_root: Path
    ) -> None:
        """旧形态放行不回退：用户根在场时 .ai_workspaces 祖先照常派生。"""
        legacy = fake_repo / ".ai_workspaces" / "proj__wt_x" / "sessions" / "t1"
        legacy.mkdir(parents=True)

        prefixes = builtin_read_allow_prefixes(str(legacy), str(legacy))

        assert [os_norm(p) for p in prefixes] == [
            os_norm(fake_repo / ".ai_workspaces")
        ]
