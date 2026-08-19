#!/usr/bin/env python3
"""Debug Center Plugin — 调试中心页面插件（纯声明，无运行时能力）。

声明即贡献（无业务逻辑、无工具、无 http_endpoints）：
1. contributes.pages —— 6 个调试页面的前端贡献点（activity-bar 侧边栏入口
   + /p/:pageId 直达），前端按 widget 名（debug_* 预置组件）渲染：
   - debug_db_admin          数据库管理（/ext/db_admin/*）
   - debug_execution_records 执行记录（/ext/channel_api/execution/*）
   - debug_sessions          会话（/ext/channel_api/execution/*）
   - debug_tasks             任务（/ext/monitoring|channel_api/tasks/*）
   - debug_users             用户（/ext/channel_api/users/*）
   - debug_evaluation_metrics 评估指标（/ext/evaluation_service/metrics）
   - debug_llm_payload        LLM 请求快照（/ext/monitoring/payload-diag/*，
                             2026-08-19 并入调试中心 hub）

页面数据经各数据源插件 HTTP 面获取，本插件不承载任何数据。
本插件无运行时能力；sidecar 仅为满足内核 entry 必填约束，空转即可。

[来源: 调试页面插件化（debug_center 立项）——同构先例 visual_customization_demo]
"""

from __future__ import annotations

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("debug_center")


if __name__ == "__main__":
    plugin.run()
