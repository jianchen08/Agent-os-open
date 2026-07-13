/**
 * Schema 注册表
 *
 * 管理所有已注册模块的 UI Schema
 * 替代旧版的 toolCardRegistry
 */

import { parseSchema, validateSchema, type ParsedSchema } from './parser'
import type { ModuleUISchema, ModuleRegistration, ChatInteractionType } from '@/types/schema'

class SchemaRegistry {
  private modules: Map<string, ModuleRegistration> = new Map()
  private listeners: Set<() => void> = new Set()

  /**
   * 注册模块 Schema
   *
   * @param schema - 模块 UI Schema
   * @param source - 注册来源（api/local/push）
   */
  register(schema: ModuleUISchema, source: ModuleRegistration['source'] = 'api'): void {
    if (!validateSchema(schema)) {
      console.warn('无效的 Schema，跳过注册:', schema.identity?.id)
      return
    }

    const existing = this.modules.get(schema.identity.id)
    this.modules.set(schema.identity.id, {
      schema,
      registeredAt: Date.now(),
      enabled: existing?.enabled ?? true,
      source,
    })

    this.notifyListeners()
  }

  /**
   * 批量注册模块
   *
   * @param schemas - 模块 Schema 数组
   * @param source - 注册来源
   */
  registerAll(schemas: ModuleUISchema[], source: ModuleRegistration['source'] = 'api'): void {
    schemas.forEach((s) => this.register(s, source))
  }

  /**
   * 取消注册模块
   *
   * @param moduleId - 模块 ID
   */
  unregister(moduleId: string): void {
    this.modules.delete(moduleId)
    this.notifyListeners()
  }

  /**
   * 获取所有已注册模块
   *
   * @returns 模块注册信息数组
   */
  getAll(): ModuleRegistration[] {
    return Array.from(this.modules.values())
  }

  /**
   * 获取已启用的模块
   *
   * @returns 已启用的模块注册信息数组
   */
  getEnabled(): ModuleRegistration[] {
    return this.getAll().filter((m) => m.enabled)
  }

  /**
   * 按分类获取模块
   *
   * @param category - 模块分类
   * @returns 匹配分类的模块注册信息数组
   */
  getByCategory(category: string): ModuleRegistration[] {
    return this.getAll().filter((m) => m.schema.identity.category === category)
  }

  /**
   * 获取指定模块
   *
   * @param moduleId - 模块 ID
   * @returns 模块注册信息或 undefined
   */
  get(moduleId: string): ModuleRegistration | undefined {
    return this.modules.get(moduleId)
  }

  /**
   * 获取指定模块的解析后 Schema
   *
   * @param moduleId - 模块 ID
   * @returns 解析后的 Schema 或 undefined
   */
  getParsed(moduleId: string): ParsedSchema | undefined {
    const reg = this.modules.get(moduleId)
    if (!reg) return undefined
    return parseSchema(reg.schema)
  }

  /**
   * 获取所有聊天交互组件类型
   *
   * @returns 去重后的聊天交互类型数组
   */
  getChatWidgets(): ChatInteractionType[] {
    const types = new Set<ChatInteractionType>()
    this.getEnabled().forEach((m) => {
      m.schema.rendering.chat.forEach((c) => types.add(c.type))
    })
    return Array.from(types)
  }

  /**
   * 订阅变更
   *
   * @param listener - 变更回调函数
   * @returns 取消订阅的函数
   */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /**
   * 清空注册表
   */
  clear(): void {
    this.modules.clear()
    this.notifyListeners()
  }

  /**
   * 通知所有监听器
   */
  private notifyListeners(): void {
    this.listeners.forEach((l) => {
      try {
        l()
      } catch {
        /* 忽略监听器错误 */
      }
    })
  }
}

/** Schema 注册表单例 */
export const schemaRegistry = new SchemaRegistry()
