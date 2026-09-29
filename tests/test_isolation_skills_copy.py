# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
# @feature: 模式体系批E 技能随包工作空间同步源扩展 | @ci: python-coverage
"""技能复制增量同步测试。

契约：按技能子目录粒度增量同步——已存在的技能保持原样不覆盖，
仅补齐缺失项，幂等且低成本（目标 skills/ 目录整体跳过会冻结工作空间
技能快照，后续新增技能永远同步不进去）。

复制源 = 项目根 skills/ ∪ 模式包 skills/（批 E §3.1 用户裁定）：包源经
mode_skill_sources 注入或按仓内布局推导（出厂 plugins/shared/modes/ +
用户根 <USER_ROOT>/plugins/modes/），同名优先序用户根包 > 出厂包 > 仓根
（与 mode_keys 双根解析一致）。

涉及模块：plugins/shared/system/isolation/workspace_lifecycle.py::
_copy_skills_to_workspace / _skill_sources
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import tests._isolation_path  # noqa: F401  # isort: skip —— 须在 workspace_lifecycle import 前注入 sys.path
from workspace_lifecycle import WorkspaceLifecycleManager

# 哨兵：区分「未传参」（钉空包源 = 既有行为基线）与「显式 None」（走推导面）
_UNSET = object()


def _make_manager(
    base_path: Path,
    mode_skill_sources: list[Path] | None | object = _UNSET,
) -> WorkspaceLifecycleManager:
    """构造一个仅满足 _copy_skills_to_workspace 依赖的 manager。

    该方法只用到 self._base_path 与 self._mode_skill_sources；其余依赖
    （resource_merge/task_tree/...）传 None/空容器即可。__init__ 内的
    _record_main_branch 对非 git 目录会失败但被 try/except 兜底，不影响
    构造。mode_skill_sources 三态：未传 = 钉空（仅仓根单源，不依赖真实
    仓内/机器用户根布局）；list = 注入源；None = 走 manager 推导面。
    """
    sources = [] if mode_skill_sources is _UNSET else mode_skill_sources
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config={},
        task_tree=None,
        ws_meta_store={},
        base_path=str(base_path),
        mode_skill_sources=cast("list[Path] | None", sources),
    )


def _write_skill(parent: Path, name: str, content: str) -> None:
    """在 parent 下建一个最小技能目录（含 SKILL.md）。"""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


class TestCopySkillsIncremental:
    """技能增量同步：目标已存在时补齐缺失项、保留已有项。"""

    def test_backfills_missing_skills_into_existing_dir(self, tmp_path):
        """根因修复：目标 skills/ 已存在（只缺 skill-b）→ 补齐 skill-b，
        同时 skill-a 保持原样（不被源覆盖）。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        # 源：两个技能
        _write_skill(base / "skills", "skill-a", "SOURCE_A")
        _write_skill(base / "skills", "skill-b", "SOURCE_B")
        # 目标：已有 skill-a（占位内容），缺 skill-b —— 模拟旧快照
        _write_skill(ws / "skills", "skill-a", "PLACEHOLDER_A")

        manager = _make_manager(base)
        manager._copy_skills_to_workspace(str(ws))

        # skill-b 被补齐
        assert (ws / "skills" / "skill-b" / "SKILL.md").read_text(
            encoding="utf-8") == "SOURCE_B"
        # skill-a 保持占位内容，未被覆盖（核心回归断言）
        assert (ws / "skills" / "skill-a" / "SKILL.md").read_text(
            encoding="utf-8") == "PLACEHOLDER_A"

    def test_idempotent_on_repeated_calls(self, tmp_path):
        """重复调用幂等：第二次不再改动任何文件。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "skill-a", "SOURCE_A")

        manager = _make_manager(base)
        manager._copy_skills_to_workspace(str(ws))
        first_mtime = (ws / "skills" / "skill-a" / "SKILL.md").stat().st_mtime

        manager._copy_skills_to_workspace(str(ws))
        second_mtime = (ws / "skills" / "skill-a" / "SKILL.md").stat().st_mtime

        assert first_mtime == second_mtime  # 已存在 → 不重写

    def test_skip_when_workspace_is_project_root(self, tmp_path):
        """源==目标（工作空间即项目根）→ 跳过，不污染原位 skills/。"""
        base = tmp_path / "base"
        _write_skill(base / "skills", "skill-a", "KEEP_AS_IS")

        manager = _make_manager(base)
        # 工作空间路径就是 base 本身
        manager._copy_skills_to_workspace(str(base))

        assert (base / "skills" / "skill-a" / "SKILL.md").read_text(
            encoding="utf-8") == "KEEP_AS_IS"

    def test_skip_when_source_skills_missing(self, tmp_path):
        """base_path 无 skills/ 目录 → 安全跳过，不抛异常、不创建目标。"""
        base = tmp_path / "base"
        base.mkdir()
        ws = tmp_path / "ws"

        manager = _make_manager(base)
        manager._copy_skills_to_workspace(str(ws))

        assert not (ws / "skills").exists()


def _pkg_skills(tmp_path: Path, pkg: str) -> Path:
    """造一个模式包技能源目录（<tmp>/modes/<pkg>/skills/，调用方建技能子目录）。"""
    skills_dir = tmp_path / "modes" / pkg / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


class TestSkillSourcesUnion:
    """源扩展（批 E）：项目根 skills/ ∪ 模式包 skills/ 并入工作空间快照。"""

    def test_root_and_package_skills_both_synced(self, tmp_path):
        """根源与包源同时存在 → 两域技能都进工作空间（并集非替换）。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "repo-skill", "REPO")
        pkg = _pkg_skills(tmp_path, "mode_utpkg")
        _write_skill(pkg, "pkg-skill", "PKG")

        _make_manager(base, mode_skill_sources=[pkg])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "repo-skill" / "SKILL.md").read_text(encoding="utf-8") == "REPO"
        assert (ws / "skills" / "pkg-skill" / "SKILL.md").read_text(encoding="utf-8") == "PKG"

    def test_package_only_source_syncs_without_root_skills(self, tmp_path):
        """装机版场景：项目根无 skills/，仅模式包源也完成同步（多技能）。"""
        base = tmp_path / "base"
        base.mkdir()
        ws = tmp_path / "ws"
        pkg = _pkg_skills(tmp_path, "mode_utpkg")
        _write_skill(pkg, "pkg-a", "A")
        _write_skill(pkg, "pkg-b", "B")

        _make_manager(base, mode_skill_sources=[pkg])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "pkg-a" / "SKILL.md").exists()
        assert (ws / "skills" / "pkg-b" / "SKILL.md").exists()

    def test_package_skills_are_incremental_too(self, tmp_path):
        """包技能同增量语义：工作空间已有同名技能保持原样，仅补缺。"""
        base = tmp_path / "base"
        base.mkdir()
        ws = tmp_path / "ws"
        pkg = _pkg_skills(tmp_path, "mode_utpkg")
        _write_skill(pkg, "pkg-a", "NEW_A")
        _write_skill(pkg, "pkg-b", "NEW_B")
        _write_skill(ws / "skills", "pkg-a", "OLD_A")  # 旧快照只缺 pkg-b

        _make_manager(base, mode_skill_sources=[pkg])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "pkg-a" / "SKILL.md").read_text(encoding="utf-8") == "OLD_A"
        assert (ws / "skills" / "pkg-b" / "SKILL.md").read_text(encoding="utf-8") == "NEW_B"


class TestSkillSourcePriority:
    """同名优先序：用户根包技能 > 出厂包技能 > 仓根（注入清单按升优先序表达）。"""

    def test_user_package_wins_over_factory_and_repo(self, tmp_path):
        """同名三源并存 → 用户包内容落工作空间；仅出厂有的技能仍从出厂补。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "shared-skill", "REPO")
        factory = _pkg_skills(tmp_path, "mode_factory")
        _write_skill(factory, "shared-skill", "FACTORY")
        _write_skill(factory, "factory-only", "FACTORY_ONLY")
        user = _pkg_skills(tmp_path, "mode_user")
        _write_skill(user, "shared-skill", "USER")

        _make_manager(base, mode_skill_sources=[factory, user])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "shared-skill" / "SKILL.md").read_text(encoding="utf-8") == "USER"
        assert (ws / "skills" / "factory-only" / "SKILL.md").read_text(encoding="utf-8") == "FACTORY_ONLY"

    def test_factory_wins_over_repo_when_user_silent(self, tmp_path):
        """对照：用户源无此技能时出厂包赢仓根；仅仓根有的技能从仓根补。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "both-skill", "REPO")
        _write_skill(base / "skills", "repo-only", "REPO_ONLY")
        factory = _pkg_skills(tmp_path, "mode_factory")
        _write_skill(factory, "both-skill", "FACTORY")
        user = _pkg_skills(tmp_path, "mode_user")  # 用户包存在但无同名技能

        _make_manager(base, mode_skill_sources=[factory, user])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "both-skill" / "SKILL.md").read_text(encoding="utf-8") == "FACTORY"
        assert (ws / "skills" / "repo-only" / "SKILL.md").read_text(encoding="utf-8") == "REPO_ONLY"


class TestSkillSourcesDerivation:
    """推导面（mode_skill_sources=None）：出厂包根（本仓真实布局）+ 用户根（env 钉 tmp）。"""

    def test_factory_and_user_roots_derived(self, tmp_path, monkeypatch):
        """推导源 = 仓根 ∪ 真实出厂模式包 ∪ env 钉定的用户根模式包。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "repo-skill", "REPO")
        user_root = tmp_path / "userroot"
        user_pkg = user_root / "plugins" / "modes" / "mode_utuser" / "skills"
        _write_skill(user_pkg, "user-skill", "USER")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
        monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)

        _make_manager(base, mode_skill_sources=None)._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "repo-skill" / "SKILL.md").read_text(encoding="utf-8") == "REPO"
        assert (ws / "skills" / "user-skill" / "SKILL.md").read_text(encoding="utf-8") == "USER"
        # 出厂根 = 本仓真实布局：批 E 迁入技能可见（迁移验收锚点）
        assert (ws / "skills" / "code-implement" / "SKILL.md").exists()
        assert (ws / "skills" / "code-godot" / "SKILL.md").exists()

    def test_user_copy_shadows_factory_same_skill(self, tmp_path, monkeypatch):
        """同包同名技能用户副本赢（与 mode_keys.find_mode_skill_dir 双根一致）。"""
        user_root = tmp_path / "userroot"
        user_pkg = user_root / "plugins" / "modes" / "mode_coding" / "skills"
        _write_skill(user_pkg, "code-implement", "USER WIN")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
        monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)

        _make_manager(tmp_path / "base", mode_skill_sources=None)._copy_skills_to_workspace(
            str(tmp_path / "ws")
        )

        content = (tmp_path / "ws" / "skills" / "code-implement" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert content == "USER WIN"

    def test_user_space_unavailable_still_syncs_repo_and_factory(
        self, tmp_path, monkeypatch
    ):
        """user_space 不可导入 → 仅仓根+出厂包源（降级不抛错，无用户源）。"""
        base = tmp_path / "base"
        _write_skill(base / "skills", "repo-skill", "REPO")
        monkeypatch.setitem(sys.modules, "user_space", None)  # import 即 ImportError

        _make_manager(base, mode_skill_sources=None)._copy_skills_to_workspace(
            str(tmp_path / "ws")
        )

        assert (tmp_path / "ws" / "skills" / "repo-skill" / "SKILL.md").exists()
        assert (tmp_path / "ws" / "skills" / "code-implement" / "SKILL.md").exists()

    def test_user_root_without_packages_still_syncs(self, tmp_path, monkeypatch):
        """用户根存在但无任何模式包 → 无用户源，仓根+出厂包照常同步。"""
        base = tmp_path / "base"
        _write_skill(base / "skills", "repo-skill", "REPO")
        empty_user_root = tmp_path / "emptyuser"
        empty_user_root.mkdir()
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(empty_user_root))
        monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)

        _make_manager(base, mode_skill_sources=None)._copy_skills_to_workspace(
            str(tmp_path / "ws")
        )

        assert (tmp_path / "ws" / "skills" / "repo-skill" / "SKILL.md").exists()
        assert (tmp_path / "ws" / "skills" / "code-implement" / "SKILL.md").exists()

    def test_user_plugins_dir_none_degrades_to_no_user_source(
        self, tmp_path, monkeypatch
    ):
        """user_space.user_plugins_dir() 返回 None（极端环境）→ 无用户源不抛错。"""
        import user_space

        base = tmp_path / "base"
        _write_skill(base / "skills", "repo-skill", "REPO")
        monkeypatch.setattr(user_space, "user_plugins_dir", lambda: None)

        _make_manager(base, mode_skill_sources=None)._copy_skills_to_workspace(
            str(tmp_path / "ws")
        )

        assert (tmp_path / "ws" / "skills" / "repo-skill" / "SKILL.md").exists()
        assert (tmp_path / "ws" / "skills" / "code-implement" / "SKILL.md").exists()


class TestSkillSourceEdgeGuards:
    """源枚举守卫：与目标同目录的源跳过（不自复制）。"""

    def test_source_equal_to_destination_skipped(self, tmp_path):
        """注入源恰为目标 skills/ 目录 → 该源自跳过，仓根源照常（不自复制）。"""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        _write_skill(base / "skills", "repo-skill", "REPO")
        self_src = ws / "skills"
        _write_skill(self_src, "already-there", "WS")  # 目标内既有技能充当"自源"

        _make_manager(base, mode_skill_sources=[self_src])._copy_skills_to_workspace(str(ws))

        assert (ws / "skills" / "repo-skill" / "SKILL.md").exists()
        assert (ws / "skills" / "already-there" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "WS"
