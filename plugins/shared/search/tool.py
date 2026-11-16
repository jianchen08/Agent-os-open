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
      agent 经 ``agent.get`` 补全 recommended_metrics；tool 仍只返回名称 +
      描述，因为 dynamic 注入在 sidecar 里不可用）

    原向量检索/数据库/外部平台/动态工具注入能力不在精简实现范围内；
    缺失的兜底链路被省略，遇到 query 时按上述数据源直接检索。
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
                "sidecar 精简实现不动态注入工具，detailed 模式仅返回完整描述",
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
                            "detailed=返回完整描述（skill 返回 SKILL.md 内容，agent 返回 recommended_metrics）。"
                            "注意：精简实现不支持动态工具注入，detailed 模式下 tool 类型不会真正加载到会话。"
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

        if resource_type in ("agent", "all"):
            try:
                names, descs, ids, details, levels, sources = await self._search_agents(
                    query, category, level, limit, detailed=detailed
                )
            except Exception as exc:  # noqa: BLE001 —— 服务失败在边界翻译为失败信封
                logger.warning("[resource_search] agent 注册表服务失败: %s", exc)
                return create_failure_result(
                    error=f"agent 注册表服务不可用: {exc}",
                    error_code="agent_registry_unavailable",
                )
            if names:
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

        if resource_type in ("tool", "all"):
            try:
                names, descs, schemas = await self._search_tools(
                    query, category, level, limit, detailed=detailed
                )
            except Exception as exc:  # noqa: BLE001 —— 对账通道失败在边界翻译为失败信封
                logger.warning("[resource_search] 工具面活注册表对账失败: %s", exc)
                return create_failure_result(
                    error=f"工具面活注册表不可用: {exc}",
                    error_code="tool_surface_unavailable",
                )
            if names:
                if detailed or not any(schemas):
                    results["tool_h"] = ["tool_name", "tool_description"]
                    results["tool_d"] = [[names[i], descs[i]] for i in range(len(names))]
                else:
                    results["tool_h"] = ["tool_name", "tool_description", "tool_schema"]
                    results["tool_d"] = [[names[i], descs[i], str(schemas[i] or {})] for i in range(len(names))]
                results["tool_c"] = len(names)

        if resource_type in ("skill", "all"):
            try:
                names, descs, details = await self._search_skills(query, language, limit, detailed=detailed)
            except Exception as exc:  # noqa: BLE001 —— 服务失败在边界翻译为失败信封
                logger.warning("[resource_search] skill 注册表服务失败: %s", exc)
                return create_failure_result(
                    error=f"skill 注册表服务不可用: {exc}",
                    error_code="skill_registry_unavailable",
                )
            if names:
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
        与取数间隙被删，按无 metrics 呈现，不算失败）。
        """
        names: list[str] = []
        descriptions: list[str] = []
        config_ids: list[str] = []
        details: list[dict] = []
        levels: list[str] = []
        sources: list[str] = []

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
            source = str(item.get("source") or "")
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

            names.append(name or config_id)
            descriptions.append(description)
            config_ids.append(config_id)
            levels.append(agent_level)
            sources.append(source)

            if detailed:
                config = await self._agent_config(str(item.get("id") or config_id))
                details.append({"recommended_metrics": config.get("recommended_metrics") or []})
            else:
                details.append({})

            if len(names) >= limit:
                break

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

    async def _search_tools(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        *,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """扫描 ``plugins/**/plugin.json`` 匹配工具声明，与内核活注册表对账。

        manifest 扫描保留为行源（描述/tags/空描述兜底文案，匹配语义 T1
        契约）；对账键 = 工具名集合交——工具名 ∉ 活注册表（禁用/卸载/
        未注册）→ 不出现（T4，禁用插件工具不再漏进结果）。描述不取活
        条目：ToolDescriptor 注册自同一 plugin.json 声明，两端同源零增益。
        """
        names: list[str] = []
        descriptions: list[str] = []
        schemas_list: list[dict] = []

        plugins_dir = _PROJECT_ROOT / "plugins"
        if not plugins_dir.exists():
            return names, descriptions, schemas_list

        live_names = await self._live_tool_names()

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 收集 plugin.json：os.walk 原位剪枝 node_modules（必须改写 dirnames，
        # 生成器才不会深入）。Path.rglob 在 Python 3.12 会跟入符号链接/junction
        # 目录——plugins 下 dsh_adapter runtime 的 node_modules 是 pnpm 循环
        # 链接结构，rglob/未剪枝的 walk 都会无限遍历。剪枝后 0.06s / 105 个 manifest。
        import os as _os  # noqa: PLC0415

        plugin_jsons: list[Path] = []
        for dirpath, dirnames, filenames in _os.walk(plugins_dir):
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
            if "plugin.json" in filenames:
                plugin_jsons.append(Path(dirpath) / "plugin.json")
        plugin_jsons.sort()
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

            # category 过滤：plugin.json 无 category 字段（plugin_type="tool"
            # 是最接近的近似），故 category 过滤对 tool 清单无实际效果。
            capabilities = data.get("capabilities", {}) or {}
            tools_decl = capabilities.get("tools", []) or []
            if not tools_decl:
                continue

            matched_any = False
            for tool_decl in tools_decl:
                tool_name = str(tool_decl.get("name", "") or "")
                if not tool_name or tool_name not in live_names:
                    continue
                tool_desc = str(tool_decl.get("description", "") or "")
                tags = tool_decl.get("tags", []) or []

                if is_wildcard or self._match_query(query_lower, tool_name, tool_desc, list(tags)):
                    if tool_name in names:
                        continue
                    names.append(tool_name)
                    descriptions.append(tool_desc or f"插件工具 {tool_name}（来源 {pj_path.parent.name}）")
                    # plugin.json 一般不包含 input_schema；用空 dict 占位（simple 模式会输出 ""）
                    schemas_list.append({})
                    matched_any = True
                    if len(names) >= limit:
                        return names, descriptions, schemas_list

            if matched_any and len(names) >= limit:
                break

        return names, descriptions, schemas_list

    @staticmethod
    async def _live_tool_names() -> set[str]:
        """经对账通道取内核活注册表工具名集合（``GET /api/v1/tools``）。

        响应信封 ``{"items": [{name, ...}], "total": n}``；items 缺席 = 空
        活集（registry 未装配的测试装配态真实反映，routes.rs tools_handler
        同语义），非 dict / items 非列表 = 显式失败（fail-closed，不回落
        纯扫描）。条目缺 name 的坏件跳过（活注册表按 name 键控，正常不出现）。
        """
        response = await _require_tool_surface()()
        if not isinstance(response, dict):
            raise ToolSurfaceUnavailable(f"活注册表返回无效（非 dict）: {str(response)[:200]}")
        items = response.get("items")
        if items is None:
            return set()
        if not isinstance(items, list):
            raise ToolSurfaceUnavailable(f"活注册表 items 非列表: {type(items).__name__}")
        return {str(it["name"]) for it in items if isinstance(it, dict) and it.get("name")}

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
        候选，通配查询零取数），匹配语义零降级。detailed 模式经
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

            body: str | None = None
            if (
                is_wildcard
                or self._match_query(query_lower, name, description, [])
                or skill_id
                and query_lower in skill_id.lower()
            ):
                hit = True
            else:
                body = await self._skill_body(skill_id)
                hit = query_lower in body.lower()

            if not hit:
                continue

            names.append(name or skill_id)
            descriptions.append(description)
            if detailed:
                if body is None:
                    body = await self._skill_body(skill_id)
                details.append({"skill_content": body})
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

    @staticmethod
    def _slim_results(results: dict[str, Any]) -> dict[str, Any]:
        """精简搜索结果，仅保留 _h/_d/_c/message 字段（与原版一致）。"""
        slim: dict[str, Any] = {}
        for key, value in results.items():
            if key.endswith("_d") or key.endswith("_h") or key.endswith("_c") or key == "message":
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
