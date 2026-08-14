#!/usr/bin/env python3
"""Visual Customization Demo Plugin — 插件前端定制化示范（主题 + CSS 注入）。

声明即贡献（无业务逻辑、无工具）：
1. contributes.themes['gold-lace'] —— 金色蕾丝主题包（纯 CSS 变量键值对，
   前端 ThemePanel 选中后 setProperty 应用，零 eval）
2. contributes.client_styles['gold-lace-border'] —— 金色蕾丝边框装饰 CSS，
   经内核 /ext/{pluginId}/assets/* 静态直出（web/border.css），前端注入 <style>

本插件无运行时能力；sidecar 仅为满足内核 entry 必填约束，空转即可。

[来源: docs/tasks/task_plugin_frontend_customization.md 任务 1/2]
"""

from __future__ import annotations

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("visual_customization_demo")


if __name__ == "__main__":
    plugin.run()
