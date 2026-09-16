/**
 * 纵向等高列表的可视窗口计算（自研最小虚拟滚动，renderer 内存优化项 1）
 *
 * 监听滚动容器（SessionList 自持滚动容器）的 scroll 事件，按等高步距（stride）
 * 换算可视行区间 ±overscan 缓冲；列表项以占位高度撑出完整滚动高度（滚动条
 * 长度不因窗口化缩水），渲染时仅挂载区间内条目。
 *
 * fail-open：容器无布局尺寸（clientHeight≤0，如 jsdom/隐藏未布局）时返回全量
 * 区间——窗口化是优化，任何环境下不因测量缺失而丢内容。
 */
import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import type { RefObject } from 'react'

export interface VirtualWindowRange {
  start: number
  end: number
}

export interface VirtualWindowOptions {
  /** 滚动容器 */
  containerRef: RefObject<HTMLElement | null>
  /** 窗口化列表占位元素（测其在容器内的纵向偏移） */
  listRef: RefObject<HTMLElement | null>
  /** 列表条目总数 */
  total: number
  /** 行步距（等高条目高度，px） */
  stride: number
  /** 上下缓冲行数 */
  overscan?: number
  /** 是否启用窗口化（false 时恒全量） */
  enabled: boolean
}

export function useVirtualWindow({
  containerRef,
  listRef,
  total,
  stride,
  overscan = 8,
  enabled,
}: VirtualWindowOptions): VirtualWindowRange {
  const [range, setRange] = useState<VirtualWindowRange>(() => ({ start: 0, end: total }))

  const compute = useCallback(() => {
    if (!enabled) {
      setRange({ start: 0, end: total })
      return
    }
    const container = containerRef.current
    if (!container || container.clientHeight <= 0) {
      setRange({ start: 0, end: total })
      return
    }
    const listEl = listRef.current
    const offsetTop = listEl
      ? listEl.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop
      : 0
    const relative = container.scrollTop - offsetTop
    const start = Math.max(0, Math.floor(relative / stride) - overscan)
    const end = Math.min(total, Math.ceil((relative + container.clientHeight) / stride) + overscan)
    setRange({ start, end: Math.max(end, start) })
  }, [containerRef, listRef, total, stride, overscan, enabled])

  // 数据面变化（总数/步距/启停）即重算，不等下一次 scroll
  useLayoutEffect(() => {
    compute()
  }, [compute])

  useEffect(() => {
    const container = containerRef.current
    if (!container || !enabled) return
    container.addEventListener('scroll', compute, { passive: true })
    return () => container.removeEventListener('scroll', compute)
  }, [containerRef, enabled, compute])

  return range
}
