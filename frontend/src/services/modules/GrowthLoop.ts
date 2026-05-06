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
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { RenderingSpaceConfig } from '@/types/schema'

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

  // BUG-FIX-fix_20260505_001: 将 workspace 空间的模块转为 WorkspaceTab
  // 问题根因: Schema 注册完成后，没有任何代码将 workspace 空间的模块转为 WorkspaceTab 并添加到 layoutModeStore
  // 修复方案: 遍历已注册模块，将 rendering.spaces 中 space === 'workspace' 的配置转为 WorkspaceTab
  const modules = schemaRegistry.getEnabled()
  const store = useLayoutModeStore.getState()

  for (const mod of modules) {
    const schema = mod.schema
    if (!schema.rendering?.spaces) continue

    const workspaceSpaces = schema.rendering.spaces.filter(
      (s: RenderingSpaceConfig) => s.space === 'workspace'
    )
    for (const space of workspaceSpaces) {
      const tabId = `${schema.identity.id}::workspace`
      const existingTabs = useLayoutModeStore.getState().workspaceTabs
      if (existingTabs.some(t => t.id === tabId)) continue

      store.addWorkspaceTab({
        id: tabId,
        title: (space.props?.title as string) ?? schema.identity.name,
        icon: schema.identity.icon,
        moduleId: schema.identity.id,
        component: space.widget,
        dataSource: space.dataSource,
        layout: space.layout as Record<string, unknown>,
        isActive: false,
        isPinned: true,
      })
    }
  }

  // 激活第一个 Tab（如果当前没有活跃 Tab）
  const currentTabs = useLayoutModeStore.getState().workspaceTabs
  const hasActive = currentTabs.some(t => t.isActive)
  if (currentTabs.length > 0 && !hasActive) {
    store.setActiveTab(currentTabs[0].id)
  }

  // Step 4: 启动轮询
  moduleManager.startPolling(30000)

  loggers.websocket.info('自生长闭环初始化完成')

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
 *
 * BUG-FIX-fix_20260506_001: 确保完全清理轮询和注册表
 * 问题根因: destroyGrowthLoop 未完全清理所有状态
 * 修复方案: 重置 layoutModeStore 的 workspace/dock 数据，清理 schema 注册表
 */
export function destroyGrowthLoop(): void {
  moduleManager.destroy()
  schemaRegistry.clear()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/**
 * 重新启动自生长闭环
 *
 * 用于登录后重新初始化模块轮询。
 */
export async function restartGrowthLoop(): Promise<void> {
  destroyGrowthLoop()
  await initializeGrowthLoop()
}
