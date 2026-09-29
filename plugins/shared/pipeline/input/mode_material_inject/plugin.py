"""模式物料注入 Input 插件 — 通用模式物料步骤（职责终局三件，设计 D10 2026-09-28）。

context_build 回归纯上下文构建后，模式物料由本独立管道步骤承载。本插件是
**通用**模式步骤（任何模式共用，零具体模式专属逻辑），按 state 激活：

1. 激活判定（优先序，设计 D6）：显式 state["execution_context"]["mode"]
   非空 > 按 state["agent.id"] 归属包派生（agent 键 mode_X/<stem> → X，包
   目录存在才派生）> 空。空 = 零动作直通（不写任何键）；形态非法 = warning
   后直通。
2. mode 观测回写：updates["mode"] = 解析结果（观测链出口：/pipelines/state
   摘要 mode → 前端模式面板自动弹出/模式徽标同源取数，无键零动作）。
3. persona 接管（通用机制，设计 D7）：mode.yaml persona 声明 + 携带文本 →
   state["context.persona_text"]（prompt_build 的 {{persona:}} 占位符据此
   换源，替换提示词人设段，骨架不动）。
4. 模式包组装器（模式专属物料出口，通用约定）：模式包可在包内提供
   material.py（种子自包含，不 import 共享根），约定暴露
   ``def build_injection(state, pkg_dir) -> str``——返回本模式的追加注入
   文本（无则空串）。本插件按真实名 ``mode_<mode>.material`` 动态加载
   （sys.modules 注册防双实例）后调用；组装器声明 books_dir/user_agents_dir
   形参时（用户层物料双根扩展点）注选传入真实用户目录，两参约定出口向后
   兼容零打扰；非空则以 "\n\n" 追加到 context.system_prompt 尾部。无
   material.py = 合法形态零追加；加载失败/函数缺失/调用异常 → warning 一次
   + 跳过（降级不阻断管道）。

模式知识注入唯一面 = prompt_build 的 {{mode_catalog}} 目录（描述+路由）；
工具面单真值 = agent yaml tool_ids 三态（模式级收窄退役）。

全路径失败均降级为 warning + 不写入（不抛出），管道行为与无模式一致。
优先级：11——紧跟 context_build（10）之后、tool_schema/prompt_build 之前
（persona 接管与物料追加须先于提示词组装落 state）。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mode_material import (
    MODE_ID_RE,
    derive_mode_from_agent,
    find_package_dir,
    resolve_mode,
    resolve_persona_takeover,
)
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)


class ModeMaterialInjectPlugin(IInputPlugin):
    """模式物料注入 Input 插件（通用模式步骤，职责终局三件）。

    激活时产出键：mode（观测回写）、context.persona_text（人设接管）、
    context.system_prompt（模式包物料追加）。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化模式物料注入插件。

        Args:
            config: 插件配置字典（预留；当前无消费键）。
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "mode_material_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return self._config.get("priority", 11)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """按 state 激活并注入模式物料。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含模式物料 state 更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行模式物料注入逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            要写入 state 的字段字典（未激活 = 空字典零写入）
        """
        updates: dict[str, Any] = {}
        state = ctx.state
        # 优先序（设计 D6）：显式 execution_context.mode > agent 归属派生 > 空。
        # 显式值在场即胜出（含无包的标签值——mode 是标签不是枚举）；显式值
        # 形态非法时走下方 warning 直通，不回落派生（在场意图不被静默替换）。
        mode = resolve_mode(state) or derive_mode_from_agent(state)
        # 无 mode（显式缺席且无归属派生：自动模式/聊天直连）= 零动作直通：
        # 不回写、不取数、零开销。
        if not mode:
            return updates
        if not MODE_ID_RE.match(mode):
            logger.warning(
                "[mode_material_inject] mode 键形态非法，跳过模式物料注入 | mode=%r",
                mode,
            )
            return updates
        # state 顶层 mode 键回写（观测链出口，消费契约见模块 docstring）。
        # 解析成功即回写、逐轮覆盖；物料注入降级（组装器失败）不回滚——回写
        # 是解析路径产物，不随注入成败翻转。
        updates["mode"] = mode
        pkg_dir = find_package_dir(mode)
        persona_text = resolve_persona_takeover(pkg_dir, state)
        if persona_text:
            # 人设接管（通用机制）：mode.yaml persona 声明 + 携带文本 →
            # prompt_build 的 {{persona:}} 占位符换源（替换提示词人设段，
            # 骨架不动）。注意缓存语义：接管改变系统提示词前缀，破坏前缀缓存
            # 命中——提示职责在前端附身建立时告知用户。
            updates["context.persona_text"] = persona_text
            logger.info(
                "[mode_material_inject] 人设接管生效 | mode=%s | chars=%d",
                mode,
                len(persona_text),
            )
        self._append_package_material(state, mode, pkg_dir, updates)
        return updates

    def _append_package_material(
        self,
        state: dict[str, Any],
        mode: str,
        pkg_dir: Path | None,
        updates: dict[str, Any],
    ) -> None:
        """模式包组装器追加段：material.py::build_injection 非空则以空行衔接。

        用户层物料目录（卡/世界书双根）按组装器签名注选传：约定出口为
        build_injection(state, pkg_dir) 两参形态（向后兼容零打扰），声明
        books_dir / user_agents_dir 形参的模式包才收到真实用户目录
        （user_config_dir()/lorebooks 与 /agents；None = 用户空间不可得，
        组装器侧语义为仅包内）。
        """
        builder = self._load_builder(mode, pkg_dir, self._read_material_decl(pkg_dir))
        if builder is None:
            return
        kwargs = self._user_layer_dir_kwargs(builder)
        try:
            text = builder(state, pkg_dir, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 降级语义：组装器失败不阻断管道
            logger.warning(
                "[mode_material_inject] 模式包物料组装失败，跳过追加 | mode=%s | err=%s",
                mode,
                exc,
            )
            return
        if isinstance(text, str) and text.strip():
            self._append_section(state, updates, text)
            logger.info(
                "[mode_material_inject] 模式包物料段已注入 | mode=%s | chars=%d",
                mode,
                len(text),
            )

    @staticmethod
    def _user_layer_dir_kwargs(builder: Callable[..., Any]) -> dict[str, str | None]:
        """按组装器签名注入用户层物料目录 kwargs（声明形参才传）。

        user_space 共享根函数内导入（server.py 种子内联同形态）；解析失败/
        用户空间未配置 = None（组装器侧语义：仅包内，降级不阻断）。
        """
        try:
            params = set(inspect.signature(builder).parameters)
        except (TypeError, ValueError):
            return {}
        wanted = {"books_dir", "user_agents_dir"} & params
        if not wanted:
            return {}
        root: Path | None = None
        try:
            from user_space import user_config_dir

            root = user_config_dir()
        except Exception:  # noqa: BLE001 — 降级语义：用户空间不可得即仅包内
            root = None
        dirs: dict[str, str | None] = {
            "books_dir": str(root / "lorebooks") if root is not None else None,
            "user_agents_dir": str(root / "agents") if root is not None else None,
        }
        return {key: dirs[key] for key in wanted}

    @staticmethod
    def _read_material_decl(pkg_dir: Path | None) -> str:
        """直读包内 mode.yaml 的 material 声明（组装器入口，声明面单一真值）。

        文件面事实直读不经服务通道（modes API 同源口径）：包缺失/
        mode.yaml 缺失/解析失败/字段缺席 = 无声明（组装器不加载，合法形态）。
        """
        if pkg_dir is None:
            return ""
        decl_path = pkg_dir / "mode.yaml"
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(decl_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "[mode_material_inject] mode.yaml 不可读，组装器声明缺席 | path=%s | err=%s",
                decl_path,
                exc,
            )
            return ""
        if isinstance(data, dict):
            decl = data.get("material")
            if isinstance(decl, str):
                return decl.strip()
        return ""

    @staticmethod
    def _load_builder(
        mode: str, pkg_dir: Path | None, material_decl: str
    ) -> Callable[..., Any] | None:
        """按 mode.yaml material 声明动态加载组装器（``<file>::<出口>`` 形态）。

        - 无声明 = 包未带组装器（合法形态）→ None 静默（声明驱动：包内文件
          不再按约定名回退发现）；
        - 声明了但文件缺失 = 声明与实物不符 → warning + None；
        - 加载失败 / 声明出口缺失 → warning 一次 + None（降级不阻断）；
        - sys.modules 按声明派生名注册防双实例（同进程重复激活共享模块对象）。
        """
        if pkg_dir is None or not material_decl:
            return None
        file_part, _, func_name = material_decl.partition("::")
        func_name = func_name.strip() or "build_injection"
        file_name = file_part.strip()
        mod_name = f"mode_{mode}._material_{file_name.replace('/', '_').replace('.', '_')}"
        cached = sys.modules.get(mod_name)
        if cached is not None:
            func = getattr(cached, func_name, None)
            return func if callable(func) else None
        path = pkg_dir / file_name
        if not path.is_file():
            logger.warning(
                "[mode_material_inject] material 声明文件缺失，跳过追加 | mode=%s | decl=%s",
                mode,
                material_decl,
            )
            return None
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            logger.warning(
                "[mode_material_inject] material spec 构造失败，跳过追加 | mode=%s",
                mode,
            )
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 — 降级语义：坏组装器不阻断管道
            sys.modules.pop(mod_name, None)
            logger.warning(
                "[mode_material_inject] material 加载失败，跳过追加 | mode=%s | err=%s",
                mode,
                exc,
            )
            return None
        func = getattr(mod, func_name, None)
        if not callable(func):
            logger.warning(
                "[mode_material_inject] material 缺声明出口 %s，跳过追加 | mode=%s",
                func_name,
                mode,
            )
            return None
        return func

    @staticmethod
    def _append_section(
        state: dict[str, Any], updates: dict[str, Any], section: str
    ) -> None:
        """追加注入段到 context.system_prompt 尾部（基线提示词保留，追加非替换）。

        基座 = 本轮已追加结果（updates）优先，回落上游（context_build）写入的
        state 值；上游未写（断链/单测裸 state）= 本段自成基座。
        """
        base = str(updates.get("context.system_prompt") or state.get("context.system_prompt") or "")
        updates["context.system_prompt"] = f"{base}\n\n{section}" if base else section
