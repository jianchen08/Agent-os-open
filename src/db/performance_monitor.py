"""
数据库查询性能监控

提供查询性能统计、慢查询检测和性能分析功能
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class QueryPerformanceMonitor:
    """
    查询性能监控器

    功能：
    1. 统计查询次数和执行时间
    2. 检测慢查询
    3. 识别 N+1 查询模式
    4. 生成性能报告
    """

    def __init__(self, slow_query_threshold: float = 0.5):
        """
        初始化性能监控器

        Args:
            slow_query_threshold: 慢查询阈值（秒）
        """
        self.slow_query_threshold = slow_query_threshold
        self.query_stats: dict[str, dict[str, Any]] = {}
        self._session_queries: list[dict[str, Any]] = []

    @asynccontextmanager
    async def monitor_query(self, query: str, params: dict | None = None):
        """
        监控单个查询

        Args:
            query: SQL 查询语句
            params: 查询参数
        """
        start_time = time.time()
        query_info = {
            "query": query[:200],  # 截断长查询
            "params": str(params)[:100] if params else None,
            "start_time": start_time,
        }

        try:
            yield
        finally:
            elapsed = time.time() - start_time
            query_info["elapsed"] = elapsed

            # 记录查询
            self._session_queries.append(query_info)

            # 更新统计
            query_hash = hash(query)
            if query_hash not in self.query_stats:
                self.query_stats[query_hash] = {
                    "query": query[:200],
                    "count": 0,
                    "total_time": 0,
                    "max_time": 0,
                }

            stats = self.query_stats[query_hash]
            stats["count"] += 1
            stats["total_time"] += elapsed
            stats["max_time"] = max(stats["max_time"], elapsed)

            # 慢查询告警
            if elapsed > self.slow_query_threshold:
                logger.warning(
                    f"[PerformanceMonitor] 慢查询检测 | "
                    f"query={query[:100]}... | "
                    f"elapsed={elapsed:.3f}s | "
                    f"threshold={self.slow_query_threshold}s"
                )

    def detect_n_plus_1(self) -> list[dict[str, Any]]:
        """
        检测 N+1 查询模式

        Returns:
            N+1 查询列表
        """
        n_plus_1_queries = []

        # 检查在短时间内执行的相似查询
        for _query_hash, stats in self.query_stats.items():
            if stats["count"] > 5:  # 阈值：相同查询执行超过 5 次
                avg_time = stats["total_time"] / stats["count"]

                n_plus_1_queries.append(
                    {
                        "query": stats["query"],
                        "count": stats["count"],
                        "avg_time": avg_time,
                        "total_time": stats["total_time"],
                        "max_time": stats["max_time"],
                    }
                )

        return n_plus_1_queries

    def get_report(self) -> dict[str, Any]:
        """
        生成性能报告

        Returns:
            性能报告字典
        """
        total_queries = len(self._session_queries)
        total_time = sum(q["elapsed"] for q in self._session_queries)

        n_plus_1_queries = self.detect_n_plus_1()

        slow_queries = [
            q for q in self._session_queries if q["elapsed"] > self.slow_query_threshold
        ]

        report = {
            "summary": {
                "total_queries": total_queries,
                "total_time": total_time,
                "avg_time": total_time / total_queries if total_queries > 0 else 0,
                "slow_queries": len(slow_queries),
                "n_plus_1_patterns": len(n_plus_1_queries),
            },
            "slow_queries": [
                {
                    "query": q["query"],
                    "elapsed": q["elapsed"],
                    "params": q["params"],
                }
                for q in slow_queries
            ],
            "n_plus_1_queries": n_plus_1_queries,
            "top_queries": sorted(
                [
                    {
                        "query": stats["query"],
                        "count": stats["count"],
                        "total_time": stats["total_time"],
                    }
                    for stats in self.query_stats.values()
                ],
                key=lambda x: x["count"],
                reverse=True,
            )[:10],
        }

        return report

    def reset(self):
        """重置统计信息"""
        self.query_stats.clear()
        self._session_queries.clear()

    def log_report(self):
        """记录性能报告到日志"""
        report = self.get_report()

        logger.info(
            f"[PerformanceMonitor] 性能报告 | "
            f"total_queries={report['summary']['total_queries']} | "
            f"total_time={report['summary']['total_time']:.3f}s | "
            f"avg_time={report['summary']['avg_time']:.3f}s | "
            f"slow_queries={report['summary']['slow_queries']} | "
            f"n_plus_1_patterns={report['summary']['n_plus_1_patterns']}"
        )

        if report["summary"]["n_plus_1_patterns"] > 0:
            logger.warning(
                f"[PerformanceMonitor] 检测到 N+1 查询模式 | "
                f"count={report['summary']['n_plus_1_patterns']}"
            )
            for pattern in report["n_plus_1_queries"][:3]:  # 只打印前 3 个
                logger.warning(
                    f"[PerformanceMonitor] N+1 模式 | "
                    f"query={pattern['query'][:50]}... | "
                    f"count={pattern['count']} | "
                    f"total_time={pattern['total_time']:.3f}s"
                )


# 全局性能监控器实例
_global_monitor: QueryPerformanceMonitor | None = None


def get_performance_monitor() -> QueryPerformanceMonitor:
    """获取全局性能监控器"""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = QueryPerformanceMonitor()
    return _global_monitor


async def log_queries(session: AsyncSession, monitor: QueryPerformanceMonitor):
    """
    记录 SQLAlchemy 会话的所有查询

    Args:
        session: 数据库会话
        monitor: 性能监控器
    """

    @event.listens_for(session.sync_session, "before_cursor_execute", named=True)
    def receive_before_cursor_execute(**kw):
        monitor._session_queries.append(
            {
                "query": str(kw["statement"]),
                "params": str(kw["parameters"]),
                "start_time": time.time(),
            }
        )

    @event.listens_for(session.sync_session, "after_cursor_execute", named=True)
    def receive_after_cursor_execute(**kw):
        if monitor._session_queries:
            last_query = monitor._session_queries[-1]
            if "elapsed" not in last_query:
                last_query["elapsed"] = time.time() - last_query["start_time"]
