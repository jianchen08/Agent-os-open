"""编排解析与完备性校验 —— task_submit 编排运行面的域逻辑（模式体系 P2）。

职责与真值（docs/working/模式包工作模式设计_20260928.md D2/一管一配置）：

- **编排键 vs pipeline_id 概念分离**：编排键（``config/pipelines`` 登记名
  或 ``autonomous``）= 管道**定义**（函数）；pipeline_id = 运行**实例** ID
  （实例化后才由引擎生成）。本模块只解析编排键，从不生成 pipeline_id。
- **两级解析**：
  - ① 任务显式带编排键 → 直接命中使用；未命中（键不存在/插件禁用）=
    fail-closed 结构化报错（含「编排键不存在」+ 当前可用编排提示），
    **禁止静默落②**（显式意图落错管道比报错糟）；
  - ② 兜底 ``autonomous``（共享默认管道是系统侧唯一真值）。
  包内 ``pipelines/`` 编排清单目录已退役（D2 同名异物消除）——编排候选
  唯一来源 = ``config/pipelines`` 登记处，mode 键不再限定候选（模式控制
  经 execution_context.mode 透传，设计 D6）。
- **完备性校验**（§3.5，派发期、实例化前）：初始输入集 ⊇ 所选编排的
  派生输入集；派生输入集 = 编排 yaml 引用的 step 集合并集其 manifest
  ``capabilities.steps[].required_state_inputs`` 静态声明（H2 地基）。
  缺字段返回结构化清单（{missing_fields, orchestration_key, suggestion}），
  由调用方门口拒派。
- **任务记录增记编排键**（§4.5）：解析结果由调用方写入出生 state
  （``task.orchestration``）作审计/回放/归因锚点。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

AUTONOMOUS_ORCHESTRATION_KEY = "autonomous"

# 管道 DSL 的动态核心步骤键（P1-1 声明驱动缺省：initial_state.core_plugin 缺省、
# post 路由 set.core_plugin 每轮重写——值即 step 引用，与 steps 列表同 vocabulary）。
_CORE_PLUGIN_KEY = "core_plugin"


class OrchestrationError(Exception):
    """编排解析/校验失败（结构化：错误码 + 结构化字段随异常携带）。"""

    error_code = "ORCHESTRATION_ERROR"

    def __init__(self, message: str, **fields: Any) -> None:
        """message 供人读，fields 供结构化消费方（错误是值，可编程恢复）。"""
        super().__init__(message)
        self.message = message
        self.fields: dict[str, Any] = fields


class OrchestrationKeyNotFoundError(OrchestrationError):
    """H3①：显式编排键未命中（键不存在/插件禁用）——fail-closed，不落③。"""

    error_code = "ORCHESTRATION_KEY_NOT_FOUND"


class OrchestrationIncompleteError(OrchestrationError):
    """完备性校验失败：初始输入集缺字段（M2：结构化清单回流主 agent/用户）。"""

    error_code = "INCOMPLETE_INITIAL_INPUTS"


@dataclass(frozen=True)
class OrchestrationDefinition:
    """一个编排键的解析结果（管道定义，非运行实例）。"""

    key: str
    path: str
    raw: dict[str, Any]


def discover_orchestrations(
    *,
    pipelines_dir: str | os.PathLike[str] | None = None,
) -> dict[str, OrchestrationDefinition]:
    """发现全部可用编排键 → 定义（config/pipelines 登记处扫描，一管一配置唯一来源）。

    ``pipelines_dir``（缺省仓库 ``config/pipelines/*.yaml``）→ 系统键 = 文件名
    stem（如 ``autonomous``/``roleplay``，登记名即编排键）。包内 ``pipelines/``
    编排清单目录已退役（D2）——本函数不扫描模式包目录。

    yaml 解析失败 → warning 留痕后跳过该文件（发现面聚合的既有降级口径，
    与 reconcile 读面故障同风格）；解析成功的键恒可解析使用。
    """
    definitions: dict[str, OrchestrationDefinition] = {}

    def _collect(directory: Path) -> None:
        if not directory.is_dir():
            return
        for yaml_path in sorted(directory.glob("*.yaml")):
            key = yaml_path.stem
            try:
                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.warning(
                    "[orchestration] 编排定义解析失败，跳过（配置故障需排查）"
                    " | key=%s | path=%s | err=%s",
                    key, yaml_path, exc,
                )
                continue
            if not isinstance(raw, dict):
                logger.warning(
                    "[orchestration] 编排定义非映射形态，跳过 | key=%s | path=%s",
                    key, yaml_path,
                )
                continue
            definitions[key] = OrchestrationDefinition(
                key=key, path=str(yaml_path), raw=raw,
            )

    _collect(Path(pipelines_dir) if pipelines_dir is not None else _repo_pipelines_dir())
    return definitions


def resolve_orchestration(
    *,
    explicit_key: str = "",
    orchestrations: dict[str, OrchestrationDefinition] | None = None,
) -> OrchestrationDefinition:
    """两级解析编排键：① 显式 → ② autonomous 兜底。

    - ① ``explicit_key`` 非空：命中即用；未命中抛
      OrchestrationKeyNotFoundError（fail-closed，含可用编排提示，不落②）。
      主 agent 意图分析产出的键经 task_submit 显式提交，走本路同款校验——
      本函数不实现意图分析本身（提示词物料层，不在解析面）。
    - ② ``autonomous``；缺失同样抛错（不静默造兜底）。

    mode 键不再限定编排候选（包内编排清单退役，D2）：模式控制经
    execution_context.mode 透传给消费面（设计 D6），与管道选择解耦。
    """
    definitions = orchestrations if orchestrations is not None else discover_orchestrations()
    if explicit_key:
        definition = definitions.get(explicit_key)
        if definition is None:
            raise OrchestrationKeyNotFoundError(
                f"编排键不存在: {explicit_key}（键不存在或提供插件被禁用）。"
                f"当前可用编排: {sorted(definitions) or '（无）'}",
                orchestration_key=explicit_key,
                available_orchestrations=sorted(definitions),
            )
        return definition

    fallback = definitions.get(AUTONOMOUS_ORCHESTRATION_KEY)
    if fallback is None:
        raise OrchestrationKeyNotFoundError(
            f"兜底编排 {AUTONOMOUS_ORCHESTRATION_KEY} 不存在"
            f"（共享默认管道缺失，系统配置故障）。"
            f"当前可用编排: {sorted(definitions) or '（无）'}",
            orchestration_key=AUTONOMOUS_ORCHESTRATION_KEY,
            available_orchestrations=sorted(definitions),
        )
    return fallback


def load_step_required_inputs(
    pipeline_plugins_root: str | os.PathLike[str] | None = None,
) -> dict[str, list[str]]:
    """索引管道 step 插件 manifest 的 required_state_inputs 声明（H2 地基）。

    扫描管道插件根（缺省 ``plugins/shared/pipeline``）下全部 plugin.json，
    取 ``capabilities.steps[]`` 中声明了 ``required_state_inputs``（字符串列表）
    的条目 → ``{step 名: [state 键, …]}``。未声明该字段的 step 不入索引
    （完备性并集中贡献空集——声明面按批次补齐，缺失声明不臆测）。

    manifest 解析失败 → warning 留痕跳过（同 discover 的发现面降级口径）。
    """
    root = (
        Path(pipeline_plugins_root)
        if pipeline_plugins_root is not None
        else _factory_pipeline_plugins_root()
    )
    index: dict[str, list[str]] = {}
    if not root.is_dir():
        return index
    for manifest_path in sorted(root.rglob("plugin.json")):
        try:
            manifest = json_load(manifest_path)
        except (OSError, ValueError) as exc:
            logger.warning(
                "[orchestration] step manifest 解析失败，跳过 | path=%s | err=%s",
                manifest_path, exc,
            )
            continue
        steps = (manifest.get("capabilities") or {}).get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            declared = step.get("required_state_inputs")
            if not isinstance(declared, list):
                continue
            name = str(step.get("name") or "")
            if not name:
                continue
            keys = [str(k) for k in declared if isinstance(k, str)]
            if name in index and index[name] != keys:
                logger.warning(
                    "[orchestration] step 声明冲突，后扫描者覆盖"
                    " | step=%s | prev=%s | next=%s | manifest=%s",
                    name, index[name], keys, manifest_path,
                )
            index[name] = keys
    return index


def iter_step_refs(raw: dict[str, Any]) -> list[str]:
    """从编排定义 yaml 抽取全部 step 引用（DSL 形状驱动，保持出现序去重）。

    覆盖 autonomous.yaml 形态：``loop_bodies[].steps[]``（字符串引用 / 带
    ``name`` 的条件步骤 / 嵌套 ``steps`` 分组）、``initial_state.core_plugin``
    缺省、post 路由 ``set.core_plugin`` 重写——动态核心步骤键的值即 step 引用。
    """
    refs: list[str] = []

    def _add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate and candidate not in refs:
            refs.append(candidate)

    def _walk_steps(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                _add(item.get("name"))
                _walk_steps(item.get("steps"))
                for route in item.get("next") or []:
                    if isinstance(route, dict):
                        route_set = route.get("set")
                        if isinstance(route_set, dict):
                            _add(route_set.get(_CORE_PLUGIN_KEY))

    for body in raw.get("loop_bodies") or []:
        if isinstance(body, dict):
            _walk_steps(body.get("steps"))
    initial_state = raw.get("initial_state")
    if isinstance(initial_state, dict):
        _add(initial_state.get(_CORE_PLUGIN_KEY))
    return refs


def derived_input_set(
    definition: OrchestrationDefinition,
    step_required: dict[str, list[str]],
) -> frozenset[str]:
    """编排的派生输入集 = 引用 step 集合并集其 required_state_inputs（§3.1①）。

    循环局部键豁免：loop_bodies[].loop_config.as 声明的迭代键由引擎每次迭代
    注入（并行 for-each，ADR 2026-10-02-loopconfig-parallel-foreach），不属
    初始输入——体内 step 的 required_state_inputs 与之相交部分不计（否则
    tool_core 的 current_call 会把任务派发永久拒之门外）。
    """
    derived: set[str] = set()

    def _add_ref(ref: Any, loop_locals: frozenset[str]) -> None:
        if isinstance(ref, str) and ref:
            derived.update(set(step_required.get(ref, ())) - loop_locals)

    def _walk_steps(items: Any, loop_locals: frozenset[str]) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, str):
                _add_ref(item, loop_locals)
            elif isinstance(item, dict):
                _add_ref(item.get("name"), loop_locals)
                # step 级循环声明（autonomous.yaml 形态：loop_config 内嵌在
                # 带 steps 的 step 字典上，如 tool_exec 的 as: current_call）
                item_cfg = item.get("loop_config")
                item_as = (
                    item_cfg.get("as") if isinstance(item_cfg, dict) else None
                )
                item_locals = loop_locals
                if isinstance(item_as, str) and item_as:
                    item_locals = loop_locals | {item_as}
                _walk_steps(item.get("steps"), item_locals)
                for route in item.get("next") or []:
                    if isinstance(route, dict):
                        route_set = route.get("set")
                        if isinstance(route_set, dict):
                            _add_ref(route_set.get(_CORE_PLUGIN_KEY), loop_locals)

    def _walk_bodies(bodies: Any, inherited: frozenset[str]) -> None:
        for body in bodies or []:
            if not isinstance(body, dict):
                continue
            cfg = body.get("loop_config")
            as_decl = cfg.get("as") if isinstance(cfg, dict) else None
            as_keys = (
                (as_decl,) if isinstance(as_decl, str) and as_decl else tuple()
            )
            locals_ = inherited | frozenset(as_keys)
            _walk_steps(body.get("steps"), locals_)
            _walk_bodies(body.get("loop_bodies"), locals_)

    _walk_bodies(definition.raw.get("loop_bodies"), frozenset())
    initial_state = definition.raw.get("initial_state")
    if isinstance(initial_state, dict):
        core = initial_state.get(_CORE_PLUGIN_KEY)
        if isinstance(core, str) and core:
            derived.update(step_required.get(core, ()))
    return frozenset(derived)


def check_completeness(
    initial_inputs: set[str],
    definition: OrchestrationDefinition,
    step_required: dict[str, list[str]],
) -> list[str]:
    """完备性校验（派发期、实例化前）：返回排序后的缺失字段清单（空 = 通过）。

    初始输入集与派生输入集同 vocabulary（管道 state 键）。缺失清单由调用方
    组装 {missing_fields, orchestration_key, suggestion} 结构化错误门口拒派。
    """
    missing = derived_input_set(definition, step_required) - initial_inputs
    return sorted(missing)


def incomplete_error(
    definition: OrchestrationDefinition,
    missing_fields: list[str],
) -> OrchestrationIncompleteError:
    """组装完备性失败的结构化错误（M2：缺失清单 + 建议动作，回流主 agent/用户）。"""
    return OrchestrationIncompleteError(
        f"初始输入不完备，编排 {definition.key} 缺少字段: {missing_fields}。"
        "请在提交参数或目标 agent 配置中补齐后重新提交，"
        "或改选输入契约相容的编排。",
        orchestration_key=definition.key,
        missing_fields=missing_fields,
        suggestion=(
            "补齐缺失字段（提交参数/agent 基座配置），"
            f"或改选编排（当前可用见派发方提示）；编排 {definition.key} "
            f"要求字段并集见其成员 step 的 required_state_inputs 声明。"
        ),
    )


# ── 路径解析（本地扫描期缺省根；注册表可用后本段随切换一并退役） ──


def _repo_root() -> Path:
    """仓库根：本模块位于 plugins/shared/system/tasks/ 下。"""
    return Path(__file__).resolve().parents[4]


def _repo_pipelines_dir() -> Path:
    return _repo_root() / "config" / "pipelines"


def _factory_pipeline_plugins_root() -> Path:
    return _repo_root() / "plugins" / "shared" / "pipeline"


def json_load(path: Path) -> dict[str, Any]:
    """manifest JSON 读取（ValueError 覆盖 JSONDecodeError，供调用方统一降级）。"""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"manifest 非映射形态: {path}")
    return data
