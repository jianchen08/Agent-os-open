"""
工具使用统计服务

提供工具使用统计跟踪功能，包括调用记录、性能统计和报告生成。
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from src.tools.types import ToolUsageStats

logger = logging.getLogger(__name__)


class ToolUsageTracker:
    """工具使用统计跟踪器"""

    def __init__(self):
        self.stats: dict[str, ToolUsageStats] = {}
        self.call_history: list[dict[str, Any]] = []
        self.user_stats: dict[str, dict[str, ToolUsageStats]] = {}
        self.category_stats: dict[str, ToolUsageStats] = {}
        self.daily_stats: dict[str, dict[str, int]] = {}

    def record_call(
        self,
        tool_name: str,
        success: bool,
        duration: float,
        error: str = None,
        user_id: str = None,
        category: str = None,
    ):
        """记录工具调用"""
        # 更新总体统计
        if tool_name not in self.stats:
            self.stats[tool_name] = ToolUsageStats(tool_name=tool_name)

        stats = self.stats[tool_name]
        stats.total_calls += 1
        stats.total_duration += duration
        stats.avg_duration = stats.total_duration / stats.total_calls
        stats.last_used = datetime.now()

        if success:
            stats.success_calls += 1
        else:
            stats.failed_calls += 1

        stats.error_rate = stats.failed_calls / stats.total_calls

        # 更新用户统计
        if user_id:
            self._update_user_stats(user_id, tool_name, success, duration)

        # 更新分类统计
        if category:
            self._update_category_stats(category, success, duration)

        # 更新日统计
        self._update_daily_stats(tool_name, success)

        # 记录调用历史
        self.call_history.append(
            {
                "tool_name": tool_name,
                "timestamp": datetime.now().isoformat(),
                "success": success,
                "duration": duration,
                "error": error,
                "user_id": user_id,
                "category": category,
            }
        )

        # 保持历史记录在合理范围内
        if len(self.call_history) > 10000:
            self.call_history = self.call_history[-5000:]

    def get_stats(self, tool_name: str = None) -> dict[str, ToolUsageStats]:
        """获取使用统计"""
        if tool_name:
            return {tool_name: self.stats.get(tool_name)}
        return self.stats.copy()

    def get_user_stats(self, user_id: str) -> dict[str, ToolUsageStats]:
        """获取用户使用统计"""
        return self.user_stats.get(user_id, {}).copy()

    def get_category_stats(self, category: str = None) -> dict[str, ToolUsageStats]:
        """获取分类统计"""
        if category:
            return {category: self.category_stats.get(category)}
        return self.category_stats.copy()

    def get_daily_stats(self, days: int = 7) -> dict[str, dict[str, int]]:
        """获取日统计"""
        today = datetime.now().date()
        result = {}

        for i in range(days):
            date_key = (today - timedelta(days=i)).isoformat()
            result[date_key] = self.daily_stats.get(date_key, {})

        return result

    def get_top_tools(self, limit: int = 10, by: str = "calls") -> list[ToolUsageStats]:
        """获取使用最多的工具"""
        sort_key = {
            "calls": lambda x: x.total_calls,
            "success": lambda x: x.success_calls,
            "duration": lambda x: x.total_duration,
            "error_rate": lambda x: x.error_rate,
        }.get(by, lambda x: x.total_calls)

        return sorted(self.stats.values(), key=sort_key, reverse=True)[:limit]

    def get_performance_report(self) -> dict[str, Any]:
        """获取性能报告"""
        total_calls = sum(stat.total_calls for stat in self.stats.values())
        total_success = sum(stat.success_calls for stat in self.stats.values())
        total_duration = sum(stat.total_duration for stat in self.stats.values())

        return {
            "total_calls": total_calls,
            "total_success": total_success,
            "overall_success_rate": (total_success / total_calls if total_calls > 0 else 0),
            "average_duration": total_duration / total_calls if total_calls > 0 else 0,
            "total_tools": len(self.stats),
            "active_users": len(self.user_stats),
            "top_tools": [stat.tool_name for stat in self.get_top_tools(5)],
            "slowest_tools": [
                stat.tool_name for stat in sorted(self.stats.values(), key=lambda x: x.avg_duration, reverse=True)[:5]
            ],
        }

    def _update_user_stats(self, user_id: str, tool_name: str, success: bool, duration: float):
        """更新用户统计"""
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {}

        if tool_name not in self.user_stats[user_id]:
            self.user_stats[user_id][tool_name] = ToolUsageStats(tool_name=tool_name)

        user_stat = self.user_stats[user_id][tool_name]
        user_stat.total_calls += 1
        user_stat.total_duration += duration
        user_stat.avg_duration = user_stat.total_duration / user_stat.total_calls
        user_stat.last_used = datetime.now()

        if success:
            user_stat.success_calls += 1
        else:
            user_stat.failed_calls += 1

        user_stat.error_rate = user_stat.failed_calls / user_stat.total_calls

    def _update_category_stats(self, category: str, success: bool, duration: float):
        """更新分类统计"""
        if category not in self.category_stats:
            self.category_stats[category] = ToolUsageStats(tool_name=category)

        cat_stat = self.category_stats[category]
        cat_stat.total_calls += 1
        cat_stat.total_duration += duration
        cat_stat.avg_duration = cat_stat.total_duration / cat_stat.total_calls
        cat_stat.last_used = datetime.now()

        if success:
            cat_stat.success_calls += 1
        else:
            cat_stat.failed_calls += 1

        cat_stat.error_rate = cat_stat.failed_calls / cat_stat.total_calls

    def _update_daily_stats(self, tool_name: str, success: bool):
        """更新日统计"""
        date_key = datetime.now().date().isoformat()

        if date_key not in self.daily_stats:
            self.daily_stats[date_key] = {}

        if tool_name not in self.daily_stats[date_key]:
            self.daily_stats[date_key][tool_name] = 0

        self.daily_stats[date_key][tool_name] += 1
