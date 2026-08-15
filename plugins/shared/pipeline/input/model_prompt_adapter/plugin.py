"""模型提示词适配 Input 插件（通用消息注入规则执行器）。

背景：部分模型的思考行为对对话分布敏感——如 DeepSeek v4 系列的思维链
在历史中存在 assistant(reasoning_content) 痕迹时更容易延续，官方仅在
特定客户端环境验证过触发。本插件把"什么条件下往 messages 注入什么消息"
收敛为配置规则（rules.yaml），按模型 / 管道状态路由，无命中规则时
零副作用透传。

注入走 messages op 协议（insert，随历史落库）。reasoning_content 字段
在发送链路全程存活：llm_core._build_messages 只剥 seq/tool_result/
_context_form；deepseek provider adapter 仅对带 tool_calls 的 assistant
采样清空，纯 assistant 的 reasoning_content 原样透传——这正是 tool 轮
回传 rc 的原生格式，不是旁路 hack。

设计纪律（防"过拟合补丁化"，每模型症状打一个专用插件的老路）：
- 每条规则显式声明 when/inject，可 enabled 开关，失效即删
  （rules.yaml 注释须记录示例内容的风格依据）；
- 注入内容放插件自持 rules.yaml，归管理员配置面——能插
  assistant+reasoning_content 等于能伪造模型输出历史，不开放为
  用户级运行时定制点；
- 仅任务首轮注入（len(messages) <= 1 判据，自幂等无状态），只在
  历史头部（system 之后的第一个位置）插入；末尾不开放（assistant
  是 prefill 续写 Beta，user 与 dynamic_vars 职责重叠）；
- 不做每轮重插——示例的使命是在任务开头建立思维链分布痕迹，
  模型输出过真实 reasoning_content 后由真实痕迹接管，压缩裁掉也不补。
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# 注入消息允许字段：白名单之外的配置字段丢弃（防奇怪字段漏进 API 载荷，
# 发送侧 _build_messages 只剥 seq/tool_result/_context_form，不认识的一律透传）
_ALLOWED_FIELDS = ("role", "content", "reasoning_content", "name")
# 角色白名单：system 会被 MiniMax 强转 user 且与 system_message 设计冲突；
# tool 会触发 tool_call 配对校验（孤儿清理）。两者语义都不属于"提示词适配"
_ALLOWED_ROLES = ("user", "assistant")


class ModelPromptAdapterPlugin(IInputPlugin):
    """按模型/状态路由的消息注入规则执行器。

    首轮窗口（len(messages) <= 1）内取首个命中的规则注入；
    同轮多规则命中按 rules 顺序取第一条（模型路由本质互斥，
    多规则叠加的排列语义对配置作者不可推理）。
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """初始化并加载规则。

        Args:
            config: 插件配置字典，支持以下键：
                - rules: 内联规则列表（优先级最高，测试/覆写用）
                - rules_path: 规则文件路径（相对路径基于插件目录）
                - default_model: state 无 model_id/model_tier 时的模型
                  匹配回退值（与 llm.yaml defaults.chat 对齐，由挂载
                  配置传入；llm_core 选模优先级 state.model_id >
                  model_tier > defaults.chat，本插件保持同序回退）
        """
        self._config = config or {}
        self._default_model = str(self._config.get("default_model") or "")
        self._rules: list[dict[str, Any]] = self._load_rules()

    @property
    def name(self) -> str:
        return "model_prompt_adapter"

    @property
    def priority(self) -> int:
        return 55

    # ── 规则加载 ──

    def _load_rules(self) -> list[dict[str, Any]]:
        """加载规则：config 内联 > rules_path > 插件目录 rules.yaml。"""
        inline = self._config.get("rules")
        if isinstance(inline, list):
            return [r for r in inline if isinstance(r, dict)]

        raw_path = self._config.get("rules_path")
        if raw_path:
            path = Path(raw_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent / path
        else:
            path = Path(__file__).resolve().parent / "rules.yaml"

        if not path.exists():
            logger.warning("[model_prompt_adapter] 规则文件不存在，透传: %s", path)
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules = data.get("rules") or []
        if not isinstance(rules, list):
            logger.warning("[model_prompt_adapter] rules 字段非列表，透传")
            return []
        return [r for r in rules if isinstance(r, dict)]

    # ── 管道注入（prepare 链调用）──

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """任务首轮（messages 至多一条）且规则命中时，在历史头部插入消息组。

        首轮判据 len(messages) <= 1：system_message 是独立 state 段不占
        messages 位，任务首轮历史即刚进来的那条 user 消息。插入后长度
        必然 > 1，后续轮次（工具轮 / LLM 轮 / 压缩后 / run 恢复）天然
        不再命中——判据自幂等，无状态标记、无指纹扫描。不做每轮重插：
        示例的使命是在任务开头建立思维链分布痕迹，模型输出过真实
        reasoning_content 后由真实痕迹接管，压缩裁掉也不补。
        """
        messages = [m for m in (ctx.state.get("messages") or []) if isinstance(m, dict)]
        if len(messages) > 1:
            return PluginResult()

        for rule in self._rules:
            if not rule.get("enabled", True):
                continue
            if not self._match_when(rule.get("when") or {}, ctx.state):
                continue

            inject = self._sanitize(rule.get("inject") or [])
            if not inject:
                continue

            # head：at 从 0 递增（引擎按序应用，原历史后段 seq 顺延）——
            # system + 示例轮构成稳定 cache 前缀
            ops = [{"op": "insert", "at": i, "msg": dict(m)} for i, m in enumerate(inject)]
            return PluginResult(state_updates={"messages": {"_ops": ops}})

        return PluginResult()

    # ── 匹配与注入构造 ──

    def _match_when(self, when: dict[str, Any], state: dict[str, Any]) -> bool:
        """规则命中判断：model_id 与 state 条件全部满足。

        model_id 匹配候选 = state.model_id > state.model_tier >
        config.default_model（与 llm_core 选模优先级同序）；任一候选
        命中任一 pattern 即通过。

        注意（实测 2026-08-15）：state.model_id 在真实运行时**恒不存在**
        （initial_state / agent yaml / WS 消息均无 model 字段），实际选模
        走 model_tier → llm.yaml defaults.chat（llm_core 内部解析，prepare
        链看不到结果）。因此 model_id 条件必须配合插件 config 的
        default_model 接线（与 defaults.chat 同步），否则永不命中。

        state 条件为任意 state 键的等值/fnmatch 通配匹配，全部满足才
        通过。值规范化：布尔 true/false（yaml 写法与 Python 值通吃）、
        数字 str 化、None/缺键 → 空串（规则写 ``""`` 或 ``*`` 可匹配缺键）。
        """
        model_pat = when.get("model_id")
        if model_pat is not None:
            patterns = [model_pat] if isinstance(model_pat, str) else list(model_pat)
            candidates = [c for c in (state.get("model_id"), state.get("model_tier"), self._default_model) if c]
            if not any(
                fnmatch.fnmatchcase(self._norm_val(c), str(p))
                for c in candidates
                for p in patterns
                if isinstance(p, str)
            ):
                return False

        for key, pat in (when.get("state") or {}).items():
            val = self._norm_val(state.get(key))
            patterns = [pat] if isinstance(pat, str) else list(pat)
            if not any(isinstance(p, str) and fnmatch.fnmatchcase(val, p) for p in patterns):
                return False
        return True

    @staticmethod
    def _norm_val(value: Any) -> str:
        """state 值规范化：布尔 → true/false（大小写稳定），None → 空串。

        fnmatchcase 大小写敏感，str(True)="True" 匹配规则里的 "true" 会
        失败；yaml 配置作者写 ``flag: true`` 期望匹配 state 布尔 True。
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _sanitize(inject: list[Any]) -> list[dict[str, Any]]:
        """白名单过滤注入消息：字段、角色，缺内容体的丢弃。"""
        out: list[dict[str, Any]] = []
        for raw in inject:
            if not isinstance(raw, dict):
                continue
            role = raw.get("role")
            if role not in _ALLOWED_ROLES:
                logger.warning("[model_prompt_adapter] 丢弃非法角色消息: %r", role)
                continue
            msg = {k: raw[k] for k in _ALLOWED_FIELDS if k in raw}
            msg["role"] = role
            if "content" not in msg and "reasoning_content" not in msg:
                continue
            out.append(msg)
        return out


def load_rules_from(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """独立加载规则文件（供测试/运维校验 rules.yaml 合法性）。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules = data.get("rules") or []
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
