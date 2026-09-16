/**
 * 元素可见性判定（renderer 内存/负载优化：离屏暂停的数据源）
 *
 * 可见 = 元素与视口相交 且 document 未被隐藏。两类不可见各自独立跟踪：
 * - IntersectionObserver：工作区面板非激活 tab 用 display:none 保活（WorkspacePanel
 *   visited 懒挂载策略），元素无盒 → 不相交，隐藏面板的轮询/重渲染据此暂停；
 * - document.visibilityState：窗口最小化/遮挡。
 *
 * ref 用回调式且 observer 生命周期全在回调内管理（不经 state）：宿主可在
 * "占位态 ↔ 内容态"间切换元素（如图表空态无根 div），元素交换后自动重挂
 * observer；同时不因挂 ref 产生额外渲染（计时敏感的宿主依赖此不变量）。
 *
 * fail-open：环境无 IntersectionObserver（jsdom/老内核）或元素未挂载时保持
 * "可见"——暂停是优化，任何环境下都不因误判不可见而停止供数。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export interface ElementVisibleResult<T extends HTMLElement> {
  /** 绑定到被测元素（挂到面板/组件根节点） */
  ref: (node: T | null) => void
  /** 当前是否可见（首次 IO 回调前按可见处理） */
  visible: boolean
}

export function useElementVisible<T extends HTMLElement>(): ElementVisibleResult<T> {
  const [intersecting, setIntersecting] = useState(true)
  const [docVisible, setDocVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  )
  const ioRef = useRef<IntersectionObserver | null>(null)

  const ref = useCallback((node: T | null) => {
    ioRef.current?.disconnect()
    ioRef.current = null
    if (node && typeof IntersectionObserver === 'function') {
      const io = new IntersectionObserver((entries) => {
        // 无目标不触发（与真实 IO 一致：未观察任何元素时不会回调）
        if (entries.length === 0) return
        setIntersecting(entries.some((e) => e.isIntersecting))
      })
      io.observe(node)
      ioRef.current = io
    }
  }, [])

  useEffect(() => {
    const onVis = () => setDocVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  return { ref, visible: intersecting && docVisible }
}
