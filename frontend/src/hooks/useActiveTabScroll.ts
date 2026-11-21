/**
 * useActiveTabScroll — 激活标签滚动可见（两条标签栏共用）
 *
 * 标签条横向可滚，窄幅视口（移动端）或标签多时激活签可能被裁在条外——
 * 激活变化时把激活签滚回可见区，标签条反馈与内容保持一致。`block/inline:
 * nearest` 保证横向对齐且不牵动纵向滚动。
 *
 * 用法：`const activeTabRef = useActiveTabScroll<HTMLDivElement>(activeTabId)`，
 * 再把 ref 挂到 `tab.isActive ? activeTabRef : undefined` 的那一个标签上。
 */

import { useEffect, useRef } from 'react'

export function useActiveTabScroll<T extends HTMLElement>(activeTabId: string | null | undefined) {
  const activeTabRef = useRef<T | null>(null)

  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeTabId])

  return activeTabRef
}
