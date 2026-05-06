/**
 * 模块管理器
 *
 * 从 /api/modules/ui 拉取 Schema → 按 category 分类 → 全局注册
 * 实现自生长闭环的核心入口
 */

import { getModuleUISchemas } from '@/services/api/modules'
import { schemaRegistry } from '@/services/schema/registry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { STORAGE_KEYS } from '@/constants/storage'
import { loggers } from '@/utils/logger'
import type { ModuleUISchema, ModuleRegistration } from '@/types/schema'

class ModuleManager {
  private initialized = false
  private pollingTimer: ReturnType<typeof setInterval> | null = null

  /**
   * 初始化模块系统
   */
  async initialize(): Promise<void> {
    if (this.initialized) return

    try {
      await this.fetchAndRegister()
      this.initialized = true
      loggers.websocket.info('模块系统初始化完成')
    } catch (error) {
      loggers.websocket.error('模块系统初始化失败:', error)
    }
  }

  /**
   * 检查当前是否已认证（存在 access_token）
   */
  private isAuthenticated(): boolean {
    return !!localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
  }

  /**
   * 拉取并注册所有模块
   *
   * BUG-FIX-fix_20260505_001: API 响应解包修复
   * 问题根因: getModuleUISchemas() 返回 { items: [...], total: N }，但代码用 Array.isArray() 检查，永远为 false
   * 修复方案: 兼容数组和 { items } 两种响应格式
   *
   * BUG-FIX-fix_20260506_001: 未认证时跳过请求，401 时停止轮询
   * 问题根因: 轮询在未认证时持续请求导致 401 死循环
   * 修复方案: 发送请求前检查认证状态，捕获 401 时自动停止轮询
   */
  async fetchAndRegister(): Promise<void> {
    if (!this.isAuthenticated()) {
      return
    }

    try {
      const response = await getModuleUISchemas()
      const schemas = Array.isArray(response) ? response : (response?.items ?? [])
      if (schemas.length > 0) {
        schemaRegistry.registerAll(schemas, 'api')
        loggers.websocket.info(`已注册 ${schemas.length} 个模块`)
      }

      // BUG-FIX-fix_20260507_002: 无论 schemas 是否为空都同步布局
      // 问题根因: schemas 为空时不同步，导致已注册模块的工作区 tab 丢失
      // 修复方案: 始终调用 _syncToLayoutStore 保持一致性
      this._syncToLayoutStore()
    } catch (error: unknown) {
      // 401 错误时停止轮询，避免死循环
      if (this._isAuthError(error)) {
        loggers.websocket.warn('认证失败，停止模块轮询')
        this.stopPolling()
        return
      }
      loggers.websocket.warn('拉取模块 Schema 失败:', error)
    }
  }

  /**
   * 判断错误是否为认证错误（401）
   */
  private _isAuthError(error: unknown): boolean {
    if (error && typeof error === 'object' && 'code' in error) {
      return (error as { code: string | number }).code === 401 ||
             (error as { code: string | number }).code === '401'
    }
    return false
  }

  /**
   * 将 schemaRegistry 中已注册模块的 workspace/dock 配置同步到 layoutModeStore
   *
   * 从每个模块的 rendering.spaces 中提取 workspace 类型的渲染空间，
   * 转换为 WorkspaceTab 写入 store；
   * 从每个模块的 rendering.dock 中提取 dock 配置，
   * 转换为 DockItem 写入 store。
   *
   * @param fullReplace - 为 true 时全量替换 workspaceTabs（用于重启场景）
   */
  private _syncToLayoutStore(fullReplace = false): void {
    const modules = schemaRegistry.getEnabled()
    const currentState = useLayoutModeStore.getState()

    const existingTabIds = fullReplace
      ? new Set<string>()
      : new Set(currentState.workspaceTabs.map((t) => t.id))
    const existingDockIds = new Set(currentState.dockItems.map((d) => d.id))
    const hasActiveTab = fullReplace ? false : currentState.workspaceTabs.some((t) => t.isActive)

    const newTabs: import('@/types/layout').WorkspaceTab[] = []
    const allDockItems: import('@/types/layout').DockItem[] = fullReplace ? [] : [...currentState.dockItems]

    modules.forEach((mod) => {
      const { identity, rendering } = mod.schema

      const workspaceSpaces = rendering.spaces.filter((s) => s.space === 'workspace')
      workspaceSpaces.forEach((space) => {
        const tabId = `ws-${identity.id}-${space.widget}`
        if (!existingTabIds.has(tabId)) {
          newTabs.push({
            id: tabId,
            title: identity.name || (space.widget as string),
            icon: identity.icon,
            moduleId: identity.id,
            component: space.widget,
            layout: space.layout as Record<string, unknown> | undefined,
            dataSource: space.dataSource,
            isActive: !hasActiveTab && newTabs.length === 0,
            isPinned: false,
          })
          existingTabIds.add(tabId)
        }
      })

      if (rendering.dock) {
        const dockId = `dock-${identity.id}`
        if (!existingDockIds.has(dockId)) {
          allDockItems.push({
            id: dockId,
            moduleId: identity.id,
            icon: rendering.dock.icon || identity.icon || 'Box',
            label: rendering.dock.label || identity.name,
            indicator: rendering.dock.indicator || 'none',
            indicatorColor: rendering.dock.indicatorColor,
            isActive: false,
            onClick: () => {
              const relatedTabId = `ws-${identity.id}`
              const tabs = useLayoutModeStore.getState().workspaceTabs
              const match = tabs.find((t) => t.id.startsWith(relatedTabId))
              if (match) {
                useLayoutModeStore.getState().setActiveTab(match.id)
              }
            },
          })
          existingDockIds.add(dockId)
        }
      }
    })

    if (fullReplace) {
      useLayoutModeStore.setState({ workspaceTabs: newTabs })
    } else if (newTabs.length > 0) {
      useLayoutModeStore.setState((state) => ({
        workspaceTabs: [...state.workspaceTabs, ...newTabs],
      }))
    }

    if (allDockItems.length > 0) {
      useLayoutModeStore.getState().setDockItems(allDockItems)
    }

    loggers.websocket.info(
      `已同步 ${newTabs.length} 个 workspace tabs (${fullReplace ? '全量替换' : '增量追加'}), ${allDockItems.length} 个 dock items`,
    )
  }

  /**
   * 获取所有已注册模块
   */
  getModules(): ModuleRegistration[] {
    return schemaRegistry.getEnabled()
  }

  /**
   * 拉取模块并全量重建布局（用于 restartGrowthLoop 场景）
   *
   * 与 fetchAndRegister 不同，此方法使用 fullReplace 模式，
   * 直接替换所有 workspaceTabs 而非增量追加，
   * 避免先清空再追加导致的闪烁问题。
   */
  async fetchAndRebuild(): Promise<void> {
    if (!this.isAuthenticated()) {
      return
    }

    try {
      const response = await getModuleUISchemas()
      const schemas = Array.isArray(response) ? response : (response?.items ?? [])
      if (schemas.length > 0) {
        schemaRegistry.registerAll(schemas, 'api')
        loggers.websocket.info(`已注册 ${schemas.length} 个模块（全量重建）`)
      }

      this._syncToLayoutStore(true)
    } catch (error: unknown) {
      if (this._isAuthError(error)) {
        loggers.websocket.warn('认证失败，停止模块轮询')
        this.stopPolling()
        return
      }
      loggers.websocket.warn('拉取模块 Schema 失败:', error)
    }
  }

  /**
   * 按分类获取模块
   */
  getModulesByCategory(category: string): ModuleRegistration[] {
    return schemaRegistry.getByCategory(category)
  }

  /**
   * 启动轮询（监听后端模块变更）
   *
   * BUG-FIX-fix_20260506_001: 轮询前检查认证状态
   * 问题根因: 未认证时轮询持续发送请求导致 401 死循环
   * 修复方案: 每次轮询 tick 先检查认证状态，未认证则跳过
   */
  startPolling(interval = 30000): void {
    this.stopPolling()
    this.pollingTimer = setInterval(() => {
      if (!this.isAuthenticated()) {
        return
      }
      this.fetchAndRegister().catch(() => {})
    }, interval)
  }

  /**
   * 停止轮询
   */
  stopPolling(): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer)
      this.pollingTimer = null
    }
  }

  /**
   * 处理 Schema 更新推送（WebSocket 推送触发）
   */
  handleSchemaUpdate(event: {
    module_id: string
    schema_version: string
    changes: string[]
  }): void {
    loggers.websocket.info(`模块 ${event.module_id} Schema 更新: v${event.schema_version}`)
    this.fetchAndRegister().catch(() => {})
  }

  /**
   * 销毁
   */
  destroy(): void {
    this.stopPolling()
    schemaRegistry.clear()
    this.initialized = false
  }
}

export const moduleManager = new ModuleManager()
