"""模式命名空间资源键解析：agent 键 `mode_X/<stem>` → 模式包内 yaml 路径。

设计真值 docs/working/模式体系落地设计_20260915.md §2.3/§3.3：agent 键两级
解析 = 系统注册表（config/agents）未命中 → 模式包目录注册表（约定子目录
`mode_X/agents/*.yaml`，键 `mode_X/<文件名 stem>`，与内核 mode_registry
同构）；两级未命中由调用方 fail-closed。

双根：用户副本 `<USER_ROOT>/plugins/modes` 优先，回落出厂
`plugins/shared/modes`（plugin.json 为包标记，双根同 id 用户赢）。
消费方：context_build（agent 配置装配）、task_submit（派发期磁盘回退）。
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
