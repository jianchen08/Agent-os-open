"""安全检查 Input 插件 — 配置驱动的统一参数匹配引擎。

负责在管道循环的输入阶段检查工具执行的安全性，
采用 YAML 规则配置驱动，支持关键词匹配和正则匹配两种模式。

安全检查维度：
1. 路径遍历检测（内置，不依赖配置规则）
2. 敏感系统目录黑名单 + CMD nul 重定向检测（内置）
3. 配置规则匹配（通用引擎，从 YAML 加载）

对于 needs_approval 规则，插件内部通过 human_interaction 服务
创建审批请求并 await 等待用户响应，审批通过后写入 allowed=True。

State 命名空间：
    - pre_decided_results : 拦截方直出的预定结果（ADR 2026-09-28 结果预填；
      security.decision 观测键已随决策面退役）
"""

# config-write-surface-exempt: 主落点user_data_dir;data字面量为user_space导入失败回落防御
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from service import (
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)

# server.py 的 on_load 注入 plugin 引用，据此拿 human-interaction capability。
_PLUGIN_REF: Any = None

# 显式注入的 human-interaction capability handle（公开装配缝，优先于自动解析）。
_HUMAN_INTERACTION_CAP: Any = None


def _set_plugin_ref(ref: Any) -> None:
    """由 server.py on_load 调用，注入 AgentOSPlugin 实例引用。"""
    global _PLUGIN_REF  # noqa: PLW0603
    _PLUGIN_REF = ref


def set_human_interaction_cap(cap: Any) -> None:
    """注入 human-interaction capability handle（公开装配缝）。

    生产路径经 server.py on_load 注入 plugin 引用后按名自动解析，不经本函数；
    显式设定的 handle 优先于自动解析。传 None 恢复自动解析。
    """
    global _HUMAN_INTERACTION_CAP  # noqa: PLW0603
    _HUMAN_INTERACTION_CAP = cap


def _get_human_interaction_cap() -> Any | None:
    """获取 human-interaction capability handle，未注入返回 None。"""
    if _HUMAN_INTERACTION_CAP is not None:
        return _HUMAN_INTERACTION_CAP
    if _PLUGIN_REF is None:
        return None
    try:
        return _PLUGIN_REF.get_capability("human-interaction")
    except (KeyError, AttributeError) as exc:
        # 审批闸的能力底座缺失不能无痕：软拦截兜底前留 warn（含解析来源与
        # 异常摘要），根因（manifest 未声明 / 注入链断）才可观测。
        logger.warning(
            "[security_check] human-interaction capability 解析失败，审批通道降级软拦截 | "
            "source=_PLUGIN_REF.get_capability('human-interaction') | error=%r",
            exc,
        )
        return None


# 前端一次性事件通道（frontend.emit，内核内置能力）——安全规则降级运行时向
# 用户推送显式提示（核心安全闸门禁静默降级）。独立于 _PLUGIN_REF（那是
# human-interaction 形态，调用协议不同）。server.py on_load 注入。
FrontendEmitFn = Callable[[str, dict[str, Any], str], Awaitable[None]]
_frontend_emit: FrontendEmitFn | None = None


def set_frontend_emit(fn: FrontendEmitFn | None) -> None:
    """注入前端事件发射函数（async fn `(event, payload, thread_id)`）。

    server.py 在 on_load 时从 ``plugin.get_capability("frontend")`` 构造注入；
    传 None 清空（未注入时降级提示只留日志，安全检查行为不变）。
    """
    global _frontend_emit  # noqa: PLW0603
    _frontend_emit = fn


from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult  # noqa: E402 — sys.path 装配后导入
from _libs.pipeline_base.types import StateKeys  # noqa: E402
from _root_api import zone_policy  # noqa: E402
from approval_scope import (  # noqa: E402 — 同目录模块，sys.path 装配后导入
    APPROVED_SIGNATURES_KEY,  # noqa: F401 — 再出口（server/tests 消费）
    PERMANENT_WHITELIST as _PERMANENT_WHITELIST,
    PIPELINE_PERMISSION_MODES as _PIPELINE_PERMISSION_MODES,  # noqa: F401 — 再出口（server 消费）
    ApprovalScopeMixin,
    coerce_tool_args as _coerce_tool_args,
    format_parsed_command_list,
    get_grants_caps,
    load_permanent_whitelist,
    load_pipeline_permission_modes,
    save_permanent_whitelist,
    save_pipeline_permission_modes,
    set_grants_caps,  # noqa: F401 — 再出口（tests 经 _mod 注入 grants 两面）
    set_grants_ref_resolver,  # noqa: F401 — 再出口（tests 夹具经 _mod 复位解析缝）
    split_command_segments,
    state_file_path,
    write_audit_event,
    zone_entries_via_project_service,  # noqa: F401 — 再出口（对账测试消费真源读取路径）
)
from sensitive_paths import is_sensitive_path  # noqa: E402 — 同目录模块

from agentos_plugin_sdk.approval_contract import (  # noqa: E402 — sys.path 装配后导入（与同块一致）
    InteractionErrorCode,
    OptionSemantics,
)
from agentos_plugin_sdk.isolation_policy import IsolationPolicyLoader  # noqa: E402
from agentos_plugin_sdk.tool_result_protocol import (  # noqa: E402
    merge_pre_decided,
    tool_result_entry,
)

logger = logging.getLogger(__name__)

# 插件共享根（本文件位于 plugins/shared/security_check/，向上 2 级 = file→
# 插件目录→shared；拍平布局 ADR 2026-10-13）。持久化状态放 shared/data/
# （permission_modes.json）——运行时产物，装机升级卸装不随包走。
_SHARED_ROOT = os.path.dirname(  # noqa: PTH120
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100,PTH120
)

# ── 权限模式（对齐 ZCode/Claude Code/Codex 的处置档位语义）──
# 黑名单制：只作用于"未被 allow 白名单/命令指纹放行的危险工具"，
# 参数未命中安全规则关键词的操作任何档位都直接执行，各档差异只在"命中后的处置"。
# 三条内置底线（路径遍历/敏感系统目录/nul 重定向）任何模式都强制执行。
# 隔离/worktree 会话的免审批默认由旁路档承载（隔离容器即安全边界，OBS-R255-1
# 2026-09-22 统一口径）：用户未显式选择权限档时隔离会话生效档即 bypass；显式选择
# default/accept_edits/auto 三档后以所选档为准，隔离不再豁免黑名单；显式选 bypass
# 即免审批本身（与隔离默认同档同路径）。显式选择判定：_PERMISSION_MODES 表在场即
# 显式，缺省不写表。
# - default      : 命中 block/needs_approval 规则都逐次弹审批
# - accept_edits : file_read/file_write 一律放行；其余工具命中规则仍弹审批
# - auto         : block 规则自动拒绝不打扰；needs_approval 规则才弹审批
# - bypass       : 跳过规则匹配与审批（保留三条内置底线）
PERMISSION_MODES: dict[str, str] = {
    "default": "命中安全规则的操作逐次确认",
    "accept_edits": "文件读写自动放行，其余命中规则才确认",
    "auto": "block 规则自动拒，needs_approval 才弹窗",
    "bypass": "跳过规则匹配与审批，保留内置底线",
}

# 权限模式表：key = session_id（会话稳定键——同会话内主/子管道、任务轮换的
# pipeline_id 各不相同，会话级设置必须以会话为键，否则显式选择被隔离免审批
# 默认吞掉）；写入端 = server.py http.handle 切换端点（前端选择器同键），
# 本插件 execute 读；sidecar 进程内共享；持久化到用户数据根防重启丢失。
_PERMISSION_MODES: dict[str, str] = {}


def _resolve_permission_modes_file(settings: dict[str, Any] | None = None) -> str:
    """落点统一用户空间（读写同源公理）：用户数据根优先，不可用回落配置声明落点。

    安装形态下 _SHARED_ROOT 指向安装目录——直写会随应用升级丢失且与其他
    写面分裂（2026-09-28 配置读写单源化审计）。回落落点经 manifest
    config_files（id=settings）注入的 ``permission_modes_file`` 覆盖，
    缺省 shared/data/（历史缺省不变）。
    """
    try:
        from _root_api import user_data_dir  # noqa: PLC0415

        data_dir = user_data_dir()
        if data_dir:
            return str(data_dir / "permission_modes.json")
    except ImportError:
        pass
    configured = str((settings or {}).get("permission_modes_file") or "").strip()
    if configured:
        return configured
    return os.path.join(_SHARED_ROOT, "data", "permission_modes.json")


_PERMISSION_MODES_FILE = _resolve_permission_modes_file()

#: 动态工具薄转发壳（plugins/shared/search 的 call_tool）工具名。
#: 展开**只认这一个工具名**（白名单式）：否则任意工具都能伪造 `tool`/`arguments`
#: 字段冒充别的工具参与判定（伪造判定键 = 绕开审批）。动态工具本身不在
#: `tool_ids` 白名单里，若只按 `call_tool` 这个名字判定，目标工具整体免审——
#: 必须展开为「目标工具名 + 目标参数」走既有判定链（四档 + 规则 + 位置闸 +
#: 层级闸全部按展开后的键生效）。
DYNAMIC_FORWARD_TOOL = "call_tool"
#: 展开后的判定参数键名（与 call_tool 的 input_schema 一致）。
_TARGET_TOOL_ARG = "tool"
_TARGET_ARGS_ARG = "arguments"


def _effective_tool_call(tc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """取判定的（工具名, 参数）：动态转发壳调用展开为目标工具 + 目标参数。

    只对 `name == DYNAMIC_FORWARD_TOOL` 且声明了非空目标名的条目展开；目标名
    缺失时按本工具原样判定（不明白放行）。非转发壳调用原样返回，判定链其余
    行为零变化。
    """
    name = str(tc.get("name", "") or "")
    args = _coerce_tool_args(tc)
    if name != DYNAMIC_FORWARD_TOOL:
        return name, args
    target = str(args.get(_TARGET_TOOL_ARG) or "").strip()
    if not target:
        return name, args
    inner = args.get(_TARGET_ARGS_ARG)
    return target, inner if isinstance(inner, dict) else {}


def apply_permission_modes_settings(settings: dict[str, Any] | None) -> None:
    """settings 注入到站重解权限模式落点（server.py on_load 落点）。

    模块导入时 config_files 注入尚在途（settings 命名空间晚于 import 到站），
    导入期解析只能拿缺省值；on_load 拿到 settings 后经此重解并重绑模块落点
    ——读（_load_permission_modes）写（_save_permission_modes）同读
    _PERMISSION_MODES_FILE 单键，重绑即读写同源换址。
    """
    global _PERMISSION_MODES_FILE  # noqa: PLW0603
    _PERMISSION_MODES_FILE = _resolve_permission_modes_file(settings)


# ── 管道级免审批/永久白名单持久化（实现面在 approval_scope.py）──────────────
# 落点派生与权限模式表同目录（读写同源单键 _PERMISSION_MODES_FILE）；以下薄
# 包装供 server.py 以既有导入形态消费（模块级表对象与 approval_scope 同一实例）。
def _state_file_path(filename: str) -> str:
    """与权限模式表同目录派生的持久化落点。"""
    return state_file_path(_PERMISSION_MODES_FILE, filename)


def _load_pipeline_permission_modes() -> None:
    """启动加载管道级覆盖表（allowed_modes 沿用四档枚举）。"""
    load_pipeline_permission_modes(
        _state_file_path("permission_modes_pipeline.json"), PERMISSION_MODES
    )


def _save_pipeline_permission_modes() -> None:
    """持久化管道级覆盖表（原子替换）。"""
    save_pipeline_permission_modes(_state_file_path("permission_modes_pipeline.json"))


def _load_permanent_whitelist() -> None:
    """启动加载永久免审批白名单（解析失败按空集 fail-closed）。"""
    load_permanent_whitelist(_state_file_path("permanent_whitelist.json"))


def _save_permanent_whitelist() -> None:
    """持久化永久免审批白名单（原子替换）。"""
    save_permanent_whitelist(_state_file_path("permanent_whitelist.json"))


def audit_event(event: dict[str, Any]) -> None:
    """追加一行审计事件到 security_audit.jsonl（失败留痕不阻断业务）。"""
    write_audit_event(_state_file_path("security_audit.jsonl"), event)


# accept_edits 放行的文件类工具
_FILE_TOOLS: frozenset[str] = frozenset({"file_read", "file_write"})

# 位置闸参数操作表（ADR 2026-09-24-read-deny-write-zones 修订：位置轴管道层
# 统一执法）：工具 → {路径参数名: 操作类}。操作类 "read" 走读黑名单链，其余
# （write/delete/move）走写区链。config ``path_param_operations`` 可覆盖/扩充。
# bash 命令串中的路径非结构化参数，不进本表——维持命令规则轨道（S4 归一化），
# 边界见 ADR 修订节。
_DEFAULT_PATH_PARAM_OPERATIONS: dict[str, dict[str, str]] = {
    # file_read 含目录模式（list_directory 合并件，ADR 2026-10-16）
    "file_read": {"path": "read"},
    "enhanced_search": {"path": "read"},
    "file_write": {"path": "write"},
    "create_directory": {"path": "write"},
    "delete_file": {"path": "delete", "paths": "delete"},
    "move_file": {"source": "move", "destination": "write"},
    "copy_file": {"source": "read", "destination": "write"},
}

# 出厂 skills/ 写区收敛的 bash 命令面扫描（ADR 2026-10-09 批 C-a 红线①）：
# bash 命令串路径非结构化、不进 path_param_operations（ADR 2026-09-24 修订
# 边界），但出厂 skills/ 直写拒必须含旁路档一致执法（否则 fs 工具闸被
# bash 一字绕穿）——故在位置闸阶段对命令参数做**词法有界**扫描：仅写动作
# 标记（重定向/tee/cp/mv/rm 等）之后的词元参与判定，读词面（cat/grep/
# 无标记引用）不扫，出厂可读语义不受影响。已知词法边界（妥协声明）：
# 变量间接（$T/skills/x）、python -c 内嵌写等形态不可判——主执法面是结构化
# fs 工具闸与提案管道，本扫描为旁路兜底；升级触发 = 真机滥用案例。
_BASH_WRITE_MARKER_RE = re.compile(
    r"(?:>>?>?|\b(?:tee|touch|mkdir|cp|mv|rm|chmod|chown|install|rsync|ln)\b"
    r"|\bsed\b[^;&|\n]*?-i\b)"
)


def _bash_write_target_tokens(command: str) -> list[str]:
    """提取命令串中写动作标记之后的词元（写意图候选；按 shell 分段符切段）。"""
    tokens: list[str] = []
    for segment in re.split(r"[;&|\n]", command):
        marker = _BASH_WRITE_MARKER_RE.search(segment)
        if marker is None:
            continue
        tokens.extend(
            t.strip("\"'") for t in segment[marker.end():].split() if t.strip("\"'")
        )
    return tokens


def _load_permission_modes() -> None:
    """启动时从持久化文件加载权限模式表（幂等，失败留痕不阻断）。

    原地清空+更新、绝不重绑定：server.py 经 `from plugin import
    _PERMISSION_MODES` 持有同一对象的引用，重绑定会把写侧（端点持有的旧
    对象）与读侧（本模块的新对象）劈成两张表——端点写入不可见、且
    _save_permission_modes 序列化旧表导致切换条目永不落盘（BUG-15 根因之一）。
    """
    try:
        with open(_PERMISSION_MODES_FILE, encoding="utf-8") as f:
            import json  # noqa: PLC0415

            data = json.load(f)
        if isinstance(data, dict):
            _PERMISSION_MODES.clear()
            _PERMISSION_MODES.update((k, v) for k, v in data.items() if v in PERMISSION_MODES)
            logger.info("[security_check] 权限模式表加载 | sessions=%d", len(_PERMISSION_MODES))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("[security_check] 权限模式表加载失败 | error=%s", exc)


def _save_permission_modes() -> None:
    """持久化权限模式表（写临时文件后原子替换，失败留痕不阻断）。"""
    try:
        import json  # noqa: PLC0415

        target = Path(_PERMISSION_MODES_FILE)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(_PERMISSION_MODES, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:
        logger.warning("[security_check] 权限模式表持久化失败 | error=%s", exc)


# 隔离策略加载器（模块级单例，构造时即从 isolation_policy.yaml 缓存）。
# Host 模式审批以 policy.execution 为单一事实源：
#   command_in_container → 命令执行类，HOST 降级需用户审批
#   host_direct          → 内部 API 工具，免审批
_policy_loader = IsolationPolicyLoader()

# 审批选项单一事实源：发起处（create_choice_request）与消费处（wait_for_choice 返回值
# 解析）共用这一份常量。选项携带 semantics 封闭语义声明（ADR 2026-10-01 决策 2），
# 词形归一由 human 服务在 respond 入口完成——本插件消费端只读 selected_semantics，
# 不再自持 label/别名反查表（别名表随语义契约退役，防开集追加复发）。
_APPROVAL_OPTIONS: list[dict[str, str]] = [
    {"id": "approved_once", "label": "仅本次执行", "semantics": "approve_once"},
    {"id": "approved_remember", "label": "本管道内同命令免批", "semantics": "approve_and_remember"},
    {
        "id": "approved_pipeline_scope",
        "label": "本管道后续免审批",
        "semantics": "approve_pipeline_scope",
        "description": (
            "确认后本管道及其派生子任务内的后续工具调用不再弹审批确认"
            "（切换前会再弹一次警告确认；内置安全底线仍然生效）；"
            "管道结束自动失效，可经撤销端点提前恢复"
        ),
    },
    {
        "id": "approved_permanent_whitelist",
        "label": "本次命令全部加入永久免审批白名单",
        "semantics": "approve_permanent_whitelist",
        "description": (
            "批准本次批次，并将卡上全部命令（复合命令按解析出的每条子命令）"
            "的工具+参数精确指纹加入跨管道跨会话的永久免审批名单，"
            "此后完全相同的命令不再询问；可在设置中逐条移除"
        ),
    },
    {"id": "denied", "label": "拒绝执行", "semantics": "deny"},
]


class SecurityCheckPlugin(ApprovalScopeMixin, IInputPlugin):
    """安全检查 Input 插件。

    配置驱动的统一参数匹配引擎，支持：
    - 黑名单模式（默认）：匹配到规则则拦截
    - 白名单模式：未匹配到规则则拦截

    检查顺序：
    1. 路径遍历检测（内置）
    2. 敏感系统目录黑名单 + nul 重定向检测（内置）
    3. 配置规则匹配（通用引擎）

    优先级：70（校验级，在参数注入之后）
    安全不确定必须停止。

    Attributes:
        _config: 插件配置字典
        _rules: 从 YAML 加载的安全规则列表
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化安全检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用安全检查（默认 True）
                - max_path_depth: 最大路径深度（默认 10）
                - rules_path: 规则配置文件路径（默认 config/plugins/security_check/security_rules.yaml）
                - rules: 直接传入规则列表（如果提供则不从文件加载）
                - path_params: 路径参数名列表（默认 6 个常见路径参数）
                - fuzzy_tool_matching: 工具名模糊匹配（默认 False）。
                  开启后规则 tools 匹配容错：大小写不敏感 + 下划线/连字符等价 +
                  Levenshtein 距离 <= 1，防 LLM 输出 bash-execute vs bash_execute
                  vs BashExecute 漂移导致规则被绕过。
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_path_depth = self._config.get("max_path_depth", 10)
        self._fuzzy_tool_matching = self._config.get("fuzzy_tool_matching", False)
        self._path_param_operations: dict[str, dict[str, str]] = {
            **_DEFAULT_PATH_PARAM_OPERATIONS,
            **(self._config.get("path_param_operations") or {}),
        }
        self._path_params = self._config.get(
            "path_params",
            [
                "path",
                "file_path",
                "filepath",
                "file",
                "filename",
                "paths",
                "file_paths",
                "directory",
                "dir",
                "cwd",
                "destination",
                "dest",
                "target",
                "output_path",
                "working_dir",
                "workdir",
            ],
        )
        # 命令参数名列表：基础检查里用于 CMD 风格 nul 重定向检测
        self._command_params = self._config.get(
            "command_params",
            [
                "command",
                "cmd",
            ],
        )

        # 连续拒绝计数（按工具签名）：防止模型反复提交同一被拦请求导致死循环。
        # 与指纹记忆同作用域（单管道）。指纹记忆 v2 起迁管道级 state 键
        # task.approved_signatures（子任务可读、随管道终态回收），实例字段退役。
        self._rejected_signatures: dict[str, int] = {}
        self._reject_threshold = self._config.get("reject_threshold", 3)

        # 加载安全规则
        self._rules_degraded = False
        self._rules = self._load_rules()
        # 降级提示每实例只发一次（同一降级周期不刷屏）
        self._degrade_notice_sent = False
        # grants 面（分级/账本）降级横幅每管道只发一次（键=pipeline_id，
        # 错误风暴不刷屏；用户裁定 2026-11-19：可见性由横幅承担，不逐调用弹卡）
        self._grants_degraded_notified: set[str] = set()

        # 危险工具轨道 2 数据源：config_files 注入的 builtin_tools_config
        # （见 _get_dangerous_operations / _parse_dangerous_ops_config）
        self._dangerous_ops_by_tool = self._parse_dangerous_ops_config()

    # 注入链失效兜底：内联默认规则（黑名单模式 + 危险命令关键词），避免
    # "规则空 = 安全闸门失效 = 所有工具都弹审批"、任务链路被审批阻塞。
    # 内联表是 YAML（config/plugins/security_check/security_rules.yaml）的降级
    # 子集而非同构：仅承载 dangerous_commands 的命令关键词，protected_paths/
    # ssrf 等规则降级态不随行（路径类底线由内置基础检查独立承载）。子集关系
    # 由对账闸锁定（test_default_rules_fallback：内联规则名 ⊆ YAML 规则名，
    # 漂移即红）。降级时经 execute 首轮向用户显式提示"安全规则降级运行"
    # （禁静默降级）。
    _DEFAULT_RULES: list[dict[str, Any]] = [
        {
            "name": "dangerous_commands",
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [
                {"type": "keyword", "value": kw}
                for kw in (
                    "rm -rf",
                    "del /s",
                    "format",
                    "mkfs",
                    "dd if=",
                    "> /dev/sd",
                    "chmod 777",
                    "chown root",
                    "shutdown",
                    "reboot",
                    "halt",
                    "poweroff",
                    "nc -",
                    "netcat ",
                )
            ],
        },
    ]

    def _load_rules(self) -> list[dict[str, Any]]:
        """从配置或 YAML 文件加载安全规则。

        优先级（高 → 低）：
        1. config 中直接提供的 rules 列表（测试/旧装配缝）
        2. manifest config_files 注入的 security_rules 命名空间
           （plugin.json 声明 config/plugins/security_check/security_rules.yaml，内核按 id
           命名空间合并进 plugin.get_config()，invoker/build_injected_config
           按 config_files[].path 精确定位）——YAML 规则在生产生效的唯一入口
        3. 内联默认规则

        注入链失效（无任何 rules 可用）时回退内联默认规则并置 _rules_degraded：
        规则缺位 = 安全闸门失效（_match_rules 迭代 None 直接 TypeError），
        本方法在任何路径下都不允许返回 None；降级经 execute 首轮向用户显式提示。

        Returns:
            安全规则列表，每条规则包含 name、tools、params、action、patterns
        """
        # 优先使用 config 中直接传入的规则
        if "rules" in self._config and self._config["rules"]:
            return self._config["rules"]

        # 注入配置：manifest config_files 按 id 命名空间合并进 plugin.get_config()
        injected = self._config.get("security_rules")
        if isinstance(injected, dict):
            yaml_rules = injected.get("rules")
            if yaml_rules:
                return yaml_rules

        self._rules_degraded = True
        logger.warning(
            "[%s] 安全规则降级运行：无可用 YAML 规则（注入链失效），回退内联默认规则（%d 关键词）",
            self.name,
            len(self._DEFAULT_RULES[0]["patterns"]),
        )
        return list(self._DEFAULT_RULES)

    def refresh_injected_config(self, config: dict[str, Any] | None) -> None:
        """注入配置到站重导派生面（on_config_changed 钩子落点，server.py 注册）。

        合宿握手只送触发成员的命名空间，本插件常以空配置构造并降级；内核每
        调用现算下发的正确配置到站时经本方法重导两个 config_files 派生面：
        安全规则（降级态翻转）与 dangerous_operations 轨道 2 数据源——两者
        都是构造期快照，不重导则降级保守审批固化（2026-09-29 注入链失效根因）。
        管道内状态（审批指纹/拒绝计数）不重置；规则再次缺位仍按降级回退
        （fail-closed 方向不变），恢复后重臂降级提示（新降级周期不再静默）。
        """
        self._config = config or {}
        previous_degraded = self._rules_degraded
        self._rules_degraded = False
        self._rules = self._load_rules()
        self._dangerous_ops_by_tool = self._parse_dangerous_ops_config()
        if not self._rules_degraded:
            self._degrade_notice_sent = False
            if previous_degraded:
                logger.info(
                    "[%s] 注入配置到站，安全规则重载恢复 | rules=%d",
                    self.name,
                    len(self._rules),
                )

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "security_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 70)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行安全检查。

        检查当前管道状态中的工具调用参数是否安全，
        包括路径遍历、工作目录边界和配置规则匹配。

        Args:
            ctx: 插件执行上下文

        Returns:
            状态更新：拦截时含 pre_decided_results（结果预填，ADR 2026-09-28），
            放行/授权时含写区授权类更新；「仅本管道」批准轮另含补参回写的
            raw_tool_calls（授权参数须随派发面下达，见 _do_work）。
        """
        # 每轮工具调用独立检查，不可短路（否则审批通过后硬底线被跳过，安全闸门失效）。
        await self._notify_rules_degraded_once(ctx)
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _notify_rules_degraded_once(self, ctx: PluginContext) -> None:
        """安全规则降级运行时向用户推送一次显式提示（禁静默降级）。

        注入链失效（config 无 rules 且 config_files 注入的 security_rules 缺
        rules 键）时回退内联默认规则——核心安全闸门降级必须有用户感知，
        经 frontend.emit + warning 日志双通道提示。每实例只推一次（同一降级
        周期不刷屏）；通道未注入或发射失败只留日志，绝不阻断安全检查。
        """
        if not self._rules_degraded or self._degrade_notice_sent:
            return
        self._degrade_notice_sent = True
        emit = _frontend_emit
        if emit is None:
            logger.warning(
                "[%s] frontend.emit 未注入，安全规则降级提示不推前端（仅日志）",
                self.name,
            )
            return
        thread_id = str(ctx.state.get(StateKeys.SESSION_ID) or ctx.state.get("thread_id") or "")
        payload = {
            "thread_id": thread_id,
            "pipeline_id": str(ctx.state.get("pipeline_id") or ""),
            "message": (
                "安全规则降级运行：安全规则 YAML 加载失败（注入链失效），"
                "已回退内置精简规则（仅覆盖危险命令关键词），"
                "建议检查 config/plugins/security_check/security_rules.yaml 注入链路。"
            ),
        }
        try:
            await emit("security_rules_degraded", payload, thread_id)
        except Exception as exc:  # noqa: BLE001 —— 通知是增强能力，失败不阻断安全检查
            logger.warning(
                "[%s] security_rules_degraded 事件推送失败（忽略）: %s",
                self.name,
                exc,
            )

    async def _notify_grants_degraded_once(self, ctx: PluginContext) -> None:
        """grants 面（分级/账本）不可用或异常时推送一次性降级横幅（禁静默、禁弹卡风暴）。

        用户裁定（2026-11-19）：加认证不得改变既有审批逻辑——grants 面缺席/异常
        一律等价于 grants 面不存在（inert 放行，既有审批档与白名单语义零变化），
        可见性由本横幅承担。同一管道只推一次（键=pipeline_id，错误风暴不刷屏）；
        通道未注入或发射失败只留日志，绝不阻断安全检查。镜像
        ``_notify_rules_degraded_once`` 的先例形态。
        """
        pipeline_id = self._current_pipeline_id(ctx)
        if pipeline_id in self._grants_degraded_notified:
            return
        self._grants_degraded_notified.add(pipeline_id)
        emit = _frontend_emit
        if emit is None:
            logger.warning(
                "[%s] frontend.emit 未注入，grants 降级提示不推前端（仅日志）",
                self.name,
            )
            return
        thread_id = str(ctx.state.get(StateKeys.SESSION_ID) or ctx.state.get("thread_id") or "")
        payload = {
            "thread_id": thread_id,
            "pipeline_id": pipeline_id,
            "message": (
                "授权分级面暂不可用，已按既有审批语义放行，不影响审批档与白名单。"
            ),
        }
        try:
            await emit("grants_degraded", payload, thread_id)
        except Exception as exc:  # noqa: BLE001 —— 通知是增强能力，失败不阻断安全检查
            logger.warning(
                "[%s] grants_degraded 事件推送失败（忽略）: %s",
                self.name,
                exc,
            )

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行安全检查逻辑。

        本方法只做编排；两道检查分别委托 ``_run_base_safety_scan`` /
        ``_authorize_tool_calls``，各自可独立理解。

        权限档单点解析（旁路档承载隔离免审批，OBS-R255-1）：
        1. 基础安全检查（路径遍历 / 敏感系统目录黑名单 / nul 重定向）→ 任何
           模式都必须执行，这是防注入、防触碰 OS 核心目录的底线，隔离不能绕过
        2. 生效档 = 旁路（隔离任务未显式选择的免审批默认，或显式/配置选旁路）
           → 基础检查通过即整体放行，不做规则匹配
        3. 其余档 → 按档走：隔离任务豁免环境类门槛（危险工具分类），非隔离
           任务危险工具分类兜底无隔离边界的任意执行，命中规则的参数按档处置
        4. 档位解析优先级见 ``_resolve_permission_mode``：显式选择 > 隔离默认
           （旁路）> 插件配置 > default

        Args:
            ctx: 插件执行上下文

        Returns:
            安全决策结果字典
        """
        if not self._enabled:
            return {}

        # 本轮无待执行调用（LLM 纯文本轮）不需要安全检查；raw_tool_calls 非空
        # ⟺ 本轮有调用待执行（静态化管道下的调用轮判据，ADR
        # 2026-10-02-loopconfig-parallel-foreach）。
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {}

        blocked = self._run_base_safety_scan(ctx, tool_calls)
        if blocked is not None:
            return blocked

        # ── 第二道：位置闸（ADR 2026-09-24-read-deny-write-zones 修订）──
        # 位置轴管道层统一执法：读黑名单链/写区链对所有档位一致执法（含旁路
        # 档——bypass 免审批摩擦，不免区域授权）。写区外触发授权卡，批准的
        # 授权经 zone_updates 落 state（本管道有效）或名单文件（永久）。
        zone_updates: dict[str, Any] = {}
        zone_blocked = await self._enforce_zone_policy(ctx, tool_calls, zone_updates)
        if zone_blocked is not None:
            return {**zone_blocked, **zone_updates}
        if zone_updates:
            # 「仅本管道」批准轮：补参只落在本地快照上，须把 raw_tool_calls
            # 并入返回更新才能到达派发面（引擎每次调用传 state 快照、只合并
            # state_updates；tool_args_inject 同通道回写，tool_core 据此派发）。
            zone_updates[StateKeys.RAW_TOOL_CALLS] = tool_calls

        # ── 第三道：按模式分流 ──

        # 权限档单点解析：管道级覆盖（审批卡授予）> 显式选择 > 隔离默认
        # （旁路）> 插件配置 > default。命中旁路 → 基础检查通过即整体放行
        # （isolation_level 是隔离唯一真相源，解析优先级见 _resolve_permission_mode）。
        execution_contexts = ctx.state.get("execution_contexts", [])
        self._remember_lineage(ctx)
        isolated = self._is_isolated(ctx, execution_contexts)
        mode = self._resolve_permission_mode(ctx, isolated=isolated)
        if mode == "bypass":
            # 管道覆盖档的来源是「本管道后续免审批」授予：终审规则仍逐次人审
            # （免审批摩擦，不豁免终审；与手动会话级 bypass 的既有语义区分）。
            # 规则降级态无从匹配终审 → 整体压制覆盖（回落既有链弹卡，保守优先）。
            override = None if self._rules_degraded else self._pipeline_override_mode(ctx)
            if override:
                final_updates = await self._enforce_final_approval(ctx, tool_calls)
                if final_updates is not None:
                    return {**final_updates, **zone_updates}
            # 高风险类闸（P1，ADR D3：旁路档免审批摩擦不免 device_control/financial
            # 逐次人审；deny 恒拒）。grants 分级面缺席/异常 → inert，既有免审批语义
            # 零变化（异常降级横幅一次性提示，用户裁定 2026-11-19）。
            high_risk_updates = await self._enforce_grants_high_risk_bypass(ctx, tool_calls)
            if high_risk_updates is not None:
                return {**high_risk_updates, **zone_updates}
            if override:
                logger.info(
                    "[%s] 管道覆盖档免审批放行（终审规则未命中），基础检查已通过",
                    self.name,
                )
            else:
                logger.info("[%s] 旁路档免审批放行（隔离默认或显式选择），基础检查已通过", self.name)
            return {**zone_updates}

        decision = await self._authorize_tool_calls(ctx, tool_calls, isolated=isolated, mode=mode)
        if decision is not None:
            return {**decision, **zone_updates}

        return {**zone_updates}

    def _run_base_safety_scan(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """第一道：基础安全检查（路径遍历 + 敏感系统目录黑名单 + nul 重定向）。

        任何模式都必须执行，这是防注入、防触碰 OS 核心目录的底线，隔离不能绕过。
        host 模式不做工作目录越界检查（不管路径）。违规时软拦截——反馈给 LLM
        让它改正，不杀引擎。

        Returns:
            违规时的软拦截决策字典；全部通过返回 None。
        """
        for tc in tool_calls:
            # 判定键展开：call_tool 按目标工具名 + 目标参数过三道底线（A13：
            # 只认这一个工具名，防伪造 target 字段骗过展开）。
            tool_name, args = _effective_tool_call(tc)

            # 1. 路径遍历检测
            traversal_reason = self._check_path_traversal(args)
            if traversal_reason:
                logger.warning(
                    "[%s] Blocked path traversal | tool=%s | reason=%s",
                    self.name,
                    tool_name,
                    traversal_reason,
                )
                return self._soft_block(ctx, tool_name, f"路径遍历攻击被拦截: {traversal_reason}")

            # 2. 敏感系统目录黑名单（禁止触碰 OS 核心目录）
            sensitive_reason = self._check_sensitive_paths(args)
            if sensitive_reason:
                logger.warning(
                    "[%s] Blocked sensitive system path | tool=%s | reason=%s",
                    self.name,
                    tool_name,
                    sensitive_reason,
                )
                return self._soft_block(ctx, tool_name, f"敏感系统目录被拦截: {sensitive_reason}")

            # 3. CMD 风格 nul 重定向检测（Git Bash/Linux 下会创建名为 nul 的垃圾文件）
            nul_reason = self._check_nul_redirect(args)
            if nul_reason:
                logger.warning(
                    "[%s] Blocked CMD-style nul redirect | tool=%s | reason=%s",
                    self.name,
                    tool_name,
                    nul_reason,
                )
                return self._soft_block(
                    ctx,
                    tool_name,
                    f"CMD 风格重定向被拦截: {nul_reason}（Git Bash 下请用 2>/dev/null）",
                )
        return None

    async def _enforce_zone_policy(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
        zone_updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """第二道：位置闸（ADR 2026-09-24-read-deny-write-zones 修订）。

        对声明在 ``path_param_operations`` 里的工具路径参数统一执法（判定单源
        zone_policy）：读走黑名单链（仓库拒绝集 + read_deny），写走写区链
        （根锚 ∪ 名单 entries ∪ 管道级授权；仓库自保目录恒拒）。所有档位一致
        执法——含旁路档（bypass 免审批摩擦，不免区域授权），故本阶段位于
        模式分流之前。写区外 → 弹授权卡（两选项：仅本管道 / 永久写入配置），
        批准的授权经 ``zone_updates`` 落 state（管道级）或名单文件（永久），
        并补写当前调用的 args（字符串形态归一回写 tc["args"]）。调用方须把
        补参后的 raw_tool_calls 经 state_updates 回写——引擎只合并返回更新，
        就地改不过插件边界；param_inject 注入发生在本阶段之前，其注入键供
        后续轮次调用（后续轮的授权由本闸经 state 键放行，无需再补参）。

        Returns:
            违规（拒绝/授权未批）时的软拦截决策字典；全部放行返回 None。
        """
        workspace = ctx.state.get("workspace", "") or None
        project_root = ctx.state.get("project_root", "") or None
        session_zones = zone_policy.load_session_zones(ctx.state)
        # 容器路由对齐（R11）：execution_contexts 与 raw_tool_calls 按下标对齐
        # （isolation_guard 同循环构造）；provider=docker 的调用在容器内文件系统
        # 执行（外部容器无宿主挂载），宿主写区/读黑名单对其路径参数不 governing
        # ——容器绝对路径（/app/...）在宿主 resolve 会得到盘符根假路径（真跑
        # 实证：E:\app\output 误判写区外弹授权卡，管道卡死等人工 24h）。
        # 键不对齐（长度不一致）时不豁免（fail-closed）。bash 命令词法扫描
        # （出厂 skills/ 直写兜底）不受豁免，保持全量执法。
        contexts = ctx.state.get("execution_contexts", [])
        aligned = contexts if len(contexts) == len(tool_calls) else []
        for idx, tc in enumerate(tool_calls):
            # 判定键展开：目标工具若是 file_write 等路径类工具，位置闸按目标参数
            # 执法（动态面不得成为绕开写区授权的通道）。
            tool_name, args = _effective_tool_call(tc)
            container_routed = bool(aligned and aligned[idx].get("provider") == "docker")
            param_ops = self._path_param_operations.get(tool_name)
            if isinstance(tc.get("args", tc.get("arguments")), str) and args:
                # 字符串形态归一即回写：授权补参须落在派发对象上（tool_core
                # 派发读 args 键；解析副本随弃，原地补参不可见）。
                tc["args"] = args
            if container_routed:
                continue
            for param, op in (param_ops or {}).items():
                raw_value = args.get(param)
                if not raw_value:
                    continue
                values = raw_value if isinstance(raw_value, list) else [raw_value]
                for value in values:
                    if not isinstance(value, str) or not value.strip():
                        continue
                    blocked = await self._enforce_single_path(
                        ctx,
                        tool_name=tool_name,
                        args=args,
                        value=value,
                        op=op,
                        workspace=workspace,
                        project_root=project_root,
                        session_zones=session_zones,
                        zone_updates=zone_updates,
                    )
                    if blocked is not None:
                        return blocked
            # 出厂 skills/ 旁路兜底：命令串写动作词元扫（路径非结构化不进操作
            # 表，见 _BASH_WRITE_MARKER_RE 注解；判定单源与结构化面同源）。
            for param in self._command_params:
                command = args.get(param)
                if not isinstance(command, str) or not command:
                    continue
                for token in _bash_write_target_tokens(command):
                    try:
                        resolved = zone_policy.resolve_anchored(token, workspace, project_root)
                    except ValueError:
                        continue  # 无锚无从判定：词法兜底不拦，结构化面与工具层兜底执法
                    deny = zone_policy.factory_skills_write_denied(resolved)
                    if deny is not None:
                        logger.warning(
                            "[%s] 位置闸拒绝 bash 直写出厂 skills/ | tool=%s | token=%s",
                            self.name,
                            tool_name,
                            token,
                        )
                        return self._soft_block(ctx, tool_name, deny)
        return None

    async def _enforce_single_path(
        self,
        ctx: PluginContext,
        *,
        tool_name: str,
        args: dict[str, Any],
        value: str,
        op: str,
        workspace: str | None,
        project_root: str | None,
        session_zones: list[str],
        zone_updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """单条路径的位置闸判定（操作类 read 走读链，其余走写区链）。"""
        try:
            resolved = zone_policy.resolve_anchored(value, workspace, project_root)
        except ValueError:
            # 无锚（workspace/project_root 均未注入）：位置闸无从判定，交由
            # 工具层 fail-closed 兜底（fs_tools 相对路径拒绝）——不在此软拦截。
            return None

        if op == "read":
            # 锚内豁免（用户裁定 2026-09-28）：任务工作区常驻仓库 .ai_workspaces，
            # 锚内读不套读黑名单——与写链 within_root_anchors 先行同序；工具层
            # fs_tools 根锚分流本就放行锚内读，此处对齐消除双层漂移（事故
            # 2026-09-28：模型列目录工具 path="." 列自己工作区被拦 3 次熔断；
            # 该能力现归 file_read 目录模式）。
            if zone_policy.within_root_anchors(resolved, workspace, project_root):
                return None
            # 锚外读链携锚入判定（用户裁定 2026-09-29）：工作空间根作为内建
            # read_allow 前缀参与放行——项目工作树是 sessions 的兄弟目录，锚
            # 覆盖不到，不携锚则撞仓库拒绝集弹读授权卡（事故 2026-09-29：
            # 无人值守停泊）。写分支不携此参（跨会话写污染防线）。
            _zone_lists = await self._zone_name_lists()
            allow, reason = zone_policy.read_verdict(
                resolved,
                zone_policy.load_session_read_grants(ctx.state),
                workspace=workspace,
                project_root=project_root,
                read_allow=_zone_lists["read_allow"],
                read_deny=_zone_lists["read_deny"],
            )
            if allow:
                return None
            # 黑名单命中不机器硬拒（用户裁定 2026-09-28）：弹读授权卡交用户
            # 裁定，批准（仅本管道/永久）即放行本次。
            logger.info(
                "[%s] 位置闸读黑名单命中，转读授权卡 | tool=%s | path=%s | reason=%s",
                self.name,
                tool_name,
                resolved,
                reason,
            )
            return await self._authorize_grant_card(
                ctx,
                tool_name=tool_name,
                args=args,
                resolved=resolved,
                zone_updates=zone_updates,
                write=False,
            )

        # 出厂 skills/ 写区收敛（ADR 2026-10-09 批 C-a 红线①）：恒拒且不可
        # 授权（授权卡会开出第二条写路径，破坏唯一写路径 = 进化提案管道）。
        # 置于根锚放行之前——dev 场景 project_root 即仓库根，锚内快捷放行会
        # 遮蔽本判定；所有档位一致执法（含旁路档）。
        factory_deny = zone_policy.factory_skills_write_denied(resolved)
        if factory_deny is not None:
            logger.warning(
                "[%s] 位置闸拒绝直写出厂 skills/ | tool=%s | path=%s",
                self.name,
                tool_name,
                resolved,
            )
            return self._soft_block(ctx, tool_name, factory_deny)

        # 根锚内（任务自己的工作区/项目根）先于一切放行——锚内不套自保拒绝，
        # 与工具层兜底校验同序（fs_tools._check_workspace_path 根锚先行）。
        if zone_policy.within_root_anchors(resolved, workspace, project_root):
            return None
        allow, reason = zone_policy.write_verdict(
            resolved, session_zones, write_zones=(await self._zone_name_lists())["entries"]
        )
        if allow:
            return None
        if reason is not None:
            # 仓库自保目录等硬拒绝：不可授权，直接拦
            logger.warning(
                "[%s] 位置闸拒绝写入（系统自保） | tool=%s | path=%s | reason=%s",
                self.name,
                tool_name,
                resolved,
                reason,
            )
            return self._soft_block(ctx, tool_name, reason)
        # 写区外：弹授权卡（两选项），批准的授权落 state/名单后放行本次
        return await self._authorize_grant_card(
            ctx,
            tool_name=tool_name,
            args=args,
            resolved=resolved,
            zone_updates=zone_updates,
            write=True,
        )

    async def _zone_name_lists(self) -> dict[str, list[str]]:
        """授权名单三节（project.load_zones 服务，T6a）。

        位置闸判定链消费面：读链取 read_allow/read_deny，写链取 entries。
        服务不可用 → 空名单（范围收缩，安全侧）+ error 留痕——降级语义与
        原共享根名单模块缺失形态一致，判定链本身不阻断。
        """
        try:
            if _PLUGIN_REF is None:
                raise RuntimeError("plugin ref not injected")
            handle = _PLUGIN_REF.get_capability("project")
            result = await handle.call("load_zones", {})
            if not isinstance(result, dict) or "error" in result:
                raise RuntimeError(str(result.get("error") if isinstance(result, dict) else result))
            return {
                section: [str(e) for e in (result.get(section) or []) if str(e or "").strip()]
                for section in ("entries", "read_deny", "read_allow")
            }
        except Exception as exc:  # noqa: BLE001 — 名单读面降级可见（error 级留痕）
            logger.error("[%s] 授权名单服务不可用，按空名单处理（范围收缩）: %s", self.name, exc)
            return {"entries": [], "read_deny": [], "read_allow": []}

    async def _authorize_grant_card(
        self,
        ctx: PluginContext,
        *,
        tool_name: str,
        args: dict[str, Any],
        resolved: Path,
        zone_updates: dict[str, Any],
        write: bool,
    ) -> dict[str, Any] | None:
        """区域授权卡（读写共用骨架）：三选项（仅本管道 / 永久写入配置 / 拒绝执行）。

        写面（ADR 2026-09-24-read-deny-write-zones）：写区外路径弹卡，批准的
        授权经 ``zone_updates`` 落 state 键 ``task.authorized_write_zones``
        （管道级，随管道终态回收）或名单文件 entries 节（永久 =
        project.add_zone 服务（locked 写面唯一系统通道，T6a）；仓库自保
        目录在调用方先行硬拒（不可授权）。
        读面（用户裁定 2026-09-28）：读黑名单（仓库拒绝集/read_deny）命中
        不机器硬拒，弹卡交用户裁定；批准落 state 键
        ``task.authorized_read_zones`` 或名单 read_allow 节。

        卡面经 human-interaction（与审批卡同通道）；批准与落盘都在本插件内
        完成，LLM 无法伪造授权。拒绝/超时/取消 → 软拦截。

        Args:
            write: True = 写区授权卡（authorized_zones / entries 名单）；
                False = 读授权卡（authorized_read_zones / read_allow 名单）。
        """
        grant_dir = zone_policy.grant_dir_of(resolved)
        hi_cap = _get_human_interaction_cap()
        if hi_cap is None:
            logger.warning(
                "[%s] human-interaction capability not injected; zone grant soft-block",
                self.name,
            )
            if write:
                return self._soft_block(
                    ctx,
                    tool_name,
                    f"路径 {grant_dir} 不在写区内，且交互服务不可用无法发起授权",
                )
            return self._soft_block(
                ctx,
                tool_name,
                f"路径 {grant_dir} 位于读黑名单，且交互服务不可用无法发起授权",
            )

        if write:
            title = f"授权写入 {grant_dir}"
            description = f"工具 {tool_name} 要写的路径在写区外。批准后写操作按会话权限档执行；拒绝则本次申请作废。"
            # 选项语义声明（ADR 2026-10-01 决策 2）：human 归一点据此把任何词形
            # 归一到规范 id/语义；消费端只认 grant_write* 语义。deny 项（与审批卡
            # 同词形）供用户显式拒绝——human 侧武装拒绝记忆（同卡重试不弹卡），
            # 消费端落下方不匹配分支按拒绝软拦截。
            options: list[dict[str, str]] = [
                {"id": "pipeline", "label": "仅当前会话", "semantics": "grant_write"},
                {"id": "permanent", "label": "永久写入配置", "semantics": "grant_write_permanent"},
                {"id": "denied", "label": "拒绝执行", "semantics": "deny"},
            ]
        else:
            title = f"授权读取 {grant_dir}"
            description = (
                f"工具 {tool_name} 要读的路径位于读黑名单（运行时/产物区或"
                "用户排除区）。批准后授权范围内读取放行；拒绝则本次申请作废。"
            )
            options = [
                {"id": "pipeline", "label": "仅当前会话", "semantics": "grant_read"},
                {"id": "permanent", "label": "永久读取配置", "semantics": "grant_read_permanent"},
                {"id": "denied", "label": "拒绝执行", "semantics": "deny"},
            ]

        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        try:
            create_res = await hi_cap.call(
                "create_choice",
                {
                    "session_id": session_id,
                    "thread_id": session_id,
                    "tab_id": "",
                    "title": title,
                    "description": description,
                    "options": options,
                    "priority": "high",
                    "user_id": ctx.state.get("user_id", ""),
                    # 审批非阻塞 M1：category 由发起方显式传（设计 §四——
                    # 不由后端按标题猜测），权限审批类恒 permission。
                    "category": "permission",
                    # 卡片来源四元组（管道 state 权威值）：授权卡与审批卡同族，
                    # 必须可见发起 agent（名/层级）/会话/管道，缺项由 human 侧省略。
                    "agent_id": str(ctx.state.get("agent.id", "") or ""),
                    "agent_level": str(ctx.state.get(StateKeys.AGENT_LEVEL, "") or ""),
                    "pipeline_id": str(ctx.state.get("pipeline_id", "") or ""),
                    "agent_name": str(ctx.state.get("context.agent_name", "") or ""),
                },
            )
            if not isinstance(create_res, dict) or create_res.get("error"):
                raise RuntimeError(f"create_choice failed: {create_res}")
            request_id = create_res.get("request_id", "-")
            wait_res = await hi_cap.call(
                "wait_for_choice",
                {"request_id": request_id, "timeout": 86400},
                timeout=86500.0,
            )
            if not isinstance(wait_res, dict):
                raise RuntimeError(f"wait_for_choice returned non-dict: {wait_res}")
            if wait_res.get("error"):
                raise RuntimeError(str(wait_res["error"]))
        except Exception as e:  # noqa: BLE001 — 拒绝/超时/取消/通道故障统一软拦截
            if write:
                logger.warning(
                    "[%s] 写区授权未批准 | tool=%s | dir=%s | err=%s",
                    self.name,
                    tool_name,
                    grant_dir,
                    e,
                )
                return self._soft_block(
                    ctx,
                    tool_name,
                    f"写区授权未批准（拒绝/超时/取消）：路径 {grant_dir} 不在写区内，写入被拒绝",
                )
            logger.warning(
                "[%s] 读授权未批准 | tool=%s | dir=%s | err=%s",
                self.name,
                tool_name,
                grant_dir,
                e,
            )
            return self._soft_block(
                ctx,
                tool_name,
                f"读授权未批准（拒绝/超时/取消）：路径 {grant_dir} 位于读黑名单，读取被拒绝",
            )

        # 消费端只读语义（ADR 2026-10-01 决策 2）：human 归一点已把任何词形
        # （id/label/历史别名 approve/approved 等）收敛为规范 id + 封闭语义；
        # 授权语义按卡的轴校验——写卡只认 grant_write*，读卡只认 grant_read*
        #（跨轴语义=契约违约，按拒绝 fail-closed）。语义缺空（无契约的旧卡/异常
        # 通道）一律按拒绝结算。
        semantics = str(wait_res.get("selected_semantics", "") or "")
        grant_write_sem = semantics in (
            OptionSemantics.GRANT_WRITE.value,
            OptionSemantics.GRANT_WRITE_PERMANENT.value,
        )
        grant_read_sem = semantics in (
            OptionSemantics.GRANT_READ.value,
            OptionSemantics.GRANT_READ_PERMANENT.value,
        )
        if (write and not grant_write_sem) or (not write and not grant_read_sem):
            explicit_deny = semantics == OptionSemantics.DENY.value
            logger.warning(
                "[%s] %s | tool=%s | selected=%s | semantics=%s",
                self.name,
                ("用户显式拒绝授权" if explicit_deny else f"{'写区' if write else '读'}授权语义不匹配（按拒绝）"),
                tool_name,
                wait_res.get("selected_option", ""),
                semantics,
            )
            # 授权卡下方自由文本随 feedback 回传：用户说明/改法进入工具返回值。
            feedback = str(wait_res.get("feedback") or "").strip()
            suffix = f"；用户反馈: {feedback[:2000]}" if feedback else ""
            if write:
                return self._soft_block(
                    ctx, tool_name, f"写区授权未批准：路径 {grant_dir} 不在写区内{suffix}"
                )
            return self._soft_block(
                ctx, tool_name, f"读授权未批准：路径 {grant_dir} 位于读黑名单{suffix}"
            )

        state_key = zone_policy.STATE_KEY if write else zone_policy.STATE_KEY_READ
        if semantics in (OptionSemantics.GRANT_WRITE.value, OptionSemantics.GRANT_READ.value):
            pipeline_id = ctx.state.get("pipeline_id", "")
            if not pipeline_id:
                return self._soft_block(
                    ctx,
                    tool_name,
                    f"无法定位所属会话，「仅当前会话」授权不可用；可重新发起并选择永久授权：{grant_dir}",
                )
            base_list = (
                zone_policy.load_session_zones(ctx.state) if write else zone_policy.load_session_read_grants(ctx.state)
            )
            merged = base_list if grant_dir in base_list else [*base_list, grant_dir]
            updates_json = json.dumps(merged)
            # state 落盘（后续轮经 param_inject 注入/本闸读到）+ 当前调用参数
            # 补授权（调用方把补参后的 raw_tool_calls 经 state_updates 回写，
            # 当次派发即携带；写入 args 的对象已由本闸归一回写 tc["args"]）。
            zone_updates[state_key] = updates_json
            ctx.state[state_key] = updates_json
            args["authorized_zones" if write else "authorized_read_zones"] = updates_json
            logger.info(
                "[%s] 管道级%s授权生效 | pipeline=%s | dir=%s",
                self.name,
                "写区" if write else "读取",
                pipeline_id,
                grant_dir,
            )
            return None

        # 永久授权：locked 名单唯一系统通道（名单文件即时生效，工具层兜底
        # 校验每次调用重读名单，无需补参数）。T6a 起写面经 project.add_zone
        # 服务（project_manager provides 直连）。
        try:
            if _PLUGIN_REF is None:
                raise RuntimeError("plugin ref not injected")
            handle = _PLUGIN_REF.get_capability("project")
            section = "entries" if write else "read_allow"
            result = await handle.call("add_zone", {"path": grant_dir, "section": section})
            if isinstance(result, dict) and "error" in result:
                return self._soft_block(ctx, tool_name, str(result["error"]))
        except ValueError as exc:
            return self._soft_block(ctx, tool_name, str(exc))
        except Exception as e:  # noqa: BLE001 — 名单服务不可用等降级可见
            logger.error("[%s] 永久授权落盘失败 | dir=%s | err=%s", self.name, grant_dir, e)
            return self._soft_block(ctx, tool_name, f"永久授权落盘失败: {e}")
        logger.info("[%s] 永久%s授权落盘 | dir=%s", self.name, "写区" if write else "读取", grant_dir)
        return None

    async def _authorize_tool_calls(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
        *,
        isolated: bool,
        mode: str,
    ) -> dict[str, Any] | None:
        """逐工具授权：免审判定声明推导制 → 规则/指纹/权限模式（ADR 2026-10-01 决策 5）。

        免审判定为**声明推导**：无 dangerous_operations 声明 ∧ 非
        command_in_container = 免审（数据源唯一：builtin_tools_config 注入 +
        isolation_policy.yaml 执行环境，无独立工具名词汇表）。参数命中声明的
        工具进规则轨道——未命中
        规则的参数照常放行，常规读写不因"存在声明"而弹审批。

        隔离豁免「环境类」检查：isolated=True 时跳过危险工具分类门槛
        （命令执行类 / dangerous_operations 声明），隔离边界承担执行环境风险，
        未命中规则的参数不弹审批。生效档为旁路时不会进入本方法（_do_work 已
        整体放行），其余档命中黑名单规则按所选档处置。

        Args:
            ctx: 插件执行上下文
            tool_calls: 工具调用列表
            isolated: 是否隔离任务（豁免环境类门槛）
            mode: ``_do_work`` 解析好的生效权限档（单点解析，避免二次判定）

        Returns:
            需要处置（软拦截/弹审批）时的决策字典；全部放行返回 None。
        """
        # 本批卡面覆盖的全部命令指纹：授予"同命令免批/永久白名单"时一次
        # 覆盖卡上展示的所有命令（含复合命令解析出的每条子命令）。
        batch_signatures = self._approval_batch_signatures(ctx, tool_calls, isolated=isolated)
        for tc in tool_calls:
            # 判定键展开：四档 + 规则匹配 + 危险工具分类 + 命令指纹全部按
            # 「目标工具名 + 目标参数」执行（动态调用与直接调用同判定）。
            tool_name, args = _effective_tool_call(tc)

            # 免审判定（声明推导）：非隔离 ∧ 无危险声明命中 → 直接放行；
            # 第一道内置底线（路径遍历/敏感目录/nul）已在本闸之前强制执行。
            if not isolated and not self._is_dangerous_tool(ctx, tool_name, args):
                continue

            # grants 分级裁决（规则匹配前查表，ADR 2026-11-19 执法点）：verdict
            # 只在规则未覆盖分支生效（规则面优先，grant 永不豁免规则裁决）。
            # grants 面缺席 → ("unavailable", None)、调用异常 → ("error", None)，
            # 两者同构 inert 处置（既有语义零变化，用户裁定 2026-11-19）。
            action_key = self._grants_action_key(tool_name, args)
            policy_cap, _ledger_cap = get_grants_caps()
            grants_status, grants_verdict = await self._grants_classify(policy_cap, tool_name, action_key)

            # ── 进入授权：allow 白名单/已记忆指纹放行，其余按权限模式处置
            #    （未命中规则视为参数安全放行，命中 block/needs_approval 才拦截）──
            action, rule_name = self._match_rules(tool_name, args)
            if action == "allow":
                logger.info(
                    "[%s] 危险工具参数命中白名单，放行 | tool=%s | rule=%s",
                    self.name,
                    tool_name,
                    rule_name,
                )
                continue

            # 命令免审批按解析出的每条子命令判定：整条调用的全部指纹都在
            # 白名单/已记忆集合才放行（部分命中仍需人审，fail-closed）。
            signatures = self._make_signatures(tool_name, args)
            if signatures and all(signature in _PERMANENT_WHITELIST for signature in signatures):
                logger.info(
                    "[%s] 危险工具命中永久免审批白名单，放行 | tool=%s | cmds=%d",
                    self.name,
                    tool_name,
                    len(signatures),
                )
                continue

            # 命中本管道内已记忆的命令指纹 → 放行（用户此前选了"同命令免批"）
            approved_signatures = self._approved_signatures_from_state(ctx)
            if signatures and all(signature in approved_signatures for signature in signatures):
                logger.info(
                    "[%s] 危险工具命中本管道已记忆指纹，放行 | tool=%s | cmds=%d",
                    self.name,
                    tool_name,
                    len(signatures),
                )
                continue

            # S2/D7 裁定（2026-09-24，用户授权自定）：降级态保守审批——规则库
            # 失明时无法确认参数安全，回落各档位自身语义（危险操作交人审）。
            # 这不是新发明：_dispatch_by_permission_mode 的 docstring 自述
            # 「规则缺失→兜底弹审批（安全优先）」，本分支是该自述的落地。
            # allow 白名单与已记忆指纹先于本判定放行（降级不吞既有豁免）；
            # bypass 显式档不进本方法（用户主权保持）；降级横幅
            # （security_rules_degraded 事件）向用户解释弹卡原因。
            if self._rules_degraded:
                action = "needs_approval"
                rule_name = "rules_degraded_conservative"

            mode_decision = await self._dispatch_by_permission_mode(
                ctx,
                tool_name=tool_name,
                action=action,
                rule_name=rule_name,
                signatures=signatures,
                batch_signatures=batch_signatures,
                mode=mode,
                action_key=action_key,
                grants_status=grants_status,
                grants_verdict=grants_verdict,
            )
            if isinstance(mode_decision, dict):
                return mode_decision
            # "pass"：本工具放行，继续下一工具
        return None

    # ── grants 账本执法面（P1 接线，ADR 2026-11-19-grants-ledger-and-tool-classification）──
    # 语义基线（D1/D3 + 执行方案 §2 + 用户裁定 2026-11-19）：分级裁决在规则匹配前
    # 查表，但规则面优先——verdict 只作用于「规则未覆盖」分支，永不豁免/改写规则
    # 裁决（block/final_approval 恒不可豁免）；grants 面缺席/异常一律 inert 放行
    # （既有审批语义零变化，等价于 grants 面不存在），可见性由一次性降级横幅承担
    # （grants_degraded，键=pipeline_id 每管道一次）；auto 类零账本依赖照常。

    #: 多动作工具的动作参数名（与 _OP_ARGS 同源口径；file_write/memory 等均用 action）。
    _GRANTS_ACTION_PARAM_NAMES: tuple[str, ...] = ("action", "operation", "op")
    #: 旁路档仍逐次人审的高风险 effect 类（D3：免审批摩擦不免高风险类）。
    _GRANTS_HIGH_RISK_EFFECTS: frozenset[str] = frozenset({"device_control", "financial"})

    def _grants_action_key(self, tool_name: str, args: dict[str, Any]) -> str:
        """动作键派生（分级表 ``tool.action`` 查表键的动作位）。

        显式动作参数（action/operation/op，多动作工具 memory/file_write/task_manage
        等）> 命令执行类固定 execute（isolation policy command_in_container，bash）>
        空串（单动作工具按整工具键查表，PolicyEngine 整工具回落同键）。
        """
        for name in self._GRANTS_ACTION_PARAM_NAMES:
            value = args.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            if _policy_loader.resolve(tool_name).execution == "command_in_container":
                return "execute"
        except Exception:  # noqa: BLE001 — 策略解析失败按无动作位查整工具键
            pass
        return ""

    @staticmethod
    async def _grants_classify(
        policy_cap: Any,
        tool_name: str,
        action_key: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """分级裁决：返回 ``(status, verdict)``，status ∈ ok | unavailable | error。

        cap 缺席 = unavailable（执法面 inert 降级）；返回形态异常或调用异常 =
        error（与 unavailable 同构处置：inert 放行 + 一次性降级横幅，既有审批
        语义零变化——用户裁定 2026-11-19：加认证不得改变既有审批逻辑）。
        """
        if policy_cap is None:
            return "unavailable", None
        try:
            verdict = await policy_cap.call("classify", {"tool": tool_name, "action": action_key})
        except Exception as exc:  # noqa: BLE001 — 分级面异常按 error 处置（调用方 inert 放行 + 横幅）
            logger.warning("[security_check] grants.policy.classify 异常: %s", exc)
            return "error", None
        if not isinstance(verdict, dict) or not verdict.get("verdict"):
            return "error", None
        return "ok", verdict

    async def _grants_issue(
        self,
        ctx: PluginContext,
        *,
        tool: str,
        action: str,
        granularity: str,
        subject_kind: str,
        subject_id: str,
        evidence: str | None = None,
        pipeline_id: str | None = None,
    ) -> None:
        """授予面双写：审批卡授予同步落账本（v1 真源 = JSON 文件/state 键，写面不动）。

        调用序保证真源写面先行且已成功；账本失败只留日志不回滚授予（双写过渡期
        账本是投影/查询面，审批主流程不被账本可用性阻断）。
        """
        _policy_cap, ledger_cap = get_grants_caps()
        if ledger_cap is None:
            return
        try:
            result = await ledger_cap.call(
                "request",
                {
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "tool": tool,
                    "action": action,
                    "granularity": granularity,
                    "source": "approval_card",
                    "evidence": evidence,
                    "pipeline_id": pipeline_id,
                },
            )
            if isinstance(result, dict) and result.get("ok"):
                logger.info(
                    "[%s] 授予面双写账本 | subject=%s/%s | tool=%s | action=%s | granularity=%s",
                    self.name,
                    subject_kind,
                    subject_id,
                    tool,
                    action,
                    granularity,
                )
            else:
                logger.warning("[%s] 授予面账本写入被拒: %s", self.name, result)
        except Exception as exc:  # noqa: BLE001 — 双写过渡：账本失败不回滚真源授予
            logger.warning("[%s] 授予面账本写入失败（真源已生效）: %s", self.name, exc)

    async def _grants_consume(self, grant: dict[str, Any], *, tool_name: str, action_key: str) -> None:
        """grant 命中后消费记账（uses+1）。失败留痕不阻断放行——放行已由命中成立。"""
        _policy_cap, ledger_cap = get_grants_caps()
        if ledger_cap is None:
            return
        try:
            result = await ledger_cap.call(
                "consume",
                {
                    "grant_id": str(grant.get("grant_id") or ""),
                    "context": {"tool": tool_name, "action": action_key},
                },
            )
            if not (isinstance(result, dict) and result.get("ok")):
                logger.warning("[%s] grant 消费记账失败: %s", self.name, result)
        except Exception as exc:  # noqa: BLE001 — 记账失败不阻断放行
            logger.warning("[%s] grant 消费记账异常: %s", self.name, exc)

    async def _grants_issue_on_remember(self, ctx: PluginContext, tool_name: str, request_id: str) -> None:
        """approved_remember 授予面双写：账本 task 粒度 grant（subject=本管道）。

        动作位从触发本次审批的调用派生（与执法侧 `_grants_action_key` 同一
        派生面，issue/authorize 两端同键可匹配）。
        """
        action_key = ""
        for tc in ctx.state.get(StateKeys.RAW_TOOL_CALLS, []):
            name, args = _effective_tool_call(tc)
            if name == tool_name:
                action_key = self._grants_action_key(tool_name, args)
                break
        pipeline_id = self._current_pipeline_id(ctx)
        await self._grants_issue(
            ctx,
            tool=tool_name,
            action=action_key,
            granularity="task",
            subject_kind="pipeline",
            subject_id=pipeline_id,
            evidence=request_id,
            pipeline_id=pipeline_id,
        )

    async def _dispatch_grants_verdict(
        self,
        ctx: PluginContext,
        *,
        tool_name: str,
        action_key: str,
        status: str,
        verdict: dict[str, Any] | None,
        signatures: list[str],
        batch_signatures: list[str],
    ) -> str | dict[str, Any]:
        """规则未覆盖的调用按分级裁决分流（grant 只作用此分支，规则面优先）。

        - unavailable（grants 面缺席）/ error（分级或账本面异常、畸形形态）→
          inert 放行 + 一次性降级横幅（既有审批语义零变化——等价于 grants 面
          不存在，黑名单未命中按档放行的老语义；用户裁定 2026-11-19）；
        - auto → 放行 + 审计事件（无卡无账本写，验收①）；
        - deny → 拦截；
        - per_call_approval → 既有审批卡（逐次人审）；
        - task_grant/domain_grant → 账本 authorize：命中消费放行（uses+1，验收②④）；
          miss 弹既有审批卡（五选项零变化）。
        """
        if status == "unavailable":
            return "pass"
        if status == "error" or not isinstance(verdict, dict):
            # error（分级面异常）与畸形入参（ok 恒携 verdict dict）同构处置：
            # 等价 grants 面不存在——inert 放行 + 一次性降级横幅，可见性由
            # 横幅承担，不逐调用弹卡（用户裁定 2026-11-19）。
            await self._notify_grants_degraded_once(ctx)
            return "pass"
        verdict_name = str(verdict.get("verdict") or "")
        policy_key = str(verdict.get("policy_key") or "")
        if verdict_name == "auto":
            audit_event(
                {
                    "event": "grants_auto_allow",
                    "tool": tool_name,
                    "action": action_key,
                    "policy_key": policy_key,
                    "effect": verdict.get("effect"),
                    "pipeline_id": self._current_pipeline_id(ctx),
                    "session_id": str(ctx.state.get(StateKeys.SESSION_ID, "") or ""),
                    "user_id": str(ctx.state.get("user_id", "") or ""),
                }
            )
            return "pass"
        if verdict_name == "deny":
            logger.warning(
                "[%s] 分级 deny 拦截 | tool=%s | key=%s",
                self.name,
                tool_name,
                policy_key,
            )
            return self._soft_block(ctx, tool_name, f"分级策略拒绝执行（deny）: {policy_key or tool_name}")
        if verdict_name == "per_call_approval":
            return await self._await_approval(
                ctx,
                tool_name,
                f"分级策略逐次人审: {policy_key or tool_name}",
                signatures=signatures,
                batch_signatures=batch_signatures,
            )
        # task_grant / domain_grant：查账本（账本面缺席/异常均等价 grants 面不
        # 存在：inert 放行 + 一次性降级横幅，用户裁定 2026-11-19——双写过渡期
        # 账本是投影/查询面，其可用性不阻断审批主流程）
        _policy_cap, ledger_cap = get_grants_caps()
        if ledger_cap is None:
            await self._notify_grants_degraded_once(ctx)
            return "pass"
        try:
            result = await ledger_cap.call(
                "authorize",
                {
                    "subject_kind": "pipeline",
                    "subject_id": self._current_pipeline_id(ctx),
                    "tool": tool_name,
                    "action": action_key,
                },
            )
        except Exception as exc:  # noqa: BLE001 — 账本异常按等价缺席处置（inert 放行 + 横幅）
            logger.warning("[%s] grants.ledger.authorize 异常: %s", self.name, exc)
            await self._notify_grants_degraded_once(ctx)
            return "pass"
        grant = result.get("grant") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("authorized") and isinstance(grant, dict):
            await self._grants_consume(grant, tool_name=tool_name, action_key=action_key)
            logger.info(
                "[%s] grant 命中放行 | tool=%s | action=%s | grant_id=%s | granularity=%s",
                self.name,
                tool_name,
                action_key,
                grant.get("grant_id"),
                grant.get("granularity"),
            )
            return "pass"
        return await self._await_approval(
            ctx,
            tool_name,
            "账本无有效 grant（分级 " + (policy_key or verdict_name) + "）",
            signatures=signatures,
            batch_signatures=batch_signatures,
        )

    async def _enforce_grants_high_risk_bypass(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """旁路档高风险类闸（ADR D3：免审批摩擦不免高风险 grant，沿位置闸先例）。

        逐调用查分级：``per_call_approval`` ∧ effect ∈ {device_control, financial}
        → 逐次人审卡（验收⑦）；deny → 恒拒（分级禁用不属审批摩擦）；其余 verdict
        照常放行（旁路档免审批摩擦语义零变化）。grants 分级面缺席/异常一律
        inert 放行 + 一次性降级横幅（既有旁路语义零变化——旁路档静默放行是既有
        路径，异常弹卡会让每个隔离管道都卡住；用户裁定 2026-11-19）。

        Returns:
            需要处置（卡/拦截）时的决策字典；全部放行返回 None。
        """
        policy_cap, _ledger_cap = get_grants_caps()
        if policy_cap is None:
            return None
        updates: dict[str, Any] = {}
        for tc in tool_calls:
            tool_name, args = _effective_tool_call(tc)
            action_key = self._grants_action_key(tool_name, args)
            status, verdict = await self._grants_classify(policy_cap, tool_name, action_key)
            if status == "error" or not isinstance(verdict, dict):
                # 分级面异常/畸形形态 → 等价 grants 面不存在：inert 放行 +
                # 一次性降级横幅（不逐调用弹卡，用户裁定 2026-11-19）。
                await self._notify_grants_degraded_once(ctx)
                continue
            verdict_name = str(verdict.get("verdict") or "")
            effect = str(verdict.get("effect") or "")
            policy_key = str(verdict.get("policy_key") or "")
            decision: dict[str, Any] | None = None
            if verdict_name == "deny":
                logger.warning(
                    "[%s] 旁路档分级 deny 拦截 | tool=%s | key=%s",
                    self.name,
                    tool_name,
                    policy_key,
                )
                decision = self._soft_block(
                    ctx, tool_name, f"分级策略拒绝执行（deny）: {policy_key or tool_name}"
                )
            elif verdict_name == "per_call_approval" and effect in self._GRANTS_HIGH_RISK_EFFECTS:
                decision = await self._await_approval(
                    ctx,
                    tool_name,
                    f"高风险操作逐次人审（旁路档不免）: {policy_key or tool_name}",
                    signatures=self._make_signatures(tool_name, args),
                )
            if decision is not None:
                if decision.get("pre_decided_results"):
                    return {**decision, **updates}
                updates.update(decision)
        return updates or None

    async def _dispatch_by_permission_mode(
        self,
        ctx: PluginContext,
        *,
        tool_name: str,
        action: str,
        rule_name: str | None,
        signatures: list[str],
        batch_signatures: list[str],
        mode: str,
        action_key: str,
        grants_status: str,
        grants_verdict: dict[str, Any] | None,
    ) -> str | dict[str, Any]:
        """按权限模式处置未命中白名单/记忆指纹的危险工具。

        模式决定"未命中规则"的档位：default=确认、accept_edits=文件放行、
        auto=尽量自动。旁路档不进入本方法（_do_work 单点判定整体放行）。
        default / auto(ask) / accept_edits(命令类) 且**未命中任何规则**
        → grants 分级分流（P1 接线：规则面优先，verdict 只作用规则未覆盖分支）。
        命中 needs_approval/block 规则的参数（rm -rf 等关键词）
        已在 _match_rules 返回对应 action，只有 action 为空（未命中）才走到放行分支；
        规则缺失/为空（安全闸门失效风险）→ 兜底弹审批（安全优先）。

        Args:
            ctx: 插件执行上下文
            tool_name: 工具名
            action: 规则匹配动作（allow/needs_approval/block 或空）
            rule_name: 命中的规则名（未命中为空）
            signatures: 当前调用的命令指纹集合（复合命令按子命令拆分）
            batch_signatures: 本批卡面覆盖的全部命令指纹（授予面）
            mode: ``_do_work`` 解析好的生效权限档
            action_key: grants 分级查表动作位（``_grants_action_key`` 派生）
            grants_status: 分级裁决状态（ok/unavailable/error）
            grants_verdict: 分级裁决结果 dict（status=ok 时非 None）

        Returns:
            "pass"（放行）；或处置完成的决策字典（auto 自动拒绝 / 弹审批）。
        """
        if mode == "accept_edits" and tool_name in _FILE_TOOLS:
            logger.info(
                "[%s] accept_edits 模式文件类放行 | tool=%s",
                self.name,
                tool_name,
            )
            return "pass"

        if mode == "auto" and action == "block":
            logger.info(
                "[%s] auto 模式自动拒绝（block 规则）| tool=%s | rule=%s",
                self.name,
                tool_name,
                rule_name,
            )
            return self._soft_block(ctx, tool_name, f"安全规则自动拒绝: {rule_name or 'block'}")

        if not rule_name:
            grants_decision = await self._dispatch_grants_verdict(
                ctx,
                tool_name=tool_name,
                action_key=action_key,
                status=grants_status,
                verdict=grants_verdict,
                signatures=signatures,
                batch_signatures=batch_signatures,
            )
            if isinstance(grants_decision, dict):
                return grants_decision
            return "pass"
        reason = "参数命中安全规则: " + rule_name
        logger.warning(
            "[%s] 危险工具参数命中规则，弹审批 | tool=%s | rule=%s",
            self.name,
            tool_name,
            rule_name,
        )
        return await self._await_approval(
            ctx,
            tool_name,
            reason,
            signatures=signatures,
            batch_signatures=batch_signatures,
        )

    def _explicit_permission_mode(self, ctx: PluginContext) -> str | None:
        """解析用户在前端显式选择的权限档；未显式选择返回 None。

        判据 = 权限模式表（_PERMISSION_MODES，键 session_id）存在该会话条目：
        表只由 http.handle 切换端点写入（前端选择器点选，含显式选 default），
        缺省态不写表，故「在场即显式」。隔离任务的免审批默认仅在本方法返回
        None 时生效；显式选择任一档（含 default）都覆盖免审批默认。

        key 为 session_id（会话稳定键）：同一会话内主/子管道、任务轮换的
        pipeline_id 各不相同，会话级设置键在 pipeline_id 上会因键位漂移失配
        （BUG-15：显式选择被隔离豁免路径吞掉）。
        """
        session_id = str(ctx.state.get(StateKeys.SESSION_ID, "") or "")
        return _PERMISSION_MODES.get(session_id)

    def _state_file_path(self, filename: str) -> str:
        """实例侧持久化落点（委托模块级单键派生，读写同源）。"""
        return _state_file_path(filename)

    def _resolve_permission_mode(self, ctx: PluginContext, *, isolated: bool = False) -> str:
        """解析当前调度的生效权限档（单点解析，OBS-R255-1）。

        优先级：管道级覆盖档（本管道键或血缘链命中）> 用户显式选择
        （http.handle 写入的 _PERMISSION_MODES，键 session_id）> 隔离免审批
        默认（isolated=True 且未显式选择 → 旁路档承载，隔离容器即安全边界）>
        插件配置（pipeline yaml security_check config 的 mode 字段）> 默认
        "default"。规则降级态压制管道覆盖档（终审不可匹配 → 保守回弹卡）。
        """
        if not self._rules_degraded:
            override = self._pipeline_override_mode(ctx)
            if override:
                return override
        explicit = self._explicit_permission_mode(ctx)
        if explicit:
            return explicit
        if isolated:
            return "bypass"
        return self._config.get("mode", "default")

    async def _await_approval(
        self,
        ctx: PluginContext,
        tool_name: str,
        rule_name: str,
        *,
        signatures: list[str] | None = None,
        batch_signatures: list[str] | None = None,
        final_approval: bool = False,
    ) -> dict[str, Any]:
        """等待用户审批并返回审批结果。

        通过 human_interaction 服务创建审批请求并 await 等待用户响应。
        优先用 engine 注入的服务（ctx.get_service），取不到时回退到
        全局单例 get_human_interaction_service()（覆盖 websocket 等未注入
        服务的场景）。审批通过后写入 allowed=True；
        拒绝/超时/取消走 _soft_block（软拦截，反馈给 LLM）。

        五个审批选项：
        - approved_once   : 仅本次执行通过（不记忆指纹）
        - approved_remember: 本管道内同命令（同工具+同指纹）后续免审批；
                            授予面 = batch_signatures（卡上全部命令，复合命令按
                            解析出的每条子命令各记一条），无指纹命令退化为"仅本次"
        - approved_pipeline_scope: 本管道（含派发子树）权限档切旁路（先警告确认）
        - approved_permanent_whitelist: 命令精确指纹入永久白名单（跨管道跨会话，
                            授予面同 batch_signatures）
        - denied          : 拒绝（软拦截）

        Args:
            ctx: 插件执行上下文
            tool_name: 触发审批的工具名称
            rule_name: 触发审批的规则/原因描述
            signatures: 当前调用的命令指纹集合（复合命令按子命令拆分）
            batch_signatures: 本批卡面覆盖的全部命令指纹（授予面；缺省退回 signatures）

        Returns:
            安全决策结果字典
        """
        # 经 human-interaction capability 调交互工具插件（统一交互入口）。
        # server.py 的 on_load 把 plugin 引用注入 _PLUGIN_REF，据此拿 capability。
        hi_cap = _get_human_interaction_cap()
        if hi_cap is None:
            logger.warning("[%s] human-interaction capability not injected; soft-block", self.name)
            # 审批闸底座缺失显式落条目 metadata（软拦截可观测，不再无痕）
            return self._soft_block(ctx, tool_name, "交互服务不可用，已拦截", approval_channel_missing=True)

        # 提取工具调用的具体参数，显示给用户审批
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        args_preview = self._format_args_for_approval(tool_calls, tool_name)
        if final_approval:
            args_preview += "\n\n终审操作：管道免审批不适用，须逐次确认。"

        # 授予面：卡上展示的全部命令（含复合命令解析段）；缺省退回当前调用。
        grant_signatures = list(batch_signatures) if batch_signatures is not None else list(signatures or [])
        if not grant_signatures:
            grant_signatures = list(signatures or [])

        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        # 拒绝记忆作用域（ADR 2026-10-01 决策 6 精确化）：工具+指纹精确键——
        # 同命令拒绝压制重试，换命令不牵连；指纹不可算时退化为工具键（与旧
        # 标题键同粒度）。复合命令用整批指纹拼接键，避免首段相同即误压制。
        memory_key = "|".join(grant_signatures) if grant_signatures else tool_name
        # request_id 预初始化：create_choice_request 抛异常时尚未赋值，
        # 但 except 链日志需引用它，占位为 "-" 表示请求未创建成功。
        request_id = "-"
        # try 覆盖创建请求+等待响应，避免服务异常上抛中断管道。
        try:
            create_res = await hi_cap.call(
                "create_choice",
                {
                    "session_id": session_id,
                    "thread_id": session_id,
                    "tab_id": "",
                    "title": f"终审审批: {tool_name}" if final_approval else f"安全审批: {tool_name}",
                    "description": args_preview,
                    "options": list(_APPROVAL_OPTIONS),
                    "priority": "high",
                    "memory_key": memory_key,
                    # 审批非阻塞 M1：category 由发起方显式传（设计 §四——
                    # 不由后端按标题猜测），安全审批恒 permission。
                    "category": "permission",
                    # 归属归因（M2）：记录创建者用户，审批面归属校验据此放行
                    # 创建者本人（一用户一租户：user_id 即归属租户）。
                    "user_id": ctx.state.get("user_id", ""),
                    # 卡片来源四元组（管道 state 权威值）：安全审批卡与授权卡
                    # 同族，必须可见发起 agent（名/层级）/会话/管道，缺项省略。
                    "agent_id": str(ctx.state.get("agent.id", "") or ""),
                    "agent_level": str(ctx.state.get(StateKeys.AGENT_LEVEL, "") or ""),
                    "pipeline_id": str(ctx.state.get("pipeline_id", "") or ""),
                    "agent_name": str(ctx.state.get("context.agent_name", "") or ""),
                },
            )
            if not isinstance(create_res, dict) or create_res.get("error"):
                raise RuntimeError(f"create_choice failed: {create_res}")
            request_id = create_res.get("request_id", "-")

            logger.info(
                "[%s] Approval request created | request_id=%s | tool=%s | rule=%s",
                self.name,
                request_id,
                tool_name,
                rule_name,
            )

            wait_res = await hi_cap.call(
                "wait_for_choice",
                {
                    "request_id": request_id,
                    "timeout": 86400,
                },
                # 等待用户审批是长等待语义：默认 30s 会先于用户点击掐断。
                # 业务超时 86400 由 human 服务 enforce；本参数仅作 SDK
                # 侧提示（内核不读 meta.timeout）——内核按 human 插件声明的
                # mcp.request_timeout_secs=86400（BUG-60 审批族统一值）等待响应。
                timeout=86500.0,
            )
            # capability 返回 error dict 时按 error_code 分类（ADR 2026-10-01
            # 决策 8，词源 SDK InteractionErrorCode；human 侧 interaction.wait_for_choice
            # 产出），不再对 error 消息做子串嗅探。
            if not isinstance(wait_res, dict):
                raise RuntimeError(f"wait_for_choice returned non-dict: {wait_res}")
            if wait_res.get("error"):
                err_msg = str(wait_res["error"])
                code = wait_res.get("error_code", "")
                if code == InteractionErrorCode.INTERACTION_DENIED.value:
                    raise InteractionDeniedError(request_id, err_msg)
                if code == InteractionErrorCode.INTERACTION_CANCELLED.value:
                    raise InteractionCancelledError(request_id, err_msg)
                if code == InteractionErrorCode.INTERACTION_TIMEOUT.value:
                    raise InteractionTimeoutError(request_id, 86400)
                raise RuntimeError(err_msg)
            result = wait_res
            response_type = result.get("response_type", "")
            # response_type 表示响应类型（answered/denied/cancelled），
            # 用户的具体选择在 selected_option / selected_semantics 字段中。
            # DENIED 和 CANCELLED 已由 wait_for_choice 抛异常处理，
            # 到这里 response_type 通常是 "answered"。
            #
            # 消费端只读语义（决策 2）：human 归一点已把任何词形（id/label/
            # 历史别名 approve/approved）收敛为规范 id + 封闭语义；本插件不再
            # 自持 label/别名反查表。语义缺空（无契约卡的透传）按拒绝 fail-closed。
            semantics = str(result.get("selected_semantics", "") or "")

            if semantics == OptionSemantics.APPROVE_PIPELINE_SCOPE.value:
                # 「本管道后续免审批」= 管道级权限档切换（含派发子树）：先弹
                # 警告确认（cancel/超时/服务不可用一律不切档），本次调用放行
                # （语义含"本次+后续"；确认失败退化为"仅本次"）。
                await self._grant_pipeline_scope(ctx, hi_cap, tool_name, rule_name)
                return {}

            if semantics == OptionSemantics.APPROVE_PERMANENT_WHITELIST.value:
                # 永久白名单：卡上全部命令的精确指纹逐条落名单文件（跨管道
                # 跨会话）；无指纹命令退化为"仅本次"（同 approved_remember 退化）。
                await self._grant_permanent_whitelist(ctx, grant_signatures, tool_name)
                return {}

            if semantics in (OptionSemantics.APPROVE_ONCE.value, OptionSemantics.APPROVE_AND_REMEMBER.value):
                # "同命令免批"：记忆精确指纹，本管道内同工具+同命令后续免审批。
                # 授予面 = 卡上全部命令（复合命令按解析段逐条记忆）；无指纹
                # （如无法归一化的参数）退化为"仅本次"。
                if semantics == OptionSemantics.APPROVE_AND_REMEMBER.value and grant_signatures:
                    updates = self._remember_signatures(ctx, grant_signatures)
                    # 授予面双写（P1）：真源 state 键已落，账本补 task 粒度
                    # grant（subject=本管道，tool+action 类级）。
                    await self._grants_issue_on_remember(ctx, tool_name, request_id)
                    logger.info(
                        "[%s] Approval granted (remember signatures) | request_id=%s | tool=%s | count=%d",
                        self.name,
                        request_id,
                        tool_name,
                        len(grant_signatures),
                    )
                    return updates
                logger.info(
                    "[%s] Approval granted (once) | request_id=%s | tool=%s | option=%s",
                    self.name,
                    request_id,
                    tool_name,
                    result.get("selected_option", ""),
                )
                return {}

            logger.warning(
                "[%s] Approval denied | request_id=%s | tool=%s | response=%s | selected=%s | semantics=%s",
                self.name,
                request_id,
                tool_name,
                response_type,
                result.get("selected_option", ""),
                semantics,
            )
            # 审批拒绝属于软拦截——用户主观选择拒绝，不应结束管道，
            # 而是把拒绝结果作为 tool_result 返回给 LLM，让 LLM 决定下一步。
            # 自由文本（审批卡下方输入框）随 feedback 回传：用户写的说明/改法
            # 必须进入工具返回值（此前被丢弃，模型只看到笼统"拒绝"）。
            feedback = str(result.get("feedback") or "").strip()
            reason = f"用户拒绝执行: {rule_name}"
            if feedback:
                reason += f"；用户反馈: {feedback[:2000]}"
            return self._soft_block(ctx, tool_name, reason)

        except InteractionDeniedError as e:
            logger.warning(
                "[%s] Approval denied (exception) | request_id=%s | tool=%s | reason=%s",
                self.name,
                request_id,
                tool_name,
                e.reason,
            )
            return self._soft_block(ctx, tool_name, f"用户拒绝执行: {e.reason or rule_name}")

        except InteractionTimeoutError:
            logger.warning(
                "[%s] Approval timed out | request_id=%s | tool=%s",
                self.name,
                request_id,
                tool_name,
            )
            return self._soft_block(ctx, tool_name, f"审批超时未响应: {rule_name}")

        except InteractionCancelledError as e:
            logger.warning(
                "[%s] Approval cancelled | request_id=%s | tool=%s | reason=%s",
                self.name,
                request_id,
                tool_name,
                e.reason,
            )
            return self._soft_block(ctx, tool_name, f"审批被取消: {e.reason or rule_name}")

        except Exception as e:
            logger.error(
                "[%s] Approval error | request_id=%s | tool=%s | error=%s",
                self.name,
                request_id,
                tool_name,
                e,
            )
            # 审批通道故障（BUG-41）：人审结果未知（用户可能已批准但响应在
            # 通道翻动中丢失）、调用从未执行 → 标记可重试，duplicate_check
            # 据此豁免该次发射的签名入窗，人审后的重试不被去重拦截。
            return self._soft_block(ctx, tool_name, f"审批服务异常: {e}", retry_allowed=True)

    def _make_signature(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """计算命令指纹，用于"本管道内同命令免批"记忆。

        指纹 = 工具名 + 关键参数（命令/路径/内容/代码）的归一化字符串的 sha256 短摘要。

        安全边界（关键）：
        - 精确匹配，不做前缀/语义模糊。命令仅做空白归一化（统一连续空白为单空格、
          去首尾），不替换路径、不截断、不解析重排。保证 "rm -rf /tmp/x" ≠ "rm -rf /"。
        - 命令前后差异（哪怕只多一个空格）也会产生不同指纹 → 用户重审，宁可多问。
        - 无任何关键参数可归一化时返回 None，调用方退化为"仅本次"。

        Args:
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            形如 "tool_name:ab12cd34" 的指纹；无法计算返回 None
        """
        import hashlib  # noqa: PLC0415

        # 提取关键参数：与 _format_args_for_approval 一致的参数集合
        parts: list[str] = [tool_name]

        cmd = args.get("command") or args.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            # 仅空白归一化：连续空白→单空格，去首尾。不改命令语义。
            parts.append("command=" + " ".join(cmd.split()))

        code = args.get("code")
        if isinstance(code, str) and code.strip():
            parts.append("code=" + " ".join(code.split()))

        content = args.get("content")
        if isinstance(content, str) and content.strip():
            parts.append("content=" + " ".join(content.split()))

        for path_key in ("path", "file_path", "directory", "dest", "target", "output_path", "working_dir"):
            val = args.get(path_key)
            if isinstance(val, str) and val.strip():
                parts.append(f"{path_key}={val.strip()}")

        # 批量参数（delete_file 的 paths 数组）：归一化后计入指纹，顺序无关。
        paths = args.get("paths")
        if isinstance(paths, (list, tuple)):
            normalized = sorted(p.strip() for p in paths if isinstance(p, str) and p.strip())
            if normalized:
                parts.append("paths=" + "|".join(normalized))

        # 只有工具名、无任何关键参数 → 无法稳定记忆，返回 None
        if len(parts) == 1:
            return None

        digest_source = "|".join(parts)
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
        return f"{tool_name}:{digest}"

    def _make_signatures(self, tool_name: str, args: dict[str, Any]) -> list[str]:
        """命令指纹集合：复合命令按解析出的每条子命令各算一个指纹。

        命令类参数（command/cmd）先经 ``split_command_segments`` 解析（引号/
        转义感知切 ``&&``/``||``/``;``/``|``/``&``/换行），逐段复用
        ``_make_signature`` 的精确指纹算法；白名单/同命令免批按段授权，
        用户卡上看到的每条命令都可单独复用（而非整串复合命令一个指纹）。
        无命令参数（路径/内容/代码类）或解析为空 → 退回单指纹。
        """
        command = args.get("command") or args.get("cmd")
        if isinstance(command, str) and command.strip():
            key = (
                "command"
                if isinstance(args.get("command"), str) and str(args.get("command")).strip()
                else "cmd"
            )
            signatures: list[str] = []
            for segment in split_command_segments(command):
                signature = self._make_signature(tool_name, {**args, key: segment})
                if signature and signature not in signatures:
                    signatures.append(signature)
            if signatures:
                return signatures
        signature = self._make_signature(tool_name, args)
        return [signature] if signature else []

    def _approval_batch_signatures(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
        *,
        isolated: bool,
    ) -> list[str]:
        """本批审批卡覆盖的全部命令指纹（逐调用解析子命令，去重保序）。

        授权粒度 = 逐条精确指纹（含完整参数）；卡面「解析出的免审批命令」列表
        是展示粒度（按可执行文件名归并），故卡面条目数 ≤ 授权面指纹数——如
        `rm -rf /tmp/a` + `rm -rf /tmp/b` → 卡面 1 条 / 授权面 2 枚指纹。归并键
        不进本方法输入输出。非隔离批次按危险工具门槛过滤（安全工具不进授权
        面）；隔离批次全量收（规则轨道对全部调用生效）。
        """
        signatures: list[str] = []
        for tc in tool_calls:
            # 卡面指纹按展开后的目标工具算：用户看到的命令就是被授权的命令。
            tool_name, args = _effective_tool_call(tc)
            if not isolated and not self._is_dangerous_tool(ctx, tool_name, args):
                continue
            for signature in self._make_signatures(tool_name, args):
                if signature not in signatures:
                    signatures.append(signature)
        return signatures

    def _soft_block(
        self,
        ctx: PluginContext,
        tool_name: str,
        reason: str,
        *,
        retry_allowed: bool = False,
        approval_channel_missing: bool = False,
    ) -> dict[str, Any]:
        """软拦截：被拒调用直出预定结果（ADR 2026-09-28 结果预填）。

        只预填命中的调用（调用级粒度，用户裁定 2026-09-28：无辜调用照常
        执行，旧整批"关联拒绝"随自建消息退役）；配对消息/事件由 tool_core
        命中后统一整形——不再自建 role=tool 消息、不清空调用、不写
        tool_results。

        连续拒绝保护：按工具签名计数，同一请求被连续拦截超阈值时，说明
        模型无法自我纠正，直接终止管道并上报（熔断路径保持整批语义：清空
        调用跳过本轮执行，终态文本不被 tool_core 覆写）。

        retry_allowed（BUG-41）：审批通道故障（wait 链路异常，人审结果未知）
        时为 True——调用从未执行且用户可能已批准，条目带 retry_allowed +
        arguments 扩展位，经 tool_core 随 tool_results 透传，duplicate_check
        据此把该次未执行的签名从重复窗口摘除（人审后的重试不得被去重拦截）
        并不计失败熔断连败。用户拒绝/超时/取消不是通道故障，不带标记
        （拒绝重试闸保留）。

        Args:
            ctx: 插件执行上下文
            tool_name: 被拒绝的工具名称
            reason: 拒绝原因
            retry_allowed: 审批通道故障标记（该次调用未执行、可重试）
            approval_channel_missing: 审批底座缺失标记（条目 metadata 可观测）

        Returns:
            状态更新字典（pre_decided_results 合并写入；熔断时追加终态键）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        metadata: dict[str, Any] = {"decided_by": "security_check"}
        if approval_channel_missing:
            metadata["approval_channel_missing"] = True
        entries: list[dict[str, Any]] = []
        for tc in tool_calls:
            # 按展开名匹配：动态转发壳被拦时条目名是 call_tool，判定名是目标工具
            if _effective_tool_call(tc)[0] != tool_name:
                continue
            extra: dict[str, Any] = {}
            if retry_allowed:
                # 参数原样透传（dict/str JSON 均可），duplicate_check 侧归一定签名
                extra["retry_allowed"] = True
                extra["arguments"] = tc.get("arguments", tc.get("args", {}))
            # 条目名必须是**原调用名**（call_tool），不是展开后的目标名：tool_core 的
            # match_entries 先按 call_id 匹配、**无 id 时按 entry.tool_name == tc.name
            # 兜底**——写目标名会让兜底失配，预填拒绝不生效（动态调用照常执行）。
            # 目标名经 metadata.target_tool 下行，供 duplicate_check 落动态桶。
            call_name = str(tc.get("name", "") or "")
            entry_meta = dict(metadata)
            if tool_name != call_name:
                entry_meta["target_tool"] = tool_name
            entries.append(
                tool_result_entry(
                    call_name or tool_name,
                    call_id=tc.get("id"),
                    success=False,
                    error=f"[审批拒绝] {reason}",
                    metadata=entry_meta,
                    **extra,
                )
            )

        updates: dict[str, Any] = {
            "pre_decided_results": merge_pre_decided(ctx.state.get("pre_decided_results"), entries),
        }

        # 连续拒绝计数：同一工具签名被反复拦截时累加，换请求即重置。
        signature = self._make_signature(tool_name, self._first_args(tool_calls, tool_name))
        if signature:
            count = self._rejected_signatures.get(signature, 0) + 1
            self._rejected_signatures[signature] = count
            if count >= self._reject_threshold:
                # 同一请求连续被拦超阈值：模型已陷入死循环、无法自我纠正。
                # 明确终止管道并上报，而非无限重试。这是"工具重复上限"的最终防线：
                # 拒绝反馈已能到达模型（tool_core 统一整形配对消息）；到这一步说明
                # 模型无视反馈反复提交同一被拦请求，继续重试只会空耗资源。
                fatal_reason = (
                    f"工具 {tool_name} 连续被安全检查拦截 {count} 次"
                    f"（阈值 {self._reject_threshold}），疑似死循环，终止任务"
                )
                logger.warning(
                    "[%s] %s | sig=%s",
                    self.name,
                    fatal_reason,
                    signature,
                )
                updates[StateKeys.RAW_RESULT] = fatal_reason
                updates[StateKeys.RAW_ERROR] = fatal_reason
                updates[StateKeys.ENDED] = True
                # 终态署名（控制状态键契约 ADR 2026-08-30：谁写终止谁署名）：
                # 内核 RunStatus::from_control_state 按 FAILED_STOP_REASONS 词表
                # 映射终态，未署名的 ended 折算 completed——熔断 run 必须署名
                # tool_fail_loop 落 failed（事故 2026-09-28：熔断 run 落
                # completed，与错误信息"终止任务"矛盾，观感即"任务没中止"）。
                updates["router.stop_reason"] = "tool_fail_loop"
                # 熔断即终局：本轮不再执行（终态文本不被 tool_core 覆写）。
                updates[StateKeys.RAW_TOOL_CALLS] = []
                # 重置该签名计数：管道即将结束，避免残留
                self._rejected_signatures[signature] = 0

        logger.info(
            "[%s] Soft-block: 拒绝预定结果待 tool_core 整形反馈 LLM | tool=%s",
            self.name,
            tool_name,
        )
        return updates

    @staticmethod
    def _first_args(
        tool_calls: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any]:
        """取出触发拒绝的工具调用参数，用于计算连续拒绝指纹。

        Args:
            tool_calls: 当前轮次的工具调用列表
            tool_name: 触发拒绝的工具名称

        Returns:
            触发工具的 args 字典；找不到时返回空字典
        """
        for tc in tool_calls:
            call_name = str(tc.get("name", "") or "")
            judged_name, judged_args = _effective_tool_call(tc)
            if judged_name != tool_name:
                continue
            if call_name == DYNAMIC_FORWARD_TOOL and judged_name != call_name:
                # 转发壳条目必须用**展开后的目标参数**：外层参数是
                # {"tool":..., "arguments":{...}}，_make_signature 只看顶层
                # command/code/content/path* → 取外层恒得 None、拒绝计数永不
                # 累计（同一被拦动态危险调用可无限重试）。
                return judged_args if isinstance(judged_args, dict) else {}
            # 非转发壳调用保持原语义（字符串 args 不解析 → 无指纹）
            raw_args = tc.get("args", tc.get("arguments", {}))
            if isinstance(raw_args, str):
                return {}
            return raw_args if isinstance(raw_args, dict) else {}
        return {}

    @staticmethod
    def _format_args_for_approval(tool_calls: list[dict[str, Any]], triggered_tool: str) -> str:  # noqa: ARG004,PLR0912
        """格式化工具调用参数，生成审批描述（含具体命令/路径/内容）。

        Args:
            tool_calls: 当前轮次的工具调用列表
            triggered_tool: 触发审批的工具名

        Returns:
            格式化的审批描述文本，包含工具名 + 具体操作内容预览
        """
        lines = ["请审批以下工具执行请求：", ""]
        parsed_segments: list[str] = []

        for tc in tool_calls:
            call_name = str(tc.get("name", "") or "")
            # 卡面必须展示**被判定的真实操作**：call_tool 条目若按外层参数渲染，
            # args.get("command") 恒空 → 用户对一条危险命令点「批准」却看不到命令
            # 内容（人审同意面失真；叠加「永久白名单」按命令指纹授权的后果更重）。
            name, args = _effective_tool_call(tc)
            lines.append(f"工具: {name}")
            if name != call_name:
                lines.append(f"（经 {call_name} 动态转发；审批与授权按目标工具 {name} 展开）")
            if not isinstance(args, dict):
                args = _coerce_tool_args(tc)

            # 提取关键参数：命令、路径、代码等
            # 命令类（bash_execute 等）
            cmd = args.get("command") or args.get("cmd")
            if cmd:
                # 限制长度，避免超长命令刷屏
                cmd_preview = str(cmd)[:500]
                if len(str(cmd)) > 500:
                    cmd_preview += "\n... (命令过长，已截断)"
                lines.append(f"命令:\n{cmd_preview}")
                # 命令免审批按解析出的每条子命令授权（指纹逐段精确）；卡面列表按命令级归并（同命令只列一条，条数与参数摘要行内标注）。
                parsed_segments.extend(split_command_segments(str(cmd)))

            # 路径类（file_write/read/delete 等）
            for path_key in ("path", "file_path", "directory", "dest", "target", "output_path", "working_dir"):
                val = args.get(path_key)
                if val:
                    lines.append(f"{path_key}: {val}")

            # 文件写入内容
            content = args.get("content")
            if content:
                content_preview = str(content)[:300]
                if len(str(content)) > 300:
                    content_preview += "\n... (内容过长，已截断)"
                lines.append(f"内容预览:\n{content_preview}")

            # 代码类
            code = args.get("code")
            if code:
                code_preview = str(code)[:300]
                if len(str(code)) > 300:
                    code_preview += "\n... (代码过长，已截断)"
                lines.append(f"代码预览:\n{code_preview}")

            # URL 类
            url = args.get("url")
            if url:
                lines.append(f"URL: {url}")

            lines.append("")

        # 本批全部调用的命令按命令级归并展示（同命令不同参数只列一条）。
        if len(parsed_segments) > 1:
            lines.extend(format_parsed_command_list(parsed_segments))

        return "\n".join(lines).strip()

    def _tool_matches_rule(self, tool_name: str, rule_tools: list[str]) -> bool:
        """判断工具名是否匹配规则声明的 tools 列表。

        规则的 tools 支持 ["*"] 通配（匹配所有工具）。
        当 _fuzzy_tool_matching 开启时，对工具名做容错匹配：
        大小写不敏感 + 下划线/连字符等价 + Levenshtein 距离 <= 1，
        防 LLM 输出 bash-execute vs bash_execute vs BashExecute 漂移
        导致规则被绕过。

        Args:
            tool_name: 实际工具名
            rule_tools: 规则声明的工具名列表

        Returns:
            是否匹配
        """
        if "*" in rule_tools:
            return True
        if tool_name in rule_tools:
            return True
        if not self._fuzzy_tool_matching:
            return False
        return any(self._fuzzy_tool_eq(tool_name, t) for t in rule_tools)

    @staticmethod
    def _fuzzy_tool_eq(a: str, b: str) -> bool:
        """工具名容错匹配。

        规范化：转小写 + 把连字符转成下划线（bash-execute ≡ bash_execute ≡ BashExecute）。
        再叠加 Levenshtein 距离 <= 1 容忍单字符差异（拼写错误）。

        Args:
            a: 工具名 A
            b: 工具名 B

        Returns:
            视容错规则是否等价
        """
        na = a.lower().replace("-", "_")
        nb = b.lower().replace("-", "_")
        if na == nb:
            return True
        # Levenshtein 距离 <= 1（单字符增/删/改）
        return SecurityCheckPlugin._levenshtein(na, nb) <= 1

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """计算两字符串的 Levenshtein 编辑距离。

        Args:
            a: 字符串 A
            b: 字符串 B

        Returns:
            编辑距离（使两字符串相同所需的最少单字符编辑次数）
        """
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i]
            for j, cb in enumerate(b, 1):
                cost = 0 if ca == cb else 1
                curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
            prev = curr
        return prev[-1]

    def _match_rules(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        """通用规则匹配引擎。

        遍历所有配置规则，对工具调用参数进行匹配。
        规则的 tools 为 ["*"] 或包含当前工具名时适用。
        支持关键词子串匹配（大小写不敏感）和正则匹配两种模式。

        优先级裁决（按规则 priority 字段，缺省 0，同优先级取先声明者）：
        allow 与 block/needs_approval 并存时，仅当 allow 的优先级 ≥ 拦截的
        优先级才放行。平级 allow 生效 = 白名单语义（豁免 dangerous_commands
        关键词子串噪音，如 `wc -l ... 2>/dev/null` 被 2>/dev/null 关键词误判）；
        高优先级拦截不被白名单掩蔽 = 位置/类别闸语义（host_path_access 以
        priority: 1 压过 safe_commands，`ls D:/secret` 照常弹审批）。
        无 allow 命中时，返回优先级最高的 block/needs_approval 规则。

        规则可声明 ``workspace_boundary_exempt: true``（host_path_access 立法
        本意是拦安装目录/系统目录等真宿主路径；用户根 workspaces 地界内路径
        是 agent 合法工作区，ADR 2026-10-01 锚点）——匹配前先把参数值中工作
        空间地界内的路径剔除，地界内盘符路径不再命中（见
        :meth:`_scrub_workspace_boundary_paths`）。

        Args:
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            元组 (action, rule_name)：
            - action 为 "allow" 表示白名单命中（放行）
            - action 为 "block" 或 "needs_approval" 表示匹配到拦截/审批规则
            - action 为空字符串表示未匹配到任何规则
            - rule_name 为匹配到的规则名称
        """
        first_reject: tuple[str, str] = ("", "")
        reject_priority = -1
        best_allow: tuple[str, str] = ("", "")
        allow_priority = -1
        for rule in self._rules:
            if not self._rule_hit(rule, tool_name, args):
                continue
            action = rule.get("action", "block")
            rule_name = rule.get("name", "unknown")
            priority = self._rule_priority(rule)
            if action == "allow":
                # 记录最高优先级 allow（平级取先声明者）
                if priority > allow_priority:
                    best_allow, allow_priority = (action, rule_name), priority
            elif priority > reject_priority:
                first_reject, reject_priority = (action, rule_name), priority

        if best_allow[0] and allow_priority >= reject_priority:
            return best_allow
        return first_reject

    def _rule_hit(self, rule: dict[str, Any], tool_name: str, args: dict[str, Any]) -> bool:
        """单规则命中判定（工具/参数/关键词/正则；匹配面双入口共用）。

        `_match_rules`（action 裁决）与 `_match_final_approval`（终审匹配）共用
        本判定，防两套匹配逻辑漂移。
        """
        # 检查工具是否匹配（支持 ["*"] 通配 + 可选模糊匹配）
        tools = rule.get("tools", [])
        if not self._tool_matches_rule(tool_name, tools):
            return False

        params = rule.get("params", [])
        boundary_exempt = bool(rule.get("workspace_boundary_exempt"))
        for param_name in params:
            value = args.get(param_name)
            if value is None:
                continue
            value_str = str(value)
            if boundary_exempt:
                value_str = self._scrub_workspace_boundary_paths(value_str)

            for pattern_def in rule.get("patterns", []):
                pat_type = pattern_def.get("type", "keyword")
                pat_value = pattern_def.get("value", "")

                if pat_type == "keyword":
                    # S4 安全整改（评估 2026-09-24 / 执行方案批次 1）：argv
                    # 归一化匹配——裸子串匹配可被空白变体绕过（"rm  -rf"
                    # 双空格不含单空格子串 "rm -rf"）。归一化 = shlex 分词
                    # （解析失败的退化输入回退按任意空白切分）后单空格
                    # 重join + 大小写折叠，语义保持（只吃空白/大小写差异）。
                    if self._keyword_in(pat_value, value_str):
                        return True
                elif pat_type == "regex":
                    # 正则匹配
                    try:
                        if re.search(pat_value, value_str):
                            return True
                    except re.error:
                        logger.warning(
                            "[%s] Invalid regex in rule '%s': %s",
                            self.name,
                            rule.get("name", "?"),
                            pat_value,
                        )
        return False

    def _scrub_workspace_boundary_paths(self, value: str) -> str:
        """剔除参数值中用户根 workspaces 地界内的路径（声明豁免规则的匹配面收缩）。

        host_path_access 的立法本意是盘符绝对路径 = 摸安装目录/系统目录（真
        宿主程序区）；工作空间根迁 <user_root>/workspaces（ADR 2026-10-01 锚点）
        后，地界内盘符路径是 agent 的合法工作区，照旧弹审批即误伤（真机
        2026-10-01：cd 进自己工作空间 → 连续拒绝 → bash 被 tool_fail_loop
        熔断）。边界单源 zone_policy.user_root_workspace_boundary()（与位置闸
        同一地界锚定）；正则大小写不敏感、路径分隔符归一（\\ 与 / 等价），
        边界后随的路径 token 一并剔除；地界兄弟目录（workspaces-backup/
        workspacesX）经词边界守卫不误剔。用户根不可得时原样返回（fail-closed，
        规则照旧全量匹配）。
        """
        boundary = zone_policy.user_root_workspace_boundary()
        if boundary is None:
            return value
        segments = [re.escape(part.rstrip("\\/")) for part in boundary.parts]
        pattern = (
            r"(?<![A-Za-z0-9])" + r"[\\\\/]+".join(segments) + r"(?![A-Za-z0-9_.\-])(?:[\\\\/]+[\w.\-/]*)?"
        )
        return re.sub(pattern, "", value, flags=re.IGNORECASE)

    def _keyword_in(self, pattern: str, value: str) -> bool:
        """argv 归一化关键词命中判定（S4）。

        归一化：shlex.split 分词（posix=False 保留 Windows 反斜杠；解析失败
        回退按任意空白切分）→ 单空格重 join → casefold。只消除空白与大小写
        差异，不改变 token 语义。

        词边界保留：归一化会吃掉模式串首尾空白——"at "（at 调度器）退化为
        二元子串 "at"，命令串里 AppData 路径片段全部误命中（工作空间根迁用户
        根后命令常驻 AppData，真机 2026-10-01：WSL 形态 cd 命令被连续拒绝至
        bash 熔断）。原模式带首尾空白 = 立法锚定了词边界，归一化值上按整词
        匹配；无首尾空白的模式（"dd if=" 等子串语义）照旧子串匹配。裸子串
        兜底分支不变：带空白模式的字符变体（"scurl " 含 "curl "）仍命中
        （fail-closed 方向不收口）。

        未命中时再比对裸 casefold 子串（覆盖模式串含引号等 shlex 会剥离的
        字符的场景，取并集 = fail-closed 方向）。
        """

        def _norm(text: str) -> str:
            try:
                tokens = shlex.split(text, posix=False)
            except ValueError:
                tokens = text.split()
            return " ".join(tokens).casefold()

        norm_pat = _norm(pattern)
        if norm_pat:
            norm_val = _norm(value)
            if pattern != pattern.strip():
                if re.search(rf"(?<!\w){re.escape(norm_pat)}(?!\w)", norm_val):
                    return True
            elif norm_pat in norm_val:
                return True
        return pattern.casefold() in value.casefold()

    def _rule_priority(self, rule: dict[str, Any]) -> int:
        """规则优先级（缺省 0）。非法值告警并按 0 处理，与非法正则同防御。"""
        raw = rule.get("priority", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[%s] Invalid priority in rule '%s': %r (treated as 0)",
                self.name,
                rule.get("name", "?"),
                raw,
            )
            return 0

    def _check_path_traversal(self, args: dict[str, Any]) -> str:
        """检查路径遍历攻击（增强版）。

        检测 ../ 等路径遍历模式，防止通过相对路径绕过工作目录限制。
        使用 Path.resolve() 解析绝对路径，同时检测 URL 编码绕过和符号链接。
        """
        from pathlib import Path  # noqa: PLC0415

        for key in self._path_params:
            if key not in args:
                continue

            path = str(args[key])

            # 1. 检查原始路径中的遍历模式
            if ".." in path.replace("\\", "/"):
                return f"Path traversal detected in raw path: {path}"

            # 2. 检查空字节注入——先于 resolve：嵌 NUL 的路径在 resolve 处
            #    恒抛 ValueError/OSError，放其后本检查永不命中、只落泛化 Invalid path
            if "\x00" in path:
                return f"Null byte injection detected: {path}"

            # 3. 使用 Path.resolve() 解析绝对路径（处理符号链接）
            try:
                resolved = Path(path).resolve()
                if ".." in str(resolved):
                    return f"Path traversal in resolved path: {path} -> {resolved}"
            except (OSError, ValueError) as e:
                return f"Invalid path: {path} ({e})"

            # 4. 检查编码绕过（URL 编码、双重编码等）
            if "%" in path:
                try:
                    decoded = urllib.parse.unquote(path)
                    decoded2 = urllib.parse.unquote(decoded)
                    if ".." in decoded.replace("\\", "/") or ".." in decoded2.replace("\\", "/"):
                        return f"Encoded path traversal detected: {path}"
                except Exception as e:
                    # fail-closed：检查器解码失败按检测命中处理——静默跳过
                    # 等于给编码穿越留绕过通道；warning 留痕便于甄别误报
                    logger.warning(
                        "[security_check] URL 解码异常，按编码穿越命中处理（fail-closed）| path=%s | error=%s",
                        path,
                        e,
                    )
                    return f"Encoded path traversal detected (decode failed): {path}"

        return ""

    def _check_sensitive_paths(self, args: dict[str, Any]) -> str:
        """检查路径参数是否命中敏感系统目录黑名单（内置安全机制）。

        Host 模式不再限制工作目录边界，但 OS 核心目录（Windows 的
        C:/Windows/System32、Linux 的 /etc /proc 等）始终禁止触碰。
        使用共享的 is_sensitive_path 统一判定。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        for key in self._path_params:
            if key in args:
                value = args[key]
                if not isinstance(value, str):
                    continue
                hit, matched = is_sensitive_path(value)
                if hit:
                    return f"Path hits sensitive system dir: {value} (matched: {matched})"
        return ""

    def _check_nul_redirect(self, args: dict[str, Any]) -> str:
        """检查命令参数是否含 CMD 风格 nul 重定向（内置安全机制）。

        Windows CMD 的 ``>nul`` / ``2>nul`` 把输出丢弃到 NUL 设备，语法合法；
        但同样的字符串在 Git Bash / Linux bash 下不识别 NUL 设备，反而会
        创建一个名为 ``nul`` 的真实文件（垃圾文件）。本检查拦截此类命令，
        命中后走软拦截反馈给 LLM，提示改用 ``2>/dev/null``。

        匹配范围限定在 command/cmd 参数（命令字符串），不影响路径参数。
        正则 ``(?:2\\s*)?>+\\s*&?\\s*nul(?!\\w)`` 覆盖 ``>nul`` / ``2>nul`` /
        ``>>nul`` / ``2>>nul`` / ``>&nul`` 等变体，大小写不敏感；负向断言
        ``(?!\\w)`` 排除 ``null`` / ``nullable`` 等合法标识符，也确保
        ``>/dev/null`` 不被误命中（``dev/null`` 里 nul 前无重定向符 ``>``）。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        for key in self._command_params:
            if key not in args:
                continue
            value = args[key]
            if not isinstance(value, str):
                continue
            # (?:2\s*)?  可选的 fd 前缀 2
            # \s*>+      一个或多个 >（覆盖 >>）
            # \s*&?\s*   可选的 &（覆盖 >&nul 这种合并写法）
            # nul        目标 nul
            # (?!\w)     后面不能跟单词字符（排除 null/nullable/nuls 等合法标识符）
            m = re.search(r"(?:2\s*)?>+\s*&?\s*nul(?!\w)", value, re.IGNORECASE)
            if m:
                return f"CMD-style nul redirect detected: {m.group(0)!r} in command"
        return ""

    def _is_dangerous_tool(self, ctx: PluginContext, tool_name: str, tool_args: dict[str, Any] | None = None) -> bool:
        r"""判定工具调用是否属于危险操作（host 模式下需要参数审批把关）。

        双轨判定（满足任一即为危险）：
        1. policy.execution == "command_in_container" —— bash 等命令执行类
           （容器不可用时降级到 host，命令任意性高，必须审批）
        2. 工具参数命中声明的 dangerous_operations —— **参数级判定**：
           `read:/etc/`（路径前缀）、`write:C:\Windows\`（路径前缀）、
           `delete_lines:`（操作参数匹配）、`rm -rf`（命令子串）。
           常规读写（如 D:/myproject 内文件）不命中声明 → 非危险 → 直接放行，
           不再"存在即危险"地全量弹审批（GAP 修复：消除 file_read/file_write
           常规操作 100% 审批的过度打扰）。

        dangerous_operations 优先取 config_files 注入的 builtin_tools_config
        （plugin.json 声明，内核命名空间合并），其次 tool_registry 服务；
        均不可用时回退到只看 policy.execution（保持原有兜底行为）。

        浏览器工具（execution == "browser_mcp"）不进轨道 1：调用经 MCP Bridge
        网关（token/白名单/域名治理），不执行任意命令，降级到 host 时按轨道 2
        判定（browser 的参数不在危险操作声明词汇表 → 放行）。

        Args:
            ctx: 插件执行上下文
            tool_name: 工具名称
            tool_args: 工具参数字典（轨道 2 参数级判定用）

        Returns:
            是否危险
        """
        # 轨道 1：命令执行类（policy.execution 判定；browser_mcp 经 Bridge 治理，不属此类）
        policy = _policy_loader.resolve(tool_name)
        if policy.execution == "command_in_container":
            return True

        # 轨道 2：参数命中声明的 dangerous_operations（参数级判定）
        dangerous_ops = self._get_dangerous_operations(ctx, tool_name)
        if dangerous_ops and self._args_hit_dangerous_ops(tool_args, dangerous_ops):
            return True
        return False

    # ── 轨道 2 参数级判定辅助 ──────────────────────────────────

    _PATH_ARGS = ("path", "file_path", "directory", "dest", "target", "output_path", "working_dir")
    _CMD_ARGS = ("command", "cmd")
    _OP_ARGS = ("operation", "op", "action")

    def _args_hit_dangerous_ops(self, tool_args: dict[str, Any] | None, ops: list[str]) -> bool:
        r"""按参数内容判定是否命中 dangerous_operations 声明。

        声明格式（与 config/tools/builtin_tools_config.yaml 对齐）：
        - ``read:/etc/`` / ``write:C:\Windows\``：操作:路径前缀，匹配路径参数
        - ``delete_lines:``：操作:（空模式），匹配操作参数值
        - ``rm -rf``：裸命令模式，匹配 command/cmd 参数子串

        无法解析的条目按裸命令模式处理（子串匹配，保守兜底）。

        Args:
            tool_args: 工具参数字典
            ops: dangerous_operations 声明列表

        Returns:
            是否命中（命中 = 本次调用需要按危险处理）
        """
        if not tool_args:
            return False
        args = {k: str(v) for k, v in tool_args.items() if isinstance(v, (str, int, float))}

        for raw in ops:
            raw_s = str(raw)
            if ":" in raw_s:
                op_name, _, pattern = raw_s.partition(":")
                if pattern:
                    # 路径前缀匹配（大小写不敏感，分隔符归一化；replace/lower
                    # 均保长，pattern 非空则 norm 必非空）
                    norm_pattern = pattern.replace("\\", "/").lower()
                    for pk in self._PATH_ARGS:
                        val = args.get(pk, "")
                        if val and val.replace("\\", "/").lower().startswith(norm_pattern):
                            return True
                else:
                    # 空模式：操作参数值匹配（如 file_write 的 operation=delete_lines）
                    for ok in self._OP_ARGS:
                        val = args.get(ok, "")
                        if val and val.lower() == op_name.lower():
                            return True
            else:
                # 裸命令模式：command/cmd 子串匹配
                norm_cmd = raw_s.lower()
                if not norm_cmd:
                    continue
                for ck in self._CMD_ARGS:
                    val = args.get(ck, "")
                    if val and norm_cmd in val.lower():
                        return True
        return False

    def _is_isolated(self, ctx: PluginContext, execution_contexts: list[dict[str, Any]]) -> bool:
        """判断当前批次是否按隔离任务处置（免审批默认准入，单点判定）。

        两条件同时成立（ADR 2026-09-29-isolated-subtask-approval-card-batch-credit，
        修订 2026-09-24-security-check-hardening-s1-s4 的批次 all() 语义）：
        1. 任务级隔离：批次中任一 context 携带 task_isolated=True（writer 语义 =
           任务隔离 AND provider≠host——非隔离任务全 False；隔离任务纯宿主批次
           判非，回落所选档，fail-closed 方向）；
        2. 批次级执行平面：不存在「宿主裸跑的命令执行类调用」。只读/文件类等
           宿主路由辅助工具（policy host_direct）不拖垮整批——2026-09-15「隔离
           容器即安全边界」裁定的免审批默认归队；其读写风险由基础检查、位置闸
           （全档执法）与非旁路档规则轨道独立覆盖。宿主裸跑 bash（主 agent L1
           路由/宿主路径/force_host/降级）仍回落所选档审批（S1 保持）。

        isolation_level 是隔离的唯一真相源：隔离任务（isolated/None/空）豁免
        「环境类」检查（危险工具分类门槛，未命中规则的参数不弹审批）——但
        参数级黑名单规则（block/needs_approval）与隔离正交，照常匹配处置。
        task_isolated 由 isolation_guard 按任务 isolation_level 归一化后注入
        每个 context。

        Args:
            ctx: 插件执行上下文（命令执行类判定经 policy loader，读服务面降级安全）
            execution_contexts: isolation_guard 写入的工具执行上下文列表

        Returns:
            True=按隔离任务处置（环境类检查豁免），False=非隔离任务（危险工具需授权）
        """
        if not execution_contexts:
            return False
        if not any(c.get("task_isolated") for c in execution_contexts):
            return False
        return not any(
            c.get("provider") == "host" and self._carries_host_execution(ctx, c)
            for c in execution_contexts
        )

    def _carries_host_execution(self, ctx: PluginContext, context: dict[str, Any]) -> bool:
        """判断单个宿主 context 是否承载执行平面（批次判定的决定性子集）。

        - tool_name 缺失（非 isolation_guard 写面/未知形态）→ 保守按执行平面
          处理（fail-closed）；
        - 命令执行类工具（policy execution == command_in_container，args 级
          dangerous_operations 判定不在本判定——参数级风险归规则/位置闸轨道）。
        """
        tool_name = str(context.get("tool_name") or "")
        if not tool_name:
            return True
        return self._is_dangerous_tool(ctx, tool_name)

    def _get_dangerous_operations(self, ctx: PluginContext, tool_name: str) -> list[str]:
        """获取工具声明的 dangerous_operations（危险工具判定轨道 2 数据源）。

        优先级（高 → 低）：
        1. config_files 注入的 builtin_tools_config（plugin.json 声明
           ``config/tools/builtin_tools_config.yaml``，内核按 id 命名空间合并进
           ``plugin.get_config()``）——配置驱动、随内核重启生效，是唯一真相源；
        2. engine 注入的 tool_registry 服务（运行时注册表）；
        3. 空（兜底，不影响主流程）。

        Args:
            ctx: 插件执行上下文
            tool_name: 工具名称

        Returns:
            工具声明的 dangerous_operations 列表，无声明或不可用时为空列表
        """
        # 轨道 2 主数据源：注入配置（config_files 命名空间 id = builtin_tools_config）
        config_ops = self._dangerous_ops_by_tool.get(tool_name)
        if config_ops is not None:
            return config_ops

        registry = None
        with contextlib.suppress(KeyError):
            registry = ctx.get_service("tool_registry")

        if registry is None:
            return []

        tool_def = registry.get(tool_name)
        if tool_def is None:
            return []

        return getattr(tool_def, "dangerous_operations", None) or []

    def _parse_dangerous_ops_config(self) -> dict[str, list[str]]:
        """解析注入配置中的工具 dangerous_operations 声明。

        数据形状对齐 config/tools/builtin_tools_config.yaml 的 ``tools`` 列表
        （collect_tool_info.py 生成）：``tools[].name`` / ``tools[].dangerous_operations``。

        Returns:
            {tool_name: dangerous_operations}；未注入或形状不符返回空 dict。
        """
        cfg = self._config.get("builtin_tools_config")
        if not isinstance(cfg, dict):
            return {}
        tools = cfg.get("tools")
        if not isinstance(tools, list):
            return {}
        result: dict[str, list[str]] = {}
        for entry in tools:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            ops = entry.get("dangerous_operations")
            result[str(name)] = [str(op) for op in ops] if isinstance(ops, list) else []
        return result
