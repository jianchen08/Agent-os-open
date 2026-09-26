"""模式物料注入 Input 插件 — 通用模式物料步骤（2026-09-24 架构重构）。

context_build 回归纯上下文构建后，模式物料由本独立管道步骤承载。本插件是
**通用**模式步骤（任何模式共用，零具体模式专属逻辑），按 state 激活：

1. 激活判定：state["execution_context"]["mode"] 非空且形态合法 → 激活；
   否则零动作直通（不写任何键，无 profile 取数）。
2. mode 观测回写：updates["mode"] = 解析结果（观测链出口：/pipelines/state
   摘要 mode → 前端模式面板自动弹出/模式徽标同源取数，无键零动作）。
3. 基础模式段：profile 经注入的取数通道（server.py 接线 mode.get_profile）
   取数，组装路由指引（编排清单/调度链/执行者池）+ 模式口径段 + 工具面注记，
   追加进 state["context.system_prompt"] 尾部；tool_ids 收窄仅当 state 基线
   存在且模式声明 material_scope.tool_ids（只收窄不扩权；基线缺失含
   tool_ids: inherit 继承全量面时不套用）。
4. 模式包组装器（模式专属物料出口，通用约定）：模式包可在包内提供
   material.py（种子自包含，不 import 共享根），约定暴露
   ``def build_injection(state, pkg_dir) -> str``——返回本模式的追加注入
   文本（无则空串）。本插件按真实名 ``mode_<mode>.material`` 动态加载
   （sys.modules 注册防双实例）后调用；组装器声明 books_dir/user_agents_dir
   形参时（用户层物料双根扩展点）注选传入真实用户目录，两参约定出口向后
   兼容零打扰；非空则以 "\\n\\n" 追加到 context.system_prompt 尾部。无
   material.py = 合法形态零追加；加载失败/函数缺失/调用异常 → warning 一次
   + 跳过（降级不阻断管道）。

全路径失败均降级为 warning + 不写入（不抛出），管道行为与无模式一致。
优先级：11——紧跟 context_build（10）之后、tool_schema/prompt_build 之前
（模式段与工具面收窄须先于工具面过滤与提示词组装落 state）。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mode_material import (
    MODE_ID_RE,
    build_mode_section,
    find_package_dir,
    resolve_mode,
    tool_surface_note,
    unwrap_mode_profile,
)
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# 模式 profile 取数通道：async (mode) -> profile dict；实现 = server.py 经
# tool-executor 显式 plugin_id 调 mode.get_profile（eval_harness 先例同通道）。
# None = 通道未接线（降级）。
ProfileFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class ModeMaterialInjectPlugin(IInputPlugin):
    """模式物料注入 Input 插件（通用模式步骤）。

    激活时产出三个键：mode（观测回写）、context.system_prompt（模式段与
    模式包物料追加）、tool_ids（仅模式声明收窄且 state 基线存在时）。

    Attributes:
        _config: 插件配置字典
        _profile_fetcher: 模式 profile 取数通道（server.py 接线；None = 未接线）
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        profile_fetcher: ProfileFetcher | None = None,
    ) -> None:
        """初始化模式物料注入插件。

        Args:
            config: 插件配置字典（预留；当前无消费键）。
            profile_fetcher: 模式 profile 取数通道（server.py 接线 mode.get_profile
                服务调用；缺省 None = 通道未接线，基础模式段降级不注入）。
        """
        self._config = config or {}
        self._profile_fetcher = profile_fetcher

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
        mode = resolve_mode(state)
        # 无 mode 键（自动模式/聊天直连）= 零动作直通：不回写、不取数、零开销。
        if not mode:
            return updates
        if not MODE_ID_RE.match(mode):
            logger.warning(
                "[mode_material_inject] mode 键形态非法，跳过模式物料注入 | mode=%r",
                mode,
            )
            return updates
        # state 顶层 mode 键回写（观测链出口，消费契约见模块 docstring）。
        # 解析成功即回写、逐轮覆盖；物料注入降级（通道未接线/取数失败/组装器
        # 失败）不回滚——回写是解析路径产物，不随注入成败翻转。
        updates["mode"] = mode
        pkg_dir = find_package_dir(mode)
        await self._inject_base_section(state, mode, pkg_dir, updates)
        self._append_package_material(state, mode, pkg_dir, updates)
        return updates

    async def _inject_base_section(
        self,
        state: dict[str, Any],
        mode: str,
        pkg_dir: Path | None,
        updates: dict[str, Any],
    ) -> None:
        """基础模式段：profile 取数 → 路由/口径/工具面注记追加 + tool_ids 收窄。"""
        if self._profile_fetcher is None:
            logger.warning(
                "[mode_material_inject] 模式 profile 取数通道未接线，"
                "基础模式段降级不注入 | mode=%s",
                mode,
            )
            return
        try:
            raw = await self._profile_fetcher(mode)
            profile = unwrap_mode_profile(raw)
        except Exception as exc:  # noqa: BLE001 — 降级语义：任何取数失败都不阻断管道
            logger.warning(
                "[mode_material_inject] mode.get_profile 调用失败，"
                "基础模式段降级不注入 | mode=%s | err=%s",
                mode,
                exc,
            )
            return
        baseline = state.get("tool_ids")
        note, narrowed = tool_surface_note(
            profile, baseline if isinstance(baseline, list) else None
        )
        section = build_mode_section(mode, profile, pkg_dir, note)
        self._append_section(state, updates, section)
        if narrowed is not None:
            updates["tool_ids"] = narrowed
        logger.info(
            "[mode_material_inject] 基础模式段已注入 | mode=%s | pkg_dir=%s | tool_ids=%s",
            mode,
            pkg_dir,
            "收窄" if narrowed is not None else "维持基线",
        )

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
        builder = self._load_builder(mode, pkg_dir)
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
    def _load_builder(mode: str, pkg_dir: Path | None) -> Callable[..., Any] | None:
        """按真实名动态加载模式包组装器 ``mode_<mode>.material::build_injection``。

        - 包目录缺失 / 无 material.py = 包未带组装器（合法形态）→ None 静默；
        - 加载失败 / build_injection 出口缺失 → warning 一次 + None（降级不阻断）；
        - sys.modules 按真实名注册防双实例（同进程重复激活共享同一模块对象）。
        """
        if pkg_dir is None:
            return None
        mod_name = f"mode_{mode}.material"
        cached = sys.modules.get(mod_name)
        if cached is not None:
            func = getattr(cached, "build_injection", None)
            return func if callable(func) else None
        path = pkg_dir / "material.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            logger.warning(
                "[mode_material_inject] material.py spec 构造失败，跳过追加 | mode=%s",
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
                "[mode_material_inject] material.py 加载失败，跳过追加 | mode=%s | err=%s",
                mode,
                exc,
            )
            return None
        func = getattr(mod, "build_injection", None)
        if not callable(func):
            logger.warning(
                "[mode_material_inject] material.py 缺 build_injection 出口，跳过追加"
                " | mode=%s",
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
