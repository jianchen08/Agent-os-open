"""评估执行器（0.2 生产版）：按指标类型真实执行评估。

- **tool 型指标**（evaluator_type=tool，如 file_check）：本地执行——按指标
  input_schema 语义（exists/not_empty/contains/is_directory），相对路径以
  任务 workspace 为根解析（_get_input_params 已注入 workspace）。
- **agent 型指标**（evaluator_type=agent，如 semantic_check）：经 chat
  capability 派发**评估子管道**（evaluator_agent）——出生 state 带
  被评估任务的 workspace/ws_meta（workspace_lifecycle 幂等跳过 → 评估者在
  被评估任务的工作区里跑，执行环境一致）；lineage 有父（挂被评估任务下）；
  plugin_configs.task_reminder.evaluation_mode=true（评估者模式：催
  evaluation_result JSON，不催 task_evaluate，防递归）。结论回收：轮询
  pipeline-state（同源白名单）evaluation.detected_result（task_reminder
  评估模式写入）+ task.status 终态，超时诚实失败。

方案：docs/working/管道工作区关联与评估管道装配方案_20260824.md 批次B；
出口契约：docs/decisions/2026-08-24-eval-pipeline-state-keys.md。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _eval_core import EvaluationResult, MetricResult

# plugins/shared 平铺契约模块（state_fields.py：ws_meta 跨边界 JSON 字符串还原）——
# 与 task_submit/tool.py 同款自举注入（server.py 已注入，此处幂等兜底直跑场景）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _libs.evaluation_metrics_path import probe_metrics_config_path  # noqa: E402
from _root_api import state_fields  # noqa: E402

logger = logging.getLogger(__name__)

# 注入约定（duck-typing，server.py on_load 装配）：
#   chat_send: async (params: dict) -> dict          —— chat.send_message 能力
#   state_rows: async () -> list[dict]                —— pipeline-state.list 能力
ChatSend = Callable[[dict[str, Any]], Any]
StateRows = Callable[[], Any]

# agent 型指标回收轮询间隔与内部回收上限（调用方 _get_eval_timeout 另有
# asyncio.wait_for 包裹，两者取先到）。语义评估 = 等整个评估子管道跑完
# （多轮 LLM，300-700s/轮），回收上限给管道级 1500s；外层默认 1800s
# （_DEFAULT_EVAL_TIMEOUT），内核面死线由 manifest mcp.request_timeout_secs=1800 承接
_POLL_INTERVAL_S = 2.0
_AGENT_RECOVER_TIMEOUT_S = 1500.0

# 评估者 agent（config/agents/system/evaluator_agent.yaml，L3 语义评估专家）
_EVALUATOR_AGENT_ID = "evaluator_agent"

# task.status 终态（失败族）：评估子管道翻车 → 指标诚实失败。
# 词汇收敛（ADR 2026-10-30）：任务域无 cancelled（退役，用户停止 = stopped），
# 停止/暂停的评估子管道不再有结论产出 → 同属失败族。
_FAILED_STATUSES = {"failed", "stopped", "timeout"}


def default_metrics_path() -> str:
    """指标定义文件坐标（config/plugins/evaluation/evaluation_metrics.yaml）。

    探测算法单源 ``_libs.evaluation_metrics_path``（祖先逐级探测，首个含该
    yaml 的祖先即根；三布局单算法通吃 + 全 miss 回落向上 3 级拼相对尾），
    本侧只提供探测坐标（本插件目录）与 str 返回形态。
    """
    return probe_metrics_config_path(os.path.dirname(os.path.abspath(__file__)))


# ── L0 检查规则纯函数（ADR 2026-10-27 步骤 2；scope/结构契约的判定核）─────────

_SCHEMA_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _matches_schema_type_ok(value: Any, expected: str) -> bool:
    """字段类型校验；未声明的类型名宽容放行（观察期契约面收敛，防假阴性误报）。"""
    check = _SCHEMA_TYPE_CHECKS.get(expected)
    return True if check is None else bool(check(value))


def _collect_schema_violations(data: Any, schema: dict[str, Any]) -> list[str]:
    """按极简 schema 契约收集产物结构违规（聚合报——观察期归因需完整清单）。

    契约子集：required=必填字段存在且值非空（None/""/[]/{} 视为缺失）；
    properties.{f}.type=字段类型；properties.{f}.min_items=数组最小长度。
    """
    violations: list[str] = []
    if not isinstance(data, dict):
        return [f"产物顶层须为对象（mapping），实为 {type(data).__name__}"]
    for field in schema.get("required") or []:
        if not isinstance(field, str):
            continue
        if data.get(field) in (None, "", [], {}):
            violations.append(f"必填字段缺失或为空: {field}")
    props = schema.get("properties")
    if isinstance(props, dict):
        for field, rule in props.items():
            if field not in data or not isinstance(rule, dict):
                continue
            value = data[field]
            expected = str(rule.get("type") or "")
            if expected and not _matches_schema_type_ok(value, expected):
                violations.append(
                    f"字段 {field} 类型应为 {expected}，实为 {type(value).__name__}"
                )
                continue
            if "min_items" in rule and isinstance(value, list):
                min_items = int(rule["min_items"])
                if len(value) < min_items:
                    violations.append(
                        f"字段 {field} 长度 {len(value)} < min_items {min_items}"
                    )
    return violations


def _parse_porcelain_paths(stdout: str) -> list[str]:
    """解析 git status --porcelain -uall 输出的改动路径清单（/ 分隔）。

    每行 "XY path"（XY 后一个空格，路径自第 4 列起，含空格路径原样保留）；
    重命名行 "XY orig -> new" 取新路径。特殊字符路径 git 会加引号（C-quoted），
    观察期按未加引号假设（任务工作区由系统生成，路径面常规）。
    """
    paths: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.replace("\\", "/"))
    return paths


def load_metric_definitions(config_path: str | None = None) -> dict[str, dict[str, Any]]:
    """加载指标定义（name → 定义），单源 = evaluation_metrics.yaml。

    缺文件/坏格式 → 空表（fail-open，调用方按未定义降级）。模块级函数供
    tool.py（配置判定）与本执行器共用同一份读取逻辑。
    """
    path = config_path or default_metrics_path()
    table: dict[str, dict[str, Any]] = {}
    try:
        import yaml  # noqa: PLC0415

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for m in data.get("metrics", []) or []:
            if isinstance(m, dict) and m.get("name"):
                table[str(m["name"])] = m
    except Exception as exc:  # noqa: BLE001 — 指标面不可用不炸评估调用
        logger.warning(
            "[EvalExecutor] 指标配置加载失败 | path=%s err=%s",
            path,
            exc,
        )
    return table


class PipelineEvaluationExecutor:
    """0.2 评估执行器：tool 型本地跑，agent 型派评估子管道（R2 工作区继承）。"""

    def __init__(
        self,
        chat_send: ChatSend,
        state_rows: StateRows,
        metrics_config_path: str | None = None,
    ) -> None:
        self._chat_send = chat_send
        self._state_rows = state_rows
        self._metrics_config_path = metrics_config_path or self._default_metrics_path()
        # mtime 校验读穿缓存（mtime → 指标表）：执行器常驻 sidecar，装载一次
        # 的进程内缓存会让指标 yaml 编辑永不生效（respawn 才可见）。
        self._metrics_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

    @staticmethod
    def _default_metrics_path() -> str:
        return default_metrics_path()

    # ── 指标定义加载 ───────────────────────────────────────────

    def _load_metrics(self) -> dict[str, dict[str, Any]]:
        """加载指标定义（name → 定义），mtime 校验读穿缓存（config_cache 先例）。

        缺文件/坏格式 → 空表（指标按未定义诚实失败）。mtime 探测失败（文件
        缺失/不可达）按缓存失效处理——重读走 load_metric_definitions 自身
        降级，返回空表并缓存；文件恢复后 mtime 变化即重载。
        """
        try:
            mtime = os.stat(self._metrics_config_path).st_mtime
        except OSError:
            mtime = -1.0
        if self._metrics_cache is None or self._metrics_cache[0] != mtime:
            self._metrics_cache = (mtime, load_metric_definitions(self._metrics_config_path))
        return self._metrics_cache[1]

    # ── 执行入口（对齐 _eval_core 契约）─────────────────────────

    async def run_evaluation(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        input_params: dict[str, dict[str, Any]] | None = None,
        fail_fast: bool = True,  # noqa: ARG002 — 逐指标独立回收，语义兼容保留
        skip_state_update: bool = False,  # noqa: ARG002 — 状态写面在工具层（writer），此处不写
    ) -> EvaluationResult:
        result = EvaluationResult(task_id=task_id)
        params = input_params or {}
        for mid in metric_ids or []:
            try:
                result.results.append(await self._evaluate_metric(task_id, mid, params.get(mid) or {}))
            except Exception as exc:  # noqa: BLE001 — 单指标炸不连坐
                logger.exception("[EvalExecutor] 指标评估异常 | task=%s metric=%s", task_id, mid)
                result.results.append(
                    MetricResult(metric_id=mid, passed=False, error=f"评估异常: {exc}")
                )
        result.compute_overall()
        return result

    async def _evaluate_metric(
        self,
        task_id: str,
        metric_id: str,
        params: dict[str, Any],
    ) -> MetricResult:
        metrics = self._load_metrics()
        definition = metrics.get(metric_id)
        if definition is None and "::" in metric_id:
            # 白话展开多实例键（file_check::<路径>，task_submit 生成）：首段即定义 ID，
            # 实例区分度由键尾段与参数自带坐标承载
            definition = metrics.get(metric_id.split("::", 1)[0])
        if definition is None:
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"指标 {metric_id} 未在 evaluation_metrics.yaml 定义（诚实失败，不猜测语义）",
            )
        evaluator_type = str(definition.get("evaluator_type") or "tool")
        if evaluator_type == "agent":
            return await self._evaluate_via_pipeline(task_id, metric_id, params)
        return self._evaluate_tool_local(metric_id, params)

    # ── tool 型：本地执行 ──────────────────────────────────────

    @staticmethod
    def _evaluate_tool_local(metric_id: str, params: dict[str, Any]) -> MetricResult:
        """tool 型本地执行：file_check 族（exists/not_empty/contains/is_directory/
        matches_schema，L0 结构契约）+ bash_check（command 在 workspace cwd 真执行，
        退出码 0=通过）+ scope 一致性（改动文件清单 ⊆ 改动面绑定表）。

        路由约定与 bash_check 同构：metric_id 显式指定或 params 携带特征键即入
        对应分支，分支内缺参诚实失败（见各 _run_* docstring）。"""
        command = str(params.get("command") or "")
        if metric_id == "bash_check" or "command" in params:
            if not command:
                return MetricResult(
                    metric_id=metric_id, passed=False, error="command 参数缺失",
                )
            return PipelineEvaluationExecutor._run_bash_check(metric_id, command, params)
        if metric_id == "scope_consistency" or "allowed_paths" in params:
            return PipelineEvaluationExecutor._run_scope_check(metric_id, params)
        raw_path = str(params.get("path") or "")
        if not raw_path:
            return MetricResult(metric_id=metric_id, passed=False, error="path 参数缺失")
        workspace = str(params.get("workspace") or "")
        p = Path(raw_path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        check = str(params.get("check") or "exists")

        if check == "exists":
            ok = p.exists()
            msg = f"存在: {p}" if ok else f"不存在: {p}"
        elif check == "is_directory":
            ok = p.is_dir()
            msg = f"目录存在: {p}" if ok else f"不是目录或不存在: {p}"
        elif check == "not_empty":
            min_size = int(params.get("min_size") or 1)
            ok = p.is_file() and p.stat().st_size >= min_size
            msg = f"非空（≥{min_size}B）: {p}" if ok else f"为空/过小/不存在: {p}"
        elif check == "contains":
            pattern = str(params.get("pattern") or "")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return MetricResult(metric_id=metric_id, passed=False, error=f"读取失败: {exc}")
            ok = bool(pattern) and pattern in text
            msg = f"包含模式: {p}" if ok else f"未包含模式 {pattern!r}: {p}"
        elif check == "matches_schema":
            return PipelineEvaluationExecutor._run_matches_schema_check(metric_id, params)
        else:
            return MetricResult(metric_id=metric_id, passed=False, error=f"未知检查类型: {check}")

        return MetricResult(metric_id=metric_id, passed=ok, message=msg)

    @staticmethod
    def _run_bash_check(metric_id: str, command: str,
                        params: dict[str, Any]) -> MetricResult:
        """bash_check：命令在任务工作区 cwd 经 bash 真执行，退出码 0=通过。

        宿主侧执行（评估执行器位于 light 组 sidecar，工作区目录直接可达）；
        超时默认 600s；输出尾部随 message 留痕供归因。"""
        import subprocess

        workspace = str(params.get("workspace") or "")
        timeout = int(params.get("timeout_seconds") or 600)
        try:
            proc = subprocess.run(
                ["bash", "-c", command],
                cwd=workspace or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            return MetricResult(
                metric_id=metric_id, passed=False,
                error="bash 不可用（执行器宿主未找到 bash 可执行文件）",
            )
        except subprocess.TimeoutExpired:
            return MetricResult(
                metric_id=metric_id, passed=False,
                error=f"命令超时（>{timeout}s）",
            )
        ok = proc.returncode == 0
        tail = ((proc.stdout or "")[-600:]
                + (f"\n[stderr] {(proc.stderr or '')[-300:]}" if proc.stderr else ""))
        message = f"exit={proc.returncode}" + (f"\n{tail.strip()}" if tail.strip() else "")
        return MetricResult(metric_id=metric_id, passed=ok, message=message)

    @staticmethod
    def _run_matches_schema_check(metric_id: str, params: dict[str, Any]) -> MetricResult:
        """matches_schema：产物 YAML/JSON 结构校验（artifact_schema_check 执行载体）。

        schema 契约子集见 _collect_schema_violations；违规聚合进 message——
        观察期误报率归因依赖完整违规清单，不逐轮往返。"""
        import yaml  # noqa: PLC0415 — 与 load_metric_definitions 同一依赖面

        raw_path = str(params.get("path") or "")
        if not raw_path:
            return MetricResult(metric_id=metric_id, passed=False, error="path 参数缺失")
        workspace = str(params.get("workspace") or "")
        p = Path(raw_path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        if not p.is_file():
            return MetricResult(metric_id=metric_id, passed=False, error=f"产物文件不存在: {p}")
        try:
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            return MetricResult(metric_id=metric_id, passed=False, error=f"产物解析失败: {exc}")
        schema = params.get("schema")
        if not isinstance(schema, dict):
            return MetricResult(metric_id=metric_id, passed=False, error="schema 参数缺失（须为对象）")
        violations = _collect_schema_violations(data, schema)
        if violations:
            return MetricResult(
                metric_id=metric_id, passed=False,
                message="结构不合规: " + "; ".join(violations),
            )
        return MetricResult(metric_id=metric_id, passed=True, message=f"结构合规: {p}")

    @staticmethod
    def _run_scope_check(metric_id: str, params: dict[str, Any]) -> MetricResult:
        """scope 一致性：改动文件清单 ⊆ 改动面绑定表（scope_consistency 执行载体）。

        git status --porcelain -uall 取任务工作区改动全集（含未跟踪），对比
        params.allowed_paths（文件精确或目录前缀匹配，/ 分隔相对路径）；越界
        清单进 message。workspace = git 仓库/worktree 根（任务默认隔离语义下
        成立）。allowed_paths 缺失 = 无授权面，诚实失败不放行。"""
        import subprocess

        workspace = str(params.get("workspace") or "")
        allowed_raw = params.get("allowed_paths")
        if not isinstance(allowed_raw, list) or not allowed_raw:
            return MetricResult(
                metric_id=metric_id, passed=False,
                error="allowed_paths 参数缺失（改动面绑定表为空 = 无改动授权，不放行）",
            )
        if not workspace:
            return MetricResult(metric_id=metric_id, passed=False, error="workspace 参数缺失")
        try:
            proc = subprocess.run(
                ["git", "-C", workspace, "status", "--porcelain", "-uall"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except FileNotFoundError:
            return MetricResult(
                metric_id=metric_id, passed=False,
                error="git 不可用（执行器宿主未找到 git 可执行文件）",
            )
        except subprocess.TimeoutExpired:
            return MetricResult(metric_id=metric_id, passed=False, error="git status 超时（>60s）")
        if proc.returncode != 0:
            return MetricResult(
                metric_id=metric_id, passed=False,
                error=f"git status 失败（非 git 仓库或仓库损坏）: {(proc.stderr or '')[-200:]}",
            )
        changed = _parse_porcelain_paths(proc.stdout or "")
        normalized_allowed = [
            str(a).replace("\\", "/").rstrip("/")
            for a in allowed_raw
            if str(a).strip()
        ]

        def _allowed(path: str) -> bool:
            return any(path == a or path.startswith(a + "/") for a in normalized_allowed)

        outside = sorted(p for p in changed if not _allowed(p))
        if outside:
            return MetricResult(
                metric_id=metric_id, passed=False,
                message="越界改动文件: " + ", ".join(outside[:20]),
            )
        return MetricResult(
            metric_id=metric_id, passed=True,
            message=f"改动 {len(changed)} 个文件全部在改动面绑定表内",
        )

    # ── agent 型：评估子管道（R2 工作区/执行环境继承）───────────

    async def _evaluate_via_pipeline(
        self,
        task_id: str,
        metric_id: str,
        params: dict[str, Any],
    ) -> MetricResult:
        task_row = await self._find_state_row(task_id) or {}
        # 工作区坐标单键读取（state 单一真相源）：task.ws_meta——task_submit
        # 出生预写（显式 plain）+ workspace_lifecycle init 创建即写，出生/创建
        # 时一次写全。state_fields 还原跨边界 JSON 字符串形态；缺失诚实失败。
        ws_meta = state_fields.as_dict(task_row.get("task.ws_meta"), field="task.ws_meta")
        if not isinstance(ws_meta, dict) or not ws_meta.get("path"):
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"任务 {task_id[:8]} 无工作区坐标（ws_meta.path），无法继承执行环境",
            )
        origin_session = str(
            (task_row or {}).get("lineage.origin_session_id") or task_id
        )
        # 派发身份 = 被评估任务的真实提交者（task_submit 出生写面落账、tasks
        # 插件 export_fields 出口）：内核 chat.send_message 创建分支按 user_id
        # 反查用户→租户（K8 fail-closed，ADR 2026-10-10），合成账号在用户表
        # 查无此人必被拒——「用户不存在（token 持有者已删除）」=
        # TenantResolveError::NotFound（R2 移动端 semantic_check 2/2 复现，
        # file_check 本地检查不派发故不受影响）。缺提交者（存量行）保留历史
        # 兜底身份，行为不回退。
        submitter = str(task_row.get("task.submitted_by") or "")
        # 会话归属（task_submit 出生契约同款）：被评估任务行的 thread_id = 其
        # 归属会话（pipeline_sessions 真映射）——评估子管道挂同一会话，前端
        # 管道树可见可定位；缺会话（自环根任务）不传，保持 task_birth 缺省语义。
        ownership_thread = str(task_row.get("thread_id") or "")
        if not ownership_thread or ownership_thread == task_id:
            ownership_thread = ""

        kickoff = (
            f"评估任务 {task_id[:8]} 的指标 {metric_id}。\n"
            f"评估参数：{json.dumps(params, ensure_ascii=False, default=str)}\n"
            "请在工作区内核对任务产出物后给出结论，输出：\n"
            '```json\n{"evaluation_result": {"passed": true/false, "score": 0-100, '
            '"feedback": "评估说明..."}}\n```'
        )
        # 执行环境共享（task_submit 子任务同款 execution_context）：工作区声明
        # 指向被评估任务工作区（workspace_lifecycle 见 state.workspace 已就位
        # 幂等跳过创建，同目录跑）；隔离声明按任务默认隔离执行（isolation_guard/
        # environment_lifecycle 的容器决策消费键）。
        execution_context: dict[str, Any] = {
            "workspace": {
                "source_path": str(ws_meta["path"]),
                "mode": str(ws_meta.get("mode") or "plain"),
                "explicit": True,
            },
            "isolation": {"level": "isolated"},
        }
        dispatch: dict[str, Any] = {
            "create": True,
            "message": kickoff,
            **({"user_id": submitter} if submitter else {}),
            "agent_id": _EVALUATOR_AGENT_ID,
            "execution_context": execution_context,
            "state": {
                # 执行身份（task_birth.py:95 出生协议同款）：agent_id 只进派发
                # 簿记不进 run 身份，缺此键 context_build 会静默回退默认主 agent
                # （L1）→ task_reminder 的 L1 早退门短路 → evaluation.detected_result
                # 永不落库 → 本工具轮询超时（B12 根因②）
                "agent.id": _EVALUATOR_AGENT_ID,
                # 血缘扁平键（有父形式）：父 = 被评估任务管道
                "lineage.parent_pipeline_id": task_id,
                "lineage.origin_session_id": origin_session,
                # R2 核心：出生即带被评估任务的工作区坐标（非保留字，注入合法；
                # workspace_lifecycle 见 state.workspace 已有幂等跳过 → 同目录跑）
                "workspace": str(ws_meta["path"]),
                "ws_meta": ws_meta,
                # 防递归：评估者模式（催 evaluation_result JSON，不催 task_evaluate）
                "plugin_configs": {"task_reminder": {"evaluation_mode": True}},
                # 评估域登记（ADR 2026-08-24-eval-pipeline-state-keys 出口）
                "evaluation.of_task": task_id,
                "evaluation.metric_id": metric_id,
                "display_name": f"评估·{metric_id}",
            },
            "background": True,
        }
        if ownership_thread:
            dispatch["thread_id"] = ownership_thread

        try:
            resp = await self._chat_send(dispatch)
        except Exception as exc:  # noqa: BLE001
            return MetricResult(metric_id=metric_id, passed=False, error=f"评估子管道派发失败: {exc}")
        eval_pid = str((resp or {}).get("pipeline_id") or "") if isinstance(resp, dict) else ""
        if not eval_pid:
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"评估子管道派发未返回 pipeline_id: {resp!r:.200}",
            )
        logger.info(
            "[EvalExecutor] 评估子管道已派发 | task=%s metric=%s eval_pipe=%s ws=%s",
            task_id,
            metric_id,
            eval_pid,
            ws_meta["path"],
        )
        return await self._recover_result(eval_pid, metric_id)

    async def _recover_result(self, eval_pid: str, metric_id: str) -> MetricResult:
        """轮询回收 evaluation.detected_result / 失败终态 / 超时（诚实失败）。"""
        deadline = asyncio.get_running_loop().time() + _AGENT_RECOVER_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            row = await self._find_state_row(eval_pid)
            if row:
                detected = row.get("evaluation.detected_result")
                if isinstance(detected, dict):
                    passed = bool(detected.get("passed"))
                    score = detected.get("score")
                    return MetricResult(
                        metric_id=metric_id,
                        passed=passed,
                        score=float(score) if isinstance(score, (int, float)) else -1.0,
                        message=str(detected.get("feedback") or ""),
                        evaluator_output=detected,
                        pipeline_run_id=eval_pid,
                    )
                status = str(row.get("task.status") or "")
                if status in _FAILED_STATUSES:
                    return MetricResult(
                        metric_id=metric_id,
                        passed=False,
                        error=f"评估子管道终态 {status}",
                        pipeline_run_id=eval_pid,
                    )
            await asyncio.sleep(_POLL_INTERVAL_S)
        return MetricResult(
            metric_id=metric_id,
            passed=False,
            error=f"评估回收超时（{_AGENT_RECOVER_TIMEOUT_S:.0f}s）",
            pipeline_run_id=eval_pid,
        )

    async def _find_state_row(self, pipeline_id: str) -> dict[str, Any] | None:
        try:
            rows = self._state_rows()
            if asyncio.iscoroutine(rows):
                rows = await rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EvalExecutor] state 行读取失败 | pipeline=%s err=%s", pipeline_id, exc)
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and str(row.get("pipeline_id") or "") == pipeline_id:
                return row
        return None
