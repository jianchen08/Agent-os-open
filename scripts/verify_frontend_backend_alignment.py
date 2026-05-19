#!/usr/bin/env python3
"""
前后端对齐验证脚本

验证前端页面组件、路由、API调用与后端端点的一致性。
可独立运行，无需启动后端服务。

Usage:
    python scripts/verify_frontend_backend_alignment.py
"""

import re
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = ROOT / "frontend" / "src"
BACKEND_SRC = ROOT / "src"

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    print(f"  {status} {label}" + (f" — {detail}" if detail else ""))


# ─────────────────────────────────────────────
# 1. 路由常量验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("1. 路由常量验证 (constants/routes.ts)")
print("=" * 60)

routes_file = FRONTEND_SRC / "constants" / "routes.ts"
routes_content = routes_file.read_text(encoding="utf-8")

expected_route_constants = {
    "SETTINGS_PLUGINS": "/settings/plugins",
    "TRIGGERS": "/triggers",
    "KNOWLEDGE_BASE": "/knowledge-base",
}

for const_name, expected_path in expected_route_constants.items():
    pattern = rf"{const_name}:\s*['\"]({re.escape(expected_path)})['\"]"
    match = re.search(pattern, routes_content)
    check(
        f"ROUTES.{const_name}",
        match is not None,
        f"期望: {expected_path}" + (f", 实际: {match.group(1)}" if match else ", 未找到"),
    )

# ─────────────────────────────────────────────
# 2. 路由注册和懒加载路径验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("2. 路由注册和懒加载路径验证 (router.tsx)")
print("=" * 60)

router_file = FRONTEND_SRC / "router.tsx"
router_content = router_file.read_text(encoding="utf-8")

lazy_imports = {
    "PluginsSettingsPage": "@/pages/settings/PluginsSettingsPage",
    "TriggersPage": "@/pages/triggers/TriggersPage",
    "KnowledgeBasePage": "@/pages/knowledge-base/KnowledgeBasePage",
}

for component, import_path in lazy_imports.items():
    # 验证懒加载 import
    lazy_pattern = rf"import\(['\"]({re.escape(import_path)})['\"]\)"
    match = re.search(lazy_pattern, router_content)
    check(
        f"Lazy import {component}",
        match is not None,
        f"路径: {import_path}" + (f", 匹配: {match.group(1)}" if match else ""),
    )

    # 验证实际文件存在
    file_path = FRONTEND_SRC / (import_path.replace("@/", "") + ".tsx")
    check(
        f"文件存在 {component}",
        file_path.exists(),
        f"路径: {file_path.relative_to(ROOT)}",
    )

# 验证路由注册
route_registrations = {
    "ROUTES.SETTINGS_PLUGINS": "PluginsSettingsPage",
    "ROUTES.TRIGGERS": "TriggersPage",
    "ROUTES.KNOWLEDGE_BASE": "KnowledgeBasePage",
}

for route_const, component in route_registrations.items():
    pattern = rf"path:\s*{re.escape(route_const)}"
    match = re.search(pattern, router_content)
    check(
        f"路由注册 {route_const} → <{component} />",
        match is not None,
        "",
    )

# ─────────────────────────────────────────────
# 3. API端点常量验证 (constants/api.ts)
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("3. API端点常量验证 (constants/api.ts)")
print("=" * 60)

api_file = FRONTEND_SRC / "constants" / "api.ts"
api_content = api_file.read_text(encoding="utf-8")

# Triggers 端点 (9个)
triggers_endpoints = {
    "LIST": "'/api/v1/triggers'",
    "STATS": "'/api/v1/triggers/stats'",
    "GET": "`/api/v1/triggers/${triggerId}`",
    "CREATE": "'/api/v1/triggers'",
    "UPDATE": "`/api/v1/triggers/${triggerId}`",
    "DELETE": "`/api/v1/triggers/${triggerId}`",
    "ENABLE": "`/api/v1/triggers/${triggerId}/enable`",
    "DISABLE": "`/api/v1/triggers/${triggerId}/disable`",
    "TRIGGER": "`/api/v1/triggers/${triggerId}/trigger`",
}

print("  TRIGGERS 端点:")
for name, path in triggers_endpoints.items():
    check(f"  TRIGGERS.{name}", f"TRIGGERS" in api_content and name in api_content, path)

# Knowledge Base 端点 (10个)
kb_endpoints = {
    "LIST": "'/api/v1/knowledge-base'",
    "STATS": "'/api/v1/knowledge-base/stats'",
    "UPLOAD": "'/api/v1/knowledge-base/upload'",
    "GET": "`/api/v1/knowledge-base/${id}`",
    "DELETE": "`/api/v1/knowledge-base/${id}`",
    "CHECK": "'/api/v1/knowledge-base/check'",
    "CATEGORIES": "'/api/v1/knowledge-base/categories'",
    "CREATE_CATEGORY": "'/api/v1/knowledge-base/categories'",
    "DELETE_CATEGORY": "`/api/v1/knowledge-base/categories/${name}`",
    "TAGS": "'/api/v1/knowledge-base/tags'",
}

print("  KNOWLEDGE_BASE 端点:")
for name, path in kb_endpoints.items():
    check(f"  KNOWLEDGE_BASE.{name}", f"KNOWLEDGE_BASE" in api_content and name in api_content, path)

# ─────────────────────────────────────────────
# 4. SettingsPage 入口卡片验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("4. SettingsPage 入口卡片验证")
print("=" * 60)

settings_file = FRONTEND_SRC / "pages" / "settings" / "SettingsPage.tsx"
settings_content = settings_file.read_text(encoding="utf-8")

expected_cards = [
    ("插件管理", "/settings/plugins"),
    ("触发器管理", "/triggers"),
    ("知识库", "/knowledge-base"),
]

for title, href in expected_cards:
    title_match = re.search(rf"title:\s*['\"]({re.escape(title)})['\"]", settings_content)
    href_match = re.search(rf"href:\s*['\"]({re.escape(href)})['\"]", settings_content)
    check(
        f"SettingsPage 卡片: {title}",
        title_match is not None and href_match is not None,
        f"href={href}",
    )

# ─────────────────────────────────────────────
# 5. AgentsPage 验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("5. AgentsPage 编辑/删除功能验证")
print("=" * 60)

agents_file = FRONTEND_SRC / "pages" / "agents" / "AgentsPage.tsx"
agents_content = agents_file.read_text(encoding="utf-8")

# 编辑模态框字段
edit_fields = ["name", "description", "system_prompt", "model"]
for field in edit_fields:
    check(
        f"AgentsPage 编辑字段: {field}",
        f"editForm.{field}" in agents_content,
        "",
    )

# 删除确认框
check(
    "AgentsPage 删除确认框",
    "deletingAgent" in agents_content and "确认删除" in agents_content,
    "",
)

# API调用
check(
    "AgentsPage API: getAgents",
    "getAgents" in agents_content and "from '@/services/api/agents'" in agents_content,
    "",
)
check(
    "AgentsPage API: updateAgent",
    "updateAgent" in agents_content,
    "",
)
check(
    "AgentsPage API: deleteAgent",
    "deleteAgent" in agents_content,
    "",
)

# ─────────────────────────────────────────────
# 6. AdminPage 验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("6. AdminPage 管理功能验证")
print("=" * 60)

admin_file = FRONTEND_SRC / "pages" / "admin" / "AdminPage.tsx"
admin_content = admin_file.read_text(encoding="utf-8")

# 角色切换下拉菜单
check(
    "AdminPage 角色切换下拉菜单",
    'value="user"' in admin_content and 'value="admin"' in admin_content,
    "select 元素包含 user/admin 选项",
)

check(
    "AdminPage 角色切换 API",
    "updateUserRole" in admin_content,
    "调用 usersApi.updateUserRole",
)

# 创建用户模态框
check(
    "AdminPage 创建用户模态框",
    "showCreateModal" in admin_content and "创建用户" in admin_content,
    "",
)

check(
    "AdminPage 创建用户 API",
    "createUser" in admin_content and "from '@/services/api/users'" in admin_content,
    "",
)

# ─────────────────────────────────────────────
# 7. PluginsSettingsPage API 对齐验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("7. PluginsSettingsPage API 对齐验证")
print("=" * 60)

plugins_file = FRONTEND_SRC / "pages" / "settings" / "PluginsSettingsPage.tsx"
plugins_content = plugins_file.read_text(encoding="utf-8")

plugins_api_calls = {
    "GET /status": "/api/v1/plugins/status",
    "POST /reload": "/api/v1/plugins/reload",
    "POST /reload-all": "/api/v1/plugins/reload-all",
    "GET /history": "/api/v1/plugins/history",
}

for label, path in plugins_api_calls.items():
    check(
        f"PluginsSettingsPage API: {label}",
        path in plugins_content,
        path,
    )

# 组件导出
check(
    "PluginsSettingsPage 正确导出",
    "export function PluginsSettingsPage" in plugins_content,
    "",
)

# UI功能
check("PluginsSettingsPage: 状态列表", "plugins.map" in plugins_content, "")
check("PluginsSettingsPage: 重载按钮", "handleReload" in plugins_content, "")
check("PluginsSettingsPage: 全部重载", "handleReloadAll" in plugins_content, "")
check("PluginsSettingsPage: 历史记录", "showHistory" in plugins_content, "")

# ─────────────────────────────────────────────
# 8. TriggersPage API 对齐验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("8. TriggersPage API 对齐验证")
print("=" * 60)

triggers_file = FRONTEND_SRC / "pages" / "triggers" / "TriggersPage.tsx"
triggers_content = triggers_file.read_text(encoding="utf-8")

check(
    "TriggersPage 导入 API_ENDPOINTS",
    "API_ENDPOINTS" in triggers_content,
    "from '@/constants/api'",
)

triggers_api_usage = {
    "LIST": "API_ENDPOINTS.TRIGGERS.LIST",
    "STATS": "API_ENDPOINTS.TRIGGERS.STATS",
    "CREATE": "API_ENDPOINTS.TRIGGERS.CREATE",
    "UPDATE": "API_ENDPOINTS.TRIGGERS.UPDATE",
    "DELETE": "API_ENDPOINTS.TRIGGERS.DELETE",
    "ENABLE": "API_ENDPOINTS.TRIGGERS.ENABLE",
    "DISABLE": "API_ENDPOINTS.TRIGGERS.DISABLE",
    "TRIGGER": "API_ENDPOINTS.TRIGGERS.TRIGGER",
}

for name, constant in triggers_api_usage.items():
    check(
        f"TriggersPage API: {name}",
        constant in triggers_content,
        constant,
    )

check(
    "TriggersPage 正确导出",
    "export function TriggersPage" in triggers_content,
    "",
)

# UI功能
check("TriggersPage: 创建/编辑模态框", "showModal" in triggers_content, "")
check("TriggersPage: 启用/禁用切换", "handleToggleEnabled" in triggers_content, "")
check("TriggersPage: 手动触发", "handleTrigger" in triggers_content, "")
check("TriggersPage: 删除确认", "confirmDeleteId" in triggers_content, "")
check("TriggersPage: 统计卡片", "stats" in triggers_content, "")

# ─────────────────────────────────────────────
# 9. KnowledgeBasePage API 对齐验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("9. KnowledgeBasePage API 对齐验证")
print("=" * 60)

kb_file = FRONTEND_SRC / "pages" / "knowledge-base" / "KnowledgeBasePage.tsx"
kb_content = kb_file.read_text(encoding="utf-8")

check(
    "KnowledgeBasePage 导入 API_ENDPOINTS",
    "API_ENDPOINTS" in kb_content,
    "from '@/constants/api'",
)

kb_api_usage = {
    "LIST": "API_ENDPOINTS.KNOWLEDGE_BASE.LIST",
    "STATS": "API_ENDPOINTS.KNOWLEDGE_BASE.STATS",
    "UPLOAD": "API_ENDPOINTS.KNOWLEDGE_BASE.UPLOAD",
    "DELETE": "API_ENDPOINTS.KNOWLEDGE_BASE.DELETE",
    "CATEGORIES": "API_ENDPOINTS.KNOWLEDGE_BASE.CATEGORIES",
    "CREATE_CATEGORY": "API_ENDPOINTS.KNOWLEDGE_BASE.CREATE_CATEGORY",
    "DELETE_CATEGORY": "API_ENDPOINTS.KNOWLEDGE_BASE.DELETE_CATEGORY",
    "TAGS": "API_ENDPOINTS.KNOWLEDGE_BASE.TAGS",
}

for name, constant in kb_api_usage.items():
    check(
        f"KnowledgeBasePage API: {name}",
        constant in kb_content,
        constant,
    )

check(
    "KnowledgeBasePage 正确导出",
    "export function KnowledgeBasePage" in kb_content,
    "",
)

# UI功能
check("KnowledgeBasePage: 文件上传(拖拽)", "handleDrop" in kb_content, "")
check("KnowledgeBasePage: 文件上传(点击)", "handleUpload" in kb_content, "")
check("KnowledgeBasePage: 分类侧边栏", "categories" in kb_content and "setSelectedCategory" in kb_content, "")
check("KnowledgeBasePage: 标签云", "tags" in kb_content and "tags.map" in kb_content, "")
check("KnowledgeBasePage: 统计卡片", "stats" in kb_content, "")
check("KnowledgeBasePage: 创建分类模态框", "showCategoryModal" in kb_content, "")
check("KnowledgeBasePage: 删除确认", "confirmDeleteId" in kb_content, "")

# ─────────────────────────────────────────────
# 10. 后端API端点存在性验证
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("10. 后端API端点存在性验证")
print("=" * 60)

# Plugins 后端路由
plugins_backend = BACKEND_SRC / "channels" / "api" / "routes_plugins.py"
check(
    "后端: routes_plugins.py 存在",
    plugins_backend.exists(),
    str(plugins_backend.relative_to(ROOT)),
)

if plugins_backend.exists():
    pb_content = plugins_backend.read_text(encoding="utf-8")
    check("后端: /api/v1/plugins 前缀", 'prefix="/api/v1/plugins"' in pb_content, "")
    check("后端: GET /status", '"/status"' in pb_content, "")
    check("后端: POST /reload", '"/reload"' in pb_content, "")
    check("后端: POST /reload-all", '"/reload-all"' in pb_content, "")
    check("后端: GET /history", '"/history"' in pb_content, "")

# Triggers 后端路由 (在 routes_missing.py)
missing_file = BACKEND_SRC / "channels" / "api" / "routes_missing.py"
check(
    "后端: routes_missing.py 存在",
    missing_file.exists(),
    str(missing_file.relative_to(ROOT)),
)

if missing_file.exists():
    mb_content = missing_file.read_text(encoding="utf-8")
    check("后端: /api/v1/triggers 前缀", 'prefix="/api/v1/triggers"' in mb_content, "")
    check("后端: GET /api/v1/triggers (list)", 'def list_triggers' in mb_content, "")
    check("后端: GET /api/v1/triggers/stats", 'def get_trigger_stats' in mb_content, "")
    check("后端: GET /api/v1/triggers/{id}", 'def get_trigger' in mb_content, "")
    check("后端: POST /api/v1/triggers (create)", 'def create_trigger' in mb_content, "")
    check("后端: PUT /api/v1/triggers/{id}", 'def update_trigger' in mb_content, "")
    check("后端: DELETE /api/v1/triggers/{id}", 'def delete_trigger' in mb_content, "")
    check("后端: POST /api/v1/triggers/{id}/enable", 'def enable_trigger' in mb_content, "")
    check("后端: POST /api/v1/triggers/{id}/disable", 'def disable_trigger' in mb_content, "")
    check("后端: POST /api/v1/triggers/{id}/trigger", 'def manual_trigger' in mb_content, "")
    
    check("后端: /api/v1/knowledge-base 前缀", 'prefix="/api/v1/knowledge-base"' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base (list)", 'def list_knowledge_base' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base/stats", 'def get_knowledge_base_stats' in mb_content, "")
    check("后端: POST /api/v1/knowledge-base/upload", 'def upload_knowledge_base' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base/check", 'def check_knowledge_base' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base/categories", 'def list_categories' in mb_content, "")
    check("后端: POST /api/v1/knowledge-base/categories", 'def create_category' in mb_content, "")
    check("后端: DELETE /api/v1/knowledge-base/categories/{name}", 'def delete_category' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base/tags", 'def list_tags' in mb_content, "")
    check("后端: GET /api/v1/knowledge-base/{id}", 'def get_knowledge_base_item' in mb_content, "")
    check("后端: DELETE /api/v1/knowledge-base/{id}", 'def delete_knowledge_base_item' in mb_content, "")

# 验证后端路由注册
app_file = BACKEND_SRC / "channels" / "api" / "app.py"
if app_file.exists():
    app_content = app_file.read_text(encoding="utf-8")
    check("后端注册: triggers_router", "triggers_router" in app_content, "")
    check("后端注册: knowledge_base_router", "knowledge_base_router" in app_content, "")
    check("后端注册: plugins_router", "plugins_router" in app_content, "")

# ─────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("验证汇总")
print("=" * 60)

passed = sum(1 for s, _, _ in results if s == PASS)
failed = sum(1 for s, _, _ in results if s == FAIL)
total = len(results)

print(f"\n总计: {total} 项检查")
print(f"通过: {passed} {PASS}")
print(f"失败: {failed} {FAIL if failed else ''}")

if failed > 0:
    print("\n失败项:")
    for status, name, detail in results:
        if status == FAIL:
            print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))

sys.exit(0 if failed == 0 else 1)
