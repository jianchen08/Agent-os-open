"""skill_manager 技能注册表——四源并集 skills/*/SKILL.md 扫描与解析单源（注册权威）。

资源注册收敛 T3：SKILL.md front matter 解析自 resource_search tool.py 整体
迁入（单消费方不进共享根，原位删除）；resource_search 的 skill 面改消费
本注册表服务（skill.list / skill.get），不再自持扫描。

扫描语义（X11-F1 起与 workspace_lifecycle 快照侧同源，P-87 清单盲区修复）：
- 扫描面 = 四源并集（``skill_source_dirs`` 单点，与 _copy_skills_to_workspace
  共用同一目录解析）：仓根 skills/（``repo_root``）→ 出厂模式包
  ``mode_*/skills/``（``mode:<plugin_id>``）→ 用户根模式包（
  ``mode:<plugin_id>``）→ 用户根 skills/（``user_root``）；升优先序，同名
  技能后源胜出（= 快照复制同款「用户副本赢」），清单条目 ``source`` 字段
  标注实际胜出源；
- 每技能一个目录：``<源目录>/<name>/SKILL.md``（大写优先，回落小写
  skill.md）；
- name/description 取 YAML front matter（``---`` 包裹；name 缺失回落
  title，再回落首个 ``# 标题``），正文 = front matter 之后的部分；
- languages 覆盖面 = 目录内任意深度存在 ``*.py`` / ``*.js`` / ``*.sh`` /
  ``*.ps1`` → ``python`` / ``nodejs`` / ``bash`` / ``powershell``（原
  filters.language 侧 rglob 存在性检测的同语义预计算）；
- 坏件（无 SKILL.md / 不可解码）跳过不炸，warning 带路径留痕（P-91：
  跳过必须可见，禁静默缩表），缺 skills 目录 = 该源零贡献。

语义边界：注册表只扩清单可见性，不改装载行为（agent 装载 = file_read 读
文件，不受注册表影响）；装载寻址契约（mode 键技能、工作空间 skills/ 快照）
各自独立。

服务契约（resource_search 经 requires_services 消费；将来「工具技能化」
在此注册权威上展开——方案 §四.4，独立需求）：
- list_skills() → ``[{id, name, description, languages, source}]``（按 id
  排序）；
- get_skill(skill_id) → ``{"found": bool, "skill": {id, name, description,
  languages, content}}``（content = 正文，detailed 模式取数面；解析面与
  list 同一四源并集、同一胜负序——清单可见即可取）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from _root_api import is_skill_name

logger = logging.getLogger(__name__)

# 项目根（skill_manager/skill_registry.py → skill_manager/ → shared/ →
# plugins/ → root），四源并集解析的 base_path 入参。
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# languages 判定表（原 resource_search filters.language ext_map 同源；
# 键集 = 工具契约声明的过滤枚举）。
LANGUAGE_EXTS: dict[str, str] = {
    "python": "*.py",
    "nodejs": "*.js",
    "bash": "*.sh",
    "powershell": "*.ps1",
}

# languages 探测的目录剪枝集（依赖/构建产物重目录：技能包带 node_modules 或
# .venv 时未剪枝的 rglob 会逐技能全树走 4 遍，list/get 侧线性放大——
# video-shotcraft 1495 项实测单技能 33ms，只是当前还没人往里塞依赖树）。
_PRUNE_DIR_NAMES = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "dist", "build"}
)


def _default_sources() -> list[tuple[Path, str]]:
    """四源并集（升优先序附来源标注；与 workspace_lifecycle 快照侧同源单点）。

    目录解析禁止另写：skill_source_dirs 是快照复制与清单扫描的共同实现
    （isolation 包内裸名兄弟 import 需包目录入 sys.path，经
    ensure_isolation_path 单源注入；惰性 import 避免装载期跨包拉起）。
    """
    from _root_api import ensure_isolation_path  # noqa: PLC0415

    ensure_isolation_path()
    from isolation.workspace_lifecycle import skill_source_dirs  # noqa: PLC0415

    return skill_source_dirs(PROJECT_ROOT)


def _pick_skill_dirs(sources: list[tuple[Path, str]]) -> dict[str, tuple[Path, str]]:
    """名 → (技能目录, 来源标注)：升优先序遍历，后源同名覆盖前源（用户副本赢）。"""
    picked: dict[str, tuple[Path, str]] = {}
    for source_dir, label in sources:
        if not source_dir.is_dir():
            continue
        for skill_dir in source_dir.iterdir():
            if skill_dir.is_dir():
                picked[skill_dir.name] = (skill_dir, label)
    return picked


def _skill_md_path(skill_dir: Path) -> Path | None:
    """技能目录的清单文件（大写 SKILL.md 优先，回落小写 skill.md）。"""
    for candidate in ("SKILL.md", "skill.md"):
        path = skill_dir / candidate
        if path.is_file():
            return path
    return None


def detect_languages(skill_dir: Path) -> list[str]:
    """目录内脚本语言覆盖面（任意深度存在性，原 rglob 检测同语义）。

    单遍 os.walk 同时探测四种扩展（原实现每语言一次 rglob = 全树 4 遍），
    依赖/构建产物目录整枝剪掉（``_PRUNE_DIR_NAMES``）；返回序保持
    LANGUAGE_EXTS 声明序。fnmatch 与 rglob 同一匹配语义（Windows 不区分
    大小写，POSIX 区分）。"""
    import fnmatch  # noqa: PLC0415
    import os as _os  # noqa: PLC0415

    found: set[str] = set()
    for _dirpath, dirnames, filenames in _os.walk(skill_dir):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIR_NAMES]
        for filename in filenames:
            for lang, pattern in LANGUAGE_EXTS.items():
                if lang not in found and fnmatch.fnmatch(filename, pattern):
                    found.add(lang)
        if len(found) == len(LANGUAGE_EXTS):
            break
    return [lang for lang in LANGUAGE_EXTS if lang in found]


def parse_skill_md(content: str, fallback_name: str) -> tuple[str, str, str]:
    """解析 SKILL.md 内容，返回 (name, description, body)。

    支持 YAML front matter（``---`` 包裹），否则用首条 ``# 标题`` 作为
    name、首段作为 description。
    """
    name = fallback_name
    description = ""
    body = content

    # YAML front matter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                front = yaml.safe_load(parts[1]) or {}
            except Exception:  # noqa: BLE001 —— 坏 front matter 回落正文推断，不炸
                front = {}
            if isinstance(front, dict):
                name = str(front.get("name") or front.get("title") or name)
                description = str(front.get("description") or "")
            body = parts[2].lstrip("\n")

    # 如果还没有 description，从正文抽取
    if not description:
        lines = body.splitlines()
        for line in lines:
            t = line.strip()
            if t.startswith("# "):
                if name == fallback_name:
                    name = t[2:].strip() or name
            elif t and not t.startswith(("---", "```", "-")):
                description = t
                break

    return name, description, body


def _parse_entry(skill_dir: Path) -> dict[str, Any] | None:
    """读单个技能目录；无清单/不可解码 → None（调用方跳过）。"""
    md = _skill_md_path(skill_dir)
    if md is None:
        return None
    try:
        content = md.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    name, description, body = parse_skill_md(content, skill_dir.name)
    return {
        "id": skill_dir.name,
        "name": name,
        "description": description,
        "languages": detect_languages(skill_dir),
        "content": body,
    }


def list_skills(skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """全量技能元数据（四源并集按 id 排序；坏件跳过并 warning；缺目录源零贡献）。

    skills_dir 显式传入 = 单源覆盖面（测试/定制根，标注 ``repo_root``）；
    缺省 = _default_sources() 四源并集（与快照侧同源）。同名技能按升优先序
    后源胜出，``source`` 标注实际胜出源。
    """
    sources = [(skills_dir, "repo_root")] if skills_dir is not None else _default_sources()
    items: list[dict[str, Any]] = []
    for _name, (skill_dir, label) in sorted(_pick_skill_dirs(sources).items()):
        entry = _parse_entry(skill_dir)
        if entry is None:
            logger.warning("[skill_registry] 坏件跳过（无清单/不可解码）| dir=%s", skill_dir)
            continue
        entry.pop("content")
        entry["source"] = label
        items.append(entry)
    return items


def get_skill(skill_id: str, skills_dir: Path | None = None) -> dict[str, Any]:
    """单技能详情（含正文）；未知 id / 坏件 / 非法形态 → found=false。

    skill_id 先过技能名形态闸（杜绝路径分隔符与 ``..`` 穿越，mode_keys
    同源），非法形态按未找到呈现。解析面与 list_skills 同一四源并集、同一
    胜负序（后源同名胜出，X11-F1：清单可见即可取，list/get 不再分叉）；
    坏件胜出 = found=false（与清单跳过一致，未命中属预期不告警）。
    """
    if not is_skill_name(skill_id):
        return {"found": False}
    sources = [(skills_dir, "repo_root")] if skills_dir is not None else _default_sources()
    hit = _pick_skill_dirs(sources).get(skill_id)
    if hit is None:
        return {"found": False}
    entry = _parse_entry(hit[0])
    if entry is None:
        return {"found": False}
    return {"found": True, "skill": entry}
