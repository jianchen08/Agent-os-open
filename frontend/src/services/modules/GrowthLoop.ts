/** 自生长闭环集成 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册 */

import { syncNavItemsFromContributes } from '@/constants/navItems'
import apiClient from '@/services/api/client'
import { getSchema } from '@/services/api/schema'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { shortcutRegistry } from '@/services/schema/shortcutRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { registerBuiltinToolChatCards } from '@/utils/builtinToolChatCards'
import { loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'
import { loggers } from '@/utils/logger'

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

  loggers.websocket.info('自生长闭环初始化完成')
}

/**
 * 重新拉取 schema 并刷新 ContributionRegistry + 导航 + 快捷键。
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
    // 内置工具（file_read/bash_execute/web_search/fetch/task_submit）的 chat_card 声明
    // 追加在 schema 声明之上：schema 热重载（load 会清空全表）后 builtin 依然生效并优先
    registerBuiltinToolChatCards()
    syncNavItemsFromContributes()
    shortcutRegistry.refresh()
  } catch (error) {
    loggers.websocket.warn('ContributionRegistry 加载失败:', error)
  }
}

/** 处理 WebSocket 推送的 Schema 更新事件（含插件热重载后的 contributes 变更） */
export function handleSchemaUpdate(event: {
  module_id: string
  schema_version: string
  changes: string[]
}): void {
  // 重新拉取 schema 刷新 ContributionRegistry（contributes 可能已变）
  void reloadContributionRegistry()
}

/** 销毁自生长闭环（完全清理） 用于登出、认证过期等场景，需要彻底清除所有模块状态。 */
export function destroyGrowthLoop(): void {
  contributionRegistry.clear()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/** 重新启动自生长闭环 原子性替换，避免清空后再拉取导致工作区闪烁 */
export async function restartGrowthLoop(): Promise<void> {
  contributionRegistry.clear()

  initializeWidgets()

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
