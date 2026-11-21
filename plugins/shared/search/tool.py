"""资源搜索工具（0.2 精简实现）。

原始 0.1 ``ResourceSearchTool`` 依赖大量基础设施（``tools.global_registry``、
``agents.global_registry``、``skills.registry``、``tools.auto_loader``、
``tools.loader``、``db.models.ToolLibrary``、``config.config_center``、
``infrastructure.db``），这些在 MCP sidecar 进程里都不可用。本文件是按
"实际可用资源" 原则重写的精简实现：

- 保留工具对外契约不变：工具名仍是 ``resource_search``，schema 与原版
  完全一致（``resource_type`` / ``query`` / ``mode`` / ``filters`` / ``limit``），
  ``get_tool_definition()`` 仍返回 ``Tool``。
- Agent 面：本工具不自持扫描，经 ``server.py`` 注入的服务调用方消费
  agent_manager 的聚合注册表服务（``agent.list`` 系统空间 + 插件包 +
  用户空间；``agent.get`` detailed 模式补全配置），匹配/过滤/limit 在
  消费侧对服务条目执行。服务不可用/调用失败 = 显式失败信封，禁止回落
  文件扫描（资源注册收敛方案 T2 fail-closed）。
- Skill 面：同款服务化（T3）——消费 skill_manager 注册权威的
  ``skill.list``（元数据 id/name/description/languages）与 ``skill.get``
  （单技能详情含正文，正文兜底匹配与 detailed 列按需取），匹配/过滤/
  limit 在消费侧执行，fail-closed 同上。
- 工具面：manifest 扫描（``plugins/**/plugin.json``，行源：描述/tags/
  匹配语义）与内核活注册表对账（T4）——经 server.py 注入的 HTTP 通道调
  冻结契约端点 ``GET /api/v1/tools``（活注册表已过 enablement 过滤，
  禁用插件工具不出口），对账键 = 工具名集合交，只列活的。通道不可用 =
  显式失败信封，禁止回落纯扫描（回落 = 禁用插件工具重新漏进结果）。
- 不依赖向量数据库或外部平台，因此在 sidecar 进程里能完整执行而非空回退。

返回结构：``{<type>_h: [...], <type>_d: [[...]], <type>_c: N,
message?: str}``，由 ``server.py`` 的 ``tool_result_envelope`` 归一返回。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from constants import ToolLimits

from agentos_plugin_sdk import (
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

# 跨插件共享类型已上提到 SDK 公共依赖层 agentos_plugin_sdk。
# ToolLimits 仅本工具使用，就近放在本工具目录 constants.py（server.py 已将本工具
# 目录注入 sys.path），直接 import。

logger = logging.getLogger(__name__)

# 项目根（search/tool.py → search/ → tools/ → shared/ → plugins/ → root）。
# 仅工具面扫描使用；agent/skill 面数据源是注册表服务。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# agent 注册表服务调用方（server.py on_load 注入）：async (tool_name, args) -> 信封
# dict，经 tool-executor.invoke 以显式 plugin_id 直达 agent_manager（系统插件服务
# 不在 LLM 工具注册表，反查必失败，须带 plugin_id——task_submit 的 agent.get
# 消费同款形态）。None = 服务调用方未注入。
AgentServiceCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
_agent_service: AgentServiceCaller | None = None

# skill 注册表服务调用方（server.py on_load 注入）：async (method, args) -> 裸
# dict，经 provides namespace "skill" 的 capability handle 直连（approval 消费
# human-interaction 生产先例；T2 信封形态是 agent_manager 无 provides 命名空间
# 下的唯一通路，skill_manager 自持 manifest 声明 provides 走一等依赖通路）。
# None = 服务调用方未注入。
SkillServiceCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
_skill_service: SkillServiceCaller | None = None

# skill 语言过滤已知键集（skill_manager LANGUAGE_EXTS 同源；未知值不过滤 =
# 原文件扫描时代语义）。
KNOWN_SKILL_LANGUAGES = frozenset({"python", "nodejs", "bash", "powershell"})

# 反调并发上限（skill.get 正文兜底 / agent.get detailed 补全的成批取数）：
# 每次反调都是一次经内核桥的跨 sidecar 往返且与同宿主组共享事件循环，
# 串行逐个取数时 N 次往返线性叠加（实测 33 技能兜底 6-20s）；有界并发把
# 往返摊平。上限取 8：远小于内核桥与 sidecar 的承载面，避免瞬时打满同宿主组。
_BATCH_CONCURRENCY = 8


async def _gather_bounded(coros: list[Awaitable[Any]], concurrency: int = _BATCH_CONCURRENCY) -> list[Any]:
    """有界并发执行协程序列，按提交序回填结果（失败以异常对象占位）。

    调用方负责扫结果挑出异常并按首错语义上抛（与串行 await 的"首错即败"
    信封语义对齐，只是并发窗内的其余取数会多跑——服务级故障本就整体失败，
    无语义差）。"""
    if not coros:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def _run(index: int, coro: Awaitable[Any]) -> tuple[int, Any]:
        async with sem:
            return index, await coro

    results: list[Any] = [None] * len(coros)
    for fut in asyncio.as_completed([_run(i, c) for i, c in enumerate(coros)]):
        index, value = await fut
        results[index] = value
    return results


def _first_exception(results: list[Any]) -> Exception | None:
    """取结果集中提交序最靠前的异常（无异常返回 None）。"""
    for value in results:
        if isinstance(value, BaseException) and isinstance(value, Exception):
            return value
    return None


def set_agent_service_caller(caller: AgentServiceCaller | None) -> None:
    """注入/清除 agent 注册表服务调用方（None = 未注入，agent 面显式失败）。"""
    global _agent_service
    _agent_service = caller


def set_skill_service_caller(caller: SkillServiceCaller | None) -> None:
    """注入/清除 skill 注册表服务调用方（None = 未注入，skill 面显式失败）。"""
    global _skill_service
    _skill_service = caller


# 工具面对账通道调用方（server.py on_load 注入）：async () -> GET /api/v1/tools
# 响应 dict（{"items": [{name, ...}], "total": n}）。HTTP 面不经 capability、
# 不受服务声明闸管（/api/v1/tools 是冻结契约端点，资源注册收敛方案 T4 明注）。
# None = 未注入。
ToolSurfaceCaller = Callable[[], Awaitable[Any]]
_tool_surface: ToolSurfaceCaller | None = None


def set_tool_surface_caller(caller: ToolSurfaceCaller | None) -> None:
    """注入/清除工具面对账通道调用方（None = 未注入，工具面显式失败）。"""
    global _tool_surface
    _tool_surface = caller


ToolSchemasCaller = Callable[[list[str]], Awaitable[Any]]
_tool_schemas: ToolSchemasCaller | None = None

#: external MCP 受管插件的识别标记（``loader/managed_mcp.rs`` 合成 manifest 的
#: ``entry`` 值）。零声明目录的工具名与 schema 只能从活注册表取。
MCP_EXTERNAL_ENTRY = "mcp:external"

#: 定义项 ``source`` 标记：external MCP 目标（``ToolSource.MCP``）。call_tool
#: 按此跳过服务端上下文补齐——标准 MCP server 对未声明参数严格校验。
MCP_SOURCE = "mcp"


def set_tool_schemas_caller(caller: ToolSchemasCaller | None) -> None:
    """注入/清除 ``tool-surface.schemas`` 通道调用方（None = 未注入，只影响成组定义）。

    external MCP 工具的真值是活注册表（观测即声明导入的 ``ToolDescriptor`` 已带
    ``input_schema``），而 ``GET /api/v1/tools`` 不投喂 schema——故定义面走内核
    既有的 ``tool-surface.schemas`` 能力（``STANDARD_CAPABILITIES`` 已列，零新增契约）。
    """
    global _tool_schemas
    _tool_schemas = caller


class ToolSchemasUnavailable(RuntimeError):
    """``tool-surface.schemas`` 通道不可用/返回无效（该批定义 fail-closed）。"""


def _is_external_mcp_manifest(manifest: dict[str, Any]) -> bool:
    """受管 external MCP 目录（零声明）判定——工具不在 manifest 里可扫。

    判据**只认 ``entry == "mcp:external"``**（``loader/managed_mcp.rs`` 合成受管
    目录的唯一标记）。不按 ``language == "external"`` 兜底：后者会把「声明了
    ``capabilities.tools`` 的 external 插件」也路由进本分支，而该分支**完全不读
    manifest 声明**（只从活注册表按 ``plugin_id`` 反查）→ 其声明工具永不出现。
    """
    return str(manifest.get("entry") or "") == MCP_EXTERNAL_ENTRY


class ToolSurfaceUnavailable(RuntimeError):
    """内核活注册表对账通道不可用/返回无效（fail-closed，不回落纯扫描）。"""


def _require_tool_surface() -> ToolSurfaceCaller:
    """取当前对账通道调用方；未注入即显式失败（fail-closed，不回落纯扫描）。"""
    if _tool_surface is None:
        raise ToolSurfaceUnavailable("对账通道未注入（on_load 未接线）")
    return _tool_surface


class AgentRegistryUnavailable(RuntimeError):
    """agent 注册表服务不可用/返回无效（fail-closed，不回落本地扫描）。"""


class SkillRegistryUnavailable(RuntimeError):
    """skill 注册表服务不可用/返回无效（fail-closed，不回落本地扫描）。"""


def _require_agent_service() -> AgentServiceCaller:
    """取当前服务调用方；未注入即显式失败（fail-closed，不回落本地扫描）。"""
    if _agent_service is None:
        raise AgentRegistryUnavailable("服务调用方未注入（on_load 未接线）")
    return _agent_service


def _require_skill_service() -> SkillServiceCaller:
    """取当前 skill 服务调用方；未注入即显式失败（fail-closed，不回落本地扫描）。"""
    if _skill_service is None:
        raise SkillRegistryUnavailable("服务调用方未注入（on_load 未接线）")
    return _skill_service


def _agent_service_data(envelope: Any) -> dict[str, Any]:
    """tool-executor 信封解包（``{"success": bool, "data": {...}}``）。

    success=false 或形态无效 → AgentRegistryUnavailable（显式失败，不猜测）。
    """
    if not isinstance(envelope, dict) or not envelope.get("success"):
        error = envelope.get("error") if isinstance(envelope, dict) else envelope
        raise AgentRegistryUnavailable(f"agent 服务调用失败: {error}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise AgentRegistryUnavailable(
            f"agent 服务返回无效（缺 data dict）: {str(envelope)[:200]}"
        )
    return data


def _skill_service_data(result: Any, method: str) -> dict[str, Any]:
    """skill 服务裸 dict 结果校验（provides 直连通路的 wire 契约）。

    非 dict / 携带 error 键 → SkillRegistryUnavailable（显式失败，不猜测）。
    """
    if not isinstance(result, dict):
        raise SkillRegistryUnavailable(f"skill.{method} 返回无效（非 dict）: {str(result)[:200]}")
    if result.get("error"):
        raise SkillRegistryUnavailable(f"skill.{method} 调用失败: {result.get('error')}")
    return result


class ResourceSearchTool:
    """资源搜索工具（0.2 精简实现）。

    提供：
    - 搜索 Agent（经 agent_manager 聚合注册表服务，行含 level/source 归属）
    - 搜索 工具（扫描 ``plugins/**/plugin.json`` ∩ 内核活注册表，只列活的）
    - 搜索 Skill（经 skill_manager 注册权威服务，元数据 + 正文按需）

    支持两种模式：
    - ``simple``：返回名称 + 描述（+ tool 的 input_schema 字符串）
    - ``detailed``：返回完整信息（skill 经 ``skill.get`` 补全 SKILL.md 内容；
      agent 经 ``agent.get`` 补全 recommended_metrics；tool 结构化返回
      ``tool_defs`` 可执行定义——含真实 ``input_schema`` / ``output_schema`` /
      ``render`` / ``plugin_id``，据以经薄转发壳 ``call_tool`` 调用）

    工具**不会**因为被搜到而进 LLM 原生工具面（``tool_ids`` 白名单不扩大）；
    动态面经 ``call_tool`` 单入口调用、按目标工具名走既有权限判定。日志/管道侧
    的动态定义集由 ``tool_schema`` 插件从本工具返回值抽取后落 ``dynamic_tools``。

    原向量检索/数据库/外部平台能力不在精简实现范围内；缺失的兜底链路被省略，
    遇到 query 时按上述数据源直接检索。
    """

    # ── 工具定义（与原版 schema 完全一致） ──────────────────────────────

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="resource_search",
            description=(
                "搜索系统内 Agent、工具、Skill 资源。已有明确资源映射时直接使用，无需搜索。"
                "空结果是正常的，不要重复搜索。"
                "（agent 面经 agent_manager 注册表服务聚合系统空间/插件包/用户空间；"
                "skill 面经 skill_manager 注册权威服务；工具面 = 本地清单对账内核活注册表，"
                "禁用插件的工具不出现。）"
            ),
            when_to_use=[
                "不确定有哪些可用资源时",
                "需要查找本地已声明的 Agent / 工具 / Skill 时",
            ],
            when_not_to_use=[
                "已知资源名称或映射 → 直接使用，无需搜索",
                "搜索文件内容 → 用 enhanced_search",
                "搜索互联网信息 → 用 web_search",
            ],
            caveats=[
                "搜索无结果是正常的，不要重复调用",
                "每次只调用一次",
                "搜到的工具不会进你的工具面；用 detailed 模式拿到 tool_defs 定义，"
                "再经 call_tool 传入目标工具名与参数调用",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["agent", "tool", "skill", "all"],
                        "description": "资源类型：agent/tool/skill/all",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（支持子串匹配；为空或通配符 *,all,所有 时返回全部）",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["simple", "detailed"],
                        "default": "simple",
                        "description": (
                            "simple=列出匹配资源；"
                            "detailed=返回完整描述（skill 返回 SKILL.md 内容，agent 返回 "
                            "recommended_metrics；tool 返回含真实 input_schema 的 tool_defs "
                            "可执行定义）。注意：搜到的工具不会自动加载进工具面，须用 "
                            "detailed 拿到 tool_defs 后经 call_tool 调用。"
                        ),
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "按分类过滤（仅 tool 类型生效，匹配 plugin.json 的 plugin_type）",
                            },
                            "level": {
                                "type": "string",
                                "enum": ["system", "user", "all"],
                                "description": "按级别过滤（仅 agent 类型生效；system≈L1，user≈L2/L3）",
                            },
                            "language": {
                                "type": "string",
                                "description": "按语言过滤 Skill 脚本：python/nodejs/bash/powershell",
                            },
                        },
                        "description": "可选过滤条件",
                    },
                    "limit": {
                        "type": "integer",
                        "default": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "maximum": ToolLimits.RESOURCE_SEARCH_DEFAULT,
                        "description": "返回数量，默认20",
                    },
                },
                "required": ["resource_type"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.SYSTEM,
            injected_params=["session_id", "parent_record_id", "_retriever"],
            tags=["search", "resource", "system"],
        )

    # ── 主执行 ───────────────────────────────────────────────────────

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行搜索：agent/skill 面走注册表服务，工具面扫描本地清单。"""
        resource_type = inputs.get("resource_type", "all")
        query = inputs.get("query", "") or ""
        mode = inputs.get("mode", "simple")
        filters = inputs.get("filters", {}) or {}
        raw_limit = inputs.get("limit", ToolLimits.RESOURCE_SEARCH_DEFAULT)
        try:
            limit = min(int(raw_limit), ToolLimits.RESOURCE_SEARCH_DEFAULT)
        except (TypeError, ValueError):
            limit = ToolLimits.RESOURCE_SEARCH_DEFAULT
        if limit <= 0:
            limit = ToolLimits.RESOURCE_SEARCH_DEFAULT

        detailed = mode == "detailed"
        # 动态面否决集（ADR 2026-11-18-agent-dynamic-tool-deny-list）：tool_args_inject
        # 服务端注入（模型下划线键已剥离，不可伪造）；缺席/空 = 无否决。
        # 命中工具从 tool 表格、tool_defs、tool_defs_errors 三面全部剔除——
        # 认知面干净，模型不浪费轮次尝试被否决工具；call_tool 门禁同集兜底。
        raw_deny = inputs.get("_tool_deny")
        deny_names = (
            {str(d) for d in raw_deny if isinstance(d, str) and d.strip()}
            if isinstance(raw_deny, list)
            else set()
        )
        category = filters.get("category")
        language = filters.get("language")
        level = filters.get("level", "all")

        logger.info(
            "[resource_search] execute: query=%r mode=%s detailed=%s resource_type=%s",
            query,
            mode,
            detailed,
            resource_type,
        )

        results: dict[str, Any] = {}

        # 三资源面并发执行（原串行 await：agent/tool/skill 三面的数据源互不
        # 依赖，串行时各面的 IPC 往返线性叠加）。失败信封语义保持原样：按
        # agent → tool → skill 优先序取首个失败面翻译为失败信封（原串行短路
        # 顺序，可见行为不变；并发窗内其余面会多跑，服务级故障本就整体失败）。
        face_defs: list[tuple[str, Any, str, tuple[str, str]]] = []
        if resource_type in ("agent", "all"):
            face_defs.append(
                (
                    "agent",
                    self._search_agents(query, category, level, limit, detailed=detailed),
                    "[resource_search] agent 注册表服务失败: %s",
                    ("agent 注册表服务不可用: {exc}", "agent_registry_unavailable"),
                )
            )
        if resource_type in ("tool", "all"):
            face_defs.append(
                (
                    "tool",
                    self._search_tools(query, category, level, limit, detailed=detailed),
                    "[resource_search] 工具面活注册表对账失败: %s",
                    ("工具面活注册表不可用: {exc}", "tool_surface_unavailable"),
                )
            )
        if resource_type in ("skill", "all"):
            face_defs.append(
                (
                    "skill",
                    self._search_skills(query, language, limit, detailed=detailed),
                    "[resource_search] skill 注册表服务失败: %s",
                    ("skill 注册表服务不可用: {exc}", "skill_registry_unavailable"),
                )
            )

        face_results = (
            await asyncio.gather(*[coro for _, coro, _, _ in face_defs], return_exceptions=True)
            if face_defs
            else []
        )

        for (face_name, _coro, warn_tpl, err_face), outcome in zip(face_defs, face_results, strict=True):
            err_tpl, err_code = err_face
            try:
                if isinstance(outcome, BaseException):
                    raise outcome
                if face_name == "agent":
                    names, descs, ids, details, levels, sources = outcome
                elif face_name == "tool":
                    names, descs, schemas, tool_defs, def_errors = outcome
                    if deny_names:
                        kept = [i for i, n in enumerate(names) if n not in deny_names]
                        names = [names[i] for i in kept]
                        descs = [descs[i] for i in kept]
                        schemas = [schemas[i] for i in kept]
                        tool_defs = [d for d in tool_defs if str(d.get("name") or "") not in deny_names]
                        def_errors = [
                            e for e in def_errors if str(e.get("name") or "") not in deny_names
                        ]
                else:
                    names, descs, details = outcome
            except Exception as exc:  # noqa: BLE001 —— 面失败在边界翻译为失败信封
                logger.warning(warn_tpl, exc)
                return create_failure_result(error=err_tpl.format(exc=exc), error_code=err_code)

            if face_name == "agent" and names:
                header = ["config_id", "agent_name", "agent_description", "level", "source"]
                rows: list[list[Any]] = []
                for i, name in enumerate(names):
                    row = [ids[i], name, descs[i], levels[i], sources[i]]
                    detail = details[i] if i < len(details) else {}
                    metrics = detail.get("recommended_metrics", []) if detailed else []
                    if metrics:
                        header, row = self._append_metrics(header, row, metrics)
                    rows.append(row)
                # header 去重保持原版语义（只在有 metrics 时追加列）
                results["agent_h"] = header
                results["agent_d"] = rows
                results["agent_c"] = len(names)
            elif face_name == "tool" and names:
                if detailed or not any(schemas):
                    results["tool_h"] = ["tool_name", "tool_description"]
                    results["tool_d"] = [[names[i], descs[i]] for i in range(len(names))]
                else:
                    results["tool_h"] = ["tool_name", "tool_description", "tool_schema"]
                    results["tool_d"] = [[names[i], descs[i], str(schemas[i] or {})] for i in range(len(names))]
                results["tool_c"] = len(names)
                # 结构化键（只增不改表格键）：可执行定义供动态面经 call_tool 调用；
                # 被 fail-closed 拒收的条目与原因显式回传（模型可自纠，运维可观测）。
                results["tool_defs"] = tool_defs
                results["tool_defs_count"] = len(tool_defs)
                results["tool_defs_errors"] = def_errors
            elif face_name == "skill" and names:
                if detailed and any(details):
                    results["skill_h"] = ["skill_name", "skill_description", "skill_content"]
                    results["skill_d"] = [
                        [names[i], descs[i], (details[i] or {}).get("skill_content", "")] for i in range(len(names))
                    ]
                else:
                    results["skill_h"] = ["skill_name", "skill_description"]
                    results["skill_d"] = [[names[i], descs[i]] for i in range(len(names))]
                results["skill_c"] = len(names)

        return create_success_result(data=self._slim_results(results), metadata={})

    # ── 搜索：Agent（agent_manager 聚合注册表服务消费）────────────────

    async def _search_agents(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        *,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[str], list[dict], list[str], list[str]]:
        """经 ``agent.list`` 聚合条目匹配 Agent，返回 (names, descs, ids, details, levels, sources)。

        匹配/过滤/limit 语义与本地扫描时代一致，在消费侧对服务条目执行；
        ``level``（L1/L2/L3）与 ``source``（system / mode:<plugin_id>）取自
        聚合条目，服务侧没有的字段如实置空。detailed 模式经 ``agent.get``
        按 item id 补全配置取 recommended_metrics（found=false = 条目在列表
        与取数间隙被删，按无 metrics 呈现，不算失败），补全取数成批有界并发。
        """
        names: list[str] = []
        descriptions: list[str] = []
        config_ids: list[str] = []
        levels: list[str] = []
        sources: list[str] = []
        matched: list[dict[str, Any]] = []

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 收集后按 config_id 排序，保证结果稳定（服务返回序不作契约）
        items = sorted(
            (it for it in await self._agent_list_items() if isinstance(it, dict)),
            key=lambda it: str(it.get("config_id") or ""),
        )
        for item in items:
            config_id = str(item.get("config_id") or "")
            name = str(item.get("name") or "")
            description = str(item.get("description") or "")
            agent_level = str(item.get("level") or "").upper()
            if not (config_id or name):
                continue

            # level 过滤（聚合条目 level 是 L1/L2/L3；system→L1，user→L2/L3 近似映射）
            if level and level != "all":
                level_map = {"system": {"L1"}, "user": {"L2", "L3"}}
                wanted = level_map.get(level.lower())
                if wanted and agent_level not in wanted:
                    continue

            # category 过滤（聚合条目一般无 category 字段，缺席即不命中）
            if category and str(item.get("category", "")) != category:
                continue

            # 聚合条目不携带 tags（消费侧不臆造），匹配落在 name/description
            raw_tags = item.get("tags")
            tags: list[Any] = list(raw_tags) if isinstance(raw_tags, list) else []
            if not is_wildcard and not self._match_query(query_lower, name, description, tags):
                if config_id and query_lower in config_id.lower():
                    pass
                else:
                    continue

            matched.append(item)
            if len(matched) >= limit:
                break

        # detailed 补全（recommended_metrics）成批有界并发：原实现每命中一条
        # 串行 await 一次 agent.get，limit=20 时 20 次跨 sidecar 往返线性叠加。
        details: list[dict] = []
        if detailed and matched:
            config_results = await _gather_bounded(
                [
                    self._agent_config(str(it.get("id") or str(it.get("config_id") or "")))
                    for it in matched
                ]
            )
            exc = _first_exception(config_results)
            if exc is not None:
                raise exc
            details = [{"recommended_metrics": config.get("recommended_metrics") or []} for config in config_results]
        else:
            details = [{} for _ in matched]

        for item in matched:
            config_id = str(item.get("config_id") or "")
            names.append(str(item.get("name") or "") or config_id)
            descriptions.append(str(item.get("description") or ""))
            config_ids.append(config_id)
            levels.append(str(item.get("level") or "").upper())
            sources.append(str(item.get("source") or ""))

        return names, descriptions, config_ids, details, levels, sources

    @staticmethod
    async def _agent_list_items() -> list[Any]:
        """调 ``agent.list`` 聚合服务，解包 items；信封无效/失败显式报错。"""
        envelope = await _require_agent_service()("agent.list", {})
        items = _agent_service_data(envelope).get("items")
        if items is None:
            return []
        if not isinstance(items, list):
            raise AgentRegistryUnavailable(f"agent.list items 非列表: {type(items).__name__}")
        return items

    @staticmethod
    async def _agent_config(item_id: str) -> dict[str, Any]:
        """调 ``agent.get`` 取完整配置；未找到返回空 dict（调用失败显式报错）。"""
        envelope = await _require_agent_service()("agent.get", {"agent_id": item_id})
        data = _agent_service_data(envelope)
        if not data.get("found"):
            return {}
        config = data.get("config")
        return config if isinstance(config, dict) else {}

    # ── 搜索：Tool（manifest 扫描 ∩ 内核活注册表）────────────────────

    @staticmethod
    def _collect_tool_manifests(plugins_dir: Path) -> list[tuple[Path, dict]]:
        """收集 tool 类 plugin.json 清单（同步，由 to_thread 承载）。

        os.walk 原位剪枝 node_modules（必须改写 dirnames，生成器才不会深入）。
        Path.rglob 在 Python 3.12 会跟入符号链接/junction 目录——plugins 下
        dsh_adapter runtime 的 node_modules 是 pnpm 循环链接结构，rglob/未剪枝
        的 walk 都会无限遍历。剪枝后 0.06s / 105 个 manifest。
        读取失败留痕跳过、非 dict 跳过、非 tool 类跳过（语义与原内联体一致）。
        """
        import os as _os  # noqa: PLC0415

        if not plugins_dir.exists():
            return []
        plugin_jsons: list[Path] = []
        for dirpath, dirnames, filenames in _os.walk(plugins_dir):
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
            if "plugin.json" in filenames:
                plugin_jsons.append(Path(dirpath) / "plugin.json")
        plugin_jsons.sort()
        manifests: list[tuple[Path, dict]] = []
        for pj_path in plugin_jsons:
            try:
                with open(pj_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.debug("[resource_search] 读取 plugin.json 失败 %s: %s", pj_path, exc)
                continue
            if not isinstance(data, dict):
                continue
            if data.get("plugin_type") != "tool":
                continue
            manifests.append((pj_path, data))
        return manifests

    async def _search_tools(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        *,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[dict], list[dict], list[dict]]:
        """扫描 ``plugins/**/plugin.json`` 匹配工具声明，与内核活注册表对账。

        manifest 扫描保留为行源（描述/tags/空描述兜底文案，匹配语义 T1
        契约）；对账键 = 工具名集合交——工具名 ∉ 活注册表（禁用/卸载/
        未注册）→ 不出现（T4，禁用插件工具不再漏进结果）。描述不取活
        条目：ToolDescriptor 注册自同一 plugin.json 声明，两端同源零增益。

        返回 ``(names, descriptions, schemas_list, tool_defs, errors)``：
        ``schemas_list`` 是表格式键 ``tool_schema`` 列的取值（manifest 声明的
        ``input_schema``，与定义项同源）；``tool_defs`` 是**可执行定义**数组
        （结构化键，动态面经薄转发壳调用时的唯一依据）；``errors`` 记录被
        fail-closed 拒收的条目与原因（``name`` / ``reason``）。拒收条目仍保留
        在表格行里（工具确实活着，模型需要知道它存在），只是拿不到可执行定义。
        """
        names: list[str] = []
        descriptions: list[str] = []
        schemas_list: list[dict] = []
        tool_defs: list[dict] = []
        errors: list[dict] = []

        plugins_dir = _PROJECT_ROOT / "plugins"
        # 注册集扫描体提取为同步函数整体下放线程池（os.walk + 逐 manifest
        # open/json.load——async handler 内同步文件 I/O 盲区，2026-11-16
        # 方案文档 P1 配方）；行为零变化（收集顺序/剪枝/过滤语义原样）。
        manifests = await asyncio.to_thread(self._collect_tool_manifests, plugins_dir)

        live_plugin_ids = await self._live_tool_plugin_ids()

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        for pj_path, data in manifests:

            # category 过滤：plugin.json 无 category 字段（plugin_type="tool"
            # 是最接近的近似），故 category 过滤对 tool 清单无实际效果。
            capabilities = data.get("capabilities", {}) or {}
            tools_decl = capabilities.get("tools", []) or []

            # external MCP 受管目录是**零声明**（观测即声明：握手 tools/list 全量
            # 导入，见 plugin_watcher.rs `is_dynamic_mcp`）——manifest 里没有
            # capabilities.tools 可扫，工具名与 schema 的真值只在活注册表。
            if _is_external_mcp_manifest(data):
                for name, desc, schema, defn, reason in await self._external_mcp_items(
                    data, live_plugin_ids, query_lower, is_wildcard
                ):
                    if name in names:
                        continue
                    names.append(name)
                    descriptions.append(desc)
                    schemas_list.append(schema)
                    if defn is None:
                        errors.append({"name": name, "reason": reason})
                    else:
                        tool_defs.append(defn)
                    if len(names) >= limit:
                        return names, descriptions, schemas_list, tool_defs, errors
                continue

            if not tools_decl:
                continue

            matched_any = False
            for tool_decl in tools_decl:
                tool_name = str(tool_decl.get("name", "") or "")
                if not tool_name or tool_name not in live_plugin_ids:
                    continue
                tool_desc = str(tool_decl.get("description", "") or "")
                tags = tool_decl.get("tags", []) or []

                if is_wildcard or self._match_query(query_lower, tool_name, tool_desc, list(tags)):
                    if tool_name in names:
                        continue
                    # 路由目标 = 内核 manifests 键面（``manifest.id``）：活注册表条目的
                    # ``plugin_id`` 就是该键（``ToolDescriptor.plugin_id`` 注册自
                    # ``manifest.id``），优先取它；条目缺该字段才回落到同源的本条
                    # manifest ``id``。**不得用插件目录名**——目录名与 manifest id 无
                    # 契约关系（本仓 103 件插件中 59 件不等，如 ``builtin_tools`` →
                    # ``agentos-builtin-tools``），按目录名查插件必然 miss。
                    plugin_id = live_plugin_ids.get(tool_name) or str(data.get("id") or "").strip()
                    names.append(tool_name)
                    descriptions.append(tool_desc or f"插件工具 {tool_name}（来源 {plugin_id or pj_path.parent.name}）")
                    schema = tool_decl.get("input_schema")
                    schemas_list.append(schema if isinstance(schema, dict) else {})
                    # 定义真值 = 本条 manifest 声明（型号只信声明面，模型回传不作真值）
                    defn, reason = self._tool_def_from_decl(tool_decl, plugin_id=plugin_id)
                    if defn is None:
                        errors.append({"name": tool_name, "reason": reason})
                    else:
                        tool_defs.append(defn)
                    matched_any = True
                    if len(names) >= limit:
                        return names, descriptions, schemas_list, tool_defs, errors

            if matched_any and len(names) >= limit:
                break

        return names, descriptions, schemas_list, tool_defs, errors

    async def _external_mcp_items(
        self,
        manifest: dict[str, Any],
        live_plugin_ids: dict[str, str],
        query_lower: str,
        is_wildcard: bool,
    ) -> list[tuple[str, str, dict[str, Any], dict[str, Any] | None, str]]:
        """external MCP 受管目录的工具项 ``[(name, desc, schema, defn, reason)]``。

        定义真值 = **活注册表**：零声明 external MCP 由握手 ``tools/list`` 观测导入
        （``plugin_watcher.rs`` 的 ``is_dynamic_mcp`` 分支），``ToolDescriptor``
        自带 ``input_schema`` / ``output_schema`` / ``render``；manifest 侧只有
        ``mcp`` 端点，没有工具声明可扫。取 schema 走内核既有
        ``tool-surface.schemas`` 能力（``STANDARD_CAPABILITIES`` 已列）——**不改内核**：
        ``GET /api/v1/tools`` 只投喂 name/plugin_id，schema 只能从该能力拿。

        通道不可用/返回无效 → 本目录工具**仍列出**（工具确实活着，模型需要知道它
        存在），但拿不到可执行定义 → 逐条记 ``tool_schemas_unavailable`` 错误项
        （fail-closed：不给盲调入口，也不假装有定义）。
        """
        plugin_id = str(manifest.get("id") or "").strip()
        if not plugin_id:
            return []
        own = [n for n, pid in live_plugin_ids.items() if pid == plugin_id]
        if not own:
            return []

        try:
            catalog = await self._live_tool_definitions(own)
        except ToolSchemasUnavailable as exc:
            logger.warning("[resource_search] external MCP 定义取数失败 %s: %s", plugin_id, exc)
            return [
                (name, f"MCP 工具 {name}（来源 {plugin_id}）", {}, None, "tool_schemas_unavailable")
                for name in own
                if is_wildcard or query_lower in name.lower()
            ]

        items: list[tuple[str, str, dict[str, Any], dict[str, Any] | None, str]] = []
        for name in own:
            entry = catalog.get(name) or {}
            desc = str(entry.get("description") or "").strip()
            if not (is_wildcard or self._match_query(query_lower, name, desc, [])):
                continue
            schema = entry.get("input_schema")
            decl = {
                "name": name,
                "description": desc or f"MCP 工具 {name}（来源 {plugin_id}）",
                "input_schema": schema,
                "output_schema": entry.get("output_schema"),
                "render": entry.get("render"),
            }
            # source="mcp"：定义面标记 external MCP 目标——call_tool 据此不补
            # 服务端上下文（标准 MCP server 严格校验未声明参数；剥离责任在内核
            # sanitize_external_mcp_arguments）。
            defn, reason = self._tool_def_from_decl(
                decl, plugin_id=plugin_id, source=MCP_SOURCE
            )
            description = str(decl["description"] or "")
            items.append(
                (name, description, schema if isinstance(schema, dict) else {}, defn, reason)
            )
        return items

    @staticmethod
    async def _live_tool_definitions(names: list[str]) -> dict[str, dict[str, Any]]:
        """经 ``tool-surface.schemas`` 取工具的可执行定义面（name → 定义字段）。

        响应 ``{"schemas": [{type, function: {name, description, parameters}}],
        "contracts": {name: {schema, render, isolation_capable}}}``——schemas 提供
        ``input_schema``（OpenAI parameters 面），contracts 提供 ``output_schema``
        与 ``render``。两处都缺的工具只返回空条（调用方按 fail-closed 拒收）。
        """
        if _tool_schemas is None:
            raise ToolSchemasUnavailable("定义通道未注入（on_load 未接线）")
        response = await _tool_schemas(list(names))
        if not isinstance(response, dict):
            raise ToolSchemasUnavailable(f"定义通道返回无效（非 dict）: {str(response)[:200]}")
        raw_schemas = response.get("schemas")
        if not isinstance(raw_schemas, list):
            raise ToolSchemasUnavailable(f"定义通道 schemas 非列表: {type(raw_schemas).__name__}")
        contracts = response.get("contracts")
        contracts = contracts if isinstance(contracts, dict) else {}

        catalog: dict[str, dict[str, Any]] = {}
        for raw in raw_schemas:
            fn = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            contract = contracts.get(name)
            contract = contract if isinstance(contract, dict) else {}
            catalog[name] = {
                "description": str(fn.get("description") or "").strip(),
                "input_schema": fn.get("parameters"),
                "output_schema": contract.get("schema"),
                "render": contract.get("render"),
            }
        return catalog


    @staticmethod
    def _tool_def_from_decl(
        tool_decl: dict[str, Any], *, plugin_id: str, source: str = "code"
    ) -> tuple[dict[str, Any] | None, str]:

        """把 manifest 的工具声明转成可执行定义项；不可执行返回 ``(None, 原因码)``。

        fail-closed：``input_schema`` 缺失或非 object 一律拒收，**不补空对象**
        ——补 ``{}`` 等于零参数盲调，与「external MCP 缺 input_schema 拒注册」
        同口径（`plugin_lifecycle.rs` 同一判定语义）。``output_schema``/``render``
        缺失只降级（前端有 generic 回退路径），不阻断可用性。

        ``source`` 是目标来源标记（``code`` = 插件声明工具；``mcp`` = external
        MCP 受管目录），call_tool 据此决定是否补服务端上下文。
        """
        name = str(tool_decl.get("name") or "").strip()
        if not name:
            return None, "name_missing"
        description = str(tool_decl.get("description") or "").strip()
        if not description:
            return None, "description_missing"
        schema = tool_decl.get("input_schema")
        if schema is None:
            return None, "input_schema_missing"
        if not isinstance(schema, dict) or str(schema.get("type") or "") != "object":
            return None, "input_schema_not_object"

        defn: dict[str, Any] = {
            "name": name,
            "description": description,
            "input_schema": schema,
            "plugin_id": plugin_id,
            "source": source,
        }
        if isinstance(tool_decl.get("output_schema"), dict):
            defn["output_schema"] = tool_decl["output_schema"]
        if isinstance(tool_decl.get("render"), dict):
            defn["render"] = tool_decl["render"]
        return defn, ""

    @staticmethod
    async def _live_tool_plugin_ids() -> dict[str, str]:
        """经对账通道取内核活注册表「工具名 → 插件 id」（``GET /api/v1/tools``）。

        响应信封 ``{"items": [{name, plugin_id, ...}], "total": n}``；items 缺席 =
        空活集（registry 未装配的测试装配态真实反映，routes.rs tools_handler
        同语义），非 dict / items 非列表 = 显式失败（fail-closed，不回落纯扫描）。
        条目缺 name 的坏件跳过（活注册表按 name 键控，正常不出现）。

        **值 = 路由真值**：条目的 ``plugin_id`` 来自 ``ToolDescriptor.plugin_id``，
        注册时取自 ``manifest.id``（``plugin-loader/src/loader.rs``），正是内核
        ``manifests`` 映射的键——按它查插件必命中。缺该字段的条目值记为 ``""``，
        由调用方回落到本条 manifest 的 ``id``（同源真值），不猜目录名。
        """
        response = await _require_tool_surface()()
        if not isinstance(response, dict):
            raise ToolSurfaceUnavailable(f"活注册表返回无效（非 dict）: {str(response)[:200]}")
        items = response.get("items")
        if items is None:
            return {}
        if not isinstance(items, list):
            raise ToolSurfaceUnavailable(f"活注册表 items 非列表: {type(items).__name__}")
        return {
            str(it["name"]): str(it.get("plugin_id") or "").strip()
            for it in items
            if isinstance(it, dict) and it.get("name")
        }

    # ── 搜索：Skill（skill_manager 注册权威服务消费）────────────────

    async def _search_skills(
        self,
        query: str,
        language: str | None,
        limit: int,
        *,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """经 ``skill.list`` 元数据匹配 Skill，返回 (names, descs, details)。

        匹配/过滤/limit 语义与本地扫描时代一致，在消费侧对服务条目执行；
        正文兜底匹配经 ``skill.get`` 按需补取（仅元数据与 id 都未命中的
        候选，通配查询零取数），匹配语义零降级；兜底与 detailed 正文取数
        均成批有界并发（原实现逐条串行 await，关键词查询对全部未命中候选
        各付一次跨 sidecar 往返，是搜索耗时的主项）。detailed 模式经
        ``skill.get`` 按条目 id 补全正文（found=false = 无内容呈现，不算
        失败，与 agent 面 metrics 补全模式对齐）。
        """
        names: list[str] = []
        descriptions: list[str] = []
        details: list[dict] = []

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 收集后按 id 排序，保证结果稳定（服务返回序不作契约）
        items = sorted(
            (it for it in await self._skill_list_items() if isinstance(it, dict)),
            key=lambda it: str(it.get("id") or ""),
        )

        # 第一遍纯内存判定（无反调）：元数据直命中 / 需正文兜底的候选。
        # 判定谓词与串行时代逐条一致；候选集的正文取数延后成批并发。
        direct_hit_ids: set[str] = set()
        candidate_ids: set[str] = set()
        ordered_candidates: list[str] = []
        for item in items:
            skill_id = str(item.get("id") or "")
            name = str(item.get("name") or "")
            description = str(item.get("description") or "")
            if not (skill_id or name):
                continue

            # language 过滤：服务条目 languages 是扫描期预计算的覆盖面；
            # 未知过滤值不过滤（原文件扫描时代语义）
            if language:
                lang = language.lower()
                item_langs = {str(x).lower() for x in item.get("languages") or []}
                if lang in KNOWN_SKILL_LANGUAGES and lang not in item_langs:
                    continue

            if (
                is_wildcard
                or self._match_query(query_lower, name, description, [])
                or skill_id
                and query_lower in skill_id.lower()
            ):
                direct_hit_ids.add(skill_id)
            elif skill_id not in candidate_ids:
                candidate_ids.add(skill_id)
                ordered_candidates.append(skill_id)

        # 正文兜底 + detailed 正文，一次成批取数（有界并发）。同一 skill_id
        # 只取一次（兜底命中即 detailed 正文复用，与串行时代 body 复用一致）。
        need_ids: list[str] = []
        if not is_wildcard:
            need_ids = list(ordered_candidates)
        if detailed:
            need_ids.extend(sorted(direct_hit_ids - candidate_ids))
        body_map: dict[str, str] = {}
        if need_ids:
            body_results = await _gather_bounded([self._skill_body(sid) for sid in need_ids])
            exc = _first_exception(body_results)
            if exc is not None:
                raise exc
            body_map = dict(zip(need_ids, body_results, strict=True))

        # 第二遍按 id 序组装：候选以取回的正文判定兜底命中；limit 截断点与
        # 串行时代一致（首个第 limit 次命中处停止）。
        for item in items:
            skill_id = str(item.get("id") or "")
            name = str(item.get("name") or "")
            description = str(item.get("description") or "")
            if not (skill_id or name):
                continue
            if language:
                lang = language.lower()
                item_langs = {str(x).lower() for x in item.get("languages") or []}
                if lang in KNOWN_SKILL_LANGUAGES and lang not in item_langs:
                    continue

            body: str | None = None
            if skill_id in candidate_ids:
                body = body_map.get(skill_id, "")
                hit = query_lower in body.lower()
            elif skill_id in direct_hit_ids:
                hit = True
                body = body_map.get(skill_id)
            else:
                continue

            if not hit:
                continue

            names.append(name or skill_id)
            descriptions.append(description)
            if detailed:
                details.append({"skill_content": body or ""})
            else:
                details.append({})

            if len(names) >= limit:
                break

        return names, descriptions, details

    @staticmethod
    async def _skill_list_items() -> list[Any]:
        """调 ``skill.list`` 注册表服务，解包 items；结果无效显式报错。"""
        result = _skill_service_data(await _require_skill_service()("list", {}), "list")
        items = result.get("items")
        if items is None:
            return []
        if not isinstance(items, list):
            raise SkillRegistryUnavailable(f"skill.list items 非列表: {type(items).__name__}")
        return items

    @staticmethod
    async def _skill_body(skill_id: str) -> str:
        """调 ``skill.get`` 取正文；未找到/缺 content 返回空串（调用失败显式报错）。"""
        result = _skill_service_data(await _require_skill_service()("get", {"skill_id": skill_id}), "get")
        if not result.get("found"):
            return ""
        skill = result.get("skill")
        if not isinstance(skill, dict):
            return ""
        content = skill.get("content")
        return content if isinstance(content, str) else ""

    @staticmethod
    def _match_query(
        query_lower: str,
        name: str,
        description: str,
        tags: list[str],
    ) -> bool:
        """关键词匹配（与原版语义一致：子串 + 通配符 + 分词）。"""
        if not query_lower:
            return True
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        if query_lower.strip() in wildcard_patterns:
            return True

        name_lower = (name or "").lower()
        desc_lower = (description or "").lower()
        tags_lower = [str(t).lower() for t in (tags or [])]

        if query_lower in name_lower or query_lower in desc_lower:
            return True
        for tag in tags_lower:
            if query_lower in tag:
                return True

        keywords = [kw.strip() for kw in query_lower.replace(",", " ").split() if kw.strip()]
        for keyword in keywords:
            if keyword in wildcard_patterns:
                return True
            if keyword in name_lower or keyword in desc_lower:
                return True
            for tag in tags_lower:
                if keyword in tag:
                    return True
        return False

    #: 结构化定义键保留前缀：``tool_defs`` / ``tool_defs_count`` / ``tool_defs_errors``
    #: 与表格式 ``_h/_d/_c`` 并列出口。不加这一段新键会被本方法剥掉、出不了工具
    #: （结构化定义形同虚设），故白名单必须与契约键同批维护。
    _SLIM_STRUCTURED_PREFIXES: tuple[str, ...] = ("tool_defs",)

    @classmethod
    def _slim_results(cls, results: dict[str, Any]) -> dict[str, Any]:
        """精简搜索结果，保留 _h/_d/_c/message 与结构化定义键（``tool_defs*``）。"""
        slim: dict[str, Any] = {}
        for key, value in results.items():
            if (
                key.endswith("_d")
                or key.endswith("_h")
                or key.endswith("_c")
                or key == "message"
                or any(key.startswith(prefix) for prefix in cls._SLIM_STRUCTURED_PREFIXES)
            ):
                slim[key] = value
        return slim

    @staticmethod
    def _append_metrics(header: list[str], row: list[Any], metrics: list[Any]) -> tuple[list[str], list[Any]]:
        """把 recommended_metrics 拼成单列字符串追加到行尾（与原版一致）。"""
        if not metrics:
            return header, row

        def _metric_str(m: Any) -> str:
            mid = ""
            params: dict[str, Any] = {}
            if isinstance(m, dict):
                mid = str(m.get("metric_id", "") or "")
                params = m.get("default_params", {}) or {}
            else:
                mid = str(getattr(m, "metric_id", "") or "")
                params = getattr(m, "default_params", {}) or {}
            if isinstance(params, dict) and params:
                return f"{mid}({', '.join(f'{k}={v}' for k, v in params.items())})"
            return mid

        metrics_str = "; ".join(_metric_str(m) for m in metrics)
        row.append(f"推荐评估: {metrics_str}")
        if "recommended_metrics" not in header:
            header.append("recommended_metrics")
        return header, row
