"""
外部资源搜索模块

暴露接口：
- ExternalResourceSearch：外部资源搜索类（平台搜索 + LLM审查 + 本地缓存）
- PlatformAdapter：平台适配器抽象基类
"""

import asyncio
import json
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 平台适配器抽象基类
# ---------------------------------------------------------------------------


class PlatformAdapter(ABC):
    """外部平台适配器抽象基类，具体平台（如 MCP Hub）需继承此类并实现 search 方法。"""

    @abstractmethod
    async def search(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        搜索外部平台资源

        Args:
            query: 搜索关键词
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            资源列表，每项包含 name / description / schema / source_platform 等字段
        """


# ---------------------------------------------------------------------------
# LLM 调用协议（与 llm.adapter.LLMAdapter 兼容）
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMCaller(Protocol):
    """LLM 调用协议，仅需 completion 方法"""

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """调用 LLM，返回包含 .text 属性的响应对象"""


# ---------------------------------------------------------------------------
# 外部资源搜索主类
# ---------------------------------------------------------------------------


class ExternalResourceSearch:
    """
    外部资源搜索器

    核心能力：
    - 通过平台适配器搜索外部 Skill/MCP 平台
    - 单次 LLM 调用审查 schema 安全性
    - 本地 JSON 文件存储信任缓存（trust_score / usage_count / review_status）
    - 使用反馈：成功提升 trust_score，失败降级
    - 搜索优先级：本地缓存(信任分高) > 外部平台新发现
    """

    # 信任分阈值：高于此值视为可信资源
    TRUST_THRESHOLD = 0.7
    # 单次成功/失败对 trust_score 的影响幅度
    TRUST_DELTA = 0.05
    # 最低信任分
    TRUST_MIN = 0.0
    # 最高信任分
    TRUST_MAX = 1.0

    def __init__(
        self,
        cache_path: str = "data/external_tool_cache.json",
        llm_caller: LLMCaller | None = None,
        review_model: str = "fast",
        platforms: list[PlatformAdapter] | None = None,
        max_results: int = 5,
    ):
        """
        初始化外部资源搜索器

        Args:
            cache_path: 缓存 JSON 文件路径
            llm_caller: LLM 调用实例（需符合 LLMCaller 协议），None 则跳过审查
            review_model: 审查用的模型标识（fast/thorough 或具体模型名）
            platforms: 平台适配器列表
            max_results: 每次搜索最大返回数量
        """
        self._cache_path = Path(cache_path)
        self._llm_caller = llm_caller
        self._review_model = review_model
        self._platforms: list[PlatformAdapter] = platforms or []
        self._max_results = max_results
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._cache_loaded = False

    # -----------------------------------------------------------------------
    # 公开接口
    # -----------------------------------------------------------------------

    async def search(
        self,
        query: str,
        resource_type: str = "tool",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        搜索外部资源，优先返回本地缓存中的可信资源

        Args:
            query: 搜索关键词
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            资源列表，每项包含 name / description / schema / source / trust_score / review_status
        """
        if not query:
            return []

        await self._ensure_cache_loaded()

        results: list[dict[str, Any]] = []

        # 1. 先从本地缓存中搜索已信任的资源
        cached_results = self._search_cached(query, resource_type, limit)
        results.extend(cached_results)
        remaining = limit - len(results)

        # 2. 缓存结果不足时，搜索外部平台
        if remaining > 0 and self._platforms:
            platform_results = await self._search_platforms(query, resource_type, remaining)
            # 过滤掉已在缓存中的结果
            existing_names = {r["name"] for r in results}
            for item in platform_results:
                if item["name"] not in existing_names:
                    results.append(item)
                    existing_names.add(item["name"])

        return results[:limit]

    async def record_usage(self, name: str, success: bool) -> None:
        """
        记录使用反馈，成功提升 trust_score，失败降级

        Args:
            name: 资源名称
            success: 是否使用成功
        """
        await self._ensure_cache_loaded()

        with self._lock:
            if name not in self._cache:
                return

            entry = self._cache[name]
            entry["usage_count"] = entry.get("usage_count", 0) + 1

            if success:
                entry["success_count"] = entry.get("success_count", 0) + 1
                old_score = entry.get("trust_score", 0.5)
                entry["trust_score"] = min(self.TRUST_MAX, old_score + self.TRUST_DELTA)
            else:
                old_score = entry.get("trust_score", 0.5)
                entry["trust_score"] = max(self.TRUST_MIN, old_score - self.TRUST_DELTA * 2)

            entry["last_used"] = datetime.now().isoformat()

        await self._async_save_cache()

        logger.info(
            "[external_search] 使用反馈: name=%s success=%s trust_score=%.2f",
            name,
            success,
            entry.get("trust_score", 0.5),
        )

    def get_cached_resource(self, name: str) -> dict[str, Any] | None:
        """
        获取缓存中的资源信息

        Args:
            name: 资源名称

        Returns:
            资源缓存条目，不存在则返回 None
        """
        return self._cache.get(name)

    def add_platform(self, platform: PlatformAdapter) -> None:
        """
        添加平台适配器

        Args:
            platform: 平台适配器实例
        """
        self._platforms.append(platform)

    # -----------------------------------------------------------------------
    # 缓存管理
    # -----------------------------------------------------------------------

    async def _ensure_cache_loaded(self) -> None:
        """确保缓存已加载，首次调用时异步加载"""
        if not self._cache_loaded:
            await self._load_cache_async()
            self._cache_loaded = True

    async def _load_cache_async(self) -> None:
        """从 JSON 文件异步加载缓存，文件不存在则创建空缓存"""
        try:
            if self._cache_path.exists():
                data = await asyncio.to_thread(self._read_cache_file)
                if isinstance(data, dict):
                    self._cache = data
                    logger.info(
                        "[external_search] 缓存加载成功: %d 条记录",
                        len(self._cache),
                    )
                    return
            # 文件不存在或格式错误，创建空缓存
            self._cache = {}
            self._ensure_cache_dir()
            await self._async_save_cache()
        except Exception as e:
            logger.warning("[external_search] 缓存加载失败，使用空缓存: %s", e)
            self._cache = {}

    def _read_cache_file(self) -> dict:
        """同步读取缓存文件（供 asyncio.to_thread 调用）"""
        with open(self._cache_path, encoding="utf-8") as f:
            return json.load(f)

    def _save_cache(self) -> None:
        """将缓存写入 JSON 文件（同步版本，调用方需持有 self._lock）"""
        try:
            self._ensure_cache_dir()
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[external_search] 缓存写入失败: %s", e)

    async def _async_save_cache(self) -> None:
        """将缓存异步写入 JSON 文件"""
        try:
            self._ensure_cache_dir()
            await asyncio.to_thread(self._write_cache_file)
        except Exception as e:
            logger.warning("[external_search] 缓存写入失败: %s", e)

    def _write_cache_file(self) -> None:
        """同步写入缓存文件（供 asyncio.to_thread 调用）"""
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def _ensure_cache_dir(self) -> None:
        """确保缓存文件所在目录存在"""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _search_cached(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        从本地缓存中搜索匹配的资源，按 trust_score 降序排列

        Args:
            query: 搜索关键词
            resource_type: 资源类型
            limit: 最大返回数量

        Returns:
            匹配的缓存资源列表
        """
        query_lower = query.lower()
        results: list[dict[str, Any]] = []

        with self._lock:
            for name, entry in self._cache.items():
                # 类型过滤
                if entry.get("resource_type") != resource_type:
                    continue
                # 关键词匹配
                if query_lower not in name.lower() and query_lower not in entry.get("description", "").lower():
                    continue

                results.append(
                    {
                        "name": name,
                        "description": entry.get("description", ""),
                        "schema": entry.get("schema", {}),
                        "source": entry.get("source_platform", "cache"),
                        "trust_score": entry.get("trust_score", 0.5),
                        "review_status": entry.get("review_status", "unreviewed"),
                        "usage_count": entry.get("usage_count", 0),
                        "from_cache": True,
                    }
                )

        # 按 trust_score 降序排列
        results.sort(key=lambda x: x["trust_score"], reverse=True)
        return results[:limit]

    # -----------------------------------------------------------------------
    # 外部平台搜索
    # -----------------------------------------------------------------------

    async def _search_platforms(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        搜索所有已注册的外部平台

        Args:
            query: 搜索关键词
            resource_type: 资源类型
            limit: 最大返回数量

        Returns:
            合并后的搜索结果列表
        """
        if not self._platforms:
            return []

        async def _search_one(platform: PlatformAdapter) -> list[dict[str, Any]]:
            try:
                return await platform.search(query, resource_type, limit)
            except Exception as e:
                logger.warning(
                    "[external_search] 平台 %s 搜索失败: %s",
                    platform.__class__.__name__,
                    e,
                )
                return []

        platform_results = await asyncio.gather(*[_search_one(p) for p in self._platforms])

        all_results: list[dict[str, Any]] = []
        for items in platform_results:
            for item in items:
                name = item.get("name", "")
                if not name:
                    continue

                # LLM 审查
                review = await self._review_resource(item)

                result = {
                    "name": name,
                    "description": item.get("description", ""),
                    "schema": item.get("schema", {}),
                    "source": item.get("source_platform", ""),
                    "trust_score": 0.5,
                    "review_status": review.get("status", "unreviewed"),
                    "risk_level": review.get("risk_level", "unknown"),
                    "needs_deep_review": review.get("needs_deep_review", False),
                    "from_cache": False,
                }

                # 写入缓存
                await self._update_cache_entry(
                    name,
                    {
                        "name": name,
                        "description": result["description"],
                        "schema": result["schema"],
                        "source_platform": result["source"],
                        "resource_type": resource_type,
                        "trust_score": result["trust_score"],
                        "review_status": result["review_status"],
                        "risk_level": result.get("risk_level", "unknown"),
                        "needs_deep_review": result.get("needs_deep_review", False),
                        "usage_count": 0,
                        "success_count": 0,
                        "first_seen": datetime.now().isoformat(),
                    },
                )

                all_results.append(result)

        return all_results[:limit]

    async def _update_cache_entry(self, name: str, entry: dict[str, Any]) -> None:
        """
        更新缓存条目（仅在新资源时写入，已有资源保留使用统计）

        Args:
            name: 资源名称
            entry: 缓存条目数据
        """
        with self._lock:
            if name in self._cache:
                # 已存在，只更新 schema 和审查状态，保留使用统计
                existing = self._cache[name]
                existing["schema"] = entry.get("schema", existing.get("schema", {}))
                existing["review_status"] = entry.get("review_status", existing.get("review_status", "unreviewed"))
                existing["risk_level"] = entry.get("risk_level", existing.get("risk_level", "unknown"))
                existing["needs_deep_review"] = entry.get("needs_deep_review", existing.get("needs_deep_review", False))
            else:
                self._cache[name] = entry

        await self._async_save_cache()

    # -----------------------------------------------------------------------
    # LLM 审查
    # -----------------------------------------------------------------------

    async def _review_resource(self, resource: dict[str, Any]) -> dict[str, Any]:
        """
        使用 LLM 快速审查资源 schema 的安全性

        审查失败时降级返回（标记为 unreviewed 而非丢弃）

        Args:
            resource: 资源信息，包含 name / description / schema 等

        Returns:
            审查结果字典，包含 status / risk_level / needs_deep_review
        """
        if not self._llm_caller:
            return {"status": "unreviewed", "risk_level": "unknown", "needs_deep_review": True}

        try:
            prompt = self._build_review_prompt(resource)
            response = await asyncio.wait_for(
                self._llm_caller.completion(
                    model=self._review_model,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=15.0,
            )

            # 解析响应
            text = getattr(response, "text", None) or ""
            if not text and hasattr(response, "choices"):
                text = response.choices[0].message.content or ""

            return self._parse_review_response(text)

        except asyncio.TimeoutError:
            logger.warning(
                "[external_search] LLM 审查超时，降级为未审查: name=%s",
                resource.get("name", ""),
            )
            return {"status": "unreviewed", "risk_level": "unknown", "needs_deep_review": True}
        except Exception as e:
            logger.warning(
                "[external_search] LLM 审查失败，降级为未审查: name=%s error=%s",
                resource.get("name", ""),
                e,
            )
            return {"status": "unreviewed", "risk_level": "unknown", "needs_deep_review": True}

    def _build_review_prompt(self, resource: dict[str, Any]) -> str:
        """
        构建 LLM 审查 prompt

        Args:
            resource: 资源信息

        Returns:
            审查 prompt 字符串
        """
        name = resource.get("name", "")
        description = resource.get("description", "")
        schema = resource.get("schema", {})

        return (
            "请审查以下外部工具/技能的 schema 安全性。只输出 JSON 格式，不要输出其他内容。\n\n"
            f"工具名称: {name}\n"
            f"工具描述: {description}\n"
            f"输入 Schema: {json.dumps(schema, ensure_ascii=False, indent=2) if schema else '无'}\n\n"
            "请输出以下 JSON 格式：\n"
            "```json\n"
            "{\n"
            '  "safe": true/false,\n'
            '  "risk_level": "low/medium/high",\n'
            '  "needs_deep_review": true/false,\n'
            '  "reason": "简要说明"\n'
            "}\n"
            "```\n\n"
            "审查标准：\n"
            "1. 是否包含文件系统写入/删除等危险操作\n"
            "2. 是否包含网络请求到可疑地址\n"
            "3. 是否包含代码执行（eval/exec）相关参数\n"
            "4. 是否请求系统级权限\n"
            "5. 参数类型是否合理，有无注入风险"
        )

    def _parse_review_response(self, text: str) -> dict[str, Any]:
        """
        解析 LLM 审查响应为结构化结果

        Args:
            text: LLM 响应文本

        Returns:
            审查结果字典
        """
        import re  # noqa: PLC0415

        # 尝试从 markdown 代码块中提取 JSON
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)

        # 尝试直接提取 JSON
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)

        try:
            data = json.loads(text)
            safe = bool(data.get("safe", False))
            risk_level = str(data.get("risk_level", "unknown")).lower()
            needs_deep_review = bool(data.get("needs_deep_review", not safe))

            return {
                "status": "safe" if safe else "risky",
                "risk_level": risk_level,
                "needs_deep_review": needs_deep_review,
                "reason": data.get("reason", ""),
            }
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("[external_search] 审查响应解析失败: %s", e)
            return {"status": "unreviewed", "risk_level": "unknown", "needs_deep_review": True}
