/**
 * Widget 注册表
 *
 * 全局单例组件注册表，管理所有可用的 Widget 组件。
 * 核心特性：注册即用，新组件注册后所有渲染场景自动可用。
 *
 * 设计参考：
 * - toolCardRegistry.tsx 的 Map-based 设计模式
 * - composer.tsx 中已有的 WidgetRegistryClass（本模块为其增强替代）
 *
 * @module WidgetRegistry
 */

import type { RenderingSpaceType } from '@/types/schema'
import type { ComponentType } from 'react'

/** Widget 组件属性类型 */
export type WidgetProps = Record<string, unknown>

/** Widget 组件类型定义 */
export type WidgetComponent = ComponentType<WidgetProps>

/** Widget 元数据 */
export interface WidgetMetadata {
  /** 组件显示名称 */
  name: string
  /** 组件描述 */
  description?: string
  /** 支持的渲染空间类型 */
  supportedSpaces: RenderingSpaceType[]
  /** 降级组件类型（当本组件不可用时回退到此组件） */
  fallbackWidget?: string
}

/** Widget 注册条目 */
export interface WidgetEntry {
  /** 组件类型标识（唯一 key） */
  type: string
  /** React 组件 */
  component: WidgetComponent
  /** 组件元数据 */
  metadata: WidgetMetadata
}

/**
 * Widget 注册表
 *
 * 全局单例，管理所有可渲染的 Widget 组件。
 * 支持注册、查询、列表、降级查找等操作。
 *
 * @example
 * ```ts
 * // 注册组件
 * widgetRegistry.register('chart', ChartWidget, {
 *   name: '图表组件',
 *   supportedSpaces: ['chat', 'workspace', 'floating'],
 * })
 *
 * // 获取组件
 * const ChartComponent = widgetRegistry.get('chart')
 *
 * // 检查是否已注册
 * if (widgetRegistry.has('chart')) { ... }
 *
 * // 列出所有已注册组件
 * const entries = widgetRegistry.list()
 * ```
 */
class WidgetRegistry {
  /** 组件注册表：type → WidgetEntry */
  private readonly entries: Map<string, WidgetEntry> = new Map()

  /**
   * 注册一个 Widget 组件
   *
   * 注册后该组件在所有渲染场景中自动可用。
   * 如果同 type 已存在，则覆盖更新。
   *
   * @param type - 组件类型标识（唯一 key）
   * @param component - React 组件
   * @param metadata - 组件元数据
   * @throws 当 type 为空字符串时抛出错误
   */
  register(
    type: string,
    component: WidgetComponent,
    metadata: Omit<WidgetMetadata, 'name'> & { name?: string },
  ): void
  /**
   * 注册一个 Widget 组件（完整元数据）
   *
   * @param type - 组件类型标识
   * @param component - React 组件
   * @param metadata - 完整的组件元数据
   */
  register(
    type: string,
    component: WidgetComponent,
    metadata: WidgetMetadata,
  ): void
  register(
    type: string,
    component: WidgetComponent,
    metadata: Partial<WidgetMetadata> & { supportedSpaces?: RenderingSpaceType[] },
  ): void {
    if (!type || type.trim() === '') {
      throw new Error('Widget type 不能为空')
    }

    const entry: WidgetEntry = {
      type,
      component,
      metadata: {
        name: metadata.name ?? type,
        description: metadata.description,
        supportedSpaces: metadata.supportedSpaces ?? ['chat', 'workspace'],
        fallbackWidget: metadata.fallbackWidget,
      },
    }

    this.entries.set(type, entry)
  }

  /**
   * 获取指定类型的 Widget 组件
   *
   * @param type - 组件类型标识
   * @returns React 组件，如果未注册则返回 undefined
   */
  get(type: string): WidgetComponent | undefined {
    const entry = this.entries.get(type)
    return entry?.component
  }

  /**
   * 获取指定类型的完整注册条目
   *
   * @param type - 组件类型标识
   * @returns Widget 注册条目，如果未注册则返回 undefined
   */
  getEntry(type: string): WidgetEntry | undefined {
    return this.entries.get(type)
  }

  /**
   * 检查指定类型的 Widget 是否已注册
   *
   * @param type - 组件类型标识
   * @returns 是否已注册
   */
  has(type: string): boolean {
    return this.entries.has(type)
  }

  /**
   * 列出所有已注册的 Widget 组件
   *
   * @returns 所有 Widget 注册条目数组
   */
  list(): WidgetEntry[] {
    return Array.from(this.entries.values())
  }

  /**
   * 列出支持指定渲染空间的所有 Widget 组件
   *
   * @param space - 渲染空间类型
   * @returns 匹配的 Widget 注册条目数组
   */
  listBySpace(space: RenderingSpaceType): WidgetEntry[] {
    return this.list().filter((entry) =>
      entry.metadata.supportedSpaces.includes(space),
    )
  }

  /**
   * 取消注册指定类型的 Widget
   *
   * @param type - 组件类型标识
   * @returns 是否成功取消注册（false 表示该组件不存在）
   */
  unregister(type: string): boolean {
    return this.entries.delete(type)
  }

  /**
   * 查找最接近的可用组件（降级机制）
   *
   * 按优先级查找：精确匹配 → metadata.fallbackWidget → 降级映射表
   *
   * @param type - 目标组件类型
   * @returns 可用的 React 组件或 undefined
   */
  findFallback(type: string): WidgetComponent | undefined {
    // 1. 精确匹配
    const direct = this.entries.get(type)
    if (direct) return direct.component

    // 2. metadata 中声明的 fallbackWidget
    if (direct?.metadata.fallbackWidget) {
      const fallback = this.entries.get(direct.metadata.fallbackWidget)
      if (fallback) return fallback.component
    }

    // 3. 降级映射表
    const fallbackMap: Record<string, string[]> = {
      kanban: ['table', 'status_card'],
      editor: ['code_block'],
      terminal: ['code_block'],
      file_tree: ['table'],
      data_grid: ['table'],
      calendar: ['table'],
      log_stream: ['code_block'],
      topology: ['chart'],
      diff: ['code_block'],
      pivot: ['table'],
      dashboard: ['chart'],
      html_preview: ['code_block'],
      image_viewer: ['gallery'],
      tree: ['table'],
    }

    const candidates = fallbackMap[type] ?? ['status_card']
    for (const candidate of candidates) {
      const entry = this.entries.get(candidate)
      if (entry) return entry.component
    }

    return undefined
  }

  /**
   * 获取已注册组件数量
   *
   * @returns 注册的组件总数
   */
  get size(): number {
    return this.entries.size
  }

  /**
   * 清空注册表
   *
   * 移除所有已注册的组件，通常仅用于测试场景。
   */
  clear(): void {
    this.entries.clear()
  }
}

/** Widget 注册表全局单例 */
export const widgetRegistry = new WidgetRegistry()

export default widgetRegistry
