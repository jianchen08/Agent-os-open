/**
 * 堆叠布局组件
 *
 * 将子节点垂直堆叠，每个子节点均分可用空间。
 *
 * @module composition/StackLayout
 */

import React from 'react'

/** 堆叠布局属性 */
interface StackLayoutProps {
  /** 子元素 */
  children: React.ReactNode
}

/**
 * 堆叠布局组件
 *
 * 子节点垂直堆叠，使用 flex-1 使每个子节点均分可用空间。
 * 适用于内容区块的纵向排列。
 *
 * @param props - 布局属性
 * @returns 堆叠布局 JSX
 */
export function StackLayout({ children }: StackLayoutProps): React.ReactNode {
  return (
    <div className="flex h-full w-full flex-col" style={{ gap: 4 }}>
      {React.Children.map(children, (child, i) => (
        <div key={i} className="flex-1 overflow-auto">
          {child}
        </div>
      ))}
    </div>
  )
}

export default StackLayout
