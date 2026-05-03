/**
 * 标签页布局组件
 *
 * 将子节点以标签页形式展示，支持默认选中标签。
 *
 * @module composition/TabLayout
 */

import React, { useState } from 'react'

/** 标签页元数据 */
interface TabMeta {
  title?: string
  icon?: string
}

/** 标签页布局属性 */
interface TabLayoutProps {
  /** 布局参数 */
  layoutProps?: {
    ratio?: number[]
    defaultTab?: number
    columns?: number
  }
  /** 标签页元数据数组 */
  tabs?: TabMeta[]
  /** 子元素 */
  children: React.ReactNode
}

/**
 * 标签页布局组件
 *
 * 子节点以标签页形式展示，点击标签切换内容。
 * 支持默认选中标签和标签图标。
 *
 * @param props - 布局属性
 * @returns 标签页布局 JSX
 */
export function TabLayout({
  layoutProps,
  tabs = [],
  children,
}: TabLayoutProps): React.ReactNode {
  const childArray = React.Children.toArray(children)
  const defaultTab = layoutProps?.defaultTab ?? 0
  const [activeTab, setActiveTab] = useState(
    Math.min(defaultTab, childArray.length - 1),
  )

  const safeActiveTab = Math.min(activeTab, childArray.length - 1)

  return (
    <div className="flex h-full w-full flex-col">
      {/* 标签栏 */}
      <div className="border-border flex shrink-0 border-b">
        {childArray.map((_, i) => {
          const meta = tabs[i]
          const isActive = i === safeActiveTab
          return (
            <button
              key={i}
              onClick={() => setActiveTab(i)}
              className={`cursor-pointer border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                isActive
                  ? 'border-primary text-foreground'
                  : 'text-muted-foreground border-transparent hover:text-foreground'
              }`}
            >
              {meta?.icon && <span className="mr-1.5">{meta.icon}</span>}
              {meta?.title ?? `Tab ${i + 1}`}
            </button>
          )
        })}
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-auto">
        {childArray[safeActiveTab] ?? null}
      </div>
    </div>
  )
}

export default TabLayout
