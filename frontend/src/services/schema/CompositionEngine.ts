/**
 * Schema 组合引擎
 *
 * 解析工作区渲染配置中的组合声明，支持两种模式：
 * - 单体模式：直接 component 引用
 * - 组合模式：layout + children 递归组合
 *
 * 三要素：layout（布局模板）+ component（原子组件）+ data_source（数据绑定）
 *
 * @module CompositionEngine
 */

import { parseDataSourceRef, resolveDataSource } from '@/services/schema/parser'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { DataSourceRef, ResolvedDataSource } from '@/types/schema'
import type { ComponentType } from 'react'

/** 支持的布局类型 */
export type LayoutType =
  | 'split-horizontal'
  | 'split-vertical'
  | 'tabs'
  | 'grid'
  | 'stack'

/** 组合节点定义 */
export interface CompositionNode {
  /** 布局类型（组合模式时存在） */
  layout?: LayoutType
  /** 子节点（组合模式时存在） */
  children?: CompositionNode[]
  /** 组件类型（单体模式时存在） */
  component?: string
  /** 组件属性 */
  props?: Record<string, unknown>
  /** 数据源引用（module://collection 格式） */
  data_source?: string
  /** 分割比例 */
  ratio?: number[]
  /** 默认激活标签索引 */
  default_tab?: number
  /** 网格列数 */
  columns?: number
  /** 标签页标题 */
  title?: string
  /** 标签页图标 */
  icon?: string
  /** 轮询间隔（毫秒） */
  polling?: number
}

/** 已解析的组件引用 */
export interface ResolvedComponent {
  /** 组件类型标识 */
  type: string
  /** React 组件（可能为 null 表示未找到） */
  component: ComponentType<Record<string, unknown>> | null
  /** 传递给组件的属性 */
  props: Record<string, unknown>
  /** 已解析的数据源 */
  resolvedDataSource?: ResolvedDataSource
  /** 轮询间隔 */
  polling?: number
}

/** 组合模式类型 */
export type CompositionMode = 'single' | 'composite'

/** 已解析的组合节点 */
export interface ResolvedNode {
  /** 组合模式 */
  mode: CompositionMode
  /** 已解析的组件引用（单体模式） */
  component?: ResolvedComponent
  /** 布局类型（组合模式） */
  layout?: LayoutType
  /** 已解析的子节点（组合模式） */
  children?: ResolvedNode[]
  /** 布局参数 */
  layoutProps?: {
    ratio?: number[]
    defaultTab?: number
    columns?: number
  }
  /** 标签页元数据 */
  tabMeta?: {
    title?: string
    icon?: string
  }
}

/**
 * Schema 组合引擎
 *
 * 将 CompositionNode 树解析为 ResolvedNode 树，实现三要素组合：
 * 1. layout - 选择布局模板
 * 2. component - 从 widgetRegistry 解析组件
 * 3. data_source - 解析数据绑定引用
 *
 * @example
 * ```ts
 * const engine = new CompositionEngine()
 * const resolved = engine.resolve(compositionNode)
 * // 将 resolved 传递给 CompositionRenderer 渲染
 * ```
 */
export class CompositionEngine {
  /**
   * 解析组合节点树
   *
   * 递归遍历 CompositionNode，解析组件引用和数据源绑定，
   * 生成可用于渲染的 ResolvedNode 树。
   *
   * @param node - 原始组合节点
   * @returns 已解析的组合节点
   */
  resolve(node: CompositionNode): ResolvedNode {
    if (!node) {
      return { mode: 'single' }
    }

    // 组合模式：有 children 和 layout
    if (node.children && node.children.length > 0 && node.layout) {
      return {
        mode: 'composite',
        layout: node.layout,
        children: node.children.map((child) => this.resolve(child)),
        layoutProps: {
          ratio: node.ratio,
          defaultTab: node.default_tab,
          columns: node.columns,
        },
        tabMeta: {
          title: node.title,
          icon: node.icon,
        },
      }
    }

    // 单体模式：直接组件引用
    if (node.component) {
      const resolvedComponent = this.resolveComponent(
        node.component,
        node.props ?? {},
        node.data_source,
      )

      return {
        mode: 'single',
        component: resolvedComponent,
        tabMeta: {
          title: node.title,
          icon: node.icon,
        },
      }
    }

    // 空节点
    return { mode: 'single' }
  }

  /**
   * 批量解析多个组合节点
   *
   * @param nodes - 原始组合节点数组
   * @returns 已解析的组合节点数组
   */
  resolveAll(nodes: CompositionNode[]): ResolvedNode[] {
    return nodes.map((node) => this.resolve(node))
  }

  /**
   * 解析组件引用
   *
   * 从 widgetRegistry 查找组件（支持降级），解析数据源绑定。
   *
   * @param componentType - 组件类型标识
   * @param props - 组件属性
   * @param dataSourceRef - 数据源引用字符串
   * @returns 已解析的组件引用
   */
  private resolveComponent(
    componentType: string,
    props: Record<string, unknown>,
    dataSourceRef?: string,
  ): ResolvedComponent {
    // 从注册表获取组件（优先精确匹配，支持降级）
    const component = widgetRegistry.get(componentType) ?? widgetRegistry.findFallback(componentType) ?? null

    // 解析数据源
    let resolvedDataSource: ResolvedDataSource | undefined
    if (dataSourceRef) {
      try {
        const ref: DataSourceRef = parseDataSourceRef(dataSourceRef)
        resolvedDataSource = resolveDataSource(ref)
      } catch (e) {
        console.warn(`[CompositionEngine] 数据源解析失败: ${dataSourceRef}`, e)
      }
    }

    return {
      type: componentType,
      component,
      props,
      resolvedDataSource,
    }
  }

  /**
   * 判断节点是否为组合模式
   *
   * @param node - 原始组合节点
   * @returns 是否为组合模式
   */
  isComposite(node: CompositionNode): boolean {
    return !!(node.children && node.children.length > 0 && node.layout)
  }

  /**
   * 判断节点是否为单体模式
   *
   * @param node - 原始组合节点
   * @returns 是否为单体模式
   */
  isSingle(node: CompositionNode): boolean {
    return !this.isComposite(node) && !!node.component
  }

  /**
   * 获取布局类型列表
   *
   * @returns 所有支持的布局类型
   */
  getSupportedLayouts(): LayoutType[] {
    return ['split-horizontal', 'split-vertical', 'tabs', 'grid', 'stack']
  }
}

/** 组合引擎单例 */
export const compositionEngine = new CompositionEngine()

export default compositionEngine
