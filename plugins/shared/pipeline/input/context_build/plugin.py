"""上下文构建 Input 插件 — agent 配置加载与上下文装配的唯一归口。

负责在管道循环的输入阶段构建上下文信息：按 state.agent_id 加载
agent yaml（自持配置，内核只留 tool_ids 窄接口），将提示词骨架、
agent 名称/层级、会话元数据等写入 state，
供后续插件（prompt_build、knowledge_inject 等）和 Core 读取。

State 命名空间：
    - context.* : 本插件写入的上下文字段
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class ContextBuildPlugin(IInputPlugin):
    """上下文构建 Input 插件。

    将 agent 配置（yaml 解析结果）、层级信息、会话元数据等组装为
    管道执行所需的上下文字段写入 state；本插件只构建"上下文数据"，
    不管理服务实例（服务经 PluginContext.get_service 获取）。

    优先级：10（准备级，先于其他 Input 插件执行）；最小上下文也能跑。

    Attributes:
        _config: 插件配置字典
        _system_prompt: 系统 prompt 模板
        _agent_name: Agent 名称
        _agent_level: Agent 层级
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文构建插件。

        Args:
            config: 插件配置字典，支持以下键：
                - system_prompt: 系统 prompt 模板
                - agent_name: Agent 名称
                - agent_level: Agent 层级 (l1_main/l2_subtask/l3_atomic)
                - extra_context: 额外上下文字典
        """
        self._config = config or {}
        self._system_prompt = self._config.get("system_prompt", "")
        self._agent_name = self._config.get("agent_name", "")
        self._agent_level = self._config.get("agent_level", "L1")
        self._extra_context = self._config.get("extra_context", {})
        # agent 配置自持：内核只把 agent_id 放进 state，
        # 加载 config/agents/**/<agent_id>.yaml 是本插件（sidecar）的职责。
        # 缓存：yaml 路径 → 解析结果（mtime 失效），进程内复用。
        self._agent_yaml_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _find_agent_yaml(self, agents_dir, agent_id: str):
        """在 agents_dir 下递归找 <agent_id>.yaml；未命中回退按 config_id 匹配。

        Returns:
            (path, mtime) 或 None。config_id 匹配覆盖执行 agent 文件名与
            config_id 不同的常态（code_writer.yaml / config_id=code_writer_agent）。
        """
        from pathlib import Path

        target = f"{agent_id}.yaml"
        fallback: list = []
        for p in Path(agents_dir).rglob("*.yaml"):
            if p.name == target:
                return p, p.stat().st_mtime
            fallback.append(p)
        for p in fallback:
            try:
                with open(p, encoding="utf-8") as f:
                    head = f.read(4096)
            except OSError:
                continue
            m = re.search(r"^config_id:\s*(\S+)\s*$", head, re.M)
            if m and m.group(1) == agent_id:
                return p, p.stat().st_mtime
        return None

    def _load_agent_config(self, agent_id: str) -> dict[str, Any]:
        """按 agent_id 加载 agent yaml（缓存 + mtime 失效）。

        目录不存在 / 未命中 yaml → 返回空 dict（无配置属合法形态，按默认运行）；
        yaml 存在但读取/解析失败 → 上抛终止：此时把人格/system_prompt/tool_ids
        整体静默换成默认值 = 配置错误被伪装成"无配置 agent"，必须让管道失败可见。

        Raises:
            RuntimeError: agent yaml 读取或解析失败（原异常经 __cause__ 保留）。
        """
        import os
        import yaml as _yaml
        from pathlib import Path

        if not agent_id:
            return {}
        root = os.environ.get("AGENTOS_CONFIG_ROOT", "")
        agents_dir = Path(root) / "agents" if root else None
        if agents_dir is None or not agents_dir.is_dir():
            logger.debug(
                "[context_build] agents 目录不存在（按默认配置运行）| root=%s", root
            )
            return {}
        found = self._find_agent_yaml(agents_dir, agent_id)
        if found is None:
            logger.debug("[context_build] 未找到 agent yaml（按默认配置运行）| agent_id=%s", agent_id)
            return {}
        path, mtime = found
        cached = self._agent_yaml_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
        except Exception as exc:
            raise RuntimeError(
                f"[context_build] agent yaml 解析失败，管道终止 | path={path} | agent_id={agent_id}"
            ) from exc
        if not isinstance(data, dict):
            data = {}
        self._agent_yaml_cache[str(path)] = (mtime, data)
        return data

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_build"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return self._config.get("priority", 10)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """构建上下文信息并写入 state。

        从 state 读取已有的 session_id、task_id 等字段，
        结合插件配置中的 system_prompt、agent_name 等，
        组装为完整的上下文字段写入 state。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含上下文状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行上下文构建逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            要写入 state 的上下文字段字典
        """
        updates: dict[str, Any] = {}

        # 同 sidecar 实例被多个 agent 管道连续复用：先复位实例层级默认值，
        # 再按本管道 agent yaml 覆盖（下方第 2 步）——前一 agent 的 level
        # 不得残留到下一 agent。
        self._agent_level = self._config.get("agent_level", "L1")

        # 1. 系统提示词：优先 state 注入；否则按 state.agent_id 加载 agent yaml
        #    （agent 配置自持——内核不再读 yaml，只负责把 agent_id 放进 state）；
        #    最终回退插件配置默认。
        agent_cfg = self._load_agent_config(str(ctx.state.get("agent_id", "") or ""))
        updates["context.system_prompt"] = (
            ctx.state.get("system_prompt", "")
            or str(agent_cfg.get("system_prompt", "") or "")
            or self._system_prompt
        )
        # agent 名称每次执行按当前管道 agent 重新解析（不缓存到实例属性——
        # 同 sidecar 进程内多个管道复用本插件实例，缓存会造成 agent_name
        # 跨管道污染）。实例属性只作配置默认值，不缓存解析结果。
        agent_name = self._agent_name or str(agent_cfg.get("display_name") or "")
        # tool_ids 随 agent 配置注入（内核 inject_tool_schemas 读 state.tool_ids
        # 过滤；agent 配置加载归本插件后由这里供给）。
        _tool_ids = agent_cfg.get("tool_ids")
        if isinstance(_tool_ids, list) and _tool_ids:
            updates["tool_ids"] = _tool_ids
        # dynamic_vars 随 agent 配置装载：agent yaml 的
        # dynamic_vars.items → context.dynamic_vars，prompt_build 据此走配置
        # 驱动路径（配置声明的 {{timestamp}}/{{path:}} 依赖此装载）。
        _dv = agent_cfg.get("dynamic_vars")
        if isinstance(_dv, dict) and _dv.get("enabled", True):
            _items = _dv.get("items")
            if isinstance(_items, list) and _items:
                updates["context.dynamic_vars"] = _items
        # static_vars 随 agent 配置装载：agent yaml 的 static_vars.items →
        # context.static_vars，prompt_build 以占位符式解析后拼在 system 内容
        # 之后（Agent 匹配决策/工具索引/{{path:用户档案}} 等）。
        _sv = agent_cfg.get("static_vars")
        if isinstance(_sv, dict) and _sv.get("enabled", True):
            _sv_items = _sv.get("items")
            if isinstance(_sv_items, list) and _sv_items:
                updates["context.static_vars"] = _sv_items
        # 约束随 agent 配置装载（注入式，不走 state 通用键）：yaml
        # hard_constraints/soft_constraints 直接渲染为约束文本块注入
        # system 内容尾部——无 state["constraints"] 中转（该键无人写入，
        # 走中转会重蹈"配置写了但 agent 看不见"断链）。
        _hard = agent_cfg.get("hard_constraints")
        _soft = agent_cfg.get("soft_constraints")
        if isinstance(_hard, list) and _hard or isinstance(_soft, list) and _soft:
            lines = [f"- [必须] {c}" for c in _hard if isinstance(c, str)] if isinstance(_hard, list) else []
            lines += [f"- [建议] {c}" for c in _soft if isinstance(c, str)] if isinstance(_soft, list) else []
            if lines:
                updates["context.constraints_text"] = "\n".join(lines)
        # 运行时参数随 agent 配置装载（与 tool_ids 同装配点）：yaml 声明值
        # 注入顶层 state 键，stop_check（max_iterations/timeout_seconds）、
        # task_reminder（max_reminders）、llm_core（model_tier）读 state 消费；
        # state 已有非空显式值（overlay/上游注入）优先，不覆盖；空串/None
        # 视为未设置——step context 模板对缺失键渲染出 "" 且先于本插件落
        # state，按"键存在即显式值"判优会令 yaml 值永远装不进去（llm_core
        # 落 defaults.chat 兜底模型）。-1 = 无限制，原值透传（stop_check
        # 对 -1 有显式语义）。
        for _key in ("model_tier", "max_iterations", "max_reminders", "timeout_seconds"):
            if ctx.state.get(_key) in (None, "") and agent_cfg.get(_key) is not None:
                updates[_key] = agent_cfg[_key]
        # agent 层级：yaml level（如 code_writer L3）覆盖插件默认 L1——子任务
        # 管道按目标 agent 定层级（L1 豁免会让 task_reminder 评估闸门旁路）。
        # 插件配置显式 agent_level 仍最高优先。
        if agent_cfg.get("level") and self._config.get("agent_level") is None:
            lvl = str(agent_cfg["level"]).strip().upper()
            if lvl.startswith("L"):
                self._agent_level = lvl

        # 2. Agent 身份信息（层级单一真值：顶层 agent_level，level_guard/
        # isolation_guard/tool_schema/param_inject 等下游统一读此键）
        updates["context.agent_name"] = agent_name

        # 始终用实际 Agent 层级覆盖 state 中的 AGENT_LEVEL，
        # 防止子管道继承父管道的层级（如 L2 agent 错误继承 L1）。
        updates[StateKeys.AGENT_LEVEL] = self._agent_level

        # 3. 会话元数据
        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        updates["context.session_id"] = session_id
        updates["context.task_id"] = task_id

        # 4. 迭代信息
        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        updates["context.iteration"] = iteration

        # 5. 额外上下文
        if self._extra_context:
            for key, value in self._extra_context.items():
                updates[f"context.{key}"] = value

        # 6. 工具执行标记（从 core_type 推断）
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        updates["context.is_tool_execution"] = core_type == "tool_execute"

        # 7. 项目级标记
        updates["context.is_project"] = self._agent_level == "L1"

        logger.debug(
            "[%s] Context built | agent=%s | level=%s | iteration=%d",
            self.name,
            self._agent_name,
            self._agent_level,
            iteration,
        )

        return updates
