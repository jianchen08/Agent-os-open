/**
 * 模块管理器
 *
 * 从 /api/modules/ui 拉取 Schema → 按 category 分类 → 全局注册
 * 实现自生长闭环的核心入口
 */

import { STORAGE_KEYS } from '@/constants/storage'
import { getModuleUISchemas } from '@/services/api/modules'
import { schemaRegistry } from '@/services/schema/registry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { loggers } from '@/utils/logger'
import type { DockItem, WorkspaceTab } from '@/types/layout'
import type { ModuleUISchema, ModuleRegistration } from '@/types/schema'

class ModuleManager {
  private initialized = false
  private pollingTimer: ReturnType<typeof setInterval> | null = null
  /**
   * 正在进行的拉取请求的 Promise（用于并发去重）。
   *
   * BUG-FIX-fix_20260621_workspace_empty_no_retry:
   * 原实现用 `_fetching: boolean` 守卫，并发调用者直接 return 且不等待结果，
   * 导致 initialize() 的重试与 syncOnReconnect() 的并发调用拿不到最新结果。
   * 改为复用同一个 in-flight Promise，所有调用者共享同一次拉取结果。
   */
  private _fetchPromise: Promise<void> | null = null

  /**
   * 初始化模块系统（带失败重试）
   *
   * BUG-FIX-fix_20260621_workspace_empty_no_retry:
   * 问题根因: 初始化失败（网络抖动、/api/v1/modules/ui 暂时不可用）时仅 console.error，
   *          不重试。此时 workspaceTabs 永远为空，工作区持续显示"工作区为空 — 模块激活后自动出现"，
   *          直到后端推送 SCHEMA_UPDATED 事件或用户重新登录才会恢复。
   * 修复方案: 初始化失败时按指数退避重试若干次；并在 finally 里兜底调用
   *          ensureWorkspaceSynced() —— 若注册表已有模块（来自缓存/前次成功）但 tabs 缺失，
   *          也强制同步一次，避免"有数据无 tab"的不一致。
   * 影响范围: 页面刷新后工作区面板首次渲染的可用性
   * 修复日期: 2026-06-21
   */
  async initialize(): Promise<void> {
    if (this.initialized) return

    const maxRetries = 3
    let lastError: unknown = null
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        // 用会抛错的 _doFetchAndRegister，使重试逻辑能感知失败
        await this._doFetchAndRegister()
        this.initialized = true
        loggers.websocket.info('模块系统初始化完成')
        return
      } catch (error) {
        lastError = error
        // 401 不重试（_doFetchAndRegister 内部已 stopPolling 并正常返回，
        // 此处 catch 收到的 401 来自 stopPolling 之后的 rethrow 路径之外的异常）
        if (this._isAuthError(error)) {
          break
        }
        if (attempt < maxRetries) {
          const backoffMs = 500 * Math.pow(2, attempt - 1)
          loggers.websocket.warn(
            `模块系统初始化失败（第 ${attempt}/${maxRetries} 次），${backoffMs}ms 后重试:`,
            error,
          )
          await new Promise((r) => setTimeout(r, backoffMs))
        }
      }
    }
    loggers.websocket.error('模块系统初始化失败（已用尽重试）:', lastError)
    // 兜底：即便拉取失败，若注册表已有模块（来自缓存/前次成功），也尝试同步一次 tabs，
    // 避免工作区持续显示"工作区为空"
    this.ensureWorkspaceSynced()
  }

  /**
   * 兜底同步：当注册表中存在模块但 workspaceTabs 为空时，强制重建 tabs。
   *
   * 用于初始化失败、WS 重连等场景，确保"有模块 schema 但工作区 tab 缺失"的不一致
   * 能被自动修复。重复调用安全（_syncToLayoutStore 的增量模式会跳过已存在的 tab）。
   */
  ensureWorkspaceSynced(): void {
    const modules = schemaRegistry.getEnabled()
    const hasWorkspaceModule = modules.some((m) =>
      m.schema.rendering.spaces.some((s) => s.space === 'workspace'),
    )
    const hasWorkspaceTab = useLayoutModeStore
      .getState()
      .workspaceTabs.some((t) => t.moduleId && t.moduleId !== '__file_editor__')

    if (hasWorkspaceModule && !hasWorkspaceTab) {
      loggers.websocket.info('检测到模块已注册但工作区 tab 缺失，触发兜底同步')
      this._syncToLayoutStore()
    }
  }

  /**
   * WS 重连后重新同步模块（公开入口）。
   *
   * BUG-FIX-fix_20260621_workspace_empty_no_retry:
   * 问题根因: WS 重连（useRealtimeEvents 的 'reconnected' 事件）只补消息，不重新同步模块，
   *          导致初始化时因网络问题未创建的 workspace tabs 不会在网络恢复后自动出现。
   * 修复方案: WS 重连时若工作区 tab 缺失，重新拉取并同步模块。
   */
  async syncOnReconnect(): Promise<void> {
    if (!this.isAuthenticated()) return
    const hasWorkspaceTab = useLayoutModeStore
      .getState()
      .workspaceTabs.some((t) => t.moduleId && t.moduleId !== '__file_editor__')
    if (!hasWorkspaceTab) {
      loggers.websocket.info('WS 重连后工作区 tab 缺失，重新拉取模块')
      try {
        await this._doFetchAndRegister()
      } catch (error) {
        loggers.websocket.warn('WS 重连后重新拉取模块失败，尝试兜底同步:', error)
        this.ensureWorkspaceSynced()
      }
    }
  }

  /**
   * 检查当前是否已认证（存在 access_token）
   */
  private isAuthenticated(): boolean {
    return !!localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
  }

  /**
   * 拉取并注册所有模块（容错版，用于轮询/WS 推送）。
   *
   * 内部调用 doFetchAndRegister()，吞掉非 401 异常（仅 warn），保证轮询/推送路径
   * 不会因单次失败而中断。需要感知失败并重试的调用方（initialize/syncOnReconnect）
   * 应直接调用 doFetchAndRegister()。
   *
   * BUG-FIX-fix_20260505_001: API 响应解包修复
   * 问题根因: getModuleUISchemas() 返回 { items: [...], total: N }，但代码用 Array.isArray() 检查，永远为 false
   * 修复方案: 兼容数组和 { items } 两种响应格式
   *
   * BUG-FIX-fix_20260506_001: 未认证时跳过请求，401 时停止轮询
   * 问题根因: 轮询在未认证时持续请求导致 401 死循环
   * 修复方案: 发送请求前检查认证状态，捕获 401 时自动停止轮询
   *
   * BUG-FIX-fix_20260621_workspace_empty_no_retry: 并发去重
   * 问题根因: 原 `_fetching: boolean` 守卫让并发调用者直接 return，丢失结果。
   * 修复方案: 复用 in-flight Promise，并发调用者共享同一次拉取结果。
   */
  async fetchAndRegister(): Promise<void> {
    try {
      await this._doFetchAndRegister()
    } catch (error: unknown) {
      if (this._isAuthError(error)) {
        return
      }
      loggers.websocket.warn('拉取模块 Schema 失败:', error)
    }
  }

  /**
   * 拉取并注册所有模块（会抛错版）。
   *
   * 401 错误时 stopPolling 并正常返回（认证问题不重试）；
   * 其他错误时 rethrow，供 initialize()/syncOnReconnect() 重试。
   * 使用 in-flight Promise 去重，并发调用共享同一次拉取。
   */
  private async _doFetchAndRegister(): Promise<void> {
    if (!this.isAuthenticated()) {
      return
    }
    if (this._fetchPromise) {
      return this._fetchPromise
    }

    this._fetchPromise = (async () => {
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
        throw error
      }
    })()

    try {
      await this._fetchPromise
    } finally {
      this._fetchPromise = null
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

    const newTabs: WorkspaceTab[] = []
    const allDockItems: DockItem[] = fullReplace ? [] : [...currentState.dockItems]

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

    const currentDockItems = useLayoutModeStore.getState().dockItems
    const dockChanged = allDockItems.length !== currentDockItems.length ||
      allDockItems.some((item, i) => item.id !== currentDockItems[i]?.id)
    if (dockChanged && allDockItems.length > 0) {
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
   *
   * 401 错误时 stopPolling 并正常返回；其他错误 rethrow，
   * 让 restartGrowthLoop 的 catch 能感知失败并清理状态。
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
      throw error
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
  startPolling(interval = 300000): void {
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
