"""Agent 配置装载 Input 插件 — agent 配置加载与上下文装配的唯一归口。

负责在管道循环的输入阶段构建上下文信息：按 state 持久键 agent.id 加载
agent yaml（自持配置，内核只留 tool_ids 窄接口），将提示词骨架、
agent 名称/层级、会话元数据等写入 state，
供后续插件（prompt_build、knowledge_inject 等）和 Core 读取。

State 命名空间：
    - context.* : 本插件写入的上下文字段
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _libs.pipeline_base.types import StateKeys
from _root_api import (
    collect_mode_inject_tool_ids,
    collect_mode_tool_ids,
    find_mode_agent_yaml,
    find_mode_package_dir,
    parse_mode_agent_key,
    resolve_active_mode,
)
from rp_packs import load_packs, merge_state_decl

logger = logging.getLogger(__name__)

# 开演向导执行选项键（RP 面板刀-Ⅰ，FR-A3/FR-G4；mode.session extra_context
# 通道，桥 fail-closed 只收非空字符串——列表以逗号分隔 id 串承载）：值增量并入
# 卡顶层 pack_ids（卡声明序在前、向导追加在后，去重保序）；键缺席/空白 = 现状
# 零变化。同族键 wizard_lorebook_ids / wizard_profile 消费在 mode_roleplay
# material 组装器（覆盖注入面）。
WIZARD_PACK_IDS_KEY = "wizard_pack_ids"

# 开演 init effects 快照键（RP 面板 v3.1 刀1；mode.session extra_context 通道，
# 桥 fail-closed 只收非空字符串）：面板把所选路线/词条的 effects 聚合为 k=v
# 快照串写入 rp_init，本步解析归一为 context.rp_init_effects（消费落点 =
# state_marker_parse 会话首轮 Validator 结算，结算序 init 最先、审计 source=init）。
#
# 快照串契约（k=v 对以逗号串承载，与 rp_axes 同构；转义方案单源在此）：
# - 段 = ``<声明路径>=<op>``（如 ``npc.阿比.好感度=+5,player.伤势==重伤``）；
#   k = 卡+包 variables 声明路径（点分段，不含逗号/等号/反斜杠——键域由声明
#   面保证）；v = op 字面（``+5`` 相对 / ``=重伤`` 绝对，故段内按**第一个**
#   ``=`` 切分、v 可含等号）。
# - v 内转义：逗号以 ``\,`` 承载、反斜杠以 ``\\`` 承载（其余 ``\x`` 序列反转义
#   保留原字 ``\x``，宽容不吞字）；k 不转义。键域校验放消费侧（Validator
#   fail-closed），本步只做形态归一（坏段 warning 跳过，不阻断装载）。
RP_INIT_KEY = "rp_init"


def _split_escaped(text: str, sep: str) -> list[str]:
    """按分隔符切段、保留反斜杠转义序列原样（反转义归调用方按域执行）。"""
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in text:
        if escaped:
            buf.append("\\")
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:  # 尾部悬挂反斜杠按字面保留
        buf.append("\\")
    parts.append("".join(buf))
    return parts


def _unescape_commas(value: str) -> str:
    """rp_init 值域反转义：``\\,``→逗号、``\\\\``→反斜杠、其余 ``\\x`` 原样。"""
    out: list[str] = []
    escaped = False
    for ch in value:
        if escaped:
            out.append("," if ch == "," else ("\\" if ch == "\\" else f"\\{ch}"))
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append("\\")
    return "".join(out)


def _rp_init_effects(state: dict[str, Any]) -> dict[str, str] | None:
    """execution_context.rp_init 快照串 → 有序 {声明路径: op}（键缺席/空白/全坏段 = None）。"""
    ec = state.get("execution_context")
    raw = ec.get(RP_INIT_KEY) if isinstance(ec, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    effects: dict[str, str] = {}
    for segment in _split_escaped(raw, ","):
        piece = segment.strip()
        if not piece:
            continue
        key, sep, value = piece.partition("=")
        key = key.strip()
        if not sep or not key:
            logger.warning(
                "[agent_config_load] rp_init 快照段缺 k=v 形态，跳过 | segment=%r", piece
            )
            continue
        effects[key] = _unescape_commas(value.strip())
    return effects or None


def _wizard_extra_ids(state: dict[str, Any], key: str) -> list[str]:
    """execution_context 向导 id 串读出（逗号分隔，去空段、去重保序）。

    键缺席/非字符串/纯空白 = 空表（零并入，兼容态零行为变化）。
    """
    ec = state.get("execution_context")
    raw = ec.get(key) if isinstance(ec, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return []
    ids: list[str] = []
    for segment in raw.split(","):
        piece = segment.strip()
        if piece and piece not in ids:
            ids.append(piece)
    return ids


def _canonical_fs_path(path: str) -> str:
    """路径比较用规范化（大小写/分隔符/相对段归一，比较双端同函数）。

    与 project_manager 登记比较的 canonicalize 同目标但本地实现（跨插件包
    不互相 import）：比较两边在命中时刻都真实存在（登记路径 = 用户所选
    目录；state.workspace = workspace_lifecycle 解析写面），``resolve()``
    即 realpath 语义；解析异常降级 normpath（不阻断装载）。
    """
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(os.path.normpath(path))


def _user_packs_dir() -> str | None:
    """用户层玩法包目录：<user_config_dir>/packs（lorebooks 用户层同模式双根）。

    user_space 不可得 = None（仅包内参与）；目录不存在不算缺失——load_packs
    按文件探测。解析失败降级 None，绝不阻断装载。
    """
    import sys
    from pathlib import Path

    shared_root = str(Path(__file__).resolve().parents[1])
    if shared_root not in sys.path:
        sys.path.insert(0, shared_root)
    try:
        from _root_api import user_space  # noqa: PLC0415

        config_dir = user_space.user_config_dir()
    except Exception as exc:  # noqa: BLE001 — 用户层不可得按「无用户 packs」定语义
        # warning（用户可见面）：用户层玩法包静默缺席会呈现为「pack 勾选无效」，
        # 作者/玩家排查需可见降级原因（debug 级不可见 = 静默降级）
        logger.warning("[agent_config_load] 用户配置层不可用（packs 仅包内）| err=%s", exc)
        return None
    return str(Path(config_dir) / "packs") if config_dir else None


def _resolve_kernel_relative_path(raw: Path | str) -> Path:
    """还原内核对相对配置路径的解析基（sidecar cwd ≠ 内核 cwd 的错位修复）。

    内核对显式传入的相对 AGENTOS_CONFIG_ROOT / AGENTOS_USER_ROOT 不做规范化
    （仅未设时写派生绝对值），sidecar 原样继承 env 且 cwd = 插件目录——直接
    Path(raw) 会按插件目录解析出幻影路径（2026-11-16 R6 真内核实测：相对
    config root → agent yaml 双根全空 → 工具面/模型档静默回退）。

    还原基 = 内核 cwd 的等价物：内核能启动 ⟹ <config_root>/kernel/instance.yaml
    必存在于内核 cwd（启动硬契约，ADR 2026-11-07 三根 fail-closed）。从本插件
    自身位置与 sidecar cwd 的祖先链找第一个使命中成立的目录，即等价还原。

    绝对路径原样返回；还原失败返回原值（既有 cwd 语义兜底，不劣于修复前）。
    """
    from pathlib import Path as _Path

    p = raw if isinstance(raw, _Path) else _Path(str(raw))
    if p.is_absolute():
        return p
    # cwd 优先（sidecar cwd=插件目录，生产与插件目录锚同源同结果；测试
    # 以 cwd 隔离 tmp 工作区时不被插件目录锚的仓祖先抢先）
    anchors = [_Path.cwd(), _Path(__file__).resolve().parent]
    seen: set[_Path] = set()
    for anchor in anchors:
        for base in [anchor, *anchor.parents]:
            if base in seen:
                continue
            seen.add(base)
            candidate = base / p
            if (candidate / "kernel" / "instance.yaml").is_file():
                return candidate
    logger.debug(
        "[agent_config_load] 相对配置路径还原失败（instance.yaml 标记未命中，"
        "保留原值按 sidecar cwd 解析）| raw=%s",
        raw,
    )
    return p


class AgentConfigLoadPlugin(IInputPlugin):
    """上下文构建 Input 插件。

    将 agent 配置（yaml 解析结果）、层级信息、会话元数据等组装为
    管道执行所需的上下文字段写入 state；本插件只构建"上下文数据"，
    不管理服务实例（服务经 PluginContext.get_service 获取）。

    优先级：10（准备级，先于其他 Input 插件执行）；最小上下文也能跑。

    Attributes:
        _config: 插件配置字典
        _system_prompt: 系统 prompt 模板
        _agent_name: Agent 名称
        _agent_level: 插件配置的默认 Agent 层级（构造期固定；本轮实际层级由
            ``_do_work`` 局部值贯穿，不经实例字段中转）
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
        # agent 配置自持：执行身份是管道 state 持久键 agent.id（出生方经 state
        # 透传落库），加载 config/agents/**/<agent_id>.yaml 是本插件（sidecar）
        # 的职责。缓存：yaml 路径 → 解析结果（mtime 失效），进程内复用。
        self._agent_yaml_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # 当前 agent 命中模式包注册表时的包根绝对路径（每次 _load_agent_config
        # 复位，_do_work 据此写 context.agent_pack_root）。
        self._mode_pack_root = ""
        # tool-surface capability 调用通道（server.py on_load 注入）：模式声明
        # 工具并入的启用判定走活注册表探针（接线先例 tool_schema/server.py）。
        # 实例属性身份唯一（合宿下多 pipeline 插件共享裸名 plugin 模块，模块级
        # 全局会被别家插件的注入写走）。
        self._capability_caller: Callable[..., Awaitable[dict[str, Any]]] | None = None
        self._project_service_caller: Callable[..., Awaitable[dict[str, Any]]] | None = None
        # card 质检服务调用通道（mode_roleplay provides namespace card，T6c；
        # server.py on_load 注入）：声明面结构 lint + actions 归一的服务消费缝。
        self._card_service_caller: (
            Callable[..., Awaitable[dict[str, Any]]] | None
        ) = None
        # llm 服务调用通道（server.py on_load 注入）：模型落定步经
        # llm_service 的 model.resolve 解析最终模型 id 写 state.model_id
        # （与 llm_core 选模共用 _config_models.resolve_model_id 单源）。
        self._llm_service_caller: Callable[..., Awaitable[dict[str, Any]]] | None = None

    def set_capability_caller(
        self, caller: Callable[..., Awaitable[dict[str, Any]]] | None
    ) -> None:
        """注入/清除 tool-surface capability 调用通道（server.py on_load 调用）。"""
        self._capability_caller = caller

    def set_project_service_caller(
        self, caller: Callable[..., Awaitable[dict[str, Any]]] | None
    ) -> None:
        """注入/清除 project.* 服务调用通道（T6a，server.py on_load 调用）。"""
        self._project_service_caller = caller

    def set_card_service_caller(
        self, caller: Callable[..., Awaitable[dict[str, Any]]] | None
    ) -> None:
        """注入/清除 card 质检服务调用通道（server.py on_load 调用）。"""
        self._card_service_caller = caller

    def set_llm_service_caller(
        self, caller: Callable[..., Awaitable[dict[str, Any]]] | None
    ) -> None:
        """注入/清除 llm 服务调用通道（落定步 model.resolve，server.py on_load 调用）。"""
        self._llm_service_caller = caller

    async def _resolve_initial_model(self, model_tier: str) -> str:
        """模型落定：经 llm_service ``model.resolve`` 解析最终模型 id（fail-soft）。

        契约（落定键 2026-10-18）：显式 model_id > tier→defaults.tiers >
        defaults.chat，与 llm_core 选模共用 ``_config_models.resolve_model_id``
        单源。通道未接线/调用失败/信封失败/空结果 → 空串 + warning（保持
        state.model_id 恒空旧行为；权威错误由 llm_core 执行时报出，不抢权威）。
        """
        caller = self._llm_service_caller
        if caller is None:
            logger.warning(
                "[agent_config_load] model.resolve 通道未接线，state.model_id 不落定（降级）"
            )
            return ""
        try:
            envelope = await caller(
                "invoke",
                {
                    "tool_name": "model.resolve",
                    "plugin_id": "llm_service",
                    "args": {"model_id": "", "model_tier": model_tier},
                },
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft：落定失败不阻断装载
            logger.warning(
                "[agent_config_load] model.resolve 调用失败，state.model_id 不落定 | err=%s",
                exc,
            )
            return ""
        if not isinstance(envelope, dict) or not envelope.get("success"):
            logger.warning(
                "[agent_config_load] model.resolve 信封失败，state.model_id 不落定 | resp=%s",
                str(envelope)[:200],
            )
            return ""
        data = envelope.get("data")
        model_id = data.get("model_id") if isinstance(data, dict) else None
        return model_id if isinstance(model_id, str) and model_id else ""

    async def _card_lint(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """card 质检服务调用（fail-soft）：不可用/失败/信封无效 = None + warning。

        lint 面是非核心 warning 面（装载 verbatim，作者面错误可见性），服务
        缺席按「非核心给降级提示」降级，不阻断装载；调用方对 None 跳过对应
        lint/归一环节（actions 归一缺失 = 本轮不写 context.rp_actions）。
        """
        caller = self._card_service_caller
        if caller is None:
            logger.warning(
                "[agent_config_load] card 质检服务未注入，跳过 %s（非核心降级）", method
            )
            return None
        try:
            result = await caller(method, params)
        except Exception as exc:  # noqa: BLE001 —— 服务失败统一降级（warning 面）
            logger.warning(
                "[agent_config_load] card 质检服务调用失败，跳过 %s: %s", method, exc
            )
            return None
        if not isinstance(result, dict) or "error" in result:
            logger.warning(
                "[agent_config_load] card 质检服务返回无效，跳过 %s（非核心降级）", method
            )
            return None
        return result

    def _find_agent_yaml(self, agents_dir, agent_id: str):
        """在 agents_dir 下解析 agent_id → yaml；未命中回退按 config_id 匹配。

        Returns:
            (path, mtime) 或 None。config_id 匹配覆盖执行 agent 文件名与
            config_id 不同的常态（evolution_agent.yaml / config_id=evolution）。
        """
        from pathlib import Path

        # 层级键（executor/generation/novel_writer_agent 形态）= 注册表下同构
        # 相对路径，直解析（与内核 user_space::resolve_config_path 同构）。
        # basename 比对永不命中带分隔符的键（p.name 无 /）、config_id 兜底只存
        # 裸 id——不直解析则配置整体不装载（tool_ids/prompt/运行时参数全断）。
        # 含空段/..段的键拒绝直解析（防注册表外穿越），仍走既有两级扫描。
        parts = f"{agent_id}.yaml".replace("\\", "/").split("/")
        if len(parts) > 1 and all(parts) and ".." not in parts:
            direct = Path(agents_dir).joinpath(*parts)
            if direct.is_file():
                return direct, direct.stat().st_mtime
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
        import sys
        from pathlib import Path

        import yaml as _yaml

        # 包根键只随模式包注册表命中出现（用户/factory config 命中保持缺席
        # 语义）；每次装载先复位，防同实例跨管道残留前一 agent 的包根。
        self._mode_pack_root = ""
        if not agent_id:
            return {}
        # 双根解析（ADR 2026-09-14 §2.4）：用户配置层的 agents/ 优先——用户层
        # 存在同路径文件即接管生效，factory 同名文件完全不参与；否则回落
        # factory（与 Rust 侧 user_space::resolve_config_path 同构）。
        # 相对 env 还原（2026-11-16 R6 返修）：内核对显式传入的相对
        # AGENTOS_CONFIG_ROOT/AGENTOS_USER_ROOT 不做规范化（main.rs 仅在未设
        # 时写派生绝对值），sidecar 原样继承且 cwd=插件目录（invoker
        # with_working_dir）——Path("config")/agents 会落到插件目录下的幻影
        # 路径，双根全空 → agent yaml 静默回退（tool_ids/model_tier 全丢，
        # 2026-11-16 真内核实测）。还原基 = 内核 cwd 的等价物：从本插件位置
        # 向上找第一个使 <raw>/kernel/instance.yaml 存在的祖先（instance.yaml
        # 是启动硬契约，可启动内核必然满足）；还原失败保留原值（既有 cwd
        # 语义兜底，不比修复前更差）。
        candidates: list[Path] = []
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from _root_api import user_space as _user_space

            user_cfg = _user_space.user_config_dir()
            if user_cfg:
                candidates.append(_resolve_kernel_relative_path(user_cfg) / "agents")
        except Exception as exc:
            logger.debug("[agent_config_load] 用户配置层不可用（仅 factory）| err=%s", exc)
        factory_root = os.environ.get("AGENTOS_CONFIG_ROOT", "")
        if factory_root:
            candidates.append(_resolve_kernel_relative_path(factory_root) / "agents")
        candidates = [d for d in candidates if d.is_dir()]
        found = None
        for agents_dir in candidates:
            found = self._find_agent_yaml(agents_dir, agent_id)
            if found is not None:
                break
        if found is None:
            # 两级解析第二级（模式体系设计稿 §3.3）：系统注册表未命中 →
            # 模式包目录注册表（mode_X/<stem> → 包内 agents/<stem>.yaml，
            # 键构与内核 mode_registry 同构）；非模式键/未命中 → None。
            mode_yaml = find_mode_agent_yaml(agent_id)
            if mode_yaml is not None:
                found = (mode_yaml, mode_yaml.stat().st_mtime)
                # 包根 = 命中 yaml 所在包目录（注册表契约 agents/<stem>.yaml 的
                # 祖父即包根，与 find_mode_package_dir 双根结果同源，免二次解析）。
                self._mode_pack_root = str(mode_yaml.parent.parent)
        if found is None:
            logger.debug(
                "[agent_config_load] 未找到 agent yaml（系统与模式包注册表均未命中，"
                "按默认配置运行）| agent_id=%s | agents_dirs=%s",
                agent_id,
                candidates,
            )
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
                f"[agent_config_load] agent yaml 解析失败，管道终止 | path={path} | agent_id={agent_id}"
            ) from exc
        if not isinstance(data, dict):
            data = {}
        self._agent_yaml_cache[str(path)] = (mtime, data)
        return data

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "agent_config_load"

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

        # 层级是 per-pipeline 事实：同 sidecar 实例被多管道并发复用（SDK 对
        # async handler 不加锁），若经实例字段中转，A 在下方 await 处让出期间
        # B 整轮写入会在 A 恢复后被当成本轮层级（L1 豁免/评估闸门旁路）。
        # 故按局部值贯穿本轮：起始取构造期配置默认，第 2 步按本管道 agent yaml
        # 覆盖，直至写入 state 只用局部量。
        agent_level = self._agent_level

        # 1. 系统提示词：优先 state 注入；否则按 state 持久键 agent.id 加载
        #    agent yaml（执行身份是管道 state 的一部分：出生方经 chat.send_message
        #    state 透传落库，热启动在内存 state、冷启动在 DB——派发不携带身份；
        #    缺省主 agent 由本消费面自持）。最终回退插件配置默认。
        agent_id_key = str(ctx.state.get("agent.id", "") or "")
        if not agent_id_key and ctx.state.get("lineage.parent_pipeline_id"):
            # 有父血缘却无身份 = 出生方漏透传 agent.id，静默降级默认主 agent
            # 会让 L1 早退门旁路评估闸门/层级语义全错（B12 根因②）——显式告警
            logger.warning(
                "[agent_config_load] state 有 lineage.* 但缺 agent.id，回退默认主 agent"
                "（出生方应按 task_birth 协议透传 agent.id）| pipeline=%s",
                ctx.state.get("task.id", ""),
            )
        agent_cfg = self._load_agent_config(agent_id_key or "agentos")
        updates["context.system_prompt"] = (
            ctx.state.get("system_prompt", "")
            or str(agent_cfg.get("system_prompt", "") or "")
            or self._system_prompt
        )
        # 模式包私有物料锚点：agent yaml 经模式包注册表命中时携带包根，
        # prompt_build 的 {{path:./...}} 包内相对形态据此解析（包随包分发，
        # 不随用户空间同步丢物料）；用户/factory config 命中不写该键（缺席语义）。
        if self._mode_pack_root:
            updates["context.agent_pack_root"] = self._mode_pack_root
        # agent 名称每次执行按当前管道 agent 重新解析（不缓存到实例属性——
        # 同 sidecar 进程内多个管道复用本插件实例，缓存会造成 agent_name
        # 跨管道污染）。实例属性只作配置默认值，不缓存解析结果。
        agent_name = self._agent_name or str(agent_cfg.get("display_name") or "")
        # tool_ids 随 agent 配置装载，三态：list = 白名单即写（含显式空表 =
        # agent 声明零工具，须与"未声明/断链"区分，下游 tool_schema 据此跳过
        # 断链告警）；"inherit" = 继承基线全量工具面（附身等场景：执行者身份
        # 换、工具能力保留），不写本键——主 agent 工具清单更新自动跟随；其余
        # （未声明/断链）同样不写。本键唯一供给方是本插件，tool_schema 经
        # tool-surface capability 据此过滤工具面（工具面单真值 = agent yaml
        # tool_ids 三态，模式级收窄已退役——设计 D10）。
        _tool_ids = agent_cfg.get("tool_ids")
        if isinstance(_tool_ids, list):
            merged, mode_source = await self._merge_mode_tools(list(_tool_ids), ctx)
            updates["tool_ids"] = merged
            if mode_source:
                # 审计键独立于 tool_ids 列表（内核对列表内非字符串条目静默丢弃，
                # 标记不得内嵌；同刀进本插件 export_fields，漏声明 = 审计出口断链）。
                updates["tool_ids.mode_source"] = mode_source
        # 动态面否决集（ADR 2026-11-18-agent-dynamic-tool-deny-list）：agent yaml
        # tool_deny 非空 list = 该 agent 搜不到（resource_search 过滤）也调不动
        # （call_tool 门禁）的工具名集；空表/未声明不写键（缺席 = 无否决）。
        # 非字符串条目与 tool_ids 同口径丢弃（内核列表键契约）。原生白名单面
        # 不受此键影响——deny 只收动态面，tool_ids 仍是原生面单真值。
        _tool_deny = agent_cfg.get("tool_deny")
        if isinstance(_tool_deny, list):
            deny_names = [str(t) for t in _tool_deny if isinstance(t, str) and t.strip()]
            if deny_names:
                updates["tool_deny"] = deny_names
        # 卡产物装载（regex_scripts / rp_state_* / rp_engine / rp_actions 与
        # pack 合并管线）单点在 _load_card_extensions，此处只并结果。
        updates.update(
            await self._load_card_extensions(agent_cfg, agent_id_key or "agentos", ctx)
        )
        # 任务域出生投影（ADR 2026-08-28 声明化）：血缘扁平键存在 = 任务管道
        # （聊天管道无血缘键，不写 task.*）——task.id 即引擎管道 id，调用方预传
        # 值一律覆盖（身份权威在引擎 id，此处统一收口）。
        if any(str(k).startswith("lineage.") for k in ctx.state):
            updates["task.id"] = str(ctx.state.get("pipeline_id", "") or "")
        # 项目范围声明（ADR 2026-09-17 决策 4）：挂靠项目（task.parent_project_id
        # 单跳键，task_submit 唯一写面）的管道注入 state.project_roots = 项目根
        # 列表（读范围 + 容器挂载清单基准）；写锚点由任务自身 project_id 推导，
        # 不设第二个 state 字段。解析失败降级不注入（与模式物料同降级语义，
        # 绝不阻断管道）。
        _project_id = str(ctx.state.get("task.parent_project_id", "") or "")
        if _project_id:
            roots = await self._resolve_project_roots(_project_id)
            if roots:
                updates["project_roots"] = roots
            else:
                logger.warning(
                    "[agent_config_load] project_id 不在登记中，project_roots 降级不注入"
                    " | project_id=%s",
                    _project_id,
                )
        else:
            # 主会话挂靠解析（ADR 2026-10-19-session-workspace-project-binding）：
            # 无任务挂靠键时按会话声明工作空间（state.workspace，主会话显式
            # source_path 写面）对登记簿路径精确匹配，命中即注入——会话选空间
            # 随建即登记（registerSessionProject）后，主 agent 的
            # {{project_agents_md_anchor}} 才有展开资格。未声明/不命中/服务
            # 不可用一律零注入（默认会话沙箱与登记路径构造性互斥）。
            _session_roots = await self._resolve_session_project_roots(ctx)
            if _session_roots:
                updates["project_roots"] = _session_roots
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
        # 模型落定（初始化步，落定键 2026-10-18）：state 无显式 model_id 时经
        # llm_service model.resolve 解析（显式 id > tiers[tier] > defaults.chat，
        # 与 llm_core 选模同源）写 state.model_id——prepare 链下游（prompt_build
        # 规则条件等）与 llm_core 消费同一事实，消灭各消费方自行近似解析。
        # state 已显式给出 model_id（overlay/上游注入）时不调用不覆盖。
        if ctx.state.get("model_id") in (None, ""):
            _tier = ctx.state.get("model_tier") or updates.get("model_tier") or ""
            _resolved_model = await self._resolve_initial_model(str(_tier))
            if _resolved_model:
                updates["model_id"] = _resolved_model
        # agent 层级：yaml level（如 code_writer L3）覆盖插件默认 L1——子任务
        # 管道按目标 agent 定层级（L1 豁免会让 task_reminder 评估闸门旁路）。
        # 插件配置显式 agent_level 仍最高优先。只覆盖本轮局部值，不回写实例。
        if agent_cfg.get("level") and self._config.get("agent_level") is None:
            lvl = str(agent_cfg["level"]).strip().upper()
            if lvl.startswith("L"):
                agent_level = lvl

        # 2. Agent 身份信息（层级单一真值：顶层 agent_level，level_guard/
        # isolation_guard/tool_schema/param_inject 等下游统一读此键）
        updates["context.agent_name"] = agent_name

        # 始终用实际 Agent 层级覆盖 state 中的 AGENT_LEVEL，
        # 防止子管道继承父管道的层级（如 L2 agent 错误继承 L1）。
        updates[StateKeys.AGENT_LEVEL] = agent_level

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

        # 6. 工具执行标记（本轮有待执行调用 ⟺ raw_tool_calls 非空）
        updates["context.is_tool_execution"] = bool(ctx.state.get(StateKeys.RAW_TOOL_CALLS))

        # 7. 项目级标记
        updates["context.is_project"] = agent_level == "L1"

        logger.debug(
            "[%s] Context built | agent=%s | level=%s | iteration=%d",
            self.name,
            self._agent_name,
            agent_level,
            iteration,
        )

        return updates

    async def _merge_mode_tools(
        self, original: list[str], ctx: PluginContext
    ) -> tuple[list[str], dict[str, str]]:
        """模式声明工具并入（ADR 2026-10-12-personal-mode-package-form-and-boundary，
        审查报告 `模式工具并入机制审查_20261012.md` §3 落点；任务模式标签注入
        见 ADR 2026-11-11-mode-scoped-assets-and-plugin-verify-tool D2）。

        两类来源、两种边界：
        - **模式包 capabilities.tools**（``collect_mode_tool_ids`` 并集）：
          仅非任务管道并入（state 无 ``lineage.*`` 血缘键）；任务管道维持
          结构性不可见（权限面收紧方向）。
        - **当前激活模式的 ``inject_tool_ids``**（``mode.yaml`` 声明，工具由
          别的插件注册）：任务/非任务管道都并入——这是"按任务模式标签挂载
          外部工具"的唯一入口（例：mode_evolution 借 plugin_verify）。

        并入仍需活注册表命中：tool-surface 探针取交集（同解禁用/G2 遮挡/
        未注册三类面）；探针通道未接线/调用失败 = 本轮不并入（降级可见：
        warning 留痕，权限面不放宽）。

        合并语义：去重保序追加（tool_ids 全部消费方假设 = list[str]、顺序
        不敏感、重复无害，审查 Q7）。

        Returns:
            (并入后清单, 审计映射)——审计映射空 = 本轮零并入（键不写，缺席
            语义不靠空值表达）。
        """
        is_task_pipeline = any(str(k).startswith("lineage.") for k in ctx.state)
        active_mode = resolve_active_mode(ctx.state)
        injected = collect_mode_inject_tool_ids(active_mode) if active_mode else {}
        declared = {} if is_task_pipeline else collect_mode_tool_ids()
        declared.update(injected)
        if not declared:
            return original, {}
        live = await self._probe_live_tools(sorted(declared))
        if not live:
            return original, {}
        merged = list(original)
        mode_source: dict[str, str] = {}
        for name in live:
            mode_source[name] = declared[name]
            if name not in merged:
                merged.append(name)
        logger.info(
            "[agent_config_load] 模式声明工具并入 | task_pipeline=%s | mode=%s | "
            "tools=%s | sources=%s",
            is_task_pipeline,
            active_mode or "-",
            live,
            sorted(set(mode_source.values())),
        )
        return merged, mode_source

    async def _probe_live_tools(self, names: list[str]) -> list[str]:
        """tool-surface 探针：声明工具名 ∩ 活注册表（排序稳定序返回）。"""
        if self._capability_caller is None:
            logger.warning(
                "[agent_config_load] tool-surface caller 未接线，模式声明工具本轮"
                "不并入（降级安全方向；server.py on_load 应注入 capability caller）"
            )
            return []
        try:
            result = await self._capability_caller("schemas", {"tool_ids": names})
        except Exception as exc:  # noqa: BLE001 — 通道断链降级不并入（不放宽权限面）
            logger.warning(
                "[agent_config_load] tool-surface 探针失败，模式声明工具本轮不并入"
                "（降级安全方向）| err=%s",
                exc,
            )
            return []
        schemas = result.get("schemas") or [] if isinstance(result, dict) else []
        available = {
            (s.get("function") or {}).get("name") for s in schemas if isinstance(s, dict)
        }
        return [name for name in names if name in available]

    async def _load_card_extensions(
        self, agent_cfg: dict[str, Any], agent_id: str, ctx: PluginContext
    ) -> dict[str, Any]:
        """卡产物装载（RP 批 1a→批 3 声明面）：agent yaml 卡 extensions 与
        pack 合并管线 → context.*（regex_scripts / rp_state_schema /
        rp_state_behaviors / rp_state_hard_rules / rp_engine / rp_actions）。

        各块契约注释随块内迁（无声明/空 = 不写键的兼容态零行为变化不变）；
        本方法只装载与 warning 面，不做判定（值域执法归消费面 Validator）。
        agent_id 为已解析缺省的执行身份键（pack 包内根解析同源）。
        声明面 lint 经 card 质检服务（T6c 服务化），故为协程。
        """
        updates: dict[str, Any] = {}
        _extensions = agent_cfg.get("extensions")
        _regex_scripts = (
            _extensions.get("regex_scripts") if isinstance(_extensions, dict) else None
        )
        if isinstance(_regex_scripts, list) and _regex_scripts:
            updates["context.regex_scripts"] = _regex_scripts
        # 卡状态契约声明随 agent 配置装载（RP 批 2-Ⅰ 2a）：agent yaml（模式卡）
        # extensions.variables / extensions.rules.hard_rules → context.rp_state_*，
        # AI 输出侧 state_marker_parse 落账前经 state 消费（State Contract
        # Validator，契约见 state_contract 模块）。RP 批 2-Ⅱ 起与 pack 合并产物
        # 走同一管线（下方）：无声明/空 = 不写键（兼容态零行为变化）；结构合法
        # 性由 card 质检服务（装载侧 warning 面）+ 消费面 fail-soft 双层把关。
        _card_variables = (
            _extensions.get("variables") if isinstance(_extensions, dict) else None
        )
        _card_behaviors = (
            _extensions.get("behaviors") if isinstance(_extensions, dict) else None
        )
        _card_actions = (
            _extensions.get("actions") if isinstance(_extensions, dict) else None
        )
        # 玩法包 pack 资产（RP 批 2-Ⅱ 2c+，FR-B6 Tier 0）：卡顶层 pack_ids 声明序
        # 双根加载（用户层 <user_config_dir>/packs 同 id 接管 → 回落包内
        # <mode_pkg>/packs，机制同 lorebook_ids 双根）；Tier 0 面 variables/
        # behaviors 并入装载（多 pack 后者逐键覆盖、卡覆盖 pack，合并语义见
        # rp_packs 模块 docstring）；graph/template/ui 键忽略 + warning（消费随
        # 批 4 宿主）。无 pack_ids = 零并入（兼容态零行为变化）。
        # 开演向导增量（RP 面板刀-Ⅰ）：execution_context.wizard_pack_ids 并入
        # 待载清单（卡声明序在前、向导追加在后，去重保序；键缺席零变化）。
        _pack_ids = agent_cfg.get("pack_ids")
        _pack_id_list = (
            [str(pid) for pid in _pack_ids] if isinstance(_pack_ids, list) else []
        )
        for _wizard_pack in _wizard_extra_ids(ctx.state, WIZARD_PACK_IDS_KEY):
            if _wizard_pack not in _pack_id_list:
                _pack_id_list.append(_wizard_pack)
        _packs: list[dict[str, Any]] = []
        if _pack_id_list:
            _packs, _pack_warnings = load_packs(
                _pack_id_list,
                self._pack_pkg_dir(agent_id),
                _user_packs_dir(),
            )
            for _warning in _pack_warnings:
                logger.warning("[agent_config_load] %s", _warning)
            # pack 呈现面声明（ui/init_ui）不入装载 state，只做 card 质检服务
            # 同源结构 warning 面（归一剔除归呈现消费侧 mode_roleplay server，
            # 见 rp_packs 模块 docstring 呈现面节）；服务缺席降级跳过。
            for _pack in _packs:
                _pack_ui_lint = await self._card_lint(
                    "lint_pack_ui",
                    {"ui": _pack.get("ui"), "init_ui": _pack.get("init_ui")},
                )
                for _warning in (_pack_ui_lint or {}).get("warnings") or []:
                    logger.warning("[agent_config_load] %s", _warning)
        _schema, _behaviors, _decl_warnings = merge_state_decl(
            _card_variables if isinstance(_card_variables, dict) else None,
            _card_behaviors if isinstance(_card_behaviors, dict) else None,
            _packs,
        )
        # 声明面结构 lint（card 质检服务，单源在 mode_roleplay card_lint）：
        # warning 日志面——作者面错误可见，装载仍 verbatim（值域执法归
        # Validator 消费面）。
        _decl_lint = await self._card_lint(
            "lint_decl", {"variables": _schema, "behaviors": _behaviors}
        )
        for _warning in (*_decl_warnings, *((_decl_lint or {}).get("warnings") or [])):
            logger.warning("[agent_config_load] %s", _warning)
        if _schema:
            updates["context.rp_state_schema"] = _schema
        if _behaviors:
            updates["context.rp_state_behaviors"] = _behaviors
        if isinstance(_extensions, dict):
            _rules = _extensions.get("rules")
            _hard_rules = _rules.get("hard_rules") if isinstance(_rules, dict) else None
            if isinstance(_hard_rules, dict) and _hard_rules:
                updates["context.rp_state_hard_rules"] = _hard_rules
        # 卡 engine 声明随 agent 配置装载（RP 批 3，作者权威——ADR 2026-10-06）：
        # agent yaml（模式卡）extensions.engine → context.rp_engine（与 rp_state_*
        # 同一卡产物经 state 传递通道）。消费子集 = allow_cheat（/cheat 拦截面）
        # + cheat_overrides.keep_state_machine（Validator 例外执法）
        # + allow_state_proposals（2026-10-13 动作驱动基线回退开关，消费面
        # state_marker_parse：state 字段默认关，显式 true 恢复 LLM 提议合法来源）；
        # llm / rule_complexity / contract_level 声明面先行收下透传、消费面登记
        # 后置（不假装生效）。无声明/空/非映射 = 不写键（兼容态零行为变化）；
        # 结构合法性由 card 质检服务 lint_engine 面 + 消费面 fail-soft 双层把关。
        _card_engine = (
            _extensions.get("engine") if isinstance(_extensions, dict) else None
        )
        if isinstance(_card_engine, dict) and _card_engine:
            _engine_lint = await self._card_lint("lint_decl", {"engine": _card_engine})
            for _warning in (_engine_lint or {}).get("warnings") or []:
                logger.warning("[agent_config_load] %s", _warning)
            updates["context.rp_engine"] = _card_engine
        # actions target 面（RP 批 2-Ⅱ 2d）：合法值 {engine, game}、缺省 engine、
        # 非法剔除 warning（fail-soft，与 regex 坏脚本同待遇）；合法条目规范化后
        # 写 context.rp_actions——消费面 = state_marker_parse 动作通道（2026-10-13
        # 动作驱动基线：engine 落点声明效果经 Validator、game 落点转发宿主）。
        # 服务缺席 = 归一不可执行，本轮不写 rp_actions（降级提示；非法条目
        # 不下发为安全方向）。
        if _card_actions is not None:
            _actions_lint = await self._card_lint(
                "lint_actions", {"actions": _card_actions}
            )
            if _actions_lint is not None:
                for _warning in _actions_lint.get("warnings") or []:
                    logger.warning("[agent_config_load] %s", _warning)
                _actions = _actions_lint.get("actions") or []
                if _actions:
                    updates["context.rp_actions"] = _actions
        # 开演 init effects 快照（RP 面板 v3.1 刀1）：execution_context.rp_init
        # → context.rp_init_effects（契约见 RP_INIT_KEY 常量注释；键域校验放
        # 消费侧 state_marker_parse Validator fail-closed，本步零键域判定）。
        # 键缺席/空白/全坏段 = 不写键（兼容态零行为变化）。
        _rp_init = _rp_init_effects(ctx.state)
        if _rp_init:
            updates["context.rp_init_effects"] = _rp_init
        return updates

    @staticmethod
    def _pack_pkg_dir(agent_id: str) -> str | None:
        """卡 agent.id 模式键 → 模式包目录（pack 包内根，双根解析经 mode_keys）。

        非模式键 / 包不可得 = None（仅用户层 packs 参与，合法形态零动作）——
        通用机制零模式特判（与 find_mode_agent_yaml 同一解析族）。
        """
        parsed = parse_mode_agent_key(agent_id)
        if parsed is None:
            return None
        pkg_dir = find_mode_package_dir(parsed[0])
        return str(pkg_dir) if pkg_dir is not None else None

    async def _resolve_project_roots(self, project_id: str) -> list[str]:
        """project_id → 项目根列表（project.list_paths 服务消费，T6a 收口项目域主）。

        登记行是 id ↔ 路径唯一真值（ADR 2026-09-17 决策 4）；服务不可用/未命中
        返回空表（调用方降级不注入——热路径读面 fail-soft，绝不阻断管道）。
        """
        caller = self._project_service_caller
        if caller is None:
            logger.warning(
                "[agent_config_load] project 服务调用方未注入，project_roots 降级不注入"
            )
            return []
        try:
            result = await caller("list_paths", {})
        except Exception as exc:  # noqa: BLE001 — 登记读面降级可见，不阻断管道
            logger.warning(
                "[agent_config_load] project.list_paths 失败，project_roots 降级不注入 | err=%s",
                exc,
            )
            return []
        paths = result.get("paths") if isinstance(result, dict) else None
        path = str((paths or {}).get(project_id, "") or "") if isinstance(paths, dict) else ""
        return [path] if path else []

    async def _resolve_session_project_roots(self, ctx: PluginContext) -> list[str]:
        """会话声明工作空间 → 登记项目根（路径精确匹配，主会话挂靠形态）。

        主会话无 task.parent_project_id 可读，但显式声明工作空间时
        workspace_lifecycle 已把声明目录写入 state.workspace；会话创建流
        （registerSessionProject）以同一目录随建即登记，故与登记簿路径
        规范化后精确匹配即挂靠成立。未声明/不命中/服务异常一律空表
        （降级不注入，与任务路径同语义）；服务未接线静默跳过——聊天
        热路径每轮都进本分支，warning 会刷屏。
        """
        workspace = str(ctx.state.get("workspace", "") or "").strip()
        if not workspace:
            return []
        caller = self._project_service_caller
        if caller is None:
            logger.debug(
                "[agent_config_load] project 服务调用方未注入，会话挂靠解析跳过"
            )
            return []
        try:
            result = await caller("list_paths", {})
        except Exception as exc:  # noqa: BLE001 — 登记读面降级可见，不阻断管道
            logger.warning(
                "[agent_config_load] project.list_paths 失败，会话挂靠不注入 | err=%s",
                exc,
            )
            return []
        paths = result.get("paths") if isinstance(result, dict) else None
        if not isinstance(paths, dict):
            return []
        want = _canonical_fs_path(workspace)
        for registered in paths.values():
            path = str(registered or "")
            if path and _canonical_fs_path(path) == want:
                return [path]
        return []
