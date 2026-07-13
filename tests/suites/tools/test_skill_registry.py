"""Skill 注册表测试

覆盖：
1. Skill 目录扫描与解析（frontmatter + body）
2. 脚本发现（scripts/ 目录）
3. 搜索 API（名称 / 描述 / 语言 / 精确匹配）
4. 挂载 API（软链 / 复制 / 幂等性）
5. 全局单例
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """创建一个包含 SKILL.md + scripts/ 的 Skill 目录。"""
    skill_path = tmp_path / "skills" / "test_skill"
    skill_path.mkdir(parents=True)

    # SKILL.md
    (skill_path / "SKILL.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: 测试用 Skill\n"
        "---\n"
        "\n"
        "# Test Skill\n"
        "\n"
        "这是一个测试 Skill。\n",
        encoding="utf-8",
    )

    # scripts/
    scripts_dir = skill_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "run.py").write_text("print('hello')\n", encoding="utf-8")
    (scripts_dir / "setup.sh").write_text("echo setup\n", encoding="utf-8")
    (scripts_dir / "README.md").write_text("不是脚本\n", encoding="utf-8")

    return skill_path.parent.parent  # 返回 tmp_path（skills/ 的父目录）


@pytest.fixture
def registry(skill_dir: Path):
    """创建一个指向 fixture 目录的注册表。"""
    from skills.registry import SkillRegistry

    r = SkillRegistry(skill_roots=[skill_dir / "skills"])
    r.refresh()
    return r


# ---------------------------------------------------------------------------
# 1. 扫描与解析
# ---------------------------------------------------------------------------


class TestScanAndParse:
    """Skill 目录扫描与解析测试。"""

    def test_finds_skill_with_skill_md(self, tmp_path: Path) -> None:
        """扫描包含 SKILL.md 的目录。"""
        from skills.registry import SkillRegistry

        skill_dir = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n\nBody", encoding="utf-8",
        )

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        assert r.is_initialized()
        skills = r.search_skills()
        assert len(skills) == 1
        assert skills[0].skill_name == "my-skill"

    def test_finds_skill_with_lowercase_skill_md(self, tmp_path: Path) -> None:
        """扫描包含 skill.md（小写）的目录。"""
        from skills.registry import SkillRegistry

        skill_dir = tmp_path / "skills" / "lowercase"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.md").write_text(
            "---\nname: lowercase\n---\n\nBody", encoding="utf-8",
        )

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        skills = r.search_skills()
        assert len(skills) == 1
        assert skills[0].skill_name == "lowercase"

    def test_skips_dir_without_skill_md(self, tmp_path: Path) -> None:
        """跳过没有 SKILL.md 的目录。"""
        from skills.registry import SkillRegistry

        (tmp_path / "skills" / "no_skill").mkdir(parents=True)

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        assert len(r.search_skills()) == 0

    def test_parses_frontmatter(self, registry: "SkillRegistry") -> None:
        """解析 YAML frontmatter 的 name 和 description。"""
        skills = registry.search_skills()
        assert len(skills) == 1
        s = skills[0]
        assert s.skill_name == "test-skill"
        assert s.description == "测试用 Skill"

    def test_fallback_to_dir_name_when_no_name(self, tmp_path: Path) -> None:
        """frontmatter 无 name 字段时，用目录名。"""
        from skills.registry import SkillRegistry

        skill_dir = tmp_path / "skills" / "my-dir-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\n---\n\nNo name", encoding="utf-8")

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        skills = r.search_skills()
        assert skills[0].skill_name == "my-dir-name"

    def test_malformed_frontmatter_fallback(self, tmp_path: Path) -> None:
        """frontmatter 格式错误时不崩溃，body 用全文。"""
        from skills.registry import SkillRegistry

        skill_dir = tmp_path / "skills" / "bad-fm"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "没有 frontmarker 的纯 Markdown", encoding="utf-8",
        )

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        skills = r.search_skills()
        assert len(skills) == 1
        assert skills[0].skill_name == "bad-fm"
        assert "Markdown" in skills[0].skill_content

    def test_skill_content_lazy_load(self, registry: "SkillRegistry") -> None:
        """skill_content 懒加载，首次访问后缓存。"""
        skill = registry.search_skills()[0]
        assert not skill._body_loaded
        content = skill.skill_content
        assert skill._body_loaded
        assert "Test Skill" in content
        # 再次访问用缓存
        content2 = skill.skill_content
        assert content == content2


# ---------------------------------------------------------------------------
# 2. 脚本发现
# ---------------------------------------------------------------------------


class TestScriptDiscovery:
    """scripts/ 目录扫描测试。"""

    def test_discovers_python_and_bash_scripts(self, registry: "SkillRegistry") -> None:
        """发现 .py 和 .sh 脚本，跳过非脚本文件。"""
        skill = registry.search_skills()[0]
        langs = {s.language for s in skill.scripts}
        assert "python" in langs
        assert "bash" in langs
        # README.md 不算脚本
        paths = [s.path.name for s in skill.scripts]
        assert "README.md" not in paths

    def test_no_scripts_dir(self, tmp_path: Path) -> None:
        """没有 scripts/ 目录时返回空列表。"""
        from skills.registry import SkillRegistry

        skill_dir = tmp_path / "skills" / "no-scripts"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\n---\n", encoding="utf-8")

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        assert r.search_skills()[0].scripts == []


# ---------------------------------------------------------------------------
# 3. 搜索 API
# ---------------------------------------------------------------------------


class TestSearchAPI:
    """搜索过滤测试。"""

    def test_search_by_name(self, registry: "SkillRegistry") -> None:
        """按名称关键词搜索。"""
        skills = registry.search_skills(query="test")
        assert len(skills) == 1

    def test_search_by_description(self, registry: "SkillRegistry") -> None:
        """按描述关键词搜索。"""
        skills = registry.search_skills(query="测试")
        assert len(skills) == 1

    def test_search_no_match(self, registry: "SkillRegistry") -> None:
        """搜索无匹配返回空列表。"""
        skills = registry.search_skills(query="nonexistent")
        assert len(skills) == 0

    def test_search_empty_query_returns_all(self, registry: "SkillRegistry") -> None:
        """空查询返回全部。"""
        skills = registry.search_skills(query="")
        assert len(skills) == 1

    def test_search_by_language(self, registry: "SkillRegistry") -> None:
        """按脚本语言过滤。"""
        skills = registry.search_skills(language="python")
        assert len(skills) == 1

        skills = registry.search_skills(language="nodejs")
        assert len(skills) == 0

    def test_exact_match(self, registry: "SkillRegistry") -> None:
        """精确匹配模式。"""
        skills = registry.search_skills(query="test-skill", exact=True)
        assert len(skills) == 1

        skills = registry.search_skills(query="test", exact=True)
        assert len(skills) == 0

    def test_limit(self, tmp_path: Path) -> None:
        """limit 参数限制返回数量。"""
        from skills.registry import SkillRegistry

        for name in ("a", "b", "c"):
            d = tmp_path / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

        r = SkillRegistry(skill_roots=[tmp_path / "skills"])
        r.refresh()
        assert len(r.search_skills(limit=2)) == 2


# ---------------------------------------------------------------------------
# 4. 挂载 API
# ---------------------------------------------------------------------------


class TestMount:
    """软链挂载测试。"""

    def test_mount_creates_symlink(self, registry: "SkillRegistry", tmp_path: Path) -> None:
        """挂载创建软链到工作空间。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mounted = registry.mount_to_workspace(["test-skill"], workspace)
        assert "test-skill" in mounted

        link = workspace / "skills" / "test-skill"
        assert link.exists()
        # 验证内容可读
        assert (link / "SKILL.md").read_text(encoding="utf-8").startswith("---")

    def test_mount_idempotent(self, registry: "SkillRegistry", tmp_path: Path) -> None:
        """重复挂载不报错，幂等。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mounted1 = registry.mount_to_workspace(["test-skill"], workspace)
        mounted2 = registry.mount_to_workspace(["test-skill"], workspace)
        assert mounted1 == mounted2 == ["test-skill"]

    def test_mount_nonexistent_skill(self, registry: "SkillRegistry", tmp_path: Path) -> None:
        """挂载不存在的 Skill 返回空。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mounted = registry.mount_to_workspace(["nonexistent"], workspace)
        assert mounted == []

    def test_mount_creates_workspace_if_missing(
        self, registry: "SkillRegistry", tmp_path: Path,
    ) -> None:
        """工作空间不存在时自动创建。"""
        workspace = tmp_path / "new_workspace"
        mounted = registry.mount_to_workspace(["test-skill"], workspace)
        assert workspace.exists()
        assert "test-skill" in mounted


# ---------------------------------------------------------------------------
# 5. ResourceSearchTool 集成（detailed 模式 + 挂载）
# ---------------------------------------------------------------------------


class TestResourceSearchSkillMount:
    """_search_skills detailed 模式触发挂载的集成测试。"""

    @pytest.mark.asyncio
    async def test_detailed_mode_triggers_mount(
        self, registry: "SkillRegistry", tmp_path: Path,
    ) -> None:
        """detailed 模式 + workspace → 自动挂载。"""
        from tools.builtin.resource_search.tool import ResourceSearchTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ResourceSearchTool(
            skill_registry=registry,
            search_engine=None,
        )

        result = await tool._search_skills(
            query="test",
            language=None,
            limit=10,
            detailed=True,
            exact=False,
            workspace=str(workspace),
        )

        names, descriptions, details_list = result
        assert len(names) == 1
        assert names[0] == "test-skill"
        assert details_list[0].get("mounted") is True
        assert details_list[0].get("container_path") == "/workspace/skills/test-skill"

        # 验证软链已建立
        link = workspace / "skills" / "test-skill"
        assert link.exists()

    @pytest.mark.asyncio
    async def test_simple_mode_no_mount(
        self, registry: "SkillRegistry", tmp_path: Path,
    ) -> None:
        """simple 模式不触发挂载。"""
        from tools.builtin.resource_search.tool import ResourceSearchTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ResourceSearchTool(
            skill_registry=registry,
            search_engine=None,
        )

        result = await tool._search_skills(
            query="test",
            language=None,
            limit=10,
            detailed=False,
            exact=False,
            workspace=str(workspace),
        )

        names, _, details_list = result
        assert len(names) == 1
        assert details_list[0].get("mounted") is None
        assert not (workspace / "skills").exists()

    @pytest.mark.asyncio
    async def test_detailed_no_workspace_no_crash(
        self, registry: "SkillRegistry",
    ) -> None:
        """detailed 模式无 workspace 时不崩溃。"""
        from tools.builtin.resource_search.tool import ResourceSearchTool

        tool = ResourceSearchTool(
            skill_registry=registry,
            search_engine=None,
        )

        result = await tool._search_skills(
            query="test",
            language=None,
            limit=10,
            detailed=True,
            exact=False,
            workspace="",
        )

        names, _, details_list = result
        assert len(names) == 1
        assert details_list[0].get("mounted") is None


# ---------------------------------------------------------------------------
# 6. 全局单例
# ---------------------------------------------------------------------------


class TestGlobalSingleton:
    """get_global_skill_registry 全局单例测试。"""

    def test_returns_registry(self) -> None:
        """返回有效的 SkillRegistry。"""
        from skills.registry import get_global_skill_registry

        r = get_global_skill_registry()
        assert r is not None
        assert r.is_initialized()

    def test_singleton_identity(self) -> None:
        """多次调用返回同一实例。"""
        from skills.registry import get_global_skill_registry

        r1 = get_global_skill_registry()
        r2 = get_global_skill_registry()
        assert r1 is r2
