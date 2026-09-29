"""模式物料通用取数/解析纯函数面（mode_material_inject 的一部分，任何模式共用，
零具体模式专属逻辑）。

mode_material_inject 职责终局三件（设计 D10，2026-09-28）= mode 观测回写 +
persona 接管 + 组装器物料 system prompt 尾追加；本模块承载其取数/解析纯函数：

- resolve_mode：读 execution_context.mode（无键/形态不符 = 空串，零激活语义）；
- derive_mode_from_agent：execution_context.mode 缺席/空时按 state["agent.id"]
  归属包派生（设计 D6：消费面单点，派发面零改动）；
- find_package_dir：模式包目录双根解析（实现单点在共享平铺模块 mode_keys）；
- resolve_persona_takeover：按 mode.yaml persona 声明解析人设接管文本
  （命中 = 调用方写 state.context.persona_text，prompt_build 的 {{persona:}}
  占位符据此换源）。

模式知识注入唯一面 = prompt_build 的 {{mode_catalog}} 目录（描述+路由）；
工具面单真值 = agent yaml tool_ids 三态——两者均不在本模块。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mode_keys import (
    find_mode_package_dir as _find_mode_package_dir,
    parse_mode_agent_key as _parse_mode_agent_key,
)

# mode 键 = plugin_id（mode_X）与包目录名的构成成分，只放行安全形态
# （设计稿口径：coding|writing|roleplay|research 一类小写标识）。
MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def resolve_mode(state: Mapping[str, Any]) -> str:
    """读 execution_context.mode；无键/形态不符返回空串（零注入语义）。"""
    ec = state.get("execution_context")
    mode = ec.get("mode") if isinstance(ec, Mapping) else None
    if not isinstance(mode, str):
        return ""
    return mode.strip()


def derive_mode_from_agent(state: Mapping[str, Any]) -> str:
    """按 state["agent.id"] 归属包派生 mode（设计 D6，消费面单点）。

    agent 键形如 ``mode_X/<stem>``：``mode_`` 前缀只用于提取候选 X（形态
    [a-z][a-z0-9_]{0,63}，parse_mode_agent_key 同口径），**真值判定靠包目录
    存在性**——find_package_dir(X) 命中（声明面表达：包内 agents/*.yaml 即
    归属）才派生，防凭空键伪造模式；禁止在前缀字符串上做其他模式行为分支。
    非模式键/形态非法/包不存在 → 空串（零派生，零注入语义不变）。
    """
    agent_id = state.get("agent.id")
    if not isinstance(agent_id, str):
        return ""
    parsed = _parse_mode_agent_key(agent_id)
    if parsed is None:
        return ""
    mode, _stem = parsed
    return mode if find_package_dir(mode) is not None else ""


def find_package_dir(mode: str) -> Path | None:
    """模式包目录双根解析（实现单点在共享平铺模块 mode_keys）。"""
    return _find_mode_package_dir(mode)


def resolve_persona_takeover(pkg_dir: Path | None, state: Mapping[str, Any]) -> str:
    """按 mode.yaml persona 声明解析人设接管文本（通用机制）。

    声明形态（mode.yaml，模式声明"接管主 agent 人设注入"）::

        persona:
          replace: true            # 接管开关（非 true = 本模式不接管）
          from: <键名>             # execution_context 中携带人设文本的键

    命中 = 返回人设文本（调用方写 state.context.persona_text，prompt_build 的
    {{persona:}} 占位符据此换源——替换提示词人设段，骨架不动）；声明缺席/
    replace 非 true/from 缺文本 = 空串（零接管）。pkg_dir 缺失或 mode.yaml
    不可读 = 空串（降级语义与组装器声明同口径）。

    缓存语义（提示职责在接入方 UI）：接管改变系统提示词前缀，破坏前缀缓存
    命中——接管声明即接受该代价。
    """
    if pkg_dir is None:
        return ""
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load((pkg_dir / "mode.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    decl = data.get("persona") if isinstance(data, dict) else None
    if not isinstance(decl, Mapping) or decl.get("replace") is not True:
        return ""
    from_key = decl.get("from")
    if not isinstance(from_key, str) or not from_key.strip():
        return ""
    ec = state.get("execution_context")
    text = ec.get(from_key.strip()) if isinstance(ec, Mapping) else None
    if isinstance(text, str) and text.strip():
        return text.strip()
    return ""
