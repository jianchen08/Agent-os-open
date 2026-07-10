"""功能覆盖率追踪器。

按 features.md 14 大类 × 74 功能点统计覆盖率，
生成 HTML 片段嵌入 combined_report.html。

来源：.project/features.md、docs/构建统一CICD测试体系_solution.md §3.4.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FeatureItem:
    """单个功能点。"""

    name: str
    description: str
    category: str
    tested: bool = False
    test_names: list[str] = field(default_factory=list)
    note: str = ""


# ── 14 大类 × 功能点定义 ──────────────────────────────
# 来源：.project/features.md 一、用户功能清单

FEATURE_CATEGORIES: dict[str, list[dict[str, str]]] = {
    "1. 对话与聊天": [
        {"name": "主对话", "desc": "用户与灵汐对话，流式响应"},
        {"name": "会话管理", "desc": "创建/切换/删除会话"},
        {"name": "更换主Agent", "desc": "在主管道切换对话Agent"},
        {"name": "审批交互", "desc": "工具调用需用户确认时弹出审批"},
        {"name": "媒体消息", "desc": "图片/视频/音频消息的发送和展示"},
        {"name": "Schema表单", "desc": "结构化表单交互"},
    ],
    "2. 任务管理": [
        {"name": "任务创建", "desc": "提交任务给指定Agent执行"},
        {"name": "任务状态追踪", "desc": "查看任务状态"},
        {"name": "容器任务", "desc": "管理含多个子任务的容器任务"},
        {"name": "任务评估", "desc": "验证任务是否满足验收标准"},
        {"name": "任务看板", "desc": "查看所有任务概览"},
        {"name": "任务工作空间", "desc": "查看任务的工作空间文件"},
    ],
    "3. Agent管理": [
        {"name": "Agent列表", "desc": "查看所有已配置Agent"},
        {"name": "Agent配置", "desc": "查看/修改Agent YAML配置"},
        {"name": "Agent层级", "desc": "L1→L2→L3三层委托体系"},
    ],
    "4. 工具系统": [
        {"name": "文件操作工具", "desc": "file_read/write/list/copy/move/delete"},
        {"name": "代码搜索", "desc": "enhanced_search (ripgrep)"},
        {"name": "Shell执行", "desc": "bash_execute"},
        {"name": "网络搜索", "desc": "web_search"},
        {"name": "浏览器测试", "desc": "playwright_test"},
        {"name": "资源搜索", "desc": "resource_search"},
        {"name": "任务管理工具", "desc": "task_submit/evaluate/manage"},
        {"name": "记忆操作", "desc": "memory store/retrieve/import"},
        {"name": "触发器管理工具", "desc": "trigger_setup"},
        {"name": "代码审查工具", "desc": "trigger_review"},
        {"name": "下载工具", "desc": "download"},
        {"name": "热替换", "desc": "hot_swap"},
        {"name": "人类交互工具", "desc": "human_interaction"},
        {"name": "LSP工具", "desc": "lsp_tools"},
        {"name": "IDE集成", "desc": "ide_open_file/get_selection/show_diff"},
        {"name": "媒体生成", "desc": "image/video/music/tts_generate"},
        {"name": "格式转换", "desc": "binary_converter/unit_converter"},
        {"name": "计算器", "desc": "scientific_calculator"},
        {"name": "YAML验证", "desc": "yaml_validate"},
        {"name": "状态更新", "desc": "state_update"},
        {"name": "资源合并", "desc": "resource_merge"},
        {"name": "外部工具", "desc": "MCP协议接入"},
        {"name": "工具注册", "desc": "register_resource"},
    ],
    "5. 记忆与知识库": [
        {"name": "知识库管理", "desc": "创建/删除/导入知识库"},
        {"name": "记忆存储", "desc": "存储语义记忆和情景记忆"},
        {"name": "记忆检索", "desc": "向量/关键词/TagWave三种检索"},
        {"name": "记忆注入", "desc": "全量/检索/摘要三种注入"},
    ],
    "6. 触发器系统": [
        {"name": "定时触发器", "desc": "Cron表达式定时执行"},
        {"name": "事件触发器", "desc": "系统事件驱动执行"},
        {"name": "间隔触发器", "desc": "固定间隔执行"},
        {"name": "触发器管理", "desc": "创建/启停/删除触发器"},
    ],
    "7. 评估系统": [
        {"name": "评估指标", "desc": "file_check/format_valid/semantic_check等"},
        {"name": "评估执行", "desc": "自动评估任务产出"},
        {"name": "评估结果", "desc": "查看评估通过/失败/分数"},
    ],
    "8. 配置管理": [
        {"name": "API设置", "desc": "API相关配置"},
        {"name": "并发设置", "desc": "任务/Agent/LLM并发配置"},
        {"name": "上下文窗口", "desc": "上下文预算/压缩/层级"},
        {"name": "成本控制", "desc": "LLM调用成本追踪"},
        {"name": "LLM模型", "desc": "模型/Provider/默认模型配置"},
        {"name": "模块配置", "desc": "各功能模块开关"},
        {"name": "插件配置", "desc": "插件管理"},
        {"name": "其他配置", "desc": "记忆/隔离/安全/评估等"},
    ],
    "9. 监控": [
        {"name": "系统监控", "desc": "实时监控Agent/任务/资源状态"},
        {"name": "调试会话", "desc": "查看历史会话详情"},
        {"name": "执行记录", "desc": "查看工具/LLM调用记录"},
        {"name": "用户管理", "desc": "查看和管理用户"},
    ],
    "10. 认证与权限": [
        {"name": "用户注册", "desc": "新用户注册"},
        {"name": "用户登录", "desc": "JWT Token认证登录"},
        {"name": "角色权限", "desc": "RBAC角色权限控制"},
        {"name": "Token管理", "desc": "Token生成/验证/撤销/刷新"},
    ],
    "11. 多通道接入": [
        {"name": "WebSocket", "desc": "Web前端主通道"},
        {"name": "CLI", "desc": "命令行界面"},
        {"name": "钉钉", "desc": "钉钉机器人"},
        {"name": "飞书", "desc": "飞书机器人"},
        {"name": "QQ", "desc": "QQ机器人"},
        {"name": "企业微信", "desc": "企微机器人"},
        {"name": "HTTP API", "desc": "RESTful配置读写API"},
    ],
    "12. 工作空间与隔离": [
        {"name": "工作空间", "desc": "每个任务独立工作目录"},
        {"name": "Docker隔离", "desc": "容器化执行环境"},
        {"name": "Git集成", "desc": "工作空间版本管理"},
    ],
    "13. 管道与插件": [
        {"name": "管道引擎", "desc": "消息处理核心调度"},
        {"name": "输入插件", "desc": "消息预处理链"},
        {"name": "输出插件", "desc": "响应后处理链"},
        {"name": "LLM适配", "desc": "多Provider LLM调用"},
    ],
    "14. 技能系统": [
        {"name": "技能加载", "desc": "从skills/目录加载脚本"},
        {"name": "技能执行", "desc": "运行Python/Node/Bash/PowerShell脚本"},
    ],
}


class FeatureCoverageTracker:
    """功能覆盖率追踪器。

    用法::

        tracker = FeatureCoverageTracker()
        tracker.mark_tested("1. 对话与聊天", "主对话", test_names=["test_chat"])
        html = tracker.to_html()
    """

    def __init__(self) -> None:
        self._items: list[FeatureItem] = []
        self._by_category: dict[str, list[FeatureItem]] = {}

        for cat, features in FEATURE_CATEGORIES.items():
            cat_items: list[FeatureItem] = []
            for feat in features:
                item = FeatureItem(
                    name=feat["name"],
                    description=feat["desc"],
                    category=cat,
                )
                self._items.append(item)
                cat_items.append(item)
            self._by_category[cat] = cat_items

    @property
    def total_features(self) -> int:
        """总功能点数。"""
        return len(self._items)

    @property
    def tested_features(self) -> int:
        """已测试功能点数。"""
        return sum(1 for item in self._items if item.tested)

    @property
    def coverage_rate(self) -> float:
        """覆盖率（0.0~1.0）。"""
        return self.tested_features / self.total_features if self.total_features > 0 else 0.0

    def mark_tested(
        self,
        category: str,
        feature_name: str,
        test_names: list[str] | None = None,
        note: str = "",
    ) -> None:
        """标记指定功能点为已测试。"""
        for item in self._by_category.get(category, []):
            if item.name == feature_name:
                item.tested = True
                if test_names:
                    item.test_names.extend(test_names)
                if note:
                    item.note = note
                return

    def mark_category_tested(
        self,
        category: str,
        test_names: list[str] | None = None,
    ) -> None:
        """标记整个大类所有功能点为已测试。"""
        for item in self._by_category.get(category, []):
            item.tested = True
            if test_names:
                item.test_names.extend(test_names)

    def category_stats(self, category: str) -> dict[str, int]:
        """返回指定大类的统计。"""
        items = self._by_category.get(category, [])
        tested = sum(1 for i in items if i.tested)
        return {"total": len(items), "tested": tested}

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        categories: list[dict[str, Any]] = []
        for cat, items in self._by_category.items():
            tested = sum(1 for i in items if i.tested)
            categories.append(
                {
                    "category": cat,
                    "total": len(items),
                    "tested": tested,
                    "rate": tested / len(items) if items else 0.0,
                    "features": [
                        {
                            "name": i.name,
                            "description": i.description,
                            "tested": i.tested,
                            "test_names": i.test_names,
                            "note": i.note,
                        }
                        for i in items
                    ],
                }
            )
        return {
            "total": self.total_features,
            "tested": self.tested_features,
            "rate": self.coverage_rate,
            "categories": categories,
        }

    def to_html(self) -> str:
        """生成功能覆盖率矩阵的 HTML 片段。"""
        cat_rows = ""
        for cat, items in self._by_category.items():
            tested = sum(1 for i in items if i.tested)
            total = len(items)
            pct = tested / total * 100 if total > 0 else 0
            bar_color = "#4caf50" if pct >= 80 else "#ff9800" if pct >= 50 else "#f44336"

            feature_cells = ""
            for item in items:
                icon = "✅" if item.tested else "⬜"
                feature_cells += f'<span class="feature-tag {"tested" if item.tested else "untested"}">{icon} {item.name}</span>'

            cat_rows += f"""
            <tr>
                <td class="cat-name">{cat}</td>
                <td class="cat-progress">
                    <div class="mini-bar"><div class="mini-bar-fill" style="width:{pct:.0f}%;background:{bar_color}"></div></div>
                    {tested}/{total} ({pct:.0f}%)
                </td>
                <td class="cat-features">{feature_cells}</td>
            </tr>"""

        overall_pct = self.coverage_rate * 100
        bar_color = (
            "#4caf50" if overall_pct >= 80 else "#ff9800" if overall_pct >= 50 else "#f44336"
        )

        return f"""
        <div class="feature-coverage">
            <div class="section-header">
                <h2>📋 功能覆盖率矩阵</h2>
                <div class="coverage-summary">
                    {self.tested_features}/{self.total_features} 功能点已覆盖 ({overall_pct:.1f}%)
                </div>
            </div>
            <div class="bar"><div class="bar-fill" style="width:{overall_pct:.1f}%;background:{bar_color}"></div></div>
            <table class="coverage-table">
                <tr><th>大类</th><th>覆盖率</th><th>功能点</th></tr>
                {cat_rows}
            </table>
        </div>"""


__all__ = ["FeatureCoverageTracker", "FEATURE_CATEGORIES"]
