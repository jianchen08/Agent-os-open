/** 自生长闭环集成 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册 */

import { initializeWidgets } from '@/services/schema/registerWidgets'
import { schemaRegistry } from '@/services/schema/registry'
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

  // Step 2: 注册客户端能力
  await registerCapabilities()

  // Step 3: 拉取并注册模块（_syncToLayoutStore 已处理 workspace tab 和 dock 的同步）
  await moduleManager.initialize()

  loggers.websocket.info('自生长闭环初始化完成')

  loggers.websocket.info(`当前已注册 ${schemaRegistry.getEnabled().length} 个模块`)
}

/** 处理 WebSocket 推送的 Schema 更新事件 */
export function handleSchemaUpdate(event: {
  module_id: string
  schema_version: string
  changes: string[]
}): void {
  moduleManager.handleSchemaUpdate(event)
}

/** 销毁自生长闭环（完全清理） 用于登出、认证过期等场景，需要彻底清除所有模块状态。 */
export function destroyGrowthLoop(): void {
  moduleManager.destroy()
  schemaRegistry.clear()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/** 重新启动自生长闭环 原子性替换，避免清空后再拉取导致工作区闪烁 */
export async function restartGrowthLoop(): Promise<void> {
  moduleManager.destroy()
  schemaRegistry.clear()

  initializeWidgets()

  try {
    await registerCapabilities()
    await moduleManager.fetchAndRebuild()
    loggers.websocket.info('自生长闭环重启完成')
    loggers.websocket.info(`当前已注册 ${schemaRegistry.getEnabled().length} 个模块`)
  } catch (error) {
    useLayoutModeStore.setState({ workspaceTabs: [] })
    useLayoutModeStore.getState().setDockItems([])
    throw error
  }
}
