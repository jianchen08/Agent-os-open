"""Skill 包入口

提供：
- Skill：单个 Skill 数据模型
- Script：Skill 携带的可执行脚本
- SkillRegistry：Skill 注册表
- get_global_skill_registry()：全局单例入口
"""

from .registry import (
    Script,
    Skill,
    SkillRegistry,
    get_global_skill_registry,
)

__all__ = ["Script", "Skill", "SkillRegistry", "get_global_skill_registry"]
