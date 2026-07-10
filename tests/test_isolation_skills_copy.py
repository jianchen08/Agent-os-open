"""技能复制增量同步测试。

覆盖修复点（BUG-FIX-fix_20260629_skills_stale_snapshot）：
- 旧逻辑「目标 skills/ 目录已存在即整体跳过」会把工作空间技能快照冻结在
  首次复制时刻，编排器后续新增的技能永远同步不进去。
- 修复后改为按技能子目录粒度增量同步：已存在的技能保持原样不覆盖，
  仅补齐缺失项，幂等且低成本。

涉及模块：src/isolation/workspace_lifecycle.py::_copy_skills_to_workspace
"""
from __future__ import annotations

from pathlib import Path

from isolation.workspace_lifecycle import WorkspaceLifecycleManager


def _make_manager(base_path: Path) -> WorkspaceLifecycleManager:
    """构造一个仅满足 _copy_skills_to_workspace 依赖的 manager。

    该方法只用到 self._base_path；其余依赖（resource_merge/task_tree/...）
    传 None/空容器即可。__init__ 内的 _record_main_branch 对非 git 目录
    会失败但被 try/except 兜底，不影响构造。
    """
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config={},
        task_tree=None,
        ws_meta_store={},
        base_path=str(base_path),
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
