"""资源搜索工具（0.2 精简实现）。

原始 0.1 ``ResourceSearchTool`` 依赖大量基础设施（``tools.global_registry``、
``agents.global_registry``、``skills.registry``、``tools.auto_loader``、
``tools.loader``、``db.models.ToolLibrary``、``config.config_center``、
``infrastructure.db``），这些在 MCP sidecar 进程里都不可用。本文件是按
"实际可用资源" 原则重写的精简实现：

- 保留工具对外契约不变：工具名仍是 ``resource_search``，schema 与原版
  完全一致（``resource_type`` / ``query`` / ``mode`` / ``filters`` / ``limit``），
  ``get_tool_definition()`` 仍返回 ``Tool``。
- 实现一个可用的子集：直接扫描本仓库中真实存在的本地清单——
  ``plugins/**/plugin.json``（工具）、``config/agents/**/*.yaml``（Agent）、
  ``skills/*/SKILL.md``（Skill），按关键词匹配后返回。
- 不依赖向量数据库、外部平台或运行时服务注册表，因此在 sidecar 进程里
  能完整执行而非空回退。

返回结构与原版一致：``{<type>_h: [...], <type>_d: [[...]], <type>_c: N,
message?: str}``，由 ``server.py`` 的 ``result.output if result.success
else {"error": result.error}`` 直接返回给上层。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from constants import ToolLimits

# 跨插件共享类型已上提到 SDK 公共依赖层 agentos_plugin_sdk。
# ToolLimits 仅本工具使用，就近放在本工具目录 constants.py（server.py 已将本工具
# 目录注入 sys.path），直接 import。
from agentos_plugin_sdk import (
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_success_result,
)

logger = logging.getLogger(__name__)

# 项目根（search/tool.py → search/ → tools/ → shared/ → plugins/ → root）。
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class ResourceSearchTool:
    """资源搜索工具（0.2 精简实现）。

    提供：
    - 搜索 Agent（扫描 ``config/agents/**/*.yaml``）
    - 搜索 工具（扫描 ``plugins/**/plugin.json``）
    - 搜索 Skill（扫描 ``skills/*/SKILL.md``）

    支持两种模式：
    - ``simple``：返回名称 + 描述（+ tool 的 input_schema 字符串）
    - ``detailed``：返回完整信息（skill 返回 SKILL.md 内容；agent 返回
      recommended_metrics；tool 仍只返回名称 + 描述，因为 dynamic 注入
      在 sidecar 里不可用）

    原向量检索/数据库/外部平台/动态工具注入能力不在精简实现范围内；
    缺失的兜底链路被省略，遇到 query 时直接走本地扫描。
    """

    def __init__(self) -> None:
        """初始化搜索工具（无运行时依赖，直接扫描本地清单）。"""

    # ── 工具定义（与原版 schema 完全一致） ──────────────────────────────

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="resource_search",
            description=(
                "搜索系统内 Agent、工具、Skill 资源。已有明确资源映射时直接使用，无需搜索。"
                "空结果是正常的，不要重复搜索。"
                "（sidecar 精简实现：扫描本地 plugins/plugin.json、config/agents/*.yaml、"
                "skills/*/SKILL.md，不依赖向量库或运行时注册表。）"
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
                                "description": "按级别过滤（仅 agent 类型生效，匹配 yaml 的 level 字段）",
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
        """执行搜索：扫描本地清单并按关键词匹配。"""
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
            names, descs, ids, details = self._search_agents(
                query, category, level, limit, detailed=detailed
            )
            if names:
                header = ["config_id", "agent_name", "agent_description"]
                rows: list[list[Any]] = []
                for i, name in enumerate(names):
                    row = [ids[i], name, descs[i]]
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
            names, descs, schemas = self._search_tools(
                query, category, level, limit, detailed
            )
            if names:
                if detailed or not any(schemas):
                    results["tool_h"] = ["tool_name", "tool_description"]
                    results["tool_d"] = [[names[i], descs[i]] for i in range(len(names))]
                else:
                    results["tool_h"] = ["tool_name", "tool_description", "tool_schema"]
                    results["tool_d"] = [
                        [names[i], descs[i], str(schemas[i] or {})] for i in range(len(names))
                    ]
                results["tool_c"] = len(names)

        if resource_type in ("skill", "all"):
            names, descs, details = self._search_skills(
                query, language, limit, detailed
            )
            if names:
                if detailed and any(details):
                    results["skill_h"] = ["skill_name", "skill_description", "skill_content"]
                    results["skill_d"] = [
                        [names[i], descs[i], (details[i] or {}).get("skill_content", "")]
                        for i in range(len(names))
                    ]
                else:
                    results["skill_h"] = ["skill_name", "skill_description"]
                    results["skill_d"] = [[names[i], descs[i]] for i in range(len(names))]
                results["skill_c"] = len(names)

        return create_success_result(data=self._slim_results(results), metadata={})

    # ── 搜索：Agent（扫描 config/agents/**/*.yaml）───────────────────

    def _search_agents(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        *,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[str], list[dict]]:
        """扫描 ``config/agents/**/*.yaml`` 匹配 Agent 配置。"""
        names: list[str] = []
        descriptions: list[str] = []
        config_ids: list[str] = []
        details: list[dict] = []

        agents_dir = _PROJECT_ROOT / "config" / "agents"
        if not agents_dir.exists():
            return names, descriptions, config_ids, details

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 收集后按 config_id 排序，保证结果稳定
        yaml_files = sorted(agents_dir.rglob("*.yaml"))
        for yaml_path in yaml_files:
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except Exception as exc:
                logger.debug("[resource_search] 读取 agent yaml 失败 %s: %s", yaml_path, exc)
                continue
            if not isinstance(data, dict):
                continue

            config_id = str(data.get("config_id", "") or "")
            name = str(data.get("name", "") or "")
            description = str(data.get("description", "") or "")
            agent_level = str(data.get("level", "") or "").upper()
            tags = data.get("tags", []) or []
            if not (config_id or name):
                continue

            # level 过滤（L1/L2/L3 → system/user 的近似映射不存在，直接按字面值过滤）
            if level and level != "all":
                # 原版 level 字段语义是 system/user；agent yaml 里是 L1/L2/L3。
                # 此处宽松匹配：level=system 视为 L1，level=user 视为 L2/L3。
                level_map = {"system": {"L1"}, "user": {"L2", "L3"}}
                wanted = level_map.get(level.lower())
                if wanted and agent_level not in wanted:
                    continue

            # category 过滤（agent yaml 一般无 category 字段，跳过）
            if category and str(data.get("category", "")) != category:
                continue

            if not is_wildcard and not self._match_query(query_lower, name, description, list(tags)):
                if config_id and query_lower in config_id.lower():
                    pass
                else:
                    continue

            names.append(name or config_id)
            descriptions.append(description)
            config_ids.append(config_id)

            if detailed:
                details.append(
                    {
                        "recommended_metrics": data.get("recommended_metrics", []) or [],
                        "deliverables": data.get("deliverables", []) or [],
                    }
                )
            else:
                details.append({})

            if len(names) >= limit:
                break

        return names, descriptions, config_ids, details

    # ── 搜索：Tool（扫描 plugins/**/plugin.json）────────────────────

    def _search_tools(
        self,
        query: str,
        category: str | None,
        level: str,
        limit: int,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """扫描 ``plugins/**/plugin.json`` 匹配工具声明。"""
        names: list[str] = []
        descriptions: list[str] = []
        schemas_list: list[dict] = []

        plugins_dir = _PROJECT_ROOT / "plugins"
        if not plugins_dir.exists():
            return names, descriptions, schemas_list

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 收集 plugin.json：os.walk 原位剪枝 node_modules（必须改写 dirnames，
        # 生成器才不会深入）。Path.rglob 在 Python 3.12 会跟入符号链接/junction
        # 目录——plugins 下 dsh_adapter runtime 的 node_modules 是 pnpm 循环
        # 链接结构，rglob/未剪枝的 walk 都会无限遍历（2026-08-17 e2e 实测
        # resource_search 卡死根因）。剪枝后 0.06s / 105 个 manifest。
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
                tool_desc = str(tool_decl.get("description", "") or "")
                if not tool_name:
                    continue
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

    # ── 搜索：Skill（扫描 skills/*/SKILL.md）────────────────────────

    def _search_skills(
        self,
        query: str,
        language: str | None,
        limit: int,
        detailed: bool,
    ) -> tuple[list[str], list[str], list[dict]]:
        """扫描 ``skills/*/SKILL.md`` 匹配本地 Skill。"""
        names: list[str] = []
        descriptions: list[str] = []
        details: list[dict] = []

        skills_dir = _PROJECT_ROOT / "skills"
        if not skills_dir.exists():
            return names, descriptions, details

        query_lower = (query or "").lower().strip()
        wildcard_patterns = {"*", "all", "所有", "全部", "any"}
        is_wildcard = (not query_lower) or (query_lower in wildcard_patterns)

        # 每个 skill 是一个目录：skills/<skill-name>/SKILL.md
        skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                # 也尝试小写
                skill_md = skill_dir / "skill.md"
                if not skill_md.exists():
                    continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("[resource_search] 读取 SKILL.md 失败 %s: %s", skill_md, exc)
                continue

            # 解析 YAML front matter（如果存在），否则从正文推断 name/description
            name, description, body = self._parse_skill_md(content, skill_dir.name)

            # language 过滤：扫描目录里是否存在 *.py / *.js / *.sh / *.ps1
            if language:
                ext_map = {
                    "python": "*.py",
                    "nodejs": "*.js",
                    "bash": "*.sh",
                    "powershell": "*.ps1",
                }
                pattern = ext_map.get(language.lower())
                if pattern and not list(skill_dir.rglob(pattern)):
                    continue

            tags: list[str] = []
            if not (is_wildcard or self._match_query(query_lower, name, description, tags)):
                if not (query_lower in body.lower() or query_lower in skill_dir.name.lower()):
                    continue

            names.append(name)
            descriptions.append(description)
            details.append({"skill_content": body} if detailed else {})

            if len(names) >= limit:
                break

        return names, descriptions, details

    # ── 工具方法 ────────────────────────────────────────────────────

    @staticmethod
    def _parse_skill_md(content: str, fallback_name: str) -> tuple[str, str, str]:
        """解析 SKILL.md 内容，返回 (name, description, body)。

        支持 YAML front matter（``---`` 包裹），否则用首条 ``# 标题`` 作为
        name、首段作为 description。
        """
        name = fallback_name
        description = ""
        body = content

        # YAML front matter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    front = yaml.safe_load(parts[1]) or {}
                except Exception:
                    front = {}
                if isinstance(front, dict):
                    name = str(front.get("name") or front.get("title") or name)
                    description = str(front.get("description") or "")
                body = parts[2].lstrip("\n")

        # 如果还没有 description，从正文抽取
        if not description:
            lines = body.splitlines()
            for line in lines:
                t = line.strip()
                if t.startswith("# "):
                    if name == fallback_name:
                        name = t[2:].strip() or name
                elif t and not t.startswith(("---", "```", "-")):
                    description = t
                    break

        return name, description, body

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
    def _append_metrics(
        header: list[str], row: list[Any], metrics: list[Any]
    ) -> tuple[list[str], list[Any]]:
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
