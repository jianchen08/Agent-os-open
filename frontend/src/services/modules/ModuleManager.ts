/**
 * 模块管理器
 *
 * 从 /api/modules/ui 拉取 Schema → 按 category 分类 → 全局注册
 * 实现自生长闭环的核心入口
 */

import { schemaRegistry } from '@/services/schema/registry'
import { getModuleUISchemas } from '@/services/api/modules'
import type { ModuleUISchema, ModuleRegistration } from '@/types/schema'
import { loggers } from '@/utils/logger'

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
   * 拉取并注册所有模块
   */
  async fetchAndRegister(): Promise<void> {
    try {
      const schemas = await getModuleUISchemas()
      if (Array.isArray(schemas)) {
        schemaRegistry.registerAll(schemas, 'api')
        loggers.websocket.info(`已注册 ${schemas.length} 个模块`)
      }
    } catch (error) {
      loggers.websocket.warn('拉取模块 Schema 失败:', error)
    }
  }

  /**
   * 获取所有已注册模块
   */
  getModules(): ModuleRegistration[] {
    return schemaRegistry.getEnabled()
  }

  /**
   * 按分类获取模块
   */
  getModulesByCategory(category: string): ModuleRegistration[] {
    return schemaRegistry.getByCategory(category)
  }

  /**
   * 启动轮询（监听后端模块变更）
   */
  startPolling(interval = 30000): void {
    this.stopPolling()
    this.pollingTimer = setInterval(() => {
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
  handleSchemaUpdate(event: { module_id: string; schema_version: string; changes: string[] }): void {
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
