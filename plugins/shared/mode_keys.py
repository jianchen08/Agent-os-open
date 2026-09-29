"""模式命名空间资源键解析：agent 键 `mode_X/<stem>` → 模式包内 yaml 路径，
技能名 `mode_X/<skill>` → 模式包内技能目录（与 agent 键同构，批 E §3.1）。

设计真值 docs/working/模式体系落地设计_20260915.md §2.3/§3.3：agent 键两级
解析 = 系统注册表（config/agents）未命中 → 模式包目录注册表（约定子目录
`mode_X/agents/*.yaml`，键 `mode_X/<文件名 stem>`，与内核 mode_registry
同构）；两级未命中由调用方 fail-closed。技能插槽与 agents 完全同构
（docs/working/模式包工作模式设计_20260928.md §3.1）：约定目录
`mode_X/skills/<name>/SKILL.md` 免声明即注册，零内核（Rust 扫描不扩）。

双根：用户副本 `<USER_ROOT>/plugins/modes` 优先，回落出厂
`plugins/shared/modes`（plugin.json 为包标记，双根同 id 用户赢）。
消费方：context_build（agent 配置装配）、task_submit（派发期磁盘回退）；
find_mode_skill_dir 是技能插槽的名→目录解析面（批 E §3.1），与 isolation
工作空间技能同步源（_copy_skills_to_workspace）同一双根优先序。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 键形态：mode_<mode_name>/<stem>。mode 名与 context_build mode_material 的
# MODE_ID_RE 同口径（小写标识）；stem 限字母数字下划线（杜绝路径分隔符与
# `..` 穿越）。
_MODE_AGENT_KEY_RE = re.compile(r"^mode_([a-z][a-z0-9_]{0,63})/([A-Za-z0-9_]+)$")

# 技能名形态：字母数字开头 + 字母数字下划线连字符（技能目录事实命名形，
# 如 code-implement/skill-test-infra；杜绝路径分隔符与 `..` 穿越）。
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_SHARED_ROOT = Path(__file__).resolve().parent


def parse_mode_agent_key(key: str) -> tuple[str, str] | None:
    """`mode_X/<stem>` → (mode, stem)；裸键/形态不符 → None（系统键走原路）。"""
    matched = _MODE_AGENT_KEY_RE.match(key) if isinstance(key, str) else None
    if matched is None:
        return None
    return matched.group(1), matched.group(2)


def find_mode_package_dir(mode: str) -> Path | None:
    """模式包目录双根解析：用户副本优先，回落出厂种子。

    用户副本 = `<USER_ROOT>/plugins/modes/mode_X`；出厂 =
    `plugins/shared/modes/mode_X`。以 plugin.json 存在为包标记；
    两侧都没有 = None（调用方按未命中处理）。
    """
    pkg_name = f"mode_{mode}"
    candidates: list[Path] = []
    try:
        import user_space  # noqa: PLC0415

        user_plugins = user_space.user_plugins_dir()
        if user_plugins:
            candidates.append(Path(user_plugins) / "modes" / pkg_name)
    except Exception as exc:  # user_space 不可得 → 仅出厂根
        logger.debug("[mode_keys] 用户插件层不可用（仅出厂种子）| err=%s", exc)
    candidates.append(_SHARED_ROOT / "modes" / pkg_name)
    for candidate in candidates:
        if (candidate / "plugin.json").is_file():
            return candidate
    return None


def find_mode_agent_yaml(key: str) -> Path | None:
    """agent 键（`mode_X/<stem>`）→ 模式包 `agents/<stem>.yaml`；未命中 None。

    仅按文件名 stem 匹配（键即注册名，§2.3 约定即注册），不走 config_id
    兜底——mode 命名空间内不存在第二种键形。
    """
    parsed = parse_mode_agent_key(key)
    if parsed is None:
        return None
    mode, stem = parsed
    pkg_dir = find_mode_package_dir(mode)
    if pkg_dir is None:
        return None
    path = pkg_dir / "agents" / f"{stem}.yaml"
    return path if path.is_file() else None


def find_mode_skill_dir(mode: str, name: str) -> Path | None:
    """技能目录解析：`mode_X/skills/<name>`；未命中 None。

    与 find_mode_agent_yaml 完全同语义（§3.1 技能与 agents 同构）：先经
    find_mode_package_dir 定包（双根同 id 用户副本赢），再在包内按名取
    `skills/<name>/`（SKILL.md 存在为技能标记，约定即注册）；包内未命中
    不跨根回落——双根胜负在包级已定，包内无此技能即不存在。
    """
    if not isinstance(name, str) or _SKILL_NAME_RE.match(name) is None:
        return None
    pkg_dir = find_mode_package_dir(mode)
    if pkg_dir is None:
        return None
    skill_dir = pkg_dir / "skills" / name
    return skill_dir if (skill_dir / "SKILL.md").is_file() else None
