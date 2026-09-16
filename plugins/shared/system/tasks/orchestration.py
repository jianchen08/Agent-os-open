"""编排解析与完备性校验 —— task_submit 编排运行面的域逻辑（模式体系 P2）。

职责与真值（docs/working/模式体系落地设计_20260915.md §3.3/§3.5/§4.5/§十一）：

- **编排键 vs pipeline_id 概念分离**：编排键（``mode_X/<编排名>`` 或
  ``autonomous``）= 管道**定义**（函数）；pipeline_id = 运行**实例** ID
  （实例化后才由引擎生成）。本模块只解析编排键，从不生成 pipeline_id。
- **三级解析**（§3.3）：
  - ① 任务显式带编排键 → 直接命中使用；未命中（键不存在/插件禁用）=
    fail-closed 结构化报错（含「编排键不存在」+ 当前可用编排提示），
    **禁止静默落③**（显式意图落错管道比报错糟，§十一 H3①）；
  - ② 无显式键但有 mode 键 → 编排候选 = 该模式包 pipelines（文件头
    task_kinds 与任务 kind 匹配优先）；mode 键是用户显式约束（限定候选）
    而非门槛（§十一 H4）——候选为空或意图不明（多个候选无法区分）时落③；
  - ③ 兜底 ``autonomous``（共享默认管道是系统侧唯一真值）。
- **完备性校验**（§3.5，派发期、实例化前）：初始输入集 ⊇ 所选编排的
  派生输入集；派生输入集 = 编排 yaml 引用的 step 集合并集其 manifest
  ``capabilities.steps[].required_state_inputs`` 静态声明（H2 地基）。
  缺字段返回结构化清单（{missing_fields, orchestration_key, suggestion}），
  由调用方门口拒派。
- **任务记录增记编排键**（§4.5）：解析结果由调用方写入出生 state
  （``task.orchestration``）作审计/回放/归因锚点。

取数边界（与内核侧并行工作的让行约定）：模式包目录扫描注册（agents/
pipelines 约定子目录）在内核侧另行开发——注册表可用前，本模块以本地读
目录方式实现编排键发现与 step 声明索引（factory + 用户插件根双根，用户赢；
yaml 解析失败降级跳过并留 warning 痕迹）。内核扫描注册接线后，本模块的
发现/索引两函数应切换为 registry 取数（函数签名不变，调用方无感）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

AUTONOMOUS_ORCHESTRATION_KEY = "autonomous"

# 编排定义 yaml 的文件头路由标注（§2.3：文件头 task_kinds 标注参与解析链②路由）。
_TASK_KINDS_KEY = "task_kinds"

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
    task_kinds: tuple[str, ...] = field(default=())


def discover_orchestrations(
    *,
    pipelines_dir: str | os.PathLike[str] | None = None,
    modes_root: str | os.PathLike[str] | None = None,
    user_modes_root: str | os.PathLike[str] | None = None,
) -> dict[str, OrchestrationDefinition]:
    """发现全部可用编排键 → 定义（本地目录扫描；注册表可用后切 registry 取数）。

    三个来源（后写覆盖先写，用户赢——对齐 §2.2 双根兜底）：
    - ``pipelines_dir``（缺省仓库 ``config/pipelines/*.yaml``）→ 系统键 = 文件名 stem
      （如 ``autonomous``，系统命名空间裸键，§十一 M6）；
    - ``modes_root``（缺省出厂种子 ``plugins/shared/modes/*/pipelines/*.yaml``）
      → 模式键 = ``<模式包目录名>/<文件名 stem>``；
    - ``user_modes_root``（缺省 ``<USER_ROOT>/plugins/modes/*/pipelines``）→ 同键名
      覆盖 factory（同 id 用户赢）。

    yaml 解析失败 → warning 留痕后跳过该文件（发现面聚合的既有降级口径，
    与 reconcile 读面故障同风格）；解析成功的键恒可解析使用。
    """
    definitions: dict[str, OrchestrationDefinition] = {}

    def _collect(directory: Path, key_prefix: str) -> None:
        if not directory.is_dir():
            return
        for yaml_path in sorted(directory.glob("*.yaml")):
            key = f"{key_prefix}{yaml_path.stem}"
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
            kinds = raw.get(_TASK_KINDS_KEY)
            definitions[key] = OrchestrationDefinition(
                key=key,
                path=str(yaml_path),
                raw=raw,
                task_kinds=tuple(
                    str(k) for k in kinds if isinstance(k, str)
                ) if isinstance(kinds, list) else (),
            )

    root = Path(pipelines_dir) if pipelines_dir is not None else _repo_pipelines_dir()
    _collect(root, "")
    factory_modes = Path(modes_root) if modes_root is not None else _factory_modes_root()
    if factory_modes.is_dir():
        for package in sorted(factory_modes.iterdir()):
            if package.is_dir():
                _collect(package / "pipelines", f"{package.name}/")
    if user_modes_root is None:
        user_root = _user_modes_root()
    else:
        user_root = Path(user_modes_root)
    if user_root is not None and user_root.is_dir():
        for package in sorted(user_root.iterdir()):
            if package.is_dir():
                _collect(package / "pipelines", f"{package.name}/")
    return definitions


def resolve_orchestration(
    *,
    explicit_key: str = "",
    mode_key: str = "",
    task_kind: str = "",
    orchestrations: dict[str, OrchestrationDefinition] | None = None,
) -> OrchestrationDefinition:
    """三级解析编排键（§3.3）：① 显式 → ② mode 限定候选 → ③ autonomous。

    - ① ``explicit_key`` 非空：命中即用；未命中抛
      OrchestrationKeyNotFoundError（fail-closed，含可用编排提示，不落③）。
      主 agent 意图分析产出的键经 task_submit 显式提交，走本路同款校验——
      本函数不实现意图分析本身（提示词物料层，不在解析面）。
    - ② ``mode_key`` 非空（且无显式键）：候选 = 该模式包名下编排；task_kinds
      含 ``task_kind`` 者优先；唯一候选/唯一命中才选定——候选为空或意图不明
      （多候选无法区分）落③（H4：mode 键是约束非门槛，③只接意图不明）。
    - ③ ``autonomous``；缺失同样抛错（不静默造兜底）。
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

    if mode_key:
        candidates = [
            key
            for key in definitions
            if key == mode_key or key.startswith(f"{mode_key}/")
        ]
        matched = (
            [k for k in candidates if task_kind in definitions[k].task_kinds]
            if task_kind
            else []
        )
        if len(matched) == 1:
            return definitions[matched[0]]
        if not matched and len(candidates) == 1:
            return definitions[candidates[0]]
        if len(candidates) > 1:
            logger.info(
                "[orchestration] mode 键下编排候选无法区分意图，落兜底 %s"
                " | mode=%s | candidates=%s | task_kind=%s",
                AUTONOMOUS_ORCHESTRATION_KEY, mode_key, candidates, task_kind or "-",
            )

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
    """编排的派生输入集 = 引用 step 集合并集其 required_state_inputs（§3.1①）。"""
    derived: set[str] = set()
    for ref in iter_step_refs(definition.raw):
        derived.update(step_required.get(ref, ()))
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


def _factory_modes_root() -> Path:
    return _repo_root() / "plugins" / "shared" / "modes"


def _factory_pipeline_plugins_root() -> Path:
    return _repo_root() / "plugins" / "shared" / "pipeline"


def _user_modes_root() -> Path | None:
    """用户模式包根（``<USER_ROOT>/plugins/modes``）；用户空间不可得 → None。"""
    try:
        import user_space  # noqa: PLC0415 — plugins/shared 平铺模块（sidecar 自举注入）
    except ImportError:
        return None
    plugins_root = user_space.user_plugins_dir()
    return plugins_root / "modes" if plugins_root is not None else None


def json_load(path: Path) -> dict[str, Any]:
    """manifest JSON 读取（ValueError 覆盖 JSONDecodeError，供调用方统一降级）。"""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"manifest 非映射形态: {path}")
    return data
