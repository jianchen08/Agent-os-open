/**
 * Schema 组合引擎
 *
 * 解析 LayoutNode 递归树，使用 5 种布局模板组合渲染工作区 UI
 * 实现原则 P11 Level 1：组合式生长
 */

import React from 'react'

/** 布局节点定义 */
export interface LayoutNode {
  layout?: 'split-horizontal' | 'split-vertical' | 'tabs' | 'grid' | 'stack'
  children?: LayoutNode[]
  ratio?: number[]
  default_tab?: number
  columns?: number
  component?: string
  data_source?: string
  params?: Record<string, unknown>
  polling?: number
  title?: string
  icon?: string
  actions?: string[]
  [key: string]: unknown
}

/** 组件渲染器函数类型 */
export type ComponentRenderer = (
  componentName: string,
  props: Record<string, unknown>,
) => React.ReactNode

// 组件注册表单例：统一收敛至 WidgetRegistry.ts（消息渲染管道与工作区渲染共用同一单例）
export { widgetRegistry } from './WidgetRegistry'

/**
 * 渲染布局节点
 *
 * 根据节点类型递归渲染布局：有子节点时按 layout 类型分发，
 * 无子节点但有 component 时直接渲染组件
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 渲染结果
 */
export function renderLayoutNode(
  node: LayoutNode,
  renderComponent: ComponentRenderer,
): React.ReactNode {
  if (node.children && node.children.length > 0) {
    switch (node.layout) {
      case 'split-horizontal':
        return renderSplitHorizontal(node, renderComponent)
      case 'split-vertical':
        return renderSplitVertical(node, renderComponent)
      case 'tabs':
        return renderTabs(node, renderComponent)
      case 'grid':
        return renderGrid(node, renderComponent)
      case 'stack':
      default:
        return renderStack(node, renderComponent)
    }
  }

  if (node.component) {
    return renderComponent(node.component, {
      data_source: node.data_source,
      params: node.params,
      polling: node.polling,
      ...node,
    })
  }

  return <div className="text-muted-foreground p-4 text-sm">空节点</div>
}

/**
 * 渲染水平分割布局
 *
 * 子节点水平排列，支持自定义比例分配
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 水平分割布局 JSX
 */
function renderSplitHorizontal(
  node: LayoutNode,
  renderComponent: ComponentRenderer,
): React.ReactNode {
  const children = node.children ?? []
  const ratio = node.ratio ?? children.map(() => 1 / children.length)

  return (
    <div className="flex h-full w-full" style={{ gap: 1 }}>
      {children.map((child, i) => (
        <div key={i} style={{ flex: ratio[i] ?? 1, overflow: 'auto' }}>
          {renderLayoutNode(child, renderComponent)}
        </div>
      ))}
    </div>
  )
}

/**
 * 渲染垂直分割布局
 *
 * 子节点垂直排列，支持自定义比例分配
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 垂直分割布局 JSX
 */
function renderSplitVertical(
  node: LayoutNode,
  renderComponent: ComponentRenderer,
): React.ReactNode {
  const children = node.children ?? []
  const ratio = node.ratio ?? children.map(() => 1 / children.length)

  return (
    <div className="flex h-full w-full flex-col" style={{ gap: 1 }}>
      {children.map((child, i) => (
        <div key={i} style={{ flex: ratio[i] ?? 1, overflow: 'auto' }}>
          {renderLayoutNode(child, renderComponent)}
        </div>
      ))}
    </div>
  )
}

/**
 * 渲染标签页布局
 *
 * 子节点以标签页形式展示，支持默认选中标签
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 标签页布局 JSX
 */
function renderTabs(node: LayoutNode, renderComponent: ComponentRenderer): React.ReactNode {
  const children = node.children ?? []
  const defaultTab = node.default_tab ?? 0

  return (
    <div className="flex h-full w-full flex-col">
      <div className="border-border flex border-b">
        {children.map((child, i) => (
          <div
            key={i}
            className={`cursor-pointer border-b-2 px-3 py-2 text-sm ${
              i === defaultTab
                ? 'border-primary text-foreground'
                : 'text-muted-foreground border-transparent'
            }`}
          >
            {child.icon && <span className="mr-1">{child.icon}</span>}
            {child.title ?? `Tab ${i + 1}`}
          </div>
        ))}
      </div>
      <div className="flex-1 overflow-auto">
        {children[defaultTab] && renderLayoutNode(children[defaultTab], renderComponent)}
      </div>
    </div>
  )
}

/**
 * 渲染网格布局
 *
 * 子节点以等宽网格排列，支持自定义列数
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 网格布局 JSX
 */
function renderGrid(node: LayoutNode, renderComponent: ComponentRenderer): React.ReactNode {
  const columns = node.columns ?? 2
  const children = node.children ?? []

  return (
    <div
      className="grid h-full w-full"
      style={{ gridTemplateColumns: `repeat(${columns}, 1fr)`, gap: 1 }}
    >
      {children.map((child, i) => (
        <div key={i} className="overflow-auto">
          {renderLayoutNode(child, renderComponent)}
        </div>
      ))}
    </div>
  )
}

/**
 * 渲染堆叠布局
 *
 * 子节点垂直堆叠，每个子节点均分空间
 *
 * @param node - 布局节点
 * @param renderComponent - 组件渲染器回调
 * @returns 堆叠布局 JSX
 */
function renderStack(node: LayoutNode, renderComponent: ComponentRenderer): React.ReactNode {
  const children = node.children ?? []

  return (
    <div className="flex h-full w-full flex-col" style={{ gap: 1 }}>
      {children.map((child, i) => (
        <div key={i} className="flex-1 overflow-auto">
          {renderLayoutNode(child, renderComponent)}
        </div>
      ))}
    </div>
  )
}
