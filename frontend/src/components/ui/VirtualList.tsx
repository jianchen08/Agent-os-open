/**
 * 虚拟滚动组件
 *
 * 用于优化长列表的性能，只渲染可见区域的项目
 * 减少DOM节点数量，提升滚动性能
 */

import { useRef, useState, useCallback } from 'react'

/**
 * 虚拟列表组件属性
 */
export interface VirtualListProps<T> {
  /** 列表数据 */
  items: T[]
  /** 单个项目高度（像素） */
  itemHeight: number
  /** 容器高度（像素） */
  containerHeight: number
  /** 渲染项目函数 */
  renderItem: (_item: T, index: number) => React.ReactNode
  /** 项目唯一标识 */
  getItemKey?: (item: T, index: number) => string | number
  /** 额外的缓冲区大小（像素） */
  overscan?: number
  /** 自定义类名 */
  className?: string
}

/**
 * 虚拟列表组件
 *
 * 适用于长列表场景，如任务列表、消息列表等
 * 只渲染可见区域的项目，大幅减少DOM节点数量
 */
export function VirtualList<T>({
  items,
  itemHeight,
  containerHeight,
  renderItem,
  getItemKey,
  overscan = 3,
  className,
}: VirtualListProps<T>) {
  const [scrollTop, setScrollTop] = useState(0)
  const containerRef = useRef<HTMLDivElement>(null)

  // 计算可见范围
  const visibleStart = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan)
  const visibleEnd = Math.min(
    items.length,
    Math.ceil((scrollTop + containerHeight) / itemHeight) + overscan,
  )

  const visibleItems = items.slice(visibleStart, visibleEnd)
  const totalHeight = items.length * itemHeight
  const offsetY = visibleStart * itemHeight

  // 处理滚动事件
  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop)
  }, [])

  // 默认的key生成函数
  const defaultGetKey = useCallback(
    (_item: T, index: number) => {
      return visibleStart + index
    },

    [visibleStart],
  )

  const getKey = getItemKey || defaultGetKey

  return (
    <div
      ref={containerRef}
      className={className}
      style={{
        height: `${containerHeight}px`,
        overflow: 'auto',
      }}
      onScroll={handleScroll}
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        <div style={{ transform: `translateY(${offsetY}px)` }}>
          {visibleItems.map((item, index) => (
            <div
              key={getKey(item, visibleStart + index)}
              style={{
                height: `${itemHeight}px`,
              }}
              data-index={visibleStart + index}
            >
              {renderItem(item, visibleStart + index)}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/**
 * 动态高度的虚拟列表组件属性
 */
export interface VirtualListDynamicProps<T> {
  /** 列表数据 */
  items: T[]
  /** 容器高度（像素） */
  containerHeight: number
  /** 渲染项目函数 */
  renderItem: (item: T, index: number) => React.ReactNode
  /** 测量项目高度函数 */
  estimateItemHeight: (item: T, index: number) => number
  /** 项目唯一标识 */
  getItemKey?: (item: T, index: number) => string | number
  /** 额外的缓冲区大小（像素） */
  overscan?: number
  /** 自定义类名 */
  className?: string
}

/**
 * 动态高度的虚拟列表
 *
 * 适用于项目高度不固定的情况
 * 会动态测量每个项目的实际高度
 */
export function VirtualListDynamic<T>({
  items,
  containerHeight,
  renderItem,
  estimateItemHeight,
  getItemKey,
  overscan = 100,
  className,
}: VirtualListDynamicProps<T>) {
  const [scrollTop, setScrollTop] = useState(0)
  const [heights, setHeights] = useState<Record<number, number>>({})
  const containerRef = useRef<HTMLDivElement>(null)

  // 计算可见范围
  let currentTop = 0
  const visibleStart = (() => {
    for (let i = 0; i < items.length; i++) {
      const height = heights[i] ?? estimateItemHeight(items[i], i)
      if (currentTop + height > scrollTop - overscan) {
        return i
      }
      currentTop += height
    }
    return items.length
  })()

  let totalHeight = 0
  const visibleEnd = (() => {
    for (let i = visibleStart; i < items.length; i++) {
      const height = heights[i] ?? estimateItemHeight(items[i], i)
      totalHeight += height
      if (totalHeight > containerHeight + overscan * 2) {
        return i + 1
      }
    }
    return items.length
  })()

  // 计算偏移量
  let offsetY = 0
  for (let i = 0; i < visibleStart; i++) {
    offsetY += heights[i] ?? estimateItemHeight(items[i], i)
  }

  const visibleItems = items.slice(visibleStart, visibleEnd)

  // 处理滚动事件
  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop)
  }, [])

  // 测量项目高度
  const measureItem = useCallback((index: number, height: number) => {
    setHeights((prev) => {
      if (prev[index] !== height) {
        return { ...prev, [index]: height }
      }
      return prev
    })
  }, [])

  // 默认的key生成函数
  const defaultGetKey = useCallback((_: T, index: number) => {
    return index
  }, [])

  const getKey = getItemKey || defaultGetKey

  return (
    <div
      ref={containerRef}
      className={className}
      style={{
        height: `${containerHeight}px`,
        overflow: 'auto',
      }}
      onScroll={handleScroll}
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        <div style={{ transform: `translateY(${offsetY}px)` }}>
          {visibleItems.map((item, index) => {
            const actualIndex = visibleStart + index
            return (
              <div
                key={getKey(item, actualIndex)}
                data-index={actualIndex}
                ref={(el) => {
                  if (el) {
                    const height = el.getBoundingClientRect().height
                    if (height !== heights[actualIndex]) {
                      measureItem(actualIndex, height)
                    }
                  }
                }}
              >
                {renderItem(item, actualIndex)}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
