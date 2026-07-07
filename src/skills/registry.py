"""Skill 注册表

职责：
1. 扫描本地 skills/ 目录发现所有 Skill
2. 解析 SKILL.md 的 YAML frontmatter
3. 扫描 scripts/ 目录识别可执行脚本
4. 提供搜索 API（按名称 / 描述 / 语言）
5. 提供挂载 API（软链到工作空间，容器内可访问）

设计原则：
- 单职责：只管「找」和「挂」，不管「下载」和「审查」
- 渐进式披露：先扫 frontmatter 索引，按需读全文
- 失败兜底：扫描失败不影响整体，错误记录日志
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 项目根目录：src/skills/ → src/ → project_root/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Skill 默认扫描根目录
DEFAULT_SKILL_ROOTS: list[Path] = [
    _PROJECT_ROOT / "skills",
]

# 脚本后缀到语言映射
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".sh": "bash",
    ".ps1": "powershell",
    ".js": "nodejs",
    ".ts": "nodejs",
}


@dataclass
class Script:
    """Skill 携带的可执行脚本。"""

    path: Path
    language: str

    def to_dict(self) -> dict[str, str]:
        """序列化为字典。"""
        return {"path": str(self.path), "language": self.language}


@dataclass
class Skill:
    """单个 Skill 的数据模型。

    Attributes:
        skill_path: Skill 目录绝对路径
        skill_name: Skill 名称（frontmatter.name 或目录名）
        description: Skill 描述（frontmatter.description 或首段文本）
        scripts: 可执行脚本列表
        frontmatter: YAML frontmatter 原始字典
    """

    skill_path: Path
    skill_name: str = ""
    description: str = ""
    scripts: list[Script] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    _body: str | None = field(default=None, repr=False)
    _body_loaded: bool = field(default=False, repr=False)

    @property
    def skill_content(self) -> str:
        """懒加载 SKILL.md 完整内容。"""
        if not self._body_loaded:
            self._load_body()
        return self._body or ""

    def _load_body(self) -> None:
        """读取 SKILL.md 完整文本。"""
        md_path = self._find_skill_md()
        if md_path and md_path.exists():
            try:
                self._body = md_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning("[Skill] 读取 SKILL.md 失败: %s | %s", md_path, exc)
                self._body = ""
        self._body_loaded = True

    def _find_skill_md(self) -> Path | None:
        """定位 SKILL.md 文件。"""
        for name in ("SKILL.md", "skill.md"):
            p = self.skill_path / name
            if p.exists():
                return p
        return None

    def to_dict(self, detailed: bool = False) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "name": self.skill_name,
            "description": self.description,
            "skill_path": str(self.skill_path),
            "scripts": [s.to_dict() for s in self.scripts],
        }
        if detailed:
            result["skill_content"] = self.skill_content
        return result


class SkillRegistry:
    """Skill 注册表。

    扫描本地 skills/ 目录，解析 SKILL.md，提供搜索和挂载能力。
    """

    def __init__(self, skill_roots: list[Path] | None = None) -> None:
        """初始化注册表。

        Args:
            skill_roots: Skill 根目录列表，默认使用 DEFAULT_SKILL_ROOTS
        """
        self._skill_roots = skill_roots or DEFAULT_SKILL_ROOTS
        self._skills: list[Skill] = []
        self._name_index: dict[str, Skill] = {}
        self._scanned_at: float = 0.0
        self._initialized: bool = False

    def is_initialized(self) -> bool:
        """注册表是否已完成初始化。"""
        return self._initialized

    # ------------------------------------------------------------------
    # 扫描与解析
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """重新扫描所有 Skill 根目录。"""
        self._skills = []
        self._name_index = {}
        for root in self._skill_roots:
            self._scan_root(root)
        self._scanned_at = time.time()
        self._initialized = True
        logger.info(
            "[SkillRegistry] 扫描完成 | roots=%d | skills=%d",
            len(self._skill_roots),
            len(self._skills),
        )

    def _scan_root(self, root: Path) -> None:
        """扫描单个根目录。"""
        if not root.exists() or not root.is_dir():
            return
        try:
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                if self._has_skill_md(entry):
                    try:
                        skill = self._parse_skill(entry)
                        self._skills.append(skill)
                        self._name_index[skill.skill_name.lower()] = skill
                    except Exception as exc:
                        logger.warning(
                            "[SkillRegistry] 解析 Skill 失败: %s | %s",
                            entry,
                            exc,
                        )
        except Exception as exc:
            logger.warning("[SkillRegistry] 扫描目录失败: %s | %s", root, exc)

    @staticmethod
    def _has_skill_md(skill_dir: Path) -> bool:
        """目录中是否存在 SKILL.md。"""
        return (skill_dir / "SKILL.md").exists() or (skill_dir / "skill.md").exists()

    def _parse_skill(self, skill_dir: Path) -> Skill:
        """解析单个 Skill 目录。"""
        md_path = skill_dir / "SKILL.md"
        if not md_path.exists():
            md_path = skill_dir / "skill.md"
        content = md_path.read_text(encoding="utf-8")

        frontmatter, _ = self._split_frontmatter(content)
        name = frontmatter.get("name") or skill_dir.name
        description = frontmatter.get("description", "")

        return Skill(
            skill_path=skill_dir,
            skill_name=name,
            description=description,
            frontmatter=frontmatter,
            scripts=self._scan_scripts(skill_dir),
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """分离 YAML frontmatter 和 Markdown body。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
        if not match:
            return {}, content
        try:
            fm: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
        except Exception:
            fm = {}
        return fm, match.group(2).strip()

    @staticmethod
    def _scan_scripts(skill_dir: Path) -> list[Script]:
        """扫描 scripts/ 目录下的可执行脚本。"""
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.exists():
            return []
        scripts: list[Script] = []
        for f in sorted(scripts_dir.iterdir()):
            if f.is_file() and f.suffix in _EXT_TO_LANGUAGE:
                scripts.append(
                    Script(path=f, language=_EXT_TO_LANGUAGE[f.suffix]),
                )
        return scripts

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search_skills(
        self,
        query: str = "",
        language: str | None = None,
        limit: int = 20,
        exact: bool = False,
    ) -> list[Skill]:
        """搜索本地 Skill。

        Args:
            query: 搜索关键词（空则返回全部）
            language: 脚本语言过滤（python / bash / nodejs / powershell）
            limit: 最大返回数量
            exact: 是否精确匹配名称
        """
        if not self._initialized:
            self.refresh()

        results: list[Skill] = []
        query_lower = query.lower().strip()

        for skill in self._skills:
            # 语言过滤
            if language and not any(s.language == language for s in skill.scripts):
                continue
            # 精确匹配
            if exact:
                if query_lower and query_lower != skill.skill_name.lower():
                    continue
            elif query_lower and (
                query_lower not in skill.skill_name.lower() and query_lower not in skill.description.lower()
            ):
                continue

            results.append(skill)
            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # 挂载（软链到工作空间）
    # ------------------------------------------------------------------

    def mount_to_workspace(
        self,
        skill_names: list[str],
        workspace: str | Path,
    ) -> list[str]:
        """把指定 Skill 软链到工作空间。

        挂载位置：<workspace>/skills/<skill_name>
        容器内路径：/workspace/skills/<skill_name>/

        Args:
            skill_names: 要挂载的 Skill 名称列表
            workspace: 任务工作空间路径

        Returns:
            成功挂载的 Skill 名称列表
        """
        if not self._initialized:
            self.refresh()

        workspace_path = Path(workspace)
        if not workspace_path.exists():
            try:
                workspace_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.error(
                    "[SkillRegistry] 无法创建工作空间: %s | %s",
                    workspace,
                    exc,
                )
                return []

        skills_link_dir = workspace_path / "skills"
        skills_link_dir.mkdir(parents=True, exist_ok=True)

        mounted: list[str] = []
        for name in skill_names:
            skill = self._name_index.get(name.lower())
            if skill is None:
                logger.warning("[SkillRegistry] 未找到 Skill: %s", name)
                continue

            target = skills_link_dir / skill.skill_name
            if target.exists() or target.is_symlink():
                mounted.append(skill.skill_name)
                continue

            source = skill.skill_path.resolve()
            if not source.exists():
                logger.warning("[SkillRegistry] Skill 路径不存在: %s", source)
                continue

            try:
                target.symlink_to(source, target_is_directory=True)
                mounted.append(skill.skill_name)
                logger.info(
                    "[SkillRegistry] Skill 软链已建立: %s → %s",
                    target,
                    source,
                )
            except OSError as symlink_err:
                # Windows 非管理员权限下符号链接可能失败 → 降级为复制
                try:
                    shutil.copytree(source, target, symlinks=True)
                    mounted.append(skill.skill_name)
                    logger.info(
                        "[SkillRegistry] Skill 已复制（软链失败）: %s ← %s",
                        target,
                        source,
                    )
                except Exception as copy_err:
                    logger.error(
                        "[SkillRegistry] Skill 挂载失败: %s | symlink=%s, copy=%s",
                        skill.skill_name,
                        symlink_err,
                        copy_err,
                    )

        return mounted


# ----------------------------------------------------------------------
# 全局单例
# ----------------------------------------------------------------------

_global_registry: SkillRegistry | None = None


def get_global_skill_registry() -> SkillRegistry | None:
    """获取全局 Skill 注册表（懒加载单例）。"""
    global _global_registry  # noqa: PLW0603
    if _global_registry is None:
        try:
            _global_registry = SkillRegistry()
            _global_registry.refresh()
        except Exception as exc:
            logger.error("[SkillRegistry] 全局注册表初始化失败: %s", exc)
            return None
    return _global_registry
