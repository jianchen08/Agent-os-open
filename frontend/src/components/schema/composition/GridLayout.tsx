/**
 * 网格布局组件
 *
 * 将子节点以等宽网格排列，支持自定义列数。
 *
 * @module composition/GridLayout
 */

import React from 'react'

/** 网格布局属性 */
interface GridLayoutProps {
  /** 布局参数 */
  layoutProps?: {
    ratio?: number[]
    defaultTab?: number
    columns?: number
  }
  /** 子元素 */
  children: React.ReactNode
}

/**
 * 网格布局组件
 *
 * 子节点以等宽网格排列，列数可配置（默认 2 列）。
 * 使用 CSS Grid 实现，自适应间距。
 *
 * @param props - 布局属性
 * @returns 网格布局 JSX
 */
export function GridLayout({
  layoutProps,
  children,
}: GridLayoutProps): React.ReactNode {
  const columns = layoutProps?.columns ?? 2

  return (
    <div
      className="grid h-full w-full"
      style={{
        gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
        gap: 4,
      }}
    >
      {React.Children.map(children, (child, i) => (
        <div key={i} className="overflow-auto">
          {child}
        </div>
      ))}
    </div>
  )
}

export default GridLayout
