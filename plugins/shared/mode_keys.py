"""模式命名空间资源键解析：agent 键 `mode_X/<stem>` → 模式包内 yaml 路径，
技能名 `mode_X/<skill>` → 模式包内技能目录（与 agent 键同构，批 E §3.1）。

设计真值 docs/working/模式体系落地设计_20260915.md §2.3/§3.3：agent 键两级
解析 = 系统注册表（config/agents）未命中 → 模式包目录注册表（约定子目录
`mode_X/agents/*.yaml`，键 `mode_X/<文件名 stem>`，与内核 mode_registry
同构）；两级未命中由调用方 fail-closed。技能插槽与 agents 完全同构
（docs/working/模式包工作模式设计_20260928.md §3.1）：约定目录
`mode_X/skills/<name>/SKILL.md` 免声明即注册，零内核（Rust 扫描不扩）。

双根：用户副本 `<USER_ROOT>/plugins/modes` 优先，回落出厂
`plugins/shared/<mode_*>`（拍平布局，ADR 2026-10-13-plugin-flat-layout；
plugin.json 为包标记，双根同 id 用户赢）。
消费方：context_build（agent 配置装配）、task_submit（派发期磁盘回退）；
find_mode_skill_dir 是技能插槽的名→目录解析面（批 E §3.1），与 isolation
工作空间技能同步源（_copy_skills_to_workspace）同一双根优先序。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 键形态：mode_<mode_name>/<stem>。mode 名与 context_build mode_material 的
# MODE_ID_RE 同口径（小写标识）；stem 限字母数字下划线（杜绝路径分隔符与
# `..` 穿越）。
_MODE_AGENT_KEY_RE = re.compile(r"^mode_([a-z][a-z0-9_]{0,63})/([A-Za-z0-9_]+)$")

# 模式 id 形态（公开面）：小写字母开头的 [a-z0-9_]，≤64 字符。与
# mode_material.MODE_ID_RE 同口径单源在此（project_manager 项目登记
# mode_tags 校验共用，批 B8；mode_material 的私有副本不改，避免翻动管道插件）。
MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 技能名形态：字母数字开头 + 字母数字下划线连字符（技能目录事实命名形，
# 如 code-implement/skill-test-infra；杜绝路径分隔符与 `..` 穿越）。
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 工具名形态（collect_mode_tool_ids 声明面闸）：字母开头的字母数字下划线
# 连字符点号（eval.screen 点号形态为仓内实存工具名）。HOST_GUI 九裸名回避是
# 模式包契约约定（契约文档条款），不在此复制集合（避免双源漂移）。
_TOOL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

# 裸名执行者键形态（系统 agent 同形，research_agent/environment_setup_agent）。
_BARE_AGENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_SHARED_ROOT = Path(__file__).resolve().parent


def parse_mode_agent_key(key: str) -> tuple[str, str] | None:
    """`mode_X/<stem>` → (mode, stem)；裸键/形态不符 → None（系统键走原路）。"""
    matched = _MODE_AGENT_KEY_RE.match(key) if isinstance(key, str) else None
    if matched is None:
        return None
    return matched.group(1), matched.group(2)


def is_skill_name(name: str) -> bool:
    """技能名形态闸（与 _SKILL_NAME_RE 同源）：字母数字开头 + 字母数字下划线
    连字符，杜绝路径分隔符与 `..` 穿越。

    技能目录解析（find_mode_skill_dir）与 mode.yaml `plan_spec` 声明值校验
    （批 B2，agent_manager 同源消费）共用——声明即注册，值必须能按名经
    工作空间并集快照解析到包内技能，不收第二路径形态。
    """
    return isinstance(name, str) and _SKILL_NAME_RE.match(name) is not None


# plan 态派发白名单·系统基线（ADR 2026-09-17 决策 2；批 B4 注册化：本集 =
# 系统基线，注册包 planning_executors 贡献经 collect_planning_executors 聚合
# 后并集判定，见 task_submit plan 态闸）。自 T6a（project_registry 吸收进
# project_manager）起收编本模块：纯数据零 IO，与聚合面同居单源；打样验证不
# 在基线内、注册贡献亦不含打样豁免——先过门（用户审批）再派，从紧口径不变。
PLAN_DISPATCH_WHITELIST = frozenset({"research_agent", "environment_setup_agent"})


def is_planning_executor_key(key: str) -> bool:
    """plan 态白名单执行者键形态闸（批 B4）：`mode_X/<stem>`（模式包注册执行者）
    或裸名（系统 agent 形态，小写标识 + 下划线）。其余形态非法（含穿越/空串/
    大写模式名/连字符 stem）。"""
    if not isinstance(key, str):
        return False
    return parse_mode_agent_key(key) is not None or _BARE_AGENT_KEY_RE.match(key) is not None


def _iter_mode_package_dirs() -> list[Path]:
    """出厂 + 用户双根模式包枚举（收集面用，与 find_mode_package_dir 同一双根
    优先序推导：用户副本优先仅对同名包有意义，集合聚合下两根并集即可）。

    包资格 = mode.yaml 在位：共享根 mode_ 前缀还有平铺文件与非模式包插件
    （mode_material_inject），声明面枚举只认声明了 mode.yaml 的包。
    """
    pkg_dirs: set[Path] = set()
    try:
        import user_space  # noqa: PLC0415

        user_plugins = user_space.user_plugins_dir()
        if user_plugins:
            user_modes = Path(user_plugins) / "modes"
            if user_modes.is_dir():
                pkg_dirs.update(p for p in user_modes.glob("mode_*") if p.is_dir())
    except Exception as exc:  # user_space 不可得 → 仅出厂根（与 find_mode_package_dir 同口径）
        logger.debug("[mode_keys] 用户插件层不可得（仅出厂包聚合）| err=%s", exc)
    pkg_dirs.update(
        p for p in _SHARED_ROOT.glob("mode_*")
        if p.is_dir() and (p / "mode.yaml").is_file()
    )
    return sorted(pkg_dirs)


def _iter_mode_package_dirs_user_first() -> list[Path]:
    """双根模式包枚举（用户副本在前，同 id 先到先得——与 find_mode_package_dir
    胜负序同源；_iter_mode_package_dirs 的有序形态）。"""
    dirs: list[Path] = []
    try:
        import user_space  # noqa: PLC0415

        user_plugins = user_space.user_plugins_dir()
        if user_plugins:
            user_modes = Path(user_plugins) / "modes"
            if user_modes.is_dir():
                dirs.extend(sorted(p for p in user_modes.glob("mode_*") if p.is_dir()))
    except Exception as exc:  # user_space 不可得 → 仅出厂根
        logger.debug("[mode_keys] 用户插件层不可得（仅出厂包聚合）| err=%s", exc)
    dirs.extend(sorted(
        p for p in _SHARED_ROOT.glob("mode_*")
        if p.is_dir() and (p / "mode.yaml").is_file()
    ))
    return dirs


def collect_planning_executors() -> frozenset[str]:
    """Σ 注册包 planning_executors 聚合（批 B4 plan 态白名单注册化）。

    枚举出厂 + 用户双根全部模式包，读 mode.yaml 可选键 planning_executors
    （字符串列表），逐键过 is_planning_executor_key 形态闸后并入集合（重复键
    去重）。缺省未声明 = 零贡献；非法形态条目跳过不贡献（fail-closed——白名单
    语义下缺贡献 = 不放行，安全方向）；mode.yaml 不可读/损坏 = 该包零贡献
    （warn 留痕，不拖垮其余包）。消费方 = task_submit plan 态派发闸（与
    PLAN_DISPATCH_WHITELIST 系统基线并集判定），磁盘读声明面，
    零 agent_manager import、零跨插件服务调用。
    """
    collected: set[str] = set()
    for pkg in _iter_mode_package_dirs():
        try:
            data = yaml.safe_load((pkg / "mode.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "[mode_keys] mode.yaml 不可读，planning_executors 零贡献 | pkg=%s | err=%s",
                pkg.name,
                exc,
            )
            continue
        declared = data.get("planning_executors") if isinstance(data, dict) else None
        if not isinstance(declared, list):
            continue  # 未声明（缺省语义不靠空值表达）= 零贡献
        for key in declared:
            if is_planning_executor_key(key):
                collected.add(key)
            else:
                logger.warning(
                    "[mode_keys] planning_executors 形态非法条目跳过（零贡献）"
                    "| pkg=%s | key=%r",
                    pkg.name,
                    key,
                )
    return frozenset(collected)


def collect_mode_tool_ids() -> dict[str, str]:
    """Σ 声明并入工具聚合（模式工具并入源，ADR 2026-10-12-personal-mode-package-
    form-and-boundary）：枚举出厂 + 用户双根全部模式包，**仅收 mode.yaml 显式
    ``inject_tools: true`` 的包**（守卫 C，包级并入声明），读其 plugin.json
    ``capabilities.tools[].name``（**工具 id 形态**，插件 id 形态被证伪——漂移
    告警每轮误报 + 面外溢），返回 ``{工具名: 模式包插件 id}``。

    守卫 C 动因（仓内实证）：capabilities.tools 兼具"注册进白名单"用途——
    mode_evolution 八件工具（eval_view_suite/proposal_apply/eval.screen 等）
    经包内 agents/evolution.yaml 显式白名单只给自己执行者、刻意不进主聊天面；
    无条件并入会把它们全量灌入一切非任务管道 = 既有模式行为回归。故并入必须
    包级显式声明（声明即意图），未声明包（含全部既有包）零贡献零变化。

    双根同 id 用户副本赢（与 find_mode_package_dir 同一胜负序）；插件清单/
    mode.yaml 不可读、未声明 inject_tools、工具名形态非法 = 该包零贡献
    （warn 留痕，不拖垮其余包；fail-closed——并入面缺贡献 = 不入面，安全
    方向）。消费方 = agent_config_load 非任务管道并入（磁盘读声明面，
    零 agent_manager import；启用判定由消费方经 tool-surface 探针做活注册表
    交集，本函数只管声明面）。
    """
    collected: dict[str, str] = {}
    seen_pkg_ids: set[str] = set()
    for pkg in _iter_mode_package_dirs_user_first():
        manifest_path = pkg / "plugin.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "[mode_keys] plugin.json 不可读，声明工具零贡献 | pkg=%s | err=%s",
                pkg.name, exc,
            )
            continue
        plugin_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(plugin_id, str) or not plugin_id:
            continue
        if plugin_id in seen_pkg_ids:
            continue  # 双根同 id 用户副本已收（先到先得）
        seen_pkg_ids.add(plugin_id)
        if not _mode_declares_tool_injection(pkg):
            continue  # 守卫 C：未显式声明并入 = 零贡献（注册式工具包缺省不入面）
        capabilities = data.get("capabilities")
        tools = capabilities.get("tools") if isinstance(capabilities, dict) else None
        if not isinstance(tools, list):
            continue  # 未声明 = 零贡献（缺省语义不靠空值表达）
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or _TOOL_ID_RE.match(name) is None:
                logger.warning(
                    "[mode_keys] 声明工具名形态非法条目跳过（零贡献）"
                    "| pkg=%s | name=%r",
                    pkg.name, name,
                )
                continue
            if name not in collected:  # 同名工具先到先得（用户副本赢，与包级胜负序一致）
                collected[name] = plugin_id
    return collected


def _mode_declares_tool_injection(pkg: Path) -> bool:
    """包级并入声明读面（守卫 C）：mode.yaml ``inject_tools is True`` = 并入。

    mode.yaml 缺席/不可读/未声明/非 true = False（并入必须显式，收紧方向缺省）。
    """
    try:
        decl = yaml.safe_load((pkg / "mode.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "[mode_keys] mode.yaml 不可读，声明工具零贡献（并入须包级显式声明）"
            "| pkg=%s | err=%s",
            pkg.name, exc,
        )
        return False
    return isinstance(decl, dict) and decl.get("inject_tools") is True


def find_mode_package_dir(mode: str) -> Path | None:
    """模式包目录双根解析：用户副本优先，回落出厂种子。

    用户副本 = `<USER_ROOT>/plugins/modes/mode_X`（装机契约冻结面）；出厂 =
    `plugins/shared/mode_X`（拍平布局，ADR 2026-10-13-plugin-flat-layout）。
    以 plugin.json 存在为包标记；两侧都没有 = None（调用方按未命中处理）。
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
    candidates.append(_SHARED_ROOT / pkg_name)
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
    if not is_skill_name(name):
        return None
    pkg_dir = find_mode_package_dir(mode)
    if pkg_dir is None:
        return None
    skill_dir = pkg_dir / "skills" / name
    return skill_dir if (skill_dir / "SKILL.md").is_file() else None
