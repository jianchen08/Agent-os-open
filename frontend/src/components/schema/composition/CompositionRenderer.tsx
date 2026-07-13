/**
 * 组合渲染入口组件
 *
 * 接收 CompositionEngine 解析后的 ResolvedNode 树，
 * 递归渲染布局和组件。
 *
 * @module composition/CompositionRenderer
 */

import React from 'react'
import { GridLayout } from './GridLayout'
import { SplitLayout } from './SplitLayout'
import { StackLayout } from './StackLayout'
import { TabLayout } from './TabLayout'
import type { ResolvedNode, ResolvedComponent } from '@/services/schema/CompositionEngine'

/**
 * 组合渲染器属性
 */
interface CompositionRendererProps {
  /** 已解析的组合节点 */
  node: ResolvedNode
  /** 自定义组件渲染器（可选，用于注入数据获取逻辑） */
  componentRenderer?: (component: ResolvedComponent) => React.ReactNode
}

/**
 * 组合渲染入口组件
 *
 * 递归渲染 ResolvedNode 树，根据 mode 分发到对应布局或组件。
 *
 * @param props - 渲染器属性
 * @returns 渲染结果
 */
export function CompositionRenderer({
  node,
  componentRenderer,
}: CompositionRendererProps): React.ReactNode {
  if (!node) return null

  if (node.mode === 'composite' && node.layout && node.children) {
    switch (node.layout) {
      case 'split-horizontal':
        return (
          <SplitLayout direction="horizontal" layoutProps={node.layoutProps}>
            {node.children.map((child, i) => (
              <CompositionRenderer
                key={i}
                node={child}
                componentRenderer={componentRenderer}
              />
            ))}
          </SplitLayout>
        )

      case 'split-vertical':
        return (
          <SplitLayout direction="vertical" layoutProps={node.layoutProps}>
            {node.children.map((child, i) => (
              <CompositionRenderer
                key={i}
                node={child}
                componentRenderer={componentRenderer}
              />
            ))}
          </SplitLayout>
        )

      case 'tabs':
        return (
          <TabLayout
            layoutProps={node.layoutProps}
            tabs={node.children.map((child) => ({
              title: child.tabMeta?.title,
              icon: child.tabMeta?.icon,
            }))}
          >
            {node.children.map((child, i) => (
              <CompositionRenderer
                key={i}
                node={child}
                componentRenderer={componentRenderer}
              />
            ))}
          </TabLayout>
        )

      case 'grid':
        return (
          <GridLayout layoutProps={node.layoutProps}>
            {node.children.map((child, i) => (
              <CompositionRenderer
                key={i}
                node={child}
                componentRenderer={componentRenderer}
              />
            ))}
          </GridLayout>
        )

      case 'stack':
        return (
          <StackLayout>
            {node.children.map((child, i) => (
              <CompositionRenderer
                key={i}
                node={child}
                componentRenderer={componentRenderer}
              />
            ))}
          </StackLayout>
        )

      default:
        return (
          <div className="text-muted-foreground p-4 text-sm">
            未知布局类型: {node.layout}
          </div>
        )
    }
  }

  // 单体模式：渲染组件
  if (node.mode === 'single' && node.component) {
    if (componentRenderer) {
      return componentRenderer(node.component)
    }
    return <WidgetRenderer resolved={node.component} />
  }

  // 空节点
  return (
    <div className="text-muted-foreground flex items-center justify-center p-8 text-sm">
      空节点
    </div>
  )
}

/**
 * Widget 组件渲染器
 *
 * 渲染单个已解析的 Widget 组件。
 *
 * @param props - 包含已解析组件信息的属性
 * @returns 组件渲染结果
 */
function WidgetRenderer({ resolved }: { resolved: ResolvedComponent }): React.ReactNode {
  const { component, props, type } = resolved

  if (!component) {
    return (
      <div className="text-muted-foreground flex flex-col items-center justify-center rounded-lg border border-dashed p-6">
        <svg
          className="mb-2 h-8 w-8"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <p className="text-sm">组件未注册: {type}</p>
      </div>
    )
  }

  // 合并数据源信息到 props
  const mergedProps: Record<string, unknown> = {
    ...props,
    ...(resolved.resolvedDataSource
      ? { _dataSource: resolved.resolvedDataSource }
      : {}),
    ...(resolved.polling ? { _polling: resolved.polling } : {}),
  }

  return React.createElement(component, mergedProps)
}

export default CompositionRenderer
