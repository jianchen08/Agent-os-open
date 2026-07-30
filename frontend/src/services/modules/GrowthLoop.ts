/** 自生长闭环集成 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册 */

import { syncNavItemsFromContributes } from '@/constants/navItems'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { schemaRegistry } from '@/services/schema/registry'
import { shortcutRegistry } from '@/services/schema/shortcutRegistry'
import { getSchema } from '@/services/api/schema'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { loggers } from '@/utils/logger'
import { registerCapabilities } from './ClientCapabilities'
import { moduleManager } from './ModuleManager'

/** 初始化自生长闭环 1. 注册所有预置组件 */
export async function initializeGrowthLoop(): Promise<void> {
  loggers.websocket.info('正在初始化自生长闭环...')

  // Step 1: 注册预置组件
  initializeWidgets()
  loggers.websocket.info('预置组件注册完成')

  // Step 2: 注册客户端能力（0.1 遗留端点，0.2 内核未实现，失败静默不阻塞）
  try {
    await registerCapabilities()
  } catch {
    loggers.websocket.debug('registerCapabilities 跳过（0.2 内核无此端点）')
  }

  // Step 3: 拉取并注册模块（0.1 遗留 /api/modules/ui 端点，0.2 未实现，失败静默）
  try {
    await moduleManager.initialize()
  } catch {
    loggers.websocket.debug('moduleManager.initialize 跳过（0.2 内核无 /api/modules/ui）')
  }

  // Step 4: 加载 schema 到 ContributionRegistry 并同步导航（0.2 核心数据源）
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
  moduleManager.handleSchemaUpdate(event)
  // 重新拉取 schema 刷新 ContributionRegistry（contributes 可能已变）
  void reloadContributionRegistry()
}

/** 销毁自生长闭环（完全清理） 用于登出、认证过期等场景，需要彻底清除所有模块状态。 */
export function destroyGrowthLoop(): void {
  moduleManager.destroy()
  schemaRegistry.clear()
  contributionRegistry.clear()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/** 重新启动自生长闭环 原子性替换，避免清空后再拉取导致工作区闪烁 */
export async function restartGrowthLoop(): Promise<void> {
  moduleManager.destroy()
  schemaRegistry.clear()
  contributionRegistry.clear()

  initializeWidgets()

  // registerCapabilities + moduleManager（0.1 遗留端点，失败静默不阻塞核心）
  try {
    await registerCapabilities()
  } catch {
    loggers.websocket.debug('registerCapabilities 跳过（重启场景）')
  }
  try {
    await moduleManager.fetchAndRebuild()
  } catch {
    loggers.websocket.debug('moduleManager.fetchAndRebuild 跳过（0.2 无 /api/modules/ui）')
  }

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
