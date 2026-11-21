"""管道级免审批与永久白名单（审批疲劳治理 v2 实现面）。

从 security_check/plugin.py 拆出的管道级覆盖表、血缘链查表、永久白名单、
审计落盘与审批授予分支；SecurityCheckPlugin 经 ApprovalScopeMixin 继承使用，
server.py 经 plugin.py 的同名再出口消费（模块级表对象单一实例，两端同读）。

设计真值：`docs/working/审批管道级免审批_设计稿v2_2026-11-06.md`；
实现决策：ADR 2026-11-12-pipeline-scope-approval-and-permanent-whitelist。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from _libs.pipeline_base.types import StateKeys

if TYPE_CHECKING:
    from collections.abc import Callable, Container

    from _libs.pipeline_base.plugin import PluginContext

logger = logging.getLogger(__name__)

# 管道级指纹记忆 state 键（安全域 task.* 命名空间，子任务可读；随管道终态回收）。
APPROVED_SIGNATURES_KEY = "task.approved_signatures"

# 管道级权限档覆盖表：键 = pipeline_id（与设置意图作用域精确对齐），值域四档。
# 与会话级 _PERMISSION_MODES 两表并行、写入方分离（会话选择器只写会话表，
# 审批卡授予分支与撤销端点只写本表，互不覆写）。
PIPELINE_PERMISSION_MODES: dict[str, str] = {}

# pipeline_id → 父 pipeline_id 血缘边（进程内增量补全；重启丢失映射 =
# 子树退化回弹卡，fail-closed 方向，不静默放权）。
PIPELINE_PARENTS: dict[str, str] = {}

# 永久免审批白名单：命令精确指纹集合（跨管道跨会话），落名单文件。
PERMANENT_WHITELIST: set[str] = set()

# 血缘链上溯上限（防环/防异常长链）。
_LINEAGE_MAX_DEPTH = 8

# ── grants 账本执法面（P1 接线，ADR 2026-11-19-grants-ledger-and-tool-classification）──
# grants 两面 capability handle（policy=分级裁决 / ledger=账本读写）。显式注入优先
# （测试/装配缝）；生产路径经 _GRANTS_REF_RESOLVER（plugin ref 访问器，server.py
# on_load 的 _set_plugin_ref 落）按 provides namespace 解析（grants.policy/grants.ledger）。
# 两面都缺席 = grants 插件不可用 → 执法侧 inert 降级（既有语义零变化，不新增卡）；
# 拿到但调用异常 → 特权类 fail-closed 弹卡（不静默放行，D1）。
_GRANTS_POLICY_CAP: Any = None
_GRANTS_LEDGER_CAP: Any = None
_GRANTS_REF_RESOLVER: Callable[[], Any] | None = None


def set_grants_caps(policy_cap: Any, ledger_cap: Any) -> None:
    """注入 grants 两面 capability handle（公开装配缝；传 None 双清恢复自动解析）。"""
    global _GRANTS_POLICY_CAP, _GRANTS_LEDGER_CAP  # noqa: PLW0603
    _GRANTS_POLICY_CAP = policy_cap
    _GRANTS_LEDGER_CAP = ledger_cap


def set_grants_ref_resolver(resolver: Callable[[], Any] | None) -> None:
    """注入 plugin ref 访问器（生产解析缝；`_set_plugin_ref` 落，传 None 清空）。"""
    global _GRANTS_REF_RESOLVER  # noqa: PLW0603
    _GRANTS_REF_RESOLVER = resolver


def get_grants_caps() -> tuple[Any, Any]:
    """解析 ``(policy_cap, ledger_cap)``：显式注入 > ref 按 provides namespace 解析。

    解析失败（grants 插件缺席/未注入）按 (None, None)——执法侧 inert 降级，
    不阻断安全检查主链（分级/账本缺席时既有执法语义零变化）。
    """
    global _GRANTS_POLICY_CAP, _GRANTS_LEDGER_CAP  # noqa: PLW0603
    if _GRANTS_POLICY_CAP is None or _GRANTS_LEDGER_CAP is None:
        resolver = _GRANTS_REF_RESOLVER
        ref: Any = None
        if resolver is not None:
            try:
                ref = resolver()
            except Exception:  # noqa: BLE001 — ref 缺席按解析失败降级
                ref = None
        if ref is not None:
            if _GRANTS_POLICY_CAP is None:
                try:
                    _GRANTS_POLICY_CAP = ref.get_capability("grants.policy")
                except (KeyError, AttributeError):
                    _GRANTS_POLICY_CAP = None
            if _GRANTS_LEDGER_CAP is None:
                try:
                    _GRANTS_LEDGER_CAP = ref.get_capability("grants.ledger")
                except (KeyError, AttributeError):
                    _GRANTS_LEDGER_CAP = None
    return _GRANTS_POLICY_CAP, _GRANTS_LEDGER_CAP


async def zone_entries_via_project_service(ref: Any) -> list[dict[str, Any]]:
    """写区授权真源读取（security_check 既有读取路径，P1 接线）：project.load_zones。

    entries 节即写区授权条目（ADR 2026-09-24）。服务/ref 不可达 → 空列表
    （真源缺省 = 无授予，投影只收缩授权面，fail-closed 方向）。proto_adapters
    的 zone loader 经本函数接线（P0 遗留：loader 注入接口的真源承接面）。
    """
    try:
        handle = ref.get_capability("project")
        result = await handle.call("load_zones", {})
    except Exception as exc:  # noqa: BLE001 — 真源不可达 = 无授予（收缩方向）
        logger.warning("[security_check] 写区真源服务不可达，zone 投影按空处理: %s", exc)
        return []
    if not isinstance(result, dict):
        return []
    entries = result.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]

# 管道级免审批切换警告文案（与 server._mode_warning("bypass") 同口径）。
_PIPELINE_SCOPE_WARNING = (
    "旁路模式：跳过全部规则匹配与审批，危险命令可能不经确认直接执行；"
    "仅保留路径遍历、敏感目录、nul 重定向三条内置底线。请仅在信任环境使用！"
)


def state_file_path(base_file: str, filename: str) -> str:
    """与权限模式表同目录派生的持久化落点（读写同源单键 _PERMISSION_MODES_FILE）。"""
    return os.path.join(os.path.dirname(base_file), filename)


def _atomic_write_json(path: str, data: Any) -> None:
    """JSON 原子写（写临时文件后原子替换；失败留痕不阻断）。"""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:
        logger.warning("[security_check] 状态文件持久化失败 | path=%s | error=%s", path, exc)


def load_pipeline_permission_modes(path: str, allowed_modes: Container[str]) -> None:
    """启动加载管道级覆盖表（原地更新，不重绑定——server.py 持有同一对象引用）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            PIPELINE_PERMISSION_MODES.clear()
            PIPELINE_PERMISSION_MODES.update((str(k), str(v)) for k, v in data.items() if v in allowed_modes)
            logger.info(
                "[security_check] 管道级权限档覆盖表加载 | entries=%d",
                len(PIPELINE_PERMISSION_MODES),
            )
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("[security_check] 管道级权限档覆盖表加载失败 | error=%s", exc)


def save_pipeline_permission_modes(path: str) -> None:
    """持久化管道级覆盖表（原子替换）。"""
    _atomic_write_json(path, PIPELINE_PERMISSION_MODES)


def load_permanent_whitelist(path: str) -> None:
    """启动加载永久免审批白名单（解析失败按空集 fail-closed）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            PERMANENT_WHITELIST.clear()
            PERMANENT_WHITELIST.update(str(x) for x in data if isinstance(x, str) and x.strip())
            logger.info(
                "[security_check] 永久免审批白名单加载 | entries=%d",
                len(PERMANENT_WHITELIST),
            )
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("[security_check] 永久免审批白名单加载失败 | error=%s", exc)


def save_permanent_whitelist(path: str) -> None:
    """持久化永久免审批白名单（原子替换）。"""
    _atomic_write_json(path, sorted(PERMANENT_WHITELIST))


def write_audit_event(path: str, event: dict[str, Any]) -> None:
    """追加一行审计事件到 JSONL（失败留痕不阻断业务）。

    日志面向排查、台账面向问责；事件由授权动作点写入，字段至少含
    ts/event/pipeline_id/session_id/user_id。
    """
    import datetime  # noqa: PLC0415

    record = {"ts": datetime.datetime.now(datetime.UTC).isoformat(), **event}
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[security_check] 审计事件落盘失败 | event=%s | error=%s", event.get("event"), exc)


def coerce_tool_args(tc: dict[str, Any]) -> dict[str, Any]:
    """args 容错归一（执法与审批格式化共面）：JSON 字符串 → 解析 dict；
    非 dict（含非法 JSON/合法非 dict）→ 空参数（与「视为空参数」既有口径一致）。"""
    args = tc.get("args", tc.get("arguments", {}))
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return args if isinstance(args, dict) else {}


def split_command_segments(command: str) -> list[str]:
    """把复合命令串解析为单条命令段（引号/转义感知，条件分隔符归一）。

    分隔符：``&&`` / ``||`` / ``;`` / ``|`` / ``&`` / 换行，均只在引号外生效；
    单双引号与反斜杠转义内的分隔符保留原样。返回去空段后的命令列表（至少
    一条；纯空白返回空列表）。用于"命令免审批解析出所有命令"——白名单与
    同命令免批按段授权，而非整串复合命令一个指纹。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < length:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < length:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in (";", "|", "&", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [segment.strip() for segment in segments if segment.strip()]


# 归并行的参数摘要面：每条被归并命令的参数原文最多取 80 字符（超出标全长），
# 一行最多列 3 条（超出标「等 N 种」）——保证卡面 `命令:` 块被 500 字符截断时
# 参数仍可辨识，又不让归并行本身刷屏。
_PARAM_SUMMARY_CHARS = 80
_PARAM_SUMMARY_MAX_ITEMS = 3


_EXE_TOKEN = re.compile(r"^\s*(?P<exe>\S+)\s*")


def _exe_and_rest(segment: str) -> tuple[str, str]:
    """切出「首个空白分隔 token（可执行文件名）写法的原文」与其余参数原文。

    只按空白切首 token，不走 shlex——shlex 的 posix 模式会吃掉 Windows 路径的
    反斜杠（``C:\\Windows\\cmd.exe`` → ``C:Windowscmd.exe``），按空白切则绝对路径
    写法与裸命令名能正确归一。引号包裹的首 token 去引号后再归一。
    """
    match = _EXE_TOKEN.match(segment)
    if not match:
        return ("", segment.strip())
    return (match.group("exe").strip("\"'"), segment[match.end() :].strip())


def command_group_key(segment: str) -> str:
    """命令段的归并键：可执行文件名（首 token 的 basename，去 ``.exe`` 后缀）。

    同一命令的绝对路径写法与裸命令名视为同一命令；无法取到 token 时返回空串
    （调用方以整段原文兜底做键）。

    键里**不含子命令**：对任意可执行文件都无法从语法上区分「子命令」与
    「第一个位置参数」——``grep -rn x src/a.py`` 的 ``x``、``mkdir out_a``
    的 ``out_a`` 都是位置参数，任何启发式升格都会把位置参数写进展示标签
    （``grep x``）。子命令/参数的可辨性由归并行的参数摘要承载（参数原文
    前缀，不做语法猜测）。
    """
    exe = _exe_and_rest(segment)[0].replace("\\", "/").rsplit("/", 1)[-1]
    return exe[:-4] if exe.lower().endswith(".exe") else exe


def segment_parameter_text(segment: str) -> str:
    """命令段去掉可执行文件名后的参数原文（只做前缀裁剪）。

    不重新拼接 token——引号与转义保持用户原始写法，避免摘要改写命令语义
    （``-m "a b"`` 不会被压成 ``-m a b``）。子命令候选原样留在参数里
    （``git status --short`` → ``status --short``），故参数摘要天然可辨。
    """
    return _exe_and_rest(segment)[1]


def _param_preview(params: str) -> str:
    """参数摘要单元：全文若超 ``_PARAM_SUMMARY_CHARS`` 截断并标注全长。"""
    if not params:
        return "(无参数)"
    if len(params) <= _PARAM_SUMMARY_CHARS:
        return params
    return f"{params[:_PARAM_SUMMARY_CHARS]}…（全长 {len(params)} 字符）"


def _group_segments(segments: list[str]) -> list[tuple[str, list[str]]]:
    """按键分组（保序）：返回 ``[(可执行文件键, 同键命令段列表)]``。"""
    groups: dict[str, list[str]] = {}
    for segment in segments:
        key = command_group_key(segment) or segment
        groups.setdefault(key, []).append(segment)
    return list(groups.items())


def _group_label(key: str, group: list[str]) -> str:
    """组内展示标签：单条原样整段；多条退到可执行文件名（子命令留在参数摘要里）。"""
    return group[0] if len(group) == 1 else key


def _group_param_summary(group: list[str]) -> str:
    """归并组的参数摘要：逐段取参数原文去重截断，最多 ``_PARAM_SUMMARY_MAX_ITEMS`` 项。

    这里承载「这条命令到底要做什么」的可辨识信息：子命令差异（``status`` 与
    ``push --force``）与同子命令下的参数差异（``--short`` 与 ``--porcelain``）
    都能看出来——标签不做子命令升格（见 ``command_group_key``），信息全靠本摘要。
    """
    previews: list[str] = []
    for segment in group:
        preview = _param_preview(segment_parameter_text(segment))
        if preview not in previews:
            previews.append(preview)
    head = " | ".join(previews[:_PARAM_SUMMARY_MAX_ITEMS])
    remaining = len(previews) - _PARAM_SUMMARY_MAX_ITEMS
    return f"{head} 等 {len(previews)} 种" if remaining > 0 else head


def merge_command_segments(segments: list[str]) -> list[tuple[str, int]]:
    """按命令级归并命令段（展示面）：返回 ``[(展示标签, 同命令条数)]``，保序。

    归并键 = 可执行文件名（命令级）。子命令既不做硬键也不进标签——对任意可执行
    文件都无法从语法上区分「子命令」与「第一个位置参数」（``rm a`` 与
    ``mkdir a`` 里的 ``a``），硬键会让同命令不同参数继续分裂（正是本条要修的
    现象），而启发式升格会把位置参数写进标签（``grep -rn x src/a.py`` → ``grep x``）。
    故多条归并时标签只到可执行文件名，子命令/参数差异由参数摘要承载
    （``format_parsed_command_list``）。只出现一次的命令仍原样展示整段。

    标签不含来源工具名：本列表是「命令级」视图（跨调用聚合，同命令不因工具不同
    而重复列），逐次调用的 ``工具: X`` / ``命令:`` 块保留完整归属；授权面指纹
    （``plugin._make_signature``）的第一个分量即工具名，工具归属在授权面精确。

    本函数是纯展示面：返回的标签/条数不参与指纹计算，也不进任何放行判定
    （授权面见 ``plugin._approval_batch_signatures``，逐条精确指纹）。
    """
    return [(_group_label(key, group), len(group)) for key, group in _group_segments(segments)]


def format_parsed_command_list(segments: list[str]) -> list[str]:
    """审批卡「解析出的免审批命令」段（命令级归并后一条命令一行）。

    入参 = 本批全部调用解析出的命令段（``split_command_segments`` 结果拼接）。
    返回文本行（含标题行）；同命令多条只出一行，条数与被归并命令的参数摘要
    都在该行内标注。

    参数摘要无条件附在归并行上，不依赖卡面 ``命令:`` 块是否触发 500 字符截断：
    归并行原本只留标签，一旦截断发生参数文本在卡面任何位置都不可见（审批知情
    权下降），故摘要常驻——展示层自洽，也不与另一模块的截断门限耦合。

    展示粒度 ≠ 授权粒度：本函数的条目数按可执行文件名归并，授权面
    （``_approval_batch_signatures``）仍逐条命令精确指纹，故卡面条目数 ≤
    授权面指纹数。
    """
    entries = _group_segments(segments)
    if len(entries) < len(segments):
        header = f"解析出的免审批命令（同命令归并后 {len(entries)} 条 / 原 {len(segments)} 条）:"
    else:
        header = f"解析出的免审批命令（{len(entries)} 条）:"
    lines = [header]
    for idx, (key, group) in enumerate(entries, 1):
        label = _group_label(key, group)
        if len(group) == 1:
            lines.append(f"  {idx}) {label}")
            continue
        params = _group_param_summary(group)
        lines.append(f"  {idx}) {label}  （同命令 {len(group)} 条已归并；参数：{params}）")
    return lines


class ApprovalScopeMixin:
    """管道级免审批/永久白名单的授予、解析与终审匹配（宿主 SecurityCheckPlugin 继承）。

    宿主须提供 ``_state_file_path``（持久化落点）、``_rules``（规则面）、
    ``_rule_hit`` / ``_make_signature`` / ``_await_approval``（既有授权面）。
    """

    if TYPE_CHECKING:
        _rules: list[dict[str, Any]]

        def _state_file_path(self, filename: str) -> str: ...

        def _rule_hit(self, rule: dict[str, Any], tool_name: str, args: dict[str, Any]) -> bool: ...

        def _make_signature(self, tool_name: str, args: dict[str, Any]) -> str | None: ...

        def _make_signatures(self, tool_name: str, args: dict[str, Any]) -> list[str]: ...

        async def _await_approval(
            self,
            ctx: PluginContext,
            tool_name: str,
            rule_name: str,
            *,
            signatures: list[str] | None = None,
            batch_signatures: list[str] | None = None,
            final_approval: bool = False,
        ) -> dict[str, Any]: ...

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
        ) -> None: ...

    @staticmethod
    def _current_pipeline_id(ctx: PluginContext) -> str:
        """当前管道 id（state 扁平键 pipeline_id；缺失返回空串）。"""
        return str(ctx.state.get(StateKeys.PIPELINE_ID) or ctx.state.get("pipeline_id") or "")

    def _remember_lineage(self, ctx: PluginContext) -> None:
        """记录 pipeline_id → 父 pipeline_id 血缘边（子树上溯查表用）。

        子任务出生 state 携带 lineage.parent_pipeline_id（task_submit 出生协议
        一次写全）；本插件每轮经此增量补全进程内映射，孙任务即可沿链命中
        祖先的管道级覆盖档。映射不持久化：重启后子树回落到「自身键命中」，
        未命中即弹卡（fail-closed 方向，不静默放权）。
        """
        pid = self._current_pipeline_id(ctx)
        parent = str(ctx.state.get("lineage.parent_pipeline_id") or "")
        if pid and parent and parent != pid:
            PIPELINE_PARENTS[pid] = parent

    def _pipeline_override_mode(self, ctx: PluginContext) -> str | None:
        """解析管道级覆盖档（本管道键 > 血缘链上溯，上限 8 层防环）。"""
        pid = self._current_pipeline_id(ctx)
        if not pid:
            return None
        mode = PIPELINE_PERMISSION_MODES.get(pid)
        if mode:
            return mode
        seen = {pid}
        cur = PIPELINE_PARENTS.get(pid) or str(ctx.state.get("lineage.parent_pipeline_id") or "")
        depth = 0
        while cur and cur not in seen and depth < _LINEAGE_MAX_DEPTH:
            mode = PIPELINE_PERMISSION_MODES.get(cur)
            if mode:
                return mode
            seen.add(cur)
            cur = PIPELINE_PARENTS.get(cur, "")
            depth += 1
        return None

    def _approved_signatures_from_state(self, ctx: PluginContext) -> set[str]:
        """读管道级指纹记忆（JSON 数组串；解析失败按空集 fail-closed）。"""
        raw = ctx.state.get(APPROVED_SIGNATURES_KEY)
        if isinstance(raw, str):
            if not raw.strip():
                return set()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return set()
        elif isinstance(raw, (list, tuple)):
            data = list(raw)
        else:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data if isinstance(x, str) and x.strip()}

    def _remember_signatures(self, ctx: PluginContext, signatures: list[str]) -> dict[str, Any]:
        """把一批指纹并入管道级 state 键，返回须经 state_updates 落盘的更新。

        复合命令（`a && b`）按解析出的每条子命令各记一个指纹；本管道后续
        任一子命令单独出现均免批（全部指纹都在集合才整体放行）。
        """
        current = self._approved_signatures_from_state(ctx)
        new = [s for s in signatures if s and s not in current]
        if not new:
            return {}
        current.update(new)
        payload = json.dumps(sorted(current), ensure_ascii=False)
        # 本地快照同步：当次调用后续工具检查即生效（引擎合并更新在返回后）。
        ctx.state[APPROVED_SIGNATURES_KEY] = payload
        return {APPROVED_SIGNATURES_KEY: payload}

    async def _grant_pipeline_scope(
        self,
        ctx: PluginContext,
        hi_cap: Any,
        tool_name: str,
        rule_name: str,
    ) -> None:
        """授予「本管道后续免审批」：警告确认 → 写管道覆盖表 → 审计。

        fail-closed：确认卡取消/超时/交互服务不可用一律不切档（本次调用仍按
        审批卡语义放行，授权范围退化为"仅本次"）。
        """
        confirmed = await self._confirm_pipeline_scope(hi_cap, ctx, tool_name, rule_name)
        if not confirmed:
            logger.info(
                "[security_check] 管道免审批切换未确认，本次按仅本次放行 | tool=%s | rule=%s",
                tool_name,
                rule_name,
            )
            return
        pipeline_id = self._current_pipeline_id(ctx)
        if not pipeline_id:
            logger.warning("[security_check] 管道 id 缺失，管道免审批切换放弃（fail-closed）")
            return
        PIPELINE_PERMISSION_MODES[pipeline_id] = "bypass"
        save_pipeline_permission_modes(self._state_file_path("permission_modes_pipeline.json"))
        # 授予面双写（P1，执行方案 §2）：真源覆盖表已落，账本补投影 task grant
        # （subject=pipeline、tool='*'，与 proto 投影形态同构，对账测试承接防漂移）。
        await self._grants_issue(
            ctx,
            tool="*",
            action="*",
            granularity="task",
            subject_kind="pipeline",
            subject_id=pipeline_id,
            evidence="pipeline_scope",
            pipeline_id=pipeline_id,
        )
        write_audit_event(
            self._state_file_path("security_audit.jsonl"),
            {
                "event": "mode_switch",
                "action": "grant",
                "mode": "bypass",
                "pipeline_id": pipeline_id,
                "session_id": str(ctx.state.get(StateKeys.SESSION_ID, "") or ""),
                "user_id": str(ctx.state.get("user_id", "") or ""),
                "source": "approval_card",
                "tool_name": tool_name,
                "rule": rule_name,
            },
        )
        logger.warning(
            "[security_check] 管道级免审批已授予（权限档切旁路）| pipeline=%s | tool=%s",
            pipeline_id,
            tool_name,
        )

    async def _confirm_pipeline_scope(
        self,
        hi_cap: Any,
        ctx: PluginContext,
        tool_name: str,
        rule_name: str,
    ) -> bool:
        """切换前的警告确认卡（与 server._confirm_switch 同款警示形态）。

        session_id 必须传真实会话 id（thread_id 同值）——前端审批 UI 按会话
        过滤订阅，传 pipeline_id 会导致确认窗不显示、确认永远超时。
        """
        session_id = str(ctx.state.get(StateKeys.SESSION_ID, "") or "")
        try:
            create_res = await hi_cap.call(
                "create_choice",
                {
                    "session_id": session_id,
                    "thread_id": session_id,
                    "tab_id": "",
                    "title": "管道免审批切换确认",
                    "description": (
                        "将把当前管道及其派生子任务的权限档切换为旁路（免审批）。\n"
                        f"触发本次切换的操作: {tool_name}（{rule_name}）\n"
                        f"{_PIPELINE_SCOPE_WARNING}\n"
                        "管道结束自动失效；可经撤销端点提前恢复弹审批。"
                    ),
                    "options": [
                        {"id": "confirm", "label": "确认切换"},
                        {"id": "cancel", "label": "取消"},
                    ],
                    "priority": "high",
                    "user_id": str(ctx.state.get("user_id", "") or ""),
                    "pipeline_id": self._current_pipeline_id(ctx),
                },
            )
            if not isinstance(create_res, dict) or create_res.get("error"):
                raise RuntimeError(f"create_choice failed: {create_res}")
            request_id = create_res.get("request_id", "")
            wait_res = await hi_cap.call(
                "wait_for_choice",
                {"request_id": request_id, "timeout": 300},
                timeout=310.0,
            )
            if not isinstance(wait_res, dict) or wait_res.get("error"):
                return False
            return str(wait_res.get("selected_option", "")) in ("confirm", "确认切换")
        except Exception as exc:  # noqa: BLE001 — 确认通道异常一律不切档（fail-closed）
            logger.warning("[security_check] 管道免审批切换确认异常，不切档 | error=%s", exc)
            return False

    async def _grant_permanent_whitelist(
        self,
        ctx: PluginContext,
        signatures: list[str],
        tool_name: str,
    ) -> None:
        """把一批命令精确指纹加入永久免审批白名单（无指纹命令退化为仅本次）。

        复合命令按解析出的每条子命令逐条落名单（授权面 = 用户卡上看到的
        全部命令，而非整串复合命令一个指纹）；每条各留一行审计。
        双写（P1）：真源名单文件已落，逐条向账本 issue standing grant
        （subject=user/*、tool=命令指纹，与 proto 投影形态同构）。
        """
        valid = [s for s in signatures if s]
        if not valid:
            logger.info(
                "[security_check] 无指纹命令选择永久白名单，退化为仅本次 | tool=%s",
                tool_name,
            )
            return
        PERMANENT_WHITELIST.update(valid)
        await asyncio.to_thread(
            save_permanent_whitelist, self._state_file_path("permanent_whitelist.json")
        )
        for signature in valid:
            await self._grants_issue(
                ctx,
                tool=signature,
                action="*",
                granularity="standing",
                subject_kind="user",
                subject_id="*",
            )
            await asyncio.to_thread(
                write_audit_event,
                self._state_file_path("security_audit.jsonl"),
                {
                    "event": "whitelist_add",
                    "pipeline_id": self._current_pipeline_id(ctx),
                    "session_id": str(ctx.state.get(StateKeys.SESSION_ID, "") or ""),
                    "user_id": str(ctx.state.get("user_id", "") or ""),
                    "tool_name": tool_name,
                    "signature": signature,
                },
            )
        logger.info(
            "[security_check] 命令已加入永久免审批白名单 | tool=%s | count=%d | sigs=%s",
            tool_name,
            len(valid),
            ",".join(valid),
        )

    async def _enforce_final_approval(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """管道覆盖档为旁路时的终审保留：命中终审规则的操作仍逐次人审。

        免审批摩擦，不豁免终审（推送/发布/大范围删除/凭据类）。未命中的调用
        直接放行（总体仍走旁路档）；命中且用户拒绝 → 返回软拦截决策。

        Returns:
            需回传的 state 更新/拦截决策；全部放行返回 None。
        """
        updates: dict[str, Any] = {}
        # 授权面 = 卡上展示的全部命令：解析每条调用的子命令指纹，白名单/
        # 同命令免批一次覆盖整批（用户裁定：解析出所有命令再设置）。
        batch_signatures: list[str] = []
        for tc in tool_calls:
            for signature in self._make_signatures(tc.get("name", ""), coerce_tool_args(tc)):
                if signature not in batch_signatures:
                    batch_signatures.append(signature)
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = coerce_tool_args(tc)
            rule = self._match_final_approval(tool_name, args)
            if not rule:
                continue
            decision = await self._await_approval(
                ctx,
                tool_name,
                f"终审操作（管道免审批不适用）: {rule}",
                signatures=batch_signatures,
                final_approval=True,
            )
            if decision.get("pre_decided_results"):
                return {**decision, **updates}
            updates.update(decision)
        return updates or None

    def _match_final_approval(self, tool_name: str, args: dict[str, Any]) -> str:
        """终审规则匹配（仅声明 final_approval: true 的规则组）。"""
        for rule in self._rules:
            if not rule.get("final_approval"):
                continue
            if self._rule_hit(rule, tool_name, args):
                return str(rule.get("name") or "final_approval")
        return ""
