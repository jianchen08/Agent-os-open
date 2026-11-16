"""任务提交工具"""

import importlib.util
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# 跨插件共享类型（含 safe_enum_value）已上提到 SDK 公共依赖层 agentos_plugin_sdk。
# 任务领域类型以 plugins/shared/tasks/ 为权威（0.2 平铺模块，由 server.py
# 将该目录注入 sys.path），在用到处懒加载直接 import。
from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
    safe_enum_value,
)

# 共享层统一出生协议（plugins/shared/task_birth.py，与 tasks http_api 同源实现，
# 出生写面单一真值）。无条件前置（模块仅导入一次，重复项无害——tests/plugins
# shared conftest 同款做法），保证测试/生产任一进程形态下该行都被执行。
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _SHARED_ROOT)
from _libs.dangerous_targets import check_root_and_system_dirs  # noqa: E402 — 危险目标空间校验单源（_libs 共享件）
from _libs.evaluation_metrics_path import (  # noqa: E402 — 评估指标坐标探测单源（_libs 共享件）
    probe_metrics_config_path,
)
from _libs.paths import canon_path  # noqa: E402 — 路径大小写 canon 单源（_libs 共享件）
from _root_api import (  # noqa: E402 — plugins/shared 平铺模块（模式键两级解析）  # noqa: E402
    WORKSPACE_PREPARE_PHASE,
    TaskBirthError,
    birth_task_pipeline,
    now_iso_utc as _now_iso,  # noqa: E402 — 共享时间戳单点
    parse_mode_agent_key,
    state_fields,  # noqa: E402 — plugins/shared 平铺模块（ws_meta 还原）
)

logger = logging.getLogger(__name__)

#: 任务"在跑"状态集（继承门槛用）：源任务处于这些状态时拒绝接续——
#: 同一 worktree/分支上两个任务并行 = 同树双写（互相踩踏）。
_LIVE_TASK_STATUSES = frozenset({"running", "pending", "evaluating"})


def _inherit_topology_hint(ws_meta: dict[str, Any]) -> str:
    """继承坐标的拓扑提示（execution_context.workspace.mode 用）。

    坐标本体整份复制（源 mode 一字不改）；此处只给执行上下文的拓扑提示：
    worktree 源 → worktree，其余（plain/shared）→ plain。
    """
    return "worktree" if str(ws_meta.get("mode") or "") == "worktree" else "plain"

# ── 服务提供者解析 ──
#
# 内核能力经 sidecar 注入，服务解析统一走 _get_service_provider：
# - task_worker：不提供——任务执行经 chat.send_message 创建管道
#   （GAP-1 统一：task = pipeline，run 终态回写任务状态）。
# - agent_registry：由 agent_manager 插件提供——
#   server.py on_load 经 tool-executor（显式 plugin_id=agent_manager）注入
#   _agent_registry_lookup（async agent_id -> config dict | None）。这是
#   agent 配置的唯一来源：解析权（两轮匹配 + 模式键）归 agent_manager 单点，
#   本插件不回退磁盘直读——自持 rglob 既与它双份必漂移，又在装机平铺布局下
#   按代码位置推导 config 目录必然落空。未注入/查询失败 → 不可用原因，
#   调用方按闸门语义 fail-closed（层级闸）或文档化降级（完备性左值）。
# - workspace_lifecycle_manager / execution_record_storage：
#   sidecar 无等价实例 → None（调用方已有降级守卫/文档化降级）。
# 测试可 monkeypatch 模块级 _get_service_provider / set_agent_registry_lookup。


class _ServiceProviderShim:
    """服务提供者适配：get(key) 返回等价服务或 None（文档化降级）。"""

    def get(self, key: str) -> Any:
        # agent_registry 经 set_agent_registry_lookup 注入的查询钩子承接
        # （_lookup_agent_config 直取 _agent_registry_lookup，
        # 此处不提供同步 registry 对象）。
        # workspace_lifecycle_manager / execution_record_storage
        # sidecar 无等价实例：调用方已有降级守卫。
        return None


def _get_service_provider() -> Any:
    """获取 0.2 服务提供者 shim（sidecar 模式下的服务解析入口）。"""
    return _ServiceProviderShim()


# ── agent_registry 查询钩子（agent_manager 服务注入，P4 收敛）──
_agent_registry_lookup: Any = None


def set_agent_registry_lookup(lookup: Any) -> None:
    """注入 agent 配置查询钩子（server.py on_load 调用）。

    约定签名：``async lookup(agent_id: str) -> dict | None``（返回 agent_manager
    agent.get 服务解析后的 yaml dict；未命中 → None；服务不可用 → 抛异常，
    调用方拿到的不是 None 而是不可用原因——不与"Agent 不存在"混淆）。
    """
    global _agent_registry_lookup  # noqa: PLW0603
    _agent_registry_lookup = lookup
    logger.info("[TaskSubmit] agent_registry 查询钩子已注入（agent_manager 服务）")


# ── GAP-1：chat.send_message 派发器（server.py on_load 注入）──
#
# 任务执行驱动：提交成功后经内核 chat capability 的 send_message 创建任务执行
# 管道——出生走 plugins/shared/task_birth.py 统一协议（与 tasks http_api 共用）：
# 出生登记（create+no_dispatch，引擎生成 pipeline_id、pipeline_sessions 落映射）
# → 身份登记（task.id=管道 id 先于任何管道步骤）
# → 工作空间创建（同步准备，ADR 2026-10-17 新增段）
# → 执行派发（kickoff+execution_context background 启动）。state 出生即带
# task.*/lineage.* 扁平键。sidecar 进程内模块级注入（能力句柄懒解析在协程内
# 完成）；未注入（capability 缺席/测试）时提交仍落库但话术诚实（不声称"异步
# 执行中"），结果携带 warning。
_chat_sender: Any = None


def set_chat_sender(sender: Any) -> None:
    """注入 chat.send_message 派发器（server.py on_load 调用）。

    约定签名：``async sender(params: dict) -> dict``，params 即
    chat.send_message 的入参（create/message/user_id/state/
    execution_context/background）；成功返回含 ``pipeline_id`` 的响应，
    失败抛异常（由调用方记录并降级为 warning 话术）。
    """
    global _chat_sender  # noqa: PLW0603
    _chat_sender = sender
    logger.info("[TaskSubmit] chat.send_message 派发器已注入")


def _get_chat_sender() -> Any:
    """获取 chat.send_message 派发器（None = 未注入，测试可 monkeypatch）。"""
    return _chat_sender


# ── 工作空间准备器（workspace_lifecycle 插件服务，server.py on_load 注入）──
#
# ADR-2026-10-17-task-birth-workspace-inclusion：工作空间创建纳入出生协议第三段，
# 提交期同步完成。职责不在本插件——四形态（worktree / plain+显式 / plain 缺省 /
# 非 git 降级）决策与真实创建由 workspace_lifecycle 插件持有（provides namespace
# ``workspace`` 的 ``prepare_task`` 方法），本插件只做注入与话术转写。
_workspace_preparer: Any = None


def set_workspace_preparer(preparer: Any) -> None:
    """注入工作空间准备器（server.py on_load 调用）。

    约定签名：``async preparer(*, pipeline_id, state, execution_context) -> dict``，
    返回工作空间坐标 state 更新（须含非空 ``workspace``），失败抛异常。未注入 =
    出生协议第三段无执行者 → 提交一律失败（无「跳过准备」路径）。
    """
    global _workspace_preparer  # noqa: PLW0603
    _workspace_preparer = preparer
    logger.info("[TaskSubmit] 工作空间准备器已注入（workspace.prepare_task）")


async def _prepare_workspace_for_birth(
    *, pipeline_id: str, state: dict[str, Any], execution_context: dict[str, Any]
) -> dict[str, Any]:
    """出生协议第三段的准备器实现（模块级 → 注入件的适配）。

    线协议（workspace.prepare_task 返回信封）与协议内契约（返回坐标 dict /
    抛异常）在此收口：能力缺席、信封非法、`ok=false` 三者一律抛异常，
    由 task_birth 统一落 failed 记录并回传失败。
    """
    if _workspace_preparer is None:
        raise RuntimeError("workspace.prepare_task 服务未注入（工作空间创建无法执行）")
    return await _workspace_preparer(pipeline_id=pipeline_id, state=state, execution_context=execution_context)


# ── project.* 服务消费面（T6a：登记簿读面收口项目域主 project_manager）──
#
# 注入约定照 set_chat_sender 族：server.py on_load 注入 async (method, args)
# -> dict 调用方（capability handle 直连 provides namespace=project）；
# 未注入/调用失败/返回非 dict → ProjectServiceUnavailable 上抛 → 调用方
# fail-closed 落失败信封（禁止静默放行——plan 态闸/项目归属解析不可降级）。
_project_service: Any = None


class ProjectServiceUnavailable(RuntimeError):
    """project.* 服务未注入/调用失败（消费方 fail-closed 信号）。"""


def set_project_service(caller: Any) -> None:
    """注入 project.* 服务调用方（server.py on_load 调用）。

    约定签名：``async caller(method: str, args: dict) -> dict``（短方法名，
    ns 由 get_capability("project") 承载；provider 返回裸 dict，失败经
    ``error`` 键表达）。
    """
    global _project_service  # noqa: PLW0603
    _project_service = caller
    logger.info("[TaskSubmit] project.* 服务调用方已注入（project_manager）")


async def _project_call(method: str, args: dict[str, Any]) -> dict[str, Any]:
    """project.<method> 统一调用点（未注入/异常/非 dict 信封统一翻译）。"""
    if _project_service is None:
        raise ProjectServiceUnavailable("project service caller not injected")
    try:
        result = await _project_service(method, args)
    except Exception as exc:
        raise ProjectServiceUnavailable(f"project.{method} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise ProjectServiceUnavailable(f"project.{method} returned non-dict payload")
    return result


def _project_unavailable_fail(exc: Exception) -> ToolExecutionResult:
    """服务不可用统一失败信封（登记簿读面不可降级，明确报错不静默）。"""
    return create_failure_result(
        error=f"项目登记服务不可用: {exc}",
        error_code="PROJECT_SERVICE_UNAVAILABLE",
    )


# ── orchestration 模块自定位加载 ──
#
# orchestration.py 是 tasks 域平铺模块，位于本插件目录的同级 tasks 子目录
# （仓内布局 plugins/shared/tasks/；装机平铺布局 plugins/tasks/）。tasks 域
# server.py 的 bootstrap 只覆盖其自身装载语境——本 sidecar 运行期裸名
# ``import orchestration`` 不保证命中（tasks 目录未必在 sys.path）。解析序：
# 裸名 import 优先（装载期已注入的进程零开销）→ ImportError 时按已知布局
# 候选显式定位，spec_from_file_location 加载并注册 sys.modules（幂等）；
# 候选全空抛带路径清单的 RuntimeError（fail-honest，不静默）。


def _orchestration_candidate_paths() -> list[Path]:
    """orchestration.py 已知布局候选（去重保序）。"""
    plugin_dir = Path(__file__).resolve().parent
    candidates = [
        plugin_dir.parent / "tasks" / "orchestration.py",
        Path(_SHARED_ROOT) / "tasks" / "orchestration.py",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _load_orchestration_module() -> Any:
    """加载 tasks 域编排模块（懒加载单点；进程内幂等）。"""
    try:
        import orchestration  # noqa: PLC0415 — 裸名优先（bootstrap 已注入语境）

        return orchestration
    except ImportError:
        pass
    for candidate in _orchestration_candidate_paths():
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("orchestration", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["orchestration"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # 半初始化模块不得驻留 sys.modules（毒化后续导入）；原错误上抛。
            sys.modules.pop("orchestration", None)
            raise
        return module
    raise RuntimeError(
        "orchestration 模块不可用：裸名导入失败且候选路径均不存在："
        + ", ".join(str(p) for p in _orchestration_candidate_paths())
    )


def _short_id(full_id: str) -> str:
    """全 id → 短 id（LLM 展示用，12 位；短 id 原样返回）。"""
    if not full_id:
        return full_id
    return full_id[:12]


async def _resolve_short_id(rows: list[dict[str, Any]] | None, candidate: str) -> str:
    """短 id → 全 id（state 聚合前缀唯一解析；精确命中原样；歧义返回 AMBIGUOUS:<id>）。

    规则：
    1. 精确命中（pipeline_id / task.owned.<id>）→ 原样；
    2. 候选 ≤12 位 → 前缀匹配（pipeline_id + task.owned.<id>）；
       唯一命中返回全 id，多命中返回 `AMBIGUOUS:<candidate>`，无命中原样。
    """
    if not candidate:
        return candidate
    for row in rows or []:
        if str(row.get("pipeline_id") or "") == candidate:
            return candidate
        if any(str(k).startswith(f"task.owned.{candidate}.") for k in row):
            return candidate
    if len(candidate) <= 12:
        hits: list[str] = []
        for row in rows or []:
            pid = str(row.get("pipeline_id") or "")
            if pid.startswith(candidate):
                hits.append(pid)
            for k in row:
                ks = str(k)
                if ks.startswith("task.owned."):
                    owned_id = ks[len("task.owned.") :].split(".", 1)[0]
                    if owned_id.startswith(candidate) and owned_id not in hits:
                        hits.append(owned_id)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return f"AMBIGUOUS:{candidate}"
    return candidate


# ── GAP-1 统一：state 聚合读取器（server.py on_load 注入，依赖校验等读面）──
# 约定签名：``() -> list[dict]``（sync 或 async，返回管道 state 聚合行，
# 行为扁平点号键如 {"pipeline_id": ..., "task.status": ...}）。None = 未注入
# （依赖校验 fail-closed）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader


def _metrics_config_path() -> Path:
    """评估指标配置路径（config/plugins/evaluation/evaluation_metrics.yaml）。

    探测算法单源 ``_libs.evaluation_metrics_path``（祖先逐级探测，首个含该
    yaml 的祖先即根；三布局单算法通吃 + 全 miss 回落向上 3 级拼相对尾），
    本侧只提供探测坐标（本插件目录）与 Path 返回形态。
    指标定义的唯一加载点（提交期校验与派发指令详情展开共用）。
    """
    here = Path(__file__).resolve().parent
    return Path(probe_metrics_config_path(str(here)))


def _load_metric_definitions() -> dict[str, dict[str, Any]]:
    """加载指标定义表（name → 定义）；缺文件/坏格式 → 空表（fail-open）。"""
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(_metrics_config_path().read_text(encoding="utf-8")) or {}
        return {str(m["name"]): m for m in data.get("metrics", []) or [] if isinstance(m, dict) and m.get("name")}
    except Exception as exc:
        logger.warning(
            "[TaskSubmit] 评估指标定义加载失败: %s",
            exc,
        )
        return {}


def _get_valid_metric_ids() -> set[str] | None:
    """获取所有合法的评估指标 ID 集合。

    评估指标的真相来源是 config/plugins/evaluation/evaluation_metrics.yaml
    （evaluation 插件同款读取）。
    用于在提交期校验 LLM 传入的 acceptance_criteria key 是否为真实存在的指标 ID，
    避免「把 pass_threshold 等 value 子字段误填为指标 ID」导致评估期反复
    METRIC_NOT_FOUND。

    Returns:
        合法指标 ID 集合；加载失败时返回 None，表示跳过校验（fail-open，
        不阻断正常提交）。
    """
    valid = set(_load_metric_definitions().keys())
    # 空集合视作加载失败（fail-open，不阻断正常提交）
    return valid or None


# ── 白话验收标准（扁平字段，唯一宣传写法；用户裁定 2026-09-29，ADR
# docs/decisions/2026-09-29-acceptance-flat-fields.md）──
# 老指标名 dict 形态静默兼容（不宣传）；两形态混用拒绝（语义归属不明）。
_FLAT_ACCEPTANCE_FIELDS = ("files", "expect", "command")
_FLAT_INSTANCE_SEP = "::"


def _metric_definition_id(key: str) -> str:
    """acceptance_criteria 键 → 指标定义 ID。

    白话展开的多实例键形如 ``file_check::chapters/chapter_025.md``（::<实例>
    后缀仅区分实例，评估参数自带真实坐标），取首段即定义 ID。
    """
    return key.split(_FLAT_INSTANCE_SEP, 1)[0]


def _validate_metric_ids(
    acceptance_criteria: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """校验 acceptance_criteria 的 key 是否为合法指标 ID（实例键按定义 ID 首段）。

    - 合法 key 保留；
    - 非法 key 剔除，记入返回的 invalid_ids 列表，由调用方决定如何处理
      （全部无效则拒绝提交，部分无效则降级保留有效项）；
    - 无法获取合法集合（_get_valid_metric_ids 返回 None）时原样返回，
      不剔除任何 key（fail-open）。

    Args:
        acceptance_criteria: 待校验的验收标准字典

    Returns:
        (过滤后的 criteria, 被剔除的无效 key 列表)
    """
    valid_ids = _get_valid_metric_ids()
    if valid_ids is None:
        return acceptance_criteria, []
    if not acceptance_criteria:
        return acceptance_criteria, []

    filtered: dict[str, Any] = {}
    invalid: list[str] = []
    for key, value in acceptance_criteria.items():
        if _metric_definition_id(key) in valid_ids:
            filtered[key] = value
        else:
            invalid.append(key)
    return filtered, invalid


def _invalid_flat_field(field: str, expectation: str) -> ToolExecutionResult:
    """白话字段形态非法的失败信封（自描述期望形态）。"""
    return create_failure_result(
        error=f"acceptance_criteria 的白话字段 {field} 形态非法，需要{expectation}。",
        error_code="INVALID_ACCEPTANCE_FIELD",
    )


def _apply_legacy_metric_aliases(acceptance_criteria: dict[str, Any]) -> dict[str, Any]:
    """深格式别名容错（就地归一，INFO 日志透明告知）——限 semantic_check 条目。

    - expected_output → expected（dict/非串形态 json.dumps/str 归一）；
    - criteria → 并入 expected（与既有值换行拼接，不覆盖）；
    归一结果落 input_params（缺席则创建），提交期必填校验按归一后判定——
    旧 criteria 形态（此前必然被拒）由此直接可用。别名不越指标定义：
    expected 是 semantic_check 的定义参数，其余指标不改写。
    """
    for metric_id, config in acceptance_criteria.items():
        if _metric_definition_id(metric_id) != "semantic_check" or not isinstance(config, dict):
            continue
        targets: list[dict[str, Any]] = [config]
        if isinstance(config.get("input_params"), dict):
            targets.append(config["input_params"])
        merged: list[str] = []
        for target in targets:
            for alias in ("expected_output", "criteria"):
                if alias not in target:
                    continue
                value = target.pop(alias)
                if isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False)
                elif value is not None and not isinstance(value, str):
                    value = str(value)
                if isinstance(value, str) and value.strip():
                    merged.append(value)
                    logger.info(
                        "[TaskSubmit] 深格式别名归一 | metric=%s | %s→expected",
                        metric_id,
                        alias,
                    )
        if merged:
            params = config.setdefault("input_params", {})
            existing = str(params.get("expected") or "")
            params["expected"] = "\n".join([existing, *merged]) if existing else "\n".join(merged)
    return acceptance_criteria


def _expand_flat_acceptance_criteria(
    acceptance_criteria: dict[str, Any],
) -> tuple[dict[str, Any], ToolExecutionResult | None]:
    """白话扁平字段展开（主形态）+ 深格式别名容错。

    - 白话字段 files/expect/command 任选组合（零指标知识零嵌套）：
      files → 逐文件 file_check（存在且非空，实例键 ``file_check::<路径>``）；
      expect → semantic_check(expected=expect)；command → bash_check(command=command)；
    - 与指标名键混用 → MIXED_ACCEPTANCE_CRITERIA 拒绝（两形态语义归属不明）；
    - 白话字段全空（无任何可执行标准）→ EMPTY_ACCEPTANCE_CRITERIA 拒绝，
      错误动态附全部可用指标+必填（复用 _metric_required_examples，不另造清单）；
    - 纯深格式 → 静默兼容原样（semantic_check 条目做别名容错）。
    """
    flat_present = [k for k in _FLAT_ACCEPTANCE_FIELDS if k in acceptance_criteria]
    if not flat_present:
        return _apply_legacy_metric_aliases(acceptance_criteria), None

    metric_keys = sorted(k for k in acceptance_criteria if k not in _FLAT_ACCEPTANCE_FIELDS)
    if metric_keys:
        logger.warning(
            "[TaskSubmit] acceptance_criteria 白话字段与指标名混用，拒绝 | flat=%s | metric_keys=%s",
            flat_present,
            metric_keys,
        )
        return {}, create_failure_result(
            error=(
                f"acceptance_criteria 混用了白话字段 {flat_present} 与指标名键 {metric_keys}，"
                "两种写法不能同时使用。请只用白话字段（files/expect/command 任选组合），"
                "或只用指标名 dict 形态（老写法，仍兼容）。"
            ),
            error_code="MIXED_ACCEPTANCE_CRITERIA",
        )

    expanded: dict[str, Any] = {}

    files = acceptance_criteria.get("files")
    if files is not None:
        if isinstance(files, str):
            files = [files]
        elif isinstance(files, dict):
            # 包装 dict 走样（LLM 常见发射形态）→ 与裸字符串同梯级容错：
            # 单键通用键对象（{"item": 路径}）或数字键顺序对象（{"0": a,...}）
            # 解包收编；多键/空洞键/dict 值意图不明，仍走下方形态闸拒绝。
            if len(files) == 1:
                only = next(iter(files.values()))
                if isinstance(only, list):
                    files = only
                elif isinstance(only, (str, int, float, bool)):
                    files = [only]
            elif files and all(k.isdigit() for k in files):
                keys = sorted(int(k) for k in files)
                if keys == list(range(len(files))):
                    files = [files[str(k)] for k in keys]
        if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
            return {}, _invalid_flat_field("files", "非空字符串数组（产出文件相对路径）")
        for path in files:
            clean = path.strip()
            expanded[f"file_check{_FLAT_INSTANCE_SEP}{clean}"] = {"input_params": {"path": clean, "check": "not_empty"}}

    expect = acceptance_criteria.get("expect")
    if expect is not None:
        if not isinstance(expect, str):
            return {}, _invalid_flat_field("expect", "非空字符串（验收期望的白话描述）")
        if expect.strip():
            expanded["semantic_check"] = {"input_params": {"expected": expect}}

    command = acceptance_criteria.get("command")
    if command is not None:
        if not isinstance(command, str):
            return {}, _invalid_flat_field("command", "非空字符串（验收命令，退出码 0=通过）")
        if command.strip():
            expanded["bash_check"] = {"input_params": {"command": command}}

    if not expanded:
        available = _metric_required_examples(sorted(_load_metric_definitions().keys()))
        menu = f"当前可用指标及必填参数：{available}。" if available else ""
        logger.warning(
            "[TaskSubmit] acceptance_criteria 白话字段全空，拒绝提交 | flat=%s",
            flat_present,
        )
        return {}, create_failure_result(
            error=(
                "acceptance_criteria 白话字段全部为空，没有任何可执行的验收标准。"
                "可用白话字段：files（产出文件相对路径数组，逐文件检查存在且非空）、"
                "expect（验收期望描述，评估 agent 在工作区内核对产出）、"
                "command（验收命令，退出码 0 = 通过）。" + menu
            ),
            error_code="EMPTY_ACCEPTANCE_CRITERIA",
        )
    return expanded, None


def _metric_required_examples(metric_ids: list[str]) -> str:
    """按指标定义动态生成必填参数示例（单源 = evaluation_metrics.yaml）。

    示例形如 ``file_check 需要 {"path": "<文件或目录路径>"}``；占位说明取自
    input_schema.properties.<field>.description。定义增删改自动跟随，永不与
    yaml 漂移。定义缺失/无必填的指标不生成条目。
    """
    definitions = _load_metric_definitions()
    examples: list[str] = []
    for metric_id in metric_ids:
        schema = (definitions.get(metric_id) or {}).get("input_schema") or {}
        required = schema.get("required") or []
        if not required:
            continue
        properties = schema.get("properties") or {}
        sample = {field: f"<{(properties.get(field) or {}).get('description') or field}>" for field in required}
        examples.append(f"{metric_id} 需要 {json.dumps(sample, ensure_ascii=False)}")
    return "；".join(examples)


def _validate_required_metric_params(
    acceptance_criteria: dict[str, Any],
) -> ToolExecutionResult | None:
    """校验各指标的必填 input_params（数据源 = 指标定义 input_schema.required）。

    缺必填参数返回失败信封（INVALID_METRIC_PARAMS，逐指标列出缺失字段，
    修复示例由同一份定义动态生成），全部齐备返回 None。校验纯数据驱动——
    新增/修改指标定义自动生效。
    """
    definitions = _load_metric_definitions()
    if not definitions:
        return None
    problems: list[str] = []
    problem_ids: list[str] = []
    for metric_id, config in acceptance_criteria.items():
        schema = (definitions.get(_metric_definition_id(metric_id)) or {}).get("input_schema") or {}
        required = schema.get("required") or []
        if not required:
            continue
        params = config.get("input_params") if isinstance(config, dict) else None
        params = params if isinstance(params, dict) else {}
        missing = [field for field in required if params.get(field) in (None, "")]
        if missing:
            problem_ids.append(metric_id)
            problems.append(f"{metric_id} 缺必填参数 {missing}")
    if not problems:
        return None
    logger.warning(
        "[TaskSubmit] acceptance_criteria 必填 input_params 缺失，拒绝提交 | problems=%s",
        problems,
    )
    examples = _metric_required_examples(problem_ids)
    hint = f"（如 {examples}）" if examples else ""
    return create_failure_result(
        error=(
            "acceptance_criteria 的评估指标缺少必填 input_params："
            + "；".join(problems)
            + "。缺失字段会在评估期每轮失败且无法自愈，"
            "请在对应指标的 input_params 中补齐后重新提交" + hint + "。"
        ),
        error_code="INVALID_METRIC_PARAMS",
    )


# 单花括号标识符占位符（全匹配，排除正则量词 \d{3}、format 规格 {:.2f} 等形态）
_PLACEHOLDER_NAME_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
# 双花括号评估期可解析形态（{{workspace}}/{{task_id}}），扫描前整体剥除
_DOUBLE_BRACE_RE = re.compile(r"\{\{[^{}]*\}\}")
# 评估期可解析的占位符白名单：{tool_id}=工作区唯一工具候选裁决
# （task_evaluate._apply_tool_id_templates）、{title}=任务标题
# （task_evaluate._inject_workspace_and_placeholders）
_RESOLVABLE_PLACEHOLDERS = frozenset({"tool_id", "title"})


def _reject_unresolvable_placeholders(
    acceptance_criteria: dict[str, Any],
) -> ToolExecutionResult | None:
    """拒绝含评估期无法解析占位符的验收参数（fail-closed）。

    派发侧把 agent 配置/编排映射表的路径模板（{output_path}/{agent_id}/
    {novel_title}/{chapter_number}/{chapter_title} 等）字面照抄进
    acceptance_criteria 时，评估期无人替换——file_check 永久查字面量路径
    必败，且参数烙在出生 state 里 LLM 无法自修，必须提交期拒绝并明示
    改写为具体路径。递归扫描 input_params 的字符串值（含嵌套 dict/list）。
    """
    found: dict[str, list[str]] = {}

    def _scan(metric_id: str, value: Any, where: str) -> None:
        if isinstance(value, str):
            stripped = _DOUBLE_BRACE_RE.sub("", value)
            for name in _PLACEHOLDER_NAME_RE.findall(stripped):
                if name not in _RESOLVABLE_PLACEHOLDERS:
                    found.setdefault(name, []).append(f"{metric_id}.{where}")
        elif isinstance(value, dict):
            for k, v in value.items():
                _scan(metric_id, v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _scan(metric_id, v, f"{where}[{i}]")

    for metric_id, config in acceptance_criteria.items():
        if isinstance(config, dict):
            for key, value in config.items():
                _scan(metric_id, value, key)
    if not found:
        return None
    detail = "；".join(f"{{{name}}} ← {locs}" for name, locs in sorted(found.items()))
    logger.warning(
        "[TaskSubmit] acceptance_criteria 含不可解析占位符，拒绝提交 | found=%s",
        sorted(found),
    )
    return create_failure_result(
        error=(
            "acceptance_criteria 含评估期无法解析的占位符：" + detail + "。"
            "路径模板需在派发时改写为具体路径（写入 goal 的同一具体值）；"
            f"可用占位符仅 {sorted(_RESOLVABLE_PLACEHOLDERS)}"
            "（{tool_id}=工作区唯一工具、{title}=任务标题）。请改写后重新提交。"
        ),
        error_code="UNRESOLVED_PLACEHOLDER",
    )


def _validate_workspace_path(workspace: str) -> str | None:
    """验证目标空间路径的安全性。

    磁盘根目录/系统危险目录检查单源 ``_libs.dangerous_targets``（盘符根判定
    =路径形态而非运行平台——Linux 侧车上对 "C:\\" 形态同样必须拒）。
    """
    if not workspace:
        return None

    # 规范化路径用于比较
    try:
        normalized = os.path.normpath(workspace)
    except (ValueError, TypeError):
        return f"目标空间路径无效: {workspace}"

    Path(normalized)

    # ── 1+2. 磁盘根目录 / 系统危险目录检查（单源：_libs.dangerous_targets）──
    root_or_system_err = check_root_and_system_dirs(normalized, workspace)
    if root_or_system_err:
        return root_or_system_err

    # ── 3. 配置文件工作空间根目录检查 ──
    normalized_lower = normalized.lower()
    try:
        from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

        ws_root = get_workspace_config_root()
        ws_root_normalized = os.path.normpath(ws_root)
        if normalized_lower == ws_root_normalized.lower():
            return (
                f"目标空间不能设置为当前配置的工作空间根目录: {workspace}。"
                f"该目录是系统管理工作空间的根目录，不允许作为任务目标操作。"
            )
    except Exception as e:
        logger.warning("[TaskSubmit] 读取工作空间配置根目录失败，跳过该检查 | error=%s", e)

    return None


def _validate_allowed_paths(value: Any) -> ToolExecutionResult | None:
    """任务书改动授权路径清单轻校验（E0-45/D3 字段面，消费方=scope_consistency）。

    只做契约面形态校验：数组（或缺省/None）且元素均为非空字符串（path 形态）；
    语义（越界检测）留给消费方。字段缺省/空数组=合法（不启用改动范围约束）。
    非法形态拒绝且错误信息指明具体字段与元素索引。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return create_failure_result(
            error=f"allowed_paths 形态非法：须为数组（元素为非空字符串路径），收到 {type(value).__name__}。",
            error_code="INVALID_ALLOWED_PATHS",
        )
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            return create_failure_result(
                error=(
                    f"allowed_paths 形态非法：第 {i} 个元素须为非空字符串"
                    f"（改动授权路径，相对工作区），收到 {type(item).__name__}。"
                ),
                error_code="INVALID_ALLOWED_PATHS",
            )
    return None


def _validate_signed_acceptance_tests(value: Any) -> ToolExecutionResult | None:
    """签发验收测试清单轻校验（E0-24/25 字段面，签发分离）。

    只做契约面结构完整性校验：数组（或缺省/None）且每个元素为对象、含
    path/frozen_version/issuer 三键、issuer 为非空字符串（签发方可追溯）；
    语义（产出验收测试未在签发后修改）留给消费方。字段缺省/空数组=合法。
    非法形态拒绝且错误信息指明具体缺失/非法的字段名。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return create_failure_result(
            error=(
                "signed_acceptance_tests 形态非法：须为数组（元素为"
                "{path, frozen_version, issuer} 对象），"
                f"收到 {type(value).__name__}。"
            ),
            error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
        )
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 形态非法：第 {i} 个元素须为对象"
                    "（含 path/frozen_version/issuer 三键），"
                    f"收到 {type(item).__name__}。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
        missing = [key for key in ("path", "frozen_version", "issuer") if key not in item]
        if missing:
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 第 {i} 个元素缺少字段：{missing}"
                    "（须含 path/frozen_version/issuer 三键）。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
        issuer = item.get("issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 第 {i} 个元素的 issuer 形态非法："
                    f"须为非空字符串（签发方标识），收到 {issuer!r}。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
    return None


def _normalize_description(value: Any) -> str:
    """将 LLM 返回的 description 归一化为 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value)


# ── 派发指令构建 ──
#
# task_submit 提交即派发（chat.send_message create 分支），把描述/验收标准/
# 工作空间提示/路径规则拼成完整输入注入下级：
# - 描述：派发消息「\n任务描述：」段；
# - 重试纠正信息：由 task_manage continue 注入消息承载（此处不重复构建）；
# - 评估指标详情 / 工作空间模式提示 / 路径使用规则：
#   按 evaluation_metrics.yaml 字段与拓扑口径展开。
#   （进度跟踪工作法注入已退役：2026-10-25 用户裁定——待办由 todo 插件跟踪，
#   派发任务不携带清单流程说明。）

_EVALUATION_PROMPT_HEADER = "评估指标详情（你的产出将被以下标准评估）："


def _build_evaluation_criteria_prompt(acceptance_criteria: dict[str, Any]) -> str:
    """按指标定义展开验收标准为可读的评估说明文本。

    指标定义（evaluation_metrics.yaml）只有 name/description/evaluator_type/
    input_schema，没有 expect/is_red_line 等判定字段——按现有字段展开
    （说明 + 评估参数），判定逻辑归 task_evaluate。

    Returns:
        格式化后的评估指标说明文本；无验收标准/定义缺失/加载失败 → 空串。
    """
    if not acceptance_criteria or not isinstance(acceptance_criteria, dict):
        return ""
    definitions = _load_metric_definitions()
    if not definitions:
        return ""

    parts: list[str] = []
    for metric_id, config in acceptance_criteria.items():
        definition = definitions.get(_metric_definition_id(metric_id))
        if not definition:
            continue
        lines: list[str] = []
        lines.append(f"- {metric_id}：{definition.get('description') or '(无说明)'}")
        if config and isinstance(config, dict):
            input_params = config.get("input_params")
            if input_params:
                lines.append(f"  评估参数：{json.dumps(input_params, ensure_ascii=False)}")
        parts.append("\n".join(lines))
    if not parts:
        return ""
    return f"\n\n{_EVALUATION_PROMPT_HEADER}\n\n" + "\n".join(parts)


def _build_workspace_guidance(ec: dict[str, Any]) -> str:
    """按 execution_context 工作空间声明生成场景提示与路径规则。

    - 继承坐标（inherit_from 命中）：空间事实由机制给出——源任务 id、工作
      目录、分支、基座逐项写明（问题 6：机制回填事实，不让模型自行断言
      "工作空间已保留"这类未经机制确认的陈述）；
    - 显式 workspace：worktree=源项目隔离副本（改完自动合并回源）/ plain=直接
      操作目标目录；shared 态（父任务空间）由子任务继承表达，不单独提示；
    - 无显式 workspace：任务在默认隔离目录执行（工作空间根/{task_id}）。
    系统自动管理路径，下级只用相对路径。
    """
    ws_spec = ec.get("workspace") if isinstance(ec, dict) else None
    if not isinstance(ws_spec, dict):
        return ""
    mode = ws_spec.get("mode") or "plain"
    inherit = ws_spec.get("inherit")
    if isinstance(inherit, dict) and inherit.get("source_task_id"):
        scene = (
            f"你接续任务 {inherit['source_task_id']} 的工作空间继续执行："
            f"工作目录 {inherit.get('path') or '-'}，"
            f"分支 {inherit.get('branch') or '-'}，"
            f"基座 {inherit.get('project_root') or '-'}。"
            "以上坐标由系统从源任务整份复制而来（上轮文件与分支状态原样保留），"
            "任务描述中关于工作空间的表述以本段事实为准。"
            "上下文不继承（本次重新开始），需要的历史结论请自行从工作目录读取。"
        )
    elif ws_spec.get("explicit"):
        scene = (
            "你在目标项目的隔离副本中执行任务。使用相对路径，修改不影响原始项目；"
            "可运行 pytest/mypy/lint。评估通过后系统自动合并回目标项目"
            if mode == "worktree"
            else "你直接在目标目录中执行任务。使用相对路径。"
        )
    else:
        scene = "你在任务专属的隔离工作目录中执行任务。使用相对路径。"
    return (
        f"\n\n工作空间模式提示：{scene}"
        "\n\n路径使用规则（重要）："
        "\n- 所有文件操作使用相对路径即可，系统会自动锚定到工作目录"
        '\n- 示例：file_write(path="docs/report.md")'
    )


# 任务提交工具的 OpenAI Function Calling schema 字面量（纯声明数据，
# 与 get_tool_definition 拆离便于 review/diff 与外部对账）。
_TASK_SUBMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {
            "type": "string",
            "minLength": 1,
            "description": "目标 Agent ID（必填）。如果系统提供了 Agent 映射表，直接使用映射表中的 ID，不要用 resource_search 搜索",
        },
        "goal": {
            "type": "string",
            "description": (
                "任务目标（主形态，一段话）：标题取首行（超 60 字截断加省略号），"
                "描述为全文。需要精确控制标题/描述时改用 goal_title/goal_description"
            ),
            "minLength": 1,
        },
        "goal_title": {
            "type": "string",
            "minLength": 1,
            "description": "任务标题（必填），简短明确",
        },
        "goal_description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "description": (
                "任务描述（必填，1-2000 字符）。只写目标和背景，"
                "禁止写执行步骤/工具选择/流程顺序；引用文件只写路径（如 docs/report.md），"
                "让下级 Agent 自行读取，不要复制文件内容。"
            ),
        },
        "acceptance_criteria": {
            "type": "object",
            "description": (
                "验收标准（可选，推荐填写）。扁平白话字段，任选组合：\n"
                "- files：产出文件相对路径数组，逐文件检查存在且非空，"
                '如 ["chapters/chapter_025.md"]\n'
                "- expect：验收期望的白话描述（评估 agent 在工作区内核对产出），"
                '如 "总纲与25章每章1200-1800字剧情连续，progress.md 记录完整"\n'
                '- command：验收命令（退出码 0 = 通过），如 "pytest tests/ -q"\n'
                '示例：{"files": ["chapters/chapter_025.md"], '
                '"expect": "总纲与25章每章1200-1800字剧情连续，progress.md 记录完整"}\n'
                "不要写指标名或嵌套对象。"
            ),
        },
        "project_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": (
                "挂靠项目 ID（可选）。指定后任务在项目文件夹下执行"
                "（默认 worktree 从项目仓库分叉）；缺省为独立任务。"
                "项目经 project_create 工具创建（= 真实文件夹 + 登记），"
                "或由会话目录登记/前端表单创建后挂靠。"
            ),
        },
        "parent_task_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": (
                "父任务 ID（task=pipeline 统一后即引擎管道身份，12 位 hex）。"
                "创建任务链子任务时指定此参数关联父任务（复盘/分层链）。"
            ),
        },
        "workspace": {
            "type": "string",
            "description": (
                "目标项目路径。指定任务需要操作（读取或修改）的项目目录。"
                "**重要**：当任务需要对某个特定文件夹进行读取或修改时，"
                "必须设置此参数为该目标文件夹的路径，否则任务将无法定位到正确的目标目录。"
                "**不要用它表达'接续'——接续走 inherit_from；本参数只表示'在哪个目录干活'。**"
                "可用范围（按任务类型）：根任务可填；普通子任务不可填（继承父任务）。"
                "工作空间拓扑由 workspace_mode 决定（worktree=在目标项目上建隔离副本；"
                "plain=直接操作目标目录）；执行环境隔离由 isolation_level 决定，两者独立。"
            ),
        },
        "workspace_mode": {
            "type": "string",
            "enum": ["worktree", "plain"],
            "description": (
                "工作空间拓扑（根任务可选，默认 worktree）。"
                "worktree：在目标项目上创建 git worktree 隔离操作，不影响原项目（默认）。"
                "plain：直接在目标目录工作，不建 worktree、不切分支。"
                "与 isolation_level（执行环境容器/宿主）相互独立。"
                "普通子任务不可选（继承父任务空间）。"
            ),
        },
        "isolation_level": {
            "type": "string",
            "enum": ["non_isolated", "isolated"],
            "description": (
                "执行环境隔离级别（根任务可选，默认使用系统配置）。"
                "non_isolated：非隔离，直接在宿主环境执行。"
                "isolated：隔离，在容器执行环境中工作。"
                "只决定执行环境，不决定工作空间拓扑（拓扑由 workspace_mode 决定）。"
                "普通子任务不可选（继承父任务）。"
            ),
        },
        "inherit_from": {
            "type": "string",
            "description": (
                "源任务 ID：填写即接续该任务的工作空间（整份复制其 worktree/分支/"
                "基座坐标），上下文重新开始；不填=普通新任务。"
            ),
        },
        "mode": {
            "type": "string",
            "description": (
                "模式身份键（可选，[a-z][a-z0-9_]{0,63}，小写标识）。"
                "派发带 mode → 子任务出生即受该模式包控制（模式物料/人设接管"
                "按该模式解析，设计 D6）；执行编排由目标 Agent 确定后即随之"
                "确定（缺省 autonomous），派发时无需指定。轻校验（批 B8）："
                "目标执行者为 mode_X/<stem> 键时 mode 必须等于 X；挂靠项目"
                "登记了 mode_tags 时 mode 必须 ∈ 该集合（未登记/不填 mode "
                "不校验，零隐式填充——集合可经 project_create 携 mode_tags "
                "登记）。"
            ),
        },
        "allowed_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "任务书改动授权路径清单（可选）：本次任务被授权改动的文件/目录"
                "绑定表（相对工作区，/ 分隔，目录前缀匹配），"
                "如 [\"plugins/shared/task_submit/\"]。"
                "由评估侧 scope_consistency 指标消费（越界改动检测）；"
                "缺省=不启用改动范围约束。"
            ),
        },
        "signed_acceptance_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "签发验收测试文件路径（相对仓库根）",
                    },
                    "frozen_version": {
                        "type": "string",
                        "description": "签发时冻结的测试版本标识",
                    },
                    "issuer": {
                        "type": "string",
                        "description": "签发方标识（验收测试由谁签发）",
                    },
                },
                "required": ["path", "frozen_version", "issuer"],
            },
            "description": (
                "签发验收测试清单（可选）：签发方冻结版本的验收测试登记"
                "（path/frozen_version/issuer 三键），供评估侧核对产出"
                "验收测试未被签发后修改；缺省=无签发件。"
            ),
        },
    },
    "required": ["goal", "target_id"],
}


def _inherit_source_id(inp: dict[str, Any]) -> str:
    """继承源任务 ID（扁平 inherit_from 优先；旧式嵌套 inherit.from 同口径）。

    只承载一个源任务 ID——任务 ID 由系统生成（工具返回值与失败通知给出），
    agent 不自选坐标对象。
    """
    direct = str(inp.get("inherit_from") or "").strip()
    if direct:
        return direct
    cfg = inp.get("inherit")
    if isinstance(cfg, dict):
        return str(cfg.get("from") or "").strip()
    return ""


def _retired_inherit_mode_given(inp: dict[str, Any]) -> bool:
    """inherit_mode 是否被显式传入（该参数已撤下——pipe 是空壳，上下文按
    「重新开始即重新开始」不进该字段）。

    命中只留 WARN 不报错：旧调用方仍在传该键，而新语义（inherit_from 单参数
    = 接续工作空间）已覆盖其唯一有效意图（继承文件），报错会打断在跑的
    面板续写路径却不带来额外正确性。
    """
    if inp.get("inherit_mode"):
        return True
    cfg = inp.get("inherit")
    return isinstance(cfg, dict) and bool(cfg.get("mode"))


class TaskSubmitTool(BuiltinTool):
    """任务提交工具（任务域读/写全走 pipeline state 聚合行，无 YAML 通道）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）；schema 见模块级 _TASK_SUBMIT_INPUT_SCHEMA。"""
        return Tool(
            name="task_submit",
            description="任务提交工具。将任务提交给指定的 Agent 执行，配置验收标准确保结果可验证。",
            input_schema=_TASK_SUBMIT_INPUT_SCHEMA,
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["task", "submit"],
            injected_params=[
                "user_id",
                "session_id",
                "task_id",
                "pipeline_id",
                "dependencies",
                "tool_record_id",
                "parent_agent_level",
            ],
            param_level_restrictions={
                "project_id": {
                    "max_visible_level": 1,  # 项目挂靠由 L1 组织（L2/L3 子任务继承项目）
                },
                "parent_task_id": {
                    "max_visible_level": 1,  # L2/L3 系统自动注入，不应手动指定
                },
                "workspace": {
                    "max_visible_level": 3,  # 任务工作空间由 agent 直接选（普通子任务执行期拒绝）
                },
                "workspace_mode": {
                    "max_visible_level": 3,  # worktree/plain 由 agent 直接选
                },
                "isolation_level": {
                    "max_visible_level": 3,  # 执行环境由 agent 直接选
                },
            },
        )

    @staticmethod
    def _parse_goal_input(inputs: dict[str, Any]) -> dict[str, Any] | None:
        """goal 字段解析（schema 已平铺为 goal_title/goal_description）。

        优先读扁平字段；同时兼容旧的 goal 对象（历史调用方/未刷新 schema 的 LLM）
        以及 goal 作为纯文本标题的容错包装。异常形态归一为 None 并告警。
        goal_description 缺席/空白时接受平铺 description 作为显式别名（容错归一：
        语义同位仅名字不同，非静默降级）；显式非空白 goal_description 优先。
        """
        goal = inputs.get("goal")
        if goal is None and (inputs.get("goal_title") is not None):
            # 平铺后的标准入口：把扁平字段重组为下游统一使用的 {title, description}
            goal_desc = inputs.get("goal_description")
            if isinstance(goal_desc, str) and not goal_desc.strip():
                goal_desc = ""  # 空白串无语义内容，视同缺席
            if not goal_desc and inputs.get("description"):
                goal_desc = inputs["description"]
                logger.info(
                    "[TaskSubmit] goal_description 缺席，采用平铺 description 别名 | title=%s | len=%d",
                    str(inputs.get("goal_title"))[:80],
                    len(_normalize_description(goal_desc)),
                )
            goal = {
                "title": inputs.get("goal_title"),
                "description": goal_desc if goal_desc is not None else "",
            }
        elif isinstance(goal, str):
            import json  # noqa: PLC0415

            try:
                goal = json.loads(goal)
            except (json.JSONDecodeError, ValueError):
                # goal 单字符串为主形态（2026-10-19 参数削减）：title=首行截断
                # （>60 字加省略号），description=全文。扁平字段仍是显式细控形态
                # （本分支仅在未提供 goal_title 时到达）。
                stripped = goal.strip()
                first_line = stripped.splitlines()[0].strip() if stripped else ""
                title = first_line[:60] + ("…" if len(first_line) > 60 else "")
                goal = {"title": title, "description": goal}
                logger.info(
                    "[TaskSubmit] goal 单字符串主形态：title=首行(%d 字) description=全文(%d 字)",
                    len(title),
                    len(goal["description"]),
                )
        if not isinstance(goal, dict):
            logger.warning(
                "[TaskSubmit] goal 类型异常: %s (value=%s)",
                type(goal).__name__ if goal is not None else "None",
                str(goal)[:200] if goal else "None",
            )
            goal = None
        return goal

    async def _gate_base_submission(
        self,
        inputs: dict[str, Any],
        goal: dict[str, Any] | None,
        parent_agent_level: Any,
    ) -> tuple[dict[str, Any], str, ToolExecutionResult | None]:
        """基础字段与归属闸门：标题/描述必填 → parent_task_id 归属 → 描述长度上限。

        返回 (验证通过的 goal, 规范化后的 description, 失败结果)；失败结果非 None
        时调用方短路返回，此时前两个值无意义（恒为空占位）。
        """
        # ── 1. 基础参数验证 ──
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 为空")
            return (
                {},
                "",
                create_failure_result(
                    error="必须提供 goal（含 title 字段）",
                    error_code="MISSING_GOAL",
                ),
            )

        # ── 1.2 任务描述非空：派发给下级 Agent 的任务只有标题没有描述 =
        # 下级无目标上下文，标题承载不了执行语义，一律拒绝。
        # （priority/max_retries 参数已退役——执行层零消费者，见 ADR
        # 2026-08-24-task-submit-param-diet；显式传入按未知参数忽略。）
        description = _normalize_description(goal.get("description", ""))
        if not description.strip():
            # 报错带实际收到的参数键（剔除系统注入键），模型可据此自纠参数名
            injected = set(self.get_tool_definition().injected_params)
            received_keys = sorted(k for k in inputs if k not in injected)
            logger.warning(
                "[TaskSubmit] goal.description 缺失或空白 | title=%s | received_keys=%s",
                goal.get("title"),
                received_keys,
            )
            return (
                {},
                "",
                create_failure_result(
                    error=(
                        "必须提供任务描述（goal_description，1-2000 字符）。"
                        f"收到键={received_keys}，缺 goal_description"
                    ),
                    error_code="MISSING_DESCRIPTION",
                ),
            )

        parent_task_id = inputs.get("parent_task_id")
        # P0-3 纵深防御：校验 parent_task_id 归属，防 L2/L3 伪造他人父任务越权挂载
        # （继承他人管道/工作空间/上下文）。合法链：父任务必须由更高层级提交。
        own_ok, own_err = await self._check_parent_ownership(parent_agent_level, parent_task_id)
        if not own_ok:
            logger.warning(
                "[TaskSubmit] parent_task_id 归属校验失败 | parent=%s | L%d | reason=%s",
                parent_task_id,
                parent_agent_level,
                own_err,
            )
            return (
                {},
                "",
                create_failure_result(
                    error=own_err or "parent_task_id 归属校验失败",
                    error_code="INSUFFICIENT_PERMISSION",
                ),
            )

        logger.info(
            "[TaskSubmit] description 追踪 | has_inputs_desc=%s | has_goal_desc=%s | final_desc_len=%d | preview=%s",
            bool(inputs.get("description")),
            bool(goal.get("description")),
            len(description),
            description[:80] if description else "(empty)",
        )

        # ── 描述长度硬限制（防止超大消息体打爆 LLM API） ──
        max_desc_len = 2000
        if len(description) > max_desc_len:
            logger.warning(
                "[TaskSubmit] 描述超长拒绝 | len=%d | max=%d | preview=%.100s",
                len(description),
                max_desc_len,
                description[:100],
            )
            return (
                {},
                "",
                create_failure_result(
                    error=(
                        f"任务描述过长（{len(description)}字符，上限{max_desc_len}字符）。"
                        "请精简描述，只写目标和文件路径，让下级 Agent 自行 file_read 文件内容。"
                    ),
                    error_code="DESCRIPTION_TOO_LONG",
                ),
            )
        return goal, description, None

    @staticmethod
    def _normalize_acceptance_criteria(
        acceptance_criteria: Any,
    ) -> tuple[dict[str, Any], ToolExecutionResult | None]:
        """验收标准归一化：类型重置 + 指标 ID 合法性校验。

        全部 key 无效 → 拒绝提交并引导 LLM 使用正确指标 ID；
        部分无效 → 剔除无效项后继续（降级，不阻断）。
        返回 (归一化后的 criteria, 失败结果)。
        """
        if not isinstance(acceptance_criteria, dict):
            logger.warning(
                "[TaskSubmit] acceptance_criteria 类型异常: %s，重置为空 dict",
                type(acceptance_criteria).__name__,
            )
            acceptance_criteria = {}

        # ── 验收标准铁律：不 fallback、不默认、不覆盖，只认大模型输入 ──
        # 评估指标完全由大模型在 acceptance_criteria 中显式指定，
        # 禁止用 agent 配置的 recommended_metrics 自动补全或覆盖，
        # 禁止任何形式的兜底默认值。模型不传 → 无验收标准（不强行补）。
        # recommended_metrics 仅作文档参考，不参与提交期逻辑。
        if acceptance_criteria:
            logger.info(
                "[TaskSubmit] 采用大模型显式传入的验收标准 | metrics=%s",
                list(acceptance_criteria.keys()),
            )

        # ── 白话扁平字段展开（主形态）+ 深格式别名容错 ──
        expanded, flat_fail = _expand_flat_acceptance_criteria(acceptance_criteria)
        if flat_fail is not None:
            return {}, flat_fail
        acceptance_criteria = expanded

        # ── 评估指标 ID 合法性校验 ──
        # 防止 LLM 把 pass_threshold / $text 等 value 子字段误填为 acceptance_criteria
        # 的 key（即 metric_id），导致评估期 METRIC_NOT_FOUND 反复重试直至失败。
        original_keys = list(acceptance_criteria.keys())
        if original_keys:
            normalized, invalid_ids = _validate_metric_ids(acceptance_criteria)
            if invalid_ids and not normalized:
                valid_ids = _get_valid_metric_ids() or set()
                valid_list = ", ".join(sorted(valid_ids)) if valid_ids else "(指标加载失败)"
                menu = _metric_required_examples(sorted(valid_ids))
                logger.warning(
                    "[TaskSubmit] acceptance_criteria 全部 key 无效，拒绝提交 | invalid=%s | valid=%s",
                    invalid_ids,
                    sorted(valid_ids),
                )
                return {}, create_failure_result(
                    error=(
                        f"acceptance_criteria 的 key（评估指标 ID）全部无效: "
                        f"{invalid_ids}。这些 key 必须是真实存在的评估指标 ID，"
                        f"不能是 pass_threshold / expected_output 等 value 子字段。"
                        f"当前合法指标 ID: {valid_list}。"
                        + (f"各指标必填参数：{menu}。" if menu else "")
                        + "也可改用白话扁平字段 files/expect/command（任选组合，无需指标知识）。"
                        "请修正后重新提交（系统不会自动补全或覆盖你传入的指标）。"
                    ),
                    error_code="INVALID_METRIC_ID",
                )
            if invalid_ids:
                logger.warning(
                    "[TaskSubmit] 剔除 acceptance_criteria 中的无效 metric_id | invalid=%s | kept=%s",
                    invalid_ids,
                    list(normalized.keys()),
                )

            # ── 必填 input_params 校验（评估器 schema 契约）──
            # 指标定义的 input_schema.required 是评估器侧硬性入参（如 file_check
            # 必须带 path）。缺必填参数的任务在评估期必然每轮失败且 LLM 无法
            # 自修（参数烙在出生 state 里），必须提交期拒绝并明示缺什么。
            param_fail = _validate_required_metric_params(normalized)
            if param_fail is not None:
                return {}, param_fail

            placeholder_fail = _reject_unresolvable_placeholders(normalized)
            if placeholder_fail is not None:
                return {}, placeholder_fail

            return normalized, None
        return acceptance_criteria, None

    @staticmethod
    def _apply_parent_task_injection(
        parent_agent_level: Any,
        parent_task_id: str | None,
        inputs: dict[str, Any],
    ) -> tuple[str | None, ToolExecutionResult | None]:
        """L2/L3 层级闸门 + 注入回退：显式指定 parent_task_id 一律拦截；
        未指定时回退管道注入的 task_id 作为父任务。
        返回 (生效的 parent_task_id, 失败结果)。"""
        # ── L2/L3 层级校验：禁止显式指定 parent_task_id ──
        if parent_agent_level >= 2 and parent_task_id is not None:
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 parent_task_id=%s，已拦截",
                parent_agent_level,
                parent_task_id,
            )
            return None, create_failure_result(
                error=f"L{parent_agent_level} Agent 不能显式指定 parent_task_id（系统自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        injected_task_id = inputs.get("task_id")
        if parent_task_id is None and injected_task_id:
            parent_task_id = injected_task_id
            logger.info(
                "[TaskSubmit] 自动注入 parent_task_id=%s (来自管道所属任务)",
                parent_task_id,
            )
        return parent_task_id, None

    async def _resolve_project_binding(
        self,
        inputs: dict[str, Any],
        parent_task_id: str | None,
        parent_agent_level: int,
        explicit_workspace_raw: str,
    ) -> tuple[str, ToolExecutionResult | None]:
        """项目挂靠解析（project_id）：L1 显式指定；L2/L3 沿父链由系统继承。

        项目 = 文件夹 + 登记（ADR 2026-08-27）：挂靠键 task.parent_project_id
        由 task_submit 唯一写入（state 行），每级子任务继承同值——深层任务
        仍归属项目（任务树分组/制品聚合按单跳键覆盖全深度）。
        项目创建与任务解耦（2026-08-30 裁定）：创建走独立入口
        （project_create 工具 / 会话目录登记 / 前端表单），工具只挂靠。

        校验序：L2/L3 显式指定拒绝 → 登记存在性 → 文件夹存在性 → 与 agent
        显式 workspace 冲突拒绝；全部通过后把 inputs["workspace"] 覆写为项目文件夹。
        返回 (project_id（空串 = 不挂靠）, 失败结果)。
        """
        project_id = str(inputs.get("project_id") or "")
        if project_id and parent_agent_level >= 2:
            # 防伪造：L2/L3 的项目归属由系统继承，显式指定一律拒绝
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 project_id=%s，已拦截（项目归属沿父链继承）",
                parent_agent_level,
                project_id,
            )
            return "", create_failure_result(
                error=(
                    f"L{parent_agent_level} Agent 不能显式指定 project_id——"
                    "子任务的项目归属由系统沿父链自动继承，直接提交子任务即可。"
                ),
                error_code="L2_CANNOT_SPECIFY_PROJECT_ID",
            )
        if not project_id and parent_task_id:
            project_id = await self._inherit_project_id(parent_task_id)
            if project_id:
                logger.info(
                    "[TaskSubmit] 子任务继承项目归属 | parent_task_id=%s | project_id=%s",
                    parent_task_id,
                    project_id,
                )
        if project_id:
            # 项目路径解析（T6a：project.list_paths 服务消费——登记簿读面收口
            # 项目域主，不再直读共享根登记文件）
            try:
                paths = (await _project_call("list_paths", {})).get("paths") or {}
            except ProjectServiceUnavailable as exc:
                logger.error("[TaskSubmit] project.list_paths 不可用 | project_id=%s | err=%s", project_id, exc)
                return "", _project_unavailable_fail(exc)
            project_path = str(paths.get(project_id) or "")
            if not project_path:
                logger.error(
                    "[TaskSubmit] project_id 不在登记中（可能已删除）| project_id=%s",
                    project_id,
                )
                return "", create_failure_result(
                    error=f"项目 {project_id} 不存在（登记中无此 id，可能已被删除）",
                    error_code="PROJECT_NOT_FOUND",
                )
            if not os.path.isdir(project_path):
                return "", create_failure_result(
                    error=f"项目文件夹不存在: {project_path}（项目可能已被删除，请检查项目登记）",
                    error_code="PROJECT_FOLDER_MISSING",
                )
            # 挂项目任务的 workspace 恒为项目文件夹（worktree 从它分叉）；
            # 显式给了不同路径 = 语义冲突，拒绝而非二选一猜测。
            if explicit_workspace_raw:
                # 等价判定经 canon 单源（Windows normpath+小写折叠，POSIX 原样）
                same = canon_path(explicit_workspace_raw) == canon_path(project_path)
                if not same:
                    return "", create_failure_result(
                        error=(
                            f"挂靠项目（{project_id}）的任务工作空间恒为项目文件夹 {project_path}，"
                            f"不能另指定 workspace={explicit_workspace_raw}。请去掉 workspace 参数重新提交。"
                        ),
                        error_code="PROJECT_WS_CONFLICT",
                    )
            inputs["workspace"] = project_path
        return project_id, None

    @staticmethod
    async def _gate_project_plan_state(project_id: str, target_type: Any, target_id: Any) -> ToolExecutionResult | None:
        """plan 态派发白名单闸（ADR 2026-09-17 决策 2；批 B4 注册化）。

        挂靠项目 workflow_state = plan（方案讨论段）时，只许派发
        系统基线（PLAN_DISPATCH_WHITELIST）∪ Σ 注册包 planning_executors
        贡献的执行者（调研/环境准备/各包声明的规划期外包，贡献经 mode_keys
        磁盘聚合，非法形态条目零贡献 fail-closed）；执行类派发直接拒绝——
        过门（project_state transition plan→running，人审）后解除。
        继承链上的子任务同闸（project_id 已沿父链继承，按最终归属判定）。
        """
        if not project_id:
            return None
        from _root_api import (  # noqa: PLC0415
            PLAN_DISPATCH_WHITELIST,
            collect_planning_executors,  # noqa: PLC0415
        )

        try:
            row = await _project_call("get", {"project_id": project_id})
        except ProjectServiceUnavailable as exc:
            logger.error("[TaskSubmit] project.get 不可用，plan 态闸 fail-closed | err=%s", exc)
            return _project_unavailable_fail(exc)
        project = row.get("project") if row.get("found") else None
        if not isinstance(project, dict) or project.get("workflow_state") != "plan":
            return None
        executor = str(target_id or "") if str(target_type or "") == "agent" else ""
        allowed = PLAN_DISPATCH_WHITELIST | collect_planning_executors()
        if executor in allowed:
            return None
        logger.warning(
            "[TaskSubmit] plan 态派发被状态闸拒绝 | project_id=%s | target_type=%s | target_id=%s",
            project_id,
            target_type,
            target_id,
        )
        return create_failure_result(
            error=(
                f"项目 {project_id} 处于 plan（方案讨论）态，只允许派发 "
                f"{', '.join(sorted(allowed))}。"
                "执行类任务请先用 project_state 工具申请 plan→running 过门"
                "（触发用户审批）；打样验证亦须先过门。"
            ),
            error_code="PROJECT_PLAN_LOCKED",
        )

    @staticmethod
    async def _gate_mode_registration(project_id: str, mode: str, target_id: Any) -> ToolExecutionResult | None:
        """mode 登记两闸（ADR 2026-10-09 批 B8：派发面 = 说明 + 轻校验）。

        零隐式填充：不填 mode 维持现状（D6 归属派生照旧），两闸只在显式带
        mode 时执法——
        - 闸①（挂靠项目时）：显式 mode 须 ∈ 该项目登记 mode_tags；项目未登记
          tags 跳过（legacy 宽容），集合外拒（防漂填/笔误）；
        - 闸②（恒执法）：target_id 为 `mode_X/<stem>` 形态时其 X 即归属包
          （mode_keys.parse_mode_agent_key 同一解析），显式 mode ≠ X 拒
          （机械闸防「code_writer 拿 game 物料」，错误可见而非隐式纠偏）。

        错误信息可指导改正：指明登记集合 / 归属包，按登记填写或经项目修改面
        （project_create 同路径补挂）调整。
        """
        mode = str(mode or "").strip()
        if not mode:
            return None  # 不填 mode = 现状不变，零新逻辑
        # 闸①：项目登记集合（挂靠项目且项目登记了 tags 才执法）——项目语境下
        # 集合错误比归属错误更可指导改正（登记集合是本项目 mode 的真值源），
        # 先查集合。
        if project_id:
            try:
                row = await _project_call("get", {"project_id": project_id})
            except ProjectServiceUnavailable as exc:
                logger.error("[TaskSubmit] project.get 不可用，mode 闸 fail-closed | err=%s", exc)
                return _project_unavailable_fail(exc)
            project = row.get("project") if row.get("found") else None
            project = project if isinstance(project, dict) else None
            if project is not None and project.get("mode_tags") and mode not in project["mode_tags"]:
                logger.warning(
                    "[TaskSubmit] mode 不在项目登记集合 | project_id=%s | mode=%s | tags=%s",
                    project_id,
                    mode,
                    project["mode_tags"],
                )
                return create_failure_result(
                    error=(
                        f"mode={mode!r} 不在项目 {project_id} 登记的模式集合内"
                        f"（该项目登记模式集合为 {project['mode_tags']}，"
                        f"default_mode={project.get('default_mode') or '未标注'}）。"
                        "请按登记填写，或经项目修改面（project_create 同路径携带 "
                        "mode_tags）调整登记后再派发。"
                    ),
                    error_code="MODE_NOT_IN_PROJECT_TAGS",
                )
        # 闸②：归属包一致性（不依赖项目挂靠）
        parsed = parse_mode_agent_key(str(target_id or ""))
        if parsed is not None:
            target_pack = parsed[0]
            if mode != target_pack:
                logger.warning(
                    "[TaskSubmit] mode 与目标归属包不一致 | mode=%s | target_id=%s",
                    mode,
                    target_id,
                )
                return create_failure_result(
                    error=(
                        f"mode={mode!r} 与目标执行者 {target_id} 的归属包不一致"
                        f"（该键属 mode_{target_pack}）。请改 mode={target_pack!r}，"
                        "或改派归属包匹配的执行者。"
                    ),
                    error_code="MODE_TARGET_PACK_MISMATCH",
                )
        return None

    def _gate_subtask_param_inheritance(
        self,
        inputs: dict[str, Any],
        parent_task_id: str | None,
        explicit_workspace_raw: str,
    ) -> ToolExecutionResult | None:
        """子任务参数闸门：继承面统一（两条提交路径同规则）。

        - inherit_from（源任务 ID）：子任务同样可用——接续失败任务的工作空间
          正是子任务的高频场景（2026-10-20 用户裁定《继承机制简化方案》，
          闸门不对称消解：旧闸门只放行「继承管道」，工作空间继承被一律拒绝，
          同一参数组合在主会话路径放行、子任务路径报错）；
        - workspace（裸路径）：仍按任务类型拒绝（子任务沿父链落位是出生契约
          的核心语义，不在本次继承面裁定范围内）；
        - workspace_mode / isolation_level：显式选择拒绝（子任务继承父任务
          拓扑与隔离配置；接续场景由继承坐标自带拓扑，agent 无需填）。
        """
        if not parent_task_id:
            return None
        if explicit_workspace_raw:
            logger.warning(
                "[TaskSubmit] 普通子任务显式指定 workspace 被拒绝（继承父任务）| parent_task_id=%s | value=%s",
                parent_task_id,
                explicit_workspace_raw,
            )
            return create_failure_result(
                error=(
                    "普通子任务继承父任务的工作空间与隔离配置，不能指定 workspace。"
                    "如需接续其它任务的工作空间，请使用 inherit_from=<源任务ID>。"
                ),
                error_code="SUBTASK_INHERITS_PARAMS",
            )
        for param in ("workspace_mode", "isolation_level"):
            if inputs.get(param):
                logger.warning(
                    "[TaskSubmit] 普通子任务显式指定 %s 被拒绝（继承父任务）| parent_task_id=%s | value=%s",
                    param,
                    parent_task_id,
                    inputs.get(param),
                )
                return create_failure_result(
                    error=(
                        f"普通子任务继承父任务的工作空间与隔离配置，不能指定 {param}。"
                        "如需接续其它任务的工作空间，请使用 inherit_from=<源任务ID>。"
                    ),
                    error_code="SUBTASK_INHERITS_PARAMS",
                )
        return None

    def _reject_subroot_without_parent(
        self,
        parent_agent_level: Any,
        parent_task_id: str | None,
        injected_task_id: Any,
        inputs: dict[str, Any],
    ) -> ToolExecutionResult | None:
        """L2/L3 层级校验：自动注入后仍无 parent_task_id → 拒绝创建根任务。

        触发即说明注入链断裂，诊断字段用于定位断裂点：
        - injected_task_id 空 → param_inject 没注入或 state["task_id"] 为空
        - inputs 无 task_id 键 → param_inject 完全没处理此调用
        - task_id 键存在但为空 → state["task_id"] 在引擎 state 中缺失
        """
        if not (parent_agent_level >= 2 and parent_task_id is None):
            return None
        diag_keys = [
            k
            for k in inputs
            if k
            in (
                "task_id",
                "parent_task_id",
                "session_id",
                "pipeline_id",
                "parent_agent_level",
                "workspace",
            )
        ]
        logger.error(
            "[TaskSubmit][DIAG] L%d 无可注入 parent_task_id，注入链断裂诊断 | "
            "injected_task_id=%r | inputs_has_task_id=%s | "
            "inputs[task_id]=%r | diag_keys=%s | all_input_keys=%s",
            parent_agent_level,
            injected_task_id,
            "task_id" in inputs,
            inputs.get("task_id"),
            diag_keys,
            sorted(inputs.keys()),
        )
        logger.warning(
            "[TaskSubmit] L%d Agent 无可注入的 parent_task_id，拒绝创建根任务",
            parent_agent_level,
        )
        return create_failure_result(
            error=f"L{parent_agent_level} Agent 必须在任务上下文中提交子任务，无法创建根任务",
            error_code="L2_REQUIRES_PARENT_TASK",
        )

    async def _resolve_resource_inheritance(
        self,
        inputs: dict[str, Any],
        workspace: str,
        explicit_workspace_raw: str = "",
    ) -> tuple[str, ToolExecutionResult | None]:
        """继承参数解析统一入口（inherit_from 单参数 = 源任务 ID）。

        2026-10-20 用户裁定《继承机制简化方案》：

        - ``inherit_from`` 只承载**源任务 ID**（系统生成，agent 不自选）：按 id
          正向查源任务 state 行 → **整份复制**其 ``task.ws_meta``（mode/path/
          project_root/branch/tree_path 一字不改）→ 新任务落在同一 worktree、
          同一分支干活，评估门控的"合并+清理"链路自然接续；
        - ``inherit_mode`` 撤下（pipe 是空壳；上下文重新开始即重新开始）；
        - 互斥：``inherit_from`` 与 ``workspace`` 同给即报错，不设静默优先级；
        - 未填 ``inherit_from`` = 普通新任务（走现状默认：项目根 ⇒ fork 新
          worktree 隔离）。

        返回 (最终 workspace, 失败结果)。
        """
        if _retired_inherit_mode_given(inputs):
            logger.warning(
                "[TaskSubmit] inherit_mode 已撤下，忽略（inherit_from 单参数即接续工作空间）"
            )
        source_id = _inherit_source_id(inputs)
        if not source_id:
            return workspace, None
        if explicit_workspace_raw:
            logger.warning(
                "[TaskSubmit] inherit_from 与 workspace 互斥，同给即报错 | source=%s | workspace=%s",
                source_id,
                explicit_workspace_raw,
            )
            return workspace, create_failure_result(
                error=(
                    f"inherit_from={source_id} 与 workspace={explicit_workspace_raw} 互斥："
                    "接续走 inherit_from（继承源任务的工作空间），workspace 只表示"
                    "'在哪个目录干活'。请去掉其中一个重新提交。"
                ),
                error_code="INHERIT_WORKSPACE_CONFLICT",
            )
        ws_meta, inherit_fail = await self._resolve_inherit_coordinate(source_id, inputs)
        if inherit_fail is not None:
            return workspace, inherit_fail
        # 契约：_resolve_inherit_coordinate 失败面全走 (None, 失败信封)，
        # 无信封即坐标必非空
        assert ws_meta is not None
        # 继承坐标即新任务落点：workspace/拓扑随坐标自带（agent 无需填
        # workspace_mode/isolation_level），执行锚点与坐标一并写入出生 state。
        inputs["_inherit_ws_meta"] = ws_meta
        inputs["_inherit_source_id"] = source_id
        inherited_path = str(ws_meta.get("path") or "")
        inputs["workspace"] = inherited_path
        explicit_mode = str(inputs.get("workspace_mode") or "")
        inputs["workspace_mode"] = _inherit_topology_hint(ws_meta)
        if explicit_mode and explicit_mode != inputs["workspace_mode"]:
            logger.warning(
                "[TaskSubmit] 继承坐标自带拓扑，agent 显式 workspace_mode 被坐标覆盖 | source=%s | explicit=%s | inherited=%s",
                source_id,
                explicit_mode,
                inputs["workspace_mode"],
            )
        workspace = inherited_path
        logger.info(
            "[TaskSubmit] inherit_from 解析完成 | source=%s | mode=%s | path=%s | branch=%s",
            source_id,
            ws_meta.get("mode"),
            inherited_path,
            ws_meta.get("branch") or "-",
        )
        return workspace, None

    async def _resolve_inherit_coordinate(
        self,
        source_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, ToolExecutionResult | None]:
        """按源任务 ID 正向查表 → 三道校验 → 整份复制其工作空间坐标。

        校验序：state 聚合可读 → 源任务行命中 → 源任务已终态 → 坐标形态 →
        坐标活着（目录存在）→ worktree 源 git 身份有效 → 同 project_root 基座。
        任一步失败返回失败信封（显式报错，不静默降级成普通任务/其它落点）。

        ``inputs`` 供基座判据取本任务的项目基座（父链坐标 project_root /
        项目挂靠解析注入的 workspace）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None, create_failure_result(
                error=(
                    f"无法查找源任务 {source_id}：state 聚合不可用。"
                    "请稍后重试，或去掉 inherit_from 参数按普通新任务提交。"
                ),
                error_code="INHERIT_STATE_UNAVAILABLE",
            )
        row = next(
            (
                r
                for r in rows
                if str(r.get("task.id") or "") == source_id
                or str(r.get("pipeline_id") or "") == source_id
            ),
            None,
        )
        if row is None:
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 不存在（任务登记中查无此 id）。"
                    "请核对源任务 ID，或去掉 inherit_from 参数按普通新任务提交。"
                ),
                error_code="INHERIT_SOURCE_NOT_FOUND",
            )
        # 终态门槛：源任务仍在跑 ⇒ 拒绝继承（同一 worktree 双写会互相踩踏）。
        # 判据取内核持有的 run_status（实际运行状态），缺失时退 task.status。
        status = str(row.get("run_status") or row.get("task.status") or "").strip().lower()
        if not status:
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 状态不可知（无 run_status / task.status），"
                    "拒绝继承（无法确认源任务已终态，同树双写风险不可判）。"
                    "请待源任务结束后重试，或去掉 inherit_from 参数。"
                ),
                error_code="INHERIT_SOURCE_STATUS_UNKNOWN",
            )
        if status in _LIVE_TASK_STATUSES:
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 仍在运行（status={status}），拒绝继承："
                    "接续同一工作空间会让两个任务在同一 worktree/分支上双写。"
                    "请待源任务结束后重试，或去掉 inherit_from 参数。"
                ),
                error_code="INHERIT_SOURCE_NOT_TERMINAL",
            )
        # 坐标只读源任务自身单键 task.ws_meta（lineage.parent_ws_meta 是父链
        # 坐标，不是源任务自身坐标，不得作为继承结果）。
        ws_meta = state_fields.as_dict(row.get("task.ws_meta"), field="task.ws_meta")
        if not isinstance(ws_meta, dict) or not ws_meta.get("path"):
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 没有工作空间坐标（task.ws_meta 缺 path）。"
                    "请去掉 inherit_from 参数按普通新任务提交。"
                ),
                error_code="INHERIT_SOURCE_NO_WS",
            )
        ws_path = str(ws_meta["path"])
        # 坐标活着：目录必须存在（源已合并清理 = 坐标已死 ⇒ 显式报错引导
        # 改派，不静默回落 project_root——静默改落点正是本次事故的病根）。
        if not Path(ws_path).is_dir():
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 的工作空间已不存在：{ws_path}"
                    "（多半已随评估通过被合并清理）。"
                    "无法接续，请改派新任务，或去掉 inherit_from 参数按普通新任务提交。"
                ),
                error_code="INHERIT_SOURCE_WS_DEAD",
            )
        # worktree 源的 git 身份失效（.git 不在）⇒ 不接续：合并门控找不到分支。
        if str(ws_meta.get("mode") or "") == "worktree" and not (Path(ws_path) / ".git").exists():
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 的工作空间 git 身份已失效：{ws_path}"
                    "（.git 不存在，多半已被合并清理）。"
                    "无法接续，请改派新任务，或去掉 inherit_from 参数。"
                ),
                error_code="INHERIT_SOURCE_GIT_INVALID",
            )
        # 基座归属：同 project_root 比对（坐标对坐标）——旧的"路径须在父基座
        # 之下"判定会杀死 .agentos-workspaces 下的 worktree 源。
        base = self._current_project_base(inputs)
        source_root = str(ws_meta.get("project_root") or "")
        if base and source_root and canon_path(base) != canon_path(source_root):
            return None, create_failure_result(
                error=(
                    f"源任务 {source_id} 属于另一个项目基座（源 project_root={source_root}，"
                    f"本任务基座={base}），拒绝跨项目接续。"
                    "请去掉 inherit_from 参数，或改派同一项目下的其它任务。"
                ),
                error_code="INHERIT_PROJECT_MISMATCH",
            )
        if not (base and source_root):
            logger.info(
                "[TaskSubmit] inherit 基座不可知，跳过归属判定 | source=%s | base=%s | source_root=%s",
                source_id,
                base or "-",
                source_root or "-",
            )
        return dict(ws_meta), None

    @staticmethod
    def _current_project_base(inputs: dict[str, Any] | None) -> str:
        """本任务的项目基座（继承归属判据），取自任务坐标。

        基座取父链坐标 parent_ws_meta.project_root（空值时再退项目挂靠解析
        注入的 workspace——那是系统派生的项目文件夹，不是 agent 显式声明）；
        无基座可取时返回空串，交由调用方跳过归属判定（缺判据不误杀）。

        严禁从插件代码文件位置推导：装机用户空间的插件目录是平铺的
        （<user_root>/plugins/task_submit/tool.py，无 shared/ 这层），按文件
        层级数数出来的"容器根"会落到 <user_root> 的上级（如 AppData/Roaming），
        使任何不在该目录下的工作空间（用户在 D 盘的项目）一律被误判跨容器。
        """
        if not isinstance(inputs, dict):
            return ""
        parent_ws_meta = state_fields.as_dict(inputs.get("parent_ws_meta"), field="parent_ws_meta")
        if isinstance(parent_ws_meta, dict):
            base = str(parent_ws_meta.get("project_root") or "")
            if base:
                return base
        return str(inputs.get("workspace") or "")

    async def _gate_target_and_dependencies(
        self,
        target_type: Any,
        target_id: Any,
        parent_agent_level: Any,
        parent_task_id: str | None,
        dependencies: list[Any],
    ) -> tuple[list[Any], ToolExecutionResult | None]:
        """目标与依赖闸门：target 必填 → 目标 Agent 存在性/级别 → parent 权限复核 → 依赖存在性。

        返回 (dependencies, 失败结果)。
        """
        # ── 2. 必填参数验证 ──
        if not target_type:
            return dependencies, create_failure_result(
                error="目标类型不能为空",
                error_code="MISSING_TARGET_TYPE",
            )
        if not target_id:
            return dependencies, create_failure_result(
                error="目标 ID 不能为空",
                error_code="MISSING_TARGET_ID",
            )

        # ── 2.5 目标 Agent 存在性与级别校验 ──
        if target_type == "agent":
            valid, err_msg, err_code = await self._validate_target_agent(
                target_id,
                parent_agent_level,
            )
            if not valid:
                logger.warning(
                    "[TaskSubmit] 目标 Agent 校验失败 | target_id=%s | parent_level=L%d | error=%s",
                    target_id,
                    parent_agent_level,
                    err_msg,
                )
                return dependencies, create_failure_result(error=err_msg, error_code=err_code)
            logger.info(
                "[TaskSubmit] 目标 Agent 校验通过 | target_id=%s | parent_level=L%d",
                target_id,
                parent_agent_level,
            )

        # ── 3. 权限验证 ──
        if not await self._validate_parent_task_id(parent_agent_level, parent_task_id):
            return dependencies, create_failure_result(
                error="L2 Agent 不能显式指定 parent_task_id（系统会自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        # ── 4. 依赖任务验证 ──
        if dependencies:
            missing_ids = await self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error("[TaskSubmit] 依赖验证失败 | 不存在的任务: %s", missing_ids)
                return dependencies, create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info("[TaskSubmit] 依赖验证通过 | dependencies=%s", dependencies)
        return dependencies, None

    async def _get_agent_base_config(self, target_id: str) -> tuple[dict[str, Any] | None, ToolExecutionResult | None]:
        """agent 基座配置 dict（完备性校验的身份性字段源）——注入钩子单源。

        服务故障（钩子已注入但查询失败）→ 显式失败信封：完备性左值缺身份
        字段会被门口以"缺字段"拒派，归因错位（系统故障被说成入参不全）。
        钩子未注入（sidecar 接线缺失，测试常态）→ (None, None) 文档化降级：
        左值宁缺勿假，身份字段不计入。
        """
        if _agent_registry_lookup is None:
            logger.warning(
                "[TaskSubmit] agent 配置查询钩子未注入，完备性左值不含身份字段 (target_id=%s)",
                target_id,
            )
            return None, None
        config, unavailable = await self._lookup_agent_config(target_id)
        if unavailable:
            return None, create_failure_result(
                error=f"agent 配置服务不可用，完备性校验无法判定身份性字段：{unavailable}",
                error_code="AGENT_CONFIG_UNAVAILABLE",
            )
        return config, None

    @staticmethod
    async def _lookup_agent_config(target_id: str) -> tuple[dict[str, Any] | None, str]:
        """agent 配置单源查询：依赖注入钩子（server.py 接 agent_manager agent.get）。

        返回 (config, 不可用原因)：钩子未注入/查询抛错 → (None, 原因)；
        未命中 → (None, "")；命中 → (config dict, "")。解析权（两轮匹配 +
        模式键）归 agent_manager 单点，本插件不做任何磁盘回退。
        """
        lookup = _agent_registry_lookup
        if lookup is None:
            return None, "agent 配置查询钩子未注入（agent_manager 服务未接线）"
        try:
            config = await lookup(target_id)
        except Exception as exc:  # noqa: BLE001 — 服务故障统一翻译为不可用原因
            logger.warning("[TaskSubmit] agent 配置查询失败 (target_id=%s): %s", target_id, exc)
            return None, f"agent_manager agent.get 服务查询失败: {exc}"
        if not isinstance(config, dict):
            return None, ""
        return config, ""

    @staticmethod
    def _assemble_initial_input_keys(
        inputs: dict[str, Any],
        agent_base: dict[str, Any] | None,
    ) -> set[str]:
        """初始输入集（完备性校验左值，§3.1 三方装配的派发期可取口径）。

        - 任务性字段：task_submit 出生 state 恒写键（task.*）+ 提交参数原样键
          + messages（阶段三 kickoff 消息恒派发）+ pipeline_id（引擎出生段
          生成、派发 state 必带的保证键）；
        - 身份性字段（agent 基座）：agent.id（target_id=agent 经出生协议写全；
          target_type 非 agent 不得虚报）+ agent 配置的 tool_ids/system_prompt
          （context_build 运行时装载的两键，同源预测）；
        - execution_context/血缘键：workspace*/isolation_level 提交参数与
          子任务 lineage.parent_pipeline_id。

        取不到的键不虚报（宁缺勿假）：左值保守，缺字段门口拒派。
        """
        keys = {
            "messages",
            # 引擎保证键：pipeline_id 由出生段生成、派发 state 必带（本方法
            # 预测的是派发时点的管道 state，不是提交入参——与 task.* 出生键
            # 同性质的保证，缺它会把引擎必然供给的键误报成入参缺失）。
            "pipeline_id",
            "task.goal",
            "task.description",
            "task.acceptance_criteria",
            "task.dependencies",
            "task.submitted_by",
        }
        if inputs.get("target_type") == "agent" and inputs.get("target_id"):
            keys.add("agent.id")
        base = agent_base or {}
        if isinstance(base.get("tool_ids"), list):
            keys.add("tool_ids")
        if base.get("system_prompt"):
            keys.add("context.system_prompt")
        for key in (
            "workspace",
            "workspace_mode",
            "isolation_level",
            "project_id",
            "parent_task_id",
            "session_id",
            "thread_id",
            "inherit_from",
        ):
            if inputs.get(key):
                keys.add(key)
        if inputs.get("pipeline_id"):
            keys.add("lineage.parent_pipeline_id")
        return keys

    async def _gate_orchestration(
        self,
        inputs: dict[str, Any],
        target_id: Any,
    ) -> tuple[str, ToolExecutionResult | None]:
        """编排解析 + 完备性校验（§3.3 实例化时序：解析 → 校验 → 实例化）。

        - 两级解析（域逻辑归 tasks/orchestration.py）：显式 orchestration_key
          （config/pipelines 登记名）→ autonomous 兜底；旧调用（无编排键）
          走兜底，行为不变；mode 键不参与编排选择（模式控制经
          execution_context.mode 透传给消费面，设计 D6）；
        - 完备性（§3.5，派发期、实例化前）：初始输入集 ⊇ 所选编排派生输入集
          （成员 step required_state_inputs 并集，H2 静态声明），缺字段门口
          拒派；结构化错误随 metadata 可编程消费（M2 反馈回路）。

        返回 (解析出的编排键, 失败结果)；失败结果非 None 时调用方短路返回，
        不实例化。
        """
        orchestration = _load_orchestration_module()

        try:
            definitions = orchestration.discover_orchestrations()
            definition = orchestration.resolve_orchestration(
                explicit_key=str(inputs.get("orchestration_key") or ""),
                orchestrations=definitions,
            )
        except orchestration.OrchestrationError as exc:
            logger.warning(
                "[TaskSubmit] 编排解析失败，拒绝派发 | code=%s | %s",
                exc.error_code,
                exc.message,
            )
            return "", create_failure_result(
                error=exc.message,
                error_code=exc.error_code,
                metadata={"action": "task_submit", **exc.fields},
            )
        agent_base, base_fail = await self._get_agent_base_config(str(target_id or ""))
        if base_fail is not None:
            return "", base_fail
        initial_inputs = self._assemble_initial_input_keys(inputs, agent_base)
        step_required = orchestration.load_step_required_inputs()
        missing = orchestration.check_completeness(initial_inputs, definition, step_required)
        if missing:
            err = orchestration.incomplete_error(definition, missing)
            logger.warning(
                "[TaskSubmit] 完备性校验失败，门口拒派 | orchestration=%s | missing=%s",
                definition.key,
                missing,
            )
            return "", create_failure_result(
                error=err.message,
                error_code=err.error_code,
                metadata={"action": "task_submit", **err.fields},
            )
        logger.info(
            "[TaskSubmit] 编排解析与完备性校验通过 | orchestration=%s | required=%s",
            definition.key,
            sorted(orchestration.derived_input_set(definition, step_required)),
        )
        return definition.key, None

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务提交：参数解析与闸门 → 项目/继承/工作空间解析 → 目标与依赖校验 → 派发。

        各阶段拆为同名职责的私有方法（_parse_goal_input / _gate_* / _resolve_* /
        _normalize_acceptance_criteria）；任一阶段返回失败信封即短路返回，
        本方法只保留阶段序列与派发编排。
        """
        parent_agent_level = inputs.get("parent_agent_level")

        # ── 0.5 短 id 入参解析：LLM 回传的 parent_task_id / task_id 可能是 12 位
        # 短 id，经 state 聚合前缀唯一解析回全 id（容器 project id 生成即短，天然命中）。
        await self._resolve_short_input_ids(inputs)

        logger.info(
            "[TaskSubmit] 开始执行 | project_id=%s | parent_agent_level=%s",
            inputs.get("project_id") or "-",
            parent_agent_level,
        )

        # ── 0. 注入参数校验 ──
        if parent_agent_level is None:
            logger.error("[TaskSubmit] 注入参数缺失 | parent_agent_level 未注入")
            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        # ── 1. goal 解析 + 基础字段/归属/长度闸门 ──
        parsed_goal = self._parse_goal_input(inputs)
        goal, description, base_fail = await self._gate_base_submission(inputs, parsed_goal, parent_agent_level)
        if base_fail is not None:
            return base_fail

        acceptance_criteria, acc_fail = self._normalize_acceptance_criteria(inputs.get("acceptance_criteria", {}))
        if acc_fail is not None:
            return acc_fail

        # ── 1.5 编排契约面字段轻校验（第 5 项片 1，E0-20-D1/E0-24/25 字段面）：
        #      只做形态校验（缺省/空数组=合法，零行为变更），语义消费在片 3。
        contract_fail = _validate_allowed_paths(inputs.get("allowed_paths"))
        if contract_fail is not None:
            return contract_fail
        contract_fail = _validate_signed_acceptance_tests(inputs.get("signed_acceptance_tests"))
        if contract_fail is not None:
            return contract_fail

        parent_task_id, parent_fail = self._apply_parent_task_injection(
            parent_agent_level, inputs.get("parent_task_id"), inputs
        )
        if parent_fail is not None:
            return parent_fail

        # agent 原始入参先留底：项目解析可能注入 workspace（项目文件夹）、项目
        # 归属可能沿父链继承，下游 execution_context 的显式性判定只认原始入参，
        # 系统注入值不算显式指定。
        explicit_workspace_raw = str(inputs.get("workspace") or "")
        explicit_project_raw = str(inputs.get("project_id") or "")
        project_id, project_fail = await self._resolve_project_binding(
            inputs,
            parent_task_id,
            parent_agent_level,
            explicit_workspace_raw,
        )
        if project_fail is not None:
            return project_fail

        subtask_gate_fail = self._gate_subtask_param_inheritance(inputs, parent_task_id, explicit_workspace_raw)
        if subtask_gate_fail is not None:
            return subtask_gate_fail

        injected_task_id = inputs.get("task_id")
        root_gate_fail = self._reject_subroot_without_parent(
            parent_agent_level, parent_task_id, injected_task_id, inputs
        )
        if root_gate_fail is not None:
            return root_gate_fail

        workspace = inputs.get("workspace", "")
        workspace, inherit_fail = await self._resolve_resource_inheritance(
            inputs, workspace, explicit_workspace_raw
        )
        if inherit_fail is not None:
            return inherit_fail

        # ── 目标空间安全检查 ──
        if workspace:
            ws_error = _validate_workspace_path(workspace)
            if ws_error:
                return create_failure_result(
                    error=ws_error,
                    error_code="UNSAFE_WORKSPACE",
                )

        # 显式 Any：与拆分前一致——入参不在此处强转，类型闸门在 _gate_target_and_dependencies
        # target_type 常量内化（enum 仅 "agent"，2026-10-19 参数削减）：
        # 不再要求模型填写，实现缺省补齐；传入值仍参与闸门校验。
        target_type: Any = inputs.get("target_type") or "agent"
        # 归一回写（单一真理源）：完备性左值装配按 inputs 读 target_type 判
        # agent.id 身份键，缺省补齐值必须落回 inputs 才对其可见，否则 LLM 面
        # 派发（无此参数）恒报「缺 agent.id」门口拒死。
        inputs["target_type"] = target_type
        target_id: Any = inputs.get("target_id")
        logger.info(
            "[TaskSubmit] 任务提交 | target_type=%s | target_id=%s",
            target_type,
            target_id,
        )
        logger.debug(
            "[TaskSubmit] 任务详情 | title=%s | metric_count=%d",
            goal.get("title", "N/A"),
            len(acceptance_criteria),
        )
        dependencies, gate_fail = await self._gate_target_and_dependencies(
            target_type,
            target_id,
            parent_agent_level,
            parent_task_id,
            inputs.get("dependencies", []),
        )
        if gate_fail is not None:
            return gate_fail

        # ── 4.4 plan 态派发白名单闸（ADR 2026-09-17 决策 2：越界派发机械化拦截）──
        plan_gate_fail = await self._gate_project_plan_state(project_id, target_type, target_id)
        if plan_gate_fail is not None:
            return plan_gate_fail

        # ── 4.4.1 mode 登记两闸（ADR 2026-10-09 批 B8：显式 mode 集合/归属轻校验，
        #      不填 mode 零新逻辑）──
        mode_gate_fail = await self._gate_mode_registration(
            project_id,
            str(inputs.get("mode") or ""),
            target_id,
        )
        if mode_gate_fail is not None:
            return mode_gate_fail

        # ── 4.5 编排解析 + 完备性校验（§3.3 实例化时序：解析→校验→实例化；
        #     旧调用无编排键/mode 走③ autonomous，行为不变）──
        orchestration_key, orch_fail = await self._gate_orchestration(inputs, target_id)
        if orch_fail is not None:
            return orch_fail

        # ── 5. GAP-1 统一（state 单一真值）：任务即管道，直接经 chat.send_message
        #    创建执行管道（引擎生成 pipeline_id = task.id）——不再创建 YAML 任务
        #    记录（task_service.create_task 退役，YAML 降级只读镜像）。
        #    - 依赖校验读 state 聚合（pipeline-state.list）而非 YAML 树；
        #    - 工作空间创建由 workspace_lifecycle 插件在**提交期同步**完成（出生
        #      协议第三段，execution_context 随准备器入参同形传入），坐标先落
        #      管道 state 再派发；init 段同段幂等复核（恢复/续跑面）；
        #    - 任务状态（task.status/ended_at）由内核 run 终态回写 state。
        dispatch = await self._dispatch_task_pipeline(
            title=goal["title"],
            description=description,
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies,
            inputs=inputs,
            agent_id=(target_id if target_type == "agent" else ""),
            # 挂靠项目时管道 state 带 task.parent_project_id（project id）——
            # 前端任务树据此把子任务挂到项目节点下（项目不是管道，不能当
            # lineage 父）；L1 显式指定与 L2/L3 父链继承同值。
            parent_project_id=project_id,
            # 工作空间显式性只认原始入参（显式 workspace / 显式挂靠）——
            # 子任务沿父链继承的项目挂靠不算显式，其落位由出生契约的父链
            # 工作空间坐标决定（继承直接上级的工作空间）。
            workspace_explicit=bool(explicit_workspace_raw) or bool(explicit_project_raw),
            # 解析出的编排键（定义坐标）随出生落账，与引擎生成的实例 id 分立。
            orchestration_key=orchestration_key,
        )
        if dispatch.get("pipeline_id"):
            task_id = dispatch["pipeline_id"]
            # 引擎生成即 12 位短 id（uuid v4 simple 前 12 位，全链路统一）；
            # _short_id 幂等（短 id 原样返回），工具入口前缀解析兼容。
            short_task_id = _short_id(task_id)
            result_data: dict[str, Any] = {
                "task_id": short_task_id,
                "title": goal["title"],
                # 真实任务投影状态（出生 state 的 task.status），非字面量猜测：
                # 提交返回时任务已受理且工作空间已就绪，但任务域终态由评估闸门
                # 写——此时仍是出生态 pending（ADR 2026-10-17 修硬编码假信号）。
                "status": dispatch["task_status"],
                "target_id": target_id,
            }
            result_data["pipeline_id"] = short_task_id
            result_data["message"] = (
                f"任务 [{goal['title']}]（ID: {short_task_id}）已提交，工作空间已就绪，"
                "执行管道已创建并派发，任务正在后台执行。"
                "子任务完成后系统会自动通知你并恢复执行。"
                "在此期间请不要再调用任何工具（包括 task_manage），直接输出纯文本等待即可。"
            )
            # 工作空间入参仅对 L1 回显（L2/L3 的 workspace 参数本身被隐藏，回显内部路径属
            # 信息泄漏）；真实创建坐标落任务记录 task.ws_meta / workspace，面板读面取那里。
            if parent_agent_level == 1:
                result_data["workspace"] = workspace or ""

            return create_success_result(
                data=result_data,
                metadata={
                    "action": "task_submit",
                    "project_id": project_id,
                },
            )

        # 出生失败 = 核心流程失败：失败信封让调用方感知并可重试，不得以
        # success 掩盖。两类失败面不同，话术分开（ADR 2026-10-17）：
        # - DISPATCH_FAILED：管道未创建，任务不存在；
        # - WORKSPACE_PREPARE_FAILED：任务记录已保留并标 failed（清理锚点），
        #   重试是新建任务而非重跑。
        _dispatch_error_code = str(dispatch.get("error_code") or "DISPATCH_FAILED")
        if _dispatch_error_code == "WORKSPACE_PREPARE_FAILED":
            _fail_detail = (
                f"任务 [{goal['title']}] 的工作空间未能创建"
                f"（{dispatch.get('warning', '未知原因')}）。"
                "任务记录已保留并标记为失败，可在任务面板查看失败原因；"
                "修正工作空间配置后重新提交将创建新任务。"
            )
        else:
            _fail_detail = (
                f"任务 [{goal['title']}] 已校验，但执行管道未能创建"
                f"（{dispatch.get('warning', '未知原因')}）。"
                "任务当前不会自动执行；可稍后重试提交。"
            )
        return create_failure_result(
            error=_fail_detail,
            error_code=_dispatch_error_code,
            metadata={
                "action": "task_submit",
                "project_id": project_id,
            },
        )

    async def _dispatch_task_pipeline(  # noqa: PLR0913
        self,
        title: str,
        description: str,
        acceptance_criteria: dict[str, Any],
        dependencies: list[str],
        inputs: dict[str, Any],
        agent_id: str = "",
        parent_project_id: str = "",
        workspace_explicit: bool = False,
        orchestration_key: str = "",
    ) -> dict[str, Any]:
        """GAP-1 统一：经统一出生协议创建任务执行管道（引擎生成 id = task.id）。

        契约（plugins/shared/task_birth.py 四段式，与内核 chat_send_handler 对齐）：
        - 出生登记（create + no_dispatch）：引擎生成 pipeline_id 并在响应返回
          （三次定案：堵 id 冒占），内核创建分支落 pipeline_sessions 映射
          （子任务唤醒注入的反查锚点）；task = pipeline，无独立 YAML 记录；
        - 身份登记：task.id = 管道 id 在任何管道步骤执行前写入出生 state——
          init 体 workspace_lifecycle 的工作区共享决策以该键为任务判据；
        - 工作空间创建（同步准备，ADR 2026-10-17 新增段）：经
          workspace_lifecycle 插件的 prepare_task 服务真实创建，坐标先落 state
          再派发；失败即失败（任务记录保留并标 failed，执行派发零调用）；
        - 执行派发（注入 + background）：kickoff + execution_context 启动；
          不阻塞工具调用等待任务完成（派发即返回 id）。
        - ``birth_state``：任务域字段出生即入（task.goal/status/description/
          acceptance_criteria/dependencies/task.submitted_by/
          task.submitted_by_level——扁平点号键）；
          血缘扁平键有父形式（lineage.parent_pipeline_id = 调用方管道，
          lineage.origin_session_id 同管道）/ 根形式（lineage.root + 来源
          声明）二选一；task.status 终态由内核 run 终态回写。

        Returns:
            ``{"pipeline_id": ...}`` 出生成功（即 task.id）；``{"warning": ...,
            "error_code": ...}`` 派发器缺席/出生失败（execute 据此出失败信封，
            无 YAML 记录可清理——管道未出生即无任务；工作空间准备失败时任务
            记录已保留并标 failed，错误码与"任务不存在"面区分）。
        """
        sender = _get_chat_sender()
        if sender is None:
            return {"warning": "chat capability 未注入（sidecar 未接线），任务未派发执行"}

        parent_pipeline_id = inputs.get("pipeline_id") or ""
        if parent_pipeline_id:
            # origin_session_id = 根人类会话锚点：内核 param_inject 注入的
            # session_id/thread_id（pipeline id 非会话 id，会话归属校验按
            # 会话 id 匹配）。
            origin_session = inputs.get("session_id") or inputs.get("thread_id") or parent_pipeline_id
            lineage_keys: dict[str, Any] = {
                "lineage.parent_pipeline_id": parent_pipeline_id,
                "lineage.origin_session_id": origin_session,
            }
            # 父链工作空间坐标（param_inject 权威注入，提交时随出生契约写全）：
            # 子任务 workspace_lifecycle 据此共享父工作空间，不再依赖发起
            # 瞬间的聚合读可见性。父无工作空间（None）时不写该键——子任务
            # 侧对缺失显式报错，不静默回退。
            parent_ws_meta = inputs.get("parent_ws_meta")
            if isinstance(parent_ws_meta, dict) and parent_ws_meta.get("path"):
                lineage_keys["lineage.parent_ws_meta"] = parent_ws_meta
        else:
            lineage_keys = {
                "lineage.root": True,
                "lineage.origin.kind": "plugin",
                "lineage.origin.source": "task_submit",
            }

        kickoff = f"执行任务「{title}」。"
        if description:
            kickoff += f"\n任务描述：{description}"
        if acceptance_criteria:
            kickoff += f"\n验收标准：{acceptance_criteria}"
        execution_context = self._build_execution_context(inputs, workspace_explicit)
        kickoff += _build_evaluation_criteria_prompt(acceptance_criteria)
        kickoff += _build_workspace_guidance(execution_context)

        # 出生 state 一次写全（2026-08-28 用户裁定：任务提交时就把任务相关的
        # 放到 state 里面）：task.* 域键 + 血缘扁平键（有父/根二选一）；
        # task.id 由统一出生协议在引擎生成 id 后、任何管道步骤执行前补齐。
        # task.submitted_by_level = 提交者 Agent 层级（L1 面板/人类入口为 1，
        # LLM 路径为 param_inject 注入的调用方层级）——子任务归属闸
        # （_check_parent_ownership）据此判定父任务层级链。
        birth_state: dict[str, Any] = {
            "task.goal": title,
            "task.status": "pending",
            # 创建基线（评估完赛证据校验的 mtime/commit 判据源；tasks http_api
            # 任务列表与评估面既有读点同源契约），出生写面一次写全。
            "task.created_at": _now_iso(),
            "task.description": description or "",
            "task.acceptance_criteria": acceptance_criteria or {},
            "task.dependencies": dependencies or [],
            "task.submitted_by": inputs.get("user_id", ""),
            "task.submitted_by_level": inputs.get("parent_agent_level"),
            **lineage_keys,
        }
        # 挂靠项目（项目非管道）：任务管道 state 带 parent_project_id——
        # 前端任务树据此挂项目节点下；lineage 有父形式仍指向提交者管道
        # （项目不是管道，不能当 lineage 父）。
        if parent_project_id:
            birth_state["task.parent_project_id"] = parent_project_id
        # 任务记录增记编排键（§4.5：审计/回放/归因锚点）——编排键是管道
        # 定义坐标，与引擎在本协议阶段一生成的实例 id（pipeline_id）分立。
        birth_state["task.orchestration"] = orchestration_key
        # 编排契约面字段出生即入任务记录（第 5 项片 1 补刀，兑现切分方案
        # 「字段可从任务记录读回」判据；片 3 params 单源装配读数源）。值=
        # 校验后的入参原样（execute 挂接点已拒非法形态）。在场且有值才写键：
        # 缺省/空数组不写（零行为变更锚，记录键只增不删）。
        if inputs.get("allowed_paths"):
            birth_state["task.allowed_paths"] = list(inputs["allowed_paths"])
        if inputs.get("signed_acceptance_tests"):
            birth_state["task.signed_acceptance_tests"] = list(inputs["signed_acceptance_tests"])
        # 出生即写工作区坐标（task.ws_meta）：显式 workspace + plain 拓扑的最终
        # 坐标提交期即确定（workspace_lifecycle._start_plain_root 同形——直接
        # 操作该目录，path = project_root = source_path），随出生 state 一次写全，
        # 兑现「任务 init 即写」单一真相源（workspace_service.resolve_workspace_
        # from_state 契约注释）。缺此预写，创建→管道 init（workspace_lifecycle
        # 落 task.ws_meta）之间是首查时序窗：文件树首查 no_workspace、重试才命中
        # （2026-10-08 装机实锤 5dc60e00）。worktree/默认拓扑的最终坐标（隔离
        # 副本路径/目录创建）提交期不可知，不预写——仍由 init 段权威落笔，避免
        # 谎报坐标。
        _ws_decl = execution_context.get("workspace") or {}
        _ws_source = str(_ws_decl.get("source_path") or "")
        if _ws_source and str(_ws_decl.get("mode") or "") == "plain":
            birth_state["task.ws_meta"] = {
                "mode": "plain",
                "path": _ws_source,
                "project_root": _ws_source,
            }
        # 继承坐标（inherit_from 命中）：源任务坐标整份复制（mode/path/
        # project_root/branch/tree_path 一字不改）+ 执行锚点 workspace 同址。
        # 提交期坐标已知（源任务 state 行），预写即事实而非谎报；坐标整份
        # 承接 → 新任务落在同一 worktree、同一分支，评估门控的合并+清理链路
        # 自然接续（工作空间已就位 → 出生第三段无创建动作）。
        _inherit_meta = inputs.get("_inherit_ws_meta")
        if isinstance(_inherit_meta, dict) and _inherit_meta.get("path"):
            birth_state["task.ws_meta"] = dict(_inherit_meta)
            birth_state["workspace"] = str(_inherit_meta["path"])
            # 接续血缘事实（任务域键）：本任务接续自哪个源任务——出生协议据此
            # 判定「工作空间已就位，无创建动作」，读面据此回溯接续关系。
            birth_state["task.inherit_from"] = str(inputs.get("_inherit_source_id") or "")

        # 共享层统一出生协议（模块级已导入 task_birth）——与 tasks http_api
        # 同一实现，出生写面单一真值。
        try:
            pipeline_id = await birth_task_pipeline(
                sender,
                title=title,
                birth_state=birth_state,
                kickoff=kickoff,
                user_id=inputs.get("user_id") or "",
                agent_id=agent_id,
                execution_context=execution_context,
                # 会话归属锚点：thread_id = 面板 HTTP 透传的用户会话；LLM 路径
                # 取 param_inject 注入的 session_id（调用方管道 state 的会话坐标
                # ——主聊天管道即根人类会话，子任务绑定正确后沿链逐级传递）。
                # 出生登记据此落真会话映射（非自环）：用户向子管道发消息才会话
                # 成员校验放行、一对一进子管道；两键皆缺（无会话根任务）保持
                # 独立自环（task_birth 缺省语义）。
                thread_id=str(inputs.get("thread_id") or inputs.get("session_id") or ""),
                # 出生第三段：工作空间同步创建（ADR 2026-10-17）。失败即失败——
                # 任务记录保留并标 failed，执行派发零调用。
                prepare_workspace=_prepare_workspace_for_birth,
            )
        except TaskBirthError as exc:
            logger.error(
                "[TaskSubmit] 任务管道出生失败 | title=%s | phase=%s | err=%s",
                title,
                exc.phase or "-",
                exc,
                exc_info=True,
            )
            return {
                "warning": str(exc),
                "error_code": (
                    "WORKSPACE_PREPARE_FAILED" if exc.phase == WORKSPACE_PREPARE_PHASE else "DISPATCH_FAILED"
                ),
            }
        logger.info(
            "[TaskSubmit] 任务执行管道已创建 | task_id=%s | title=%s",
            pipeline_id,
            title,
        )
        # 语义统一：任务 id 写"自己的管道"（提交者管道）state——
        # task.owned.<id> 自持（本管道插件也能读它处理它）；执行管道的
        # task.id 身份已由统一出生协议写全（= 管道 id）。
        # 写入通道：chat.send_message 注入分支 + no_dispatch（只写 state 不派发）。
        if parent_pipeline_id:
            try:
                await sender(
                    {
                        "pipeline_id": parent_pipeline_id,
                        "message": f"登记任务「{title}」（{pipeline_id}）。",
                        **({"user_id": inputs["user_id"]} if inputs.get("user_id") else {}),
                        "no_dispatch": True,
                        "state": {
                            f"task.owned.{pipeline_id}.title": title,
                            f"task.owned.{pipeline_id}.created_at": _now_iso(),
                            f"task.owned.{pipeline_id}.submitted_by": inputs.get("user_id", ""),
                            # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：
                            # 登记键只承载声明元数据，无 status——面板 join 子行真值。
                            # 子任务挂号（ADR 2026-08-28-task-closure-three-signal-gate
                            # 信号③）：提交者管道持「已提交待回执」键，task_reminder
                            # 据此收束等待唤醒通知；子任务终态事件经 task_service 写
                            # null 清除（pipeline-state.update 无键删除语义，null 即
                            # 已回执；task.* 前缀满足该写面的任务域键约束）。
                            f"task.subtasks_pending.{pipeline_id}": _now_iso(),
                        },
                    }
                )
            except Exception as exc:
                logger.warning(
                    "[TaskSubmit] 任务登记到提交者管道失败（不影响执行）| task=%s | err=%s",
                    pipeline_id,
                    exc,
                )
        return {
            "pipeline_id": pipeline_id,
            # 出生 state 的 task.status（提交返回时任务域仍是出生态——终态归
            # 评估闸门写）。工具出口回显它而非字面量"running"。
            "task_status": str(birth_state.get("task.status") or ""),
        }

    def _build_execution_context(self, inputs: dict[str, Any], workspace_explicit: bool) -> dict[str, Any]:
        """结构化 execution_context（GAP-1 统一：随派发透传，不写 YAML metadata）。

        任务默认隔离执行——
        - isolation 默认 isolated（显式 isolation_level 优先）。
        - workspace 声明语义：无显式 workspace（含 workspace_mode 未选）时
          mode 留空——执行管道落「工作空间根/{task_id}」默认目录（plain 拓扑，
          workspace_lifecycle._bootstrap 的 mode 缺省 plain）。worktree 拓扑
          仅在显式 workspace 或显式 workspace_mode='worktree' 下成立
          （源 = workspace 路径 / 项目根）；显式 workspace 时 mode 缺省 worktree。
        - explicit = 调用方显式声明（显式 workspace 或显式项目挂靠，L1/面板
          才可能成立——L2/L3 两者均被闸门拒绝）。子任务沿父链继承的项目挂靠
          **不算显式**：source_path 仍解析为项目文件夹供工具锚点使用，但落位
          由出生契约的父链工作空间坐标（lineage.parent_ws_meta）决定——
          子任务继承直接上级的工作空间（2026-09-03 用户裁定，收窄 2026-08-30
          「挂靠即显式锚」裁定的适用范围至显式挂靠）。
        - 继承坐标（inherit_from 命中）：落点与拓扑随源任务坐标自带（agent
          不声明 → explicit=False），并携 inherit 事实块（源任务 id/分支/基座）。
        """
        _ec: dict[str, Any] = {}
        inherit_ws_meta = inputs.get("_inherit_ws_meta")
        if isinstance(inherit_ws_meta, dict) and inherit_ws_meta.get("path"):
            # 继承坐标自带拓扑与落点（agent 不声明 → explicit=False）：
            # source_path = 源任务坐标 path；inherit 事实块随坐标透传，供
            # 任务书陈述与下游读面引用（坐标事实由机制给出，不由模型自述）。
            _ec["workspace"] = {
                "source_path": str(inherit_ws_meta.get("path") or ""),
                "mode": inputs.get("workspace_mode") or "plain",
                "explicit": False,
                "inherit": {
                    "source_task_id": str(inputs.get("_inherit_source_id") or ""),
                    "mode": str(inherit_ws_meta.get("mode") or ""),
                    "branch": str(inherit_ws_meta.get("branch") or ""),
                    "project_root": str(inherit_ws_meta.get("project_root") or ""),
                    "path": str(inherit_ws_meta.get("path") or ""),
                },
            }
        elif inputs.get("workspace"):
            _ec["workspace"] = {
                "source_path": inputs["workspace"],
                "mode": inputs.get("workspace_mode") or "worktree",
                "explicit": workspace_explicit,
            }
        else:
            _ec["workspace"] = {
                "source_path": "",
                "mode": inputs.get("workspace_mode") or "",
                "explicit": False,
            }
        level = inputs.get("isolation_level") or "isolated"
        if level:
            _ec["isolation"] = {"level": level}
        # 模式键透传（消息级 execution_context 契约键，与聊天主链同形）：内核
        # 1a2 并入出生管道 initial state 后 context_build 据此回写 state.mode
        # （观测链出口/模式面板行源过滤键）——丢键则派发管道 state 无 mode，
        # 模式面板对该任务零呈现（BUG-61）。空白视同未声明。
        mode = str(inputs.get("mode") or "").strip()
        if mode:
            _ec["mode"] = mode
        return _ec

    async def _check_parent_ownership(
        self,
        parent_agent_level: int,
        parent_task_id: str | None,
    ) -> tuple[bool, str | None]:
        """P0-3 纵深防御：校验 parent_task_id 归属，防 L2/L3 伪造他人父任务越权挂载。

        合法链：子任务只能挂在「比自己更高层级」的祖先任务下——
        - L2 的父任务须由 L1 提交（``task.submitted_by_level == 1``）；
        - L3 的父任务须由 L1/L2 提交（``task.submitted_by_level < 3``）。
        父任务层级键缺失或 ≥ 本层级 → 视为他人同级任务，拒绝（fail-closed）。

        层级真值 = 父任务 state 行的 ``task.submitted_by_level``（提交时随出生
        契约写全）；state 行缺失（父任务不存在）或 state 读面不可用同样拒绝——
        权限闸不放宽。

        L1 不受此约束（根 Agent，可提交根任务或挂在任意已存在任务下，存在性由
        ``_validate_parent_task_id`` 等后续校验保证）。

        Args:
            parent_agent_level: 调用者 Agent 层级
            parent_task_id: 客户端/框架传入的父任务 ID（可能被篡改）

        Returns:
            (ok, error_msg)：ok=False 时 error_msg 为拒绝原因
        """
        if parent_agent_level < 2 or not parent_task_id:
            return True, None
        rows = await self._read_state_rows()
        if rows is None:
            return False, (f"权限校验失败：state 聚合不可读，无法确认父任务 {parent_task_id} 的归属层级（fail-closed）")
        parent_row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == parent_task_id),
            None,
        )
        if parent_row is None:
            return False, f"父任务不存在: {parent_task_id}"
        parent_level = parent_row.get("task.submitted_by_level")
        if parent_level is None or int(parent_level) >= parent_agent_level:
            return False, (
                f"权限不足：parent_task_id={parent_task_id} 非本 Agent 层级链所有"
                f"（parent submitted_by_level={parent_level}，当前 L{parent_agent_level}），"
                "无法在他人的同级任务下挂载子任务"
            )
        return True, None

    async def _validate_parent_task_id(self, parent_agent_level: int, parent_task_id: str | None) -> bool:
        """验证 parent_task_id 参数的使用权限（存在性以 state 为唯一通道）。"""
        if parent_agent_level == 1 and parent_task_id is not None:
            # GAP-1 统一：登记型任务 = 提交者管道自持的声明（task.owned.*），
            # 执行任务 = state 聚合行（task.*）——父任务存在性读 state 单通道
            # （task = pipeline，无独立 YAML 记录可比对）。
            if not await self._parent_exists_in_state(parent_task_id):
                logger.error("[TaskSubmit] parent_task_id 不存在: %s", parent_task_id)
                return False

        # L2/L3: parent_task_id 必须已由自动注入填充
        if parent_agent_level >= 2 and parent_task_id is None:
            logger.warning(
                "[TaskSubmit] L%d Agent 无 parent_task_id（纵深防御拦截）",
                parent_agent_level,
            )
            return False

        return True

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list capability；None = 桥未就绪）。

        读取失败/未注入返回 None（fail-closed 语义由调用方决定）；
        返回 list[dict] 时行为扁平点号键（pipeline_id/task.*/lineage.*）。
        """
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if inspect.isawaitable(rows):
                rows = await rows  # type: ignore[misc]
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩提交
            logger.warning("[TaskSubmit] state 聚合读取失败，依赖校验降级: %s", exc)
            return None

    async def _inherit_project_id(self, parent_task_id: str) -> str:
        """项目归属的父链继承：读父任务 state 行的 task.parent_project_id（单跳）。

        每级任务提交时都写自己的 parent_project_id（L1 显式 / 子级继承），
        单跳读父行即得全链归属；父行无键（独立任务链）返回空。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return ""
        for row in rows:
            if str(row.get("pipeline_id") or "") != parent_task_id:
                continue
            return str(row.get("task.parent_project_id") or "")
        return ""

    async def _resolve_short_input_ids(self, inputs: dict[str, Any]) -> None:
        """LLM 入参短 id → 全 id（state 聚合前缀唯一解析，就地改写 inputs）。

        容器 project id（task.owned.<id>，生成即短）与普通任务 id（pipeline_id
        前 12 位）都可被 LLM 以短形式回传；精确命中原样，多命中/无命中保持原样
        让既有校验路径报错。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return
        for key in ("parent_task_id", "task_id", "pipeline_id", "dependencies"):
            if key == "dependencies":
                if isinstance(inputs.get(key), list):
                    resolved: list[str] = []
                    for d in inputs[key]:
                        if isinstance(d, str):
                            r = await _resolve_short_id(rows, d)
                            if not r.startswith("AMBIGUOUS:"):
                                resolved.append(r)
                            else:
                                resolved.append(d)
                        else:
                            resolved.append(d)
                    inputs[key] = resolved
                continue
            val = inputs.get(key)
            if not isinstance(val, str) or not val:
                continue
            r = await _resolve_short_id(rows, val)
            if not r.startswith("AMBIGUOUS:"):
                inputs[key] = r

    async def _parent_exists_in_state(self, parent_task_id: str) -> bool:
        """父任务存在性 state 兜底：容器任务（task.owned.*）或执行管道（task.* 行）。"""
        rows = await self._read_state_rows()
        if rows is None:
            return False
        for row in rows:
            if str(row.get("pipeline_id") or "") == parent_task_id:
                return True
            if any(str(k).startswith(f"task.owned.{parent_task_id}.") for k in row):
                return True
        return False

    async def _check_dependencies_exist(self, dependencies: list[str]) -> list[str]:
        """检查依赖任务是否存在。"""

        rows = await self._read_state_rows()
        if rows is None:
            logger.warning(
                "[TaskSubmit] state 聚合不可用，依赖校验 fail-closed | dependencies=%s",
                dependencies,
            )
            return list(dependencies)
        known = {str(r.get("pipeline_id") or "") for r in rows}
        return [d for d in dependencies if d not in known]

    async def _validate_target_agent(
        self,
        target_id: str,
        parent_agent_level: int,
    ) -> tuple[bool, str, str]:
        """校验目标 Agent 是否存在且级别匹配。"""
        agent_level_str = ""
        agent_level = 0
        # 目标 Agent 是否启用（is_active）。两条查找路径都要拿到它，
        # 用于在级别校验后统一拦截派发给已禁用 Agent 的任务。
        is_active = True

        agent_config, unavailable = await self._lookup_agent_config(target_id)
        if unavailable:
            return (
                False,
                f"无法校验目标 Agent '{target_id}'：{unavailable}。请稍后重试。",
                "AGENT_CONFIG_UNAVAILABLE",
            )
        if agent_config is None:
            return (
                False,
                f"目标 Agent '{target_id}' 不存在。"
                f"请检查 target_id 是否正确。如果系统提供了 Agent 映射表，请使用映射表中的 Agent ID。",
                "TARGET_AGENT_NOT_FOUND",
            )
        level_value = safe_enum_value(str(agent_config.get("level", "")))
        level_map = {"L1": 1, "L2": 2, "L3": 3}
        agent_level = level_map.get(level_value, 0)
        agent_level_str = level_value
        is_active = bool(agent_config.get("is_active", True))

        # level 缺失/非法 → 0 会让下方两道层级闸门全部短路（0≠1 且不>0），
        # 必须显式拒绝而非静默放行
        if agent_level == 0:
            return (
                False,
                f"Agent level 缺失/非法（agent='{target_id}'，level={agent_level_str!r}）。"
                f"层级闸门无法判定，拒绝派发——请修复该 Agent 的 level 配置（合法值：L1/L2/L3）。",
                "TARGET_AGENT_LEVEL_MISSING",
            )

        if agent_level == 1:
            return (
                False,
                f"不能将任务提交给 L1 Agent（'{target_id}'）。"
                f"L1 是主调度层，只负责接收用户请求和派发任务，不执行具体工作。"
                f"请选择 L2 编排层或 L3 执行层的 Agent。",
                "TARGET_AGENT_IS_L1",
            )

        if agent_level > 0 and agent_level <= parent_agent_level:
            return (
                False,
                f"目标 Agent '{target_id}' 的级别为 {agent_level_str}，"
                f"不能作为 L{parent_agent_level} Agent 的下级执行者。"
                f"任务委托应向下流动：L1→L2→L3，请选择级别更低（L{parent_agent_level + 1}+）的 Agent。",
                "TARGET_AGENT_LEVEL_INVALID",
            )

        if not is_active:
            return (
                False,
                f"目标 Agent '{target_id}' 已禁用（is_active=false），不能作为任务执行者。"
                f"请使用映射表中已启用的 Agent ID。",
                "TARGET_AGENT_INACTIVE",
            )

        return (True, "", "")
