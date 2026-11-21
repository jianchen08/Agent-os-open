"""Harbor BaseAgent 适配器（AgentOSHarborAgent）：Terminal-Bench 参赛形态。

参赛形态（已定裁定）：内核在宿主跑，执行面指向 Harbor 起的任务容器——
- setup：解析任务容器（hostname 自证 + docker inspect）→ 建宿主工作空间
  （guard 悬空挂载防护要求真实存在路径）→ 经内核 REST 绑定外部容器
  （fail-loud：容器/内核不可达即抛错，trial 判失败，绝不静默降级）；
- run（R17 双通道，缺省任务接口）：instruction 经任务接口建真实任务
  （显式 workspace + plain + isolated——绑定键与任务管道 state.workspace
  对齐，task_reminder/评估闸门/失败轮回 route 8 原生参与）→ 轮询
  task.status 至终态；dispatch_mode="session" 保留 WS 会话链路（R6-R16
  既有形态，向后兼容）。终态回填 AgentContext.metadata 审计坐标后交还
  控制权（task 终态 completed/failed 如实透传，容器终态归 verifier 判分）。

接口真源 = 本地安装 harbor 包的 BaseAgent（0.26.0，Ubuntu 侧 tb-venv；抽象
契约 name/version/setup/run 与 0.26 以下兼容）。本模块 import harbor 是硬
依赖（BaseAgent 子类必须在类定义期拿到基类），harbor 缺席时本模块不可导入
——这是 harbor agent 的天然属性，不是缺陷。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

# 包目录自推 sys.path（裸名平铺 import，包形态/文件形态两种加载上下文同跑）
_PKG_DIR = str(Path(__file__).resolve().parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from container_probe import resolve_container_async  # noqa: E402
from harbor.agents.base import BaseAgent  # noqa: E402
from harbor.agents.capabilities import AgentCapabilities  # noqa: E402
from harbor.agents.options import AgentOptions  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402
from harbor.models.bridge import BridgeKind  # noqa: E402
from kernel_bridge import (  # noqa: E402
    CONTAINER_INSTRUCTION_PATH,
    KernelBridge,
    KernelBridgeError,
    compose_task_description,
    outcome_to_context_metadata,
)
from pydantic import Field  # noqa: E402

logger = logging.getLogger(__name__)

#: 会话执行身份（R10 方向裁定：评测 = 内置代码 agent 原样——
#: config/agents/executor/general_agent.yaml，工具面/系统提示/mode 注入全部
#: 走产品原生形态，不定制不瘦身；测评对象就是产品真实形态。历史
#: tb_eval_agent.yaml 保留但评测不再使用）。
DEFAULT_EVAL_AGENT_ID = "general_agent"

#: kernel_url 兜底链末端（dev 内核缺省端口；env AGENTOS_KERNEL_URL 实例期优先）
DEFAULT_KERNEL_URL = "http://127.0.0.1:9101"

#: 派发通道合法值（R17）：task = 任务接口产品原样（缺省）；session = WS 会话
#: （R6-R16 既有链路，向后兼容保留）。
DISPATCH_MODES = ("task", "session")


class AgentOSAgentOptions(AgentOptions):
    """用户面参数（harbor run -a ... -c '{"kernel_url": ...}' 或环境变量注入）。

    kernel_url 缺省**不在类定义期读 env**（pydantic Field default 求值一次，
    会让测试进程内的 monkeypatch.setenv 失效——实证踩坑：默认值钉死 9101
    差点连上宿主真内核）。空串 = 实例期走 env 兜底链（见 _resolve_kernel_url）。

    口令只走环境变量 AGENTOS_ADMIN_PASSWORD（spawn_test_kernel 既有机制），
    不进 options schema（避免 trajectory/日志泄密面）。
    """

    kernel_url: str = Field(
        default="",
        description="AgentOS 内核 HTTP 基址（空 = AGENTOS_KERNEL_URL env → "
        f"{DEFAULT_KERNEL_URL}）",
    )
    kernel_username: str = Field(default="admin", description="内核登录用户名")
    agent_id: str = Field(
        default=DEFAULT_EVAL_AGENT_ID,
        description="会话执行身份（config/agents 下 agent 配置加载键；缺省=内置代码 agent 原样）",
    )
    poll_timeout_s: float = Field(
        default=0.0,
        description="终态轮询上限（秒）。0 = 未表达。解析链（R19）："
        "trial 的 task.toml [agent] timeout_sec（评测基准自己的配置，第一"
        "优先）→ 本 option → AGENTOS_POLL_TIMEOUT_S env → 1800 缺省。"
        "本 option 只作 task.toml 缺席时的兜底，勿作缺省写非零值"
        "（R16 实证：Field 缺省 1800 短路整条链）",
    )
    stream_timeout_s: float = Field(
        default=1800.0, description="WS 收流上限（秒；仅 dispatch_mode=session 用）"
    )
    workspace_root: str = Field(
        default="",
        description="宿主工作空间根目录（空 = logs_dir/agentos-workspaces）",
    )
    dispatch_mode: str = Field(
        default="task",
        description="派发通道（R17）：task = 任务接口产品原样（建真实任务，"
        "task_reminder/评估闸门/route 8 原生参与，轮询 task.status）；"
        "session = WS 会话派发（R6-R16 既有链路，向后兼容保留）",
    )


def _resolve_kernel_url(opts_kernel_url: str) -> str:
    """kernel_url 解析：显式 option > AGENTOS_KERNEL_URL env > dev 缺省端口。

    实例期求值（env 变更即时生效）；空串/空白视同未表达。
    """
    explicit = (opts_kernel_url or "").strip()
    if explicit:
        return explicit
    from_env = (os.environ.get("AGENTOS_KERNEL_URL") or "").strip()
    if from_env:
        return from_env
    return DEFAULT_KERNEL_URL


#: 轮询预算缺省（秒，= 30min）：解析链末端兜底（R19 起位次在 task.toml /
#: option / env 之后）。
DEFAULT_POLL_TIMEOUT_S = 1800.0


def read_task_toml_agent_timeout_s(trial_dir: Path) -> float | None:
    """解析评测基准自己的 agent 预算：trial_dir/lock.json → task.path →
    task.toml [agent] timeout_sec（R19 第一优先源）。

    harbor 在 Trial 构造期就把解析后的任务坐标写进 trial_dir/lock.json
    （trial.py `_write_trial_lock`，先于 agent setup/run）；本地任务
    task.path = 任务目录，task.toml 与其同址。任一环缺失/解析失败返回
    None（预算链降级到下一源；探针面不拍定任何兜底值——「不要人为拍时间，
    以评测基准自己的配置为准」）。
    """
    lock_path = trial_dir / "lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    task = lock.get("task") if isinstance(lock, dict) else None
    task_path = task.get("path") if isinstance(task, dict) else None
    if not task_path:
        return None
    try:
        config = tomllib.loads((Path(task_path) / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    agent = config.get("agent")
    timeout = agent.get("timeout_sec") if isinstance(agent, dict) else None
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        return None
    if timeout <= 0:
        return None
    return float(timeout)


def _resolve_poll_timeout(
    opts_poll_timeout_s: float | None,
    task_timeout_s: float | None = None,
) -> float:
    """轮询预算解析链：task.toml [agent] timeout_sec > 显式 option > env >
    缺省 30min（R19；R8 链降级为兜底）。

    第一优先源 = 评测基准自己的配置（task.toml，harbor trial 同源执法该
    预算）——用户裁定「不要人为拍时间」：env 人为拉长（5400）与 option
    拍定全部降级为 task.toml 缺席时的兜底。非正数/非法值视同未表达。
    """
    if task_timeout_s is not None and task_timeout_s > 0:
        return float(task_timeout_s)
    explicit = opts_poll_timeout_s if isinstance(opts_poll_timeout_s, (int, float)) else 0
    if explicit and float(explicit) > 0:
        return float(explicit)
    from_env = (os.environ.get("AGENTOS_POLL_TIMEOUT_S") or "").strip()
    if from_env:
        try:
            value = float(from_env)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_POLL_TIMEOUT_S


class AgentOSHarborAgent(BaseAgent):
    """AgentOS 整系统 Harbor 适配器。

    一次 trial 的生命周期：Harbor 起任务容器 → setup 解析容器并绑定 →
    run 发 instruction 给内核跑管道 → 管道终态后交还控制权。容器内一切
    副作用留在容器里（TB verifier 判分对象），宿主只新增一个空工作空间目录。
    """

    capabilities = AgentCapabilities(
        atif=False,
        resume=False,
        load_native_trajectory=False,
        load_atif_trajectory=False,
        handoff=False,
        native_config=False,
        windows=False,
        bridges=frozenset[BridgeKind](),
    )
    options_model = AgentOSAgentOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._container_ref: Any | None = None
        self._workspace: Path | None = None
        self._bridge: KernelBridge | None = None

    @staticmethod
    def name() -> str:
        """Harbor agent 注册名（harbor run --agent 引用用）。"""
        return "agentos"

    def version(self) -> str | None:
        """适配器版本（随台账轮次推进）。"""
        return "0.1.0"

    # ── 内部构造面 ──────────────────────────────────────────────

    def _make_bridge(self) -> KernelBridge:
        """按 options 构造内核桥（password 只走环境变量）。"""
        opts: AgentOSAgentOptions = self.options  # type: ignore[assignment]
        return KernelBridge(
            base_url=_resolve_kernel_url(opts.kernel_url),
            username=opts.kernel_username,
        )

    def _prepare_workspace(self) -> Path:
        """建宿主工作空间（guard 悬空挂载防护要求真实存在路径）。"""
        opts: AgentOSAgentOptions = self.options  # type: ignore[assignment]
        root = Path(opts.workspace_root) if opts.workspace_root else self.logs_dir / "agentos-workspaces"
        ws = root / f"tb-{uuid.uuid4().hex[:12]}"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    # ── BaseAgent 契约 ──────────────────────────────────────────

    async def setup(self, environment: Any) -> None:
        """解析容器 + 建工作空间 + 绑定外部容器（全部 fail-loud）。

        绑定放 setup（run 之前）：内核/容器不可达在烧任何 LLM token 之前
        就显式失败，trial 直接判错——静默进 run 只会把失败推迟到烧完 token。
        """
        self._container_ref = await resolve_container_async(environment)
        logger.info(
            "[AgentOSHarborAgent] 任务容器解析完成 | name=%s | workdir=%s",
            self._container_ref.name,
            self._container_ref.workdir,
        )
        self._workspace = self._prepare_workspace()
        self._bridge = self._make_bridge()
        bind_result = self._bridge.bind_external_container(
            task_id=f"tb-{uuid.uuid4().hex[:12]}",
            workspace=str(self._workspace),
            container=self._container_ref.name,
            workdir=self._container_ref.workdir,
        )
        # 规范 workspace 采用（R7）：跨宿主编排时（适配器在编排器宿主、内核在
        # 另一宿主），本地 workspace 路径在内核侧不存在——bind 端会落内核侧
        # 工作空间锚并把规范路径回传；guard 的悬空挂载预检据此放行，绑定
        # 查找才可达。同宿主形态（B2 e2e）bind 回传原路径，行为不变。
        canonical_ws = (bind_result or {}).get("workspace")
        if canonical_ws:
            self._workspace = Path(str(canonical_ws))
        logger.info(
            "[AgentOSHarborAgent] 外部容器已绑定 | container=%s | workspace=%s",
            self._container_ref.name,
            self._workspace,
        )

    async def _stage_instruction_file(self, environment: Any, instruction: str) -> str:
        """把原题 base64 直写容器内指令文件（指针模式，fail-loud）。

        base64 编码后经 printf | base64 -d 落盘——指令文本可含任意引号/
        反斜杠/$，shell 面只过 base64 安全字符集，零转义歧义。rc != 0 显式
        失败（trial 判错），绝不静默降级为内联（会撞 task_submit 2000 字闸）。
        """
        encoded = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
        cmd = (
            "mkdir -p /workspace && "
            f"printf %s {encoded} | base64 -d > {CONTAINER_INSTRUCTION_PATH}"
        )
        result = await environment.exec(cmd, timeout_sec=30)
        rc = getattr(result, "return_code", None)
        if rc != 0:
            stderr = str(getattr(result, "stderr", ""))[:300]
            raise RuntimeError(
                f"指令文件写入容器失败（rc={rc}）: {stderr} "
                f"| path={CONTAINER_INSTRUCTION_PATH}"
            )
        return CONTAINER_INSTRUCTION_PATH

    async def run(
        self,
        instruction: str,
        environment: Any,
        context: AgentContext,
    ) -> None:
        """发 instruction 给内核跑至终态，回填 context 后交还控制权。

        R17 双通道：缺省任务接口（建真实任务 → 轮询 task.status；task.id
        在场 → task_reminder/评估闸门/route 8 原生参与，纯文本轮不再静默
        完成）；dispatch_mode="session" 保留 WS 会话链路（向后兼容）。

        harbor 对 agent 只有 setup/run 两钩子（trial.py 实读，无 cleanup）——
        收尾（解绑定，只删登记不碰容器）在 run 内 try/finally 完成：成功与
        失败路径都要解绑，失败信息在清理后照常抛出（trial 判失败）。
        """
        if self._bridge is None or self._workspace is None or self._container_ref is None:
            raise RuntimeError(
                "run 前必须先 setup（容器解析/工作空间/绑定三件缺一即显式失败）"
            )
        opts: AgentOSAgentOptions = self.options  # type: ignore[assignment]
        if opts.dispatch_mode not in DISPATCH_MODES:
            raise RuntimeError(
                f"dispatch_mode 非法: {opts.dispatch_mode!r}（合法值 {DISPATCH_MODES}）"
            )
        session_label = self.session_id or f"agentos-{uuid.uuid4().hex[:8]}"
        # R19：轮询预算第一优先源 = 评测基准自己的 task.toml（[agent]
        # timeout_sec，harbor trial 同源执法）；logs_dir = trial_dir/agent，
        # lock.json 在 trial_dir（Trial 构造期写入，先于本钩子）。
        task_timeout_s = read_task_toml_agent_timeout_s(self.logs_dir.parent)
        poll_budget = _resolve_poll_timeout(opts.poll_timeout_s, task_timeout_s)
        logger.info(
            "[AgentOSHarborAgent] 轮询预算解析 | budget=%ss | source=%s",
            poll_budget,
            "task.toml" if task_timeout_s and task_timeout_s > 0 else "option/env/缺省",
        )
        try:
            instruction_file_path = ""
            if opts.dispatch_mode == "task":
                _, needs_file = compose_task_description(instruction, poll_budget)
                if needs_file:
                    instruction_file_path = await self._stage_instruction_file(
                        environment, instruction
                    )
            outcome = await self._bridge.run_task(
                instruction,
                workspace=str(self._workspace),
                container_name=self._container_ref.name,
                agent_id=opts.agent_id,
                title=f"TB trial {session_label}",
                stream_timeout_s=opts.stream_timeout_s,
                poll_timeout_s=poll_budget,
                dispatch_mode=opts.dispatch_mode,
                instruction_file_path=instruction_file_path,
            )
            context.metadata = outcome_to_context_metadata(outcome)
            if not outcome.ok:
                raise RuntimeError(self._non_converged_detail(outcome))
            logger.info(
                "[AgentOSHarborAgent] 管道已收敛，控制权交还 Harbor"
                " | mode=%s | pipeline=%s | status=%s",
                outcome.dispatch_mode,
                outcome.pipeline_id,
                outcome.task_status if outcome.dispatch_mode == "task" else outcome.run_status,
            )
        finally:
            try:
                self._bridge.unbind_external_container(workspace=str(self._workspace))
            except KernelBridgeError as exc:
                logger.warning("[AgentOSHarborAgent] unbind 失败（忽略）: %s", exc)

    @staticmethod
    def _non_converged_detail(outcome: Any) -> str:
        """未收敛 raise 消息（按通道分形，坐标全量入消息面）。"""
        if outcome.dispatch_mode == "task":
            detail = (
                "AgentOS 任务未正常收敛 | mode=task"
                f" | task_status={outcome.task_status} | task={outcome.task_id}"
            )
            if outcome.task_error:
                detail += f" | error={outcome.task_error}"
            return detail
        detail = (
            f"AgentOS 管道未正常收敛 | terminal={outcome.stream_terminal} "
            f"| run_status={outcome.run_status} | pipeline={outcome.pipeline_id}"
        )
        return detail
