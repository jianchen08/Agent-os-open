/**
 * Schema 路由表
 *
 * widget_type → 渲染空间的映射，支持动态扩展。
 * 第三方插件注册自定义 Widget 后，路由表自动更新。
 *
 * 核心职责：
 * 1. 维护默认路由映射（内置 Widget 类型 → 渲染空间）
 * 2. 接受插件 ui_contributions 的动态注册
 * 3. resolve(widgetType) 返回目标渲染空间
 *
 * @module SchemaRouter
 */

import type { RenderingSpaceType, UIContribution } from '@/types/schema'

/**
 * Schema 路由表
 *
 * 管理 widget_type → render_space 的映射关系。
 * 默认路由表覆盖内置 Widget 类型，插件通过 register/registerFromContributions 扩展。
 */
export class SchemaRouter {
  /** 自定义路由覆盖表：widget_type → render_space */
  private readonly customRoutes: Map<string, RenderingSpaceType> = new Map()

  /**
   * 默认路由表
   *
   * 基于任务文档中的设计：
   * - review_document → workspace（文档审阅）
   * - image_viewer → chat（图片查看）
   * - floating_assistant → floating（悬浮助手）
   * - custom_tool_panel → dock（自定义工具面板）
   * - digital_human → workspace（数字人/形象是 workspace 的 widget，不占独立空间；ADR §2.1 / §7.6）
   */
  private readonly defaultRoutes: Map<string, RenderingSpaceType> = new Map([
    ['review_document', 'workspace'],
    ['image_viewer', 'chat'],
    ['floating_assistant', 'floating'],
    ['custom_tool_panel', 'dock'],
    ['digital_human', 'workspace'],
    // 通用 Widget 默认路由
    ['form', 'chat'],
    ['chart', 'chat'],
    ['table', 'workspace'],
    ['gallery', 'chat'],
    ['code_block', 'chat'],
    ['status_card', 'chat'],
    ['progress', 'chat'],
    ['decision', 'chat'],
    ['file_tree', 'workspace'],
    ['html_preview', 'workspace'],
  ])

  /**
   * 解析 widget_type 到渲染空间
   *
   * 查找优先级：自定义路由 > 默认路由 > fallback('chat')
   *
   * @param widgetType - Widget 类型标识
   * @returns 目标渲染空间（未找到时返回 'chat'）
   */
  resolve(widgetType: string): RenderingSpaceType {
    return this.customRoutes.get(widgetType)
      ?? this.defaultRoutes.get(widgetType)
      ?? 'chat'
  }

  /**
   * 注册自定义 widget_type → render_space 映射
   *
   * 注册后覆盖同名的默认路由。
   *
   * @param widgetType - Widget 类型标识
   * @param renderSpace - 目标渲染空间
   */
  register(widgetType: string, renderSpace: RenderingSpaceType): void {
    this.customRoutes.set(widgetType, renderSpace)
  }

  /**
   * 批量注册路由
   *
   * @param routes - 路由映射对象
   */
  registerAll(routes: Record<string, RenderingSpaceType>): void {
    for (const [widgetType, renderSpace] of Object.entries(routes)) {
      this.register(widgetType, renderSpace)
    }
  }

  /**
   * 从插件 ui_contributions 列表批量注册路由
   *
   * 插件声明的 renderSpace 优先于默认路由。
   *
   * @param contributions - UI 贡献项列表
   */
  registerFromContributions(contributions: UIContribution[]): void {
    for (const contrib of contributions) {
      this.register(contrib.widgetType, contrib.renderSpace)
    }
  }

  /**
   * 列出所有路由（默认 + 自定义）
   *
   * @returns 路由映射的只读副本
   */
  listRoutes(): Map<string, RenderingSpaceType> {
    const result = new Map(this.defaultRoutes)
    for (const [key, value] of this.customRoutes) {
      result.set(key, value)
    }
    return result
  }

  /**
   * 获取指定渲染空间的所有 widget_type
   *
   * @param space - 渲染空间
   * @returns 匹配的 widget_type 列表
   */
  getRoutesForSpace(space: RenderingSpaceType): string[] {
    const all = this.listRoutes()
    return Array.from(all.entries())
      .filter(([, s]) => s === space)
      .map(([type]) => type)
  }

  /**
   * 清空自定义路由（不影响默认路由）
   */
  clearCustom(): void {
    this.customRoutes.clear()
  }
}

/** Schema 路由表全局单例 */
export const schemaRouter = new SchemaRouter()

export default schemaRouter
