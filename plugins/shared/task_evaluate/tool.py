"""任务评估工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskEvaluateTool：任务评估工具类

0.2 迁移（FP-MIGR / F-MIGR-2）：
- 顶层类型走 agentos_plugin_sdk（BuiltinTool / Tool / ToolCategory / ToolLevel /
  ToolSource / ToolExecutionResult / create_failure_result / create_success_result）；
- 评估类型面（EvaluationResult / MetricResult / sanitize_eval_paths /
  EvaluationExecutor）就地重建于同目录 _eval_core.py；
- 任务领域类型（TaskStatus）以 plugins/shared/tasks/ 平铺模块为权威
  （server.py 注入 sys.path）；
- 服务获取面 = 模块级 _get_service_provider() shim（get 返回 None，
  调用方均有降级守卫）；
- 评估执行器由外部注入（构造参数 executor=...，duck-typing）：未注入时评估
  路径返回 EVAL_ENGINE_UNAVAILABLE，不静默空转。
"""

import asyncio
import glob
import logging
import os
import re
import subprocess
from datetime import datetime
from typing import Any, NamedTuple

import worktree_merge  # noqa: E402 — 本插件平铺模块（bootstrap 注入自身目录）
from _eval_core import sanitize_eval_paths
from _executor import load_metric_definitions
from _root_api import (
    now_iso_utc as _now_iso,  # 共享时间戳单点（task.ended_at 落 state）
    resolve_repo_root as _resolve_repo_root,
    state_fields,
)
from state_machine import InvalidTransitionError
from task_types import TaskModel, TaskStatus

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# path 类参数中的单花括号占位符段（{novel_title}/{chapter_number}/
# {chapter_title}/{agent_id} 等，执行期才确定值）；不含 `{{` 双花括号面。
_PATH_TOKEN_RE = re.compile(r"\{[a-z_][a-z0-9_]*\}")


# ── GAP-1 统一：state 聚合读取器 + 任务状态写面（server.py on_load 注入）──
# 读：``() -> list[dict]``（sync 或 async，管道 state 聚合行，行为扁平点号键
# 如 {"pipeline_id": ..., "task.status": ..., "task.goal": ...}）。None = 未注入
# （读面按缺失处理，不静默造数据）。
# 写：``(pipeline_id, fields) -> None``（async，经 pipeline-state.update 落
# state 单一真值——任务状态由任务域插件裁决写入，内核不回写 task.status）。
# None = 未注入（写面降级：调用方按返回值裁决是否落账）。
_state_reader: Any = None
_state_writer: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader


def set_state_writer(writer: Any) -> None:
    """注入任务状态写面（server.py on_load 经 pipeline-state.update capability）。"""
    global _state_writer  # noqa: PLW0603
    _state_writer = writer


def _get_state_writer() -> Any:
    """获取任务状态写面（None = 未注入，写面降级仅 YAML 镜像）。"""
    return _state_writer


# ── 默认评估执行器（server.py on_load 装配）──
# PipelineEvaluationExecutor（_executor.py）：tool 型本地跑，agent 型派评估
# 子管道继承任务工作区。构造参数 ``executor=...`` 显式注入优先（测试），缺省
# 回退本注入点；两处皆无 → EVAL_ENGINE_UNAVAILABLE（文档化降级，不静默空转）。
_default_executor: Any = None


def set_default_executor(executor: Any) -> None:
    """注入默认评估执行器（server.py on_load 装配 _executor.PipelineEvaluationExecutor）。"""
    global _default_executor  # noqa: PLW0603
    _default_executor = executor


async def _write_task_state(task_id: str, fields: dict[str, Any]) -> bool:
    """把任务域字段写入管道 state 单一真值（GAP-1：task.id == pipeline_id）。

    任务状态由任务域插件裁决写入（pipeline-state update capability），内核
    不再回写 task.status。写失败不阻断调用方主流程（仅日志），但返回值
    如实回告是否落账——持久化承重的调用点（终态回写/重试计数）必须检查。

    Returns:
        True = 已写入写面；False = 写面未注入或写入失败（未落账）
    """
    writer = _get_state_writer()
    if writer is None:
        logger.debug(
            "[TaskEvaluate] state 写面未注入，跳过 state 写入 | task=%s fields=%s",
            task_id,
            sorted(fields),
        )
        return False
    try:
        await writer(task_id, fields)
    except Exception as exc:  # noqa: BLE001 — 写面失败不阻断评估主流程
        logger.warning(
            "[TaskEvaluate] state 写入失败（评估结果仍有效）| task=%s fields=%s err=%s",
            task_id,
            sorted(fields),
            exc,
        )
        return False
    return True


async def _read_row_task_ws_meta(task_id: str) -> dict[str, Any]:
    """state 行工作空间坐标单键读取（模块级，task.ws_meta）。

    task_evaluate_func 等无 self 读面的调用点用；读面未注入/读失败/无行/
    无键一律返回空 dict（调用方按门控失败裁决，不静默造坐标）。
    """
    reader = _get_state_reader()
    if reader is None:
        return {}
    try:
        rows = reader()
        if asyncio.iscoroutine(rows):
            rows = await rows
    except Exception as exc:  # noqa: BLE001 — 读面降级按无坐标处理
        logger.warning("[TaskEvaluate] state 聚合读取失败（ws_meta 按缺失处理）: %s", exc)
        return {}
    if not isinstance(rows, list):
        return {}
    row = next(
        (r for r in rows if isinstance(r, dict) and str(r.get("pipeline_id") or "") == str(task_id)),
        None,
    )
    if row is None:
        return {}
    return state_fields.as_dict(row.get("task.ws_meta"), field="task.ws_meta") or {}


_DEFAULT_MAX_RETRIES = 3
# 语义评估 = 等评估子管道全程（多轮 LLM），管道级预算 1800s，与 manifest
# mcp.request_timeout_secs 对齐（内核面死线不得小于此值，否则先被内核掐断）
_DEFAULT_EVAL_TIMEOUT = 1800.0
_DEFAULT_MAX_EVAL_CALLS = 15

_VALID_EVALUATE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}
_VALID_AUTO_COMPLETE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}


class _ServiceProviderShim:
    """0.2 服务提供者适配：get(key) 返回 0.2 等价或 None（文档化降级）。

    0.2 sidecar
    无 agent_registry / tool_registry / execution_record_storage 等价单例 →
    统一返回 None，调用方均有降级守卫（如 _register_eval_pipelines 跳过管道
    注册）。worktree 合并门控已不在此列：0.2 改经本插件 worktree_merge 模块
    本地执行（数据源 state ws_meta），无跨进程服务获取。
    """

    def get(self, key: str) -> Any:
        return None


def _get_service_provider() -> Any:
    """服务提供者获取（未注入时 get 返回 None，调用方降级）。"""
    return _ServiceProviderShim()


# ── 完赛前置证据校验（双轨：文件面 / commit 面，完赛单漏斗）──────────────────
# 双轨判据（ADR 2026-11-11-evidence-gate-dual-track）：
# - 豁免轨：任务声明 acceptance_criteria.files（task_submit 白话字段，存储为
#   ``file_check::<路径>`` 实例键或字面 files 键）且工作空间非 worktree 模式
#   → 文件面替代 commit 面：清单取 required_files 与 acceptance files 并集
#   （都声明都查），逐一核对存在 + mtime 晚于任务创建基线，commit 面整体
#   跳过（零 git 探测）。动因：无 bash 命令工具的 agent（novel/research/
#   orchestrator 家族）执行不了 git commit，commit 面恒查 = 结构性死锁。
# - 既有轨：未声明 acceptance files，或 worktree 挂靠项目任务（交付语义 =
#   commit 后合并回项目仓库）→ 原闸不变：声明 required_files → 文件面 +
#   commit 面 AND 双闸；未声明 → 只查 commit 面（用户裁定 2026-11-04：无
#   git 记录的任务不落 completed，回执要求 agent 先提交）。
# 取证环境不完整如实降级（warning 留痕，不因环境差异误杀合法完赛）：工作
# 空间坐标不可得 / 基线缺失或不可解析 / 工作空间非 git 仓 / git 不可用或
# 超时（豁免轨只用文件面，无 git 降级形态）。
_REQUIRED_FILES_META_KEY = "required_files"
_EVIDENCE_GIT_TIMEOUT = 30  # git 取证命令超时（秒），对齐 worktree_merge._GIT_TIMEOUT
_ACCEPTANCE_FILES_FLAT_KEY = "files"
_FILE_CHECK_DEFINITION_ID = "file_check"
_FILE_CHECK_INSTANCE_SEP = "::"


def _required_files_declared(metadata: dict[str, Any] | None) -> list[str]:
    """读任务 metadata 的 required_files 声明（相对路径清单）。

    未声明/非列表/空清单 → []（无文件面要求；commit 面判据不受此声明影响，
    由完赛证据校验统一持有）。
    """
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get(_REQUIRED_FILES_META_KEY)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _resolve_task_placeholder(path: str, task_id: str) -> str:
    """验收路径 ``{{task_id}}`` → 真实任务 ID（评估实查前唯一替换通道）。

    派发时 task_id 未出生，任务书验收路径按契约写模板 ``{{task_id}}_<后缀>``
    （提交期双花括号白名单放行）；评估实查前统一替换，杜绝按字面路径验收
    （实测 1a42e85aaa47：agent 被迫落字面名文件才能过闸）。只做精确子串
    替换——``{{workspace}}``/``{{state:...}}``/单花 ``{title}`` 等其他占位符
    原样保留，不与值面替换（_inject_workspace_and_placeholders）混流。
    """
    return path.replace("{{task_id}}", str(task_id))


def _acceptance_files_declared(acceptance_criteria: Any) -> list[str]:
    """读任务 acceptance_criteria 的 files 声明（相对路径清单，豁免轨触发器）。

    双形态同源（同一 files 字段信号的存储两面，task_submit 白话展开契约）：
    - 展开形态（主管道实际存储）：files 展开为 ``file_check::<路径>`` 实例键，
      实例后缀即声明路径；
    - 原始形态（直构 metadata 的任务/测试）：字面 ``files`` 键字符串数组。
    非 dict/未声明/空清单 → []（不豁免）。裸 ``file_check`` 指标键（指标名
    dict 形态提交，无实例后缀）不属 files 字段信号，不计入——豁免触发只认
    显式 files 声明，不做启发式扩面（ADR 2026-11-11 被否方案 3）。
    """
    if not isinstance(acceptance_criteria, dict):
        return []
    declared: list[str] = []
    raw = acceptance_criteria.get(_ACCEPTANCE_FILES_FLAT_KEY)
    if isinstance(raw, list):
        declared.extend(str(item) for item in raw)
    for key in acceptance_criteria:
        if not isinstance(key, str) or _FILE_CHECK_INSTANCE_SEP not in key:
            continue
        definition_id, _, instance_path = key.partition(_FILE_CHECK_INSTANCE_SEP)
        if definition_id == _FILE_CHECK_DEFINITION_ID and instance_path.strip():
            declared.append(instance_path)
    return declared


def _is_worktree_workspace(ws_meta: dict[str, Any]) -> bool:
    """worktree 边界判定（豁免轨硬边界）：mode == "worktree" 不豁免 commit 面。

    假设：state 无独立 workspace_mode 键——工作空间模式单源在
    ``task.ws_meta.mode``（出生写面 workspace_lifecycle，取值
    plain/shared/worktree），本判定与合并门控对同一字段的读法一致
    （mode != "worktree" 即不合并）。mode 键缺失按非 worktree（独立任务）
    保守代理——与设计裁定的「project_id 缺省=独立任务」同口径。
    升级触发：worktree 判定键位/取值改名，或新增第四种挂靠项目工作空间模式
    时，本判定与合并门控须同刀更新。
    """
    return str(ws_meta.get("mode") or "") == "worktree"


def _parse_created_at_epoch(created_at: Any) -> float | None:
    """任务创建基线解析（ISO 文本 → epoch 秒，两判定面共用）。

    空文本/不可解析 → None（调用方按基线缺失降级，不用猜测基线拒任务）。
    naive 文本按本地时区换算——与文件 st_mtime 同机同源，统一 epoch 对比。
    """
    text = str(created_at or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.timestamp()


def _collect_required_files_violations(
    workspace: str,
    required_files: list[str],
    baseline: float,
) -> list[str]:
    """文件面逐项核对，返回违规明细行（空 = 文件证据齐备）。

    每个声明项判定：存在性（isfile）+ 新鲜度（st_mtime 严格晚于基线，恰等
    按过期计）。解析后越出工作空间的声明项按缺失计（回执注明，fail-closed）。
    """
    violations: list[str] = []
    ws_abs = os.path.abspath(workspace)
    baseline_text = datetime.fromtimestamp(baseline).isoformat(timespec="seconds")
    for rel in required_files:
        rel_text = rel.strip()
        if not rel_text:
            violations.append("- <空路径>: required_files 含空白条目")
            continue
        full = os.path.abspath(os.path.join(ws_abs, rel_text))
        if full != ws_abs and not full.startswith(ws_abs + os.sep):
            violations.append(f"- {rel_text}: 路径越出工作空间，按缺失计")
            continue
        if not os.path.isfile(full):
            violations.append(f"- {rel_text}: 文件不存在")
            continue
        mtime = os.path.getmtime(full)
        if mtime <= baseline:
            mtime_text = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            violations.append(f"- {rel_text}: 过期（修改时间 {mtime_text} 未晚于任务创建基线 {baseline_text}）")
    return violations


class _CommitEvidence(NamedTuple):
    """commit 面取证结果（单次探测，文件面/commit 面共享）。

    degrade_reason 为空 = 取证可用（has_new_commit 可信）；非空 = 取证环境
    不可用（非 git 仓 / git 不可用或超时），调用方放行并如实留痕。
    """

    has_new_commit: bool
    degrade_reason: str


def _git_failure_reason(exc: BaseException) -> str:
    """git 进程边界异常 → 降级原因串（超时/未安装/其他 OS 错误，如实留痕）。"""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"超时（{exc.__class__.__name__}）"
    if isinstance(exc, FileNotFoundError):
        return "git 不可用（FileNotFoundError：未安装或不可执行）"
    return f"git 不可用（{exc.__class__.__name__}）"


def _path_key(path: str) -> str:
    """路径比较键（realpath + normcase：Windows 盘符/大小写/短路径差异归一）。"""
    return os.path.normcase(os.path.realpath(path))


def _git_stdout(args: list[str], workspace: str) -> subprocess.CompletedProcess[str]:
    """跑一条只读取证 git 命令（统一超时/解码口径，结果交调用方判定）。"""
    return subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_EVIDENCE_GIT_TIMEOUT,
        check=False,
    )


def _probe_commit_evidence(workspace: str, since_text: str) -> _CommitEvidence:
    """commit 面取证（同步，经线程池调用）：仓库可用性 + 基线后新 commit。

    ``git rev-parse --is-inside-work-tree`` 判仓（非 git 仓 → 不可用 + 原因）；
    ``git log --since=<基线> --oneline -- .``（限本工作空间子树）非空判新
    commit。仓已判活后 log 退码非零（空仓无提交）按无新 commit 计——证据
    不成立，补 commit 即自愈，不属取证环境差异；git 进程异常（未安装/超时等）
    按不可用返回原因串（调用方放行并如实留痕，不因取证环境差异阻塞任务）。

    归属性判定（ADR 2026-11-04-l0-gate-scope-and-eval-git-evidence 决策 3：
    只检查「可归属的隔离/worktree 工作空间」，不检查非 git 工作空间与多会话
    共享仓根）——两类不可归属形态一律降级跳过并在日志/回执面留痕：
    - 工作空间是某仓根的子目录（dev ``.ai_workspaces/<task_id>`` 形态）：
      ``rev-parse --show-toplevel`` ≠ 工作空间本身；该仓的提交来自多任务/
      多会话，归不到单个任务；
    - 工作空间恰是仓库锚根（用户/多会话共享的主仓）：提交无法区分归属。
    """
    try:
        probe = _git_stdout(["rev-parse", "--is-inside-work-tree"], workspace)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return _CommitEvidence(has_new_commit=False, degrade_reason=f"git 取证失败：{_git_failure_reason(exc)}")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        stderr_lines = (probe.stderr or "").strip().splitlines()
        detail = f"：{stderr_lines[-1]}" if stderr_lines else ""
        return _CommitEvidence(
            has_new_commit=False,
            degrade_reason=f"工作空间非 git 仓（rev-parse rc={probe.returncode}{detail}）",
        )
    try:
        top = _git_stdout(["rev-parse", "--show-toplevel"], workspace)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return _CommitEvidence(has_new_commit=False, degrade_reason=f"git 取证失败：{_git_failure_reason(exc)}")
    toplevel = (top.stdout or "").strip()
    if top.returncode != 0 or not toplevel:
        return _CommitEvidence(
            has_new_commit=False,
            degrade_reason=f"git 取证失败：rev-parse --show-toplevel 无结果（rc={top.returncode}）",
        )
    if _path_key(toplevel) != _path_key(workspace):
        return _CommitEvidence(
            has_new_commit=False,
            degrade_reason=(
                f"工作空间为共享仓根子目录（toplevel={toplevel}），commit 不可归属到单个任务"
                "（ADR 2026-11-04 决策 3）"
            ),
        )
    anchor = _resolve_repo_root()
    if anchor is not None and _path_key(str(anchor)) == _path_key(workspace):
        return _CommitEvidence(
            has_new_commit=False,
            degrade_reason=(
                "工作空间为多会话共享仓根，commit 不可归属到单个任务（ADR 2026-11-04 决策 3）"
            ),
        )
    try:
        log = _git_stdout(["log", f"--since={since_text}", "--oneline", "--", "."], workspace)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return _CommitEvidence(has_new_commit=False, degrade_reason=f"git log 取证失败：{_git_failure_reason(exc)}")
    return _CommitEvidence(
        has_new_commit=log.returncode == 0 and bool(log.stdout.strip()),
        degrade_reason="",
    )



def _build_required_files_receipt(task_id: str, violations: list[str], baseline_text: str) -> str:
    """评估拒绝回执（required_files 双闸证据不足）——文案单点产出。"""
    lines = [
        f"评估不通过：任务 {task_id} 交付证据不足，先补齐以下证据再完赛：",
        *violations,
        f"判定基线：文件须存在于工作空间且修改时间晚于任务创建时间（{baseline_text}），"
        "且工作空间存在该基线之后的新 commit。",
    ]
    return "\n".join(lines)


def _build_acceptance_files_receipt(task_id: str, violations: list[str], baseline_text: str) -> str:
    """评估拒绝回执（豁免轨文件面证据不足）——文案单点产出。

    回执只给文件面补救路径，不得出现「先 git 提交/commit」类指令：豁免轨的
    执行者多为无 bash 命令工具的 agent，提交指令对其不可执行（结构性死锁的
    回执面教训，ADR 2026-11-11-evidence-gate-dual-track）。
    """
    lines = [
        f"评估不通过：任务 {task_id} 交付证据不足，先补齐以下证据再完赛：",
        *violations,
        f"判定基线：本任务按验收文件证据完赛（文件面即交付证据，无需版本库提交），"
        f"交付文件须存在于工作空间且修改时间晚于任务创建时间（{baseline_text}）。",
    ]
    return "\n".join(lines)


def _build_commit_evidence_receipt(task_id: str, baseline_text: str) -> str:
    """评估拒绝回执（未声明 required_files 的 commit 面证据不足）——文案单点产出。"""
    return "\n".join(
        [
            f"评估不通过：任务 {task_id} 交付未落库，commit 后再完赛：",
            "- 请先 git 提交：工作空间在任务创建基线之后没有新 commit"
            "（未提交的改动不算交付证据）",
            f"判定基线：工作空间须存在晚于任务创建时间（{baseline_text}）的新 commit。",
        ]
    )


def _reject_incomplete_delivery(
    task_id: str, receipt: str, *, metric_id: str, detail: str
) -> ToolExecutionResult:
    """交付证据不足拒绝结果（result=retry，任务不落 completed）——形态单点产出。"""
    logger.info(
        "[TaskEvaluate] 完赛交付证据不足，拒绝完赛 | task=%s | 面=%s | %s",
        task_id,
        metric_id,
        detail,
    )
    return create_success_result(
        data={
            "task_id": task_id,
            "overall_passed": False,
            "summary": receipt,
            "metrics": [{"metric_id": metric_id, "passed": False, "message": receipt}],
        },
        metadata={
            "action": "auto_complete",
            "result": "retry",
            "message": receipt,
        },
    )


async def task_evaluate_func(inputs: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911
    """同步任务评估函数（仅测试/兼容入口）。

    自成一条完成路径：只走 worktree 合并门控，**不经过完赛证据闸**
    （required_files 文件面 / commit 面）。生产完赛路径 = task_evaluate 工具
    （TaskEvaluateTool._complete_task → _check_completion_evidence）。

    Args:
        inputs: 包含 action 和 task_id 的字典

    Returns:
        评估结果字典
    """
    from task_types import TaskStatus  # noqa: PLC0415

    action = inputs.get("action")
    task_id = inputs.get("task_id")

    if not action:
        return {"success": False, "error_code": "MISSING_ACTION", "error": "缺少 action 参数"}

    if not task_id:
        return {"success": False, "error_code": "MISSING_TASK_ID", "error": "缺少 task_id 参数"}

    if action not in ("evaluate_single", "auto_complete"):
        return {"success": False, "error_code": "INVALID_ACTION", "error": f"不支持的操作: {action}"}

    try:
        from service_access import get_task_service  # noqa: PLC0415

        task_service = get_task_service()
        if task_service is None:
            return {"success": False, "error_code": "SERVICE_UNAVAILABLE", "error": "TaskService 不可用"}
    except Exception:
        return {"success": False, "error_code": "SERVICE_UNAVAILABLE", "error": "TaskService 不可用"}

    task = task_service.get_task(task_id)
    if task is None:
        return {"success": False, "error_code": "TASK_NOT_FOUND", "error": "任务不存在"}

    valid_statuses = _VALID_EVALUATE_STATUSES if action == "evaluate_single" else _VALID_AUTO_COMPLETE_STATUSES

    if task.status == TaskStatus.RUNNING:
        # 评估/补落链建立在状态一致前提上：迁移失败或 evaluating 过渡态未落账
        # 都必须显式报错返回，不得吞掉后继续（库面/state 面漂移要可见）。
        try:
            await task_service.move_to_evaluating(task_id)
        except (KeyError, InvalidTransitionError) as exc:
            return {
                "success": False,
                "error_code": "EVAL_TRANSITION_FAILED",
                "error": f"评估前置状态迁移失败（running→evaluating），补落链不执行: {exc}",
            }
        if not await _write_task_state(task_id, {"task.status": "evaluating"}):
            return {
                "success": False,
                "error_code": "EVAL_STATE_WRITE_FAILED",
                "error": "evaluating 过渡态写 state 失败（任务未落账 evaluating），补落链不执行",
            }

    if task.status not in valid_statuses and task.status != TaskStatus.RUNNING:
        return {"success": False, "error_code": "INVALID_STATUS", "error": f"不支持的状态: {task.status}"}

    try:
        # worktree 合并门控（2026-09-04 裁定）：completed 前必须合并成功。
        # ws_meta 数据源 = state 行单键 task.ws_meta（出生/创建时一次写全）；
        # 合并失败 = 任务标记 failed，禁止绕门置完成（worktree 产物会留在
        # 未合并副本里静默丢失）。
        ws_meta = await _read_row_task_ws_meta(str(task_id))
        merge_error = await asyncio.to_thread(worktree_merge.merge_worktree_before_complete, task_id, ws_meta)
        if merge_error:
            # 终态写面 = pipeline-state.update 单通道（YAML 状态机写面已退役，
            # ADR 2026-10-08-eval-state-single-write）
            state_marked = await _write_task_state(
                task_id,
                {
                    "task.status": "failed",
                    "task.ended_at": _now_iso(),
                    "task.error": merge_error,
                    "task.failure_class": "merge_gate",
                    "task.failure_reason": merge_error,
                },
            )
            if state_marked:
                return {
                    "success": False,
                    "error_code": "MERGE_GATE_FAILED",
                    "error": f"评估通过但完成前工作区合并失败，任务标记为 failed: {merge_error}",
                }
            # failed 终态未落账必须可见——静默返回"已标记 failed"会把库面/
            # state 面漂移伪装成成功落账。
            return {
                "success": False,
                "error_code": "MERGE_GATE_FAILED",
                "error": (
                    f"评估通过但完成前工作区合并失败: {merge_error}；"
                    "且 failed 终态写 state 未落账（库面/state 面可能漂移），需人工核对"
                ),
            }

        persisted = await _write_task_state(
            task_id,
            {"task.status": "completed", "task.ended_at": _now_iso()},
        )
        if not persisted:
            return {
                "success": False,
                "error_code": "EVAL_STATE_WRITE_FAILED",
                "error": "完成终态写 state 失败（任务未落账 completed）",
            }
        return {"success": True, "status": "completed"}
    except Exception as e:
        return {"success": False, "error_code": "EVAL_FAILED", "error": str(e)}


class TaskEvaluateTool(BuiltinTool):
    """任务评估工具。

    负责：
    1. 解析输入参数
    2. 调用 EvaluationExecutor 执行评估
    3. 根据评估结果处理三种情况：
       - 全部通过 → 更新状态 COMPLETED + 通知提交者
       - 失败但次数未耗尽 → 返回评估结果，Agent 继续工作
       - 失败且次数耗尽 → 更新状态 FAILED + 通知提交者
    """

    def __init__(self, executor: Any = None, **kwargs: Any) -> None:
        """初始化任务评估工具。

        Args:
            executor: 评估执行器实例（0.2 注入版，duck-typing，实现
                ``async run_evaluation(...) -> EvaluationResult``）。None 表示
                未注入 → 评估路径返回 EVAL_ENGINE_UNAVAILABLE（不静默空转）。
        """
        super().__init__()
        self._executor = executor

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_evaluate",
            description=(
                "任务评估工具：用于评估任务的验收指标是否满足，评估通过则自动完成任务。"
                "\n\n两种评估模式（均可完成任务）："
                "\n1. evaluate_single（单指标评估）：逐个评估指标，每次只评估一个 metric_id。"
                "当所有指标都通过后，任务自动完成。适合需要分步验证或针对性修复的场景。"
                "\n2. auto_complete（完全评估）：一次性评估所有指标。"
                "已通过的指标会自动跳过，只评估未通过的。适合首次评估或最终验证。"
                "\n\n【重要】调用前提：你必须已完成任务要求的全部工作步骤和产出物。"
                "如果你还有未完成的步骤、未输出的产出物、或未处理的待办事项，禁止调用此工具——先完成它们。"
                "无验收标准的任务会自动通过；指标重试超过上限后任务会被标记为失败。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["evaluate_single", "auto_complete"],
                        "description": (
                            "评估模式："
                            "evaluate_single-评估单个指标(需提供metric_id)，"
                            "所有指标逐一通过后任务自动完成；"
                            "auto_complete-评估所有未通过的指标(已通过的自动跳过)，默认"
                        ),
                        "default": "auto_complete",
                    },
                    "metric_id": {
                        "type": "string",
                        "description": "评估指标ID，仅在evaluate_single模式时必填",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "任务完成摘要（推荐填写）。内容应包含："
                            "1) 完成了什么工作（简要说明实现思路和做了哪些改动）；"
                            "2) 产出了什么（文件、配置、数据等产物）；"
                            "3) 产物的存放路径（相对路径，如 src/auth/login.py、config/rules.yaml）。"
                            "示例：'实现了用户登录功能，新增 JWT 认证模块。产出：src/auth/login.py、src/auth/jwt_handler.py、tests/test_login.py。'"
                            "评估器将依据此摘要了解任务成果并验证产物。"
                        ),
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            tags=["task", "evaluate", "metric", "completion"],
            injected_params=["session_id", "user_id", "tool_record_id", "task_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """执行任务评估。

        通过 injected_params 获取 task_id 等运行时参数，
        通过 TaskService 获取任务数据，通过 EvaluationExecutor 执行评估。

        系统级错误（INJECTION_ERROR/SERVICE_UNAVAILABLE）直接 fail_task + 返回
        task_failed 标记，避免 LLM 无意义重试。
        """
        action = inputs.get("action", "auto_complete")
        task_id = inputs.get("task_id")

        # 短 id 入参解析：LLM 工具面 id 短化——LLM 回传的 task_id 可能是
        # 12 位短 id，经 state 聚合前缀唯一解析回全 id。
        task_id = await self._resolve_task_id(task_id)
        if isinstance(task_id, str) and task_id.startswith("AMBIGUOUS:"):
            return create_failure_result(
                error=f"任务 ID '{task_id[len('AMBIGUOUS:') :]}' 匹配到多个任务，请使用完整 ID 重试",
                error_code="AMBIGUOUS_TASK_ID",
            )

        logger.warning(
            "[TRACE-EVAL] execute() ENTRY | action=%s | task_id=%s | input_keys=%s",
            action,
            task_id,
            list(inputs.keys()),
        )

        # GAP-1 统一：读面 state 优先（task = pipeline，task.* 即任务数据）；
        # task_service 仅余少量状态迁移辅助（evaluating 过渡态），评估数据
        # 读写全走 state 单键（YAML 影子写已退役，ADR 2026-10-08-eval-state-single-write）。
        task_service = self._get_task_service()

        if not task_id:
            # 零推断：task_id 缺失即注入链断裂（state 未取到 run 的 task_id，
            # 未变成工具参数注入），候选任务再多也不猜——猜测活跃任务会把
            # A 任务的评估写到 B 任务上，且掩盖断裂本身。
            return create_failure_result(
                error="系统错误：task_id 未注入，请联系管理员",
                error_code="INJECTION_ERROR",
                metadata={"task_failed": True},
            )

        task = await self._get_task_from_state(task_id)
        if task is None and task_service is not None:
            task = task_service.get_task(task_id)
        if task is None:
            return create_failure_result(error="任务不存在", error_code="TASK_NOT_FOUND")

        max_eval_calls = _DEFAULT_MAX_EVAL_CALLS
        if task.metadata and isinstance(task.metadata, dict):
            max_eval_calls = task.metadata.get("max_eval_calls", _DEFAULT_MAX_EVAL_CALLS)
        eval_total_calls = self._increment_eval_call_count(task)
        await _write_task_state(str(task_id), {"task.eval_total_calls": eval_total_calls})

        if eval_total_calls > max_eval_calls:
            logger.warning(
                "[TaskEvaluate] 全局评估调用次数超限 | task_id=%s | calls=%d | max=%d | 直接标记失败",
                task_id,
                eval_total_calls,
                max_eval_calls,
            )
            return create_failure_result(
                error=(
                    f"评估调用次数已达上限（{eval_total_calls}/{max_eval_calls}），"
                    f"任务自动失败。请检查评估指标是否存在无法满足的条件。"
                ),
                error_code="EVAL_CALL_LIMIT_EXCEEDED",
                metadata={"task_failed": True},
            )

        if action == "evaluate_single":
            return await self._evaluate_single(inputs, task_service, task)
        if action == "auto_complete":
            return await self._auto_complete(inputs, task_service, task)
        return create_failure_result(error=f"不支持的操作: {action}", error_code="INVALID_ACTION")

    async def _resolve_task_id(self, task_id: Any) -> Any:
        """LLM 入参 task_id 短 id → 全 id（state 聚合前缀唯一解析；歧义报错）。"""
        if not isinstance(task_id, str) or not task_id:
            return task_id
        try:
            rows = await self._read_state_rows()
        except Exception:  # noqa: BLE001 — 解析降级：原样放行既有"任务不存在"路径
            return task_id
        if rows is None:
            return task_id
        try:
            from id_utils import resolve_id  # noqa: PLC0415

            resolved = await resolve_id(rows, task_id)
        except Exception:  # noqa: BLE001 — 解析失败降级：原样放行
            return task_id
        if resolved.startswith("AMBIGUOUS:"):
            return f"AMBIGUOUS:{task_id}"
        return resolved

    async def _evaluate_single(  # noqa: PLR0911
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """评估单个指标（增量模式）。

        逐个评估指标，每次只评估指定的 metric_id。
        评估后汇总历史记录：如果所有声明的指标都已通过，自动完成任务；
        否则返回当前结果，Agent 可继续评估其他指标或改进后重试。

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        metric_id = inputs.get("metric_id")
        task_id = task.id

        logger.warning(
            "[TRACE-EVAL] _evaluate_single ENTRY | task=%s | metric_id=%s | metric_ids=%s | task_metadata_keys=%s",
            task_id,
            metric_id,
            self._get_metric_ids(task),
            list(task.metadata.keys()) if hasattr(task, "metadata") and task.metadata else "N/A",
        )

        if not metric_id:
            return create_failure_result(
                error="单指标评估模式需要提供 metric_id",
                error_code="METRIC_ID_REQUIRED",
            )

        metric_ids = self._get_metric_ids(task)
        if len(metric_ids) == 1:
            logger.info(
                "[TaskEvaluate] 单指标任务自动转为完全评估 | task_id=%s | metric_count=%d",
                task_id,
                len(metric_ids),
            )
            return await self._auto_complete(inputs, task_service, task)

        try:
            import litellm  # noqa: PLC0415
        except ImportError:  # 0.2 精简环境可选依赖：限速细粒度识别降级
            litellm = None  # type: ignore[assignment]

        executor = self._create_executor(task_service)
        if executor is None:
            logger.warning(
                "[TaskEvaluate] 评估执行器未注入（0.2 评估引擎不可用）| task_id=%s | metric_id=%s",
                task_id,
                metric_id,
            )
            return create_failure_result(
                error="评估执行器未注入（0.2 评估引擎不可用），无法执行评估",
                error_code="EVAL_ENGINE_UNAVAILABLE",
            )
        timeout = self._get_eval_timeout(task)

        single_params: dict[str, dict[str, Any]] = {}
        summary_from_input = inputs.get("summary", "")
        if summary_from_input:
            single_params[metric_id] = {"summary": summary_from_input}

        try:
            result = await asyncio.wait_for(
                executor.run_evaluation(
                    task_id=task_id,
                    metric_ids=[metric_id],
                    input_params=single_params,
                    skip_state_update=True,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "[TaskEvaluate] 单指标评估超时 | task_id=%s | metric_id=%s | timeout=%ss",
                task_id,
                metric_id,
                timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_id} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
        except Exception as exc:
            if litellm is not None and isinstance(exc, litellm.RateLimitError):
                logger.warning(
                    "[TaskEvaluate] 评估期间 API 限速 | task_id=%s | metric_id=%s: %s",
                    task_id,
                    metric_id,
                    exc,
                )
                return create_failure_result(
                    error=f"评估期间 API 限速: {exc}",
                    error_code="RATE_LIMITED",
                )
            logger.exception("[TaskEvaluate] 单指标评估失败: %s", exc)
            return create_failure_result(error=f"评估失败: {exc}", error_code="EVAL_FAILED")

        # 注册评估子管道 + 追加历史记录（历史落 state 单键，跨调用累积）
        self._register_eval_pipelines(task_service, task, result)
        self._append_eval_history(task, result)
        await _write_task_state(
            str(task_id),
            {"task.evaluation_history": task.metadata.get("evaluation_history") or []},
        )
        await _write_task_state(
            str(task_id),
            {"task.evaluation": self._build_result_data(result)},
        )

        # 无评估结果（指标未找到或未加载）→ 返回明确错误，不误导 Agent 重试
        if not result.results:
            return create_failure_result(
                error=f"指标 '{metric_id}' 未找到：该指标不存在于评估指标注册表中，请确认指标 ID 是否正确",
                error_code="METRIC_NOT_FOUND",
            )

        # 当前指标未通过 → 返回结果，Agent 继续改进
        if not result.overall_passed:
            return create_success_result(
                data=self._build_result_data(result),
                metadata={
                    "action": "evaluate_single",
                    "result": "retry",
                    "message": f"指标 {metric_id} 未通过，请根据反馈继续改进",
                },
            )

        # 当前指标通过，检查所有声明指标是否都已通过
        if self._all_metrics_passed(task, metric_ids):
            logger.info(
                "[TaskEvaluate] 所有指标已通过，完成任务 | task_id=%s",
                task_id,
            )
            return await self._complete_task(
                task_service,
                task,
                result,
                str(inputs.get("summary") or "").strip(),
            )

        # 还有指标未评估，返回进度
        evaluated, remaining = self._get_eval_progress(task, metric_ids)
        return create_success_result(
            data=self._build_result_data(result),
            metadata={
                "action": "evaluate_single",
                "result": "partial_pass",
                "message": (
                    f"指标 {metric_id} 已通过。进度：{evaluated}/{len(metric_ids)}，剩余：{', '.join(remaining)}"
                ),
            },
        )

    async def _auto_complete(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """自动完成评估（评估任务提交时声明的所有指标）。"""
        # 指标来源兜底：0.2 任务=管道架构下指标实际在管道 state 的
        # task.acceptance_criteria——从 state 聚合行补齐（含 JSON 字符串还原）。
        await self._ensure_criteria_from_state(task)
        logger.warning(
            "[TRACE-EVAL] _auto_complete ENTRY | task=%s | metric_ids=%s",
            task.id,
            self._get_metric_ids(task),
        )
        metric_ids = self._get_metric_ids(task)
        try:
            input_params = self._get_input_params(task)
        except ValueError as exc:
            # 指标参数解析失败（如 {tool_id} 模板歧义）：显式失败，不带病评估
            logger.error(
                "[TaskEvaluate] 指标参数解析失败 | task_id=%s | %s",
                task.id,
                exc,
            )
            return create_failure_result(
                error=f"指标参数解析失败：{exc}",
                error_code="INVALID_INPUT_PARAMS",
            )

        if not metric_ids:
            logger.warning(
                "[TaskEvaluate] 任务 %s 未声明任何评估指标，未经评估直接完成（结论带无验收标注）",
                task.id,
            )
            # 诚实标注：无验收指标 = 零验证放行，执行者自我报告不得以裸文本
            # 充当评估结论。标注前缀随 task.eval_summary 落账，工具返回消息
            # 同步如实。
            submitted = str(inputs.get("summary") or "").strip()
            annotated_summary = (
                f"【无验收指标，未经评估直接完成】{submitted}" if submitted else "未声明评估指标，未经评估直接完成"
            )
            return await self._complete_task(
                task_service,
                task,
                type(
                    "EvalResult",
                    (),
                    {
                        "task_id": task.id,
                        "overall_passed": True,
                        "summary": "未声明评估指标，未经评估直接完成",
                        "results": [],
                    },
                )(),
                annotated_summary,
                completion_message="任务未声明验收指标，未经评估直接完成",
            )

        # 跳过已通过的指标，只评估未通过的
        already_passed, remaining_ids = self._get_eval_progress(
            task,
            metric_ids,
        )
        # 未配置评估依据的指标直接通过：没有验收标准 = 视为满足（不做任务
        # 描述顶替——评估引擎按 input_params 检查语义判定，顶替只会伪造
        # "有标准"假象）；留 warning 让配置缺失可见。
        # 配置判定单源 = 指标定义（evaluation_metrics.yaml）：
        # - agent 型（语义评估）：input_schema 任一属性键出现在参数里即算
        #   已配置——semantic_check 的 output/expected 均有效；存量 criteria
        #   任务兼容保留；
        # - 其余类型维持键面判定：criteria 或可执行验证参数（command/path）
        #   （工具型指标参数键本就不是 criteria，免检放行 = 验证器没跑就判
        #   过，ADR 2026-09-16-external-dataset-sourcing；human 型不进自动
        #   执行器，维持直通）。
        definitions = load_metric_definitions()

        def _has_eval_basis(mid: str, p: dict[str, Any]) -> bool:
            definition = definitions.get(mid) or {}
            if str(definition.get("evaluator_type") or "") == "agent":
                properties = (definition.get("input_schema") or {}).get("properties") or {}
                return bool(any(key in p for key in properties) or (p.get("criteria") or "").strip())
            return bool((p.get("criteria") or "").strip() or p.get("command") or p.get("path"))

        no_criteria_ids = [mid for mid in remaining_ids if not _has_eval_basis(mid, input_params.get(mid, {}))]
        if no_criteria_ids:
            already_passed += len(no_criteria_ids)
            remaining_ids = [mid for mid in remaining_ids if mid not in no_criteria_ids]
            logger.warning(
                "[TaskEvaluate] 指标未配置评估依据，直接通过 | task_id=%s | metric_ids=%s",
                task.id,
                sorted(no_criteria_ids),
            )
        if not remaining_ids:
            logger.info(
                "[TaskEvaluate] 所有指标已通过，直接完成任务 | task_id=%s | passed=%d/%d",
                task.id,
                already_passed,
                len(metric_ids),
            )
            # 通过来源如实标注：未配置评估依据直接通过 ≠ 历史评估记录通过
            _pass_note = (
                f"（其中 {len(no_criteria_ids)} 个未配置评估依据直接通过）"
                if no_criteria_ids
                else "（来自历史评估记录）"
            )
            return await self._complete_task(
                task_service,
                task,
                type(
                    "EvalResult",
                    (),
                    {
                        "task_id": task.id,
                        "overall_passed": True,
                        "summary": str(inputs.get("summary") or "").strip()
                        or f"所有 {len(metric_ids)} 个指标均已通过{_pass_note}",
                        "results": [],
                    },
                )(),
                str(inputs.get("summary") or "").strip(),
            )

        summary_from_input = inputs.get("summary", "")
        if summary_from_input:
            for _mid, p in input_params.items():
                if not p.get("summary"):
                    p["summary"] = summary_from_input

        logger.info(
            "[TaskEvaluate] 自动评估 | task_id=%s | total=%d | already_passed=%d | to_eval=%s",
            task.id,
            len(metric_ids),
            already_passed,
            remaining_ids,
        )

        executor = self._create_executor(task_service)
        if executor is None:
            logger.warning(
                "[TaskEvaluate] 评估执行器未注入（0.2 评估引擎不可用）| task_id=%s | metrics=%s",
                task.id,
                remaining_ids,
            )
            return create_failure_result(
                error="评估执行器未注入（0.2 评估引擎不可用），无法执行评估",
                error_code="EVAL_ENGINE_UNAVAILABLE",
            )
        timeout = self._get_eval_timeout(task)
        try:
            result = await asyncio.wait_for(
                executor.run_evaluation(
                    task_id=task.id,
                    metric_ids=remaining_ids,
                    input_params=input_params,
                    skip_state_update=True,
                ),
                timeout=timeout,
            )
            return await self._handle_evaluation_result(inputs, task_service, task, result)
        except TimeoutError:
            logger.warning(
                "[TaskEvaluate] 自动评估超时 | task_id=%s | metrics=%s | timeout=%ss",
                task.id,
                remaining_ids,
                timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_ids} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
        except Exception as e:
            logger.exception("[TaskEvaluate] 自动完成评估失败: %s", e)
            return create_failure_result(error=f"评估失败: {e}", error_code="EVAL_FAILED")

    async def _handle_evaluation_result(  # noqa: PLR0912,PLR0915
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
        eval_result: Any,
    ) -> ToolExecutionResult:
        """根据评估结果处理三种情况。

        - 全部通过 → COMPLETED + 通知
        - 失败但次数未耗尽 → 返回结果，Agent 继续
        - 失败且次数耗尽 → FAILED + 通知

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例

        Returns:
            工具执行结果
        """
        max_retries = _DEFAULT_MAX_RETRIES
        if task.metadata and isinstance(task.metadata, dict):
            max_retries = task.metadata.get("max_eval_retries", _DEFAULT_MAX_RETRIES)

        retry_counts: dict[str, int] = {}
        if task.metadata and isinstance(task.metadata, dict):
            retry_counts = task.metadata.get("eval_retry_count", {})
            if not isinstance(retry_counts, dict):
                retry_counts = {}

        has_failure = False
        exhausted = False

        _UNRECOVERABLE_PATTERNS = (  # noqa: N806
            "command not found",
            "no such file or directory",
            "module not found",
            "is not recognized",
        )

        for r in eval_result.results:
            mid = r.metric_id
            if not r.passed:
                has_failure = True
                output_str = str(r.evaluator_output or "").lower()
                message_str = (r.message or "").lower()
                is_unrecoverable = any(p in output_str or p in message_str for p in _UNRECOVERABLE_PATTERNS)
                if is_unrecoverable:
                    retry_counts[mid] = max_retries
                    exhausted = True
                    continue
                current = retry_counts.get(mid, 0) + 1
                retry_counts[mid] = current
                if current >= max_retries:
                    exhausted = True
            else:
                # 渐进重试：指标通过时重置连续失败计数
                prev_count = retry_counts.get(mid, 0)
                if prev_count > 0:
                    logger.info(
                        "[TaskEvaluate] 指标 %s 通过，重置连续失败计数 %d → 0 (渐进重试)",
                        mid,
                        prev_count,
                    )
                retry_counts[mid] = 0

        if task.metadata is None:
            task.metadata = {}
        task.metadata["eval_retry_count"] = retry_counts

        # 注册评估子管道到根任务子目录
        self._register_eval_pipelines(task_service, task, eval_result)

        # 追加本次评估记录到历史（保留所有评估尝试）——历史与评估结果落
        # state 单键（进度判定/结果读回的跨调用真值，YAML 影子写已退役）
        self._append_eval_history(task, eval_result)
        history_persisted = await _write_task_state(
            str(task.id),
            {
                "task.eval_retry_count": retry_counts,
                "task.eval_total_calls": task.metadata.get("eval_total_calls", 0),
                "task.evaluation_history": task.metadata.get("evaluation_history") or [],
                "task.evaluation": self._build_result_data(eval_result),
            },
        )
        if not history_persisted:
            # 历史未落账 = 跨调用进度判定退化为全量重评（重评安全，不死循环），
            # 但必须留痕让降级可观测。
            logger.warning(
                "[TaskEvaluate] 评估历史/计数落 state 失败（下轮进度判定退化为全量重评）| task=%s",
                task.id,
            )

        # 无评估结果（所有指标 ID 均未在评估指标注册表中找到）→ 不误导完成
        if not eval_result.results:
            return create_failure_result(
                error="未找到任何有效的评估指标，所有指定的指标 ID 均不存在于评估指标注册表中，请确认指标配置是否正确",
                error_code="METRIC_NOT_FOUND",
            )

        if not has_failure:
            return await self._complete_task(
                task_service,
                task,
                eval_result,
                str(inputs.get("summary") or "").strip(),
            )
        if exhausted:
            return await self._fail_task(task_service, task, eval_result, max_retries)
        min_remaining = max_retries - min(retry_counts.values())
        eval_total = task.metadata.get("eval_total_calls", 0) if task.metadata else 0
        max_eval_calls = (
            task.metadata.get("max_eval_calls", _DEFAULT_MAX_EVAL_CALLS) if task.metadata else _DEFAULT_MAX_EVAL_CALLS
        )
        overall_remaining = max_eval_calls - eval_total
        failed_details = []
        for r in eval_result.results:
            if not r.passed:
                detail = f"- [{r.metric_id}] 未通过"
                if r.message:
                    detail += f": {r.message}"
                if r.score is not None:
                    detail += f" (得分: {r.score})"
                failed_details.append(detail)
        feedback = "评估未通过，请根据以下反馈继续改进：\n" + "\n".join(failed_details)
        feedback += f"\n\n指标连续失败剩余重试：{min_remaining} 次"
        feedback += f"\n全局评估调用剩余次数：{overall_remaining} 次（已调用 {eval_total}/{max_eval_calls}）"
        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": inputs.get("action", "auto_complete"),
                "result": "retry",
                "retry_remaining": min_remaining,
                "message": feedback,
            },
        )

    async def _complete_task(
        self,
        task_service: Any,
        task: Any,
        eval_result: Any,
        submitted_summary: str = "",
        completion_message: str = "",
    ) -> ToolExecutionResult:
        """评估通过，完成任务。

        合并前置策略：对于 worktree 模式的任务，在标记 completed 之前先执行合并，
        合并成功才变更状态，合并失败则标记为 failed。
        非 worktree 模式直接完成。

        TaskService.on_state_change 回调会自动发送终态通知。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
            submitted_summary: LLM 经 task_evaluate 入参提交的任务完成总结
                （0.1 task_notifier 评估结论数据源；优先于系统生成的逐指标说明）
            completion_message: 完成返回消息（空 = 默认"评估通过，任务已完成"；
                无验收指标完成路径传诚实标注文案，不得谎称评估通过）

        Returns:
            工具执行结果
        """
        # 完赛前置证据校验（单漏斗：required_files 双闸 / 未声明时 commit 面）
        # ——已完成任务的幂等短路不重开闸：只在仍要授予完成态的路径（FAILED
        # 恢复完成/常态完成）上设闸。
        if task.status != TaskStatus.COMPLETED:
            evidence_rejection = await self._check_completion_evidence(task)
            if evidence_rejection is not None:
                return evidence_rejection
        # 完成落库的终态字段（成功授予完成态的分支置位）——尾部同批带进成功
        # 结果（terminal_fields），经 tool_core 投影进引擎在飞 state：评估完成
        # 写面只落 DB/registry，在飞 state 当轮不同步，run.completed 终态载荷
        # 会带陈旧未决 task.status，任务域派生（未决态不派生）被拦、父通知断
        # （2026-10-25 装机 a3615d3c5bb5 实锤）。
        terminal_written: dict[str, Any] | None = None
        if task.status == TaskStatus.COMPLETED:
            logger.info("[TaskEvaluate] 任务 %s 已完成，跳过状态回写", task.id)
        elif task.status == TaskStatus.FAILED:
            logger.warning(
                "[TaskEvaluate] 任务 %s 已失败但评估通过，尝试恢复为完成",
                task.id,
            )
            # 恢复为完成同样必须过合并门控（2026-09-04 裁定：合并成功才准
            # 置完成）——失败任务的工作区可能仍是 worktree，绕门恢复会把
            # worktree 产物留在未合并的副本里。
            merge_error = await self._try_merge_before_complete(task)
            if merge_error:
                logger.error(
                    "[TaskEvaluate] 恢复完成前合并门控失败，保持 failed: task_id=%s, error=%s",
                    task.id,
                    merge_error,
                )
                return create_failure_result(
                    error=f"评估通过但完成前工作区合并失败，任务保持 failed: {merge_error}",
                    error_code="MERGE_GATE_FAILED",
                    metadata={"task_failed": True},
                )
            try:
                # 终态写面 = pipeline-state.update 单通道（YAML 状态机写面退役，
                # ADR 2026-10-08-eval-state-single-write）；合并备注随终态落库
                # （2026-10-23 裁定：备份落主空间后必须告知上级对账）。
                recover_state: dict[str, Any] = {
                    "task.status": "completed",
                    "task.ended_at": _now_iso(),
                    "task.eval_summary": submitted_summary or self._build_rich_summary(eval_result),
                    "task.evaluation": self._build_result_data(eval_result),
                }
                collisions = worktree_merge.last_collision_report()
                if collisions:
                    recover_state["task.merge_notes"] = "；".join(
                        f"{c['path']}→{c['action']}" for c in collisions
                    )
                state_persisted = await _write_task_state(task.id, recover_state)
                terminal_written = recover_state
            except Exception as e:
                logger.error("[TaskEvaluate] 恢复完成终态写 state 失败: %s", e)
                state_persisted = False
            if not state_persisted:
                # 终态回写失败不得返回与成功同值（task 实际仍 failed）
                return create_failure_result(
                    error="恢复完成终态写 state 失败（任务未落账 completed）",
                    error_code="RECOVER_TO_COMPLETED_FAILED",
                    metadata={"task_failed": True},
                )
        else:
            # worktree 合并门控（0.1 判定）：worktree 模式 completed 前先合并，
            # 合并成功才变更状态，失败则标记为 failed。合并是同步 git 子进程
            # 串（可阻塞数分钟），门控内部丢线程池执行，不冻主事件循环。
            # merge_error 自描述分类（ws_meta 读取失败 / worktree 合并失败），
            # 此处按原文透传，不得再套统一前缀。
            merge_error = await self._try_merge_before_complete(task)
            if merge_error:
                # 门控失败一律直接判死：坐标单键在出生/创建时一次写全（出生
                # 预写 + init 创建写），不存在"元数据镜像时序窗口"，重试无意义
                # （2026-10-08 state 单一真相源收敛，原 merge_gate_failures
                # 重试计数随窗口一并退役）。
                logger.error(
                    "[TaskEvaluate] 完成前工作区门控失败，任务标记为 failed: task_id=%s, error=%s",
                    task.id,
                    merge_error,
                )
                state_persisted = await _write_task_state(
                    task.id,
                    {
                        "task.status": "failed",
                        "task.ended_at": _now_iso(),
                        "task.eval_summary": submitted_summary or self._build_rich_summary(eval_result),
                        "task.error": merge_error,
                        "task.failure_class": "merge_gate",
                        "task.failure_reason": merge_error,
                    },
                )
                return create_failure_result(
                    error=merge_error,
                    metadata={"task_failed": True, "state_persisted": state_persisted},
                )
            # 终态写面 = pipeline-state.update 单通道（YAML 状态机写面退役，
            # ADR 2026-10-08-eval-state-single-write）；未落账不得谎报完成。
            # 未跟踪碰撞自愈随终态落 task.merge_notes（2026-10-23 用户裁定：
            # 备份落主空间后必须告知上级对账——tasks/events 派生 tags →
            # triggers_ext._auto_notify_parent 渲染进父通知）。
            collisions = worktree_merge.last_collision_report()
            completed_state: dict[str, Any] = {
                "task.status": "completed",
                "task.ended_at": _now_iso(),
                "task.eval_summary": submitted_summary or self._build_rich_summary(eval_result),
                "task.evaluation": self._build_result_data(eval_result),
            }
            if collisions:
                completed_state["task.merge_notes"] = "；".join(
                    f"{c['path']}→{c['action']}" for c in collisions
                )
            state_persisted = await _write_task_state(task.id, completed_state)
            if not state_persisted:
                logger.error(
                    "[TaskEvaluate] 完成终态写 state 失败（未落账，不谎报完成）: task_id=%s",
                    task.id,
                )
                return create_failure_result(
                    error="完成终态写 state 失败（任务未落账 completed）",
                    error_code="EVAL_STATE_WRITE_FAILED",
                    metadata={"task_failed": True},
                )
            terminal_written = completed_state

        result_data = self._build_result_data(eval_result)
        if terminal_written is not None:
            # 终态投影字段同批带进成功结果（tool_core 消费，见 terminal_written 注）；
            # task.evaluation 大对象不进在飞 state。
            result_data["terminal_fields"] = {
                k: v for k, v in terminal_written.items() if k != "task.evaluation"
            }
        return create_success_result(
            data=result_data,
            metadata={
                "action": "auto_complete",
                "result": "completed",
                "message": completion_message or "评估通过，任务已完成",
            },
        )

    async def _try_merge_before_complete(self, task: Any) -> str | None:
        """在标记 completed 之前执行 worktree 合并门控（0.1 判定，进程内本地执行）。

        ws_meta 数据源（state 单一真相源）：state 行单键 ``task.ws_meta``；
        合并 git 机制经 worktree_merge 共享模块原样执行（同步 git 子进程串，
        asyncio.to_thread 丢线程池——不冻主事件循环，合并锁跨线程有效）。
        无"服务不可用跳过"分支：ws_meta 读不到 = 门控失败
        （worktree 产物不能静默丢失）。

        合并落点由 agent 自主选择（路径白名单把守合法性，内核不裁判落点
        世界——2026-09-19 用户裁决）：门控只管合并机制本身。

        Returns:
            None 表示合并成功或不需要合并（plain/shared 模式），
            str 表示门控失败原因（自描述分类：ws_meta 读取失败 / 元数据残缺 /
            worktree 合并失败 / 产物未到达），调用方按原文透传并据此
            标记任务 failed。
        """
        ws_meta = await self._read_task_ws_meta(task)
        return await asyncio.to_thread(worktree_merge.merge_worktree_before_complete, task.id, ws_meta)

    async def _read_task_ws_meta(self, task: Any) -> dict[str, Any]:
        """任务 ws_meta 数据源（state 单键单通道）：state 行 ``task.ws_meta``。

        写面 = task_submit 出生预写（显式 plain）+ workspace_lifecycle init
        创建即写（worktree/默认/继承，经 pipeline-state.update 即时入注册表），
        出生/创建时一次写全、无时序窗口。聚合行值可能是 JSON 字符串（DB 投影
        原样存储），经 state_fields.as_dict 还原成 dict；缺失返回空 dict（由
        合并门控按失败裁决，不静默跳过）。返回空时留诊断日志（行数/行键），
        供合并失败根因定位。
        """
        rows = await self._read_state_rows()
        if rows:
            row = next(
                (r for r in rows if str(r.get("pipeline_id") or "") == str(task.id)),
                None,
            )
            if row is not None:
                meta = state_fields.as_dict(row.get("task.ws_meta"), field="task.ws_meta")
                if meta:
                    return meta
                logger.warning(
                    "[TaskEvaluate] ws_meta 诊断：state 行在但无 task.ws_meta 键 | task_id=%s | 行键=%s",
                    task.id,
                    sorted(row.keys()),
                )
            else:
                logger.warning(
                    "[TaskEvaluate] ws_meta 诊断：state 行数=%d，无本任务行 | task_id=%s",
                    len(rows),
                    task.id,
                )
        else:
            logger.warning(
                "[TaskEvaluate] ws_meta 诊断：state 聚合行不可用（rows=%r）| task_id=%s",
                rows,
                task.id,
            )
        return {}

    async def _check_completion_evidence(self, task: Any) -> ToolExecutionResult | None:
        """完赛前置证据校验（双轨单漏斗：文件面 / commit 面，完赛单入口）。

        豁免轨：声明 acceptance_criteria.files 的非 worktree 任务 → 文件面
        替代 commit 面（清单 = required_files 与 acceptance files 并集，逐一
        核对存在 + mtime 晚于基线；commit 面整体跳过，零 git 探测）。
        既有轨：未声明 acceptance files 或 worktree 任务 → 原闸不变：声明
        required_files → 文件面（存在且 mtime 晚于任务创建基线）与 commit 面
        （工作空间基线后有新 commit）AND 双闸；未声明 → 只查 commit 面
        （无 git 记录不落 completed，回执要求 agent 先提交）。commit 面单次
        探测、两分支共享同一结果（同一任务不做第二次 git 探测）。
        任一证据不足 → 返回拒绝结果（result=retry，回执写明细），任务不落
        completed。
        降级（warning 留痕，放行）：工作空间坐标不可得 / 基线缺失或不可解析
        → 全放行；既有轨下工作空间非 git 仓 / git 不可用或超时 → commit 面
        跳过（声明 required_files 时文件面照查，未被降级豁免；豁免轨纯文件
        面，无 git 降级形态）。

        Returns:
            None = 证据校验通过或降级放行；ToolExecutionResult = 证据不足拒绝。
        """
        required_files = [
            _resolve_task_placeholder(p, str(task.id))
            for p in _required_files_declared(task.metadata)
        ]
        ws_meta = await self._read_task_ws_meta(task)
        workspace = str(ws_meta.get("path") or "")
        acceptance_files = [
            _resolve_task_placeholder(p, str(task.id))
            for p in _acceptance_files_declared((task.metadata or {}).get("acceptance_criteria"))
        ]
        exempt = bool(acceptance_files) and not _is_worktree_workspace(ws_meta)
        face = (
            "文件面（acceptance files 豁免轨）"
            if exempt
            else ("文件面+commit 面" if required_files else "commit 面")
        )
        if not workspace or not os.path.isdir(workspace):
            logger.warning(
                "[TaskEvaluate] 完赛证据校验降级放行（工作空间坐标不可得）| task=%s | 面=%s | ws_path=%r",
                task.id,
                face,
                workspace,
            )
            return None
        baseline = _parse_created_at_epoch(getattr(task, "created_at", ""))
        if baseline is None:
            logger.warning(
                "[TaskEvaluate] 完赛证据校验降级放行（任务创建基线缺失/不可解析）"
                "| task=%s | 面=%s | created_at=%r",
                task.id,
                face,
                getattr(task, "created_at", ""),
            )
            return None
        baseline_text = datetime.fromtimestamp(baseline).isoformat(timespec="seconds")
        if exempt:
            # 豁免轨（ADR 2026-11-11-evidence-gate-dual-track）：文件面替代
            # commit 面——并集清单照查，commit 面整体跳过（不做 git 探测）。
            union_files = list(dict.fromkeys([*required_files, *acceptance_files]))
            violations = _collect_required_files_violations(workspace, union_files, baseline)
            if not violations:
                return None
            return _reject_incomplete_delivery(
                str(task.id),
                _build_acceptance_files_receipt(str(task.id), violations, baseline_text),
                metric_id="acceptance_files",
                detail=f"违规={len(violations)}（豁免轨文件面）",
            )
        since_text = datetime.fromtimestamp(baseline).strftime("%Y-%m-%d %H:%M:%S")
        commit = await asyncio.to_thread(_probe_commit_evidence, workspace, since_text)
        if commit.degrade_reason:
            logger.warning(
                "[TaskEvaluate] 完赛证据校验降级（commit 面跳过，放行）"
                "| task=%s | 面=%s | ws=%s | 原因=%s",
                task.id,
                face,
                workspace,
                commit.degrade_reason,
            )
        if not required_files:
            if commit.degrade_reason or commit.has_new_commit:
                return None
            return _reject_incomplete_delivery(
                str(task.id),
                _build_commit_evidence_receipt(str(task.id), baseline_text),
                metric_id="commit_evidence",
                detail="无基线后新 commit",
            )
        violations = _collect_required_files_violations(workspace, required_files, baseline)
        if not commit.degrade_reason and not commit.has_new_commit:
            violations.append("- 交付未落库，commit 后再完赛（任务创建基线之后工作空间无新 commit）")
        if not violations:
            return None
        return _reject_incomplete_delivery(
            str(task.id),
            _build_required_files_receipt(str(task.id), violations, baseline_text),
            metric_id="required_files",
            detail=f"违规={len(violations)}",
        )

    async def _fail_task(
        self,
        task_service: Any,
        task: Any,
        eval_result: Any,
        max_retries: int,
    ) -> ToolExecutionResult:
        """评估失败且次数耗尽，标记任务失败。

        终态写面 = pipeline-state.update 单通道（YAML 状态机写面退役，
        ADR 2026-10-08-eval-state-single-write）。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
            max_retries: 最大重试次数

        Returns:
            工具执行结果
        """
        try:
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                _summary = self._build_rich_summary(eval_result)
                _reason = f"评估未通过: {_summary}" if _summary else "评估未通过"
                state_persisted = await _write_task_state(
                    task.id,
                    {
                        "task.status": "failed",
                        "task.ended_at": _now_iso(),
                        "task.eval_summary": _summary,
                        "task.error": _reason,
                        "task.failure_class": "eval_retry_exhausted",
                        "task.failure_reason": _reason,
                        "task.evaluation": self._build_result_data(eval_result),
                    },
                )
                if not state_persisted:
                    # 终态未落账不得返回与成功同值（task 实际仍非 failed）
                    return create_failure_result(
                        error="评估耗尽终态写 state 失败（任务未落账 failed）",
                        error_code="EVAL_STATE_WRITE_FAILED",
                        metadata={"task_failed": True},
                    )
            else:
                logger.info("[TaskEvaluate] 任务 %s 已是终态(%s)，跳过状态回写", task.id, task.status.value)
        except Exception as e:
            logger.error("[TaskEvaluate] 评估耗尽终态写 state 失败: %s", e)
            return create_failure_result(
                error=f"评估耗尽终态写 state 失败: {e}",
                metadata={"eval_data": str(self._build_result_data(eval_result))[:200]},
            )

        failed_metrics = [r.metric_id for r in eval_result.results if not r.passed]
        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": "auto_complete",
                "result": "failed",
                "message": (
                    f"评估未通过且重试次数耗尽({max_retries}次)，任务失败。未通过指标：{', '.join(failed_metrics)}"
                ),
            },
        )

    @staticmethod
    def _register_eval_pipelines(
        task_service: Any,
        task: Any,
        eval_result: Any,
    ) -> None:
        """将 Agent 型评估产生的子管道注册到根任务子目录。

        作为 _pre_register_eval_pipeline 的兜底：如果 engine.py
        的早期注册因 ServiceProvider 不可用等原因被跳过，
        此处会在评估完成后再次尝试。
        """
        try:
            root_id = task_service.get_root_task_id(task.id)
            if not root_id:
                logger.debug(
                    "[TaskEvaluate] 无 root_id，跳过评估管道注册 | task=%s",
                    task.id,
                )
                return
            provider = _get_service_provider()
            exec_storage = provider.get("execution_record_storage")
            if not exec_storage:
                logger.warning("[TaskEvaluate] execution_record_storage 不可用，跳过评估管道注册")
                return
            registered = 0
            skipped = 0
            for r in eval_result.results:
                pid = getattr(r, "pipeline_run_id", None)
                if pid:
                    exec_storage.register_pipeline(pid, root_id)
                    registered += 1
                elif hasattr(r, "pipeline_run_id"):
                    skipped += 1
            if registered or skipped:
                logger.info(
                    "[TaskEvaluate] 评估管道注册 | task=%s | root=%s | registered=%d | skipped=%d",
                    task.id,
                    root_id,
                    registered,
                    skipped,
                )
        except Exception as exc:
            logger.warning(
                "[TaskEvaluate] 注册评估管道分组失败 | task=%s | error=%s",
                task.id,
                exc,
            )

    @staticmethod
    def _append_eval_history(task: Any, eval_result: Any) -> None:
        """将本次评估结果追加到 task.metadata 的 evaluation_history。

        每次评估（无论通过/失败/重试）都会被记录，包含时间戳、
        评估指标详情（含评估器输入/输出和 Agent 管道 ID）。

        Args:
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
        """
        from datetime import datetime  # noqa: PLC0415

        metrics = []
        for r in eval_result.results:
            m: dict[str, Any] = {
                "metric_id": r.metric_id,
                "passed": r.passed,
                "score": r.score,
                "message": r.message,
                "error": r.error,
                "evidence": getattr(r, "evidence", []),
                "suggestions": getattr(r, "suggestions", []),
                "details": getattr(r, "details", {}),
            }
            if hasattr(r, "evaluator_input") and r.evaluator_input:
                m["evaluator_input"] = r.evaluator_input
            if hasattr(r, "evaluator_output") and r.evaluator_output:
                m["evaluator_output"] = r.evaluator_output
            if hasattr(r, "pipeline_run_id") and r.pipeline_run_id:
                m["pipeline_run_id"] = r.pipeline_run_id
            metrics.append(m)

        history = task.metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "passed": eval_result.overall_passed,
                "summary": getattr(eval_result, "summary", ""),
                "metrics": metrics,
            }
        )
        task.metadata["evaluation_history"] = history

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list；None = 桥未就绪）。"""
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩评估
            logger.warning("[TaskEvaluate] state 聚合读取失败: %s", exc)
            return None

    async def _ensure_criteria_from_state(self, task: Any) -> None:
        """验收标准兜底：task.metadata 无 acceptance_criteria 时从 state 聚合行补齐。

        0.2 任务=管道架构下指标落在管道 state（task.acceptance_criteria），
        state 组装对象缺该键时（出口白名单未含）不兜底会导致评估误判
        "未声明任何评估指标"而直接标记完成（实测管道 b8b92a56ad72）。
        聚合行值可能是 JSON 字符串（DB 投影原样存储），还原成 dict。
        """
        if not getattr(task, "id", None):
            return
        if task.metadata and task.metadata.get("acceptance_criteria"):
            return
        try:
            rows = await self._read_state_rows()
        except Exception:  # noqa: BLE001 — 兜底失败按无指标继续（既有降级路径）
            return
        if not rows:
            return
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == str(task.id)),
            None,
        )
        if row is None:
            return
        ac = state_fields.as_dict(row.get("task.acceptance_criteria"), field="task.acceptance_criteria")
        if ac:
            task.metadata = dict(task.metadata or {})
            task.metadata["acceptance_criteria"] = ac

    async def _get_task_from_state(self, task_id: str) -> Any:
        """从 state 聚合行组装任务对象（GAP-1 统一：task = pipeline）。

        返回真实 TaskModel（工具内评估逻辑按对象字段消费；真实模型字段集
        保证属性访问面完整）。行字段（state 出口白名单）：task.goal/task.status/
        task.description/task.acceptance_criteria/task.evaluation/
        task.evaluation_history/task.eval_retry_count/task.eval_total_calls/
        task.ws_meta/lineage.parent_pipeline_id 等扁平点号键。
        缺行返回 None（调用方回退 YAML 只读镜像）。

        行解析身份门（ADR 2026-11-06 D5 N8）：无 ``task.id`` 且无
        ``lineage.*`` 非空键的行是会话主管道（可带幽灵残留）——不解析为
        任务对象（调用方按结构化 not found 处理）。判据与 tasks 插件
        ``events.is_task_pipeline_row`` 同口径（本地副本，不引入跨插件 import）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None:
            return None
        if str(row.get("task.id") or "") == "" and not any(
            str(k).startswith("lineage.") and row[k] is not None and row[k] != ""
            for k in row
        ):
            return None
        status_str = str(row.get("task.status") or "pending")
        try:
            status = TaskStatus(status_str)
        except (ValueError, AttributeError):
            status = TaskStatus.PENDING
        metadata: dict[str, Any] = {}
        ac = state_fields.as_dict(row.get("task.acceptance_criteria"), field="task.acceptance_criteria")
        if ac:
            metadata["acceptance_criteria"] = ac
        eval_res = row.get("task.evaluation")
        if isinstance(eval_res, dict):
            metadata["evaluation"] = eval_res
        # 评估历史读回（进度判定/已过指标跳过的跨调用真值，见 _handle_evaluation_result）
        history = state_fields.as_list(row.get("task.evaluation_history"), field="task.evaluation_history")
        if history:
            metadata["evaluation_history"] = history
        # 评估耗尽计数读回（跨调用累积的单一真值在 state，见 _handle_evaluation_result）
        retry_counts = state_fields.as_dict(row.get("task.eval_retry_count"), field="task.eval_retry_count")
        if retry_counts:
            metadata["eval_retry_count"] = retry_counts
        total_calls = row.get("task.eval_total_calls")
        if total_calls is not None and total_calls != "":
            try:
                metadata["eval_total_calls"] = int(total_calls)
            except (TypeError, ValueError):
                pass
        # ws_meta 回填（数据源适配）：workspace 注入消费点（_get_input_params）读
        # task.metadata.ws_meta，0.2 权威在管道 state 单键 task.ws_meta（出生/
        # 创建时一次写全）。缺 ws_meta 时指标在错误目录解析相对路径（实测
        # file_check「不存在: result.txt」误判）。
        ws_meta = state_fields.as_dict(row.get("task.ws_meta"), field="task.ws_meta")
        if ws_meta:
            metadata["ws_meta"] = ws_meta
        # 血缘回填：出生协议只写管道坐标（lineage.parent_pipeline_id），GAP-1 下
        # 父任务 id = 父管道 id。
        parent_pipeline_id = row.get("lineage.parent_pipeline_id") or None
        # 创建基线读回（required_files 证据校验的 mtime/commit 判据源）：出生
        # 协议写 task.created_at（task_submit birth_state）。行缺此键（历史任
        # 务）如实空串——证据校验按基线缺失降级，不得用组装时刻冒充创建基线
        # （否则基线≈评估时刻，会把任务期产物全判过期）。
        return TaskModel(
            id=task_id,
            title=str(row.get("task.goal") or task_id),
            description=str(row.get("task.description") or ""),
            parent_task_id=parent_pipeline_id,
            parent_pipeline_id=parent_pipeline_id,
            status=status,
            created_at=str(row.get("task.created_at") or ""),
            metadata=metadata,
            result=None,
        )

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。

        委托到 tasks 平铺模块（plugins/shared/tasks/service_access.py）的
        公共接口（M3 后插件自包含单例，不再依赖 infrastructure.service_provider）。

        Returns:
            TaskService 实例，获取失败返回 None
        """
        from service_access import get_task_service  # noqa: PLC0415

        return get_task_service()

    def _create_executor(self, task_service: Any) -> Any:
        """获取评估执行器（显式构造注入优先，回退 server 装配的默认执行器）。

        0.2 生产执行器 = _executor.PipelineEvaluationExecutor（server.py on_load
        经 set_default_executor 装配）：tool 型指标本地执行，agent 型指标派
        评估子管道（R2：继承被评估任务的 workspace/执行环境）。两处皆未注入
        返回 None → 调用方返回 EVAL_ENGINE_UNAVAILABLE（文档化降级，不静默空转）。

        Args:
            task_service: TaskService 实例（签名兼容保留，执行器不再消费）

        Returns:
            评估执行器；未注入返回 None
        """
        return self._executor or _default_executor

    @staticmethod
    def _all_metrics_passed(task: Any, metric_ids: list[str]) -> bool:
        """检查所有声明指标是否都在历史记录中通过了。

        从 task.metadata.evaluation_history 中收集每个指标最近一次评估结果，
        判断是否所有指标都已通过。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            所有指标是否都已通过
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            return False

        # 收集每个指标最近一次评估结果
        latest: dict[str, bool] = {}
        for entry in history:
            metrics = entry.get("metrics", [])
            for m in metrics:
                mid = m.get("metric_id")
                if mid:
                    latest[mid] = m.get("passed", False)

        return all(latest.get(mid, False) for mid in metric_ids)

    @staticmethod
    def _get_eval_progress(
        task: Any,
        metric_ids: list[str],
    ) -> tuple[int, list[str]]:
        """获取评估进度：已通过数量和剩余未通过的指标 ID。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            (已通过数量, 未通过的指标 ID 列表)
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])

        latest: dict[str, bool] = {}
        if isinstance(history, list):
            for entry in history:
                metrics = entry.get("metrics", [])
                for m in metrics:
                    mid = m.get("metric_id")
                    if mid:
                        latest[mid] = m.get("passed", False)

        passed_count = sum(1 for mid in metric_ids if latest.get(mid, False))
        remaining = [mid for mid in metric_ids if not latest.get(mid, False)]
        return passed_count, remaining

    @staticmethod
    def _get_eval_timeout(task: Any) -> float:
        """根据任务元数据获取评估超时时间（秒）。

        优先使用 task.metadata.eval_timeout（允许单个任务自定义），
        默认 _DEFAULT_EVAL_TIMEOUT。
        """
        metadata = task.metadata if task.metadata else {}
        custom_timeout = metadata.get("eval_timeout")
        if custom_timeout is not None:
            try:
                return float(custom_timeout)
            except (TypeError, ValueError):
                pass
        return _DEFAULT_EVAL_TIMEOUT

    @staticmethod
    def _increment_eval_call_count(task: Any) -> int:
        """递增全局评估调用计数并返回当前值。

        每次 task_evaluate 工具被调用时执行，用于实现全局调用次数上限。
        计数值存储在 task.metadata["eval_total_calls"] 中。
        超过上限直接标记任务失败，防止 Agent 无限循环调用评估工具。

        Args:
            task: TaskModel 实例

        Returns:
            递增后的调用次数
        """
        if task.metadata is None:
            task.metadata = {}
        current = task.metadata.get("eval_total_calls", 0)
        if not isinstance(current, int):
            current = 0
        current += 1
        task.metadata["eval_total_calls"] = current
        return current

    def _get_metric_ids(self, task: Any) -> list[str]:
        """从任务模型中提取评估指标 ID 列表。

        Args:
            task: TaskModel 实例

        Returns:
            指标 ID 列表
        """
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            return task.metadata["evaluation_metric_ids"]
        if task.metadata and "acceptance_criteria" in task.metadata:
            ac = task.metadata["acceptance_criteria"]
            if isinstance(ac, dict):
                return list(ac.keys())
        return []

    def _get_input_params(self, task: Any) -> dict[str, dict[str, Any]]:
        """从任务模型的 acceptance_criteria 中提取各指标的输入参数。

        对于工具型评估指标（如 file_check），自动注入 workspace 参数，
        确保评估工具在正确的工作目录下解析文件路径。
        从 task.metadata 解析 workspace 绝对路径，注入到工具型评估指标的参数中，
        使 file_read 等工具在任务工作空间而非项目根目录查找文件。

        Args:
            task: TaskModel 实例

        Returns:
            key=metric_id, value=input_params 的字典
        """
        params, ac = self._extract_raw_input_params(task)

        all_metric_ids: set[str] = set()
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            all_metric_ids = set(task.metadata["evaluation_metric_ids"])
        if isinstance(ac, dict):
            all_metric_ids.update(ac.keys())

        # 直接从任务数据读取 ws_meta.path 作为 workspace
        ws_meta = (task.metadata or {}).get("ws_meta") if task.metadata else None
        workspace_abs: str | None = ws_meta.get("path") if ws_meta else None

        self._inject_workspace_and_placeholders(params, all_metric_ids, task, workspace_abs)
        self._apply_tool_id_templates(params, all_metric_ids, workspace_abs)
        self._apply_glob_templates(params, all_metric_ids, workspace_abs)
        return params

    @staticmethod
    def _extract_raw_input_params(
        task: Any,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """提取各指标的原始输入参数（占位符替换前的形态）。

        config.input_params 存在则直接采用；否则视为 LLM 把参数平铺在顶层，
        过滤 expected_output/pass_threshold/description 等已知非参数键后整体采纳。
        返回 (params, acceptance_criteria 原始对象——非 dict 形态归一为空 dict)。
        """
        params: dict[str, dict[str, Any]] = {}
        ac: dict[str, Any] = {}
        if task.metadata and "acceptance_criteria" in task.metadata:
            candidate = task.metadata["acceptance_criteria"]
            if isinstance(candidate, dict):
                ac = candidate
                non_param_keys = {"expected_output", "pass_threshold", "description"}
                for metric_id, config in candidate.items():
                    if isinstance(config, dict):
                        if "input_params" in config:
                            params[metric_id] = config["input_params"]
                        else:
                            params[metric_id] = {k: v for k, v in config.items() if k not in non_param_keys}
        return params, ac

    def _inject_workspace_and_placeholders(
        self,
        params: dict[str, dict[str, Any]],
        metric_ids: set[str],
        task: Any,
        workspace_abs: str | None,
    ) -> None:
        """workspace 注入 + {{workspace}}/{{task_id}} 与 {title} 占位符替换。

        {title} 取 task.title（派发侧契约：{title} 用任务名原文，不翻译不
        改写）；标题为空则不替换（保留原样，file_check 反馈可见定位）。

        criteria 不做任何兜底：未配置就是未配置（评估引擎按 input_params
        的检查语义判定，不消费 criteria；拿任务描述顶替只会伪造"有标准"
        假象）。未配置评估依据的指标在 _auto_complete 直接通过。
        """
        raw_title = getattr(task, "title", "")
        task_title = raw_title.strip() if isinstance(raw_title, str) else ""
        for metric_id in metric_ids:
            p = params.get(metric_id, {})
            if workspace_abs:
                p["workspace"] = workspace_abs
            for key, val in list(p.items()):
                if isinstance(val, str):
                    if workspace_abs:
                        val = val.replace("{{workspace}}", workspace_abs)  # noqa: PLW2901
                    val = val.replace("{{task_id}}", task.id)  # noqa: PLW2901
                    if task_title:
                        val = val.replace("{title}", task_title)  # noqa: PLW2901
                    p[key] = val
            params[metric_id] = p

    def _apply_tool_id_templates(
        self,
        params: dict[str, dict[str, Any]],
        metric_ids: set[str],
        workspace_abs: str | None,
    ) -> None:
        """解析参数值中的 ``{tool_id}`` 模板并原地写回。

        唯一候选可用，0/多候选拒绝——绝不"取第一个文件"猜测被评估工具，
        猜测会把评估打到错误对象，错误放行或错误失败。
        """
        tool_id_candidates = self._resolve_tool_id_candidates(workspace_abs)
        for metric_id in metric_ids:
            p = params.get(metric_id, {})
            uses_template = any(isinstance(val, str) and "{tool_id}" in val for val in p.values())
            if not uses_template:
                continue
            if len(tool_id_candidates) != 1:
                raise ValueError(
                    f"指标 {metric_id} 引用了 {{tool_id}} 模板，但工作区工具候选为 "
                    f"{len(tool_id_candidates)} 个（{'、'.join(tool_id_candidates) or '无'}），"
                    "无法确定被评估工具；请显式指定工具或清理工作区后重试"
                )
            for key, val in list(p.items()):
                if isinstance(val, str):
                    p[key] = val.replace("{tool_id}", tool_id_candidates[0])
            params[metric_id] = p

    @staticmethod
    def _resolve_tool_id_candidates(workspace_abs: str | None) -> list[str]:
        """收集工作空间 plugins/shared/ 下的插件 id 候选（排除 _ 前缀目录）。

        0.2 平铺布局：工具 = plugins/shared/<tool_id>/ 插件目录
        （plugin.json + Python 实现 + 测试，ADR 2026-10-13）。返回全部候选
        （按目录名排序）由调用方做唯一性裁决——本函数不做"取第一个"
        猜测，歧义交给调用方显式报错。

        Args:
            workspace_abs: 任务工作空间绝对路径（ws_meta.path），无则返回空列表。

        Returns:
            按目录名排序的工具 id 候选列表。
        """
        if not workspace_abs:
            return []
        from pathlib import Path  # noqa: PLC0415

        shared_dir = Path(workspace_abs) / "plugins" / "shared"
        if not shared_dir.is_dir():
            return []
        return sorted(
            child.name
            for child in shared_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith("_")
            and (child / "plugin.json").is_file()
        )

    def _apply_glob_templates(
        self,
        params: dict[str, dict[str, Any]],
        metric_ids: set[str],
        workspace_abs: str | None,
    ) -> None:
        """path 类参数中残余 {token} 占位符的工作区 glob 唯一匹配解析。

        {novel_title}/{chapter_number}/{chapter_title}/{agent_id} 等值在执行期
        由写入方按 goal 决定（中央静态无从取名），以工作区实际文件裁决：
        占位符段视作通配，唯一命中即采纳命中路径；0/多命中保留原样
        （file_check 反馈可见字面量定位，与 {tool_id} 唯一候选同口径，
        不做猜测）。含 ``{{`` 双花括号的值不进入本步（另一注入面）。
        """
        if not workspace_abs:
            return
        from pathlib import Path  # noqa: PLC0415

        ws = Path(workspace_abs)
        ws_prefix = str(ws).replace("\\", "/").rstrip("/") + "/"
        for metric_id in metric_ids:
            if metric_id not in params:
                continue
            p = params[metric_id]
            for key in ("path", "output_path"):
                val = p.get(key)
                if not isinstance(val, str) or "{{" in val:
                    continue
                if not _PATH_TOKEN_RE.search(val):
                    continue
                val_norm = val.replace("\\", "/")
                if val_norm.startswith(ws_prefix):
                    origin, raw = "abs", val_norm[len(ws_prefix):]
                elif not Path(val_norm).is_absolute():
                    origin, raw = "rel", val_norm
                else:
                    continue
                parts = _PATH_TOKEN_RE.split(raw)
                pattern = "*".join(glob.escape(part) for part in parts)
                matches = sorted(ws.glob(pattern))
                if len(matches) != 1:
                    continue
                hit = matches[0]
                p[key] = str(hit) if origin == "abs" else hit.relative_to(ws).as_posix()

    def _build_result_data(self, result: Any) -> dict[str, Any]:
        """将评估结果构建为工具返回数据。

        包含评估器输入/输出、Agent 评估的结构化反馈（issues/suggestions/
        report_path）和管道 ID，便于 LLM 直接定位问题并修复。

        Args:
            result: EvaluationResult 实例

        Returns:
            可序列化的结果字典
        """
        metrics = []
        for r in result.results:
            if r.passed:
                metrics.append(
                    {
                        "metric_id": r.metric_id,
                        "passed": True,
                    }
                )
            else:
                m: dict[str, Any] = {
                    "metric_id": r.metric_id,
                    "passed": False,
                    "score": r.score,
                    "message": r.message,
                    "error": r.error,
                }
                # Agent 评估的结构化反馈
                if r.evaluator_output:
                    eo = r.evaluator_output
                    if eo.get("issues"):
                        m["issues"] = eo["issues"]
                    if eo.get("suggestions"):
                        m["suggestions"] = eo["suggestions"]
                    if eo.get("report_path"):
                        m["report_path"] = sanitize_eval_paths(eo["report_path"])
                # 期望条件失败的详细信息
                if r.details and isinstance(r.details, dict):
                    failed = r.details.get("failed_conditions")
                    if failed:
                        m["failed_conditions"] = failed
                if r.evaluator_input:
                    m["evaluator_input"] = sanitize_eval_paths(r.evaluator_input)
                if r.evaluator_output:
                    m["evaluator_output"] = sanitize_eval_paths(r.evaluator_output)
                if r.pipeline_run_id:
                    m["pipeline_run_id"] = r.pipeline_run_id
                metrics.append(m)
        return {
            "task_id": result.task_id,
            "overall_passed": result.overall_passed,
            "summary": result.summary,
            "metrics": metrics,
        }

    @staticmethod
    def _build_rich_summary(result: Any) -> str:
        """构建富评估摘要（0.1 evaluation.mapper.build_summary 同款）。

        首行汇总 + 逐指标说明（agent 型评估 = 评估 Agent 提交的 feedback
        总结，脚本化评估 = 检查结果 message）。无指标结果时回退 result.summary
        ——那是 LLM 经 task_evaluate 入参提交的任务完成总结（"直接通过"
        分支无逐指标说明，不能输出空泛的 0/0）。
        """
        total = len(result.results)
        if total == 0:
            return str(getattr(result, "summary", "") or "").strip()

        lines: list[str] = []
        passed = sum(1 for r in result.results if r.passed)
        lines.append(f"评估结果: {passed}/{total} 指标通过")

        for r in result.results:
            status = "✅ PASS" if r.passed else "❌ FAIL"
            msg = (r.message or "").strip()
            if msg:
                lines.append(f"  {status} {r.metric_id}: {msg}")
            else:
                lines.append(f"  {status} {r.metric_id}")

        return "\n".join(lines)
