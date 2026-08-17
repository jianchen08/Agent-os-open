/** 自生长闭环集成 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册 */

import { syncNavItemsFromContributes } from '@/constants/navItems'
import apiClient from '@/services/api/client'
import { getSchema } from '@/services/api/schema'
import { syncPluginStyles, removeAllPluginStyles } from '@/services/pluginStyles'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { shortcutRegistry } from '@/services/schema/shortcutRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useThemeStore } from '@/stores/themeStore'
import { loggers } from '@/utils/logger'
import { loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { disposeResyncOnSchema, initResyncOnSchema } from '@/services/websocket/resync'
import { loadDshAdapterContributions } from '@/services/dshAdapter'
import { loadRenderIntents } from '@/utils/dshRenderIntent'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'

/** 初始化自生长闭环 1. 注册所有预置组件 */
export async function initializeGrowthLoop(): Promise<void> {
  loggers.websocket.info('正在初始化自生长闭环...')

  // Step 0: 注入命令内核 transport（命令面板/快捷键/菜单 → 内核 capability 出口）
  commandDispatcher.setTransport(async (commandId, args) => {
    await apiClient.post('/api/v1/actions/execute', { action: commandId, args })
  })

  // Step 1: 注册预置组件
  initializeWidgets()
  loggers.websocket.info('预置组件注册完成')

  // Step 2: 加载 schema 到 ContributionRegistry 并同步导航（0.2 核心数据源）
  await reloadContributionRegistry()

  // Step 3: 订阅 resync_required（内核重启/回放缓冲 miss 时的被动全量重同步通道）
  initResyncOnSchema()

  loggers.websocket.info('自生长闭环初始化完成')
}

/**
 * 重新拉取 schema 并刷新 ContributionRegistry + 导航 + 快捷键 + 插件主题/CSS。
 *
 * 集中了 contributes 数据的加载逻辑，供初始化、重启、schema_updated 事件复用。
 * 失败仅 warn 不抛出（contributes 加载失败不应阻塞主流程）。
 */
async function reloadContributionRegistry(): Promise<void> {
  try {
    const schema = await getSchema()
    contributionRegistry.loadFromSchema(schema as unknown as Record<string, unknown>)
    // 工具卡片声明（chat_card）从 tools[].ui.chat_card 装载到解释器注册表
    loadChatCardDeclarations(
      (schema as { tools?: Array<{ name?: string; ui?: { chat_card?: ChatCardDeclaration } }> }).tools ?? [],
    )
    // render 意图声明：tools[].render 装载到 dshRenderIntent 注册表（声明路由），
    // 无声明时工具结果按数据形状自动路由（数据路由），均未命中落通用数据渲染
    loadRenderIntents(
      (schema as { tools?: Array<{ name?: string; render?: Record<string, unknown> }> }).tools ?? [],
    )
    syncNavItemsFromContributes()
    shortcutRegistry.refresh()

    // DSH 适配器贡献（task_dsh_plugin_adapter 任务 2）：renderers 兜底注册 +
    // 来源版本记录。失败隔离在服务内部（不影响本函数其余步骤）。
    await loadDshAdapterContributions()

    // 插件视觉贡献同步（contributes.themes / client_styles）：
    // - 主题：插件主题合入主题列表；当前用的插件主题被移除（插件禁用）→ 回退 base
    // - CSS：以注册表为权威注入新样式 / 移除失效样式（禁用插件无残留）
    useThemeStore.getState().syncPluginThemes()
    syncPluginStyles(contributionRegistry.getClientStyles())
  } catch (error) {
    loggers.websocket.warn('ContributionRegistry 加载失败:', error)
  }
}

/** 处理 WebSocket 推送的 Schema 更新事件（含插件热重载后的 contributes 变更） */
export function handleSchemaUpdate(_event: {
  module_id: string
  schema_version: string
  changes: string[]
}): void {
  // 重新拉取 schema 刷新 ContributionRegistry（contributes 可能已变）
  void reloadContributionRegistry()
}

/**
 * 主动刷新插件贡献（插件启用/禁用切换后调用，无需 WS 事件）
 *
 * 插件禁用语义：contributes 不再导出 → schema 重载后其主题从列表移除、
 * 注入 CSS 被清理；当前在用其主题时回退 base（syncPluginThemes 内处理）。
 */
export function refreshPluginContributions(): Promise<void> {
  return reloadContributionRegistry()
}

/** 销毁自生长闭环（完全清理） 用于登出、认证过期等场景，需要彻底清除所有模块状态。 */
export function destroyGrowthLoop(): void {
  // 先注销 resync 订阅并取消未触发的防抖（globalWS.disconnect 也会清 handler，双保险）
  disposeResyncOnSchema()
  contributionRegistry.clear()
  // 插件注入样式随闭环销毁清除（防跨会话残留）
  removeAllPluginStyles()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/** 重新启动自生长闭环 原子性替换，避免清空后再拉取导致工作区闪烁 */
export async function restartGrowthLoop(): Promise<void> {
  contributionRegistry.clear()

  initializeWidgets()

  // 补挂 resync_required 订阅（登出 disconnect 会清空全部 handler，幂等可重复调用）
  initResyncOnSchema()

  try {
    // 重新加载 schema 到 ContributionRegistry（0.2 核心数据源）
    await reloadContributionRegistry()
    loggers.websocket.info('自生长闭环重启完成')
  } catch (error) {
    useLayoutModeStore.setState({ workspaceTabs: [] })
    useLayoutModeStore.getState().setDockItems([])
    throw error
  }
}
