"""context_build 模式物料档组装（模式体系 P2，主会话路径）。

设计真值 docs/working/模式体系落地设计_20260915.md §3.3②/§4.1/§4.2：
- state 带 execution_context.mode（main 会话路径）→ 组装主 agent 提示词的
  模式段：路由指引（编排清单/调度链/任务型对应）+ 模式口径段 + 工具面注记；
  tool_ids 按模式声明收窄（只收窄不扩权）。
- 无 mode 键 = 零注入；profile 取数失败 = 降级不注入（调用方 warning，
  不阻断管道）。
- 专属 agent 绑定分支（§4.2 档2/档3）不在本模块（Wave2/P3 接线）。

取数通道：
- profile 经 mode.get_profile 服务调用（tool-executor 显式 plugin_id，
  eval_harness 仓内先例同通道；信封解析见 unwrap_mode_profile）；
- 编排清单/口径段直读模式包约定目录（pipelines/*.yaml 键 = mode_X/<stem>、
  文件头 task_kinds；rules/*.md）。目录扫描注册（Rust 侧）可用前的过渡取数，
  注册面就绪后切 registry。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mode_keys import find_mode_package_dir as _find_mode_package_dir

logger = logging.getLogger(__name__)

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


def find_package_dir(mode: str) -> Path | None:
    """模式包目录双根解析（实现单点在共享平铺模块 mode_keys）。"""
    return _find_mode_package_dir(mode)


def read_orchestrations(pkg_dir: Path | None, mode: str) -> list[dict[str, Any]]:
    """直读模式包 pipelines/*.yaml → 编排清单（键 mode_X/<stem> + 文件头 task_kinds）。

    目录/文件缺失 = 空清单（包未带编排，合法形态）；单文件解析失败 warning
    跳过——本清单是路由咨询物料，不阻断管道（fail-closed 闸在 G2 注册面）。
    """
    if pkg_dir is None:
        return []
    pipelines_dir = pkg_dir / "pipelines"
    if not pipelines_dir.is_dir():
        return []
    import yaml  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for path in sorted(pipelines_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "[context_build] 模式编排文件解析失败，跳过 | path=%s | err=%s", path, exc
            )
            continue
        kinds = data.get("task_kinds") if isinstance(data, Mapping) else None
        out.append({
            "key": f"mode_{mode}/{path.stem}",
            "task_kinds": [str(k) for k in kinds] if isinstance(kinds, list) else [],
        })
    return out


def read_rules_text(pkg_dir: Path | None) -> str:
    """读模式包 rules/*.md 口径段（文件名序拼接）；无规则 = 空串（调用方占位）。"""
    if pkg_dir is None:
        return ""
    rules_dir = pkg_dir / "rules"
    if not rules_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(rules_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[context_build] 模式规则文件不可读，跳过 | path=%s | err=%s", path, exc
            )
            continue
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def mode_tool_ids(profile: Mapping[str, Any]) -> list[str] | None:
    """模式声明的工具面 material_scope.tool_ids；未声明/形态不符 = None（不收窄）。"""
    scope = profile.get("material_scope")
    if not isinstance(scope, Mapping):
        return None
    ids = scope.get("tool_ids")
    if not isinstance(ids, list):
        return None
    return [t for t in ids if isinstance(t, str)]


def tool_surface_note(
    profile: Mapping[str, Any], baseline: list[str] | None
) -> tuple[str, list[str] | None]:
    """工具面注记文本 + 收窄结果。只收窄不扩权：

    - 模式未声明 material_scope.tool_ids（一期未细化）= 不收窄，如实注明；
    - agent 基线未声明 tool_ids = 模式声明不套用（套用即扩权）；
    - 两边都有 = 基线 ∩ 模式声明（保基线序，结果 ⊆ 基线）。
    """
    mode_ids = mode_tool_ids(profile)
    if mode_ids is None:
        return ("本模式 material_scope 未细化 tool_ids，本期不收窄（维持基线白名单）。", None)
    if baseline is None:
        return ("agent 基线未声明 tool_ids，模式声明不套用（只收窄不扩权）。", None)
    narrowed = [t for t in baseline if t in set(mode_ids)]
    note = (
        f"模式工具面收窄：基线 {len(baseline)} 项 ∩ 模式声明 {len(mode_ids)} 项"
        f" = {len(narrowed)} 项。"
    )
    return note, narrowed


def build_mode_section(
    mode: str,
    profile: Mapping[str, Any],
    pkg_dir: Path | None,
    tool_note: str,
) -> str:
    """组装主 agent 提示词模式段（路由指引 + 口径段 + 工具面注记）。

    三件内容取数：路由指引 = profile.chain（调度链/执行者池）+ 包内
    pipelines/*.yaml（任务型→编排对应，主 agent 自行分析任务状态选编排，
    主 agent 是路由器）；口径段 = 包内 rules/*.md（取不到注明占位）；
    工具面 = 调用方经 tool_surface_note 预生成的注记。
    """
    name = str(profile.get("name") or mode)
    lines: list[str] = [
        f"## 模式物料（mode={mode}）",
        f"本轮任务以「{name}」模式执行，以下物料由模式包 mode_{mode} 提供。",
        "",
        "### 编排路由（你是路由器）",
        "自行分析当前任务状态与任务型，从下列清单选择匹配编排并以其编排键派单；"
        "无匹配时按既有派单规则走共享编排。",
    ]
    orch = read_orchestrations(pkg_dir, mode)
    if orch:
        lines.append("可用编排（编排键 → 任务型）：")
        for item in orch:
            kinds = "、".join(item["task_kinds"]) if item["task_kinds"] else "未标注"
            lines.append(f"- `{item['key']}`：任务型 {kinds}")
    else:
        lines.append(
            "本模式包暂未携带专属编排（pipelines/ 为空），"
            "按既有派单规则使用共享编排 `autonomous`。"
        )
    chain = profile.get("chain")
    if isinstance(chain, Mapping):
        path = chain.get("expected_path")
        if isinstance(path, list) and path:
            lines.append("调度链（偏好非强制）：" + " → ".join(str(p) for p in path))
        pool = chain.get("executor_pool")
        if isinstance(pool, list) and pool:
            lines.append("执行者池：" + "、".join(str(p) for p in pool))
    lines.append("")
    lines.append("### 模式口径")
    rules = read_rules_text(pkg_dir)
    if rules:
        lines.append(rules)
    else:
        lines.append(
            f"（模式包 mode_{mode} 暂未携带口径规则 rules/*.md，占位——随模式包进化补全）"
        )
    lines.append("")
    lines.append("### 工具面")
    lines.append(tool_note)
    return "\n".join(lines)


def unwrap_mode_profile(res: Any) -> dict[str, Any]:
    """mode.get_profile 返回信封解析：容忍 {"data": {...}} 或 profile 本体。

    非 dict / 缺 mode 键 = 服务返回无效，抛 ValueError（调用方降级不注入）。
    """
    data = res.get("data") if isinstance(res, Mapping) else None
    profile = data if isinstance(data, dict) else res
    if not isinstance(profile, dict) or not profile.get("mode"):
        raise ValueError(f"mode.get_profile 返回无效 profile: {str(res)[:200]}")
    return profile
