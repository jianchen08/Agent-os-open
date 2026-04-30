/**
 * 自生长闭环集成
 *
 * 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册
 * 实现完整的自生长闭环：Schema 变更时 WebSocket 推送 → 前端自动更新渲染
 */

import { initializeWidgets } from '@/services/schema/registerWidgets'
import { schemaRegistry } from '@/services/schema/registry'
import { loggers } from '@/utils/logger'
import { registerCapabilities } from './ClientCapabilities'
import { moduleManager } from './ModuleManager'

/**
 * 初始化自生长闭环
 *
 * 1. 注册所有预置组件
 * 2. 注册客户端能力
 * 3. 拉取并注册模块 Schema
 * 4. 启动轮询监听变更
 */
export async function initializeGrowthLoop(): Promise<void> {
  loggers.websocket.info('正在初始化自生长闭环...')

  // Step 1: 注册预置组件
  initializeWidgets()
  loggers.websocket.info('预置组件注册完成')

  // Step 2: 注册客户端能力
  await registerCapabilities()

  // Step 3: 拉取并注册模块
  await moduleManager.initialize()

  // Step 4: 启动轮询
  moduleManager.startPolling(30000)

  loggers.websocket.info('自生长闭环初始化完成')

  // 输出当前状态
  const modules = schemaRegistry.getEnabled()
  loggers.websocket.info(`当前已注册 ${modules.length} 个模块`)
}

/**
 * 处理 WebSocket 推送的 Schema 更新事件
 */
export function handleSchemaUpdate(event: {
  module_id: string
  schema_version: string
  changes: string[]
}): void {
  moduleManager.handleSchemaUpdate(event)
}

/**
 * 销毁自生长闭环
 */
export function destroyGrowthLoop(): void {
  moduleManager.destroy()
}
