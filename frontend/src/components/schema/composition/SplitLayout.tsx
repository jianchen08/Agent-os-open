/**
 * 分割布局组件
 *
 * 支持水平分割（左右分栏）和垂直分割（上下分栏），
 * 支持自定义比例分配和拖拽调整大小。
 *
 * @module composition/SplitLayout
 */

import React from 'react'
import { Splitter } from 'antd'

/** 分割布局属性 */
interface SplitLayoutProps {
  /** 分割方向 */
  direction: 'horizontal' | 'vertical'
  /** 布局参数 */
  layoutProps?: {
    /** 默认尺寸比例（如 [3, 7] 表示 30% / 70%），拖拽后可改变 */
    ratio?: number[]
    /** 已废弃但保留兼容 */
    defaultTab?: number
    /** 已废弃但保留兼容 */
    columns?: number
    /** 最小百分比（默认 15），所有面板共享 */
    minRatio?: number
    /** 是否可拖动调整（默认 true），false 回退到固定 flex 模式 */
    resizable?: boolean
  }
  /** 子元素 */
  children: React.ReactNode
}

/**
 * 分割布局组件
 *
 * 水平分割时子节点左右排列，垂直分割时上下排列。
 * 默认使用 antd Splitter 支持拖拽调整大小，
 * 可通过 layoutProps.resizable = false 回退到固定 flex 模式。
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
  const minPercent = layoutProps?.minRatio ?? 15
  const resizable = layoutProps?.resizable ?? true

  // 不可拖动时回退到原始 CSS flex 模式
  if (!resizable) {
    const isHorizontal = direction === 'horizontal'
    return (
      <div
        className={`h-full w-full ${isHorizontal ? 'flex' : 'flex flex-col'}`}
        style={{ gap: 4 }}
      >
        {childArray.map((child, i) => {
          const flexValue = (ratio[i] ?? 1) / totalRatio
          return (
            <div key={i} className="overflow-auto" style={{ flex: flexValue }}>
              {child}
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <Splitter layout={direction} className="h-full w-full">
      {childArray.map((child, i) => (
        <Splitter.Panel
          key={i}
          defaultSize={`${Math.round((ratio[i] / totalRatio) * 100)}%`}
          min={`${minPercent}%`}
          resizable={resizable}
          collapsible={false}
        >
          <div className="h-full">{child}</div>
        </Splitter.Panel>
      ))}
    </Splitter>
  )
}

export default SplitLayout
