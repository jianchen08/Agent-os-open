/**
 * 分割布局组件
 *
 * 支持水平分割（左右分栏）和垂直分割（上下分栏），
 * 支持自定义比例分配。
 *
 * @module composition/SplitLayout
 */

import React from 'react'

/** 分割布局属性 */
interface SplitLayoutProps {
  /** 分割方向 */
  direction: 'horizontal' | 'vertical'
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
 * 分割布局组件
 *
 * 水平分割时子节点左右排列，垂直分割时上下排列。
 * 通过 ratio 属性控制各子节点的空间占比。
 *
 * @param props - 布局属性
 * @returns 分割布局 JSX
 */
export function SplitLayout({
  direction,
  layoutProps,
  children,
}: SplitLayoutProps): React.ReactNode {
  const childArray = React.Children.toArray(children)
  const ratio = layoutProps?.ratio ?? childArray.map(() => 1)
  const totalRatio = ratio.reduce((sum, r) => sum + r, 0) || 1

  const isHorizontal = direction === 'horizontal'

  return (
    <div
      className={`h-full w-full ${isHorizontal ? 'flex' : 'flex flex-col'}`}
      style={{ gap: 4 }}
    >
      {childArray.map((child, i) => {
        const flexValue = (ratio[i] ?? 1) / totalRatio
        return (
          <div
            key={i}
            className="overflow-auto"
            style={{ flex: flexValue }}
          >
            {child}
          </div>
        )
      })}
    </div>
  )
}

export default SplitLayout
